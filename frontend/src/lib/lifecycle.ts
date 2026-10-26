import type { RunSummary } from "../api/client";

export type StepState = "done" | "current" | "waiting" | "blocked" | "stopped" | "reverted" | "pending" | "skipped";
export type RailStep = { key: string; label: string; gate?: boolean; ai?: boolean; statuses: string[] };

/** The optimization lifecycle as operators think about it; gates are decision points. */
export const RAIL: readonly RailStep[] = [
  { key: "snapshot", label: "Snapshot", statuses: ["CREATED", "SNAPSHOTTING"] },
  { key: "diagnose", label: "Diagnose", ai: true, statuses: ["DIAGNOSING"] },
  { key: "candidates", label: "Candidates", statuses: ["GENERATING_CANDIDATES"] },
  { key: "rank", label: "Rank", ai: true, statuses: ["RANKING"] },
  { key: "calibrate", label: "Calibrate", statuses: ["CALIBRATING"] },
  { key: "evaluate", label: "Sandbox trials", statuses: ["EVALUATING"] },
  { key: "admit", label: "Admission", gate: true, statuses: ["ADMISSION", "ADMITTED"] },
  { key: "approve", label: "Approval", gate: true, statuses: ["APPROVAL_PENDING", "APPROVED"] },
  { key: "deploy", label: "Deploy", statuses: ["DEPLOYING", "DEPLOYED"] },
  { key: "monitor", label: "Monitor", statuses: ["MONITORING"] },
];

export const TERMINAL = new Set(["COMPLETED", "ROLLED_BACK", "ROLLBACK_BLOCKED", "FAILED"]);

const stopAt: Record<string, string> = {
  NO_CANDIDATES: "candidates",
  NO_ADMITTED_CANDIDATE: "admit",
  APPROVAL_REJECTED: "approve",
  APPROVAL_EXPIRED: "approve",
};

/** Compute the visual state of every rail step from a persisted run. */
export function railStates(run: Pick<RunSummary, "status" | "completion_reason" | "job_status" | "deployment_mode">, authorityAutomatic = false): Record<string, StepState> {
  const states: Record<string, StepState> = {};
  const index = (key: string) => RAIL.findIndex((step) => step.key === key);
  if (run.status === "COMPLETED" || run.status === "ROLLED_BACK" || run.status === "ROLLBACK_BLOCKED") {
    const stop = run.status === "COMPLETED" ? stopAt[run.completion_reason ?? ""] : undefined;
    const stopIndex = stop ? index(stop) : RAIL.length;
    RAIL.forEach((step, position) => {
      if (position < stopIndex) states[step.key] = "done";
      else if (position === stopIndex) states[step.key] = "stopped";
      else states[step.key] = "skipped";
    });
    if (run.status === "ROLLBACK_BLOCKED") states.monitor = "blocked";
    if (run.status === "ROLLED_BACK") states.monitor = "reverted";
    if (authorityAutomatic && states.approve === "done") states.approve = "skipped";
    return states;
  }
  const current = RAIL.findIndex((step) => step.statuses.includes(run.status));
  const stalled = run.job_status === "FAILED" || run.status === "FAILED";
  RAIL.forEach((step, position) => {
    if (current < 0) states[step.key] = run.status === "FAILED" ? "pending" : "pending";
    else if (position < current) states[step.key] = "done";
    else if (position === current) {
      if (stalled) states[step.key] = "blocked";
      else if (run.status === "APPROVAL_PENDING") states[step.key] = "waiting";
      else if (run.status === "ADMITTED" || run.status === "APPROVED" || run.status === "DEPLOYED") states[step.key] = "done";
      else states[step.key] = "current";
    } else states[step.key] = "pending";
  });
  // A finished sub-step means the next one is the live position.
  const keys = RAIL.map((step) => step.key);
  if (["ADMITTED", "APPROVED", "DEPLOYED"].includes(run.status) && current >= 0 && current + 1 < keys.length) {
    states[keys[current + 1]] = stalled ? "blocked" : "current";
  }
  return states;
}

export function runOutcome(run: Pick<RunSummary, "status" | "completion_reason">): string {
  if (run.status === "COMPLETED") {
    switch (run.completion_reason) {
      case "NO_CANDIDATES": return "Completed safely: no candidates were generated.";
      case "NO_ADMITTED_CANDIDATE": return "Completed safely: no candidate met admission requirements.";
      case "APPROVAL_REJECTED": return "Completed safely: the approval request was rejected.";
      case "APPROVAL_EXPIRED": return "Completed safely: nobody approved within 15 minutes, so nothing was deployed.";
      case "DEPLOYMENT_SUCCEEDED": return "Deployment completed successfully.";
      default: return run.completion_reason ?? "Deployment completed successfully.";
    }
  }
  switch (run.status) {
    case "ROLLED_BACK": return "Monitoring detected a regression; the optimizer's change was rolled back.";
    case "ROLLBACK_BLOCKED": return "Rollback was blocked because the target drifted; manual review is required.";
    case "FAILED": return "The run failed and made no further changes.";
    case "APPROVAL_PENDING": return "Waiting for an independent approver.";
    case "CREATED": return "Queued for the worker.";
    default: return "In progress.";
  }
}

