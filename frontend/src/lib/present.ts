/** Plain-language presentation of system codes. Every page goes through here so wording stays consistent. */
import type { Tone } from "./lifecycle";

export type RunLike = { status: string; completion_reason?: string | null; job_status?: string | null };

const NO_CHANGE = new Set(["NO_CANDIDATES", "NO_ADMITTED_CANDIDATE", "APPROVAL_REJECTED", "APPROVAL_EXPIRED", "LEGACY_COMPLETED"]);
const TERMINAL = new Set(["COMPLETED", "ROLLED_BACK", "ROLLBACK_BLOCKED", "FAILED"]);

export function isStalled(run: RunLike): boolean {
  return !TERMINAL.has(run.status) && run.status !== "APPROVAL_PENDING" && run.job_status === "FAILED";
}

/** The one-or-two word badge an operator scans for. */
export function runBadge(run: RunLike, stalled = isStalled(run)): { label: string; tone: Tone } {
  if (stalled) return { label: "Stalled", tone: "bad" };
  switch (run.status) {
    case "COMPLETED":
      if (run.completion_reason === "DEPLOYMENT_SUCCEEDED") return { label: "Deployed", tone: "ok" };
      return { label: NO_CHANGE.has(run.completion_reason ?? "") ? "No change" : "Completed", tone: "muted" };
    case "ROLLED_BACK": return { label: "Rolled back", tone: "warn" };
    case "ROLLBACK_BLOCKED": return { label: "Needs review", tone: "bad" };
    case "FAILED": return { label: "Failed", tone: "bad" };
    case "APPROVAL_PENDING": return { label: "Needs approval", tone: "warn" };
    case "APPROVED": return { label: "Approved", tone: "accent" };
    case "DEPLOYING": case "DEPLOYED": return { label: "Deploying", tone: "accent" };
    case "MONITORING": return { label: "Monitoring", tone: "accent" };
    case "CREATED": return { label: "Queued", tone: "accent" };
    default: return { label: "Running", tone: "accent" };
  }
}

/** Which pipeline step a status belongs to, in the rail's words. */
export function stepName(status: string): string {
  const steps: Record<string, string> = {
    CREATED: "Snapshot", SNAPSHOTTING: "Snapshot", DIAGNOSING: "Diagnose", GENERATING_CANDIDATES: "Candidates",
    RANKING: "Rank", CALIBRATING: "Calibrate", EVALUATING: "Sandbox trials", ADMISSION: "Admission", ADMITTED: "Admission",
    APPROVAL_PENDING: "Approval", APPROVED: "Approval", DEPLOYING: "Deploy", DEPLOYED: "Deploy", MONITORING: "Monitor",
  };
  return steps[status] ?? status.charAt(0) + status.slice(1).toLowerCase().replace(/_/g, " ");
}

type ShapeLike = { operation: string; namespace: string; shape?: Record<string, unknown> };

function fieldsOf(value: unknown): string[] {
  if (!value || typeof value !== "object") return [];
  return Object.keys(value as Record<string, unknown>).filter((key) => !key.startsWith("$"));
}

/** "find invoices by account_id, sorted by created_at" from a literal-free query shape. */
export function describeShape(shape: ShapeLike): string {
  const collection = shape.namespace.split(".").slice(1).join(".") || shape.namespace;
  const query = ((shape.shape ?? {}) as { query?: Record<string, unknown> }).query ?? {};
  const filter = fieldsOf(query.filter ?? query.q ?? query.query);
  const sort = fieldsOf(query.sort);
  let text = `${shape.operation} ${collection}`;
  if (filter.length) text += ` by ${filter.join(", ")}`;
  if (sort.length) text += `, sorted by ${sort.join(", ")}`;
  return text;
}

const SOURCES: Record<string, string> = {
  QUERY_STATS: "query statistics", PROFILER: "profiler", DIAGNOSTIC_LOG: "slow-query log", CURRENT_OP: "live operation samples",
};
export function telemetrySource(source: string | null | undefined): string {
  if (!source) return "telemetry";
  return SOURCES[source.toUpperCase()] ?? source.toLowerCase().replace(/_/g, " ");
}

export function topology(value: unknown): string {
  return String(value ?? "unknown").replace(/_/g, " ");
}

const EVENTS: Record<string, string> = {
  DEPLOYMENT_INTENT: "About to add index", DEPLOYMENT_APPLIED: "Index added",
  ROLLBACK_INTENT: "About to remove index", ROLLBACK_APPLIED: "Index removed (rollback)", ROLLBACK_BLOCKED: "Rollback blocked",
  QUERY_SETTINGS_APPLIED: "Query setting applied", QUERY_SETTINGS_ROLLED_BACK: "Query setting removed (rollback)",
};
export function ledgerEvent(eventType: string): string {
  return EVENTS[eventType] ?? eventType.charAt(0) + eventType.slice(1).toLowerCase().replace(/_/g, " ");
}

const ACTORS: Record<string, string> = {
  "durable-production-worker": "Optimizer (deploy)",
  "durable-rollback-worker": "Optimizer (rollback)",
  "durable-query-settings-worker": "Optimizer (query settings)",
};
export function actorName(actor: string): string {
  return ACTORS[actor] ?? actor;
}

/** "17× slower" / "76% faster" for a before → after latency. */
export function speedChange(before: number | null | undefined, after: number | null | undefined): string {
  if (!before || !after || before <= 0 || after <= 0) return "";
  if (Math.abs(after / before - 1) < 0.005) return "no change";
  if (after > before) {
    const ratio = after / before;
    return ratio >= 2 ? `${ratio >= 10 ? ratio.toFixed(0) : ratio.toFixed(1)}× slower` : `${Math.round((ratio - 1) * 100)}% slower`;
  }
  return `${Math.round((1 - after / before) * 100)}% faster`;
}

/**
 * Read an admission bound in plain words. Bounds are log ratios: for a benefit, log(before / after);
 * for a protected metric, log(after / before). Positive regression = slower.
 */
export function boundWords(kind: "benefit" | "protected", bound: number | null | undefined, limit: number | null | undefined): string {
  if (bound === null || bound === undefined || !Number.isFinite(bound)) return "";
  const limitPct = limit === null || limit === undefined ? null : Math.round((kind === "benefit" ? 1 - Math.exp(-limit) : Math.exp(limit) - 1) * 100);
  if (kind === "benefit") {
    const faster = Math.round((1 - Math.exp(-bound)) * 100);
    return faster > 0 ? `at least ${faster}% faster${limitPct !== null ? ` (needs ${limitPct}%)` : ""}` : `not proven faster${limitPct !== null ? ` (needs ${limitPct}%)` : ""}`;
  }
  const change = Math.exp(bound) - 1;
  const words = change <= 0 ? `at worst still ${Math.round(-change * 100)}% faster` : change < 0.01 ? "at most 1% slower" : `could be up to ${Math.round(change * 100)}% slower`;
  return `${words}${limitPct !== null ? ` (limit: ${limitPct}% slower)` : ""}`;
}
