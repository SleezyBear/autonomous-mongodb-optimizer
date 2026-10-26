import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, decideApproval, type Candidate, type Evidence, type MetricResult, type RunDetail as RunDetailData } from "../api/client";
import { dateTime, ms, pct, relativeTime, shortId } from "../lib/format";
import { explainAdmission, explainBlocker, railStates, runOutcome, TERMINAL } from "../lib/lifecycle";
import { actorName, boundWords, describeShape, isStalled, ledgerEvent, speedChange, stepName, telemetrySource } from "../lib/present";
import PipelineRail from "../ui/PipelineRail";
import { Countdown, Empty, ErrorNote, Loading, Page, Panel, Pill, RunStatus } from "../ui/primitives";
import TrialChart from "../ui/TrialChart";

type Record_ = Record<string, unknown>;
const VERDICT_TEXT: Record<string, string> = {
  ADMITTED: "Passed",
  REJECTED_SAFETY_INVARIANT: "Rejected: results differed",
  REJECTED_REGRESSION: "Rejected: might slow something down",
  REJECTED_NO_MEANINGFUL_BENEFIT: "Rejected: not faster enough",
  INCONCLUSIVE_NOISE: "Inconclusive: too noisy",
  INCONCLUSIVE_ENVIRONMENT: "Inconclusive: sandbox changed",
  INCONCLUSIVE_MISSING_METRIC: "Inconclusive: missing data",
};
const METRIC_NAMES: Record<string, string> = {
  p50_latency_ms: "p50 latency", p95_latency_ms: "p95 latency", p99_latency_ms: "p99 latency",
  write_p95_latency_ms: "Write p95 latency", throughput_ops_per_second: "Throughput",
};

function indexWords(candidate: Candidate | undefined): string {
  if (!candidate?.action) return "an index";
  const fields = candidate.action.payload.fields.map((field) => `${field.field}${field.direction === -1 ? " (descending)" : ""}`).join(", ");
  return `an index on ${candidate.action.payload.collection} (${fields})`;
}

function primaryResult(candidate: Candidate | undefined) {
  const evaluation = candidate?.evaluation;
  if (!evaluation) return null;
  const metric = evaluation.primary_metric;
  const value = evaluation.summary[metric];
  if (!value) return null;
  return { metric, name: METRIC_NAMES[metric] ?? metric, before: value.baseline_median, after: value.candidate_median, pairs: evaluation.pairs };
}

type MonitoringPayload = { status: string; baseline_windows: { p99_latency_ms: number }[]; observed_windows: { p99_latency_ms: number; error_count: number }[]; rollback_required: boolean };
function monitoringOf(evidence: Evidence | undefined) {
  const event = evidence ? [...evidence.events].reverse().find((item) => item.event_type === "DURABLE_POST_DEPLOYMENT_MONITORING") : undefined;
  if (!event) return null;
  const payload = event.payload as MonitoringPayload;
  const baseline = payload.baseline_windows.reduce((sum, window) => sum + window.p99_latency_ms, 0) / Math.max(payload.baseline_windows.length, 1);
  const observed = payload.observed_windows.map((window) => window.p99_latency_ms);
  const worst = observed.length ? Math.max(...observed) : null;
  return { at: event.created_at, payload, baseline, observed, worst };
}

export default function RunDetail() {
  const { id = "" } = useParams();
  const query = useQuery({
    queryKey: ["run", id],
    queryFn: () => api.getRun(id),
    refetchInterval: (state) => state.state.data && TERMINAL.has(state.state.data.run.status) ? false : 3_000,
  });
  if (query.isPending) return <Page title="Run"><Loading what="run" /></Page>;
  if (query.isError) return <Page title="Run"><ErrorNote error={query.error} /></Page>;
  return <RunView detail={query.data} />;
}

