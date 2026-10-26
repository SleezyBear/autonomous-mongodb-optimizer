"""Production sandbox evaluation: copy, replay, measure, and persist paired trials.

This is the worker-side implementation of the orchestrator's
``evaluation_executor`` boundary.  For every frozen candidate of a run it

1. copies the affected collection from the monitored target (read-only
   observer credential) into the separate evaluation replica (sandbox
   credential), bounded by ``EVALUATION_COPY_MAX_DOCUMENTS``;
2. builds a replay plan from the run's immutable workload snapshot;
3. measures paired baseline / candidate arms in the deterministic AB/BA order,
   with an interleaved A/A baseline for noise calibration, while holding the
   shared benchmark/LLM isolation lock;
4. checks result correctness (same documents, valid sort order) in both arms;
5. persists every pair through :class:`DurableEvaluationService`; and
6. captures read-only production baseline probe windows that post-deployment
   monitoring later compares against.

Only optimizer-named indexes are ever created or dropped, and only on the
sandbox copy.  Nothing here touches the monitored target with a write.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from inspect import isawaitable
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

import numpy as np
from pymongo.errors import PyMongoError
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.admission.models import BenchmarkProfile
from app.admission.statistics import deterministic_arm_order
from app.db import models
from app.evaluation.durable import DurableEvaluationService
from app.evaluation.replay import (
    InstantiatedQuery,
    ReplayPlan,
    build_replay_plan,
    instantiate,
    respects_sort,
    result_fingerprint,
)
from app.isolation.lock import BenchmarkOllamaIsolationLock, shared_measurement_lock
from app.mongodb.connections import TargetConnectionRegistry


LATENCY_KEYS = ("p50_latency_ms", "p95_latency_ms", "p99_latency_ms")
BASELINE_PROBE_EVENT = "PRODUCTION_BASELINE_PROBE"
EVALUATION_EVIDENCE_EVENT = "SANDBOX_EVALUATION_EVIDENCE"


class EvaluationExecutionError(RuntimeError):
    """A sanitized, operator-facing evaluation failure code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.error_code = code


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class EvaluationSettings:
    smoke_pairs: int = 10
    autonomous_pairs: int = 30
    publication_pairs: int = 40
    ops_per_arm: int = 40
    warmup_ops: int = 5
    # Write p95 is a protected metric with a 3% limit; a p95 needs enough inserts to be stable.
    write_ops: int = 400
    write_warmup_ops: int = 10
    sample_documents: int = 64
    copy_max_documents: int = 250_000
    copy_batch_size: int = 1_000
    probe_windows: int = 10
    probe_ops: int = 20
    correctness_cap: int = 2_000

    @classmethod
    def from_environment(cls) -> EvaluationSettings:
        return cls(
            smoke_pairs=_env_int("EVALUATION_SMOKE_PAIRS", 10, 3, 40),
            autonomous_pairs=_env_int("EVALUATION_AUTONOMOUS_PAIRS", 30, 10, 30),
            publication_pairs=_env_int("EVALUATION_PUBLICATION_PAIRS", 40, 15, 40),
            ops_per_arm=_env_int("EVALUATION_OPS_PER_ARM", 40, 10, 500),
            warmup_ops=_env_int("EVALUATION_WARMUP_OPS", 5, 0, 100),
            write_ops=_env_int("EVALUATION_WRITE_OPS", 400, 20, 2_000),
            write_warmup_ops=_env_int("EVALUATION_WRITE_WARMUP_OPS", 10, 0, 200),
            sample_documents=_env_int("EVALUATION_SAMPLE_DOCUMENTS", 64, 4, 1_000),
            copy_max_documents=_env_int("EVALUATION_COPY_MAX_DOCUMENTS", 250_000, 1, 50_000_000),
            copy_batch_size=_env_int("EVALUATION_COPY_BATCH_SIZE", 1_000, 10, 10_000),
            probe_windows=_env_int("MONITORING_BASELINE_WINDOWS", 10, 10, 15),
            probe_ops=_env_int("MONITORING_PROBE_OPS", 20, 5, 200),
            correctness_cap=_env_int("EVALUATION_CORRECTNESS_CAP", 2_000, 10, 50_000),
        )

    def pairs_for(self, profile: BenchmarkProfile) -> int:
        if profile is BenchmarkProfile.AUTONOMOUS:
            return self.autonomous_pairs
        if profile is BenchmarkProfile.PUBLICATION:
            return self.publication_pairs
        return self.smoke_pairs


