"""Durable worker process: PostgreSQL claims, leases, dispatch, and shutdown."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from uuid import UUID, uuid4

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import models
from app.db.runtime import create_control_plane_repositories
from app.mongodb.connections import TargetConnectionRegistry
from app.runs.orchestrator import OrchestrationInvariantError, OrchestrationOutcome, OptimizationRunOrchestrator
from app.settings_store import WORKER_SCOPE, WORKER_STATUS_KEY, upsert_setting
from app.approvals.durable import DurableApprovalService
from app.telemetry.collector import TelemetryCollector
from app.worker.composition import AISettings, ProductionOrchestratorFactory, build_ai_provider
from app.worker.durable import DurableJobWorker, ExecutionContext, Job, JobKind, JobRepository, LeaseLostError, PermanentJobError

TERMINAL = {models.RunStatus.COMPLETED, models.RunStatus.ROLLED_BACK, models.RunStatus.ROLLBACK_BLOCKED, models.RunStatus.FAILED}
_log = structlog.get_logger("worker")


class OrchestrationBlocked(RuntimeError):
    """A transient lifecycle blocker; its code is persisted as the job's last error."""

    def __init__(self, blocker: str) -> None:
        super().__init__(blocker)
        self.error_code = blocker


OrchestratorFactory = Callable[[UUID], Awaitable[OptimizationRunOrchestrator]]


class OptimizationJobHandler:
    """Validate an authoritative job then call the real durable orchestrator."""

    def __init__(self, repositories: object, orchestrator: OptimizationRunOrchestrator | None = None, *, orchestrator_factory: OrchestratorFactory | None = None) -> None:
        self._repositories = repositories
        self._orchestrator = orchestrator
        self._factory = orchestrator_factory

    async def handle(self, job: Job, context: ExecutionContext) -> None:
        if job.optimization_run_id is None:
            raise PermanentJobError("OPTIMIZATION_RUN_ID_REQUIRED")
        payload_run = job.payload.get("run_id")
        if payload_run is not None and str(payload_run) != job.optimization_run_id:
            raise PermanentJobError("JOB_RUN_ID_MISMATCH")
        run = await getattr(self._repositories, "runs").get(job.optimization_run_id)
        if run is None:
            raise PermanentJobError("OPTIMIZATION_RUN_NOT_FOUND")
        if run["status"] in TERMINAL:
            return
        context.ensure_lease_owned()
        orchestrator = self._orchestrator
        if orchestrator is None and self._factory is not None:
            # Target-specific services (Mongo adapters, sandbox evaluator) are
            # composed per run from the worker-only connection registry.
            orchestrator = await self._factory(UUID(job.optimization_run_id))
        if orchestrator is None:
            raise PermanentJobError("DURABLE_ORCHESTRATOR_REQUIRED")
        # One claimed job owns the complete uninterrupted state-machine slice.
        # Releasing the job after each transition would strand a durable run
        # without a continuation job. Approval is the only deliberate pause;
        # its API decision atomically enqueues the sole continuation.
        for _step in range(32):
            try:
                result = await orchestrator.run(UUID(job.optimization_run_id), context)
            except OrchestrationInvariantError as error:
                raise PermanentJobError("ORCHESTRATION_INVARIANT_FAILURE") from error
            if result.outcome is OrchestrationOutcome.TERMINAL:
                return
            if result.outcome is OrchestrationOutcome.BLOCKED:
                if result.status is models.RunStatus.APPROVAL_PENDING:
                    return
                raise OrchestrationBlocked(result.blocker or "ORCHESTRATION_TRANSIENT_BLOCK")
            context.ensure_lease_owned()
        raise PermanentJobError("ORCHESTRATION_STEP_LIMIT_EXCEEDED")


class JobDispatcher:
    """Typed, centralized durable job dispatch."""

    def __init__(self, optimization: OptimizationJobHandler) -> None:
        self._optimization = optimization

    async def dispatch(self, job: Job, context: ExecutionContext) -> None:
        try:
            kind = JobKind(job.kind)
        except ValueError as error:
            raise PermanentJobError("UNKNOWN_JOB_KIND") from error
        if kind is JobKind.OPTIMIZATION:
            await self._optimization.handle(job, context)
            return
        raise PermanentJobError("UNKNOWN_JOB_KIND")


def worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex}"


