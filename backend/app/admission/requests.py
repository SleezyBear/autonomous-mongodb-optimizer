"""Build frozen-engine admission requests from persisted sandbox trial evidence.

The deterministic statistics engine is unchanged.  This module only translates
the measured pairs recorded by the sandbox evaluator into
:class:`AdmissionRequest` objects:

* the run's primary metric must prove a meaningful benefit;
* p95 and p99 read latency are always protected; write p95 latency is
  protected whenever the snapshot contained writes for the collection;
* A/A calibration scores come from the interleaved baseline arms;
* any result-set mismatch between arms is a ``result_correctness`` safety
  invariant failure, which rejects the candidate before statistics run;
* every pair must share one evaluation environment fingerprint.
"""

from __future__ import annotations

import math
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.admission.models import AdmissionRequest, BenchmarkProfile, MetricEvaluationInput, MetricPolicy
from app.admission.policy import DEFAULT_POLICIES
from app.db import models


PROTECTED_READ_METRICS = ("p95_latency_ms", "p99_latency_ms")
_POLICY_ALIASES = {"throughput_ops_per_second": "throughput_ops_s"}


class AdmissionEvidenceError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.error_code = code


def policy_for(metric_key: str) -> MetricPolicy:
    policy = DEFAULT_POLICIES.get(_POLICY_ALIASES.get(metric_key, metric_key))
    if policy is None:
        raise AdmissionEvidenceError("PRIMARY_METRIC_HAS_NO_POLICY")
    return policy


def _aa_scores(policy: MetricPolicy, first: list[float], second: list[float]) -> tuple[float, ...]:
    scores: list[float] = []
    for a, b in zip(first, second):
        if policy.comparison_mode.value == "LOG_RATIO":
            scores.append(math.log(max(b, 0.001) / max(a, 0.001)))
        else:
            scores.append(b - a)
    return tuple(scores)


class AdmissionRequestBuilder:
    """Callable ``admission_requests`` boundary for the orchestrator."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def __call__(self, run_id: UUID) -> dict[UUID, AdmissionRequest]:
        trial_metric, trial_pair, evaluation = models.TrialMetric.__table__, models.TrialPair.__table__, models.EvaluationRun.__table__
        async with self._engine.connect() as connection:
            run = (await connection.execute(select(models.OptimizationRun.__table__).where(models.OptimizationRun.__table__.c.id == run_id))).mappings().one()
            plan = (await connection.execute(select(models.EvaluationPlan.__table__).where(models.EvaluationPlan.__table__.c.optimization_run_id == run_id))).mappings().one()
            selected = [UUID(value) for value in plan["selected_candidate_ids"]]
            rows = (await connection.execute(
                select(
                    evaluation.c.id.label("evaluation_id"), evaluation.c.candidate_id, evaluation.c.environment_fingerprint,
                    trial_pair.c.pair_number, trial_metric.c.metric_name, trial_metric.c.baseline_value, trial_metric.c.candidate_value,
                )
                .join(trial_pair, trial_pair.c.evaluation_run_id == evaluation.c.id)
                .join(trial_metric, trial_metric.c.trial_pair_id == trial_pair.c.id)
                .where(evaluation.c.candidate_id.in_(selected))
                .order_by(evaluation.c.candidate_id, trial_pair.c.pair_number, trial_metric.c.metric_name)
            )).mappings().all()
        primary_key = str(run["primary_metric_key"] or "p99_latency_ms")
        primary_policy = policy_for(primary_key)
        profile = BenchmarkProfile(str(plan["profile"]))
        grouped: dict[UUID, dict[str, Any]] = {}
        for row in rows:
            entry = grouped.setdefault(row["candidate_id"], {"evaluation_id": row["evaluation_id"], "environment": row["environment_fingerprint"], "metrics": {}})
            series = entry["metrics"].setdefault(str(row["metric_name"]), {})
            series[int(row["pair_number"])] = (float(row["baseline_value"]), float(row["candidate_value"]))
        requests: dict[UUID, AdmissionRequest] = {}
        for candidate_id in selected:
            if candidate_id not in grouped:
                raise AdmissionEvidenceError("CANDIDATE_TRIAL_EVIDENCE_MISSING")
            entry = grouped[candidate_id]
            metrics: dict[str, dict[int, tuple[float, float]]] = entry["metrics"]
            pairs = sorted(metrics.get(primary_key, {}))
            keys = list(dict.fromkeys([primary_key, *PROTECTED_READ_METRICS]))
            if "write_p95_latency_ms" in metrics:
                keys.append("write_p95_latency_ms")
            inputs: list[MetricEvaluationInput] = []
            for key in keys:
                series = metrics.get(key, {})
                aa = metrics.get(f"aa_{key}", {})
                policy = primary_policy if key == primary_key else policy_for(key)
                ordered = [series[number] for number in pairs if number in series]
                aa_ordered = [aa[number] for number in pairs if number in aa]
                inputs.append(MetricEvaluationInput(
                    key, "GLOBAL", "replayed-workload", policy,
                    tuple(value[0] for value in ordered), tuple(value[1] for value in ordered),
                    aa_scores=_aa_scores(policy, [value[0] for value in aa_ordered], [value[1] for value in aa_ordered]),
                ))
            mismatches = sum(value[1] for value in metrics.get("result_mismatch_count", {}).values())
            safe = mismatches == 0
            requests[candidate_id] = AdmissionRequest(
                str(candidate_id), str(entry["evaluation_id"]), profile=profile, primary_metric_key=primary_key,
                metrics=tuple(inputs), environment_valid=bool(entry["environment"]),
                safety_invariants_safe=safe,
                safety_invariant_results=() if safe else ("result_correctness",),
            )
        return requests