@dataclass(frozen=True)
class ArmMeasurement:
    latencies_ms: tuple[float, ...]
    elapsed_seconds: float
    error_count: int

    def metrics(self) -> dict[str, float]:
        values = list(self.latencies_ms) or [0.0]
        return {
            "p50_latency_ms": max(float(np.percentile(values, 50)), 0.001),
            "p95_latency_ms": max(float(np.percentile(values, 95)), 0.001),
            "p99_latency_ms": max(float(np.percentile(values, 99)), 0.001),
            "throughput_ops_per_second": len(self.latencies_ms) / max(self.elapsed_seconds, 0.000_001),
            "error_count": float(self.error_count),
        }


async def _maybe_await(value: Any) -> Any:
    return await value if isawaitable(value) else value


async def _to_list(cursor: Any, length: int | None) -> list[dict[str, Any]]:
    cursor = await _maybe_await(cursor)
    return list(await cursor.to_list(length=length))


async def sample_documents(collection: Any, size: int) -> list[dict[str, Any]]:
    """Return up to ``size`` random documents (values never leave this process)."""
    return await _to_list(collection.aggregate([{"$sample": {"size": size}}]), size)


def instantiate_queries(plan: ReplayPlan, samples: Sequence[Mapping[str, Any]], count: int, seed: str) -> list[InstantiatedQuery]:
    """Deterministically build ``count`` concrete queries following the plan schedule."""
    if plan.is_empty() or not samples:
        return []
    rng = random.Random(hashlib.sha256(seed.encode()).hexdigest())
    queries: list[InstantiatedQuery] = []
    attempts = 0
    while len(queries) < count and attempts < count * 20:
        attempts += 1
        shape = plan.shapes[plan.schedule[len(queries) % len(plan.schedule)]]
        first = samples[rng.randrange(len(samples))]
        second = samples[rng.randrange(len(samples))]
        query = instantiate(shape, first, second)
        if query is not None:
            queries.append(query)
    return queries


async def _run_query(collection: Any, query: InstantiatedQuery) -> list[dict[str, Any]]:
    sort = list(query.shape.sort) or None
    cursor = collection.find(query.filter, sort=sort, limit=query.shape.limit)
    return await _to_list(cursor, query.shape.limit)


async def measure_reads(collection: Any, queries: Sequence[InstantiatedQuery], warmup: int) -> ArmMeasurement:
    for query in list(queries)[:warmup]:
        try:
            await _run_query(collection, query)
        except PyMongoError:
            pass
    latencies: list[float] = []
    errors = 0
    started = time.perf_counter()
    for query in queries:
        began = time.perf_counter()
        try:
            await _run_query(collection, query)
        except PyMongoError:
            errors += 1
            continue
        latencies.append((time.perf_counter() - began) * 1000)
    return ArmMeasurement(tuple(latencies), time.perf_counter() - started, errors)


async def measure_reads_aa(collection: Any, queries: Sequence[InstantiatedQuery], warmup: int) -> tuple[ArmMeasurement, ArmMeasurement]:
    """Interleave two identical baseline arms so temporal drift affects both."""
    for query in list(queries)[:warmup]:
        try:
            await _run_query(collection, query)
        except PyMongoError:
            pass
    arms: tuple[list[float], list[float]] = ([], [])
    errors = [0, 0]
    elapsed = [0.0, 0.0]
    for query in queries:
        for index in (0, 1):
            began = time.perf_counter()
            try:
                await _run_query(collection, query)
            except PyMongoError:
                errors[index] += 1
                continue
            duration = time.perf_counter() - began
            elapsed[index] += duration
            arms[index].append(duration * 1000)
    return (
        ArmMeasurement(tuple(arms[0]), elapsed[0], errors[0]),
        ArmMeasurement(tuple(arms[1]), elapsed[1], errors[1]),
    )


