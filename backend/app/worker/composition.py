"""Production composition root for the durable optimization worker.

Before this module the shipped worker constructed an orchestrator with no
stage services, so every run created from the UI stopped at ``DIAGNOSING``.
Here every stage is wired to its real durable service:

=====================  ==================================================
stage                  service
=====================  ==================================================
snapshot               :class:`WorkloadSnapshotService` (PostgreSQL only)
diagnosis / ranking    configured AI provider (Ollama, OpenAI-compatible,
                       or the deterministic no-LLM provider)
candidates             :class:`CandidateGenerationService` + executor adapter
evaluation             :class:`SandboxEvaluationExecutor` (observer → sandbox)
admission              :class:`DurableAdmissionService` + request builder
authority / approval   :class:`DurableAuthorityService`, approvals
deployment             :class:`DurableDeploymentService` (executor adapter)
monitoring / rollback  :class:`DurableMonitoringService` with live probes
=====================  ==================================================

Target-specific adapters are resolved per run from the worker-only
:class:`TargetConnectionRegistry`; no credential ever reaches the API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.mongodb import MongoDBAdapter
from app.admission.durable import DurableAdmissionService
from app.admission.requests import AdmissionRequestBuilder
from app.ai.provider import AIProvider, FakeAIProvider, OllamaAIProvider, OpenAICompatibleAIProvider
from app.approvals.durable import DurableApprovalService
from app.authority.durable import DurableAuthorityService
from app.candidates.durable import CandidateGenerationService
from app.db import models
from app.db.repositories import ControlPlaneRepositories
from app.diagnosis.durable import DiagnosisService
from app.evaluation.durable import DurableEvaluationService
from app.evaluation.executor import SandboxEvaluationExecutor
from app.monitoring.collector import ProductionMonitoringCollector
from app.monitoring.durable import DurableMonitoringService
from app.monitoring.post_deployment import MonitoringResult
from app.mongodb.connections import ConnectionConfigurationError, TargetConnectionRegistry
from app.production.durable import DurableDeploymentService
from app.ranking.durable import CandidateRankingService
from app.rollback.durable import DurableRollbackService
from app.runs.orchestrator import OptimizationRunOrchestrator
from app.workloads.durable import WorkloadSnapshotService


@dataclass(frozen=True)
class AISettings:
    provider: str
    base_url: str
    chat_model: str
    embedding_model: str
    timeout_seconds: float

    @classmethod
    def from_environment(cls) -> AISettings:
        provider = os.getenv("AI_PROVIDER", os.getenv("DEFAULT_AI_PROVIDER", "ollama")).strip().lower()
        if provider not in {"ollama", "openai_compatible", "deterministic"}:
            raise RuntimeError("AI_PROVIDER must be ollama, openai_compatible, or deterministic.")
        return cls(
            provider=provider,
            base_url=os.getenv("OLLAMA_BASE_URL", os.getenv("AI_BASE_URL", "http://127.0.0.1:11434")),
            chat_model=os.getenv("OLLAMA_CHAT_MODEL", "gemma4:e4b"),
            embedding_model=os.getenv("OLLAMA_EMBEDDING_MODEL", "embeddinggemma"),
            timeout_seconds=float(os.getenv("AI_TIMEOUT_SECONDS", "600")),
        )

    def public(self) -> dict[str, object]:
        return {"provider": self.provider, "chat_model": self.chat_model if self.provider != "deterministic" else None, "embedding_model": self.embedding_model if self.provider != "deterministic" else None}


def build_ai_provider(settings: AISettings) -> tuple[AIProvider, httpx.AsyncClient | None]:
    """Return the advisory provider; the deterministic one needs no LLM."""
    if settings.provider == "deterministic":
        return FakeAIProvider(), None
    client = httpx.AsyncClient(base_url=settings.base_url)
    if settings.provider == "openai_compatible":
        return OpenAICompatibleAIProvider(client, settings.chat_model, settings.embedding_model, timeout_seconds=settings.timeout_seconds), client
    return OllamaAIProvider(client, settings.chat_model, settings.embedding_model, timeout_seconds=settings.timeout_seconds), client


class TargetConnectionMissing(ConnectionConfigurationError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.error_code = code


class ProductionOrchestratorFactory:
    """Compose a fully wired orchestrator for the target of one run."""

    def __init__(self, engine: AsyncEngine, repositories: ControlPlaneRepositories, connections: TargetConnectionRegistry, ai_provider: AIProvider) -> None:
        self._engine = engine
        self._repositories = repositories
        self._connections = connections
        self._ai = ai_provider

    async def __call__(self, run_id: UUID) -> OptimizationRunOrchestrator:
        async with self._engine.connect() as connection:
            label = (await connection.execute(
                select(models.Target.__table__.c.connection_label)
                .join(models.OptimizationRun.__table__, models.OptimizationRun.__table__.c.target_id == models.Target.__table__.c.id)
                .where(models.OptimizationRun.__table__.c.id == run_id)
            )).scalar_one_or_none()
        if label is None:
            raise TargetConnectionMissing("RUN_TARGET_NOT_FOUND")
        if not self._connections.has_role(str(label), "executor"):
            raise TargetConnectionMissing("TARGET_EXECUTOR_CONNECTION_NOT_CONFIGURED")
        profile = self._connections.profile(str(label))
        adapter = MongoDBAdapter(self._connections.client(str(label), "executor"), profile.database)
        evaluation = DurableEvaluationService(self._engine)
        rollback = DurableRollbackService(self._engine, adapter)
        collector = ProductionMonitoringCollector(self._engine, self._connections)

        async def monitor(active_run_id: UUID) -> MonitoringResult:
            return await DurableMonitoringService(self._engine, collector, rollback).monitor_for_run(active_run_id)

        return OptimizationRunOrchestrator(
            self._engine,
            self._repositories,
            snapshot_service=WorkloadSnapshotService(self._engine),
            diagnosis_service=DiagnosisService(self._engine, self._ai),
            candidate_service=CandidateGenerationService(self._engine, adapter),
            ranking_service=CandidateRankingService(self._engine, self._ai),
            evaluation_service=evaluation,
            admission_service=DurableAdmissionService(self._engine),
            evaluation_executor=SandboxEvaluationExecutor(self._engine, self._connections),
            admission_requests=AdmissionRequestBuilder(self._engine),
            authority_service=DurableAuthorityService(self._engine),
            approval_service=DurableApprovalService(self._engine),
            deployment_service=DurableDeploymentService(self._engine, adapter),
            monitoring_executor=monitor,
        )
