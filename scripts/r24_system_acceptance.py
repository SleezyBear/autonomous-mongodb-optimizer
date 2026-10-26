"""R24 no-mock lifecycle qualification against disposable real infrastructure.

The shell acceptance gate owns the PostgreSQL/MongoDB containers.  This driver
uses the authenticated HTTP API, real durable worker/orchestrator, real Ollama,
real Mongo telemetry/index mutations, frozen admission, monitoring, rollback,
and restart-safe PostgreSQL evidence.  It refuses non-qualification resources.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
from pymongo import AsyncMongoClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.adapters.mongodb import MongoDBAdapter
from app.actions.schemas import SetQuerySettingsIndexHintAction
from app.adapters.contracts import Namespace, QuerySettingsIndexHint
from app.admission.durable import DurableAdmissionService
from app.admission.models import AdmissionRequest, MetricEvaluationInput
from app.admission.policy import DEFAULT_POLICIES
from app.admission.statistics import deterministic_arm_order, evaluate_candidate_admission
from app.ai.provider import OllamaAIProvider
from app.approvals.durable import DurableApprovalService
from app.auth.security import PasswordService
from app.authority.durable import DurableAuthorityService
from app.candidates.durable import CandidateGenerationService
from app.db import models
from app.db.repositories import create_repositories
from app.diagnosis.durable import DiagnosisService
from app.evaluation.durable import DurableEvaluationService
from app.metrics.collector import MetricSnapshot
from app.monitoring.durable import DurableMonitoringService
from app.monitoring.post_deployment import MonitoringWindow
from app.production.durable import DurableDeploymentService
from app.production.query_settings_durable import (
    DurableQuerySettingsDeploymentService,
    DurableQuerySettingsError,
)
from app.ranking.durable import CandidateRankingService
from app.rollback.durable import DurableRollbackService
from app.runs.orchestrator import OptimizationRunOrchestrator
from app.telemetry.persistence import TelemetryPersistenceService
from app.telemetry.providers import (
    CurrentOpTelemetryProvider,
    DiagnosticLogTelemetryProvider,
    TelemetryObservation,
    TelemetryProvider,
    TelemetrySource,
)
from app.worker.durable import DurableJobWorker, JobRepository
from app.worker.service import JobDispatcher, OptimizationJobHandler
from app.workloads.durable import WorkloadSnapshotService
from app.workloads.snapshots import Share


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts" / "generated" / "r24-system.json"
PASSWORD = "R24-qualification-password!"
DOCUMENT_COUNT = int(os.environ.get("R24_DOCUMENT_COUNT", "20000"))
SMOKE_QUERY_REPETITIONS = int(os.environ.get("R24_SMOKE_QUERY_REPETITIONS", "7"))
AUTONOMOUS_QUERY_REPETITIONS = int(
    os.environ.get("R24_AUTONOMOUS_QUERY_REPETITIONS", "31")
)
WARMUP_QUERY_REPETITIONS = int(os.environ.get("R24_WARMUP_QUERY_REPETITIONS", "3"))


@dataclass(frozen=True)
class PathResult:
    name: str
    run_id: str
    status: str
    completion_reason: str | None
    detail: dict[str, Any]


class FilteredCurrentOpProvider(TelemetryProvider):
    """Filter a real currentOp provider to one owned namespace."""

    source = TelemetrySource.CURRENT_OP

    def __init__(self, provider: CurrentOpTelemetryProvider, collection: str) -> None:
        self._provider = provider
        self._collection = collection

    async def available(self) -> bool:
        return bool(await self._provider.available())

    async def collect(self) -> tuple[TelemetryObservation, ...]:
        return tuple(
            item for item in await self._provider.collect() if item.collection == self._collection
        )


class SystemQualification:
    def __init__(self) -> None:
        self.database_url = _required("DATABASE_URL")
        self.api_url = _required("R24_API_URL").rstrip("/")
        self.mongo_root_uri = _required("R24_MONGO_ROOT_URI")
        self.mongo_executor_uri = _required("R24_MONGO_EXECUTOR_URI")
        self.mongo_evaluation_uri = _required("R24_MONGO_EVALUATION_URI")
        self.ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        marker = _required("R24R27_PROJECT")
        if not marker.startswith("r24r27-") or "r24r27" not in self.database_url:
            raise RuntimeError("refusing to run outside owned R24-R27 infrastructure")
        self.engine: AsyncEngine = create_async_engine(self.database_url, pool_pre_ping=True)
        self.root: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(self.mongo_root_uri)
        self.executor: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(self.mongo_executor_uri)
        self.evaluation: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(self.mongo_evaluation_uri)
        self.ai_http = httpx.AsyncClient(base_url=self.ollama_url)
        self.ai = OllamaAIProvider(
            self.ai_http,
            os.environ.get("OLLAMA_CHAT_MODEL", "gemma4:e4b"),
            os.environ.get("OLLAMA_EMBEDDING_MODEL", "embeddinggemma"),
            timeout_seconds=600,
        )
        self.api = httpx.AsyncClient(base_url=self.api_url, timeout=30)
        self.operator_id: UUID | None = None
        self.approver_id: UUID | None = None
        self.operator_email = ""
        self.approver_email = ""
        self.operator_token = ""
        self.approver_token = ""
        self.results: list[PathResult] = []
        self.query_settings_result: dict[str, Any] = {}

    async def close(self) -> None:
        await self.api.aclose()
        await self.ai_http.aclose()
        await self.root.close()
        await self.executor.close()
        await self.evaluation.close()
        await self.engine.dispose()

    async def bootstrap_users(self) -> None:
        marker = uuid4().hex
        now_sql = "now()"
        self.operator_id, self.approver_id = uuid4(), uuid4()
        self.operator_email = f"r24-operator-{marker}@example.com"
        self.approver_email = f"r24-approver-{marker}@example.com"
        async with self.engine.begin() as connection:
            for user_id, role, email in (
                (self.operator_id, "OPERATOR", self.operator_email),
                (self.approver_id, "APPROVER", self.approver_email),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO users "
                        f"(id,created_at,updated_at,email,password_hash,role,status,failed_login_count) "
                        f"VALUES (:id,{now_sql},{now_sql},:email,:password,:role,'ACTIVE',0)"
                    ),
                    {
                        "id": user_id,
                        "email": email,
                        "password": PasswordService().hash_password(PASSWORD),
                        "role": role,
                    },
                )
        self.operator_token = await self._login(self.operator_email)
        self.approver_token = await self._login(self.approver_email)
        _write_playwright_env(
            {
                "R24_OPERATOR_EMAIL": self.operator_email,
                "R24_APPROVER_EMAIL": self.approver_email,
                "R24_PASSWORD": PASSWORD,
            }
        )

    async def _login(self, email: str) -> str:
        response = await self.api.post(
            "/auth/login", json={"email": email, "password": PASSWORD}
        )
        response.raise_for_status()
        return cast(str, response.json()["access_token"])

    async def prepare_target(
        self,
        name: str,
        *,
        deployment_mode: str,
        current_op: bool = False,
        allowlisted: bool = True,
    ) -> tuple[UUID, str]:
        collection = f"r24r27_{name}_{uuid4().hex[:10]}"
        if not collection.startswith("r24r27_"):
            raise RuntimeError("unsafe qualification collection")
        # Real local inference can outlive the production JWT TTL. Obtain a
        # fresh credential at each API boundary instead of weakening expiry.
        self.operator_token = await self._login(self.operator_email)
        headers = _bearer(self.operator_token)
        monitored = await self.api.post(
            "/api/v1/targets",
            headers=headers,
            json={
                "name": f"{name}-{uuid4().hex[:8]}",
                "connection_label": "r24-monitored",
                "deployment_mode": deployment_mode,
            },
        )
        monitored.raise_for_status()
        evaluation = await self.api.post(
            "/api/v1/targets",
            headers=headers,
            json={"name": f"{name}-eval-{uuid4().hex[:8]}", "connection_label": "r24-evaluation"},
        )
        evaluation.raise_for_status()
        target_id = UUID(monitored.json()["id"])
        evaluation_id = UUID(evaluation.json()["id"])
        now = datetime.now(timezone.utc)
        async with self.engine.begin() as connection:
            await connection.execute(
                models.EvaluationMapping.__table__.insert().values(
                    id=uuid4(),
                    created_at=now,
                    updated_at=now,
                    target_id=target_id,
                    evaluation_target_id=evaluation_id,
                    mapping_status="ACTIVE",
                )
            )
            await connection.execute(
                models.CapabilitySnapshot.__table__.insert().values(
                    id=uuid4(),
                    created_at=now,
                    updated_at=now,
                    target_id=target_id,
                    capabilities={"create_index": True, "query_settings": True, "topology": "replica_set"},
                    fingerprint=f"r24-capability-{target_id.hex}",
                )
            )
        await self._seed_collection(collection)
        await self._persist_real_telemetry(target_id, collection, current_op=current_op)
        if allowlisted:
            async with self.engine.begin() as connection:
                updated = await connection.execute(
                    models.Namespace.__table__.update()
                    .where(
                        models.Namespace.__table__.c.target_id == target_id,
                        models.Namespace.__table__.c.name == f"commerce.{collection}",
                    )
                    .values(allowlisted=True)
                )
                if updated.rowcount != 1:
                    raise RuntimeError("owned telemetry namespace was not persisted")
        return target_id, collection

    async def _seed_collection(self, collection: str) -> None:
        documents = [
            {"_id": index, "customer_id": index, "status": "open" if index % 2 else "closed"}
            for index in range(DOCUMENT_COUNT)
        ]
        for client in (self.root, self.evaluation):
            database = client.get_database("commerce")
            if collection in await database.list_collection_names():
                raise RuntimeError("owned unique collection unexpectedly exists")
            await database.get_collection(collection).insert_many(documents, ordered=True)

    async def _persist_real_telemetry(
        self, target_id: UUID, collection: str, *, current_op: bool
    ) -> None:
        database = self.root.get_database("commerce")
        target_collection = database.get_collection(collection)
        if current_op:
            pending = asyncio.create_task(
                target_collection.find_one(
                    {"customer_id": DOCUMENT_COUNT - 1, "$where": "sleep(2500) || true"}
                )
            )
            await asyncio.sleep(0.8)
            provider: TelemetryProvider = FilteredCurrentOpProvider(
                CurrentOpTelemetryProvider(self.root.get_database("admin")), collection
            )
            if not await provider.available():
                raise RuntimeError("real currentOp telemetry unavailable")
            await TelemetryPersistenceService(self.engine).collect_and_persist(target_id, provider)
            await pending
        else:
            await target_collection.find_one(
                {"customer_id": DOCUMENT_COUNT - 1, "$where": "sleep(2500) || true"}
            )
            log = await self.root.get_database("admin").command({"getLog": "global"})
            lines = tuple(
                line
                for line in log.get("log", [])
                if isinstance(line, str) and f'"ns":"commerce.{collection}"' in line
            )
            provider = DiagnosticLogTelemetryProvider(lines)
            if not await provider.available():
                raise RuntimeError("real diagnostic-log telemetry unavailable")
            await TelemetryPersistenceService(self.engine).collect_and_persist(target_id, provider)
        # A fresh engine/session must observe the completed telemetry window.
        await self.engine.dispose()
        self.engine = create_async_engine(self.database_url, pool_pre_ping=True)
        async with self.engine.connect() as connection:
            count = await connection.scalar(
                select(models.TelemetryWindow.__table__.c.id).where(
                    models.TelemetryWindow.__table__.c.target_id == target_id,
                    models.TelemetryWindow.__table__.c.status == "COMPLETED",
                )
            )
        if count is None:
            raise RuntimeError("telemetry did not survive repository recreation")

    async def create_run(self, target_id: UUID, mode: str) -> UUID:
        response = await self.api.post(
            "/api/v1/runs",
            headers=_bearer(self.operator_token),
            json={
                "target_id": str(target_id),
                "deployment_mode": mode,
                "primary_metric_key": "p99_latency_ms",
            },
        )
        response.raise_for_status()
        return UUID(response.json()["run_id"])

    async def run_path(
        self,
        name: str,
        *,
        mode: str = "APPROVAL_CONTROLLED",
        current_op: bool = False,
        allowlisted: bool = True,
        admission_safe: bool = True,
        approval: str | None = None,
        monitoring: str = "stable",
        deployment_crash_recovery: bool = False,
        rollback_crash_recovery: bool = False,
    ) -> PathResult:
        target_id, collection = await self.prepare_target(
            name,
            deployment_mode=mode,
            current_op=current_op,
            allowlisted=allowlisted,
        )
        run_id = await self.create_run(target_id, mode)
        await self._run_real_worker(
            run_id,
            collection,
            admission_safe=admission_safe,
            monitoring=monitoring,
            worker_identity=f"r24-worker-{name}-1",
        )
        state = await self._run_row(run_id)
        expected_before_approval = {
            "no_candidates": ("COMPLETED", "NO_CANDIDATES"),
            "no_admitted": ("COMPLETED", "NO_ADMITTED_CANDIDATE"),
            "autonomous_success": ("COMPLETED", "DEPLOYMENT_SUCCEEDED"),
            "autonomy_fallback": ("APPROVAL_PENDING", None),
        }
        if approval is not None:
            if _value(state["status"]) != "APPROVAL_PENDING":
                raise AssertionError(f"{name} did not reach approval: {state}")
            approval_id = await self._approval_id(run_id)
            self.approver_token = await self._login(self.approver_email)
            response = await self.api.post(
                f"/api/v1/approvals/{approval_id}/{approval}",
                headers=_bearer(self.approver_token),
                json={"reason": f"R24 {approval} qualification"},
            )
            response.raise_for_status()
            if approval == "approve":
                if deployment_crash_recovery:
                    await self._inject_deployment_process_failures(run_id)
                # Fresh engine, repositories, provider services and worker model a
                # real worker process restart before the continuation job.
                await self.engine.dispose()
                self.engine = create_async_engine(self.database_url, pool_pre_ping=True)
                await self._run_real_worker(
                    run_id,
                    collection,
                    admission_safe=admission_safe,
                    monitoring="pause" if rollback_crash_recovery else monitoring,
                    worker_identity=f"r24-worker-{name}-restart",
                )
                if rollback_crash_recovery:
                    await self._inject_rollback_process_failure(run_id)
                    await asyncio.sleep(1.1)
                    await self._run_real_worker(
                        run_id,
                        collection,
                        admission_safe=admission_safe,
                        monitoring=monitoring,
                        worker_identity=f"r24-worker-{name}-rollback-recovery",
                    )
            state = await self._run_row(run_id)
        elif name in expected_before_approval:
            expected_status, expected_reason = expected_before_approval[name]
            actual = (_value(state["status"]), _optional_value(state["completion_reason"]))
            if actual != (expected_status, expected_reason):
                raise AssertionError(
                    f"{name}: expected {(expected_status, expected_reason)}, got {actual}"
                )
        expected_after_approval = {
            "approval_rejected": ("COMPLETED", "APPROVAL_REJECTED"),
            "approval_success": ("COMPLETED", "DEPLOYMENT_SUCCEEDED"),
            "monitoring_regression": ("ROLLED_BACK", None),
            "rollback_drift": ("ROLLBACK_BLOCKED", None),
        }
        if approval is not None and name in expected_after_approval:
            expected_status, expected_reason = expected_after_approval[name]
            actual = (_value(state["status"]), _optional_value(state["completion_reason"]))
            if actual != (expected_status, expected_reason):
                raise AssertionError(
                    f"{name}: expected {(expected_status, expected_reason)}, got {actual}"
                )
        self.operator_token = await self._login(self.operator_email)
        detail_response = await self.api.get(
            f"/api/v1/runs/{run_id}", headers=_bearer(self.operator_token)
        )
        detail_response.raise_for_status()
        result = PathResult(
            name,
            str(run_id),
            _value(state["status"]),
            _optional_value(state["completion_reason"]),
            detail_response.json(),
        )
        self.results.append(result)
        print(
            f"R24 PATH {name}: {result.status}"
            + (f" ({result.completion_reason})" if result.completion_reason else ""),
            flush=True,
        )
        return result

    async def _inject_deployment_process_failures(self, run_id: UUID) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "backend")
        script = str(ROOT / "scripts" / "r24_crash_boundary_worker.py")
        for boundary, expected in (("before_mutation", 73), ("after_mutation", 74)):
            process = subprocess.run(
                [sys.executable, script, str(run_id), boundary],
                cwd=ROOT,
                env=environment,
                check=False,
            )
            if process.returncode != expected:
                raise RuntimeError(
                    f"crash boundary {boundary} returned {process.returncode}, expected {expected}"
                )
        async with self.engine.connect() as connection:
            artifact_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM deployment_artifacts "
                    "WHERE optimization_run_id=:run"
                ),
                {"run": run_id},
            )
            intent_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM production_ledger_entries "
                    "WHERE run_id=:run AND event_type='DEPLOYMENT_INTENT'"
                ),
                {"run": run_id},
            )
        if artifact_count != 1 or intent_count != 1:
            raise AssertionError("deployment crash recovery duplicated durable intent")

    async def _inject_rollback_process_failure(self, run_id: UUID) -> None:
        state = await self._run_row(run_id)
        if _value(state["status"]) != "MONITORING":
            raise AssertionError("rollback crash injection requires MONITORING state")
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "backend")
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "r24_crash_boundary_worker.py"),
                str(run_id),
                "rollback_after_mutation",
            ],
            cwd=ROOT,
            env=environment,
            check=False,
        )
        if process.returncode != 75:
            raise RuntimeError(
                f"rollback crash boundary returned {process.returncode}, expected 75"
            )
        async with self.engine.connect() as connection:
            status = await connection.scalar(
                text(
                    "SELECT status FROM rollback_records WHERE candidate_id=("
                    "SELECT candidate_id FROM deployment_artifacts "
                    "WHERE optimization_run_id=:run)"
                ),
                {"run": run_id},
            )
        if status != "INTENT_RECORDED":
            raise AssertionError("rollback intent was not durable before process exit")

    async def _run_real_worker(
        self,
        run_id: UUID,
        collection: str,
        *,
        admission_safe: bool,
        monitoring: str,
        worker_identity: str,
    ) -> None:
        adapter = MongoDBAdapter(self.executor, "commerce")
        evaluation = DurableEvaluationService(self.engine)

        async def evaluate(active_run_id: UUID) -> None:
            await self._evaluate_real(active_run_id, collection, evaluation)

        async def requests(active_run_id: UUID) -> dict[UUID, AdmissionRequest]:
            return await self._admission_requests(active_run_id, admission_safe=admission_safe)

        rollback = DurableRollbackService(self.engine, adapter)

        async def monitor(active_run_id: UUID):  # type: ignore[no-untyped-def]
            service = DurableMonitoringService(
                self.engine,
                lambda measured_run_id: self._monitoring_windows(
                    measured_run_id, collection, monitoring=monitoring
                ),
                rollback,
            )
            return await service.monitor_for_run(active_run_id)

        repositories = create_repositories(self.engine)
        orchestrator = OptimizationRunOrchestrator(
            self.engine,
            repositories,
            snapshot_service=WorkloadSnapshotService(self.engine),
            diagnosis_service=DiagnosisService(self.engine, self.ai),
            candidate_service=CandidateGenerationService(self.engine, adapter),
            ranking_service=CandidateRankingService(self.engine, self.ai),
            evaluation_service=evaluation,
            admission_service=DurableAdmissionService(self.engine),
            evaluation_executor=evaluate,
            admission_requests=requests,
            authority_service=DurableAuthorityService(self.engine),
            approval_service=DurableApprovalService(self.engine),
            deployment_service=DurableDeploymentService(self.engine, adapter),
            monitoring_executor=monitor,
        )
        dispatcher = JobDispatcher(OptimizationJobHandler(repositories, orchestrator))
        worker = DurableJobWorker(JobRepository(self.engine), worker_identity)
        for _attempt in range(8):
            worked = await worker.run_once(dispatcher.dispatch)
            if not worked:
                break
            state = await self._run_row(run_id)
            if _value(state["status"]) in {
                "COMPLETED",
                "APPROVAL_PENDING",
                "ROLLED_BACK",
                "ROLLBACK_BLOCKED",
                "FAILED",
            }:
                break
            if monitoring == "pause" and _value(state["status"]) == "MONITORING":
                break
            await asyncio.sleep(1.1)

    async def _evaluate_real(
        self, run_id: UUID, collection: str, service: DurableEvaluationService
    ) -> None:
        async with self.engine.connect() as connection:
            plan = (
                await connection.execute(
                    select(models.EvaluationPlan.__table__.c.profile).where(
                        models.EvaluationPlan.__table__.c.optimization_run_id == run_id
                    )
                )
            ).scalar_one()
            candidate_rows = (
                await connection.execute(
                    select(
                        models.Candidate.__table__.c.id,
                        models.CandidateAction.__table__.c.action_payload,
                    )
                    .join(
                        models.CandidateAction.__table__,
                        models.CandidateAction.__table__.c.candidate_id
                        == models.Candidate.__table__.c.id,
                    )
                    .where(models.Candidate.__table__.c.optimization_run_id == run_id)
                )
            ).mappings().all()
        database = self.evaluation.get_database("commerce")
        mongo_collection = database.get_collection(collection)
        hello = await self.evaluation.get_database("admin").command({"hello": 1})
        build = await self.evaluation.get_database("admin").command({"buildInfo": 1})
        environment = f"{build.get('version')}:{hello.get('setName')}:{collection}"
        profile = _benchmark_profile(str(plan))
        pair_count = 30 if profile.value == "AUTONOMOUS" else 10
        repetitions = (
            AUTONOMOUS_QUERY_REPETITIONS
            if profile.value == "AUTONOMOUS"
            else SMOKE_QUERY_REPETITIONS
        )
        for candidate_row in candidate_rows:
            payload = candidate_row["action_payload"]
            index_name = str(payload["index_name"])
            keys = [(str(field["field"]), int(field["direction"])) for field in payload["fields"]]
            for pair_number in range(1, pair_count + 1):
                await _drop_owned_index_if_present(mongo_collection, index_name)
                arm_order = deterministic_arm_order(str(run_id), pair_number)
                if arm_order == "AB":
                    await _warm_find(mongo_collection)
                    baseline, aa_second = await _measured_aa_find(
                        mongo_collection, repetitions
                    )
                    await mongo_collection.create_index(keys, name=index_name)
                    await _warm_find(mongo_collection)
                    candidate = await _measured_find(mongo_collection, repetitions)
                else:
                    await mongo_collection.create_index(keys, name=index_name)
                    await _warm_find(mongo_collection)
                    candidate = await _measured_find(mongo_collection, repetitions)
                    await _drop_owned_index_if_present(mongo_collection, index_name)
                    await _warm_find(mongo_collection)
                    aa_second, baseline = await _measured_aa_find(
                        mongo_collection, repetitions
                    )
                await service.record_pair(
                    candidate_row["id"],
                    pair_number,
                    environment,
                    {"p99_latency_ms": baseline, "aa_p99_latency_ms": baseline},
                    {"p99_latency_ms": candidate, "aa_p99_latency_ms": aa_second},
                    arm_order,
                )
            await _drop_owned_index_if_present(mongo_collection, index_name)
            await service.mark_complete(candidate_row["id"])

    async def _admission_requests(
        self, run_id: UUID, *, admission_safe: bool
    ) -> dict[UUID, AdmissionRequest]:
        async with self.engine.connect() as connection:
            plan = (
                await connection.execute(
                    select(models.EvaluationPlan.__table__).where(
                        models.EvaluationPlan.__table__.c.optimization_run_id == run_id
                    )
                )
            ).mappings().one()
            rows = (
                await connection.execute(
                    select(
                        models.EvaluationRun.__table__.c.id.label("evaluation_id"),
                        models.EvaluationRun.__table__.c.candidate_id,
                        models.TrialPair.__table__.c.pair_number,
                        models.TrialMetric.__table__.c.baseline_value,
                        models.TrialMetric.__table__.c.candidate_value,
                    )
                    .join(
                        models.TrialPair.__table__,
                        models.TrialPair.__table__.c.evaluation_run_id
                        == models.EvaluationRun.__table__.c.id,
                    )
                    .join(
                        models.TrialMetric.__table__,
                        models.TrialMetric.__table__.c.trial_pair_id
                        == models.TrialPair.__table__.c.id,
                    )
                    .where(
                        models.EvaluationRun.__table__.c.candidate_id.in_(
                            [UUID(value) for value in plan["selected_candidate_ids"]]
                        ),
                        models.TrialMetric.__table__.c.metric_name == "p99_latency_ms",
                    )
                    .order_by(
                        models.EvaluationRun.__table__.c.candidate_id,
                        models.TrialPair.__table__.c.pair_number,
                    )
                )
            ).mappings().all()
            aa_rows = (
                await connection.execute(
                    select(
                        models.EvaluationRun.__table__.c.candidate_id,
                        models.TrialMetric.__table__.c.baseline_value,
                        models.TrialMetric.__table__.c.candidate_value,
                    )
                    .join(
                        models.TrialPair.__table__,
                        models.TrialPair.__table__.c.evaluation_run_id
                        == models.EvaluationRun.__table__.c.id,
                    )
                    .join(
                        models.TrialMetric.__table__,
                        models.TrialMetric.__table__.c.trial_pair_id
                        == models.TrialPair.__table__.c.id,
                    )
                    .where(
                        models.EvaluationRun.__table__.c.candidate_id.in_(
                            [UUID(value) for value in plan["selected_candidate_ids"]]
                        ),
                        models.TrialMetric.__table__.c.metric_name == "aa_p99_latency_ms",
                    )
                    .order_by(
                        models.EvaluationRun.__table__.c.candidate_id,
                        models.TrialPair.__table__.c.pair_number,
                    )
                )
            ).mappings().all()
        grouped: dict[UUID, list[Any]] = {}
        for row in rows:
            grouped.setdefault(row["candidate_id"], []).append(row)
        aa_grouped: dict[UUID, list[float]] = {}
        for row in aa_rows:
            baseline = max(float(row["baseline_value"]), 0.001)
            candidate = max(float(row["candidate_value"]), 0.001)
            aa_grouped.setdefault(row["candidate_id"], []).append(math.log(candidate / baseline))
        requests: dict[UUID, AdmissionRequest] = {}
        for candidate_id in (UUID(value) for value in plan["selected_candidate_ids"]):
            measurements = grouped[candidate_id]
            metric = MetricEvaluationInput(
                "p99_latency_ms",
                "GLOBAL",
                "all",
                DEFAULT_POLICIES["p99_latency_ms"],
                tuple(float(row["baseline_value"]) for row in measurements),
                tuple(float(row["candidate_value"]) for row in measurements),
                aa_scores=tuple(aa_grouped[candidate_id]),
            )
            request = AdmissionRequest(
                str(candidate_id),
                str(measurements[0]["evaluation_id"]),
                profile=_benchmark_profile(str(plan["profile"])),
                primary_metric_key="p99_latency_ms",
                metrics=(metric,),
                safety_invariants_safe=admission_safe,
                safety_invariant_results=()
                if admission_safe
                else ("QUALIFICATION_INJECTED_SAFETY_FAILURE",),
            )
            requests[candidate_id] = request
            if request.profile.value == "AUTONOMOUS":
                diagnostic = evaluate_candidate_admission(request)
                print(
                    "R24 AUTONOMOUS ADMISSION: "
                    f"{diagnostic.status.value} "
                    f"pairs={diagnostic.actual_pair_count}/required={diagnostic.required_pair_count} "
                    f"reasons={','.join(diagnostic.reason_codes) or 'none'}",
                    flush=True,
                )
        return requests

    async def _monitoring_windows(
        self, run_id: UUID, collection: str, *, monitoring: str
    ) -> tuple[tuple[MonitoringWindow, ...], tuple[MonitoringWindow, ...]]:
        async with self.engine.connect() as connection:
            baseline_values = (
                await connection.execute(
                    select(models.TrialMetric.__table__.c.baseline_value)
                    .join(
                        models.TrialPair.__table__,
                        models.TrialPair.__table__.c.id
                        == models.TrialMetric.__table__.c.trial_pair_id,
                    )
                    .join(
                        models.EvaluationRun.__table__,
                        models.EvaluationRun.__table__.c.id
                        == models.TrialPair.__table__.c.evaluation_run_id,
                    )
                    .join(
                        models.Candidate.__table__,
                        models.Candidate.__table__.c.id
                        == models.EvaluationRun.__table__.c.candidate_id,
                    )
                    .where(
                        models.Candidate.__table__.c.optimization_run_id == run_id,
                        models.TrialMetric.__table__.c.metric_name == "p99_latency_ms",
                    )
                    .order_by(models.TrialPair.__table__.c.pair_number)
                    .limit(10)
                )
            ).scalars().all()
        if len(baseline_values) < 10:
            raise RuntimeError("monitoring requires ten persisted baseline windows")
        collection_handle = self.root.get_database("commerce").get_collection(collection)
        if monitoring == "drift_blocked":
            await collection_handle.create_index(
                [("human_external_field", 1)], name=f"human_r24_{uuid4().hex[:8]}"
            )
        if monitoring == "pause":
            raise TimeoutError("qualification pause before monitoring evidence")
        if monitoring in {"regression", "drift_blocked"}:
            started = time.perf_counter()
            await collection_handle.find_one(
                {"customer_id": DOCUMENT_COUNT - 1, "$where": "sleep(250) || true"}
            )
            observed_value = (time.perf_counter() - started) * 1000
        else:
            observed_value = await _measured_find(collection_handle)
        baseline = tuple(_window(float(value)) for value in baseline_values)
        observed = (_window(observed_value),)
        return baseline, observed

    async def _approval_id(self, run_id: UUID) -> UUID:
        async with self.engine.connect() as connection:
            value = await connection.scalar(
                select(models.ApprovalRequest.__table__.c.id).where(
                    models.ApprovalRequest.__table__.c.optimization_run_id == run_id
                )
            )
        if value is None:
            raise RuntimeError("run has no durable approval request")
        return UUID(str(value))

    async def _run_row(self, run_id: UUID) -> dict[str, Any]:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(models.OptimizationRun.__table__).where(
                        models.OptimizationRun.__table__.c.id == run_id
                    )
                )
            ).mappings().one()
        return dict(row)

    async def verify(self) -> None:
        by_name = {result.name: result for result in self.results}
        expected = {
            "no_candidates": ("COMPLETED", "NO_CANDIDATES"),
            "no_admitted": ("COMPLETED", "NO_ADMITTED_CANDIDATE"),
            "approval_rejected": ("COMPLETED", "APPROVAL_REJECTED"),
            "approval_success": ("COMPLETED", "DEPLOYMENT_SUCCEEDED"),
            "autonomous_success": ("COMPLETED", "DEPLOYMENT_SUCCEEDED"),
            "autonomy_fallback": ("APPROVAL_PENDING", None),
            "monitoring_regression": ("ROLLED_BACK", None),
            "rollback_drift": ("ROLLBACK_BLOCKED", None),
        }
        if set(by_name) != set(expected):
            raise AssertionError(f"missing lifecycle results: {set(expected) - set(by_name)}")
        for name, (status, reason) in expected.items():
            actual = by_name[name]
            if actual.status != status or actual.completion_reason != reason:
                raise AssertionError(f"{name}: expected {(status, reason)}, got {actual}")
        fallback = by_name["autonomy_fallback"].detail["artifacts"]["authority"]
        if fallback["authority_reason"] != "AUTONOMY_INELIGIBLE_REQUIRES_APPROVAL":
            raise AssertionError("currentOp autonomy fallback reason was not persisted")
        async with self.engine.connect() as connection:
            monitoring_count = await connection.scalar(
                select(models.AuditEvent.__table__.c.id)
                .where(models.AuditEvent.__table__.c.event_type == "DURABLE_POST_DEPLOYMENT_MONITORING")
                .limit(1)
            )
            experience_count = await connection.scalar(
                select(models.ExperienceRecord.__table__.c.id).limit(1)
            )
            crash_run = UUID(by_name["approval_success"].run_id)
            applied_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM production_ledger_entries "
                    "WHERE run_id=:run AND event_type='DEPLOYMENT_APPLIED'"
                ),
                {"run": crash_run},
            )
            deployment_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM deployment_artifacts "
                    "WHERE optimization_run_id=:run AND status='APPLIED'"
                ),
                {"run": crash_run},
            )
        if monitoring_count is None or experience_count is None:
            raise AssertionError("monitoring/experience evidence is not durable")
        if applied_count != 1 or deployment_count != 1:
            raise AssertionError("crash recovery duplicated deployment evidence")

    async def qualify_query_settings(self, successful: PathResult) -> None:
        """Qualify supported real query settings and exact owned rollback."""
        target_id = UUID(successful.detail["run"]["target_id"])
        run_id = UUID(successful.run_id)
        deployment = successful.detail["artifacts"]["deployment"]
        if deployment is None:
            raise AssertionError("successful lifecycle has no deployment artifact")
        async with self.engine.connect() as connection:
            action = (
                await connection.execute(
                    select(models.CandidateAction.__table__.c.action_payload)
                    .join(
                        models.Candidate.__table__,
                        models.Candidate.__table__.c.id
                        == models.CandidateAction.__table__.c.candidate_id,
                    )
                    .where(models.Candidate.__table__.c.optimization_run_id == run_id)
                )
            ).scalar_one()
        collection = str(action["collection"])
        index_name = str(action["index_name"])
        mongo_collection = self.root.get_database("commerce").get_collection(collection)
        await mongo_collection.find_one(
            {"customer_id": DOCUMENT_COUNT - 1, "$where": "sleep(180) || true"}
        )
        query_hash = await self._latest_query_shape_hash(collection)
        adapter = MongoDBAdapter(self.executor, "commerce")
        typed = SetQuerySettingsIndexHintAction(
            database="commerce",
            collection=collection,
            query_shape_hash=query_hash,
            allowed_indexes=(index_name,),
            previous_allowed_indexes=None,
        )
        service = DurableQuerySettingsDeploymentService(self.engine, adapter)
        first = await service.deploy(
            target_id=target_id,
            action=typed,
            evidence_hash=f"r24-query-settings-{run_id}",
            optimization_run_id=run_id,
        )
        if first["status"] != "APPLIED":
            raise AssertionError("query setting did not become durable APPLIED state")
        # Fresh service reconciles without a duplicate mutation.
        second = await DurableQuerySettingsDeploymentService(self.engine, adapter).deploy(
            target_id=target_id,
            action=typed,
            evidence_hash=f"r24-query-settings-{run_id}",
            optimization_run_id=run_id,
        )
        if second["id"] != first["id"]:
            raise AssertionError("query setting retry created duplicate ownership")
        rolled_back = await service.rollback(
            first["id"], evidence_hash=f"r24-query-settings-{run_id}"
        )
        if rolled_back["status"] != "ROLLED_BACK":
            raise AssertionError("query setting exact inverse was not durable")
        if await adapter.get_query_settings_index_hint(Namespace(collection), query_hash) is not None:
            raise AssertionError("query setting inverse did not restore absent before-state")

        # A separate externally-created setting is observed but never changed.
        await mongo_collection.find_one(
            {"status": "open", "$where": "sleep(180) || true"}
        )
        human_hash = await self._latest_query_shape_hash(collection, exclude=query_hash)
        human_hint = QuerySettingsIndexHint(("_id_",))
        root_adapter = MongoDBAdapter(self.root, "commerce")
        await root_adapter.set_query_settings_index_hint(
            Namespace(collection), human_hash, human_hint
        )
        human_action = SetQuerySettingsIndexHintAction(
            database="commerce",
            collection=collection,
            query_shape_hash=human_hash,
            allowed_indexes=(index_name,),
            previous_allowed_indexes=("_id_",),
        )
        try:
            await service.deploy(
                target_id=target_id,
                action=human_action,
                evidence_hash="r24-human-setting-refusal",
                optimization_run_id=run_id,
            )
        except DurableQuerySettingsError as error:
            if str(error) != "HUMAN_QUERY_SETTINGS_PRESENT":
                raise
        else:
            raise AssertionError("human-created query setting was modified")
        if await adapter.get_query_settings_index_hint(Namespace(collection), human_hash) != human_hint:
            raise AssertionError("human-created query setting changed during refusal")
        # The qualification harness owns this unique collection and restores its
        # own simulated external fixture after proving the refusal.
        await root_adapter.set_query_settings_index_hint(Namespace(collection), human_hash, None)
        self.query_settings_result = {
            "supported": True,
            "deployment_id": str(first["id"]),
            "exact_inverse": True,
            "human_setting_untouched": True,
        }

    async def _latest_query_shape_hash(
        self, collection: str, *, exclude: str | None = None
    ) -> str:
        log = await self.root.get_database("admin").command({"getLog": "global"})
        for line in reversed(log.get("log", [])):
            if not isinstance(line, str) or f'"ns":"commerce.{collection}"' not in line:
                continue
            document = json.loads(line)
            value = document.get("attr", {}).get("queryShapeHash")
            if isinstance(value, str) and value and value != exclude:
                return value
        raise RuntimeError("real Mongo diagnostic log has no queryShapeHash")

    def persist_result(self) -> None:
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": "SYSTEM_PASS",
            "real_ollama": True,
            "real_api": True,
            "real_worker": True,
            "real_mongodb": True,
            "real_postgresql": True,
            "query_settings": self.query_settings_result,
            "paths": [
                {
                    "name": result.name,
                    "run_id": result.run_id,
                    "status": result.status,
                    "completion_reason": result.completion_reason,
                }
                for result in self.results
            ],
        }
        ARTIFACT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"R24 SYSTEM_PASS: {len(self.results)} real lifecycle paths")


async def _main() -> None:
    qualification = SystemQualification()
    try:
        await qualification.bootstrap_users()
        await qualification.run_path("no_candidates", allowlisted=False)
        await qualification.run_path("no_admitted", admission_safe=False)
        await qualification.run_path("approval_rejected", approval="reject")
        success = await qualification.run_path(
            "approval_success", approval="approve", deployment_crash_recovery=True
        )
        await qualification.qualify_query_settings(success)
        await qualification.run_path("autonomous_success", mode="FULL_AUTONOMOUS")
        await qualification.run_path(
            "monitoring_regression",
            approval="approve",
            monitoring="regression",
            rollback_crash_recovery=True,
        )
        await qualification.run_path(
            "rollback_drift", approval="approve", monitoring="drift_blocked"
        )
        # Last, so its 15-minute approval window is still open when the
        # Playwright stage approves it through the console.
        await qualification.run_path(
            "autonomy_fallback", mode="FULL_AUTONOMOUS", current_op=True
        )
        await qualification.verify()
        _write_playwright_env(
            {
                "R24_TARGET_ID": success.detail["run"]["target_id"],
                "R24_SUCCESS_RUN_ID": success.run_id,
                "R24_NOOP_RUN_ID": next(
                    result.run_id for result in qualification.results if result.name == "no_candidates"
                ),
                "R24_ROLLBACK_RUN_ID": next(
                    result.run_id
                    for result in qualification.results
                    if result.name == "monitoring_regression"
                ),
                "R24_PENDING_RUN_ID": next(
                    result.run_id
                    for result in qualification.results
                    if result.name == "autonomy_fallback"
                ),
            }
        )
        qualification.persist_result()
    finally:
        await qualification.close()


def _window(p99: float) -> MonitoringWindow:
    metrics = MetricSnapshot(
        p99 / 3,
        p99 * 0.8,
        p99,
        1000 / max(p99, 0.001),
        50,
        10,
        1,
        1,
        1,
        0,
        0,
        0,
        1,
        1,
        1,
        0,
        0,
        0,
    )
    return MonitoringWindow(metrics, (Share("owned-query-shape", 1.0),), True)


async def _warm_find(collection: Any) -> None:
    for _ in range(WARMUP_QUERY_REPETITIONS):
        if await collection.find_one({"customer_id": DOCUMENT_COUNT - 1}) is None:
            raise RuntimeError("real evaluation warmup query returned no document")


async def _measured_aa_find(collection: Any, repetitions: int) -> tuple[float, float]:
    """Interleave two independent real A/A arms to control temporal drift."""
    first: list[float] = []
    second: list[float] = []
    for _ in range(repetitions):
        for values in (first, second):
            started = time.perf_counter()
            document = await collection.find_one({"customer_id": DOCUMENT_COUNT - 1})
            values.append((time.perf_counter() - started) * 1000)
            if document is None:
                raise RuntimeError("real A/A evaluation query returned no document")
    return max(0.001, statistics.median(first)), max(
        0.001, statistics.median(second)
    )


async def _measured_find(collection: Any, repetitions: int = SMOKE_QUERY_REPETITIONS) -> float:
    timings: list[float] = []
    for _ in range(repetitions):
        started = time.perf_counter()
        document = await collection.find_one({"customer_id": DOCUMENT_COUNT - 1})
        timings.append((time.perf_counter() - started) * 1000)
        if document is None:
            raise RuntimeError("real evaluation query returned no document")
    return max(0.001, statistics.median(timings))


async def _drop_owned_index_if_present(collection: Any, index_name: str) -> None:
    indexes = await collection.index_information()
    if index_name in indexes:
        if not index_name.startswith("optimizer_"):
            raise RuntimeError("refusing to remove a non-optimizer index")
        await collection.drop_index(index_name)


def _benchmark_profile(value: str):  # type: ignore[no-untyped-def]
    from app.admission.models import BenchmarkProfile

    return BenchmarkProfile(value)


def _value(value: object) -> str:
    return str(value.value if hasattr(value, "value") else value)


def _optional_value(value: object | None) -> str | None:
    return None if value is None else _value(value)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _write_playwright_env(values: dict[str, str]) -> None:
    path = ROOT / "artifacts" / "generated" / "r24-playwright.env"
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator:
                existing[key] = value
    existing.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{key}={value}\n" for key, value in sorted(existing.items())))


if __name__ == "__main__":
    asyncio.run(_main())
