import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { relativeTime } from "../lib/format";
import { Countdown, Empty, ErrorNote, Loading, Page, Panel, Pill } from "../ui/primitives";

export default function Approvals() {
  const inbox = useQuery({ queryKey: ["inbox"], queryFn: api.inbox, refetchInterval: 10_000 });
  if (inbox.isPending) return <Page title="Approvals"><Loading what="approvals" /></Page>;
  if (inbox.isError) return <Page title="Approvals"><ErrorNote error={inbox.error} /></Page>;
  const pending = inbox.data.records.filter((item) => item.status === "PENDING");
  const history = inbox.data.records.filter((item) => item.status !== "PENDING");
  return (
    <Page title="Approvals" subtitle="Changes that passed their sandbox tests wait here for a second person to approve. Each request expires after 15 minutes, and the person who started a run can never approve it.">
      <Panel title="Waiting for a decision" aside={`${pending.length} pending`}>
        {pending.length ? (
          <ul className="grid divide-y divide-[var(--line-soft)]">
            {pending.map((item) => (
              <li key={item.id} className="flex flex-wrap items-center justify-between gap-3 py-3 first:pt-0 last:pb-0">
                <div className="min-w-0">
                  <p className="font-medium">{item.target_name}: add an index on {item.action ? <>{item.action.payload.collection} (<code>{item.action.payload.fields.map((field) => field.field).join(", ")}</code>)</> : "a collection"}</p>
                  <p className="text-sm muted">Requested {relativeTime(item.created_at)}{item.deployment_mode === "FULL_AUTONOMOUS" ? ", needs a person because autonomy rules were not met" : ""}. <Countdown until={item.expires_at} /></p>
                  {!item.can_decide && <p className="text-xs faint">You started this run, so someone else must approve it.</p>}
                </div>
                <Link className={`btn ${item.can_decide ? "btn-primary" : ""}`} to={`/runs/${item.run_id}`}>{item.can_decide ? "Review and decide" : "View run"}</Link>
              </li>
            ))}
          </ul>
        ) : <Empty title="Nothing to approve">When a run finds a change that passes its tests, it waits here for approval.</Empty>}
      </Panel>
      {history.length > 0 && (
        <Panel title="Past decisions" flush>
          <table className="table">
            <thead><tr><th>Database</th><th>Index on</th><th>Decision</th><th className="num">Requested</th></tr></thead>
            <tbody>
              {history.map((item) => (
                <tr key={item.id}>
                  <td><Link to={`/runs/${item.run_id}`}>{item.target_name}</Link></td>
                  <td>{item.action ? <span className="text-sm">{item.action.payload.collection} (<code>{item.action.payload.fields.map((field) => field.field).join(", ")}</code>)</span> : "—"}</td>
                  <td><Pill tone={item.status === "APPROVED" ? "ok" : item.status === "REJECTED" ? "muted" : "warn"}>{item.status.charAt(0) + item.status.slice(1).toLowerCase()}</Pill></td>
                  <td className="num text-sm muted">{relativeTime(item.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </Page>
  );
}
