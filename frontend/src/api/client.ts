import type { ApiGetResponses, ApiPath } from "./openapi.generated";
import { clearSession, readSession, shouldRefresh, storeTokens } from "../lib/session";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";

export class ApiError extends Error {
  constructor(public readonly status: number, message: string, public readonly detail?: string) {
    super(message);
    this.name = "ApiError";
  }
}

export function shouldRetryApiRequest(failureCount: number, error: Error): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
  return failureCount < 3;
}

let refreshing: Promise<boolean> | null = null;

/** Rotate tokens ahead of access-token expiry. A rejected token is never silently replaced. */
async function refreshIfExpiring(): Promise<void> {
  const session = readSession();
  if (!session.refreshToken || !shouldRefresh(session.accessToken)) return;
  refreshing ??= (async () => {
    try {
      const response = await fetch(`${API_BASE}/auth/refresh`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh_token: session.refreshToken }) });
      if (!response.ok) { clearSession(); return false; }
      storeTokens(await response.json() as { access_token: string; refresh_token: string });
      return true;
    } finally { refreshing = null; }
  })();
  await refreshing;
}

async function request(path: string, init?: RequestInit): Promise<unknown> {
  await refreshIfExpiring();
  const token = readSession().accessToken;
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...init?.headers },
  });
  if (!response.ok) {
    let detail: string | undefined;
    try { const body = await response.json() as { detail?: unknown }; detail = typeof body.detail === "string" ? body.detail : undefined; } catch { /* non-JSON body */ }
    const message = response.status === 403 ? (detail ?? "You are not authorized for this action.") : response.status === 401 ? "API request failed (401)" : detail ?? `API request failed (${response.status})`;
    throw new ApiError(response.status, message, detail);
  }
  return response.json();
}

export async function getApi<Path extends ApiPath>(path: Path, query = ""): Promise<ApiGetResponses[Path]> {
  return request(path + query) as Promise<ApiGetResponses[Path]>;
}

// ---------- Types ----------

export type Role = "VIEWER" | "OPERATOR" | "APPROVER" | "ADMIN";

export type RunSummary = {
  run_id: string; target_id: string; status: string; deployment_mode: string;
  primary_metric_key: string | null; completion_reason: string | null;
  created_at: string; updated_at: string;
  target_name?: string | null; job_status?: string | null; job_attempts?: number | null; last_error_code?: string | null;
};

export type Check = { key: string; label: string; ok: boolean; detail: string };

export type WorkerStatus = {
  state: "ONLINE" | "STALE" | "NEVER_SEEN"; worker_id?: string; heartbeat_at?: string; heartbeat_age_seconds?: number | null; started_at?: string;
  ai: { provider: string; chat_model: string | null; embedding_model: string | null } | null;
  connections: { label: string; database: string; roles: string[]; hosts: string[] }[];
  orchestration: string; telemetry_interval_seconds?: number;
};

export type Overview = {
  runs_by_status: Record<string, number>; active_runs: number; pending_approvals: number;
  stalled_runs: { run_id: string; status: string; target_id: string; last_error_code: string | null; job_status: string; attempts: number }[];
  recent_runs: { run_id: string; status: string; completion_reason: string | null; deployment_mode: string; target_name: string; created_at: string; updated_at: string | null }[];
  targets: { id: string; name: string; connection_label: string; deployment_mode: string; ready: boolean; checks: Check[]; telemetry_age_seconds: number | null }[];
  deployments_last_7_days: number; worker: WorkerStatus; principal: { user_id: string; role: Role };
};

export type TargetRecord = { id: string; name: string; connection_label: string; deployment_mode: string; state: string; is_active: boolean; owner_user_id: string; created_at: string };

