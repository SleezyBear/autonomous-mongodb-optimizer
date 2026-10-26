import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { dateTime, shortId } from "../lib/format";
import { actorName, ledgerEvent } from "../lib/present";
import { Empty, ErrorNote, Loading, Page, Panel } from "../ui/primitives";

export default function Ledger() {
  const ledger = useQuery({ queryKey: ["ledger"], queryFn: api.ledger });
  const verification = useQuery({ queryKey: ["ledger-verify"], queryFn: api.verifyLedger });
  return (
    <Page title="Ledger" subtitle="A permanent record of every change the optimizer made to a production database, and every undo.">
      <Panel title="Tamper check" aside="Re-checked every time you open this page">
        <p className="mb-3 max-w-[80ch] text-sm muted">Each entry stores a fingerprint (hash) of itself and of the entry before it, like links in a chain. Editing or deleting any old entry would break every link after it, and this check would fail.</p>
        {verification.isPending ? <Loading what="verification" /> : verification.isError ? <ErrorNote error={verification.error} /> : verification.data.targets.length ? (
          <ul className="grid gap-2">
            {verification.data.targets.map((target) => (
              <li key={target.target_id} className="flex flex-wrap items-center justify-between gap-2">
                <span>{target.target_name} <span className="text-sm muted">({target.entries} {target.entries === 1 ? "entry" : "entries"})</span></span>
                <span className={target.verified ? "tone-ok" : "tone-bad font-medium"}>{target.verified ? "Chain verified" : "Chain broken: investigate immediately"}</span>
              </li>
            ))}
          </ul>
        ) : <p className="text-sm muted">Nothing to check yet: no production changes have been recorded.</p>}
      </Panel>
      {ledger.isPending ? <Loading what="ledger" /> : ledger.isError ? <ErrorNote error={ledger.error} /> : (
        <Panel title="Entries" aside="Newest first" flush>
          {ledger.data.length ? (
            <table className="table">
              <thead><tr><th>When</th><th>Database</th><th>What happened</th><th>Done by</th><th>Run</th><th>Fingerprint</th></tr></thead>
              <tbody>
                {ledger.data.map((entry) => (
                  <tr key={entry.entry_id}>
                    <td className="text-sm whitespace-nowrap">{dateTime(entry.created_at)}</td>
                    <td>{entry.target_name ?? shortId(entry.target_id)} <span className="faint">#{entry.sequence}</span></td>
                    <td className={`text-sm ${entry.event_type.startsWith("ROLLBACK") ? "tone-warn" : ""}`}>{ledgerEvent(entry.event_type)}</td>
                    <td className="text-sm muted">{actorName(entry.actor)}</td>
                    <td>{entry.run_id ? <Link to={`/runs/${entry.run_id}`} className="text-sm">Open run</Link> : "—"}</td>
                    <td className="hash" title={entry.entry_hash}>{entry.entry_hash.slice(0, 12)}…</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="panel-body"><Empty title="No changes yet">Entries appear when the optimizer adds or removes an index on a production database.</Empty></div>}
        </Panel>
      )}
    </Page>
  );
}
