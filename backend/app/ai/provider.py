"""Structured Ollama and fake AI providers with no database execution authority."""

from __future__ import annotations

import hashlib
import json
import os
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Literal, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from app.isolation.lock import BenchmarkOllamaIsolationLock, shared_measurement_lock
from app.security.privacy import PrivacyBoundary, PrivacyMode


DEFAULT_AI_PROVIDER = "ollama"
DEFAULT_OLLAMA_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "600"))


class StructuredOutput(BaseModel):
    """Strict output base: providers may return analysis, never executable commands."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderFailureCode(str, Enum):
    UNAVAILABLE = "AI_PROVIDER_UNAVAILABLE"
    TIMEOUT = "AI_PROVIDER_TIMEOUT"
    HTTP_ERROR = "AI_PROVIDER_HTTP_ERROR"
    STRUCTURED_OUTPUT_UNSUPPORTED = "AI_STRUCTURED_OUTPUT_UNSUPPORTED"
    RESPONSE_SCHEMA_INVALID = "AI_RESPONSE_SCHEMA_INVALID"


class ProviderError(RuntimeError):
    def __init__(self, code: ProviderFailureCode, *, retryable: bool, cause: Exception | None = None) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code.value)
        if cause is not None:
            self.__cause__ = cause


class Diagnosis(StructuredOutput):
    summary: str = Field(min_length=1)
    evidence: tuple[str, ...]
    bottlenecks: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    candidate_family_priorities: tuple[str, ...] = ()
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    limitations: tuple[str, ...] = ()


class DiagnosisFindingType(str, Enum):
    """Closed, non-executable vocabulary for grounded workload interpretation."""

    QUERY_LATENCY_HOTSPOT = "QUERY_LATENCY_HOTSPOT"
    EXCESSIVE_DOCUMENT_SCAN = "EXCESSIVE_DOCUMENT_SCAN"
    EXCESSIVE_KEY_SCAN = "EXCESSIVE_KEY_SCAN"
    FILTER_INDEX_MISMATCH = "FILTER_INDEX_MISMATCH"
    SORT_INDEX_MISMATCH = "SORT_INDEX_MISMATCH"
    WORKLOAD_SKEW = "WORKLOAD_SKEW"
    READ_PRESSURE = "READ_PRESSURE"
    WRITE_PRESSURE = "WRITE_PRESSURE"
    RESOURCE_PRESSURE = "RESOURCE_PRESSURE"
    REPLICATION_PRESSURE = "REPLICATION_PRESSURE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class DiagnosisFinding(StructuredOutput):
    finding_id: str = Field(min_length=1, max_length=128)
    finding_type: DiagnosisFindingType
    summary: str = Field(min_length=1, max_length=2048)
    rationale: str = Field(min_length=1, max_length=4096)
    query_shape_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = Field(min_length=1)


class DiagnosisArtifactResult(StructuredOutput):
    """Versioned output accepted by the durable diagnosis boundary."""

    findings: tuple[DiagnosisFinding, ...] = ()


class CandidateRanking(StructuredOutput):
    """Legacy advisory ranking for non-durable pipeline callers."""
    candidate_ids: tuple[str, ...]
    rationale: str = Field(min_length=1)
    reasons_by_candidate: dict[str, str] = Field(default_factory=dict)
    risk_notes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


class RankingCandidateInput(StructuredOutput):
    """Closed-set, non-authoritative transport input for a durable ranking."""

    handle: str = Field(pattern=r"^C[1-9][0-9]*$")
    safe_summary: str = Field(min_length=1)


class CandidateHandleRanking(StructuredOutput):
    """Model output that can rank handles but cannot author candidate identity."""

    candidate_handles: tuple[str, ...]
    rationale: str = Field(min_length=1)
    reasons_by_handle: dict[str, str] = Field(default_factory=dict)
    risk_notes: tuple[str, ...] = ()


class AIInvocationRecord(StructuredOutput):
    prompt_version: str
    model: str
    input_hash: str
    sanitized_input: str
    structured_result: dict[str, object]
    latency_ms: float


class DecisionExplanation(StructuredOutput):
    explanation: str = Field(min_length=1)


class ExperienceEmbedding(StructuredOutput):
    vector: tuple[float, ...] = Field(min_length=1)


class AIProvider(ABC):
    """Typed advisory interface; it exposes no database credentials or execution method."""

    @abstractmethod
    async def diagnose(self, evidence: str) -> Diagnosis:
        """Return structured diagnostic analysis."""

    @abstractmethod
    async def diagnose_artifact(self, evidence: str, *, allowed_query_shape_ids: tuple[str, ...] = (), allowed_evidence_refs: tuple[str, ...] = ()) -> DiagnosisArtifactResult:
        """Interpret supplied immutable evidence without producing executable actions."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Stable audit label; never a credential or connection URI."""

    @property
    @abstractmethod
    def chat_model(self) -> str:
        """Configured advisory model recorded with durable invocation evidence."""

    @abstractmethod
    async def rank_candidates(self, candidate_summaries: tuple[str, ...]) -> CandidateRanking:
        """Return a structured ranking of already-generated candidate IDs."""

    @abstractmethod
    async def rank_candidate_handles(self, candidates: tuple[RankingCandidateInput, ...]) -> CandidateHandleRanking:
        """Rank a closed, handle-only set; PostgreSQL remains identity authority."""

    @abstractmethod
    async def explain_decision(self, decision: str) -> DecisionExplanation:
        """Return a structured explanation of a deterministic decision."""

    @abstractmethod
    async def embed_experience(self, experience: str) -> ExperienceEmbedding:
        """Return a numeric embedding for experience retrieval only."""