async def measure_writes(collection: Any, samples: Sequence[Mapping[str, Any]], count: int, seed: str, warmup: int = 0) -> ArmMeasurement:
    """Insert sandbox-only probe documents (cloned shapes), then remove them.

    The first ``warmup`` inserts are not timed, so connection and cache warm-up
    does not land in the protected write-latency percentile.
    """
    rng = random.Random(seed)
    inserted: list[str] = []
    latencies: list[float] = []
    errors = 0
    started = time.perf_counter()
    try:
        for index in range(warmup + count):
            template = dict(samples[rng.randrange(len(samples))])
            identifier = f"optimizer-probe-{uuid4().hex}"
            template["_id"] = identifier
            began = time.perf_counter()
            try:
                await collection.insert_one(template)
            except PyMongoError:
                if index >= warmup:
                    errors += 1
                continue
            inserted.append(identifier)
            if index >= warmup:
                latencies.append((time.perf_counter() - began) * 1000)
    finally:
        if inserted:
            await collection.delete_many({"_id": {"$in": inserted}})
    return ArmMeasurement(tuple(latencies), time.perf_counter() - started, errors)


async def correctness_fingerprints(collection: Any, plan: ReplayPlan, samples: Sequence[Mapping[str, Any]], cap: int, seed: str) -> dict[str, tuple[str, bool]]:
    """Per-shape (result identity fingerprint, sort-order respected)."""
    results: dict[str, tuple[str, bool]] = {}
    rng = random.Random(seed)
    for shape in plan.shapes:
        query = None
        for _ in range(10):
            query = instantiate(shape, samples[rng.randrange(len(samples))], samples[rng.randrange(len(samples))])
            if query is not None:
                break
        if query is None:
            continue
        projection = {"_id": 1, **{name: 1 for name, _ in shape.sort}}
        documents = await _to_list(collection.find(query.filter, projection, sort=list(shape.sort) or None, limit=cap), cap)
        results[shape.query_shape_id] = (result_fingerprint([document.get("_id") for document in documents]), respects_sort(documents, shape.sort))
    return results


def mismatch_count(baseline: Mapping[str, tuple[str, bool]], candidate: Mapping[str, tuple[str, bool]]) -> int:
    mismatches = 0
    for shape_id in set(baseline) | set(candidate):
        left, right = baseline.get(shape_id), candidate.get(shape_id)
        if left is None or right is None or left[0] != right[0] or not right[1]:
            mismatches += 1
    return mismatches


async def drop_owned_index(collection: Any, index_name: str) -> None:
    if not index_name.startswith("optimizer_"):
        raise EvaluationExecutionError("REFUSED_NON_OPTIMIZER_INDEX_DROP")
    information = await collection.index_information()
    if index_name in information:
        await collection.drop_index(index_name)


@dataclass(frozen=True)
class CopyResult:
    collection: str
    documents: int
    indexes: tuple[str, ...]
    skipped_ttl_indexes: tuple[str, ...]


