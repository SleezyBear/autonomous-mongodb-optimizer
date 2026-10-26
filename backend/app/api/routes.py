"""Persisted, paginated control-plane resource APIs."""

from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from app.approvals.durable import DurableApprovalRequired, DurableApprovalService
from app.auth.routes import current_principal
from app.auth.security import AuthorizationError, Principal, Role, require_approval_authority, require_role
from app.autonomy.policy import DeploymentMode
from app.db import models
from app.runs.service import OptimizationRunCreationService
from app.api.evidence import run_evidence
from app.settings_store import WORKER_SCOPE, WORKER_STATUS_KEY, read_setting


class ApiGroup(str, Enum):
    AUTH = "auth"
    TARGETS = "targets"
    TELEMETRY = "telemetry"
    WORKLOADS = "workloads"
    QUERY_SHAPES = "query-shapes"
    RUNS = "runs"
    CANDIDATES = "candidates"
    EVALUATIONS = "evaluations"
    ADMISSIONS = "admissions"
    APPROVALS = "approvals"
    LEDGER = "ledger"
    ROLLBACKS = "rollbacks"
    EXPERIENCE = "experience"
    BENCHMARKS = "benchmarks"
    SETTINGS = "settings"
    SYSTEM = "system"


class Page(BaseModel):
    group: ApiGroup
    records: list[dict[str, Any]]
    limit: int
    offset: int


class TargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    connection_label: str = Field(min_length=1, max_length=256)
    deployment_mode: DeploymentMode = DeploymentMode.APPROVAL_CONTROLLED


class RunCreate(BaseModel):
    target_id: UUID
    deployment_mode: DeploymentMode = DeploymentMode.APPROVAL_CONTROLLED
    # Calibration requires a primary metric; default to protected p99 latency.
    primary_metric_key: str | None = Field(default="p99_latency_ms", max_length=256)


class RunResponse(BaseModel):
    run_id: UUID
    target_id: UUID
    status: str
    deployment_mode: str
    primary_metric_key: str | None
    completion_reason: str | None
    created_at: datetime
    updated_at: datetime
    target_name: str | None = None
    job_status: str | None = None
    job_attempts: int | None = None
    last_error_code: str | None = None


class ApprovalDecisionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=512)


router = APIRouter(prefix="/api/v1")
_REPOS = {
    ApiGroup.TELEMETRY: "telemetry",
    ApiGroup.WORKLOADS: "workloads",
    ApiGroup.QUERY_SHAPES: "query_shapes",
    ApiGroup.RUNS: "runs",
    ApiGroup.CANDIDATES: "candidates",
    ApiGroup.EVALUATIONS: "evaluations",
    ApiGroup.ADMISSIONS: "admissions",
    ApiGroup.APPROVALS: "approvals",
    ApiGroup.ROLLBACKS: "rollback",
    ApiGroup.EXPERIENCE: "experience",
    ApiGroup.BENCHMARKS: "jobs",
    ApiGroup.SETTINGS: "settings",
}
_PRIMARY_METRICS = frozenset({"p50_latency_ms", "p95_latency_ms", "p99_latency_ms", "throughput_ops_per_second"})


def _record(row: Any) -> dict[str, Any]:
    return {
        key: str(value) if isinstance(value, (UUID, datetime)) else value
        for key, value in dict(row).items()
    }