class OllamaAIProvider(AIProvider):
    """Ollama HTTP provider that requests strict JSON at temperature zero."""

    def __init__(self, client: httpx.AsyncClient, model: str, embedding_model: str, timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS, isolation_lock: BenchmarkOllamaIsolationLock = shared_measurement_lock, privacy_boundary: PrivacyBoundary | None = None) -> None:
        self._client = client
        self._model = model
        self._embedding_model = embedding_model
        self._timeout_seconds = timeout_seconds
        self._isolation_lock = isolation_lock
        self._privacy_boundary = privacy_boundary or PrivacyBoundary(PrivacyMode.LOCAL_NORMALIZED)
        self.invocations: list[AIInvocationRecord] = []

    async def diagnose(self, evidence: str) -> Diagnosis:
        return await self._structured(_prompt("diagnosis", evidence), Diagnosis)

    async def diagnose_artifact(self, evidence: str, *, allowed_query_shape_ids: tuple[str, ...] = (), allowed_evidence_refs: tuple[str, ...] = ()) -> DiagnosisArtifactResult:
        # R19F passes only a typed, literal-free immutable snapshot view.  Its
        # durable IDs are evidence references and must not be rewritten by the
        # free-text sanitizer (which intentionally redacts UUID-shaped values).
        result = await self._structured(_prompt("diagnosis", evidence), _grounded_diagnosis_schema(allowed_query_shape_ids, allowed_evidence_refs), pre_sanitized=True)
        return cast(DiagnosisArtifactResult, DiagnosisArtifactResult.model_validate(result.model_dump(mode="json")))

    @property
    def provider_name(self) -> str:
        return DEFAULT_AI_PROVIDER

    @property
    def chat_model(self) -> str:
        return self._model

    async def rank_candidates(self, candidate_summaries: tuple[str, ...]) -> CandidateRanking:
        return await self._structured(_prompt("ranking", "\n".join(candidate_summaries)), CandidateRanking)

    async def rank_candidate_handles(self, candidates: tuple[RankingCandidateInput, ...]) -> CandidateHandleRanking:
        handles = tuple(candidate.handle for candidate in candidates)
        result = await self._structured(
            _prompt("ranking", "\n".join(f"{candidate.handle}: {candidate.safe_summary}" for candidate in candidates)),
            _grounded_ranking_schema(handles),
        )
        return cast(CandidateHandleRanking, CandidateHandleRanking.model_validate(result.model_dump(mode="json")))

    async def explain_decision(self, decision: str) -> DecisionExplanation:
        return await self._structured("Explain this deterministic decision:\n" + decision, DecisionExplanation)

    async def embed_experience(self, experience: str) -> ExperienceEmbedding:
        async with self._isolation_lock.ollama_request():
            response = await self._client.post("/api/embed", json={"model": self._embedding_model, "input": self._privacy_boundary.sanitize_prompt_text(experience)}, timeout=self._timeout_seconds)
            response.raise_for_status()
            embeddings = response.json().get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != 1:
            raise ValueError("Ollama embedding response is malformed.")
        return ExperienceEmbedding(vector=tuple(embeddings[0]))

    async def _structured(self, prompt: str, output_type: type[StructuredOutput], *, pre_sanitized: bool = False) -> StructuredOutput:
        prompt = prompt if pre_sanitized else self._privacy_boundary.sanitize_prompt_text(prompt)
        started = time.perf_counter()
        for attempt in range(2):
            try:
                async with self._isolation_lock.ollama_request():
                    response = await self._client.post("/api/chat", json={"model": self._model, "messages": [{"role": "user", "content": prompt}], "stream": False, "think": False, "format": output_type.model_json_schema(), "options": {"temperature": 0}}, timeout=self._timeout_seconds)
                    response.raise_for_status()
                    message = response.json().get("message")
                    raw_output = message.get("content") if isinstance(message, dict) else None
            except httpx.TimeoutException as error:
                raise ProviderError(ProviderFailureCode.TIMEOUT, retryable=True, cause=error) from error
            except httpx.ConnectError as error:
                raise ProviderError(ProviderFailureCode.UNAVAILABLE, retryable=True, cause=error) from error
            except httpx.HTTPStatusError as error:
                raise ProviderError(ProviderFailureCode.HTTP_ERROR, retryable=error.response.status_code >= 500, cause=error) from error
            if isinstance(raw_output, str):
                try:
                    result = cast(StructuredOutput, output_type.model_validate(json.loads(raw_output)))
                    self.invocations.append(
                        AIInvocationRecord(
                            prompt_version="v1",
                            model=self._model,
                            input_hash=_input_hash(prompt),
                            sanitized_input=prompt,
                            structured_result=result.model_dump(mode="json"),
                            latency_ms=(time.perf_counter() - started) * 1_000,
                        )
                    )
                    return result
                except (ValidationError, json.JSONDecodeError) as error:
                    if attempt == 1:
                        raise ProviderError(ProviderFailureCode.RESPONSE_SCHEMA_INVALID, retryable=False, cause=error) from error
            if attempt == 1:
                break
        raise ProviderError(ProviderFailureCode.STRUCTURED_OUTPUT_UNSUPPORTED, retryable=False)


