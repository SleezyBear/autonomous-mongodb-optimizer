"""PostgreSQL-backed four-eyes approvals shared by every API and worker process."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, insert, select, text, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import models


class DurableApprovalRequired(PermissionError):
    """Raised when no unexpired, evidence-bound approval can authorize an action."""


class DurableApprovalService:
    """A transactionally consistent approval authority, not an in-memory cache."""

    def __init__(self, engine: AsyncEngine, approval_ttl: timedelta = timedelta(minutes=15)) -> None:
        self._engine = engine
        self._approval_ttl = approval_ttl

    async def request(self, *, action_id: str, target_id: UUID, candidate_id: UUID, evidence_hash: str, requester_id: UUID, optimization_run_id: UUID | None = None, candidate_fingerprint: str | None = None) -> RowMapping:
        now = datetime.now(timezone.utc)
        payload = {"id": uuid4(), "created_at": now, "updated_at": now, "admission_decision_id": None, "requested_by_user_id": requester_id, "action_id": action_id, "target_id": target_id, "candidate_id": candidate_id, "optimization_run_id": optimization_run_id, "candidate_fingerprint": candidate_fingerprint, "evidence_hash": evidence_hash, "expires_at": now + self._approval_ttl, "status": "PENDING"}
        async with self._engine.begin() as connection:
            if optimization_run_id is not None:
                existing = (await connection.execute(select(models.ApprovalRequest.__table__).where(models.ApprovalRequest.__table__.c.optimization_run_id == optimization_run_id).with_for_update())).mappings().one_or_none()
                if existing is not None:
                    if existing["candidate_id"] != candidate_id or existing["candidate_fingerprint"] != candidate_fingerprint:
                        raise DurableApprovalRequired("approval request cannot authorize candidate drift")
                    return existing
            result = await connection.execute(insert(models.ApprovalRequest.__table__).values(**payload).returning(*models.ApprovalRequest.__table__.c))
            return result.mappings().one()

    async def approve(self, approval_id: UUID, approver_id: UUID) -> RowMapping:
        now = datetime.now(timezone.utc)
        async with self._engine.begin() as connection:
            result = await connection.execute(select(models.ApprovalRequest.__table__).where(models.ApprovalRequest.id == approval_id).with_for_update())
            request = result.mappings().one_or_none()
            if request is None or request["status"] != "PENDING" or request["expires_at"] <= now:
                raise DurableApprovalRequired("approval is not pending and unexpired")
            if request["requested_by_user_id"] == approver_id:
                raise DurableApprovalRequired("four-eyes separation forbids self-approval")
            await connection.execute(update(models.ApprovalRequest.__table__).where(models.ApprovalRequest.id == approval_id).values(status="APPROVED", updated_at=now))
            decision = {"id": uuid4(), "created_at": now, "updated_at": now, "approval_request_id": approval_id, "decided_by_user_id": approver_id, "decision": "APPROVED", "reason": None}
            await connection.execute(insert(models.ApprovalDecision.__table__).values(**decision))
            approved = await connection.execute(select(models.ApprovalRequest.__table__).where(models.ApprovalRequest.id == approval_id))
            return approved.mappings().one()

    async def approve_and_enqueue_continuation(self, approval_id: UUID, approver_id: UUID, reason: str | None = None) -> RowMapping:
        """Approve an exact request and atomically create/reuse its one continuation job."""
        now = datetime.now(timezone.utc)
        async with self._engine.begin() as connection:
            request = (await connection.execute(select(models.ApprovalRequest.__table__).where(models.ApprovalRequest.id == approval_id).with_for_update())).mappings().one_or_none()
            if request is None or request["optimization_run_id"] is None:
                raise DurableApprovalRequired("durable run-bound approval required")
            if request["status"] != "PENDING" or request["expires_at"] <= now or request["requested_by_user_id"] == approver_id:
                raise DurableApprovalRequired("approval is not eligible")
            run = (await connection.execute(select(models.OptimizationRun.__table__).where(models.OptimizationRun.id == request["optimization_run_id"]).with_for_update())).mappings().one_or_none()
            if run is None or _value(run["status"]) != models.RunStatus.APPROVAL_PENDING.value:
                raise DurableApprovalRequired("run is not awaiting this approval")
            await connection.execute(update(models.ApprovalRequest.__table__).where(models.ApprovalRequest.id == approval_id).values(status="APPROVED", updated_at=now))
            await connection.execute(insert(models.ApprovalDecision.__table__).values(id=uuid4(), created_at=now, updated_at=now, approval_request_id=approval_id, decided_by_user_id=approver_id, decision="APPROVED", reason=reason))
            await connection.execute(update(models.OptimizationRun.__table__).where(models.OptimizationRun.id == request["optimization_run_id"]).values(status=models.RunStatus.APPROVED, updated_at=now))
            await connection.execute(text("INSERT INTO jobs (id, created_at, updated_at, payload, status, attempts, optimization_run_id, kind) VALUES (:id, :now, :now, CAST(:payload AS jsonb), 'PENDING', 0, :run_id, 'OPTIMIZATION') ON CONFLICT DO NOTHING"), {"id": uuid4(), "now": now, "run_id": request["optimization_run_id"], "payload": '{\"continuation\": \"true\"}'})
            return (await connection.execute(select(models.ApprovalRequest.__table__).where(models.ApprovalRequest.id == approval_id))).mappings().one()

    async def reject_and_complete_run(self, approval_id: UUID, approver_id: UUID, reason: str | None = None) -> RowMapping:
        """Record an immutable rejection and atomically finish the bound run without mutation."""
        now = datetime.now(timezone.utc)
        async with self._engine.begin() as connection:
            request = (await connection.execute(select(models.ApprovalRequest.__table__).where(models.ApprovalRequest.id == approval_id).with_for_update())).mappings().one_or_none()
            if request is None or request["optimization_run_id"] is None:
                raise DurableApprovalRequired("durable run-bound approval required")
            if request["status"] != "PENDING" or request["expires_at"] <= now or request["requested_by_user_id"] == approver_id:
                raise DurableApprovalRequired("approval rejection is not eligible")
            run = (await connection.execute(select(models.OptimizationRun.__table__).where(models.OptimizationRun.id == request["optimization_run_id"]).with_for_update())).mappings().one_or_none()
            if run is None or _value(run["status"]) != models.RunStatus.APPROVAL_PENDING.value:
                raise DurableApprovalRequired("run is not awaiting this approval")
            await connection.execute(update(models.ApprovalRequest.__table__).where(models.ApprovalRequest.id == approval_id).values(status="REJECTED", updated_at=now))
            await connection.execute(insert(models.ApprovalDecision.__table__).values(id=uuid4(), created_at=now, updated_at=now, approval_request_id=approval_id, decided_by_user_id=approver_id, decision="REJECTED", reason=reason))
            await connection.execute(update(models.OptimizationRun.__table__).where(models.OptimizationRun.id == request["optimization_run_id"]).values(status=models.RunStatus.COMPLETED, completion_reason=models.RunCompletionReason.APPROVAL_REJECTED, updated_at=now))
            return (await connection.execute(select(models.ApprovalRequest.__table__).where(models.ApprovalRequest.id == approval_id))).mappings().one()

    async def expire_overdue(self, *, now: datetime | None = None) -> list[UUID]:
        """Close pending approvals past their deadline and complete their runs without mutation.

        Returns the ids of runs that were completed with ``APPROVAL_EXPIRED``.
        """
        now = now or datetime.now(timezone.utc)
        requests = models.ApprovalRequest.__table__
        runs = models.OptimizationRun.__table__
        completed: list[UUID] = []
        async with self._engine.begin() as connection:
            overdue = (await connection.execute(
                select(requests.c.id, requests.c.optimization_run_id)
                .where(requests.c.status == "PENDING", requests.c.expires_at <= now)
                .with_for_update(skip_locked=True)
            )).mappings().all()
            for request in overdue:
                await connection.execute(update(requests).where(requests.c.id == request["id"]).values(status="EXPIRED", updated_at=now))
                if request["optimization_run_id"] is None:
                    continue
                run = (await connection.execute(
                    select(runs.c.id, runs.c.status).where(runs.c.id == request["optimization_run_id"]).with_for_update()
                )).mappings().one_or_none()
                if run is not None and _value(run["status"]) == models.RunStatus.APPROVAL_PENDING.value:
                    await connection.execute(
                        update(runs).where(runs.c.id == run["id"]).values(
                            status=models.RunStatus.COMPLETED, completion_reason=models.RunCompletionReason.APPROVAL_EXPIRED, updated_at=now
                        )
                    )
                    completed.append(UUID(str(run["id"])))
        return completed

    async def require_current(self, *, action_id: str, target_id: UUID, candidate_id: UUID, evidence_hash: str) -> RowMapping:
        now = datetime.now(timezone.utc)
        async with self._engine.begin() as connection:
            result = await connection.execute(select(models.ApprovalRequest.__table__).where(and_(models.ApprovalRequest.action_id == action_id, models.ApprovalRequest.target_id == target_id, models.ApprovalRequest.candidate_id == candidate_id, models.ApprovalRequest.evidence_hash == evidence_hash, models.ApprovalRequest.status == "APPROVED", models.ApprovalRequest.expires_at > now)).with_for_update())
            approval = result.mappings().one_or_none()
            if approval is None:
                raise DurableApprovalRequired("current approval required")
            return approval


def _value(value: object) -> str:
    return str(value.value if hasattr(value, "value") else value)