export type TargetDetail = {
  target: TargetRecord; role: "MONITORED" | "SANDBOX"; mapped_as_sandbox_for?: number; sandbox_for?: { id: string; name: string }[]; ready: boolean; checks: Check[];
  evaluation_mapping: { evaluation_target_id: string; evaluation_target_name: string | null; status: string } | null;
  namespaces: { id: string; name: string; allowlisted: boolean; query_shapes: number }[];
  capability: { capabilities: Record<string, unknown>; fingerprint: string; observed_at: string | null } | null;
  telemetry: { latest_window_at: string | null; source: string | null; age_seconds: number | null; fresh_within_seconds: number; collector: Record<string, unknown> | null };
  connection: { label: string; database: string; roles: string[]; hosts: string[] } | null;
  recent_runs: { run_id: string; status: string; completion_reason: string | null; created_at: string }[];
};

export type Metrics = Record<string, number>;
export type MetricResult = {
  metric_key: string; baseline_reference: number; point_estimate_transformed: number; upper_confidence_bound: number; lower_confidence_bound: number;
  allowed_regression_transformed: number; pair_count: number; passed: boolean; failure_reason: string | null;
  benefit_passed: boolean | null; primary_benefit_lower_ci: number | null; transformed_minimum_benefit: number | null;
};
export type AdmissionDetails = {
  status: string; reason_codes: string[]; required_pair_count: number; actual_pair_count: number; profile: string;
  protected_metric_results: MetricResult[]; primary_benefit_result: MetricResult | null; production_eligible: boolean;
  family_wise_passed: boolean | null; safety_invariant_results: string[];
};
export type Candidate = {
  id: string; status: string; policy_classification: string | null; generation_rule: string | null; fingerprint: string | null;
  affected_query_shape_ids: string[]; evidence_refs: string[];
  action: { action_type: string; payload: { database: string; collection: string; index_name: string; fields: { field: string; direction: number }[] } } | null;
  rank: number | null; selected_for_evaluation: boolean; selected_for_deployment: boolean;
  evaluation: { status: string; environment_fingerprint: string; pairs: number; primary_metric: string; summary: Record<string, { baseline_median: number | null; candidate_median: number | null; change_ratio: number | null }> } | null;
  admission: { verdict: string; evidence_hash: string; details: AdmissionDetails | null } | null;
  trials: { pair_number: number; arm_order: string; baseline: Metrics; candidate: Metrics }[];
};
export type Shape = { query_shape_id: string; query_shape_hash: string; operation: string; namespace: string; operations: number; operation_share: number; execution_time_share: number | null; execution_time_ms: number | null; protected: boolean; provider: string; shape: unknown };
export type Evidence = {
  shapes: Shape[];
  findings: { finding_id: string; finding_type: string; summary: string; rationale: string; query_shape_ids: string[]; evidence_refs: string[] }[];
  candidates: Candidate[];
  ai_invocations: { stage: string; provider: string; model: string; status: string; latency_ms: number | null; safe_error_code: string | null; attempt: number; created_at: string; rationale: string | null }[];
  ledger: { sequence: number; event_type: string; actor: string; action_id: string; evidence_hash: string; previous_hash: string; entry_hash: string; forward_action: Record<string, unknown>; created_at: string }[];
  events: { event_type: string; created_at: string; payload: Record<string, unknown> }[];
  job: { id: string; status: string; attempts: number; last_error_code: string | null; available_at: string | null; failed_at: string | null; heartbeat_at: string | null } | null;
  approval_decision: { decision: string; reason: string | null; decided_by_user_id: string; decided_at: string } | null;
};
export type Artifact = Record<string, unknown> | null;
export type RunDetail = { run: RunSummary; artifacts: Record<string, Artifact | Record<string, unknown>[]>; evidence?: Evidence };

export type InboxItem = {
  id: string; run_id: string; target_id: string; target_name: string; status: string; run_status: string; deployment_mode: string;
  requested_by_user_id: string; created_at: string; expires_at: string; evidence_hash: string;
  action: Candidate["action"]; can_decide: boolean;
};