function RunView({ detail }: { detail: RunDetailData }) {
  const client = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me, staleTime: 60_000 });
  const [expert, setExpert] = useState(false);
  const run = detail.run;
  const evidence = detail.evidence;
  const artifacts = detail.artifacts as Record<string, Record_ | null>;
  const authority = artifacts.authority;
  const approval = artifacts.approval;
  const deployment = artifacts.deployment;
  const rollback = artifacts.rollback;
  const stalled = isStalled(run);
  const retrying = !TERMINAL.has(run.status) && run.job_status === "PENDING" && Boolean(run.last_error_code);
  const automatic = authority?.authority_type === "AUTONOMOUS";
  const states = railStates(run, automatic);
  const refresh = () => client.invalidateQueries({ queryKey: ["run", run.run_id] });
  const resume = useMutation({ mutationFn: () => api.resumeRun(run.run_id), onSuccess: refresh });
  const canOperate = me.data?.role === "OPERATOR" || me.data?.role === "ADMIN";
  const showDeployment = Boolean(deployment) || ["DEPLOYING", "DEPLOYED", "MONITORING", "ROLLED_BACK", "ROLLBACK_BLOCKED"].includes(run.status) || run.completion_reason === "DEPLOYMENT_SUCCEEDED";
  const showRollback = Boolean(rollback) || run.status === "ROLLED_BACK" || run.status === "ROLLBACK_BLOCKED";
  const selected = evidence?.candidates.find((item) => item.selected_for_deployment);

  return (
    <Page
      title={<>Run on {run.target_name ? <Link to={`/targets/${run.target_id}`}>{run.target_name}</Link> : "a deleted target"}</>}
      subtitle={<>{run.deployment_mode === "FULL_AUTONOMOUS" ? "Full autonomous" : "Approval controlled"}, improving {METRIC_NAMES[run.primary_metric_key ?? ""] ?? "latency"}. Started {relativeTime(run.created_at)}. <span className="faint">Run {shortId(run.run_id)}</span></>}
      actions={<button className="btn" type="button" aria-pressed={expert} onClick={() => setExpert((value) => !value)}>{expert ? "Hide technical details" : "Technical details"}</button>}
    >
      <section className="panel p-5" aria-label="Run state">
        <div className="mb-5 flex flex-wrap items-center gap-3">
          <RunStatus label="Run status" status={run.status} reason={run.completion_reason} stalled={stalled} />
          <p className="text-[0.95rem]">{stalled ? `Stopped at ${stepName(run.status)}.` : runOutcome(run)}</p>
        </div>
        <PipelineRail states={states} />
        <p className="mt-4 flex flex-wrap gap-x-5 gap-y-1 text-xs faint">
          <span><span className="mr-1 inline-block h-2 w-2 rounded-full bg-[var(--ai)] align-middle" aria-hidden />AI gives advice at this step</span>
          <span><span className="mr-1 inline-block h-2 w-2 rotate-45 border border-[var(--muted)] align-middle" aria-hidden />Decision point</span>
        </p>
      </section>

      {stalled && (
        <div className="callout callout-bad" role="alert">
          <div className="flex-1">
            <p className="font-medium tone-bad">The worker could not continue, so it stopped at {stepName(run.status)}.</p>
            <p className="mt-1 text-sm muted">{explainBlocker(run.last_error_code)} It tried {run.job_attempts ?? "several"} times. Nothing was changed on the database.</p>
            {run.last_error_code && <p className="mt-1 text-xs faint mono">{run.last_error_code}</p>}
          </div>
          {canOperate && (
            <div className="flex flex-wrap gap-2">
              <Link className="btn" to={`/targets/${run.target_id}`}>Fix target</Link>
              <button className="btn" disabled={resume.isPending} onClick={() => resume.mutate()} type="button">{resume.isPending ? "Resuming…" : "Resume run"}</button>
            </div>
          )}
        </div>
      )}
      {resume.isError && <ErrorNote error={resume.error} />}
      {retrying && (
        <div className="callout callout-warn" role="status">
          <p className="text-sm"><span className="font-medium tone-warn">Retrying {stepName(run.status)}.</span> <span className="muted">{explainBlocker(run.last_error_code)}</span></p>
        </div>
      )}

      {run.status === "APPROVAL_PENDING" && approval && typeof approval.id === "string" && (
        <ApprovalPanel approval={approval} authority={authority} candidate={selected} requestedByMe={me.data?.user_id === approval.requested_by_user_id} role={me.data?.role} onDecided={refresh} />
      )}

      {evidence && (TERMINAL.has(run.status) || ["APPROVED", "DEPLOYING", "DEPLOYED", "MONITORING"].includes(run.status)) && (
        <StorySection run={run} evidence={evidence} artifacts={artifacts} automatic={automatic} />
      )}

      {evidence && <WorkloadSection evidence={evidence} snapshot={artifacts.snapshot} />}
      {evidence && <DiagnosisSection evidence={evidence} />}
      {evidence && <CandidatesSection evidence={evidence} primary={run.primary_metric_key ?? "p99_latency_ms"} />}

      {showDeployment && (
        <Panel title="Deployment" aside={deployment ? relativeTime(String(deployment.updated_at ?? deployment.created_at)) : "not started"}>
          {deployment ? <DeploymentBody deployment={deployment} evidence={evidence} candidate={selected} /> : <p className="text-sm muted">The change has not been applied yet.</p>}
        </Panel>
      )}
      {evidence && <MonitoringSection evidence={evidence} />}
      {showRollback && (
        <Panel title="Rollback">
          {rollback ? (
            <p className="text-sm">{rollback.status === "ROLLED_BACK"
              ? "The optimizer removed the index it had added. It first checked that it had created that index itself and that nothing else had changed, so it never touches indexes made by people."
              : <>The optimizer did not remove the index, because the database had changed in a way it could not verify. A person should review it. <span className="faint mono">{String((rollback.rollback_payload as Record_ | undefined)?.reason ?? rollback.status)}</span></>}</p>
          ) : <p className="text-sm muted">No rollback was recorded.</p>}
        </Panel>
      )}
      {evidence && evidence.ai_invocations.length > 0 && <AiSection evidence={evidence} />}
      {expert && <RawEvidence detail={detail} />}
    </Page>
  );
}