async def copy_collection(source_database: Any, destination_database: Any, name: str, *, maximum_documents: int, batch_size: int) -> CopyResult:
    """Replace the sandbox copy of one collection with the monitored contents."""
    source = source_database.get_collection(name)
    destination = destination_database.get_collection(name)
    estimated = int(await source.estimated_document_count())
    if estimated > maximum_documents:
        raise EvaluationExecutionError("EVALUATION_COPY_LIMIT_EXCEEDED")
    await destination.drop()
    copied = 0
    batch: list[dict[str, Any]] = []
    cursor = await _maybe_await(source.find({}, sort=[("_id", 1)], batch_size=batch_size))
    async for document in cursor:
        batch.append(document)
        copied += 1
        if copied > maximum_documents:
            raise EvaluationExecutionError("EVALUATION_COPY_LIMIT_EXCEEDED")
        if len(batch) >= batch_size:
            await destination.insert_many(batch, ordered=False)
            batch = []
    if batch:
        await destination.insert_many(batch, ordered=False)
    created: list[str] = []
    skipped: list[str] = []
    index_cursor = await _maybe_await(source.list_indexes())
    async for index in index_cursor:
        index_name = str(index["name"])
        if index_name == "_id_":
            continue
        if "expireAfterSeconds" in index:
            # TTL deletion would mutate the measured sandbox while it is measured.
            skipped.append(index_name)
            continue
        options = {key: index[key] for key in ("unique", "sparse", "partialFilterExpression", "collation") if key in index}
        await destination.create_index(list(index["key"].items()), name=index_name, **options)
        created.append(index_name)
    actual = int(await destination.count_documents({}))
    if actual != copied:
        raise EvaluationExecutionError("EVALUATION_COPY_VERIFICATION_FAILED")
    return CopyResult(name, copied, tuple(sorted(created)), tuple(sorted(skipped)))