async def _list(
    group: ApiGroup, request: Request, principal: Principal, limit: int, offset: int
) -> Page:
    if group is ApiGroup.SYSTEM:
        return Page(
            group=group,
            records=[{"status": "ready", "principal_id": principal.user_id}],
            limit=limit,
            offset=offset,
        )
    if group is ApiGroup.LEDGER:
        from sqlalchemy import text

        engine = request.app.state.control_plane.ledger._engine
        scoped = principal.role not in {Role.ADMIN, Role.APPROVER}
        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT l.*, t.name AS target_name FROM production_ledger_entries l "
                            "JOIN targets t ON t.id = l.target_id "
                            + ("WHERE t.owner_user_id = :owner " if scoped else "")
                            + "ORDER BY l.created_at DESC, l.sequence DESC LIMIT :limit OFFSET :offset"
                        ),
                        {"limit": limit, "offset": offset, **({"owner": UUID(principal.user_id)} if scoped else {})},
                    )
                )
                .mappings()
                .all()
            )
        return Page(group=group, records=[_record(row) for row in rows], limit=limit, offset=offset)
    repository = getattr(
        request.app.state.control_plane, "targets" if group is ApiGroup.TARGETS else _REPOS[group]
    )
    rows = await repository.list()
    if group is ApiGroup.TARGETS and principal.role not in {Role.ADMIN, Role.APPROVER}:
        rows = tuple(row for row in rows if str(row["owner_user_id"]) == principal.user_id)
    return Page(
        group=group,
        records=[_record(row) for row in rows[offset : offset + limit]],
        limit=limit,
        offset=offset,
    )