/** A plain-language account of the run, top to bottom, for anyone who opens it afterwards. */
function StorySection({ run, evidence, artifacts, automatic }: { run: RunDetailData["run"]; evidence: Evidence; artifacts: Record<string, Record_ | null>; automatic: boolean }) {
  const steps: { tone: string; text: ReactNode }[] = [];
  const summary = ((artifacts.snapshot ?? {}) as Record_).snapshot as Record_ | undefined;
  const top = [...evidence.shapes].sort((a, b) => (b.execution_time_share ?? 0) - (a.execution_time_share ?? 0))[0];
  if (top) steps.push({ tone: "muted", text: <>Looked at {String(summary?.operation_count ?? "recent")} queries. The costliest pattern was <strong>{describeShape({ ...top, shape: top.shape as Record_ })}</strong>.</> });
  const tested = evidence.candidates.filter((candidate) => candidate.evaluation);
  if (!evidence.candidates.length && run.completion_reason === "NO_CANDIDATES") steps.push({ tone: "muted", text: "No index would help these queries (they may already be indexed), so nothing was tested." });
  const best = evidence.candidates.find((candidate) => candidate.selected_for_deployment) ?? tested[0];
  const result = primaryResult(best);
  if (best && result) steps.push({ tone: "muted", text: <>Tested {indexWords(best)} on the sandbox copy: {result.name} went from {ms(result.before)} to {ms(result.after)} (<strong>{speedChange(result.before, result.after)}</strong>) over {result.pairs} paired trials.</> });
  if (best?.admission) {
    const passed = best.admission.verdict === "ADMITTED";
    const reasons = explainAdmission(best.admission.details?.reason_codes ?? []);
    const failed = best.admission.details?.protected_metric_results.find((item) => !item.passed);
    const failedWords = failed ? `${METRIC_NAMES[failed.metric_key] ?? failed.metric_key} ${boundWords("protected", failed.upper_confidence_bound, failed.allowed_regression_transformed)}` : null;
    steps.push({ tone: passed ? "tone-ok" : "tone-warn", text: passed
      ? "It passed every check: at least 5% faster, nothing slower, identical results."
      : failedWords
        ? <>It did not pass the safety checks, so nothing was deployed: <strong>{failedWords}</strong>.{failed?.metric_key.startsWith("write") ? " Faster reads are not worth slower writes, so the change was refused." : ""}</>
        : <>It did not pass the checks, so nothing was deployed. {reasons[0] ?? VERDICT_TEXT[best.admission.verdict] ?? ""}</> });
  }
  const decision = evidence.approval_decision;
  if (decision) steps.push({ tone: decision.decision === "APPROVED" ? "tone-ok" : "tone-warn", text: <>{decision.decision === "APPROVED" ? "Approved" : "Rejected"} by a second person {relativeTime(decision.decided_at)}{decision.reason ? <>: <em>“{decision.reason}”</em></> : "."}</> });
  else if (automatic && artifacts.deployment) steps.push({ tone: "muted", text: "Applied automatically: the run was full autonomous and the change met the stricter rules." });
  if (run.completion_reason === "APPROVAL_EXPIRED") steps.push({ tone: "tone-warn", text: "Nobody approved within 15 minutes, so the approval expired and nothing was deployed." });
  if (artifacts.deployment) steps.push({ tone: "muted", text: <>Added the index to the production database {relativeTime(String(artifacts.deployment.updated_at ?? artifacts.deployment.created_at))}.</> });
  const monitoring = monitoringOf(evidence);
  if (monitoring) {
    steps.push(monitoring.payload.rollback_required
      ? { tone: "tone-bad", text: <>After deployment, production got <strong>{speedChange(monitoring.baseline, monitoring.worst)}</strong> (p99 {ms(monitoring.baseline)} before, {ms(monitoring.worst)} after).</> }
      : { tone: "tone-ok", text: <>Watched production after the change: stable (p99 {ms(monitoring.baseline)} before, {monitoring.observed.map((value) => ms(value)).join(" and ")} after).</> });
  }
  if (run.status === "ROLLED_BACK") steps.push({ tone: "tone-warn", text: "So the optimizer removed the index automatically. Production is back to how it was." });
  if (run.status === "ROLLBACK_BLOCKED") steps.push({ tone: "tone-bad", text: "The automatic undo was blocked because the database changed underneath it. A person needs to review it." });
  if (!steps.length) return null;
  return (
    <Panel title="What happened">
      <ol className="grid gap-2.5">
        {steps.map((step, index) => (
          <li key={index} className="grid grid-cols-[1.5rem_minmax(0,1fr)] gap-2 text-sm">
            <span className="mono faint">{index + 1}.</span>
            <span className={step.tone === "muted" ? "" : step.tone}>{step.text}</span>
          </li>
        ))}
      </ol>
    </Panel>
  );
}

