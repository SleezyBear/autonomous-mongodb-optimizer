"""PostgreSQL-backed durable job claiming with leases and heartbeats."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, TypeVar, cast
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.resources.protection import HeavyTaskKind, HeavyTaskLimiter, shared_heavy_task_limiter


T = TypeVar("T")


class JobKind(str, Enum):
    OPTIMIZATION = "OPTIMIZATION"


class LeaseLostError(RuntimeError):
    """Raised when a worker may no longer act as the job's lease owner."""


class PermanentJobError(RuntimeError):
    """A sanitized domain failure which must not be retried."""


@dataclass(frozen=True)
class ExecutionContext:
    """Cooperative ownership boundary required before sensitive future stages."""

    lease_lost: asyncio.Event

    def ensure_lease_owned(self) -> None:
        if self.lease_lost.is_set():
            raise LeaseLostError("job lease ownership has been lost")


@dataclass(frozen=True)
class Job:
    """A claimed durable job whose lease belongs to one worker."""

    id: str
    optimization_run_id: str | None
    kind: str
    payload: Mapping[str, Any]
    attempts: int


class JobRepository:
    """Persist jobs and atomically claim them with PostgreSQL row locks."""

    def __init__(self, engine: AsyncEngine, lease_duration: timedelta = timedelta(seconds=30), max_attempts: int = 5) -> None:
        self._engine = engine
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts

    async def enqueue(self, payload: Mapping[str, Any], *, optimization_run_id: str | None = None, kind: JobKind = JobKind.OPTIMIZATION, connection: AsyncConnection | None = None) -> str:
        """Persist one pending job and return its durable identifier."""
        job_id = str(uuid4())
        now = datetime.now(timezone.utc)
        async def insert_job(active_connection: AsyncConnection) -> None:
            await active_connection.execute(
                text(
                    "INSERT INTO jobs (id, created_at, updated_at, payload, status, attempts, optimization_run_id, kind) "
                "VALUES (:id, :now, :now, CAST(:payload AS jsonb), 'PENDING', 0, :optimization_run_id, :kind)"
                ),
                {"id": job_id, "now": now, "payload": json.dumps(dict(payload), sort_keys=True), "optimization_run_id": optimization_run_id, "kind": kind.value},
            )
        if connection is None:
            async with self._engine.begin() as owned_connection:
                await insert_job(owned_connection)
        else:
            await insert_job(connection)
        return job_id

    async def claim(self, worker_id: str, *, payload_tag: str | None = None) -> Job | None:
        """Atomically lease one pending or expired job using SKIP LOCKED."""
        now = datetime.now(timezone.utc)
        expires_at = now + self._lease_duration
        statement = text(
            "WITH claimable AS ("
            " SELECT id FROM jobs"
            " WHERE ((status = 'PENDING' AND available_at <= :now) OR (status = 'RUNNING' AND lease_expires_at <= :now))"
            " AND (CAST(:payload_tag AS text) IS NULL OR payload ->> '_queue_tag' = CAST(:payload_tag AS text))"
            " ORDER BY created_at, id"
            " FOR UPDATE SKIP LOCKED"
            " LIMIT 1"
            ")"
            " UPDATE jobs AS job"
            " SET status = 'RUNNING', lease_owner = :worker_id, lease_expires_at = :expires_at,"
            " heartbeat_at = :now, updated_at = :now, attempts = attempts + 1"
            " FROM claimable"
            " WHERE job.id = claimable.id"
            " RETURNING job.id, job.optimization_run_id, job.kind, job.payload, job.attempts"
        )
        async with self._engine.begin() as connection:
            row = (await connection.execute(statement, {"worker_id": worker_id, "now": now, "expires_at": expires_at, "payload_tag": payload_tag})).mappings().one_or_none()
        if row is None:
            return None
        return Job(id=str(row["id"]), optimization_run_id=str(row["optimization_run_id"]) if row["optimization_run_id"] else None, kind=str(row["kind"]), payload=row["payload"], attempts=row["attempts"])

    async def is_ready(self) -> bool:
        """Check the durable PostgreSQL boundary without changing job state."""
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    async def heartbeat(self, job_id: str, worker_id: str) -> bool:
        """Extend a worker's current lease; a lost lease cannot be renewed."""
        now = datetime.now(timezone.utc)
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    "UPDATE jobs SET heartbeat_at = :now, lease_expires_at = :expires_at, updated_at = :now "
                    "WHERE id = :id AND status = 'RUNNING' AND lease_owner = :worker_id AND lease_expires_at > :now"
                ),
                {"id": job_id, "worker_id": worker_id, "now": now, "expires_at": now + self._lease_duration},
            )
        return bool(result.rowcount == 1)

    async def complete(self, job_id: str, worker_id: str) -> bool:
        """Complete only a job still leased by the calling worker."""
        now = datetime.now(timezone.utc)
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    "UPDATE jobs SET status = 'COMPLETED', completed_at = :now, updated_at = :now, "
                    "lease_owner = NULL, lease_expires_at = NULL "
                    "WHERE id = :id AND status = 'RUNNING' AND lease_owner = :worker_id"
                ),
                {"id": job_id, "worker_id": worker_id, "now": now},
            )
        return bool(result.rowcount == 1)

    async def fail(self, job: Job, worker_id: str, error_code: str, *, permanent: bool) -> None:
        """Release a transient job with bounded backoff, or terminally fail it."""
        now = datetime.now(timezone.utc)
        final = permanent or job.attempts >= self._max_attempts
        delay = self.backoff_seconds(job.attempts)
        status = "FAILED" if final else "PENDING"
        async with self._engine.begin() as connection:
            result = await connection.execute(text(
                "UPDATE jobs SET status=:status, last_error_code=:error, failed_at=CASE WHEN :final THEN :now ELSE CAST(NULL AS timestamptz) END, "
                "available_at=CASE WHEN :final THEN available_at ELSE :available_at END, lease_owner=NULL, lease_expires_at=NULL, updated_at=:now "
                "WHERE id=:id AND status='RUNNING' AND lease_owner=:worker"
            ), {"status": status, "error": error_code[:128], "final": final, "now": now, "available_at": now + timedelta(seconds=delay), "id": job.id, "worker": worker_id})
        if result.rowcount != 1:
            raise LeaseLostError(f"job lease lost before failure handling: {job.id}")

    @staticmethod
    def backoff_seconds(attempt: int) -> int:
        """Deterministic bounded 1,2,4,8,16-second retry policy."""
        return int(min(16, 2 ** max(0, attempt - 1)))