def _add_read_route(group: ApiGroup) -> None:
    async def get_group(
        request: Request,
        principal: Principal = Depends(current_principal),
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(status_code=422, detail="invalid pagination")
        return await _list(group, request, principal, limit, offset)

    router.add_api_route(
        f"/{group.value}",
        get_group,
        methods=["GET"],
        response_model=Page,
        tags=[group.value],
        operation_id=f"list_{group.value.replace('-', '_')}",
    )


for _group in ApiGroup:
    if _group in {ApiGroup.RUNS, ApiGroup.APPROVALS}:
        continue
    _add_read_route(_group)


def _enum(value: object | None) -> str | None:
    if value is None:
        return None
    return str(value.value if hasattr(value, "value") else value)


async def _authorized_target(
    request: Request, target_id: UUID, principal: Principal, *, allow_approver: bool = False
) -> dict[str, Any]:
    target = await request.app.state.control_plane.targets.get(target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found.")
    allowed = principal.role is Role.ADMIN or str(target["owner_user_id"]) == principal.user_id
    if allow_approver and principal.role is Role.APPROVER:
        allowed = True
    if not allowed:
        raise HTTPException(status_code=403, detail="Not authorized for this target.")
    return dict(target)


def _run_response(row: dict[str, Any], extras: dict[str, Any] | None = None) -> RunResponse:
    extras = extras or {}
    return RunResponse(
        run_id=row["id"], target_id=row["target_id"], status=_enum(row["status"]) or "UNKNOWN",
        deployment_mode=str(row["deployment_mode"]), primary_metric_key=row["primary_metric_key"],
        completion_reason=_enum(row["completion_reason"]), created_at=row["created_at"], updated_at=row["updated_at"],
        target_name=extras.get("target_name"), job_status=extras.get("job_status"),
        job_attempts=extras.get("job_attempts"), last_error_code=extras.get("last_error_code"),
    )


async def _run_extras(connection: Any, run_ids: list[Any]) -> dict[Any, dict[str, Any]]:
    """Target names and the latest durable job state for each run."""
    if not run_ids:
        return {}
    runs, targets, jobs = models.OptimizationRun.__table__, models.Target.__table__, models.Job.__table__
    names = dict((await connection.execute(select(runs.c.id, targets.c.name).join(targets, targets.c.id == runs.c.target_id).where(runs.c.id.in_(run_ids)))).all())
    job_rows = (await connection.execute(select(jobs.c.optimization_run_id, jobs.c.status, jobs.c.attempts, jobs.c.last_error_code, jobs.c.created_at).where(jobs.c.optimization_run_id.in_(run_ids)).order_by(jobs.c.created_at))).mappings().all()
    latest: dict[Any, Any] = {}
    for job in job_rows:
        latest[job["optimization_run_id"]] = job
    return {
        run_id: {
            "target_name": names.get(run_id),
            "job_status": latest[run_id]["status"] if run_id in latest else None,
            "job_attempts": latest[run_id]["attempts"] if run_id in latest else None,
            "last_error_code": latest[run_id]["last_error_code"] if run_id in latest else None,
        }
        for run_id in run_ids
    }


@router.post("/runs", response_model=RunResponse, status_code=201)
async def create_run(body: RunCreate, request: Request, principal: Principal = Depends(current_principal)) -> RunResponse:
    try:
        require_role(principal, Role.OPERATOR, Role.ADMIN)
    except AuthorizationError as error:
        raise HTTPException(status_code=403, detail="Operator permission is required.") from error
    target = await _authorized_target(request, body.target_id, principal)
    if body.primary_metric_key is None:
        raise HTTPException(status_code=422, detail="A primary metric is required; the run cannot be calibrated without one.")
    if body.primary_metric_key not in _PRIMARY_METRICS:
        raise HTTPException(status_code=422, detail="Primary metric is not supported.")
    if not target["is_active"] or target["state"] != "ACTIVE":
        raise HTTPException(status_code=409, detail="Target is not active.")
    engine = request.app.state.control_plane.runs._engine
    async with engine.connect() as connection:
        mapping = (
            await connection.execute(
                select(models.EvaluationMapping.__table__.c.evaluation_target_id).where(
                    models.EvaluationMapping.__table__.c.target_id == body.target_id,
                    models.EvaluationMapping.__table__.c.mapping_status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
    if mapping is None or mapping == body.target_id:
        raise HTTPException(status_code=409, detail="Target requires an active distinct evaluation mapping.")
    created = await OptimizationRunCreationService(engine).create_run_with_initial_job(
        target_id=body.target_id, requested_by_user_id=UUID(principal.user_id),
        deployment_mode=body.deployment_mode, primary_metric_key=body.primary_metric_key,
    )
    return _run_response(dict(created.run))


@router.get("/runs", response_model=Page)
async def list_runs(
    request: Request, principal: Principal = Depends(current_principal), limit: int = 50, offset: int = 0,
    status: str | None = None, target_id: UUID | None = None, deployment_mode: DeploymentMode | None = None,
) -> Page:
    if not 1 <= limit <= 100 or offset < 0:
        raise HTTPException(status_code=422, detail="invalid pagination")
    engine = request.app.state.control_plane.runs._engine
    statement = select(models.OptimizationRun.__table__).order_by(models.OptimizationRun.__table__.c.created_at.desc(), models.OptimizationRun.__table__.c.id)
    runs_table = models.OptimizationRun.__table__
    if principal.role is Role.APPROVER:
        # Same visibility as GET /runs/{id}: own runs, runs awaiting (or whose approval lapsed), runs this approver decided.
        approvals = models.ApprovalRequest.__table__
        decisions = models.ApprovalDecision.__table__
        pending = select(approvals.c.id).where(approvals.c.optimization_run_id == runs_table.c.id, approvals.c.status.in_(("PENDING", "EXPIRED"))).exists()
        decided = (
            select(decisions.c.id)
            .join(approvals, approvals.c.id == decisions.c.approval_request_id)
            .where(approvals.c.optimization_run_id == runs_table.c.id, decisions.c.decided_by_user_id == UUID(principal.user_id))
            .exists()
        )
        statement = statement.where(or_(runs_table.c.requested_by_user_id == UUID(principal.user_id), pending, decided))
    elif principal.role is not Role.ADMIN:
        statement = statement.where(runs_table.c.requested_by_user_id == UUID(principal.user_id))
    if target_id is not None:
        await _authorized_target(request, target_id, principal)
        statement = statement.where(models.OptimizationRun.__table__.c.target_id == target_id)
    if status is not None:
        try:
            statement = statement.where(models.OptimizationRun.__table__.c.status == models.RunStatus(status))
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Invalid run status.") from error
    if deployment_mode is not None:
        statement = statement.where(models.OptimizationRun.__table__.c.deployment_mode == deployment_mode.value)
    async with engine.connect() as connection:
        rows = (await connection.execute(statement.limit(limit).offset(offset))).mappings().all()
        extras = await _run_extras(connection, [row["id"] for row in rows])
    return Page(group=ApiGroup.RUNS, records=[_run_response(dict(row), extras.get(row["id"])).model_dump(mode="json") for row in rows], limit=limit, offset=offset)


@router.get("/runs/{run_id}")
async def get_run(run_id: UUID, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    engine = request.app.state.control_plane.runs._engine
    async with engine.connect() as connection:
        run = (await connection.execute(select(models.OptimizationRun.__table__).where(models.OptimizationRun.__table__.c.id == run_id))).mappings().one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        approver_access = None
        if principal.role is Role.APPROVER:
            approver_access = await connection.scalar(
                select(models.ApprovalRequest.__table__.c.id).where(
                    models.ApprovalRequest.__table__.c.optimization_run_id == run_id,
                    models.ApprovalRequest.__table__.c.status.in_(("PENDING", "EXPIRED")),
                )
            )
            if approver_access is None:
                approver_access = await connection.scalar(
                    select(models.ApprovalDecision.__table__.c.id)
                    .join(
                        models.ApprovalRequest.__table__,
                        models.ApprovalRequest.__table__.c.id
                        == models.ApprovalDecision.__table__.c.approval_request_id,
                    )
                    .where(
                        models.ApprovalRequest.__table__.c.optimization_run_id == run_id,
                        models.ApprovalDecision.__table__.c.decided_by_user_id
                        == UUID(principal.user_id),
                    )
                )
        await _authorized_target(
            request,
            run["target_id"],
            principal,
            allow_approver=approver_access is not None,
        )
        tables = {
            "snapshot": models.WorkloadSnapshot.__table__, "diagnosis": models.DiagnosisArtifact.__table__,
            "generation": models.CandidateGenerationArtifact.__table__, "ranking": models.CandidateRankingArtifact.__table__,
            "evaluation_plan": models.EvaluationPlan.__table__, "admission": models.AdmissionArtifact.__table__,
            "authority": models.AuthorityDecision.__table__, "deployment": models.DeploymentArtifact.__table__,
            "approval": models.ApprovalRequest.__table__,
        }
        artifacts: dict[str, Any] = {}
        for name, table in tables.items():
            if name == "snapshot":
                key, value = table.c.id, run["workload_snapshot_id"]
            else:
                key, value = table.c.optimization_run_id, run_id
            artifact = (
                (await connection.execute(select(table).where(key == value))).mappings().one_or_none()
                if value is not None
                else None
            )
            artifacts[name] = _record(artifact) if artifact is not None else None
        candidates = (await connection.execute(select(models.Candidate.__table__).where(models.Candidate.__table__.c.optimization_run_id == run_id).order_by(models.Candidate.__table__.c.created_at))).mappings().all()
        artifacts["candidates"] = [_record(row) for row in candidates]
        artifacts["rollback"] = None
        if artifacts["deployment"] is not None:
            rollback = (await connection.execute(select(models.RollbackRecord.__table__).where(models.RollbackRecord.__table__.c.candidate_id == artifacts["deployment"]["candidate_id"]))).mappings().one_or_none()
            artifacts["rollback"] = _record(rollback) if rollback is not None else None
        evidence = await run_evidence(connection, dict(run), artifacts)
        extras = (await _run_extras(connection, [run_id])).get(run_id)
    return {"run": _run_response(dict(run), extras).model_dump(mode="json"), "artifacts": artifacts, "evidence": evidence}


@router.get("/approvals", response_model=Page)
async def list_approvals(request: Request, principal: Principal = Depends(current_principal), limit: int = 50, offset: int = 0) -> Page:
    if not 1 <= limit <= 100 or offset < 0:
        raise HTTPException(status_code=422, detail="invalid pagination")
    engine = request.app.state.control_plane.approvals._engine
    statement = select(models.ApprovalRequest.__table__).order_by(models.ApprovalRequest.__table__.c.created_at.desc())
    if principal.role not in {Role.ADMIN, Role.APPROVER}:
        statement = statement.where(models.ApprovalRequest.__table__.c.requested_by_user_id == UUID(principal.user_id))
    async with engine.connect() as connection:
        rows = (await connection.execute(statement.limit(limit).offset(offset))).mappings().all()
    return Page(group=ApiGroup.APPROVALS, records=[_record(row) for row in rows], limit=limit, offset=offset)


async def _decide_approval(approval_id: UUID, body: ApprovalDecisionRequest, request: Request, principal: Principal, *, approve: bool) -> dict[str, Any]:
    engine = request.app.state.control_plane.approvals._engine
    async with engine.connect() as connection:
        approval = (await connection.execute(select(models.ApprovalRequest.__table__).where(models.ApprovalRequest.__table__.c.id == approval_id))).mappings().one_or_none()
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found.")
    await _authorized_target(request, approval["target_id"], principal, allow_approver=True)
    try:
        require_approval_authority(principal, str(approval["requested_by_user_id"]))
        service = DurableApprovalService(engine)
        result = await (service.approve_and_enqueue_continuation(approval_id, UUID(principal.user_id), body.reason) if approve else service.reject_and_complete_run(approval_id, UUID(principal.user_id), body.reason))
    except (AuthorizationError, DurableApprovalRequired) as error:
        raise HTTPException(status_code=403 if isinstance(error, AuthorizationError) else 409, detail="Approval cannot be applied.") from error
    return _record(result)


@router.post("/approvals/{approval_id}/approve")
async def approve(approval_id: UUID, body: ApprovalDecisionRequest, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    return await _decide_approval(approval_id, body, request, principal, approve=True)


@router.post("/approvals/{approval_id}/reject")
async def reject(approval_id: UUID, body: ApprovalDecisionRequest, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    return await _decide_approval(approval_id, body, request, principal, approve=False)


PRODUCTION_ROLES = frozenset({"observer", "executor"})


def connection_kind(roles: object) -> str | None:
    """Classify a worker-published connection: PRODUCTION (observer+executor), SANDBOX, or None."""
    role_set = {str(role) for role in roles} if isinstance(roles, (list, tuple, set, frozenset)) else set()
    if PRODUCTION_ROLES <= role_set:
        return "PRODUCTION"
    if "sandbox" in role_set:
        return "SANDBOX"
    return None


async def worker_connection_roles(request: Request) -> dict[str, list[str]] | None:
    """Labels the worker published, with their roles; None when the worker has never reported."""
    status = await read_setting(request.app.state.control_plane.runs._engine, WORKER_SCOPE, WORKER_STATUS_KEY)
    if not status or not isinstance(status.get("connections"), list):
        return None
    return {str(item.get("label")): list(item.get("roles", [])) for item in status["connections"] if isinstance(item, dict)}


async def require_known_label(request: Request, label: str) -> None:
    """Reject a label the worker does not have, naming the ones it does."""
    known = await worker_connection_roles(request)
    if known and label not in known:
        options = ", ".join(f"{name} ({'sandbox only' if connection_kind(roles) == 'SANDBOX' else 'production'})" for name, roles in sorted(known.items()))
        raise HTTPException(status_code=422, detail=f"Connection label '{label}' is not configured on the worker. Choose one of: {options}.")


async def require_unique_target_name(request: Request, owner_user_id: str, name: str, *, exclude: UUID | None = None) -> None:
    table = models.Target.__table__
    statement = select(table.c.id).where(table.c.owner_user_id == UUID(owner_user_id), table.c.name == name)
    if exclude is not None:
        statement = statement.where(table.c.id != exclude)
    async with request.app.state.control_plane.runs._engine.connect() as connection:
        if await connection.scalar(statement.limit(1)):
            raise HTTPException(status_code=409, detail=f"You already have a target named '{name}'. Choose another name, or edit that target instead.")


@router.post("/targets", response_model=dict[str, Any])
async def create_target(
    body: TargetCreate, request: Request, principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    try:
        require_role(principal, Role.OPERATOR, Role.ADMIN)
    except AuthorizationError as error:
        raise HTTPException(status_code=403, detail="Operator permission is required.") from error
    name, label = body.name.strip(), body.connection_label.strip()
    if not name or not label:
        raise HTTPException(status_code=422, detail="Name and connection label are required.")
    await require_known_label(request, label)
    await require_unique_target_name(request, principal.user_id, name)
    created = await request.app.state.control_plane.targets.create(
        owner_user_id=UUID(principal.user_id),
        name=name,
        connection_label=label,
        deployment_mode=body.deployment_mode.value,
        state="ACTIVE",
        is_active=True,
    )
    return _record(created)
