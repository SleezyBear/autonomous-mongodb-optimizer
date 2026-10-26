"""Operator-facing read models and onboarding actions for the control plane.

These routes complement the generic resource API with what an operator needs
to actually run the optimizer: target readiness, evaluation mapping, namespace
allowlisting, a dashboard overview, an approval inbox, resuming stalled runs,
and ledger verification.  None of them accepts or returns a credential.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.routes import _authorized_target, _record, connection_kind, require_known_label, require_unique_target_name, worker_connection_roles
from app.autonomy.policy import DeploymentMode
from app.auth.routes import current_principal
from app.auth.security import AuthorizationError, Principal, Role, require_role
from app.db import models
from app.ledger.postgres import PostgresProductionLedger
from app.settings_store import TELEMETRY_CURSOR_SCOPE, TELEMETRY_STATUS_SCOPE, WORKER_SCOPE, WORKER_STATUS_KEY, delete_setting, read_scope, read_setting
from app.workloads.durable import MAX_SOURCE_AGE_SECONDS


router = APIRouter(prefix="/api/v1")
_SOURCE_NAMES = {"QUERY_STATS": "query statistics", "PROFILER": "profiler", "DIAGNOSTIC_LOG": "slow-query log", "CURRENT_OP": "live operation samples"}
TERMINAL_STATUSES = frozenset({"COMPLETED", "ROLLED_BACK", "ROLLBACK_BLOCKED", "FAILED"})
WORKER_STALE_SECONDS = 60


class EvaluationMappingRequest(BaseModel):
    evaluation_target_id: UUID


class AllowlistRequest(BaseModel):
    allowlisted: bool


class TargetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    connection_label: str | None = Field(default=None, min_length=1, max_length=256)
    deployment_mode: DeploymentMode | None = None


def _require_operator(principal: Principal) -> None:
    try:
        require_role(principal, Role.OPERATOR, Role.ADMIN)
    except AuthorizationError as error:
        raise HTTPException(status_code=403, detail="Operator permission is required.") from error


def _engine(request: Request) -> Any:
    return request.app.state.control_plane.runs._engine


def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


async def worker_status(request: Request) -> dict[str, Any]:
    """The worker's published, non-secret heartbeat with a liveness verdict."""
    status = await read_setting(_engine(request), WORKER_SCOPE, WORKER_STATUS_KEY)
    if status is None:
        return {"state": "NEVER_SEEN", "connections": [], "ai": None, "orchestration": "UNKNOWN"}
    heartbeat = status.get("heartbeat_at")
    age = None
    if isinstance(heartbeat, str):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)).total_seconds()
    status["heartbeat_age_seconds"] = age
    status["state"] = "ONLINE" if age is not None and age <= WORKER_STALE_SECONDS else "STALE"
    return status


