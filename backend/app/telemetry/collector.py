"""Periodic, read-only telemetry and capability collection for monitored targets.

Every ``TELEMETRY_INTERVAL_SECONDS`` the worker visits each active, non-sandbox
target whose ``connection_label`` has an observer credential and:

* records a capability snapshot when the read-only probe result changes;
* registers newly seen collections as (not allowlisted) namespaces;
* collects one completed telemetry window from the first provider, in the
  frozen preference order, that yields observations:
  ``$queryStats`` (cumulative counters differenced against a cursor) →
  existing profiler (``system.profile`` since the last cursor) →
  structured diagnostic log (slow-query events since the last cursor) →
  ``currentOp`` (a point-in-time sample, never autonomy-eligible).

It never enables profiling, never changes server parameters, and persists only
literal-free shapes through :class:`TelemetryPersistenceService`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from inspect import isawaitable
from typing import Any, Mapping
from uuid import UUID, uuid4

import structlog
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import models
from app.mongodb.client import probe_target
from app.mongodb.connections import TargetConnectionRegistry
from app.settings_store import TELEMETRY_CURSOR_SCOPE, TELEMETRY_STATUS_SCOPE, read_setting, upsert_setting
from app.telemetry.persistence import TelemetryPersistenceService
from app.telemetry.providers import (
    TelemetryObservation,
    TelemetrySource,
    _from_command_document,
    _namespace,
    _observation,
    _operation_from_command,
)


_log = structlog.get_logger("telemetry")
_PROFILER_OPERATIONS = {"query": None, "command": None, "getmore": None, "update": "update", "remove": "delete", "insert": "insert"}


async def _maybe_await(value: Any) -> Any:
    return await value if isawaitable(value) else value


async def _aggregate(database: Any, pipeline: list[dict[str, Any]], length: int, collection: str | None = None) -> list[dict[str, Any]]:
    target = database.get_collection(collection) if collection else database
    cursor = await _maybe_await(target.aggregate(pipeline))
    return list(await cursor.to_list(length=length))


def _normalize_operation(observation: TelemetryObservation, reported: str | None, command: Mapping[str, Any]) -> TelemetryObservation:
    if reported is None:
        return observation
    mapped = _PROFILER_OPERATIONS.get(reported, reported)
    operation = mapped if mapped is not None else _operation_from_command(command)
    if operation == observation.operation:
        return observation
    return TelemetryObservation(
        observation.source, observation.operation_count, observation.database, observation.collection, operation,
        observation.normalized_shape, observation.successful_operation_count, observation.failure_count,
        observation.timeout_count, observation.aggregate_execution_time_ms,
    )


def _query_stats_key(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(document.get("key"), sort_keys=True, default=str).encode()).hexdigest()


def _query_stats_counters(document: Mapping[str, Any]) -> tuple[int, float | None]:
    metrics = document.get("metrics") if isinstance(document.get("metrics"), Mapping) else {}
    assert isinstance(metrics, Mapping)
    count = metrics.get("execCount", document.get("execCount", 0))
    micros: Any = metrics.get("totalExecMicros", document.get("totalExecMicros"))
    if isinstance(micros, Mapping):
        micros = micros.get("sum")
    return int(count or 0), float(micros) if isinstance(micros, (int, float)) and not isinstance(micros, bool) else None


async def collect_query_stats(admin: Any, database_name: str, cursor: dict[str, Any]) -> tuple[list[TelemetryObservation], dict[str, Any]]:
    documents = await _aggregate(admin, [{"$queryStats": {}}], 2_000)
    previous: dict[str, list[float]] = dict(cursor.get("query_stats", {}))
    current: dict[str, list[float]] = {}
    observations: list[TelemetryObservation] = []
    for document in documents:
        key = document.get("key")
        if not isinstance(key, Mapping):
            continue
        namespace = _namespace(key.get("ns") or (key.get("queryShape") or {}).get("cmdNs"))
        if namespace is None or namespace[0] != database_name:
            continue
        identity = _query_stats_key(document)
        count, micros = _query_stats_counters(document)
        current[identity] = [count, micros or 0.0]
        before = previous.get(identity)
        delta_count = count - int(before[0]) if before else count
        delta_micros = (micros or 0.0) - float(before[1]) if before else (micros or 0.0)
        if delta_count <= 0:
            continue
        shape = key.get("queryShape", key)
        operation = str(shape.get("command", "unknown")) if isinstance(shape, Mapping) else "unknown"
        observations.append(_observation(
            TelemetrySource.QUERY_STATS, delta_count, namespace, operation, shape,
            successful_operation_count=delta_count, aggregate_execution_time_ms=max(delta_micros, 0.0) / 1_000,
        ))
    # Bound cursor size; unknown shapes simply restart from their cumulative counts.
    return observations, {"query_stats": dict(list(current.items())[:2_000])}


async def collect_profiler(database: Any, database_name: str, cursor: dict[str, Any]) -> tuple[list[TelemetryObservation], dict[str, Any]]:
    status = await database.command({"profile": -1})
    if int(status.get("was", 0)) <= 0:
        return [], {}
    since = cursor.get("profiler_ts")
    match: dict[str, Any] = {"ns": {"$regex": f"^{database_name}\\."}}
    if since:
        match["ts"] = {"$gt": datetime.fromisoformat(str(since))}
    documents = await _aggregate(database, [{"$match": match}, {"$sort": {"ts": 1}}, {"$limit": 5_000}], 5_000, "system.profile")
    observations: list[TelemetryObservation] = []
    latest = since
    for document in documents:
        if str(document.get("ns", "")).endswith(".system.profile"):
            continue
        command = document.get("command", {})
        observation = _from_command_document(TelemetrySource.PROFILER, document)
        if observation is not None:
            observations.append(_normalize_operation(observation, str(document.get("op")) if document.get("op") else None, command if isinstance(command, Mapping) else {}))
        timestamp = document.get("ts")
        if isinstance(timestamp, datetime):
            latest = timestamp.isoformat()
    return observations, ({"profiler_ts": latest} if latest else {})


async def collect_diagnostic_log(admin: Any, database_name: str, cursor: dict[str, Any]) -> tuple[list[TelemetryObservation], dict[str, Any]]:
    result = await admin.command({"getLog": "global"})
    since = str(cursor.get("log_t") or "")
    latest = since
    observations: list[TelemetryObservation] = []
    for line in result.get("log", []):
        if not isinstance(line, str) or '"Slow query"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        stamp = str((event.get("t") or {}).get("$date", ""))
        if since and stamp <= since:
            continue
        attributes = event.get("attr")
        if not isinstance(attributes, Mapping) or not str(attributes.get("ns", "")).startswith(database_name + "."):
            continue
        observation = _from_command_document(TelemetrySource.DIAGNOSTIC_LOG, attributes)
        if observation is not None and observation.operation not in {"unknown"}:
            observations.append(observation)
        latest = max(latest, stamp)
    return observations, ({"log_t": latest} if latest else {})


async def collect_current_op(admin: Any, database_name: str) -> list[TelemetryObservation]:
    result = await admin.command({"currentOp": 1, "$all": False})
    observations: list[TelemetryObservation] = []
    for operation in result.get("inprog", []):
        if not str(operation.get("ns", "")).startswith(database_name + "."):
            continue
        observation = _from_command_document(TelemetrySource.CURRENT_OP, operation)
        if observation is not None:
            observations.append(observation)
    return observations


class TelemetryCollector:
    """Run one collection pass, or loop until asked to stop."""

    def __init__(self, engine: AsyncEngine, connections: TargetConnectionRegistry, *, interval_seconds: float | None = None) -> None:
        self._engine = engine
        self._connections = connections
        self._interval = interval_seconds if interval_seconds is not None else max(10.0, float(os.getenv("TELEMETRY_INTERVAL_SECONDS", "60")))
        self._persistence = TelemetryPersistenceService(engine)

    async def run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            try:
                await self.collect_once()
            except Exception:  # never let telemetry take the worker down
                _log.warning("telemetry_pass_failed", service="worker")
            try:
                await asyncio.wait_for(stopping.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    async def collect_once(self) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for target in await self._targets():
            label = str(target["connection_label"])
            if not self._connections.has_role(label, "observer"):
                continue
            try:
                results[str(target["id"])] = await self._collect_target(target["id"], label)
            except Exception as error:  # sanitized: the exception text may contain a URI
                status = {"state": "ERROR", "error": type(error).__name__, "attempted_at": datetime.now(timezone.utc).isoformat()}
                await upsert_setting(self._engine, TELEMETRY_STATUS_SCOPE, str(target["id"]), status)
                results[str(target["id"])] = status
        return results

    async def _targets(self) -> list[Mapping[str, Any]]:
        targets, mappings = models.Target.__table__, models.EvaluationMapping.__table__
        async with self._engine.connect() as connection:
            sandbox_ids = set((await connection.execute(select(mappings.c.evaluation_target_id).where(mappings.c.mapping_status == "ACTIVE"))).scalars().all())
            rows = (await connection.execute(select(targets).where(targets.c.is_active.is_(True), targets.c.state == "ACTIVE"))).mappings().all()
        return [row for row in rows if row["id"] not in sandbox_ids]

    async def _collect_target(self, target_id: UUID, label: str) -> dict[str, Any]:
        profile = self._connections.profile(label)
        client = self._connections.client(label, "observer")
        admin = client.get_database("admin")
        database = client.get_database(profile.database)
        await self._capability(target_id, client)
        await self._namespaces(target_id, database, profile.database)
        cursor = await read_setting(self._engine, TELEMETRY_CURSOR_SCOPE, str(target_id)) or {}
        cursor.pop("_updated_at", None)
        now = datetime.now(timezone.utc)
        previous_end = cursor.get("window_end")
        started = datetime.fromisoformat(previous_end) if isinstance(previous_end, str) else now - timedelta(seconds=self._interval)
        tried: list[str] = []
        chosen: str | None = None
        observations: list[TelemetryObservation] = []
        updates: dict[str, Any] = {}
        for source in ("QUERY_STATS", "PROFILER", "DIAGNOSTIC_LOG", "CURRENT_OP"):
            try:
                if source == "QUERY_STATS":
                    found, update = await collect_query_stats(admin, profile.database, cursor)
                elif source == "PROFILER":
                    found, update = await collect_profiler(database, profile.database, cursor)
                elif source == "DIAGNOSTIC_LOG":
                    found, update = await collect_diagnostic_log(admin, profile.database, cursor)
                else:
                    found, update = await collect_current_op(admin, profile.database), {}
            except Exception as error:
                tried.append(f"{source}:{type(error).__name__}")
                continue
            # A successful provider advances its own cursor even when quiet.
            updates.update(update)
            tried.append(f"{source}:{len(found)}")
            if found:
                chosen, observations = source, found
                break
        window_id: UUID | None = None
        if chosen is not None:
            window_id = await self._persistence.persist_completed(target_id, chosen, tuple(observations), started_at=started, ended_at=now)
        await upsert_setting(self._engine, TELEMETRY_CURSOR_SCOPE, str(target_id), {**cursor, **updates, "window_end": now.isoformat()})
        status = {
            "state": "COLLECTED" if chosen else "NO_OBSERVATIONS",
            "source": chosen,
            "observations": len(observations),
            "window_id": str(window_id) if window_id else None,
            "providers": tried,
            "attempted_at": now.isoformat(),
            "interval_seconds": self._interval,
        }
        await upsert_setting(self._engine, TELEMETRY_STATUS_SCOPE, str(target_id), status)
        return status

    async def _capability(self, target_id: UUID, client: Any) -> None:
        capabilities = await probe_target(client)
        fingerprint = hashlib.sha256(json.dumps(capabilities, sort_keys=True, default=str).encode()).hexdigest()
        table = models.CapabilitySnapshot.__table__
        async with self._engine.begin() as connection:
            latest = (await connection.execute(select(table.c.fingerprint).where(table.c.target_id == target_id).order_by(table.c.created_at.desc()).limit(1))).scalar_one_or_none()
            if latest == fingerprint:
                return
            now = datetime.now(timezone.utc)
            await connection.execute(insert(table).values(id=uuid4(), created_at=now, updated_at=now, target_id=target_id, capabilities=dict(capabilities), fingerprint=fingerprint))

    async def _namespaces(self, target_id: UUID, database: Any, database_name: str) -> None:
        names = [name for name in await database.list_collection_names() if not name.startswith("system.")]
        table = models.Namespace.__table__
        async with self._engine.begin() as connection:
            existing = set((await connection.execute(select(table.c.name).where(table.c.target_id == target_id))).scalars().all())
            now = datetime.now(timezone.utc)
            for name in sorted(names):
                qualified = f"{database_name}.{name}"
                if qualified not in existing:
                    await connection.execute(insert(table).values(id=uuid4(), created_at=now, updated_at=now, target_id=target_id, name=qualified, allowlisted=False))