export type Tone = "ok" | "warn" | "bad" | "ai" | "accent" | "muted";

export function statusTone(status: string, completionReason?: string | null, stalled = false): Tone {
  if (stalled) return "bad";
  if (status === "COMPLETED") return completionReason === "DEPLOYMENT_SUCCEEDED" ? "ok" : "muted";
  if (status === "ROLLED_BACK" || status === "APPROVAL_PENDING") return "warn";
  if (status === "ROLLBACK_BLOCKED" || status === "FAILED") return "bad";
  if (status === "DIAGNOSING" || status === "RANKING") return "ai";
  return "accent";
}

export function statusLabel(status: string): string {
  const text = status.toLowerCase().replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Plain-language explanations for durable blocker codes recorded by the worker. */
const BLOCKERS: Record<string, string> = {
  NO_COMPLETED_TELEMETRY: "No slow-query data has been collected for this database yet. Run some traffic (locally: make dev-workload) and wait about 30 seconds.",
  STALE_TELEMETRY: "The latest query data is more than 5 minutes old. Make sure traffic is running and the worker can reach the database.",
  EMPTY_WORKLOAD: "The collected query data had no queries to analyse.",
  TARGET_EXECUTOR_CONNECTION_NOT_CONFIGURED: "This target's connection is not allowed to add indexes. Pick a production connection in the target's Settings.",
  MONITORED_OBSERVER_CONNECTION_REQUIRED: "This target's connection cannot read query data. Pick a production connection in the target's Settings.",
  EVALUATION_SANDBOX_CONNECTION_REQUIRED: "The chosen sandbox uses a connection that is not a sandbox copy. Choose a different sandbox.",
  EVALUATION_MAPPING_REQUIRED: "Choose a sandbox copy for this database before running.",
  EVALUATION_COPY_LIMIT_EXCEEDED: "The collection is too large to copy to the sandbox (limit set by EVALUATION_COPY_MAX_DOCUMENTS).",
  NO_REPLAYABLE_QUERY_SHAPES: "None of the slow queries could be safely repeated on the sandbox copy.",
  EVALUATION_COLLECTION_EMPTY: "The sandbox copy of the collection is empty, so nothing could be tested.",
  AI_PROVIDER_UNAVAILABLE: "The AI model could not be reached. Start Ollama, or set AI_PROVIDER=deterministic to run without AI.",
  AI_PROVIDER_TIMEOUT: "The AI model took too long to answer.",
  MONITORING_BASELINE_MISSING: "No 'before' measurements of production were taken, so the change cannot be monitored.",
  TARGET_LOCK_UNAVAILABLE: "Another change is being applied to this database right now. The worker will try again.",
  PREDEPLOYMENT_EVIDENCE_DRIFT: "The database changed after the change was approved, so the deployment was refused for safety.",
  CURRENT_APPROVAL_REQUIRED: "The approval expired or no longer matches the change, so the deployment was refused.",
  ORCHESTRATION_INVARIANT_FAILURE: "The run's saved data failed an integrity check, so the worker stopped for safety.",
  DURABLE_ORCHESTRATOR_REQUIRED: "The worker has no database connections set up, so it cannot run.",
  DIAGNOSIS_SERVICE_UNAVAILABLE: "The worker has no database connections set up (TARGET_CONNECTIONS_FILE).",
};

export function explainBlocker(code: string | null | undefined): string {
  if (!code) return "The worker stopped retrying this run.";
  return BLOCKERS[code] ?? `The worker recorded ${code}.`;
}

const ADMISSION_REASONS: Record<string, string> = {
  AA_CALIBRATION_EXCEEDS_PROFILE_MAXIMUM:
    "The sandbox was too noisy to prove this change. Back-to-back runs with no change (A/A calibration) varied so much that more trial pairs would be needed than this profile allows, so nothing was deployed. Quiet the sandbox or use a larger replica, then run again.",
  INSUFFICIENT_CANDIDATE_PAIRS: "Fewer trial pairs were measured than the noise calibration requires, so the result cannot be trusted yet.",
  PRIMARY_BENEFIT_NOT_PROVEN: "The speed-up was not large or consistent enough to prove at least 5% faster with 95% confidence.",
  PROTECTED_METRIC_REGRESSION: "The tests could not rule out that something protected (p95, p99 or write speed) gets more than 3% slower, so the change was not applied. The row marked Fail below shows which one.",
  FAMILY_WISE_PROTECTION_FAILED: "",
  SAFETY_INVARIANT_FAILED: "Query results differed between the two arms, so the change was rejected outright.",
  ENVIRONMENT_MISMATCH: "The sandbox environment changed between trials, so the comparison is not valid.",
  REQUIRED_METRIC_MISSING: "A metric required for admission was not recorded during the trials.",
  PRIMARY_METRIC_MISSING: "The primary metric was not recorded during the trials.",
  INVALID_METRIC_DATA: "Trial measurements contained invalid values.",
};

export function explainAdmission(codes: readonly string[]): string[] {
  // An empty explanation means the code only restates another one (e.g. the family-wise check behind a protected-metric failure).
  return codes.map((code) => ADMISSION_REASONS[code] ?? `Admission recorded ${code}.`).filter(Boolean);
}
