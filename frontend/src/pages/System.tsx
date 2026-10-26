import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { dateTime, relativeTime } from "../lib/format";
import { Empty, ErrorNote, Loading, Page, Panel } from "../ui/primitives";

const ROLE_TEXT: Record<string, string> = { observer: "read query data", executor: "add and remove indexes", sandbox: "sandbox copy (testing only)" };

export default function System() {
  const overview = useQuery({ queryKey: ["overview"], queryFn: api.overview, refetchInterval: 10_000 });
  if (overview.isPending) return <Page title="System"><Loading what="system status" /></Page>;
  if (overview.isError) return <Page title="System"><ErrorNote error={overview.error} /></Page>;
  const worker = overview.data.worker;
  const online = worker.state === "ONLINE";
  return (
    <Page title="System" subtitle="Health of the background worker that does the actual work. Database passwords stay with the worker and are never shown or sent here.">
      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="Worker" aside={<span className={online ? "tone-ok" : "tone-bad"}>{online ? "Online" : worker.state === "NEVER_SEEN" ? "Never seen" : "Not reporting"}</span>}>
          {worker.state === "NEVER_SEEN" ? <Empty title="No heartbeat yet">Start the worker (<code>docker compose --profile light up -d worker</code>).</Empty> : (
            <dl className="kv">
              <dt>Last heartbeat</dt><dd>{relativeTime(worker.heartbeat_at)}</dd>
              <dt>Started</dt><dd>{dateTime(worker.started_at)}</dd>
              <dt>Can run</dt><dd>{worker.orchestration === "PRODUCTION" ? "Yes, all steps are set up" : "No: it has no database connections, so runs stop safely"}</dd>
              <dt>Checks databases</dt><dd>{worker.telemetry_interval_seconds ? `every ${worker.telemetry_interval_seconds} seconds` : "—"}</dd>
              <dt>Worker ID</dt><dd className="hash">{worker.worker_id}</dd>
            </dl>
          )}
        </Panel>
        <Panel title="AI">
          {worker.ai ? (
            <dl className="kv">
              <dt>AI model</dt><dd>{worker.ai.provider === "deterministic" ? "None: fixed rules only" : worker.ai.provider}</dd>
              {worker.ai.chat_model && <><dt>Chat model</dt><dd><code>{worker.ai.chat_model}</code></dd></>}
              {worker.ai.embedding_model && <><dt>Embeddings</dt><dd><code>{worker.ai.embedding_model}</code></dd></>}
            </dl>
          ) : <p className="text-sm muted">Unknown until the worker reports.</p>}
          <p className="mt-3 text-sm faint">The AI only explains slow queries and suggests which index to try first. Whether a change is accepted is decided by statistics and by people.</p>
        </Panel>
      </div>
      <Panel title="Database connections" aside="Names you pick when adding a target" flush>
        {worker.connections.length ? (
          <table className="table">
            <thead><tr><th>Connection</th><th>Database</th><th>Allowed to</th><th>Server</th></tr></thead>
            <tbody>
              {worker.connections.map((connection) => (
                <tr key={connection.label}>
                  <td><code>{connection.label}</code></td>
                  <td><code>{connection.database}</code></td>
                  <td className="text-sm">{connection.roles.map((role) => ROLE_TEXT[role] ?? role).join(", ")}</td>
                  <td className="text-sm muted">{connection.hosts.join(", ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <div className="panel-body"><Empty title="No connections configured">Mount a JSON connection file at <code>TARGET_CONNECTIONS_FILE</code> on the worker. See <code>infra/dev/target_connections.json</code> for the format.</Empty></div>}
      </Panel>
    </Page>
  );
}