async def _target_readiness(connection: AsyncConnection, target: dict[str, Any], worker: dict[str, Any], telemetry: dict[str, Any] | None) -> dict[str, Any]:
    target_id = target["id"]
    mapping = (await connection.execute(select(models.EvaluationMapping.__table__).where(
        models.EvaluationMapping.__table__.c.target_id == target_id,
        models.EvaluationMapping.__table__.c.mapping_status == "ACTIVE",
    ))).mappings().one_or_none()
    evaluation_target = None
    if mapping is not None:
        evaluation_target = (await connection.execute(select(models.Target.__table__).where(models.Target.__table__.c.id == mapping["evaluation_target_id"]))).mappings().one_or_none()
    namespaces = (await connection.execute(select(models.Namespace.__table__).where(models.Namespace.__table__.c.target_id == target_id).order_by(models.Namespace.__table__.c.name))).mappings().all()
    shape_counts = dict((await connection.execute(
        select(models.QueryShape.__table__.c.namespace_id, func.count()).group_by(models.QueryShape.__table__.c.namespace_id)
        .where(models.QueryShape.__table__.c.namespace_id.in_([row["id"] for row in namespaces] or [uuid4()]))
    )).all())
    capability = (await connection.execute(select(models.CapabilitySnapshot.__table__).where(models.CapabilitySnapshot.__table__.c.target_id == target_id).order_by(models.CapabilitySnapshot.__table__.c.created_at.desc()).limit(1))).mappings().one_or_none()
    window = (await connection.execute(select(models.TelemetryWindow.__table__).where(
        models.TelemetryWindow.__table__.c.target_id == target_id, models.TelemetryWindow.__table__.c.status == "COMPLETED",
    ).order_by(models.TelemetryWindow.__table__.c.ended_at.desc()).limit(1))).mappings().one_or_none()
    labels = {str(item.get("label")): item for item in worker.get("connections", []) if isinstance(item, dict)}
    connection_info = labels.get(str(target["connection_label"]))
    evaluation_info = labels.get(str(evaluation_target["connection_label"])) if evaluation_target is not None else None
    telemetry_age = (datetime.now(timezone.utc) - window["ended_at"]).total_seconds() if window is not None else None
    allowlisted = [row for row in namespaces if row["allowlisted"]]
    label = str(target["connection_label"])
    kind = connection_kind(connection_info.get("roles", [])) if connection_info else None
    production_labels = sorted(name for name, item in labels.items() if connection_kind(item.get("roles", [])) == "PRODUCTION")
    suggestion = f" Edit this target and pick {', '.join(production_labels)}." if production_labels else ""
    if connection_info is None:
        connection_detail = f"label '{label}' is not in the worker connection file." + suggestion
    elif kind == "PRODUCTION":
        connection_detail = f"'{label}' can read query data and add or remove indexes"
    else:
        connection_detail = f"label '{label}' only has sandbox credentials, so it cannot be monitored." + suggestion
    evaluation_kind = connection_kind(evaluation_info.get("roles", [])) if evaluation_info else None
    if evaluation_target is None:
        evaluation_detail = "choose the sandbox copy to test changes on (below)"
    elif evaluation_kind != "SANDBOX":
        evaluation_detail = f"{evaluation_target['name']} uses label '{evaluation_target['connection_label']}', which has no sandbox credentials. Map a sandbox target instead."
    else:
        evaluation_detail = "changes are tested on " + str(evaluation_target["name"])
    checks = [
        {"key": "connection", "label": "Connection set up", "ok": kind == "PRODUCTION", "detail": connection_detail},
        {"key": "evaluation", "label": "Sandbox copy chosen", "ok": evaluation_target is not None and evaluation_kind == "SANDBOX", "detail": evaluation_detail},
        {"key": "capability", "label": "Database reached", "ok": capability is not None,
         "detail": f"MongoDB {capability['capabilities'].get('version', '?')}, {str(capability['capabilities'].get('topology', '?')).replace('_', ' ')}" if capability is not None else "waiting for the worker to connect (it checks every telemetry pass)"},
        {"key": "telemetry", "label": "Recent query data", "ok": telemetry_age is not None and telemetry_age <= MAX_SOURCE_AGE_SECONDS,
         "detail": (f"from the {_SOURCE_NAMES.get(str(window['source']), str(window['source']).lower())}, {int(telemetry_age)}s old" if telemetry_age is not None and window is not None else "no slow queries seen yet; run some traffic (locally: make dev-workload)")
         + (" (older than 5 minutes; needs fresh traffic)" if telemetry_age is not None and telemetry_age > MAX_SOURCE_AGE_SECONDS else "")},
        {"key": "allowlist", "label": "Collection allowed", "ok": bool(allowlisted),
         "detail": (f"{len(allowlisted)} of {len(namespaces)} collections may be changed" if namespaces else "no collections seen yet") if allowlisted or not namespaces else "tick at least one collection below to allow changes to it"},
    ]
    return {
        "evaluation_mapping": None if mapping is None else {"evaluation_target_id": str(mapping["evaluation_target_id"]), "evaluation_target_name": evaluation_target["name"] if evaluation_target is not None else None, "status": mapping["mapping_status"]},
        "namespaces": [{"id": str(row["id"]), "name": row["name"], "allowlisted": bool(row["allowlisted"]), "query_shapes": int(shape_counts.get(row["id"], 0))} for row in namespaces],
        "capability": None if capability is None else {"capabilities": capability["capabilities"], "fingerprint": capability["fingerprint"], "observed_at": _iso(capability["created_at"])},
        "telemetry": {"latest_window_at": _iso(window["ended_at"]) if window is not None else None, "source": window["source"] if window is not None else None, "age_seconds": telemetry_age, "fresh_within_seconds": MAX_SOURCE_AGE_SECONDS, "collector": telemetry},
        "connection": connection_info,
        "checks": checks,
        "ready": all(check["ok"] for check in checks),
    }


@router.get("/targets/{target_id}")
async def get_target(target_id: UUID, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    target = await _authorized_target(request, target_id, principal, allow_approver=True)
    worker = await worker_status(request)
    telemetry = await read_setting(_engine(request), TELEMETRY_STATUS_SCOPE, str(target_id))
    async with _engine(request).connect() as connection:
        readiness = await _target_readiness(connection, target, worker, telemetry)
        runs = (await connection.execute(
            select(models.OptimizationRun.__table__.c.id, models.OptimizationRun.__table__.c.status, models.OptimizationRun.__table__.c.completion_reason, models.OptimizationRun.__table__.c.created_at)
            .where(models.OptimizationRun.__table__.c.target_id == target_id)
            .order_by(models.OptimizationRun.__table__.c.created_at.desc()).limit(10)
        )).mappings().all()
        sandbox_for = (await connection.execute(
            select(models.Target.__table__.c.id, models.Target.__table__.c.name)
            .join(models.EvaluationMapping.__table__, models.EvaluationMapping.__table__.c.target_id == models.Target.__table__.c.id)
            .where(models.EvaluationMapping.__table__.c.evaluation_target_id == target_id, models.EvaluationMapping.__table__.c.mapping_status == "ACTIVE", models.Target.__table__.c.is_active.is_(True))
            .order_by(models.Target.__table__.c.name)
        )).mappings().all()
        is_sandbox = len(sandbox_for)
    sandbox_label = connection_kind((readiness["connection"] or {}).get("roles", [])) == "SANDBOX"
    return {
        "target": _record(target),
        "role": "SANDBOX" if is_sandbox or sandbox_label else "MONITORED",
        "mapped_as_sandbox_for": int(is_sandbox),
        "sandbox_for": [{"id": str(row["id"]), "name": row["name"]} for row in sandbox_for],
        **readiness,
        "recent_runs": [{"run_id": str(row["id"]), "status": _value(row["status"]), "completion_reason": _value(row["completion_reason"]), "created_at": _iso(row["created_at"])} for row in runs],
    }


@router.post("/targets/{target_id}/evaluation-mapping")
async def set_evaluation_mapping(target_id: UUID, body: EvaluationMappingRequest, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    _require_operator(principal)
    if body.evaluation_target_id == target_id:
        raise HTTPException(status_code=422, detail="The sandbox target must differ from the monitored target.")
    target = await _authorized_target(request, target_id, principal)
    evaluation = await _authorized_target(request, body.evaluation_target_id, principal)
    if not target["is_active"] or not evaluation["is_active"]:
        raise HTTPException(status_code=409, detail="Both targets must be active.")
    if target["connection_label"] == evaluation["connection_label"]:
        raise HTTPException(status_code=422, detail="The sandbox target must use a different connection than the monitored target.")
    known = await worker_connection_roles(request)
    if known is not None:
        if connection_kind(known.get(str(evaluation["connection_label"]), [])) != "SANDBOX":
            raise HTTPException(status_code=422, detail=f"{evaluation['name']} uses '{evaluation['connection_label']}', which has no sandbox credentials. Pick a target whose label is sandbox-only.")
        if connection_kind(known.get(str(target["connection_label"]), [])) == "SANDBOX":
            raise HTTPException(status_code=422, detail=f"{target['name']} uses the sandbox label '{target['connection_label']}'. Edit it to use a production label before mapping a sandbox.")
    table = models.EvaluationMapping.__table__
    now = datetime.now(timezone.utc)
    async with _engine(request).begin() as connection:
        reverse = await connection.scalar(select(func.count()).select_from(table).where(table.c.target_id == body.evaluation_target_id, table.c.mapping_status == "ACTIVE"))
        if reverse:
            raise HTTPException(status_code=409, detail="The chosen sandbox is itself a monitored target with a mapping.")
        existing = (await connection.execute(select(table).where(table.c.target_id == target_id).with_for_update())).mappings().one_or_none()
        if existing is None:
            await connection.execute(insert(table).values(id=uuid4(), created_at=now, updated_at=now, target_id=target_id, evaluation_target_id=body.evaluation_target_id, mapping_status="ACTIVE"))
        else:
            await connection.execute(update(table).where(table.c.id == existing["id"]).values(evaluation_target_id=body.evaluation_target_id, mapping_status="ACTIVE", updated_at=now))
    await request.app.state.control_plane.audit.create(actor_user_id=UUID(principal.user_id), optimization_run_id=None, event_type="EVALUATION_MAPPING_SET", event_payload={"target_id": str(target_id), "evaluation_target_id": str(body.evaluation_target_id)})
    return {"target_id": str(target_id), "evaluation_target_id": str(body.evaluation_target_id), "status": "ACTIVE"}


async def _active_run_count(connection: AsyncConnection, target_id: UUID) -> int:
    runs = models.OptimizationRun.__table__
    return int(await connection.scalar(select(func.count()).select_from(runs).where(
        runs.c.target_id == target_id, runs.c.status.notin_([models.RunStatus(value) for value in TERMINAL_STATUSES])
    )) or 0)


@router.patch("/targets/{target_id}")
async def update_target(target_id: UUID, body: TargetUpdate, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Rename a target, fix its connection label, or change its deployment mode."""
    _require_operator(principal)
    target = await _authorized_target(request, target_id, principal)
    if not target["is_active"]:
        raise HTTPException(status_code=409, detail="This target is archived.")
    values: dict[str, Any] = {}
    if body.name is not None and body.name.strip() != target["name"]:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Name is required.")
        await require_unique_target_name(request, principal.user_id, name, exclude=target_id)
        values["name"] = name
    label_changed = body.connection_label is not None and body.connection_label.strip() != target["connection_label"]
    if label_changed:
        label = str(body.connection_label).strip()
        await require_known_label(request, label)
        values["connection_label"] = label
    if body.deployment_mode is not None and body.deployment_mode.value != target["deployment_mode"]:
        values["deployment_mode"] = body.deployment_mode.value
    if not values:
        return _record(target)
    table = models.Target.__table__
    async with _engine(request).begin() as connection:
        if label_changed and await _active_run_count(connection, target_id):
            raise HTTPException(status_code=409, detail="A run is in progress on this target. Wait for it to finish before changing the connection.")
        row = (await connection.execute(update(table).where(table.c.id == target_id).values(**values, updated_at=datetime.now(timezone.utc)).returning(*table.c))).mappings().one()
    if label_changed:
        # Telemetry from the old connection no longer describes this target.
        await delete_setting(_engine(request), TELEMETRY_CURSOR_SCOPE, str(target_id))
        await delete_setting(_engine(request), TELEMETRY_STATUS_SCOPE, str(target_id))
    await request.app.state.control_plane.audit.create(actor_user_id=UUID(principal.user_id), optimization_run_id=None, event_type="TARGET_UPDATED", event_payload={"target_id": str(target_id), "changes": {key: str(value) for key, value in values.items()}})
    return _record(row)


@router.post("/targets/{target_id}/archive")
async def archive_target(target_id: UUID, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Retire a target: it stops being polled, leaves the list, and its name can be reused. History is kept."""
    _require_operator(principal)
    target = await _authorized_target(request, target_id, principal)
    if not target["is_active"]:
        return _record(target)
    targets, mappings = models.Target.__table__, models.EvaluationMapping.__table__
    now = datetime.now(timezone.utc)
    async with _engine(request).begin() as connection:
        if await _active_run_count(connection, target_id):
            raise HTTPException(status_code=409, detail="A run is in progress on this target. Wait for it to finish before archiving.")
        dependants = (await connection.execute(
            select(targets.c.name).join(mappings, mappings.c.target_id == targets.c.id)
            .where(mappings.c.evaluation_target_id == target_id, mappings.c.mapping_status == "ACTIVE", targets.c.is_active.is_(True))
        )).scalars().all()
        if dependants:
            raise HTTPException(status_code=409, detail=f"This is the sandbox for {', '.join(dependants)}. Map a different sandbox there first.")
        await connection.execute(update(mappings).where(mappings.c.target_id == target_id).values(mapping_status="INACTIVE", updated_at=now))
        archived_name = f"{target['name']} (archived {now:%Y-%m-%d %H:%M})"[:128]
        row = (await connection.execute(update(targets).where(targets.c.id == target_id).values(
            is_active=False, state="ARCHIVED", name=archived_name, updated_at=now
        ).returning(*targets.c))).mappings().one()
    await delete_setting(_engine(request), TELEMETRY_STATUS_SCOPE, str(target_id))
    await request.app.state.control_plane.audit.create(actor_user_id=UUID(principal.user_id), optimization_run_id=None, event_type="TARGET_ARCHIVED", event_payload={"target_id": str(target_id), "name": target["name"]})
    return _record(row)


@router.post("/targets/{target_id}/namespaces/{namespace_id}/allowlist")
async def set_namespace_allowlist(target_id: UUID, namespace_id: UUID, body: AllowlistRequest, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    _require_operator(principal)
    await _authorized_target(request, target_id, principal)
    table = models.Namespace.__table__
    async with _engine(request).begin() as connection:
        result = await connection.execute(update(table).where(table.c.id == namespace_id, table.c.target_id == target_id).values(allowlisted=body.allowlisted, updated_at=datetime.now(timezone.utc)).returning(table.c.name))
        name = result.scalar_one_or_none()
    if name is None:
        raise HTTPException(status_code=404, detail="Namespace not found for this target.")
    await request.app.state.control_plane.audit.create(actor_user_id=UUID(principal.user_id), optimization_run_id=None, event_type="NAMESPACE_ALLOWLIST_CHANGED", event_payload={"target_id": str(target_id), "namespace_id": str(namespace_id), "allowlisted": body.allowlisted})
    return {"id": str(namespace_id), "name": name, "allowlisted": body.allowlisted}


@router.get("/connections")
async def list_connections(request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    del principal
    status = await worker_status(request)
    return {"worker_state": status["state"], "connections": status.get("connections", [])}


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: UUID, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Re-queue a non-terminal run whose durable job exhausted its retries."""
    _require_operator(principal)
    runs, jobs = models.OptimizationRun.__table__, models.Job.__table__
    async with _engine(request).begin() as connection:
        run = (await connection.execute(select(runs).where(runs.c.id == run_id).with_for_update())).mappings().one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        await _authorized_target(request, run["target_id"], principal)
        if _value(run["status"]) in TERMINAL_STATUSES or _value(run["status"]) == "APPROVAL_PENDING":
            raise HTTPException(status_code=409, detail="Only a stalled, non-terminal run can be resumed.")
        job = (await connection.execute(select(jobs).where(jobs.c.optimization_run_id == run_id).order_by(jobs.c.created_at.desc()).limit(1).with_for_update())).mappings().one_or_none()
        if job is None or job["status"] != "FAILED":
            raise HTTPException(status_code=409, detail="The run's job is not in a failed state.")
        now = datetime.now(timezone.utc)
        await connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="PENDING", attempts=0, available_at=now, failed_at=None, lease_owner=None, lease_expires_at=None, updated_at=now))
    await request.app.state.control_plane.audit.create(actor_user_id=UUID(principal.user_id), optimization_run_id=run_id, event_type="RUN_RESUMED", event_payload={"previous_error": job["last_error_code"]})
    return {"run_id": str(run_id), "job_id": str(job["id"]), "status": "PENDING"}


def _value(value: object) -> str | None:
    if value is None:
        return None
    return str(value.value if hasattr(value, "value") else value)


@router.get("/approvals/inbox")
async def approval_inbox(request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    approvals, runs, targets = models.ApprovalRequest.__table__, models.OptimizationRun.__table__, models.Target.__table__
    statement = (
        select(approvals, runs.c.status.label("run_status"), runs.c.deployment_mode, targets.c.name.label("target_name"))
        .join(runs, runs.c.id == approvals.c.optimization_run_id)
        .join(targets, targets.c.id == approvals.c.target_id)
        .order_by(approvals.c.created_at.desc()).limit(100)
    )
    if principal.role not in {Role.ADMIN, Role.APPROVER}:
        statement = statement.where(approvals.c.requested_by_user_id == UUID(principal.user_id))
    async with _engine(request).connect() as connection:
        rows = (await connection.execute(statement)).mappings().all()
        actions = {}
        if rows:
            action_rows = (await connection.execute(select(models.CandidateAction.__table__.c.candidate_id, models.CandidateAction.__table__.c.action_type, models.CandidateAction.__table__.c.action_payload).where(models.CandidateAction.__table__.c.candidate_id.in_([row["candidate_id"] for row in rows])))).mappings().all()
            actions = {row["candidate_id"]: row for row in action_rows}
    now = datetime.now(timezone.utc)
    records = []
    for row in rows:
        action = actions.get(row["candidate_id"])
        expired = row["status"] == "PENDING" and row["expires_at"] <= now
        records.append({
            "id": str(row["id"]), "run_id": str(row["optimization_run_id"]), "target_id": str(row["target_id"]), "target_name": row["target_name"],
            "status": "EXPIRED" if expired else row["status"], "run_status": _value(row["run_status"]), "deployment_mode": row["deployment_mode"],
            "requested_by_user_id": str(row["requested_by_user_id"]), "created_at": _iso(row["created_at"]), "expires_at": _iso(row["expires_at"]),
            "evidence_hash": row["evidence_hash"],
            "action": None if action is None else {"action_type": action["action_type"], "payload": action["action_payload"]},
            "can_decide": row["status"] == "PENDING" and not expired and principal.role in {Role.ADMIN, Role.APPROVER} and str(row["requested_by_user_id"]) != principal.user_id,
        })
    return {"records": records, "pending": sum(record["status"] == "PENDING" for record in records)}


@router.get("/ledger/verify")
async def verify_ledger(request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    async with _engine(request).connect() as connection:
        statement = select(models.Target.__table__.c.id, models.Target.__table__.c.name)
        if principal.role not in {Role.ADMIN, Role.APPROVER}:
            statement = statement.where(models.Target.__table__.c.owner_user_id == UUID(principal.user_id))
        targets = (await connection.execute(statement)).mappings().all()
        from sqlalchemy import text

        counts = dict((await connection.execute(text("SELECT target_id, count(*) FROM production_ledger_entries GROUP BY target_id"))).all())
    ledger = PostgresProductionLedger(_engine(request))
    results = []
    for target in targets:
        entries = int(counts.get(target["id"], 0))
        if entries == 0:
            continue
        results.append({"target_id": str(target["id"]), "target_name": target["name"], "entries": entries, "verified": await ledger.verify_target(target["id"])})
    return {"targets": results, "verified": all(item["verified"] for item in results)}


@router.get("/overview")
async def overview(request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    runs, jobs, targets, approvals = models.OptimizationRun.__table__, models.Job.__table__, models.Target.__table__, models.ApprovalRequest.__table__
    admin = principal.role is Role.ADMIN
    run_scope = runs.c.requested_by_user_id == UUID(principal.user_id)
    target_scope = targets.c.owner_user_id == UUID(principal.user_id)
    worker = await worker_status(request)
    telemetry = await read_scope(_engine(request), TELEMETRY_STATUS_SCOPE)
    now = datetime.now(timezone.utc)
    async with _engine(request).connect() as connection:
        status_query = select(runs.c.status, func.count()).group_by(runs.c.status)
        if not admin:
            status_query = status_query.where(run_scope)
        by_status = {_value(status): int(count) for status, count in (await connection.execute(status_query)).all()}
        latest_job = (
            select(jobs.c.optimization_run_id, func.max(jobs.c.created_at).label("created_at")).group_by(jobs.c.optimization_run_id).subquery()
        )
        stalled_query = (
            select(runs.c.id, runs.c.status, runs.c.target_id, jobs.c.last_error_code, jobs.c.status.label("job_status"), jobs.c.attempts)
            .join(latest_job, latest_job.c.optimization_run_id == runs.c.id)
            .join(jobs, and_(jobs.c.optimization_run_id == runs.c.id, jobs.c.created_at == latest_job.c.created_at))
            .where(runs.c.status.notin_([models.RunStatus(value) for value in TERMINAL_STATUSES] + [models.RunStatus.APPROVAL_PENDING]))
            .where(or_(jobs.c.status == "FAILED", and_(jobs.c.status == "PENDING", jobs.c.attempts > 0)))
        )
        if not admin:
            stalled_query = stalled_query.where(run_scope)
        stalled = (await connection.execute(stalled_query.limit(20))).mappings().all()
        pending_query = select(func.count()).select_from(approvals).where(approvals.c.status == "PENDING", approvals.c.expires_at > now)
        if principal.role not in {Role.ADMIN, Role.APPROVER}:
            pending_query = pending_query.where(approvals.c.requested_by_user_id == UUID(principal.user_id))
        pending = int(await connection.scalar(pending_query) or 0)
        recent_query = (
            select(runs.c.id, runs.c.status, runs.c.completion_reason, runs.c.deployment_mode, runs.c.created_at, runs.c.updated_at, targets.c.name.label("target_name"))
            .join(targets, targets.c.id == runs.c.target_id).order_by(runs.c.updated_at.desc().nullslast(), runs.c.created_at.desc()).limit(8)
        )
        if not admin:
            recent_query = recent_query.where(run_scope)
        recent = (await connection.execute(recent_query)).mappings().all()
        target_query = select(targets).where(targets.c.is_active.is_(True)).order_by(targets.c.name)
        if not admin and principal.role is not Role.APPROVER:
            target_query = target_query.where(target_scope)
        target_rows = (await connection.execute(target_query)).mappings().all()
        sandbox_ids = set((await connection.execute(select(models.EvaluationMapping.__table__.c.evaluation_target_id).where(models.EvaluationMapping.__table__.c.mapping_status == "ACTIVE"))).scalars().all())
        target_cards = []
        for target in target_rows:
            if target["id"] in sandbox_ids:
                continue
            readiness = await _target_readiness(connection, dict(target), worker, telemetry.get(str(target["id"])))
            target_cards.append({"id": str(target["id"]), "name": target["name"], "connection_label": target["connection_label"], "deployment_mode": target["deployment_mode"], "ready": readiness["ready"], "checks": readiness["checks"], "telemetry_age_seconds": readiness["telemetry"]["age_seconds"]})
        since = now - timedelta(days=7)
        deployments = int(await connection.scalar(select(func.count()).select_from(models.DeploymentArtifact.__table__).where(models.DeploymentArtifact.__table__.c.created_at >= since)) or 0)
    return {
        "runs_by_status": by_status,
        "active_runs": sum(count for status, count in by_status.items() if status not in TERMINAL_STATUSES),
        "pending_approvals": pending,
        "stalled_runs": [{"run_id": str(row["id"]), "status": _value(row["status"]), "target_id": str(row["target_id"]), "last_error_code": row["last_error_code"], "job_status": row["job_status"], "attempts": row["attempts"]} for row in stalled],
        "recent_runs": [{"run_id": str(row["id"]), "status": _value(row["status"]), "completion_reason": _value(row["completion_reason"]), "deployment_mode": row["deployment_mode"], "target_name": row["target_name"], "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"])} for row in recent],
        "targets": target_cards,
        "deployments_last_7_days": deployments,
        "worker": worker,
        "principal": {"user_id": principal.user_id, "role": principal.role.value},
    }