class SandboxEvaluationExecutor:
    """Callable ``evaluation_executor`` for :class:`OptimizationRunOrchestrator`."""

    def __init__(
        self,
        engine: AsyncEngine,
        connections: TargetConnectionRegistry,
        *,
        settings: EvaluationSettings | None = None,
        isolation: BenchmarkOllamaIsolationLock = shared_measurement_lock,
    ) -> None:
        self._engine = engine
        self._connections = connections
        self._settings = settings or EvaluationSettings.from_environment()
        self._isolation = isolation
        self._service = DurableEvaluationService(engine)

    async def __call__(self, run_id: UUID) -> None:
        context = await self._load(run_id)
        monitored_label, evaluation_label = context["monitored_label"], context["evaluation_label"]
        monitored_profile = self._connections.profile(monitored_label)
        evaluation_profile = self._connections.profile(evaluation_label)
        if not self._connections.has_role(monitored_label, "observer"):
            raise EvaluationExecutionError("MONITORED_OBSERVER_CONNECTION_REQUIRED")
        if not self._connections.has_role(evaluation_label, "sandbox"):
            raise EvaluationExecutionError("EVALUATION_SANDBOX_CONNECTION_REQUIRED")
        source_database = self._connections.client(monitored_label, "observer").get_database(monitored_profile.database)
        sandbox_client = self._connections.client(evaluation_label, "sandbox")
        sandbox_database = sandbox_client.get_database(evaluation_profile.database)
        profile = BenchmarkProfile(context["profile"])
        pair_target = self._settings.pairs_for(profile)
        evidence: dict[str, Any] = {"profile": profile.value, "pairs_per_candidate": pair_target, "collections": {}}
        copies: dict[str, CopyResult] = {}
        plans: dict[str, ReplayPlan] = {}
        hello = await sandbox_client.get_database("admin").command({"hello": 1})
        build = await sandbox_client.get_database("admin").command({"buildInfo": 1})
        for candidate in context["candidates"]:
            action = candidate["action_payload"]
            collection_name = str(action["collection"])
            if str(action["database"]) != monitored_profile.database:
                raise EvaluationExecutionError("CANDIDATE_DATABASE_NOT_CONFIGURED")
            if collection_name not in copies:
                copies[collection_name] = await copy_collection(
                    source_database, sandbox_database, collection_name,
                    maximum_documents=self._settings.copy_max_documents, batch_size=self._settings.copy_batch_size,
                )
                plans[collection_name] = build_replay_plan(collection_name, context["shapes"].get(collection_name, []))
                evidence["collections"][collection_name] = {
                    "copied_documents": copies[collection_name].documents,
                    "copied_indexes": list(copies[collection_name].indexes),
                    "skipped_ttl_indexes": list(copies[collection_name].skipped_ttl_indexes),
                    "replay_plan": plans[collection_name].describe(),
                }
            plan = plans[collection_name]
            if plan.is_empty():
                raise EvaluationExecutionError("NO_REPLAYABLE_QUERY_SHAPES")
            collection = sandbox_database.get_collection(collection_name)
            samples = await sample_documents(collection, self._settings.sample_documents)
            if not samples:
                raise EvaluationExecutionError("EVALUATION_COLLECTION_EMPTY")
            environment = hashlib.sha256(json.dumps({
                "version": build.get("version"), "set": hello.get("setName"), "collection": collection_name,
                "documents": copies[collection_name].documents, "indexes": copies[collection_name].indexes,
            }, sort_keys=True, default=str).encode()).hexdigest()[:32]
            await self._evaluate_candidate(run_id, candidate, collection, plan, samples, environment, pair_target)
        await self._capture_baseline_probes(run_id, source_database, plans, evidence)
        await self._audit(run_id, EVALUATION_EVIDENCE_EVENT, evidence)

    async def _evaluate_candidate(self, run_id: UUID, candidate: Mapping[str, Any], collection: Any, plan: ReplayPlan, samples: list[dict[str, Any]], environment: str, pair_target: int) -> None:
        candidate_id = candidate["id"]
        action = candidate["action_payload"]
        index_name = str(action["index_name"])
        keys = [(str(item["field"]), int(item["direction"])) for item in action["fields"]]
        recorded = await self._recorded_pairs(candidate_id)
        settings = self._settings
        for pair_number in range(1, pair_target + 1):
            if pair_number in recorded:
                continue
            seed = f"{run_id}:{candidate_id}:{pair_number}"
            queries = instantiate_queries(plan, samples, settings.ops_per_arm, seed)
            if not queries:
                raise EvaluationExecutionError("REPLAY_INSTANTIATION_FAILED")
            order = deterministic_arm_order(str(run_id), pair_number)
            await drop_owned_index(collection, index_name)
            async with self._isolation.benchmark_window():
                if order == "AB":
                    baseline, aa_second, base_writes, aa_writes, base_check = await self._baseline_arm(collection, plan, queries, samples, seed)
                    await collection.create_index(keys, name=index_name)
                    candidate_arm, cand_writes, cand_check = await self._candidate_arm(collection, plan, queries, samples, seed)
                    await drop_owned_index(collection, index_name)
                else:
                    await collection.create_index(keys, name=index_name)
                    candidate_arm, cand_writes, cand_check = await self._candidate_arm(collection, plan, queries, samples, seed)
                    await drop_owned_index(collection, index_name)
                    baseline, aa_second, base_writes, aa_writes, base_check = await self._baseline_arm(collection, plan, queries, samples, seed)
            base_metrics = baseline.metrics()
            second_metrics = aa_second.metrics()
            cand_metrics = candidate_arm.metrics()
            baseline_values: dict[str, float] = {**base_metrics, "result_mismatch_count": 0.0}
            candidate_values: dict[str, float] = {**cand_metrics, "result_mismatch_count": float(mismatch_count(base_check, cand_check))}
            for key in (*LATENCY_KEYS, "throughput_ops_per_second"):
                baseline_values[f"aa_{key}"] = base_metrics[key]
                candidate_values[f"aa_{key}"] = second_metrics[key]
            if base_writes is not None and cand_writes is not None and aa_writes is not None:
                baseline_values["write_p95_latency_ms"] = base_writes.metrics()["p95_latency_ms"]
                candidate_values["write_p95_latency_ms"] = cand_writes.metrics()["p95_latency_ms"]
                baseline_values["aa_write_p95_latency_ms"] = base_writes.metrics()["p95_latency_ms"]
                candidate_values["aa_write_p95_latency_ms"] = aa_writes.metrics()["p95_latency_ms"]
            await self._service.record_pair(candidate_id, pair_number, environment, baseline_values, candidate_values, order)
        await drop_owned_index(collection, index_name)
        await self._service.mark_complete(candidate_id)

    async def _baseline_arm(self, collection: Any, plan: ReplayPlan, queries: list[InstantiatedQuery], samples: list[dict[str, Any]], seed: str) -> tuple[ArmMeasurement, ArmMeasurement, ArmMeasurement | None, ArmMeasurement | None, dict[str, tuple[str, bool]]]:
        first, second = await measure_reads_aa(collection, queries, self._settings.warmup_ops)
        writes = aa_writes = None
        if plan.has_writes:
            writes = await measure_writes(collection, samples, self._settings.write_ops, seed + ":w1", self._settings.write_warmup_ops)
            aa_writes = await measure_writes(collection, samples, self._settings.write_ops, seed + ":w2", self._settings.write_warmup_ops)
        check = await correctness_fingerprints(collection, plan, samples, self._settings.correctness_cap, seed)
        return first, second, writes, aa_writes, check

    async def _candidate_arm(self, collection: Any, plan: ReplayPlan, queries: list[InstantiatedQuery], samples: list[dict[str, Any]], seed: str) -> tuple[ArmMeasurement, ArmMeasurement | None, dict[str, tuple[str, bool]]]:
        measured = await measure_reads(collection, queries, self._settings.warmup_ops)
        writes = None
        if plan.has_writes:
            writes = await measure_writes(collection, samples, self._settings.write_ops, seed + ":w1", self._settings.write_warmup_ops)
        check = await correctness_fingerprints(collection, plan, samples, self._settings.correctness_cap, seed)
        return measured, writes, check

    async def _capture_baseline_probes(self, run_id: UUID, source_database: Any, plans: Mapping[str, ReplayPlan], evidence: dict[str, Any]) -> None:
        """Measure read-only production probe windows before any deployment."""
        windows: list[dict[str, Any]] = []
        shares: dict[str, float] = {}
        for collection_name, plan in plans.items():
            for shape in plan.shapes:
                shares[shape.query_shape_hash] = shares.get(shape.query_shape_hash, 0.0) + shape.weight
        for index in range(self._settings.probe_windows):
            window = await probe_window(source_database, plans, self._settings.probe_ops, f"{run_id}:baseline:{index}")
            windows.append(window)
        total = sum(shares.values()) or 1.0
        payload = {
            "windows": windows,
            "workload": [{"name": name, "fraction": value / total} for name, value in sorted(shares.items())],
            "plans": {name: plan.describe() for name, plan in plans.items()},
        }
        evidence["baseline_probe_windows"] = len(windows)
        await self._audit(run_id, BASELINE_PROBE_EVENT, payload)

    async def _recorded_pairs(self, candidate_id: UUID) -> set[int]:
        async with self._engine.connect() as connection:
            rows = (await connection.execute(
                select(models.TrialPair.__table__.c.pair_number)
                .join(models.EvaluationRun.__table__, models.EvaluationRun.__table__.c.id == models.TrialPair.__table__.c.evaluation_run_id)
                .where(models.EvaluationRun.__table__.c.candidate_id == candidate_id)
            )).scalars().all()
        return {int(value) for value in rows}

    async def _audit(self, run_id: UUID, event_type: str, payload: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        async with self._engine.begin() as connection:
            await connection.execute(insert(models.AuditEvent.__table__).values(
                id=uuid4(), created_at=now, updated_at=now, actor_user_id=None,
                optimization_run_id=run_id, event_type=event_type, event_payload=json.loads(json.dumps(payload, default=str)),
            ))

    async def _load(self, run_id: UUID) -> dict[str, Any]:
        run_table, plan_table = models.OptimizationRun.__table__, models.EvaluationPlan.__table__
        async with self._engine.connect() as connection:
            run = (await connection.execute(select(run_table).where(run_table.c.id == run_id))).mappings().one_or_none()
            plan = (await connection.execute(select(plan_table).where(plan_table.c.optimization_run_id == run_id))).mappings().one_or_none()
            if run is None or plan is None or run["workload_snapshot_id"] is None:
                raise EvaluationExecutionError("EVALUATION_PLAN_REQUIRED")
            target = (await connection.execute(select(models.Target.__table__).where(models.Target.__table__.c.id == run["target_id"]))).mappings().one()
            mapping = (await connection.execute(select(models.EvaluationMapping.__table__).where(
                models.EvaluationMapping.__table__.c.target_id == run["target_id"],
                models.EvaluationMapping.__table__.c.mapping_status == "ACTIVE",
            ))).mappings().one_or_none()
            if mapping is None:
                raise EvaluationExecutionError("EVALUATION_MAPPING_REQUIRED")
            evaluation_target = (await connection.execute(select(models.Target.__table__).where(models.Target.__table__.c.id == mapping["evaluation_target_id"]))).mappings().one()
            selected = [UUID(value) for value in plan["selected_candidate_ids"]]
            candidate_rows = (await connection.execute(
                select(models.Candidate.__table__.c.id, models.CandidateAction.__table__.c.action_payload)
                .join(models.CandidateAction.__table__, models.CandidateAction.__table__.c.candidate_id == models.Candidate.__table__.c.id)
                .where(models.Candidate.__table__.c.id.in_(selected))
            )).mappings().all()
            shapes = (await connection.execute(
                select(
                    models.WorkloadSnapshotQueryShape.__table__.c.query_shape_id,
                    models.WorkloadSnapshotQueryShape.__table__.c.query_shape_hash,
                    models.WorkloadSnapshotQueryShape.__table__.c.operation,
                    models.WorkloadSnapshotQueryShape.__table__.c.workload_operation_share,
                    models.QueryShape.__table__.c.normalized_shape,
                    models.Namespace.__table__.c.name.label("namespace"),
                )
                .join(models.QueryShape.__table__, models.QueryShape.__table__.c.id == models.WorkloadSnapshotQueryShape.__table__.c.query_shape_id)
                .join(models.Namespace.__table__, models.Namespace.__table__.c.id == models.QueryShape.__table__.c.namespace_id)
                .where(models.WorkloadSnapshotQueryShape.__table__.c.workload_snapshot_id == run["workload_snapshot_id"])
            )).mappings().all()
        by_id = {row["id"]: row for row in candidate_rows}
        candidates = [by_id[candidate_id] for candidate_id in selected if candidate_id in by_id]
        if len(candidates) != len(selected):
            raise EvaluationExecutionError("EVALUATION_CANDIDATE_ACTION_MISSING")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in shapes:
            _database, _, collection = str(row["namespace"]).partition(".")
            grouped.setdefault(collection, []).append(dict(row))
        return {
            "profile": str(plan["profile"]),
            "monitored_label": str(target["connection_label"]),
            "evaluation_label": str(evaluation_target["connection_label"]),
            "candidates": candidates,
            "shapes": grouped,
        }


async def probe_window(database: Any, plans: Mapping[str, ReplayPlan], operations: int, seed: str) -> dict[str, Any]:
    """One read-only probe window against a live database (observer credential)."""
    latencies: list[float] = []
    errors = 0
    timeouts = 0
    started = time.perf_counter()
    for collection_name, plan in plans.items():
        if plan.is_empty():
            continue
        collection = database.get_collection(collection_name)
        samples = await sample_documents(collection, 32)
        queries = instantiate_queries(plan, samples, max(1, operations // max(len(plans), 1)), seed + collection_name)
        measured = await measure_reads(collection, queries, 0)
        latencies.extend(measured.latencies_ms)
        errors += measured.error_count
    elapsed = time.perf_counter() - started
    values = latencies or [0.0]
    return {
        "p50_latency_ms": float(np.percentile(values, 50)),
        "p95_latency_ms": float(np.percentile(values, 95)),
        "p99_latency_ms": float(np.percentile(values, 99)),
        "operations": len(latencies),
        "throughput_ops_per_second": len(latencies) / max(elapsed, 0.000_001),
        "error_count": errors,
        "timeout_count": timeouts,
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }
