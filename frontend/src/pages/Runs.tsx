import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, type RunSummary } from "../api/client";
import { relativeTime } from "../lib/format";
import { explainBlocker, runOutcome, TERMINAL } from "../lib/lifecycle";
import { isStalled, runBadge, stepName } from "../lib/present";
import { Empty, ErrorNote, Loading, Page, Panel, RunStatus } from "../ui/primitives";

/** Filters follow what an operator asks ("what needs me?", "what got deployed?"), not internal statuses. */
const FILTERS: { value: string; label: string; match: (run: RunSummary) => boolean }[] = [
  { value: "", label: "All", match: () => true },
  { value: "approval", label: "Needs approval", match: (run) => run.status === "APPROVAL_PENDING" },
  { value: "running", label: "In progress", match: (run) => !TERMINAL.has(run.status) && run.status !== "APPROVAL_PENDING" && !isStalled(run) },
  { value: "deployed", label: "Deployed", match: (run) => runBadge(run).label === "Deployed" },
  { value: "nochange", label: "No change", match: (run) => runBadge(run).label === "No change" },
  { value: "rolledback", label: "Rolled back", match: (run) => run.status === "ROLLED_BACK" },
  { value: "problems", label: "Problems", match: (run) => isStalled(run) || run.status === "FAILED" || run.status === "ROLLBACK_BLOCKED" },
];
const LEGACY: Record<string, string> = { APPROVAL_PENDING: "approval", ROLLED_BACK: "rolledback", ROLLBACK_BLOCKED: "problems", COMPLETED: "deployed" };

function detail(run: RunSummary): { text: string; tone: string } {
  if (isStalled(run)) return { text: `Stopped at ${stepName(run.status)}. ${explainBlocker(run.last_error_code)}`, tone: "tone-bad" };
  if (run.job_status === "PENDING" && run.last_error_code) return { text: `Retrying ${stepName(run.status)}. ${explainBlocker(run.last_error_code)}`, tone: "tone-warn" };
  if (TERMINAL.has(run.status) || run.status === "APPROVAL_PENDING") return { text: runOutcome(run), tone: "muted" };
  return { text: `At step: ${stepName(run.status)}`, tone: "muted" };
}

export default function Runs() {
  const [params, setParams] = useSearchParams();
  const raw = params.get("filter") ?? params.get("status") ?? "";
  const active = FILTERS.find((filter) => filter.value === (LEGACY[raw] ?? raw)) ?? FILTERS[0];
  const runs = useQuery({ queryKey: ["runs", "all"], queryFn: () => api.listRuns(), refetchInterval: 5_000 });
  const me = useQuery({ queryKey: ["me"], queryFn: api.me, staleTime: 60_000 });
  const canOperate = me.data?.role === "OPERATOR" || me.data?.role === "ADMIN";
  const all = runs.data ?? [];
  const shown = all.filter(active.match);
  return (
    <Page
      title="Runs"
      subtitle="A run looks at a database's slow queries, tests possible indexes on the sandbox copy, and deploys only what passes."
      actions={canOperate && <Link className="btn btn-primary" to="/runs/new">Start a run</Link>}
    >
      <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter runs">
        {FILTERS.map((filter) => {
          const count = all.filter(filter.match).length;
          const selected = filter.value === active.value;
          return (
            <button key={filter.value} type="button" aria-pressed={selected}
              className={`btn h-8 text-sm ${selected ? "border-[var(--accent)] text-[var(--accent)]" : ""}`}
              onClick={() => setParams(filter.value ? { filter: filter.value } : {})}>
              {filter.label}{runs.data && <span className="faint tabular-nums">{count}</span>}
            </button>
          );
        })}
      </div>
      {runs.isPending ? <Loading what="runs" /> : runs.isError ? <ErrorNote error={runs.error} /> : (
        <Panel flush>
          {shown.length ? (
            <table className="table">
              <thead><tr><th>Database</th><th>Status</th><th>What happened</th><th>Mode</th><th className="num">Updated</th></tr></thead>
              <tbody>
                {shown.map((run) => {
                  const info = detail(run);
                  return (
                    <tr key={run.run_id}>
                      <td><Link to={`/runs/${run.run_id}`} className="font-medium">{run.target_name ?? "Unknown target"}</Link></td>
                      <td><RunStatus status={run.status} reason={run.completion_reason} stalled={isStalled(run)} /></td>
                      <td className={`max-w-[420px] text-sm ${info.tone}`}>{info.text}</td>
                      <td className="text-sm muted">{run.deployment_mode === "FULL_AUTONOMOUS" ? "Autonomous" : "Approval"}</td>
                      <td className="num text-sm muted">{relativeTime(run.updated_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <div className="panel-body">
              <Empty title={active.value ? `No runs in "${active.label}"` : "No runs yet"}>
                {canOperate ? <><Link to="/runs/new">Start a run</Link> on a database that is ready.</> : "Runs appear here once an operator starts one."}
              </Empty>
            </div>
          )}
        </Panel>
      )}
    </Page>
  );
}
