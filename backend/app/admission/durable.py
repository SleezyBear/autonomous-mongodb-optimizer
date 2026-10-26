"""Persist deterministic statistical admission and highest-ranked selection."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.admission.models import AdmissionRequest, AdmissionStatus
from app.admission.statistics import evaluate_candidate_admission
from app.db import models
from app.db.repositories import AdmissionArtifactRepository, AdmissionRepository, SafetyRepository


@dataclass(frozen=True)
class DurableAdmissionResult:
    artifact_id: UUID
    selected_candidate_id: UUID | None
    admitted_candidate_ids: tuple[UUID, ...]
    production_eligible: bool


class DurableAdmissionService:
    """Use the frozen deterministic engine and persist every candidate verdict."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def decide_for_run(self, run_id: UUID, requests: dict[UUID, AdmissionRequest]) -> DurableAdmissionResult:
        async with self._engine.begin() as connection:
            existing = (await connection.execute(select(models.AdmissionArtifact.__table__).where(models.AdmissionArtifact.__table__.c.optimization_run_id == run_id).with_for_update())).mappings().one_or_none()
            if existing is not None:
                return DurableAdmissionResult(existing["id"], existing["selected_candidate_id"], tuple(UUID(value) for value in existing["admitted_candidate_ids"]), bool(existing["production_eligible"]))
            plan = (await connection.execute(select(models.EvaluationPlan.__table__).where(models.EvaluationPlan.__table__.c.optimization_run_id == run_id))).mappings().one_or_none()
            if plan is None:
                raise ValueError("durable evaluation plan is required before admission")
            ranked = [UUID(value) for value in plan["selected_candidate_ids"]]
            if set(requests) != set(ranked):
                raise ValueError("admission requests must exactly match frozen evaluation selection")
            admitted: list[UUID] = []
            production_eligible_by_candidate: dict[UUID, bool] = {}
            evidence: dict[str, object] = {}
            for candidate_id in ranked:
                result = evaluate_candidate_admission(requests[candidate_id])
                evidence[str(candidate_id)] = asdict(result)
                decision = await AdmissionRepository(self._engine).create_in_transaction(connection, candidate_id=candidate_id, verdict=result.status.value, evidence_hash=_hash(asdict(result)))
                # Persist the full per-metric evidence (confidence bounds, pair
                # counts, reasons) so operators can audit the verdict later.
                await SafetyRepository(self._engine).create_in_transaction(
                    connection,
                    admission_decision_id=decision["id"],
                    check_name="STATISTICAL_ADMISSION",
                    verdict=result.status.value,
                    details=_finite(json.loads(json.dumps(asdict(result), sort_keys=True, default=_json_default))),
                )
                if result.status is AdmissionStatus.ADMITTED:
                    admitted.append(candidate_id)
                    production_eligible_by_candidate[candidate_id] = result.production_eligible
                    await connection.execute(models.Candidate.__table__.update().where(models.Candidate.__table__.c.id == candidate_id).values(status=models.CandidateStatus.ADMITTED))
                else:
                    await connection.execute(models.Candidate.__table__.update().where(models.Candidate.__table__.c.id == candidate_id).values(status=models.CandidateStatus.REJECTED))
            selected = next((candidate_id for candidate_id in ranked if candidate_id in admitted), None)
            production_eligible = bool(selected is not None and production_eligible_by_candidate.get(selected, False))
            row = await AdmissionArtifactRepository(self._engine).create_in_transaction(connection, optimization_run_id=run_id, evaluation_plan_id=plan["id"], selected_candidate_id=selected, admitted_candidate_ids=[str(candidate_id) for candidate_id in admitted], production_eligible=production_eligible, artifact_fingerprint=_hash({"run_id": str(run_id), "plan": str(plan["id"]), "results": evidence, "selected": str(selected) if selected else None, "production_eligible": production_eligible}))
            return DurableAdmissionResult(row["id"], selected, tuple(admitted), production_eligible)


def _finite(value: object) -> object:
    """PostgreSQL JSONB rejects NaN/Infinity; persist them as null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    return value


def _json_default(value: object) -> object:
    return value.value if hasattr(value, "value") else str(value)


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