function ApprovalPanel({ approval, authority, candidate, requestedByMe, role, onDecided }: { approval: Record_; authority: Record_ | null; candidate?: Candidate; requestedByMe: boolean; role?: string; onDecided: () => void }) {
  const [reason, setReason] = useState("");
  const decide = useMutation({ mutationFn: ({ approve }: { approve: boolean }) => decideApproval(String(approval.id), approve, reason), onSuccess: onDecided });
  const mayDecide = (role === "APPROVER" || role === "ADMIN") && !requestedByMe;
  const expiresAt = typeof approval.expires_at === "string" ? Date.parse(approval.expires_at) : Number.NaN;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  const expired = Number.isFinite(expiresAt) && now >= expiresAt;
  const targetId = typeof approval.target_id === "string" ? approval.target_id : undefined;
  const result = primaryResult(candidate);
  const details = candidate?.admission?.details;
  const protectedOk = details ? details.protected_metric_results.every((item) => item.passed) : candidate?.admission?.verdict === "ADMITTED";
  const mismatches = candidate?.evaluation?.summary.result_mismatch_count?.candidate_median ?? 0;
  const why = authority?.authority_reason === "AUTONOMY_INELIGIBLE_REQUIRES_APPROVAL" && authority?.deployment_mode === "FULL_AUTONOMOUS"
    ? "This run was full autonomous, but its query data came only from point-in-time samples, so a person must approve."
    : "This database is approval controlled, so a second person must approve every change.";
  const checks: [boolean, string][] = [
    [candidate?.admission?.verdict === "ADMITTED", "At least 5% faster, with 95% confidence"],
    [protectedOk, "Nothing got slower (p95, p99 and writes)"],
    [mismatches === 0, "Returned exactly the same results"],
  ];
  return (
    <section className="panel border-[rgba(240,180,92,0.45)]" aria-labelledby="approval-heading">
      <div className="panel-head"><h2 id="approval-heading">Approval required</h2><span className="text-sm">{typeof approval.expires_at === "string" && <Countdown until={approval.expires_at} />}</span></div>
      <div className="panel-body grid gap-4">
        <div className="grid gap-1">
          <p className="text-[1.05rem] font-medium">Add {indexWords(candidate)} to the production database?</p>
          <p className="text-sm muted">{why}</p>
        </div>
        {result && (
          <div className="grid gap-3 rounded-[var(--radius-sm)] border border-[var(--line-soft)] bg-[var(--ink-0)] p-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
            <div>
              <p className="text-xs uppercase tracking-wide faint">Tested on the sandbox copy</p>
              <p className="mt-1 text-2xl font-semibold tabular-nums">{ms(result.before)} <span className="faint">→</span> {ms(result.after)}</p>
              <p className="text-sm"><span className="tone-ok font-medium">{speedChange(result.before, result.after)}</span> <span className="muted">{result.name}, over {result.pairs} paired trials</span></p>
            </div>
            <ul className="grid content-center gap-1.5 text-sm">
              {checks.map(([ok, text]) => <li key={text} className={ok ? "" : "tone-bad"}><span aria-hidden className={ok ? "tone-ok" : "tone-bad"}>{ok ? "✓" : "✗"}</span> {text}<span className="sr-only">{ok ? ": passed" : ": failed"}</span></li>)}
            </ul>
          </div>
        )}
        <details className="text-sm">
          <summary className="cursor-pointer muted">Exact change and evidence fingerprint</summary>
          <dl className="kv mt-2">
            {candidate?.action && <><dt>Index name</dt><dd><code>{candidate.action.payload.index_name}</code></dd><dt>Collection</dt><dd><code>{candidate.action.payload.database}.{candidate.action.payload.collection}</code></dd></>}
            <dt>Evidence</dt><dd className="hash">{String(approval.evidence_hash)}</dd>
          </dl>
          <p className="mt-2 text-xs faint">The approval only applies to this exact evidence. If anything changes, it becomes invalid.</p>
        </details>
        {expired ? (
          <div className="callout callout-warn" role="status">
            <p className="text-sm">
              <span className="font-medium tone-warn">This approval expired {relativeTime(new Date(expiresAt).toISOString())} and can no longer be applied.</span>{" "}
              <span className="muted">Nothing was deployed. The worker closes the run shortly; {targetId ? <Link to={`/runs/new?target=${targetId}`}>start a new run</Link> : "start a new run"} to test again.</span>
            </p>
          </div>
        ) : mayDecide ? (
          <>
            <label className="field">Note for the record (optional)
              <textarea className="input" maxLength={512} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Why you approved or rejected" />
            </label>
            <div className="flex flex-wrap gap-2">
              <button className="btn btn-ok" onClick={() => decide.mutate({ approve: true })} disabled={decide.isPending} type="button">Approve</button>
              <button className="btn btn-danger" onClick={() => decide.mutate({ approve: false })} disabled={decide.isPending} type="button">Reject</button>
            </div>
          </>
        ) : <p className="text-sm muted">{requestedByMe ? "You started this run, so a different approver must decide (four-eyes rule)." : "Only an approver or administrator can decide."}</p>}
        {decide.isError && <ErrorNote error={decide.error} />}
      </div>
    </section>
  );
}