class OpenAICompatibleAIProvider(AIProvider):
    """OpenAI-compatible local HTTP provider with the same advisory-only contract as Ollama."""

    def __init__(self, client: httpx.AsyncClient, model: str, embedding_model: str, timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS, isolation_lock: BenchmarkOllamaIsolationLock = shared_measurement_lock, privacy_boundary: PrivacyBoundary | None = None) -> None:
        self._client = client
        self._model = model
        self._embedding_model = embedding_model
        self._timeout_seconds = timeout_seconds
        self._isolation_lock = isolation_lock
        self._privacy_boundary = privacy_boundary or PrivacyBoundary(PrivacyMode.LOCAL_NORMALIZED)
        self.invocations: list[AIInvocationRecord] = []

    async def diagnose(self, evidence: str) -> Diagnosis:
        return await self._structured(_prompt("diagnosis", evidence), Diagnosis)

    async def diagnose_artifact(self, evidence: str, *, allowed_query_shape_ids: tuple[str, ...] = (), allowed_evidence_refs: tuple[str, ...] = ()) -> DiagnosisArtifactResult:
        result = await self._structured(_prompt("diagnosis", evidence), _grounded_diagnosis_schema(allowed_query_shape_ids, allowed_evidence_refs), pre_sanitized=True)
        return cast(DiagnosisArtifactResult, DiagnosisArtifactResult.model_validate(result.model_dump(mode="json")))

    @property
    def provider_name(self) -> str:
        return "openai_compatible"

    @property
    def chat_model(self) -> str:
        return self._model

    async def rank_candidates(self, candidate_summaries: tuple[str, ...]) -> CandidateRanking:
        return await self._structured(_prompt("ranking", "\n".join(candidate_summaries)), CandidateRanking)

    async def rank_candidate_handles(self, candidates: tuple[RankingCandidateInput, ...]) -> CandidateHandleRanking:
        handles = tuple(candidate.handle for candidate in candidates)
        result = await self._structured(
            _prompt("ranking", "\n".join(f"{candidate.handle}: {candidate.safe_summary}" for candidate in candidates)),
            _grounded_ranking_schema(handles),
        )
        return cast(CandidateHandleRanking, CandidateHandleRanking.model_validate(result.model_dump(mode="json")))

    async def explain_decision(self, decision: str) -> DecisionExplanation:
        return await self._structured("Explain this deterministic decision:\n" + decision, DecisionExplanation)

    async def embed_experience(self, experience: str) -> ExperienceEmbedding:
        payload = {"model": self._embedding_model, "input": self._privacy_boundary.sanitize_prompt_text(experience)}
        async with self._isolation_lock.ollama_request():
            response = await self._client.post("/v1/embeddings", json=payload, timeout=self._timeout_seconds)
            response.raise_for_status()
            data = response.json().get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict) or not isinstance(data[0].get("embedding"), list):
            raise ValueError("OpenAI-compatible embedding response is malformed.")
        return ExperienceEmbedding(vector=tuple(data[0]["embedding"]))

    async def _structured(self, prompt: str, output_type: type[StructuredOutput], *, pre_sanitized: bool = False) -> StructuredOutput:
        prompt = prompt if pre_sanitized else self._privacy_boundary.sanitize_prompt_text(prompt)
        started = time.perf_counter()
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_schema", "json_schema": {"name": output_type.__name__, "schema": output_type.model_json_schema()}},
            "temperature": 0,
        }
        for attempt in range(2):
            async with self._isolation_lock.ollama_request():
                response = await self._client.post("/v1/chat/completions", json=payload, timeout=self._timeout_seconds)
                response.raise_for_status()
                choices = response.json().get("choices")
            if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    try:
                        result = cast(StructuredOutput, output_type.model_validate(json.loads(message["content"])))
                        self.invocations.append(
                            AIInvocationRecord(
                                prompt_version="v1",
                                model=self._model,
                                input_hash=_input_hash(prompt),
                                sanitized_input=prompt,
                                structured_result=result.model_dump(mode="json"),
                                latency_ms=(time.perf_counter() - started) * 1_000,
                            )
                        )
                        return result
                    except (ValidationError, json.JSONDecodeError):
                        pass
            if attempt == 1:
                break
        raise ValueError("OpenAI-compatible provider returned malformed structured output twice.")


