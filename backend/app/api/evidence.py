"""Read model that turns one run's immutable artifacts into operator evidence.

Everything returned here is already literal-free by construction (snapshots,
shapes, typed actions, measurements, verdicts, ledger hashes).  Sanitized LLM
*inputs* are never returned; only invocation status, timing, and the validated
advisory rationale.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text

from app.db import models


MAX_TRIALS_PER_CANDIDATE = 40
DISPLAY_METRICS = ("p50_latency_ms", "p95_latency_ms", "p99_latency_ms", "throughput_ops_per_second", "write_p95_latency_ms", "result_mismatch_count")
EVENT_TYPES = ("SANDBOX_EVALUATION_EVIDENCE", "PRODUCTION_BASELINE_PROBE", "DURABLE_POST_DEPLOYMENT_MONITORING", "RUN_RESUMED")


def _plain(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value") and not isinstance(value, (int, float, str)):
        return value.value
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    try:
        return float(value) if value.__class__.__name__ == "Decimal" else value
    except (TypeError, ValueError):
        return value


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


async def run_evidence(connection: Any, run: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    run_id = run["id"]
    evidence: dict[str, Any] = {}

    # Workload snapshot shapes (literal-free structures and shares).
    shapes: list[dict[str, Any]] = []
    if run["workload_snapshot_id"] is not None:
        snapshot_shapes, query_shapes = models.WorkloadSnapshotQueryShape.__table__, models.QueryShape.__table__
        rows = (await connection.execute(
            select(snapshot_shapes, query_shapes.c.normalized_shape)
            .join(query_shapes, query_shapes.c.id == snapshot_shapes.c.query_shape_id)
            .where(snapshot_shapes.c.workload_snapshot_id == run["workload_snapshot_id"])
            .order_by(snapshot_shapes.c.workload_operation_share.desc())
        )).mappings().all()
        shapes = [
            {
                "query_shape_id": str(row["query_shape_id"]), "query_shape_hash": row["query_shape_hash"], "operation": row["operation"],
                "namespace": row["namespace"], "operations": int(row["observed_operation_count"]),
                "operation_share": float(row["workload_operation_share"]),
                "execution_time_share": None if row["execution_time_share"] is None else float(row["execution_time_share"]),
                "execution_time_ms": None if row["aggregate_execution_time_ms"] is None else float(row["aggregate_execution_time_ms"]),
                "protected": bool(row["base_protected"]), "provider": row["provider"],
                "shape": row["normalized_shape"],
            }
            for row in rows
        ]
    evidence["shapes"] = shapes

    # Grounded diagnosis findings.
    findings: list[dict[str, Any]] = []
    diagnosis = artifacts.get("diagnosis")
    if diagnosis is not None:
        rows = (await connection.execute(
            select(models.DiagnosisFinding.__table__).where(models.DiagnosisFinding.__table__.c.diagnosis_artifact_id == UUID(str(diagnosis["id"])))
            .order_by(models.DiagnosisFinding.__table__.c.finding_id)
        )).mappings().all()
        findings = [{"finding_id": row["finding_id"], "finding_type": row["finding_type"], "summary": row["summary"], "rationale": row["rationale"], "query_shape_ids": list(row["query_shape_ids"]), "evidence_refs": list(row["evidence_refs"])} for row in rows]
    evidence["findings"] = findings

    # Candidates with their typed action, rank, measurements and verdicts.
    candidates_table, actions_table = models.Candidate.__table__, models.CandidateAction.__table__
    candidate_rows = (await connection.execute(
        select(candidates_table, actions_table.c.action_type, actions_table.c.action_payload)
        .join(actions_table, actions_table.c.candidate_id == candidates_table.c.id, isouter=True)
        .where(candidates_table.c.optimization_run_id == run_id)
        .order_by(candidates_table.c.deterministic_fingerprint)
    )).mappings().all()
    ranking = artifacts.get("ranking") or {}
    ranked = [str(value) for value in (ranking.get("ordered_candidate_ids") or [])]
    plan = artifacts.get("evaluation_plan") or {}
    selected = {str(value) for value in (plan.get("selected_candidate_ids") or [])}
    admission = artifacts.get("admission") or {}
    candidate_ids = [row["id"] for row in candidate_rows]
    decisions: dict[Any, dict[str, Any]] = {}
    trials: dict[Any, list[dict[str, Any]]] = {}
    evaluations: dict[Any, dict[str, Any]] = {}
    if candidate_ids:
        decision_rows = (await connection.execute(
            select(models.AdmissionDecision.__table__.c.candidate_id, models.AdmissionDecision.__table__.c.verdict, models.AdmissionDecision.__table__.c.evidence_hash, models.SafetyResult.__table__.c.details)
            .join(models.SafetyResult.__table__, models.SafetyResult.__table__.c.admission_decision_id == models.AdmissionDecision.__table__.c.id, isouter=True)
            .where(models.AdmissionDecision.__table__.c.candidate_id.in_(candidate_ids))
        )).mappings().all()
        for row in decision_rows:
            decisions[row["candidate_id"]] = {"verdict": row["verdict"], "evidence_hash": row["evidence_hash"], "details": row["details"]}
        evaluation_rows = (await connection.execute(select(models.EvaluationRun.__table__).where(models.EvaluationRun.__table__.c.candidate_id.in_(candidate_ids)))).mappings().all()
        evaluations = {row["candidate_id"]: row for row in evaluation_rows}
        if evaluations:
            pair_rows = (await connection.execute(
                select(models.TrialPair.__table__).where(models.TrialPair.__table__.c.evaluation_run_id.in_([row["id"] for row in evaluation_rows]))
                .order_by(models.TrialPair.__table__.c.pair_number)
            )).mappings().all()
            by_evaluation = {row["id"]: row["candidate_id"] for row in evaluation_rows}
            for pair in pair_rows:
                candidate_id = by_evaluation[pair["evaluation_run_id"]]
                baseline = pair["baseline_measurement"] or {}
                candidate = pair["candidate_measurement"] or {}
                trials.setdefault(candidate_id, []).append({
                    "pair_number": pair["pair_number"],
                    "arm_order": baseline.get("arm_order"),
                    "baseline": {key: value for key, value in (baseline.get("metrics") or {}).items() if key in DISPLAY_METRICS},
                    "candidate": {key: value for key, value in (candidate.get("metrics") or {}).items() if key in DISPLAY_METRICS},
                })
    primary = str(run.get("primary_metric_key") or "p99_latency_ms")
    candidates: list[dict[str, Any]] = []
    for row in candidate_rows:
        key = str(row["id"])
        pairs = trials.get(row["id"], [])
        summary: dict[str, Any] = {}
        for metric in DISPLAY_METRICS:
            base = [float(pair["baseline"][metric]) for pair in pairs if metric in pair["baseline"]]
            cand = [float(pair["candidate"][metric]) for pair in pairs if metric in pair["candidate"]]
            if base and cand:
                b, c = _median(base), _median(cand)
                summary[metric] = {"baseline_median": b, "candidate_median": c, "change_ratio": (c / b - 1) if b and c is not None else None}
        evaluation = evaluations.get(row["id"])
        candidates.append({
            "id": key,
            "status": _plain(row["status"]),
            "policy_classification": row["policy_classification"],
            "generation_rule": f"{row['generation_rule_id']}@{row['generation_rule_version']}" if row["generation_rule_id"] else None,
            "fingerprint": row["deterministic_fingerprint"],
            "affected_query_shape_ids": list(row["affected_query_shape_ids"] or []),
            "evidence_refs": list(row["evidence_refs"] or []),
            "action": None if row["action_type"] is None else {"action_type": row["action_type"], "payload": row["action_payload"]},
            "rank": ranked.index(key) + 1 if key in ranked else None,
            "selected_for_evaluation": key in selected,
            "selected_for_deployment": str(admission.get("selected_candidate_id")) == key,
            "evaluation": None if evaluation is None else {"status": evaluation["status"], "environment_fingerprint": evaluation["environment_fingerprint"], "pairs": len(pairs), "primary_metric": primary, "summary": summary},
            "admission": _plain(decisions.get(row["id"])),
            "trials": pairs[:MAX_TRIALS_PER_CANDIDATE],
        })
    candidates.sort(key=lambda item: (item["rank"] is None, item["rank"] or 0, item["id"]))
    evidence["candidates"] = candidates

    # Advisory model invocations (status/timing only; never sanitized inputs).
    invocations = (await connection.execute(
        select(models.AIInvocation.__table__).where(models.AIInvocation.__table__.c.optimization_run_id == run_id).order_by(models.AIInvocation.__table__.c.created_at)
    )).mappings().all()
    evidence["ai_invocations"] = [
        {
            "stage": row["stage"], "provider": row["provider"], "model": row["model"], "status": row["status"],
            "latency_ms": None if row["latency_ms"] is None else float(row["latency_ms"]), "safe_error_code": row["safe_error_code"],
            "attempt": row["attempt_number"], "prompt_version": row["prompt_version"], "created_at": _plain(row["created_at"]),
            "rationale": (row["validated_output"] or {}).get("rationale") if isinstance(row["validated_output"], dict) else None,
        }
        for row in invocations
    ]

    # Append-only production ledger entries for this run.
    ledger_rows = (await connection.execute(
        text("SELECT sequence, event_type, actor, action_id, evidence_hash, previous_hash, entry_hash, forward_action, created_at FROM production_ledger_entries WHERE run_id = :run ORDER BY sequence"),
        {"run": run_id},
    )).mappings().all()
    evidence["ledger"] = [_plain(dict(row)) for row in ledger_rows]

    # Worker-recorded evaluation / monitoring / operator events.
    events = (await connection.execute(
        select(models.AuditEvent.__table__).where(models.AuditEvent.__table__.c.optimization_run_id == run_id, models.AuditEvent.__table__.c.event_type.in_(EVENT_TYPES))
        .order_by(models.AuditEvent.__table__.c.created_at)
    )).mappings().all()
    evidence["events"] = [{"event_type": row["event_type"], "created_at": _plain(row["created_at"]), "payload": row["event_payload"]} for row in events]

    # Durable job state and approval decision.
    job = (await connection.execute(
        select(models.Job.__table__).where(models.Job.__table__.c.optimization_run_id == run_id).order_by(models.Job.__table__.c.created_at.desc()).limit(1)
    )).mappings().one_or_none()
    evidence["job"] = None if job is None else {
        "id": str(job["id"]), "status": job["status"], "attempts": job["attempts"], "last_error_code": job["last_error_code"],
        "available_at": _plain(job["available_at"]), "failed_at": _plain(job["failed_at"]), "heartbeat_at": _plain(job["heartbeat_at"]),
    }
    approval = artifacts.get("approval")
    decision = None
    if approval is not None:
        decision_row = (await connection.execute(select(models.ApprovalDecision.__table__).where(models.ApprovalDecision.__table__.c.approval_request_id == UUID(str(approval["id"]))))).mappings().one_or_none()
        decision = None if decision_row is None else {"decision": decision_row["decision"], "reason": decision_row["reason"], "decided_by_user_id": str(decision_row["decided_by_user_id"]), "decided_at": _plain(decision_row["created_at"])}
    evidence["approval_decision"] = decision
    return evidence