function WorkloadSection({ evidence, snapshot }: { evidence: Evidence; snapshot: Record_ | null }) {
  if (!snapshot) return null;
  const completeness = (snapshot.completeness ?? {}) as Record_;
  const summary = (snapshot.snapshot ?? {}) as Record_;
  const sources = ((completeness.providers as string[] | undefined) ?? []).map(telemetrySource).join(", ") || "query data";
  return (
    <Panel title="Slow queries looked at" aside={`${String(summary.operation_count ?? "?")} queries, from the ${sources}`} flush>
      <table className="table">
        <thead><tr><th>Query pattern</th><th>Share of queries</th><th className="num">Share of database time</th></tr></thead>
        <tbody>
          {evidence.shapes.map((shape) => (
            <tr key={shape.query_shape_id}>
              <td><code title={`${shape.namespace}, pattern ${shape.query_shape_hash.slice(0, 12)}`}>{describeShape({ ...shape, shape: shape.shape as Record_ })}</code></td>
              <td className="min-w-[160px]"><div className="flex items-center gap-2"><div className="bar w-24"><span style={{ width: `${Math.max(2, shape.operation_share * 100)}%` }} /></div><span className="text-sm tabular-nums">{pct(shape.operation_share)}</span></div></td>
              <td className="num text-sm">{pct(shape.execution_time_share)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {completeness.production_autonomy_eligible === false && <p className="border-t border-[var(--line-soft)] px-4 py-3 text-sm tone-warn">The query data came only from point-in-time samples, so this run cannot deploy without a person approving.</p>}
    </Panel>
  );
}

function aiModel(call: { provider: string; model: string }): string {
  return ["fake", "deterministic"].includes(call.provider) ? "No AI model (fixed rules)" : `${call.provider} / ${call.model}`;
}

function DiagnosisSection({ evidence }: { evidence: Evidence }) {
  const diagnosis = evidence.ai_invocations.find((item) => item.stage === "DIAGNOSIS");
  if (!diagnosis && !evidence.findings.length) return null;
  return (
    <Panel title="AI diagnosis" aside={diagnosis ? <span className="tone-ai">advice only: {aiModel(diagnosis)}</span> : undefined}>
      {evidence.findings.length ? (
        <ul className="grid gap-4">
          {evidence.findings.map((finding) => (
            <li key={finding.finding_id} className="grid gap-1 border-l-2 border-[var(--ai)] pl-3">
              <p className="font-medium">{finding.summary}</p>
              <p className="max-w-[80ch] text-sm muted">{finding.rationale}</p>
              <p className="text-xs faint">{finding.finding_type.toLowerCase().replace(/_/g, " ")}; based on {finding.evidence_refs.length} measurement{finding.evidence_refs.length === 1 ? "" : "s"} from this run</p>
            </li>
          ))}
        </ul>
      ) : <p className="text-sm muted">The AI had nothing to add. The suggested indexes come from fixed rules for slow queries, and every one must still pass the sandbox tests.</p>}
    </Panel>
  );
}

function MetricVerdicts({ details }: { details: NonNullable<NonNullable<Candidate["admission"]>["details"]> }) {
  const rows: (MetricResult & { role: string })[] = [
    ...(details.primary_benefit_result ? [{ ...details.primary_benefit_result, role: "Benefit" }] : []),
    ...details.protected_metric_results.map((result) => ({ ...result, role: "Protected" })),
  ];
  const reasons = explainAdmission(details.reason_codes ?? []);
  const notes = reasons.length > 0 && (
    <div className="px-4 pb-3">
      {reasons.map((text, index) => <p key={index} className="max-w-[80ch] text-sm">{text}</p>)}
      <p className="mt-1 text-xs muted mono">{details.reason_codes.join(", ")}</p>
    </div>
  );
  if (!rows.length) return notes || <p className="px-4 pb-3 text-sm muted">No metric results were recorded.</p>;
  return (
    <>
      {notes}
      <p className="px-4 pb-2 text-xs faint">Each check uses a paired bootstrap: the trials are resampled thousands of times to find a 95% confidence bound, which must clear the limit.</p>
      <table className="table">
        <thead><tr><th>Check</th><th>Metric</th><th>In plain words (95% confidence)</th><th className="num">Trial pairs</th><th className="num">Bound</th><th className="num">Limit</th><th>Result</th></tr></thead>
        <tbody>
          {rows.map((row, index) => {
            const benefit = row.role === "Benefit";
            const passed = benefit ? Boolean(row.benefit_passed) : row.passed;
            return (
              <tr key={`${row.role}-${row.metric_key}-${index}`}>
                <td className="text-sm">{benefit ? "Improves by ≥5%" : "Does not get slower"}</td>
                <td className="text-sm">{METRIC_NAMES[row.metric_key] ?? row.metric_key}</td>
                <td className={`text-sm ${passed ? "" : "tone-bad"}`}>{benefit
                  ? boundWords("benefit", row.primary_benefit_lower_ci ?? row.lower_confidence_bound, row.transformed_minimum_benefit)
                  : boundWords("protected", row.upper_confidence_bound, row.allowed_regression_transformed)}</td>
                <td className="num">{row.pair_count}</td>
                <td className="num mono">{(benefit ? row.primary_benefit_lower_ci ?? row.lower_confidence_bound : row.upper_confidence_bound)?.toFixed(3)}</td>
                <td className="num mono">{benefit ? `≥ ${row.transformed_minimum_benefit?.toFixed(3)}` : `≤ ${row.allowed_regression_transformed?.toFixed(3)}`}</td>
                <td className={`text-sm ${passed ? "tone-ok" : "tone-bad"}`}>{passed ? "Pass" : "Fail"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}

function CandidatesSection({ evidence, primary }: { evidence: Evidence; primary: string }) {
  if (!evidence.candidates.length) return null;
  return (
    <section className="grid gap-3" aria-labelledby="candidates-heading">
      <h2 id="candidates-heading">Indexes tested on the sandbox copy</h2>
      {evidence.candidates.map((candidate) => {
        const verdict = candidate.admission?.verdict;
        const summary = candidate.evaluation?.summary ?? {};
        const words = indexWords(candidate);
        return (
          <article key={candidate.id} className={`panel ${candidate.selected_for_deployment ? "border-[rgba(92,207,160,0.45)]" : ""}`}>
            <div className="panel-head flex-wrap">
              <div className="grid min-w-0 gap-0.5">
                <p className="font-medium">{words.charAt(0).toUpperCase() + words.slice(1)}{candidate.rank && <span className="ml-2 text-sm font-normal faint">suggestion #{candidate.rank}</span>}</p>
                {candidate.action && <code className="text-xs faint">{candidate.action.payload.collection}.createIndex({`{ ${candidate.action.payload.fields.map((field) => `${field.field}: ${field.direction}`).join(", ")} }`})</code>}
              </div>
              {verdict ? <Pill tone={verdict === "ADMITTED" ? "ok" : verdict.startsWith("REJECTED") ? "bad" : "warn"}>{VERDICT_TEXT[verdict] ?? verdict}</Pill>
                : <span className="text-sm muted">{candidate.selected_for_evaluation ? "Waiting for sandbox trials" : "Not tested (lower-ranked)"}</span>}
            </div>
            {candidate.evaluation && (
              <div className="panel-body grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
                <div className="grid min-w-0 content-start gap-4">
                  <div className="overflow-x-auto"><table className="table">
                    <thead><tr><th>Metric</th><th className="num">Without index</th><th className="num">With index</th><th className="num">Change</th></tr></thead>
                    <tbody>
                      {Object.entries(summary).filter(([key]) => key !== "result_mismatch_count").map(([key, value]) => {
                        const better = key === "throughput_ops_per_second" ? (value.change_ratio ?? 0) > 0 : (value.change_ratio ?? 0) < 0;
                        const throughput = key === "throughput_ops_per_second";
                        return (
                          <tr key={key}>
                            <td className="text-sm">{METRIC_NAMES[key] ?? key}{key === primary && <span className="faint"> (goal)</span>}</td>
                            <td className="num mono">{throughput ? `${value.baseline_median?.toFixed(0)}/s` : ms(value.baseline_median)}</td>
                            <td className="num mono">{throughput ? `${value.candidate_median?.toFixed(0)}/s` : ms(value.candidate_median)}</td>
                            <td className={`num text-sm ${Math.abs(value.change_ratio ?? 0) < 0.005 ? "muted" : better ? "tone-ok" : "tone-bad"}`}>{throughput ? `${((value.change_ratio ?? 0) * 100).toFixed(0)}% more` : speedChange(value.baseline_median, value.candidate_median)}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table></div>
                  <p className="text-xs faint">Middle values over {candidate.evaluation.pairs} paired trials. {(summary.result_mismatch_count?.candidate_median ?? 0) > 0 ? <span className="tone-bad">Some trials returned different results.</span> : "Both versions returned identical results in every trial."}</p>
                </div>
                <div className="grid min-w-0 content-start gap-5">
                  <TrialChart trials={candidate.trials} metric={primary in summary ? primary : "p99_latency_ms"} />
                  {"write_p95_latency_ms" in summary && <TrialChart trials={candidate.trials} metric="write_p95_latency_ms" />}
                </div>
              </div>
            )}
            {candidate.admission?.details && (
              <details className="border-t border-[var(--line-soft)]">
                <summary className="cursor-pointer px-4 py-3 text-sm muted">Statistical admission evidence: {candidate.admission.details.actual_pair_count} trial pairs measured, {candidate.admission.details.required_pair_count} needed ({candidate.admission.details.profile.toLowerCase()} profile)</summary>
                <div className="overflow-x-auto pb-2"><MetricVerdicts details={candidate.admission.details} /></div>
              </details>
            )}
          </article>
        );
      })}
    </section>
  );
}

function DeploymentBody({ deployment, evidence, candidate }: { deployment: Record_; evidence?: Evidence; candidate?: Candidate }) {
  return (
    <div className="grid gap-4">
      <p className="text-sm">Added {indexWords(candidate)} to the production database on {dateTime(String(deployment.updated_at ?? deployment.created_at))}.</p>
      {evidence && evidence.ledger.length > 0 && (
        <div>
          <h3 className="mb-2">Recorded in the ledger</h3>
          <ol className="grid gap-2">
            {evidence.ledger.map((entry) => (
              <li key={entry.entry_hash} className="flex flex-wrap items-baseline justify-between gap-2 rounded-[var(--radius-sm)] border border-[var(--line-soft)] px-3 py-2">
                <span className="text-sm"><span className="faint">#{entry.sequence}</span> {ledgerEvent(entry.event_type)} <span className="muted">by {actorName(entry.actor)}</span></span>
                <span className="text-xs faint">{dateTime(entry.created_at)} <span className="mono" title={entry.entry_hash}>{entry.entry_hash.slice(0, 10)}…</span></span>
              </li>
            ))}
          </ol>
        </div>
      )}
      <details className="text-sm">
        <summary className="cursor-pointer muted">Safety key</summary>
        <p className="mt-2 text-xs faint">If the worker restarts mid-deployment, this key stops it adding the index twice.</p>
        <p className="hash mt-1">{String(deployment.idempotency_key)}</p>
      </details>
    </div>
  );
}

function MonitoringSection({ evidence }: { evidence: Evidence }) {
  const monitoring = monitoringOf(evidence);
  if (!monitoring) return null;
  const { payload, baseline, observed, worst } = monitoring;
  return (
    <Panel title="After deployment" aside={relativeTime(monitoring.at)}>
      <div className="grid gap-3">
        <p className={payload.rollback_required ? "tone-bad" : "tone-ok"}>{payload.rollback_required ? "Regression detected; rollback was triggered." : payload.status === "WORKLOAD_SHIFT_DETECTED" ? "Stable, but the traffic pattern changed, so no firm conclusion is drawn." : "Stable after deployment."}</p>
        <dl className="kv">
          <dt>p99 before</dt><dd>{ms(baseline)} <span className="faint">(average of {payload.baseline_windows.length} checks before the change)</span></dd>
          <dt>p99 after</dt><dd>{observed.map((value) => ms(value)).join(", ")}{worst ? <span className={payload.rollback_required ? "tone-bad" : "tone-ok"}> ({speedChange(baseline, worst)})</span> : null}</dd>
        </dl>
        <p className="text-xs faint">The optimizer keeps timing the real queries after a change. It undoes the change if they become 3× slower at once, or 20% slower over several checks, or if errors appear.</p>
      </div>
    </Panel>
  );
}

function AiSection({ evidence }: { evidence: Evidence }) {
  return (
    <Panel title="AI calls" aside="The AI explains and ranks; it never decides or makes changes" flush>
      <table className="table">
        <thead><tr><th>Step</th><th>Model</th><th>Result</th><th className="num">Time</th><th>Its explanation</th></tr></thead>
        <tbody>
          {evidence.ai_invocations.map((call, index) => (
            <tr key={`${call.stage}-${index}`}>
              <td className="text-sm">{call.stage === "DIAGNOSIS" ? "Diagnose" : call.stage === "RANKING" ? "Rank" : call.stage.toLowerCase()}</td>
              <td className="text-sm">{aiModel(call)}</td>
              <td className={`text-sm ${call.status === "FAILED" ? "tone-bad" : "tone-ok"}`}>{call.status === "FAILED" ? "Failed (ignored safely)" : "Done"}{call.safe_error_code ? <code> {call.safe_error_code}</code> : null}</td>
              <td className="num mono">{call.latency_ms === null ? "—" : ms(call.latency_ms)}</td>
              <td className="max-w-[420px] text-sm muted">{call.rationale ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}

function RawEvidence({ detail }: { detail: RunDetailData }) {
  const keys = Object.keys(detail.artifacts);
  return (
    <Panel title="Technical details" aside="Every stored record for this run, exactly as saved">
      {keys.length ? (
        <div className="grid gap-2">
          {keys.map((key) => (
            <details key={key} className="rounded-[var(--radius-sm)] border border-[var(--line-soft)]">
              <summary className="cursor-pointer px-3 py-2 text-sm"><code>{key}</code>{detail.artifacts[key] === null && <span className="faint"> not recorded</span>}</summary>
              <pre className="m-0 max-h-[420px] overflow-auto border-t border-[var(--line-soft)] p-3 text-xs leading-relaxed text-[var(--muted)]">{JSON.stringify(detail.artifacts[key], null, 2)}</pre>
            </details>
          ))}
          <details className="rounded-[var(--radius-sm)] border border-[var(--line-soft)]">
            <summary className="cursor-pointer px-3 py-2 text-sm"><code>worker events</code></summary>
            <pre className="m-0 max-h-[420px] overflow-auto border-t border-[var(--line-soft)] p-3 text-xs leading-relaxed text-[var(--muted)]">{JSON.stringify(detail.evidence?.events ?? [], null, 2)}</pre>
          </details>
        </div>
      ) : <Empty title="No records" />}
    </Panel>
  );
}