class FakeAIProvider(AIProvider):
    """Deterministic structured provider for unit tests and offline development."""

    async def diagnose(self, evidence: str) -> Diagnosis:
        return Diagnosis(summary="fake diagnosis", evidence=(evidence,), bottlenecks=("synthetic",), evidence_refs=(evidence,), candidate_family_priorities=("index",), confidence=1.0)

    async def diagnose_artifact(self, evidence: str, *, allowed_query_shape_ids: tuple[str, ...] = (), allowed_evidence_refs: tuple[str, ...] = ()) -> DiagnosisArtifactResult:
        return DiagnosisArtifactResult()

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def chat_model(self) -> str:
        return "fake"

    async def rank_candidates(self, candidate_summaries: tuple[str, ...]) -> CandidateRanking:
        selected = tuple(sorted(candidate_summaries))
        return CandidateRanking(candidate_ids=selected, rationale="deterministic fake ranking", reasons_by_candidate={candidate: "deterministic" for candidate in selected})

    async def rank_candidate_handles(self, candidates: tuple[RankingCandidateInput, ...]) -> CandidateHandleRanking:
        handles = tuple(candidate.handle for candidate in candidates)
        return CandidateHandleRanking(candidate_handles=handles, rationale="deterministic fake ranking", reasons_by_handle={handle: "deterministic" for handle in handles})

    async def explain_decision(self, decision: str) -> DecisionExplanation:
        return DecisionExplanation(explanation="fake explanation: " + decision)

    async def embed_experience(self, experience: str) -> ExperienceEmbedding:
        return ExperienceEmbedding(vector=(float(len(experience)),))


def _prompt(kind: str, evidence: str) -> str:
    """Load the versioned prompt contract while retaining one explicit evidence boundary."""
    from pathlib import Path

    template = (Path(__file__).with_name("prompts") / "v1" / f"{kind}.txt").read_text(encoding="utf-8")
    return template + "\n\nEvidence:\n" + evidence


def _input_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _grounded_diagnosis_schema(query_shape_ids: tuple[str, ...], evidence_refs: tuple[str, ...]) -> type[StructuredOutput]:
    """Constrain model references to the immutable snapshot's actual IDs."""
    query_type: Any = Literal.__getitem__(query_shape_ids) if query_shape_ids else str
    evidence_type: Any = Literal.__getitem__(evidence_refs) if evidence_refs else str
    finding = create_model(
        "GroundedDiagnosisFinding",
        __base__=StructuredOutput,
        # Finding identifiers are non-authoritative labels, but they must be
        # stable, non-placeholder, and unique within a model response.  The
        # service enforces uniqueness after this schema-level format check.
        finding_id=(str, Field(pattern=r"^F-[1-9][0-9]*$", max_length=128)),
        finding_type=(DiagnosisFindingType, ...),
        summary=(str, Field(min_length=1, max_length=2048)),
        rationale=(str, Field(min_length=1, max_length=4096)),
        query_shape_ids=(tuple[query_type, ...], ()),
        evidence_refs=(tuple[evidence_type, ...], Field(min_length=1)),
    )
    artifact = create_model("GroundedDiagnosisArtifact", __base__=StructuredOutput, findings=(tuple[finding, ...], ()))  # type: ignore[valid-type]
    return cast(type[StructuredOutput], artifact)


def _grounded_ranking_schema(handles: tuple[str, ...]) -> type[StructuredOutput]:
    """Constrain the model to opaque handles from this exact ranking request."""
    if not handles or len(handles) != len(set(handles)):
        raise ValueError("ranking handles must be non-empty and unique")
    handle_type: Any = Literal.__getitem__(handles)
    return cast(
        type[StructuredOutput],
        create_model(
            "GroundedCandidateHandleRanking",
            __base__=StructuredOutput,
            candidate_handles=(tuple[handle_type, ...], Field(min_length=len(handles), max_length=len(handles))),
            rationale=(str, Field(min_length=1)),
            reasons_by_handle=(dict[handle_type, str], Field(default_factory=dict)),
            risk_notes=(tuple[str, ...], ()),
        ),
    )