export type LedgerRow = { entry_id: string; sequence: number; target_id: string; target_name?: string; run_id: string | null; event_type: string; actor: string; entry_hash: string; previous_hash: string; evidence_hash: string; created_at: string; forward_action: Record<string, unknown> };

// ---------- Calls ----------

export const api = {
  me: () => request("/auth/me") as Promise<{ user_id: string; role: Role; email?: string }>,
  overview: () => request("/api/v1/overview") as Promise<Overview>,
  listRuns: async (status?: string): Promise<RunSummary[]> => ((await request(`/api/v1/runs?limit=100${status ? `&status=${encodeURIComponent(status)}` : ""}`)) as { records: RunSummary[] }).records,
  getRun: (runId: string) => request(`/api/v1/runs/${encodeURIComponent(runId)}`) as Promise<RunDetail>,
  createRun: (input: { target_id: string; deployment_mode: string; primary_metric_key: string }) => request("/api/v1/runs", { method: "POST", body: JSON.stringify(input) }) as Promise<RunSummary>,
  resumeRun: (runId: string) => request(`/api/v1/runs/${encodeURIComponent(runId)}/resume`, { method: "POST" }),
  listTargets: async (): Promise<TargetRecord[]> => ((await request("/api/v1/targets?limit=100")) as { records: TargetRecord[] }).records,
  getTarget: (id: string) => request(`/api/v1/targets/${encodeURIComponent(id)}`) as Promise<TargetDetail>,
  createTarget: (input: { name: string; connection_label: string; deployment_mode: string }) => request("/api/v1/targets", { method: "POST", body: JSON.stringify(input) }) as Promise<TargetRecord>,
  updateTarget: (id: string, input: { name?: string; connection_label?: string; deployment_mode?: string }) => request(`/api/v1/targets/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(input) }) as Promise<TargetRecord>,
  archiveTarget: (id: string) => request(`/api/v1/targets/${encodeURIComponent(id)}/archive`, { method: "POST" }) as Promise<TargetRecord>,
  setMapping: (id: string, evaluationTargetId: string) => request(`/api/v1/targets/${encodeURIComponent(id)}/evaluation-mapping`, { method: "POST", body: JSON.stringify({ evaluation_target_id: evaluationTargetId }) }),
  setAllowlist: (id: string, namespaceId: string, allowlisted: boolean) => request(`/api/v1/targets/${encodeURIComponent(id)}/namespaces/${encodeURIComponent(namespaceId)}/allowlist`, { method: "POST", body: JSON.stringify({ allowlisted }) }),
  connections: () => request("/api/v1/connections") as Promise<{ worker_state: string; connections: WorkerStatus["connections"] }>,
  inbox: () => request("/api/v1/approvals/inbox") as Promise<{ records: InboxItem[]; pending: number }>,
  ledger: async (): Promise<LedgerRow[]> => ((await request("/api/v1/ledger?limit=100")) as { records: LedgerRow[] }).records,
  verifyLedger: () => request("/api/v1/ledger/verify") as Promise<{ verified: boolean; targets: { target_id: string; target_name: string; entries: number; verified: boolean }[] }>,
};

export async function decideApproval(id: string, approve: boolean, reason?: string): Promise<void> {
  await request(`/api/v1/approvals/${encodeURIComponent(id)}/${approve ? "approve" : "reject"}`, { method: "POST", body: JSON.stringify({ reason: reason || undefined }) });
}

export async function login(email: string, password: string): Promise<void> {
  const response = await fetch(`${API_BASE}/auth/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password }) });
  if (!response.ok) throw new ApiError(response.status, response.status === 401 ? "That email and password combination was not accepted." : `Sign-in failed (${response.status}).`);
  storeTokens(await response.json() as { access_token: string; refresh_token: string });
}

export async function logout(): Promise<void> {
  const { refreshToken } = readSession();
  if (refreshToken) {
    try { await fetch(`${API_BASE}/auth/logout`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh_token: refreshToken }) }); } catch { /* best effort */ }
  }
  clearSession();
}