async def _wait_or_stop(stopping: asyncio.Event, seconds: float) -> None:
    """Sleep without delaying a SIGTERM/SIGINT shutdown."""
    try:
        await asyncio.wait_for(stopping.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


class WorkerService:
    """Long-lived polling process over the durable PostgreSQL job store."""

    def __init__(self, durable: DurableJobWorker, dispatcher: JobDispatcher, *, poll_interval: float = 1.0, identity: str) -> None:
        self._durable = durable
        self._dispatcher = dispatcher
        self._poll_interval = max(0.1, poll_interval)
        self._identity = identity
        self.live = False

    async def run(self, stopping: asyncio.Event) -> None:
        """Poll without hot-looping; retain unfinished work for lease recovery."""
        self.live = True
        _log.info("worker_started", service="worker", worker_id=self._identity)
        try:
            while not stopping.is_set():
                try:
                    worked = await self._durable.run_once(self._dispatcher.dispatch, stopping=stopping)
                except LeaseLostError:
                    if stopping.is_set():
                        break
                    _log.warning("worker_lease_lost", service="worker", worker_id=self._identity)
                    worked = True
                except (OSError, SQLAlchemyError):
                    # Never log the exception body: it can contain a credentialed URI.
                    _log.warning("worker_database_unavailable", service="worker", worker_id=self._identity)
                    worked = False
                if not worked:
                    await _wait_or_stop(stopping, self._poll_interval)
        finally:
            self.live = False
            _log.info("worker_stopping", service="worker", worker_id=self._identity)


async def readiness(repository: JobRepository) -> bool:
    """Readiness is limited to reachability of the durable PostgreSQL store."""
    try:
        return bool(await repository.is_ready())
    except (OSError, SQLAlchemyError):
        return False


async def publish_status(engine: AsyncEngine, identity: str, connections: TargetConnectionRegistry, ai: AISettings, *, stopping: asyncio.Event, interval: float = 15.0) -> None:
    """Publish a non-secret heartbeat so operators can see worker configuration."""
    started = datetime.now(timezone.utc).isoformat()
    while not stopping.is_set():
        try:
            await upsert_setting(engine, WORKER_SCOPE, WORKER_STATUS_KEY, {
                "worker_id": identity,
                "started_at": started,
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "ai": ai.public(),
                "connections": connections.public_document(),
                "orchestration": "PRODUCTION" if connections.labels else "UNCONFIGURED",
                "telemetry_interval_seconds": float(os.getenv("TELEMETRY_INTERVAL_SECONDS", "60")),
            })
        except (OSError, SQLAlchemyError):
            _log.warning("worker_status_publish_failed", service="worker", worker_id=identity)
        await _wait_or_stop(stopping, interval)


async def expire_approvals(engine: AsyncEngine, *, stopping: asyncio.Event, interval: float = 30.0) -> None:
    """End runs whose approval window lapsed, so they do not wait forever."""
    service = DurableApprovalService(engine)
    while not stopping.is_set():
        try:
            for run_id in await service.expire_overdue():
                _log.info("approval_expired", service="worker", run_id=str(run_id))
        except (OSError, SQLAlchemyError):
            _log.warning("approval_expiry_failed", service="worker")
        await _wait_or_stop(stopping, interval)


async def main() -> None:
    engine, repositories = create_control_plane_repositories()
    identity = worker_id()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    connections = TargetConnectionRegistry.from_environment()
    ai_settings = AISettings.from_environment()
    ai_provider, ai_client = build_ai_provider(ai_settings)
    if connections.labels:
        handler = OptimizationJobHandler(repositories, orchestrator_factory=ProductionOrchestratorFactory(engine, repositories, connections, ai_provider))
    else:
        # Without target connections the worker still fails closed: runs
        # stop at the first stage that needs a real service.
        handler = OptimizationJobHandler(repositories, OptimizationRunOrchestrator(engine, repositories))
    service = WorkerService(
        DurableJobWorker(JobRepository(engine), identity),
        JobDispatcher(handler),
        poll_interval=float(os.getenv("WORKER_POLL_INTERVAL_SECONDS", "1")),
        identity=identity,
    )
    background = [
        asyncio.create_task(publish_status(engine, identity, connections, ai_settings, stopping=stopping)),
        asyncio.create_task(expire_approvals(engine, stopping=stopping, interval=float(os.getenv("APPROVAL_EXPIRY_INTERVAL_SECONDS", "30")))),
    ]
    if connections.labels and os.getenv("TELEMETRY_ENABLED", "true").lower() == "true":
        background.append(asyncio.create_task(TelemetryCollector(engine, connections).run(stopping)))
    try:
        await service.run(stopping)
    finally:
        stopping.set()
        await asyncio.gather(*background, return_exceptions=True)
        await connections.close()
        if ai_client is not None:
            await ai_client.aclose()
        await engine.dispose()


async def healthcheck() -> None:
    """Fail unless PostgreSQL and the durable jobs boundary are reachable."""
    engine, _repositories = create_control_plane_repositories()
    try:
        if not await readiness(JobRepository(engine)):
            raise RuntimeError("durable PostgreSQL store is not ready")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(healthcheck() if "--healthcheck" in sys.argv else main())
