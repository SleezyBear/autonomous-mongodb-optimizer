"""End-to-end production worker pipeline on real PostgreSQL with an in-memory Mongo double.

This is the path that previously could not run from the shipped worker: an
operator onboards targets through the API, the worker collects telemetry,
and one durable run goes CREATED → … → APPROVAL_PENDING → (approved) →
DEPLOYED → MONITORING → COMPLETED(DEPLOYMENT_SUCCEEDED) using the production
composition root, the sandbox evaluator, statistical admission, and live
monitoring probes.  The deterministic advisory provider stands in for an LLM.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.ai.provider import FakeAIProvider
from app.auth.security import JwtService, Principal, Role
from app.db.repositories import create_repositories
from app.main import app
from app.mongodb.connections import ConnectionProfile, TargetConnectionRegistry
from app.settings_store import WORKER_SCOPE, WORKER_STATUS_KEY, upsert_setting
from app.telemetry.collector import TelemetryCollector
from app.worker.composition import ProductionOrchestratorFactory
from app.worker.durable import DurableJobWorker, JobRepository
from app.worker.service import JobDispatcher, OptimizationJobHandler
from tests.support.fake_mongo import FakeClient, FakeServer


_KEY = "production-pipeline-signing-key-long-enough"


class FakeRegistry(TargetConnectionRegistry):
    def __init__(self, servers: dict[str, FakeServer]) -> None:
        super().__init__({
            "fake-monitored": ConnectionProfile("fake-monitored", "commerce", "mongodb://observer@monitored", "mongodb://executor@monitored", None),
            "fake-sandbox": ConnectionProfile("fake-sandbox", "commerce", None, None, "mongodb://sandbox@sandbox"),
        })
        self._servers = servers

    def client(self, label: str, role: str) -> Any:  # type: ignore[override]
        if not self.has_role(label, role):
            raise AssertionError(f"role {role} not configured for {label}")
        return FakeClient(self._servers[label])


def _headers(user_id: UUID, role: Role) -> dict[str, str]:
    return {"Authorization": "Bearer " + JwtService(_KEY).issue(Principal(str(user_id), role))}


def _slow_query_lines(count: int) -> list[str]:
    base = datetime.now(timezone.utc) - timedelta(seconds=30)
    lines = []
    for index in range(count):
        lines.append(json.dumps({
            "t": {"$date": (base + timedelta(milliseconds=index)).isoformat()},
            "msg": "Slow query",
            "attr": {"type": "command", "ns": "commerce.orders", "command": {"find": "orders", "filter": {"customer_id": index % 300}, "$db": "commerce"}, "durationMillis": 15},
        }))
    return lines


async def _drain(worker: DurableJobWorker, dispatcher: JobDispatcher) -> None:
    for _ in range(4):
        if not await worker.run_once(dispatcher.dispatch):
            return


@pytest.mark.asyncio
async def test_operator_onboarding_to_monitored_deployment(disposable_worker_database: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", disposable_worker_database)
    monkeypatch.setenv("JWT_SIGNING_KEY", _KEY)
    for name, value in {
        "EVALUATION_SMOKE_PAIRS": "4", "EVALUATION_OPS_PER_ARM": "12", "EVALUATION_WARMUP_OPS": "1",
        "MONITORING_WINDOWS": "2", "MONITORING_WINDOW_SECONDS": "0", "MONITORING_PROBE_OPS": "6",
    }.items():
        monkeypatch.setenv(name, value)

    monitored, sandbox = FakeServer("rs-monitored"), FakeServer("rs-sandbox")
    orders = monitored.client().get_database("commerce").get_collection("orders")
    await orders.insert_many([{"_id": index, "customer_id": index % 300, "status": "open" if index % 3 else "closed"} for index in range(3_000)])
    monitored.log_lines = _slow_query_lines(50)
    registry = FakeRegistry({"fake-monitored": monitored, "fake-sandbox": sandbox})

    engine = create_async_engine(disposable_worker_database)
    operator, approver = uuid4(), uuid4()
    async with engine.begin() as connection:
        for user, role in ((operator, "OPERATOR"), (approver, "APPROVER")):
            await connection.execute(text("INSERT INTO users (id,created_at,updated_at,email,password_hash,role,status,failed_login_count) VALUES (:id,now(),now(),:email,'unused',:role,'ACTIVE',0)"), {"id": user, "email": f"{role.lower()}-{user.hex}@example.test", "role": role})
    await upsert_setting(engine, WORKER_SCOPE, WORKER_STATUS_KEY, {"heartbeat_at": datetime.now(timezone.utc).isoformat(), "connections": registry.public_document(), "ai": {"provider": "deterministic"}})

    op_headers, approver_headers = _headers(operator, Role.OPERATOR), _headers(approver, Role.APPROVER)
    with TestClient(app) as client:
        viewer = client.post("/api/v1/targets", headers=_headers(approver, Role.APPROVER), json={"name": "nope", "connection_label": "x"})
        assert viewer.status_code == 403
        target = client.post("/api/v1/targets", headers=op_headers, json={"name": "orders-prod", "connection_label": "fake-monitored"}).json()
        sandbox_target = client.post("/api/v1/targets", headers=op_headers, json={"name": "orders-sandbox", "connection_label": "fake-sandbox"}).json()
        mapped = client.post(f"/api/v1/targets/{target['id']}/evaluation-mapping", headers=op_headers, json={"evaluation_target_id": sandbox_target["id"]})
        assert mapped.status_code == 200, mapped.text
        same = client.post(f"/api/v1/targets/{target['id']}/evaluation-mapping", headers=op_headers, json={"evaluation_target_id": target["id"]})
        assert same.status_code == 422

    collected = await TelemetryCollector(engine, registry, interval_seconds=60).collect_once()
    assert collected[target["id"]]["state"] == "COLLECTED"
    assert collected[target["id"]]["source"] == "DIAGNOSTIC_LOG"
    # A sandbox target is never polled for telemetry.
    assert sandbox_target["id"] not in collected

    with TestClient(app) as client:
        detail = client.get(f"/api/v1/targets/{target['id']}", headers=op_headers).json()
        assert {check["key"]: check["ok"] for check in detail["checks"]} == {"connection": True, "evaluation": True, "capability": True, "telemetry": True, "allowlist": False}
        namespace = next(item for item in detail["namespaces"] if item["name"] == "commerce.orders")
        allow = client.post(f"/api/v1/targets/{target['id']}/namespaces/{namespace['id']}/allowlist", headers=op_headers, json={"allowlisted": True})
        assert allow.status_code == 200
        assert client.get(f"/api/v1/targets/{target['id']}", headers=op_headers).json()["ready"] is True
        created = client.post("/api/v1/runs", headers=op_headers, json={"target_id": target["id"], "deployment_mode": "APPROVAL_CONTROLLED", "primary_metric_key": "p99_latency_ms"})
        assert created.status_code == 201, created.text
        run_id = created.json()["run_id"]

    repositories = create_repositories(engine)
    handler = OptimizationJobHandler(repositories, orchestrator_factory=ProductionOrchestratorFactory(engine, repositories, registry, FakeAIProvider()))
    dispatcher = JobDispatcher(handler)
    worker = DurableJobWorker(JobRepository(engine), "pipeline-worker")
    await _drain(worker, dispatcher)

    with TestClient(app) as client:
        pending = client.get(f"/api/v1/runs/{run_id}", headers=op_headers).json()
        assert pending["run"]["status"] == "APPROVAL_PENDING", pending["run"]
        candidates = pending["evidence"]["candidates"]
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate["action"]["payload"]["fields"] == [{"field": "customer_id", "direction": 1}]
        assert candidate["admission"]["verdict"] == "ADMITTED"
        assert candidate["admission"]["details"]["primary_benefit_result"]["benefit_passed"] is True
        summary = candidate["evaluation"]["summary"]["p99_latency_ms"]
        assert summary["candidate_median"] < summary["baseline_median"]
        assert len(candidate["trials"]) == 4
        assert all(trial["candidate"]["result_mismatch_count"] == 0 for trial in candidate["trials"])
        assert pending["evidence"]["shapes"][0]["operation"] == "find"
        events = {event["event_type"] for event in pending["evidence"]["events"]}
        assert {"SANDBOX_EVALUATION_EVIDENCE", "PRODUCTION_BASELINE_PROBE"} <= events
        # The sandbox evaluation never created an index on the monitored target.
        assert set(orders.indexes) == {"_id_"}

        inbox = client.get("/api/v1/approvals/inbox", headers=approver_headers).json()
        assert inbox["pending"] == 1 and inbox["records"][0]["can_decide"] is True
        own = client.get("/api/v1/approvals/inbox", headers=op_headers).json()
        assert own["records"][0]["can_decide"] is False
        approval_id = inbox["records"][0]["id"]
        approved = client.post(f"/api/v1/approvals/{approval_id}/approve", headers=approver_headers, json={})
        assert approved.status_code == 200, approved.text

    await _drain(worker, dispatcher)

    with TestClient(app) as client:
        final = client.get(f"/api/v1/runs/{run_id}", headers=op_headers).json()
        assert final["run"]["status"] == "COMPLETED", final["run"]
        assert final["run"]["completion_reason"] == "DEPLOYMENT_SUCCEEDED"
        assert [entry["event_type"] for entry in final["evidence"]["ledger"]] == ["DEPLOYMENT_INTENT", "DEPLOYMENT_APPLIED"]
        assert "DURABLE_POST_DEPLOYMENT_MONITORING" in {event["event_type"] for event in final["evidence"]["events"]}
        assert final["evidence"]["approval_decision"]["decision"] == "APPROVED"
        overview = client.get("/api/v1/overview", headers=op_headers).json()
        assert overview["runs_by_status"] == {"COMPLETED": 1}
        assert overview["deployments_last_7_days"] == 1
        verification = client.get("/api/v1/ledger/verify", headers=op_headers).json()
        assert verification["verified"] is True and verification["targets"][0]["entries"] == 2
        ledger = client.get("/api/v1/ledger", headers=op_headers).json()["records"]
        assert ledger[0]["event_type"] == "DEPLOYMENT_APPLIED"
    deployed = [name for name in orders.indexes if name.startswith("optimizer_")]
    assert deployed == [candidate["action"]["payload"]["index_name"]]

    # A second run on the same target reuses overlapping telemetry; the deployed
    # index now covers the shape, so it completes safely with no candidates.
    with TestClient(app) as client:
        second = client.post("/api/v1/runs", headers=op_headers, json={"target_id": target["id"]}).json()["run_id"]
    await _drain(worker, dispatcher)
    with TestClient(app) as client:
        again = client.get(f"/api/v1/runs/{second}", headers=op_headers).json()["run"]
    assert (again["status"], again["completion_reason"]) == ("COMPLETED", "NO_CANDIDATES"), again
    await engine.dispose()


@pytest.mark.asyncio
async def test_stalled_run_reports_blocker_and_can_be_resumed(disposable_worker_database: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", disposable_worker_database)
    monkeypatch.setenv("JWT_SIGNING_KEY", _KEY)
    engine = create_async_engine(disposable_worker_database)
    operator = uuid4()
    async with engine.begin() as connection:
        await connection.execute(text("INSERT INTO users (id,created_at,updated_at,email,password_hash,role,status,failed_login_count) VALUES (:id,now(),now(),:email,'unused','OPERATOR','ACTIVE',0)"), {"id": operator, "email": f"op-{operator.hex}@example.test"})
    registry = FakeRegistry({"fake-monitored": FakeServer("a"), "fake-sandbox": FakeServer("b")})
    headers = _headers(operator, Role.OPERATOR)
    with TestClient(app) as client:
        target = client.post("/api/v1/targets", headers=headers, json={"name": "quiet", "connection_label": "fake-monitored"}).json()
        sandbox_target = client.post("/api/v1/targets", headers=headers, json={"name": "quiet-sandbox", "connection_label": "fake-sandbox"}).json()
        client.post(f"/api/v1/targets/{target['id']}/evaluation-mapping", headers=headers, json={"evaluation_target_id": sandbox_target["id"]})
        run_id = client.post("/api/v1/runs", headers=headers, json={"target_id": target["id"]}).json()["run_id"]
        early = client.post(f"/api/v1/runs/{run_id}/resume", headers=headers)
        assert early.status_code == 409

    repositories = create_repositories(engine)
    handler = OptimizationJobHandler(repositories, orchestrator_factory=ProductionOrchestratorFactory(engine, repositories, registry, FakeAIProvider()))
    worker = DurableJobWorker(JobRepository(engine, max_attempts=1), "stall-worker")
    assert await worker.run_once(JobDispatcher(handler).dispatch)

    with TestClient(app) as client:
        detail = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
        # No telemetry exists, so the snapshot stage blocks with a readable code.
        assert detail["run"]["status"] == "SNAPSHOTTING"
        assert detail["run"]["job_status"] == "FAILED"
        assert detail["run"]["last_error_code"] == "NO_COMPLETED_TELEMETRY"
        listed = client.get("/api/v1/runs", headers=headers).json()["records"][0]
        assert listed["last_error_code"] == "NO_COMPLETED_TELEMETRY" and listed["target_name"] == "quiet"
        overview = client.get("/api/v1/overview", headers=headers).json()
        assert overview["stalled_runs"][0]["last_error_code"] == "NO_COMPLETED_TELEMETRY"
        resumed = client.post(f"/api/v1/runs/{run_id}/resume", headers=headers)
        assert resumed.status_code == 200, resumed.text
        assert client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["run"]["job_status"] == "PENDING"
    await engine.dispose()


@pytest.mark.asyncio
async def test_post_deployment_regression_rolls_back_only_the_owned_index(disposable_worker_database: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.support import fake_mongo

    monkeypatch.setenv("DATABASE_URL", disposable_worker_database)
    monkeypatch.setenv("JWT_SIGNING_KEY", _KEY)
    for name, value in {
        "EVALUATION_SMOKE_PAIRS": "3", "EVALUATION_OPS_PER_ARM": "10", "EVALUATION_WARMUP_OPS": "1",
        "MONITORING_WINDOWS": "1", "MONITORING_WINDOW_SECONDS": "0", "MONITORING_PROBE_OPS": "5",
    }.items():
        monkeypatch.setenv(name, value)
    monitored, sandbox = FakeServer("rs-monitored"), FakeServer("rs-sandbox")
    orders = monitored.client().get_database("commerce").get_collection("orders")
    await orders.insert_many([{"_id": index, "customer_id": index % 200} for index in range(2_000)])
    await orders.create_index([("legacy_field", 1)], name="human_legacy")
    monitored.log_lines = _slow_query_lines(20)
    registry = FakeRegistry({"fake-monitored": monitored, "fake-sandbox": sandbox})
    engine = create_async_engine(disposable_worker_database)
    operator, approver = uuid4(), uuid4()
    async with engine.begin() as connection:
        for user, role in ((operator, "OPERATOR"), (approver, "APPROVER")):
            await connection.execute(text("INSERT INTO users (id,created_at,updated_at,email,password_hash,role,status,failed_login_count) VALUES (:id,now(),now(),:email,'unused',:role,'ACTIVE',0)"), {"id": user, "email": f"{role.lower()}-{user.hex}@example.test", "role": role})
    headers, approver_headers = _headers(operator, Role.OPERATOR), _headers(approver, Role.APPROVER)
    with TestClient(app) as client:
        target = client.post("/api/v1/targets", headers=headers, json={"name": "regress", "connection_label": "fake-monitored"}).json()
        sandbox_target = client.post("/api/v1/targets", headers=headers, json={"name": "regress-sandbox", "connection_label": "fake-sandbox"}).json()
        client.post(f"/api/v1/targets/{target['id']}/evaluation-mapping", headers=headers, json={"evaluation_target_id": sandbox_target["id"]})
    await TelemetryCollector(engine, registry, interval_seconds=60).collect_once()
    with TestClient(app) as client:
        namespace = next(item for item in client.get(f"/api/v1/targets/{target['id']}", headers=headers).json()["namespaces"] if item["name"] == "commerce.orders")
        client.post(f"/api/v1/targets/{target['id']}/namespaces/{namespace['id']}/allowlist", headers=headers, json={"allowlisted": True})
        run_id = client.post("/api/v1/runs", headers=headers, json={"target_id": target["id"]}).json()["run_id"]
    repositories = create_repositories(engine)
    dispatcher = JobDispatcher(OptimizationJobHandler(repositories, orchestrator_factory=ProductionOrchestratorFactory(engine, repositories, registry, FakeAIProvider())))
    worker = DurableJobWorker(JobRepository(engine), "regression-worker")
    await _drain(worker, dispatcher)
    with TestClient(app) as client:
        state = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
        assert state["run"]["status"] == "APPROVAL_PENDING", state["run"]
        # The API defaulted the primary metric so calibration could proceed.
        assert state["run"]["primary_metric_key"] == "p99_latency_ms"
        approval = client.get("/api/v1/approvals/inbox", headers=approver_headers).json()["records"][0]
        assert client.post(f"/api/v1/approvals/{approval['id']}/approve", headers=approver_headers, json={}).status_code == 200
    # After deployment the "indexed" path becomes catastrophically slower.
    monkeypatch.setattr(fake_mongo, "INDEXED_SECONDS", 0.25)
    await _drain(worker, dispatcher)
    with TestClient(app) as client:
        final = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert final["run"]["status"] == "ROLLED_BACK", final["run"]
    assert [entry["event_type"] for entry in final["evidence"]["ledger"]] == ["DEPLOYMENT_INTENT", "DEPLOYMENT_APPLIED", "ROLLBACK_APPLIED"]
    assert set(orders.indexes) == {"_id_", "human_legacy"}
    await engine.dispose()
