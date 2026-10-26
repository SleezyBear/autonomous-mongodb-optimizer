import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { relativeTime } from "../lib/format";
import { explainBlocker, runOutcome } from "../lib/lifecycle";
import { connectionKind } from "../lib/connections";
import { stepName } from "../lib/present";
import { Empty, ErrorNote, Loading, Page, Panel, RunStatus, Stat } from "../ui/primitives";

export default function Overview() {
  const overview = useQuery({ queryKey: ["overview"], queryFn: api.overview, refetchInterval: 10_000 });
  const connections = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  if (overview.isPending) return <Page title="Overview"><Loading what="overview" /></Page>;
  if (overview.isError) return <Page title="Overview"><ErrorNote error={overview.error} /></Page>;
  const data = overview.data;
  const worker = data.worker;
  const canOperate = data.principal.role === "OPERATOR" || data.principal.role === "ADMIN";
  const stalled = data.stalled_runs.filter((run) => run.job_status === "FAILED");
  const retrying = data.stalled_runs.filter((run) => run.job_status !== "FAILED");
  const waiting = data.runs_by_status?.APPROVAL_PENDING ?? 0;
  const inProgress = Math.max(0, data.active_runs - waiting - stalled.length);
  const sandboxLabels = new Set((connections.data?.connections ?? []).filter((item) => connectionKind(item.roles) === "SANDBOX").map((item) => item.label));
  const production = data.targets.filter((target) => !sandboxLabels.has(target.connection_label));
  const targetName = (id: string) => data.targets.find((target) => target.id === id)?.name ?? "A target";
  return (
    <Page
      title="Overview"
      subtitle="What the optimizer is doing now, and what needs you."
      actions={canOperate && <Link className="btn btn-primary" to="/runs/new">Start a run</Link>}
    >
      {worker.state !== "ONLINE" && (
        <div className="callout callout-warn" role="status">
          <div>
            <p className="font-medium tone-warn">{worker.state === "NEVER_SEEN" ? "No worker has reported in yet" : `The worker last reported ${relativeTime(worker.heartbeat_at)}`}</p>
            <p className="mt-0.5 text-sm muted">Runs will queue but not progress until the worker is running. See <Link to="/system">System</Link>.</p>
          </div>
        </div>
      )}
      {worker.state === "ONLINE" && worker.orchestration !== "PRODUCTION" && (
        <div className="callout callout-warn" role="status">
          <p className="text-sm"><span className="font-medium tone-warn">The worker has no target connections.</span> <span className="muted">Mount a connection file (TARGET_CONNECTIONS_FILE) so it can evaluate and deploy.</span></p>
        </div>
      )}

      <section className="panel grid grid-cols-2 gap-5 p-5 sm:grid-cols-4" aria-label="Summary">
        <Stat value={inProgress} label="Runs in progress" />
        <Stat value={data.pending_approvals} label="Waiting for approval" tone={data.pending_approvals ? "warn" : undefined} />
        <Stat value={stalled.length} label="Stalled runs" tone={stalled.length ? "bad" : undefined} />
        <Stat value={data.deployments_last_7_days} label="Indexes deployed, 7 days" />
      </section>

      {(stalled.length > 0 || retrying.length > 0) && (
        <Panel title="Needs attention" aside="Runs the worker could not finish">
          <ul className="grid divide-y divide-[var(--line-soft)]">
            {[...stalled, ...retrying].map((run) => {
              const failed = run.job_status === "FAILED";
              return (
                <li className="flex flex-wrap items-start justify-between gap-3 py-3 first:pt-0 last:pb-0" key={run.run_id}>
                  <div className="min-w-0 max-w-[70ch]">
                    <p className="font-medium">
                      {targetName(run.target_id)}: <span className={failed ? "tone-bad" : "tone-warn"}>{failed ? `stopped at ${stepName(run.status)}` : `retrying ${stepName(run.status)}`}</span>
                    </p>
                    <p className="mt-0.5 text-sm muted">{explainBlocker(run.last_error_code)}{failed ? " Nothing was changed on the database." : ""}</p>
                    {run.last_error_code && <p className="mt-1 text-xs faint mono">{run.last_error_code}</p>}
                  </div>
                  <div className="flex gap-2">
                    <Link className="btn" to={`/targets/${run.target_id}`}>Fix target</Link>
                    <Link className="btn" to={`/runs/${run.run_id}`}>Open run</Link>
                  </div>
                </li>
              );
            })}
          </ul>
        </Panel>
      )}

      <div className="grid gap-5 lg:grid-cols-[1fr_380px]">
        <Panel title="Recent runs" aside={<Link to="/runs">All runs</Link>} flush>
          {data.recent_runs.length ? (
            <table className="table">
              <thead><tr><th>Database</th><th>Status</th><th>What happened</th><th className="num">Updated</th></tr></thead>
              <tbody>
                {data.recent_runs.map((run) => {
                  const stall = stalled.find((item) => item.run_id === run.run_id);
                  return (
                    <tr key={run.run_id}>
                      <td><Link to={`/runs/${run.run_id}`}>{run.target_name}</Link></td>
                      <td><RunStatus status={run.status} reason={run.completion_reason} stalled={Boolean(stall)} /></td>
                      <td className={`max-w-[320px] text-sm ${stall ? "tone-bad" : "muted"}`}>{stall ? explainBlocker(stall.last_error_code) : runOutcome(run)}</td>
                      <td className="num text-sm muted">{relativeTime(run.updated_at ?? run.created_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : <div className="panel-body"><Empty title="No runs yet">Set up a target, then start a run to have the optimizer analyse its workload.</Empty></div>}
        </Panel>

        <Panel title="Production databases" aside={<Link to="/targets">Manage</Link>}>
          {production.length ? (
            <ul className="grid gap-3">
              {production.map((target) => {
                const missing = target.checks.filter((check) => !check.ok);
                return (
                  <li key={target.id} className="grid gap-1">
                    <div className="flex items-center justify-between gap-2">
                      <Link to={`/targets/${target.id}`} className="font-medium">{target.name}</Link>
                      <span className={`text-sm ${target.ready ? "tone-ok" : "tone-warn"}`}>{target.ready ? "Ready" : `${missing.length} to set up`}</span>
                    </div>
                    <p className="text-sm muted">{target.ready ? `Ready to run. Latest query data ${target.telemetry_age_seconds !== null ? `${Math.round(target.telemetry_age_seconds)}s old` : "not collected yet"}.` : `Still needed: ${missing.map((check) => check.label.toLowerCase()).join(", ")}.`}</p>
                  </li>
                );
              })}
            </ul>
          ) : <Empty title="No databases yet"><Link to="/targets">Add the database you want to speed up</Link>, plus a sandbox copy to test on.</Empty>}
        </Panel>
      </div>
    </Page>
  );
}
