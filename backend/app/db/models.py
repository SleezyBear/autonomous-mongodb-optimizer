"""Relational models for the durable optimizer control plane."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer, JSON, LargeBinary, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func
from pgvector.sqlalchemy import Vector  # type: ignore[import-untyped]


class Base(DeclarativeBase):
    """Base for all control-plane mappings."""


class RunStatus(str, Enum):
    CREATED = "CREATED"
    SNAPSHOTTING = "SNAPSHOTTING"
    DIAGNOSING = "DIAGNOSING"
    GENERATING_CANDIDATES = "GENERATING_CANDIDATES"
    RANKING = "RANKING"
    CALIBRATING = "CALIBRATING"
    EVALUATING = "EVALUATING"
    ADMISSION = "ADMISSION"
    ADMITTED = "ADMITTED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    DEPLOYING = "DEPLOYING"
    DEPLOYED = "DEPLOYED"
    MONITORING = "MONITORING"
    COMPLETED = "COMPLETED"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_BLOCKED = "ROLLBACK_BLOCKED"
    FAILED = "FAILED"


class RunCompletionReason(str, Enum):
    """Machine-readable terminal reason for a successfully completed run."""

    NO_CANDIDATES = "NO_CANDIDATES"
    NO_ADMITTED_CANDIDATE = "NO_ADMITTED_CANDIDATE"
    DEPLOYMENT_SUCCEEDED = "DEPLOYMENT_SUCCEEDED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    LEGACY_COMPLETED = "LEGACY_COMPLETED"


class CandidateStatus(str, Enum):
    PROPOSED = "PROPOSED"
    EVALUATING = "EVALUATING"
    ADMITTED = "ADMITTED"
    REJECTED = "REJECTED"


class TimestampedUUID(Base):
    """Common UUID identity and timestamps for durable entities."""

    __abstract__ = True
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())


class User(TimestampedUUID):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(32), default="VIEWER")
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefreshToken(TimestampedUUID):
    """One-time refresh-token record; only a SHA-256 digest is retained."""

    __tablename__ = "refresh_tokens"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Target(TimestampedUUID):
    __tablename__ = "targets"
    owner_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    deployment_mode: Mapped[str] = mapped_column(String(32), default="APPROVAL_CONTROLLED")
    state: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    connection_label: Mapped[str] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("owner_user_id", "name", name="uq_targets_owner_name"),)


class TargetCredential(TimestampedUUID):
    __tablename__ = "target_credentials"
    target_id: Mapped[UUID] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), unique=True)
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary)
    key_version: Mapped[int] = mapped_column(Integer)
    credential_type: Mapped[str] = mapped_column(String(64))


class EvaluationMapping(TimestampedUUID):
    __tablename__ = "evaluation_mappings"
    target_id: Mapped[UUID] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), unique=True)
    evaluation_target_id: Mapped[UUID] = mapped_column(ForeignKey("targets.id", ondelete="RESTRICT"))
    mapping_status: Mapped[str] = mapped_column(String(32), default="ACTIVE")


class CapabilitySnapshot(TimestampedUUID):
    __tablename__ = "capability_snapshots"
    target_id: Mapped[UUID] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), index=True)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(128))


class Namespace(TimestampedUUID):
    __tablename__ = "namespaces"
    target_id: Mapped[UUID] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(256))
    allowlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("target_id", "name", name="uq_namespaces_target_name"),)


class QueryShape(TimestampedUUID):
    __tablename__ = "query_shapes"
    namespace_id: Mapped[UUID] = mapped_column(ForeignKey("namespaces.id", ondelete="CASCADE"))
    shape_hash: Mapped[str] = mapped_column(String(128))
    normalized_shape: Mapped[dict[str, Any]] = mapped_column(JSON)
    operation: Mapped[str] = mapped_column(String(32))
    __table_args__ = (UniqueConstraint("namespace_id", "shape_hash", name="uq_query_shapes_namespace_hash"),)


class TelemetryWindow(TimestampedUUID):
    __tablename__ = "telemetry_windows"
    target_id: Mapped[UUID] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))


class MetricObservation(TimestampedUUID):
    __tablename__ = "metric_observations"
    telemetry_window_id: Mapped[UUID] = mapped_column(ForeignKey("telemetry_windows.id", ondelete="CASCADE"), index=True)
    query_shape_id: Mapped[UUID | None] = mapped_column(ForeignKey("query_shapes.id", ondelete="SET NULL"))
    metric_name: Mapped[str] = mapped_column(String(64))
    metric_value: Mapped[float] = mapped_column(Numeric)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkloadSnapshot(TimestampedUUID):
    __tablename__ = "workload_snapshots"
    target_id: Mapped[UUID] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"))
    telemetry_window_id: Mapped[UUID] = mapped_column(ForeignKey("telemetry_windows.id", ondelete="RESTRICT"))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(128), unique=True)
    # Nullable for pre-R19E historical rows.  R19E-created rows always set it.
    source_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("optimization_runs.id", ondelete="RESTRICT"), unique=True)
    observed_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    anchor_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completeness: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class WorkloadSnapshotSourceWindow(TimestampedUUID):
    __tablename__ = "workload_snapshot_source_windows"
    workload_snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("workload_snapshots.id", ondelete="RESTRICT"))
    telemetry_window_id: Mapped[UUID] = mapped_column(ForeignKey("telemetry_windows.id", ondelete="RESTRICT"))
    __table_args__ = (UniqueConstraint("workload_snapshot_id", "telemetry_window_id", name="uq_snapshot_source_window"),)


class WorkloadSnapshotQueryShape(TimestampedUUID):
    __tablename__ = "workload_snapshot_query_shapes"
    workload_snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("workload_snapshots.id", ondelete="RESTRICT"), index=True)
    query_shape_id: Mapped[UUID] = mapped_column(ForeignKey("query_shapes.id", ondelete="RESTRICT"))
    query_shape_hash: Mapped[str] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(32))
    namespace: Mapped[str] = mapped_column(String(256))
    observed_operation_count: Mapped[int] = mapped_column(Integer)
    successful_operation_count: Mapped[int | None] = mapped_column(Integer)
    failure_count: Mapped[int | None] = mapped_column(Integer)
    timeout_count: Mapped[int | None] = mapped_column(Integer)
    aggregate_execution_time_ms: Mapped[float | None] = mapped_column(Numeric)
    workload_operation_share: Mapped[float] = mapped_column(Numeric)
    execution_time_share: Mapped[float | None] = mapped_column(Numeric)
    manually_critical: Mapped[bool] = mapped_column(Boolean, default=False)
    protected_manual_critical: Mapped[bool] = mapped_column(Boolean, default=False)
    protected_operation_share: Mapped[bool] = mapped_column(Boolean, default=False)
    protected_execution_time_share: Mapped[bool] = mapped_column(Boolean, default=False)
    base_protected: Mapped[bool] = mapped_column(Boolean, default=False)
    provider: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("workload_snapshot_id", "query_shape_id", name="uq_snapshot_query_shape"),)


class WorkloadSnapshotMetric(TimestampedUUID):
    __tablename__ = "workload_snapshot_metrics"
    workload_snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("workload_snapshots.id", ondelete="RESTRICT"), index=True)
    source_metric_observation_id: Mapped[UUID] = mapped_column(ForeignKey("metric_observations.id", ondelete="RESTRICT"))
    scope: Mapped[str] = mapped_column(String(32))
    metric_name: Mapped[str] = mapped_column(String(64))
    metric_value: Mapped[float] = mapped_column(Numeric)
    __table_args__ = (UniqueConstraint("workload_snapshot_id", "source_metric_observation_id", name="uq_snapshot_metric_observation"),)


class AIInvocation(TimestampedUUID):
    """Append-only, privacy-safe audit history for advisory model attempts."""

    __tablename__ = "ai_invocations"
    optimization_run_id: Mapped[UUID] = mapped_column(ForeignKey("optimization_runs.id", ondelete="RESTRICT"), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(256))
    prompt_version: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(64))
    workload_snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("workload_snapshots.id", ondelete="RESTRICT"))
    input_hash: Mapped[str] = mapped_column(String(128))
    sanitized_input: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_hash: Mapped[str | None] = mapped_column(String(128))
    validated_output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    latency_ms: Mapped[float | None] = mapped_column(Numeric)
    status: Mapped[str] = mapped_column(String(32))
    safe_error_code: Mapped[str | None] = mapped_column(String(128))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_number: Mapped[int] = mapped_column(Integer)


class DiagnosisArtifact(TimestampedUUID):
    """One immutable, authoritative grounded diagnosis for an optimization run."""

    __tablename__ = "diagnosis_artifacts"
    optimization_run_id: Mapped[UUID] = mapped_column(ForeignKey("optimization_runs.id", ondelete="RESTRICT"), unique=True)
    workload_snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("workload_snapshots.id", ondelete="RESTRICT"))
    target_id: Mapped[UUID] = mapped_column(ForeignKey("targets.id", ondelete="RESTRICT"))
    ai_invocation_id: Mapped[UUID] = mapped_column(ForeignKey("ai_invocations.id", ondelete="RESTRICT"))
    schema_version: Mapped[str] = mapped_column(String(64))
    source_snapshot_fingerprint: Mapped[str] = mapped_column(String(128))
    deterministic_evidence_hash: Mapped[str] = mapped_column(String(128))
    artifact_fingerprint: Mapped[str] = mapped_column(String(128), unique=True)


class DiagnosisFinding(TimestampedUUID):
    """Typed, immutable interpretation grounded in snapshot evidence identifiers."""

    __tablename__ = "diagnosis_findings"
    diagnosis_artifact_id: Mapped[UUID] = mapped_column(ForeignKey("diagnosis_artifacts.id", ondelete="RESTRICT"), index=True)
    finding_id: Mapped[str] = mapped_column(String(128))
    finding_type: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(String(2048))
    rationale: Mapped[str] = mapped_column(String(4096))
    query_shape_ids: Mapped[list[str]] = mapped_column(JSON)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("diagnosis_artifact_id", "finding_id", name="uq_diagnosis_finding_identity"),)


class OptimizationRun(TimestampedUUID):
    __tablename__ = "optimization_runs"
    target_id: Mapped[UUID] = mapped_column(ForeignKey("targets.id", ondelete="RESTRICT"), index=True)
    workload_snapshot_id: Mapped[UUID | None] = mapped_column(ForeignKey("workload_snapshots.id", ondelete="RESTRICT"))
    status: Mapped[RunStatus] = mapped_column(SAEnum(RunStatus, name="run_status"), default=RunStatus.CREATED)
    requested_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    deployment_mode: Mapped[str] = mapped_column(String(32))
    primary_metric_key: Mapped[str | None] = mapped_column(String(256))
    completion_reason: Mapped[RunCompletionReason | None] = mapped_column(
        SAEnum(RunCompletionReason, name="run_completion_reason")
    )


class Candidate(TimestampedUUID):
    __tablename__ = "candidates"
    optimization_run_id: Mapped[UUID] = mapped_column(ForeignKey("optimization_runs.id", ondelete="CASCADE"), index=True)
    query_shape_id: Mapped[UUID] = mapped_column(ForeignKey("query_shapes.id", ondelete="RESTRICT"))
    status: Mapped[CandidateStatus] = mapped_column(SAEnum(CandidateStatus, name="candidate_status"), default=CandidateStatus.PROPOSED)
    candidate_hash: Mapped[str] = mapped_column(String(128), unique=True)
    source_snapshot_id: Mapped[UUID | None] = mapped_column(ForeignKey("workload_snapshots.id", ondelete="RESTRICT"))
    source_diagnosis_id: Mapped[UUID | None] = mapped_column(ForeignKey("diagnosis_artifacts.id", ondelete="RESTRICT"))
    deterministic_fingerprint: Mapped[str | None] = mapped_column(String(128))
    generation_rule_id: Mapped[str | None] = mapped_column(String(128))
    generation_rule_version: Mapped[str | None] = mapped_column(String(64))
    affected_query_shape_ids: Mapped[list[str] | None] = mapped_column(JSON)
    evidence_refs: Mapped[list[str] | None] = mapped_column(JSON)
    policy_classification: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("optimization_run_id", "deterministic_fingerprint", name="uq_candidate_run_fingerprint"),)


class CandidateAction(TimestampedUUID):
    __tablename__ = "candidate_actions"
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    action_type: Mapped[str] = mapped_column(String(64))
    action_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    reversible: Mapped[bool] = mapped_column(Boolean, default=True)


class EvaluationRun(TimestampedUUID):
    __tablename__ = "evaluation_runs"
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    environment_fingerprint: Mapped[str] = mapped_column(String(128))


class TrialPair(TimestampedUUID):
    __tablename__ = "trial_pairs"
    evaluation_run_id: Mapped[UUID] = mapped_column(ForeignKey("evaluation_runs.id", ondelete="CASCADE"), index=True)
    pair_number: Mapped[int] = mapped_column(Integer)
    baseline_measurement: Mapped[dict[str, Any]] = mapped_column(JSON)
    candidate_measurement: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("evaluation_run_id", "pair_number", name="uq_trial_pairs_run_number"),)


class TrialMetric(TimestampedUUID):
    __tablename__ = "trial_metrics"
    trial_pair_id: Mapped[UUID] = mapped_column(ForeignKey("trial_pairs.id", ondelete="CASCADE"), index=True)
    metric_name: Mapped[str] = mapped_column(String(64))
    baseline_value: Mapped[float] = mapped_column(Numeric)
    candidate_value: Mapped[float] = mapped_column(Numeric)


class AdmissionDecision(TimestampedUUID):
    __tablename__ = "admission_decisions"
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("candidates.id", ondelete="RESTRICT"), unique=True)
    verdict: Mapped[str] = mapped_column(String(32))
    evidence_hash: Mapped[str] = mapped_column(String(128))


class CandidateGenerationArtifact(TimestampedUUID):
    """Authoritative immutable output of deterministic candidate generation."""

    __tablename__ = "candidate_generation_artifacts"
    optimization_run_id: Mapped[UUID] = mapped_column(ForeignKey("optimization_runs.id", ondelete="RESTRICT"), unique=True)
    workload_snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("workload_snapshots.id", ondelete="RESTRICT"))
    diagnosis_artifact_id: Mapped[UUID] = mapped_column(ForeignKey("diagnosis_artifacts.id", ondelete="RESTRICT"))
    generator_version: Mapped[str] = mapped_column(String(64))
    candidate_count: Mapped[int] = mapped_column(Integer)
    ordered_candidate_fingerprints: Mapped[list[str]] = mapped_column(JSON)
    artifact_fingerprint: Mapped[str] = mapped_column(String(128), unique=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CandidateRankingArtifact(TimestampedUUID):
    """Validated immutable advisory ordering of an already-fixed candidate set."""

    __tablename__ = "candidate_ranking_artifacts"
    optimization_run_id: Mapped[UUID] = mapped_column(ForeignKey("optimization_runs.id", ondelete="RESTRICT"), unique=True)
    generation_artifact_id: Mapped[UUID] = mapped_column(ForeignKey("candidate_generation_artifacts.id", ondelete="RESTRICT"))
    ai_invocation_id: Mapped[UUID] = mapped_column(ForeignKey("ai_invocations.id", ondelete="RESTRICT"))
    ordered_candidate_ids: Mapped[list[str]] = mapped_column(JSON)
    artifact_fingerprint: Mapped[str] = mapped_column(String(128), unique=True)


class EvaluationPlan(TimestampedUUID):
    """Frozen selection and profile before any controlled benchmark measurement."""

    __tablename__ = "evaluation_plans"
    optimization_run_id: Mapped[UUID] = mapped_column(ForeignKey("optimization_runs.id", ondelete="RESTRICT"), unique=True)
    ranking_artifact_id: Mapped[UUID] = mapped_column(ForeignKey("candidate_ranking_artifacts.id", ondelete="RESTRICT"))
    profile: Mapped[str] = mapped_column(String(32))
    selected_candidate_ids: Mapped[list[str]] = mapped_column(JSON)
    calibration: Mapped[dict[str, Any]] = mapped_column(JSON)
    artifact_fingerprint: Mapped[str] = mapped_column(String(128), unique=True)


class AdmissionArtifact(TimestampedUUID):
    """Immutable run-level result selecting only independently admitted candidates."""

    __tablename__ = "admission_artifacts"
    optimization_run_id: Mapped[UUID] = mapped_column(ForeignKey("optimization_runs.id", ondelete="RESTRICT"), unique=True)
    evaluation_plan_id: Mapped[UUID] = mapped_column(ForeignKey("evaluation_plans.id", ondelete="RESTRICT"))
    selected_candidate_id: Mapped[UUID | None] = mapped_column(ForeignKey("candidates.id", ondelete="RESTRICT"))
    admitted_candidate_ids: Mapped[list[str]] = mapped_column(JSON)
    production_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    artifact_fingerprint: Mapped[str] = mapped_column(String(128), unique=True)


class AuthorityDecision(TimestampedUUID):
    """Immutable authority bound to the exact selected candidate definition."""

    __tablename__ = "authority_decisions"
    optimization_run_id: Mapped[UUID] = mapped_column(ForeignKey("optimization_runs.id", ondelete="RESTRICT"), unique=True)
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("candidates.id", ondelete="RESTRICT"))
    candidate_fingerprint: Mapped[str] = mapped_column(String(128))
    deployment_mode: Mapped[str] = mapped_column(String(32))
    authority_type: Mapped[str] = mapped_column(String(32))
    authority_reason: Mapped[str] = mapped_column(String(128))
    production_autonomy_eligible: Mapped[bool] = mapped_column(Boolean)
    integrity_fingerprint: Mapped[str] = mapped_column(String(128), unique=True)


class DeploymentArtifact(TimestampedUUID):
    """Durable intent and reconciliation state for one owned production action."""

    __tablename__ = "deployment_artifacts"
    optimization_run_id: Mapped[UUID] = mapped_column(ForeignKey("optimization_runs.id", ondelete="RESTRICT"), unique=True)
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("candidates.id", ondelete="RESTRICT"))
    candidate_fingerprint: Mapped[str] = mapped_column(String(128))
    action_fingerprint: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32))
    before_state: Mapped[dict[str, Any]] = mapped_column(JSON)
    inverse_action: Mapped[dict[str, Any]] = mapped_column(JSON)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class QuerySettingsDeployment(TimestampedUUID):
    """Owned query-settings intent, observed states, and exact inverse."""

    __tablename__ = "query_settings_deployments"
    target_id: Mapped[UUID] = mapped_column(ForeignKey("targets.id", ondelete="RESTRICT"), index=True)
    optimization_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("optimization_runs.id", ondelete="RESTRICT"))
    query_shape_hash: Mapped[str] = mapped_column(String(255))
    namespace: Mapped[str] = mapped_column(String(320))
    action_fingerprint: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32))
    before_state: Mapped[dict[str, Any]] = mapped_column(JSON)
    intended_state: Mapped[dict[str, Any]] = mapped_column(JSON)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    inverse_action: Mapped[dict[str, Any]] = mapped_column(JSON)
    ownership_token: Mapped[str] = mapped_column(String(128))
    __table_args__ = (
        UniqueConstraint(
            "target_id", "query_shape_hash", "ownership_token", name="uq_query_settings_owned"
        ),
    )


class SafetyResult(TimestampedUUID):
    __tablename__ = "safety_results"
    admission_decision_id: Mapped[UUID] = mapped_column(ForeignKey("admission_decisions.id", ondelete="CASCADE"), index=True)
    check_name: Mapped[str] = mapped_column(String(64))
    verdict: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict[str, Any]] = mapped_column(JSON)


class ApprovalRequest(TimestampedUUID):
    __tablename__ = "approval_requests"
    admission_decision_id: Mapped[UUID | None] = mapped_column(ForeignKey("admission_decisions.id", ondelete="RESTRICT"), unique=True)
    requested_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    action_id: Mapped[str] = mapped_column(String(128), index=True)
    target_id: Mapped[UUID] = mapped_column(ForeignKey("targets.id", ondelete="RESTRICT"), index=True)
    candidate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), index=True)
    optimization_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("optimization_runs.id", ondelete="RESTRICT"), unique=True)
    candidate_fingerprint: Mapped[str | None] = mapped_column(String(128))
    evidence_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32))


class ApprovalDecision(TimestampedUUID):
    __tablename__ = "approval_decisions"
    approval_request_id: Mapped[UUID] = mapped_column(ForeignKey("approval_requests.id", ondelete="CASCADE"), unique=True)
    decided_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    decision: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)


class LedgerEntry(TimestampedUUID):
    __tablename__ = "ledger_entries"
    optimization_run_id: Mapped[UUID] = mapped_column(ForeignKey("optimization_runs.id", ondelete="RESTRICT"), index=True)
    candidate_id: Mapped[UUID | None] = mapped_column(ForeignKey("candidates.id", ondelete="SET NULL"))
    sequence_number: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(128))
    __table_args__ = (UniqueConstraint("optimization_run_id", "sequence_number", name="uq_ledger_run_sequence"),)


class RollbackRecord(TimestampedUUID):
    __tablename__ = "rollback_records"
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("candidates.id", ondelete="RESTRICT"), unique=True)
    status: Mapped[str] = mapped_column(String(32))
    rollback_payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ExperienceRecord(TimestampedUUID):
    __tablename__ = "experience_records"
    candidate_id: Mapped[UUID | None] = mapped_column(ForeignKey("candidates.id", ondelete="SET NULL"))
    outcome: Mapped[str | None] = mapped_column(String(64))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(768))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    adapter_type: Mapped[str] = mapped_column(String(64), default="mongodb")
    action_type: Mapped[str] = mapped_column(String(64), index=True, default="CREATE_INDEX")
    query_structure_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    workload_features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    bottleneck: Mapped[str | None] = mapped_column(String(128))
    candidate_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prediction: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    admission_outcome: Mapped[str | None] = mapped_column(String(64))
    actual_postdeploy_outcome: Mapped[str | None] = mapped_column(String(64))
    rollback_outcome: Mapped[str | None] = mapped_column(String(64))
    embedding_model: Mapped[str] = mapped_column(String(128), default="embeddinggemma")
    embedding_model_version: Mapped[str] = mapped_column(String(128), default="unknown")


class Job(TimestampedUUID):
    __tablename__ = "jobs"
    optimization_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("optimization_runs.id", ondelete="SET NULL"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(64), default="OPTIMIZATION")
    # Retry scheduling and sanitized failure facts (migration 0012).
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(TimestampedUUID):
    __tablename__ = "audit_events"
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    optimization_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("optimization_runs.id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(String(64))
    event_payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class Setting(TimestampedUUID):
    __tablename__ = "settings"
    scope: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("scope", "key", name="uq_settings_scope_key"),)