JobHandler = Callable[[Job, ExecutionContext], Awaitable[None]]


def safe_error_code(error: BaseException) -> str:
    """Return a bounded, credential-free failure code for durable job state."""
    code = getattr(error, "error_code", None)
    if isinstance(code, str) and code and all(character.isalnum() or character in "_:-." for character in code):
        return code[:128]
    return type(error).__name__


class DurableJobWorker:
    """Execute each successfully completed claim once while holding its database lease."""

    def __init__(self, repository: JobRepository, worker_id: str, heavy_task_limiter: HeavyTaskLimiter = shared_heavy_task_limiter, payload_tag: str | None = None) -> None:
        self._repository = repository
        self._worker_id = worker_id
        self._heavy_task_limiter = heavy_task_limiter
        self._payload_tag = payload_tag

    async def run_heavy_task(self, kind: HeavyTaskKind, operation: Callable[[], Awaitable[T]], target_id: str | None = None) -> T:
        """Run expensive worker work under the Intel-safe concurrency limits."""
        return cast(T, await self._heavy_task_limiter.run(kind, operation, target_id))

    async def run_once(self, handler: JobHandler, *, stopping: asyncio.Event | None = None) -> bool:
        """Claim, execute, and complete one job; return false when no job is claimable."""
        job = await self._repository.claim(self._worker_id, payload_tag=self._payload_tag)
        if job is None:
            return False
        lease_lost = asyncio.Event()
        interval = max(0.1, min(self._repository._lease_duration.total_seconds() / 3, 10.0))
        async def heartbeat_loop() -> None:
            while not lease_lost.is_set():
                await asyncio.sleep(interval)
                if not await self._repository.heartbeat(job.id, self._worker_id):
                    lease_lost.set()
                    return
        heartbeat = asyncio.create_task(heartbeat_loop())
        shutdown_watcher: asyncio.Task[None] | None = None
        if stopping is not None:
            async def watch_shutdown() -> None:
                await stopping.wait()
                lease_lost.set()
            shutdown_watcher = asyncio.create_task(watch_shutdown())
        try:
            context = ExecutionContext(lease_lost)
            await handler(job, context)
            if lease_lost.is_set() or not await self._repository.complete(job.id, self._worker_id):
                raise LeaseLostError(f"job lease lost before completion: {job.id}")
        except PermanentJobError as error:
            await self._repository.fail(job, self._worker_id, str(error), permanent=True)
        except LeaseLostError:
            raise
        except Exception as error:
            # Sanitized operator-facing code: an explicit ``error_code`` attribute
            # (never the exception message, which may contain a URI) or the type.
            await self._repository.fail(job, self._worker_id, safe_error_code(error), permanent=False)
        finally:
            heartbeat.cancel()
            tasks: list[asyncio.Task[object]] = [heartbeat]
            if shutdown_watcher is not None:
                shutdown_watcher.cancel()
                tasks.append(shutdown_watcher)
            await asyncio.gather(*tasks, return_exceptions=True)
        return True

    async def run_until_idle(self, handler: JobHandler) -> int:
        """Run jobs until this worker cannot atomically claim another one."""
        completed = 0
        while await self.run_once(handler):
            completed += 1
        return completed
