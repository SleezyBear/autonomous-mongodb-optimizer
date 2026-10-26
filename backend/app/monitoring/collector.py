"""Collect real post-deployment monitoring windows for a deployed run.

Baseline windows are the read-only production probes captured during sandbox
evaluation (before any deployment).  Observed windows repeat the identical
probe plan against the monitored target after deployment, spaced by
``MONITORING_WINDOW_SECONDS``.  Replica health is read from
``replSetGetStatus``.  Only the observer credential is used.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import models
from app.evaluation.executor import BASELINE_PROBE_EVENT, probe_window
from app.evaluation.replay import ReplayPlan, ReplayShape, FieldTemplate
from app.metrics.collector import MetricSnapshot
from app.monitoring.post_deployment import MonitoringWindow
from app.mongodb.connections import TargetConnectionRegistry
from app.workloads.snapshots import Share


class MonitoringBaselineMissing(OSError):
    """No pre-deployment production baseline exists yet (treated as transient)."""

    error_code = "MONITORING_BASELINE_MISSING"


def window_from_probe(probe: dict[str, Any], workload: tuple[Share, ...], replica_healthy: bool) -> MonitoringWindow:
    p99 = float(probe["p99_latency_ms"])
    metrics = MetricSnapshot(
        float(probe["p50_latency_ms"]), float(probe["p95_latency_ms"]), p99,
        float(probe.get("throughput_ops_per_second", 0.0)), float(probe.get("throughput_ops_per_second", 0.0)), 0.0,
        None, None, None, None, None, None, 0, int(probe.get("operations", 0)), 0, 0.0,
        int(probe.get("error_count", 0)), int(probe.get("timeout_count", 0)),
    )
    return MonitoringWindow(metrics, workload, replica_healthy)


def plan_from_description(description: dict[str, Any]) -> ReplayPlan:
    shapes = tuple(
        ReplayShape(
            str(item["query_shape_id"]), str(item["query_shape_hash"]), str(description["collection"]),
            tuple(FieldTemplate(str(field["field"]), str(field["kind"]), lower=bool(field.get("lower")), upper=bool(field.get("upper"))) for field in item["fields"]),
            tuple((str(name), int(direction)) for name, direction in item["sort"]),
            int(item["limit"]), float(item["weight"]),
        )
        for item in description.get("shapes", [])
    )
    schedule = tuple(range(len(shapes)))
    return ReplayPlan(str(description["collection"]), shapes, (), bool(description.get("has_writes")), schedule)


async def replica_healthy(client: Any) -> bool:
    try:
        status = await client.get_database("admin").command({"replSetGetStatus": 1})
    except Exception:  # standalone servers and permission gaps are not "unhealthy"
        return True
    members = status.get("members", [])
    return all(int(member.get("health", 1)) == 1 for member in members)


class ProductionMonitoringCollector:
    """``collect_windows`` callable for :class:`DurableMonitoringService`."""

    def __init__(self, engine: AsyncEngine, connections: TargetConnectionRegistry) -> None:
        self._engine = engine
        self._connections = connections
        self._windows = max(1, min(15, int(os.getenv("MONITORING_WINDOWS", "3"))))
        self._interval = max(0.0, float(os.getenv("MONITORING_WINDOW_SECONDS", "20")))
        self._operations = max(5, int(os.getenv("MONITORING_PROBE_OPS", "20")))

    async def __call__(self, run_id: UUID) -> tuple[tuple[MonitoringWindow, ...], tuple[MonitoringWindow, ...]]:
        async with self._engine.connect() as connection:
            baseline = (await connection.execute(
                select(models.AuditEvent.__table__.c.event_payload)
                .where(models.AuditEvent.__table__.c.optimization_run_id == run_id, models.AuditEvent.__table__.c.event_type == BASELINE_PROBE_EVENT)
                .order_by(models.AuditEvent.__table__.c.created_at.desc()).limit(1)
            )).scalar_one_or_none()
            target = (await connection.execute(
                select(models.Target.__table__.c.connection_label)
                .join(models.OptimizationRun.__table__, models.OptimizationRun.__table__.c.target_id == models.Target.__table__.c.id)
                .where(models.OptimizationRun.__table__.c.id == run_id)
            )).scalar_one()
        if not isinstance(baseline, dict) or not baseline.get("windows"):
            raise MonitoringBaselineMissing("MONITORING_BASELINE_MISSING")
        workload = tuple(Share(str(item["name"]), float(item["fraction"])) for item in baseline.get("workload", []))
        plans = {name: plan_from_description(description) for name, description in baseline.get("plans", {}).items()}
        label = str(target)
        profile = self._connections.profile(label)
        client = self._connections.client(label, "observer")
        database = client.get_database(profile.database)
        baseline_windows = tuple(window_from_probe(window, workload, True) for window in baseline["windows"])
        observed: list[MonitoringWindow] = []
        for index in range(self._windows):
            if index:
                await asyncio.sleep(self._interval)
            probe = await probe_window(database, plans, self._operations, f"{run_id}:observed:{index}")
            observed.append(window_from_probe(probe, workload, await replica_healthy(client)))
        return baseline_windows, tuple(observed)
