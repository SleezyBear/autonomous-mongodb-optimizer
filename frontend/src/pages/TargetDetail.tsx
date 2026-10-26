import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, type TargetDetail as TargetDetailData } from "../api/client";
import { kindOfLabel, type ConnectionInfo } from "../lib/connections";
import { relativeTime } from "../lib/format";
import { runOutcome } from "../lib/lifecycle";
import { telemetrySource, topology } from "../lib/present";
import ConnectionField from "../ui/ConnectionField";
import { CheckRow, Empty, ErrorNote, Loading, Page, Panel, RunStatus } from "../ui/primitives";

function TargetSettings({ data, connections, onSaved }: { data: TargetDetailData; connections: readonly ConnectionInfo[]; onSaved: () => Promise<unknown> }) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const target = data.target;
  const [name, setName] = useState(target.name);
  const [label, setLabel] = useState(target.connection_label);
  const [mode, setMode] = useState(target.deployment_mode);
  const [confirmArchive, setConfirmArchive] = useState(false);
  const save = useMutation({
    mutationFn: () => api.updateTarget(target.id, { name: name.trim(), connection_label: label.trim(), deployment_mode: mode }),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ["targets"] }); await onSaved(); },
  });
  const archive = useMutation({
    mutationFn: () => api.archiveTarget(target.id),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ["targets"] }); navigate("/targets"); },
  });
  const changed = name.trim() !== target.name || label.trim() !== target.connection_label || mode !== target.deployment_mode;
  const sandboxLabel = kindOfLabel(label, connections) === "SANDBOX";
  if (!target.is_active) {
    return <Panel title="Settings" id="settings"><p className="text-sm muted">This target is archived. Its runs and ledger history are kept; it is no longer watched.</p></Panel>;
  }
  return (
    <Panel title="Settings" id="settings" aside={sandboxLabel ? "Fix a wrong name or connection" : "Fix a wrong name, connection or mode"}>
      <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); if (changed) save.mutate(); }}>
        <div className="grid items-start gap-4 sm:grid-cols-3">
          <label className="field">Name<input className="input" required maxLength={128} value={name} onChange={(event) => setName(event.target.value)} /></label>
          <ConnectionField id="edit-connection-label" value={label} onChange={setLabel} connections={connections} />
          {!sandboxLabel && (
            <label className="field">Deployment mode
              <select className="input" value={mode} onChange={(event) => setMode(event.target.value)}>
                <option value="APPROVAL_CONTROLLED">Approval controlled</option>
                <option value="FULL_AUTONOMOUS">Full autonomous</option>
              </select>
            </label>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <button className="btn btn-primary" type="submit" disabled={!changed || save.isPending}>{save.isPending ? "Saving…" : "Save changes"}</button>
          {save.isSuccess && !changed && <span className="text-sm tone-ok" role="status">Saved.</span>}
          {save.isError && <span className="text-sm tone-bad" role="alert">{save.error.message}</span>}
        </div>
      </form>
      <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-[var(--line-soft)] pt-4">
        {confirmArchive ? (
          <>
            <span className="text-sm">Archive <strong>{target.name}</strong>? It stops being watched and its name can be reused. History is kept.</span>
            <button className="btn btn-danger" type="button" onClick={() => archive.mutate()} disabled={archive.isPending}>{archive.isPending ? "Archiving…" : "Yes, archive"}</button>
            <button className="btn" type="button" onClick={() => setConfirmArchive(false)}>Keep it</button>
          </>
        ) : (
          <>
            <button className="btn" type="button" onClick={() => setConfirmArchive(true)}>Archive target</button>
            <span className="text-sm faint">Use this to remove a target you added by mistake.</span>
          </>
        )}
        {archive.isError && <span className="text-sm tone-bad" role="alert">{archive.error.message}</span>}
      </div>
    </Panel>
  );
}

export default function TargetDetail() {
  const { id = "" } = useParams();
  const client = useQueryClient();
  const detail = useQuery({ queryKey: ["target", id], queryFn: () => api.getTarget(id), refetchInterval: 15_000 });
  const targets = useQuery({ queryKey: ["targets"], queryFn: api.listTargets });
  const connectionsQuery = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const connections = connectionsQuery.data?.connections ?? [];
  const me = useQuery({ queryKey: ["me"], queryFn: api.me, staleTime: 60_000 });
  const canOperate = me.data?.role === "OPERATOR" || me.data?.role === "ADMIN";
  const [sandbox, setSandbox] = useState("");
  const refresh = () => client.invalidateQueries({ queryKey: ["target", id] });
  const mapping = useMutation({ mutationFn: (evaluationId: string) => api.setMapping(id, evaluationId), onSuccess: refresh });
  const [optimistic, setOptimistic] = useState<Record<string, boolean>>({});
  const allowlist = useMutation({
    mutationFn: ({ namespace, allowed }: { namespace: string; allowed: boolean }) => api.setAllowlist(id, namespace, allowed),
    onMutate: ({ namespace, allowed }) => setOptimistic((current) => ({ ...current, [namespace]: allowed })),
    onSettled: async (_data, _error, { namespace }) => {
      await refresh();
      setOptimistic(({ [namespace]: _done, ...rest }) => rest);
    },
  });

  if (detail.isPending) return <Page title="Target"><Loading what="target" /></Page>;
  if (detail.isError) return <Page title="Target"><ErrorNote error={detail.error} /></Page>;
  const data = detail.data;
  const sandboxCandidates = (targets.data ?? []).filter((target) => target.is_active && target.id !== id && target.connection_label !== data.target.connection_label
    && (connections.length === 0 || kindOfLabel(target.connection_label, connections) === "SANDBOX"));
  const sandboxLabels = connections.filter((item) => kindOfLabel(item.label, connections) === "SANDBOX").map((item) => item.label);
  const capability = data.capability?.capabilities ?? {};
  const isSandbox = data.role === "SANDBOX";

  const mappedId = data.evaluation_mapping?.evaluation_target_id ?? "";
  const chosenSandbox = sandbox || mappedId;
  const checksLeft = data.checks.filter((check) => !check.ok).length;
  // The repo's MongoDB init script creates this collection to test permissions; it holds no workload.
  const namespaces = data.namespaces.filter((namespace) => !namespace.name.endsWith(".permission_probe"));
  const modeWords = data.target.deployment_mode === "FULL_AUTONOMOUS" ? "full autonomous" : "approval controlled";

  return (
    <Page
      title={data.target.name}
      subtitle={<>{isSandbox ? "Sandbox copy" : "Production database"} on connection <code>{data.target.connection_label}</code>{data.connection ? <>, database <code>{data.connection.database}</code></> : null}{isSandbox ? null : <>, {modeWords}</>}</>}
      actions={!isSandbox && canOperate && data.target.is_active && (data.ready
        ? <Link className="btn btn-primary" to={`/runs/new?target=${id}`}>Start a run</Link>
        : <span className="btn" aria-disabled="true" title="Complete the setup steps first">{checksLeft} setup step{checksLeft === 1 ? "" : "s"} left</span>)}
    >
      {!data.target.is_active && <div className="callout callout-info"><p className="text-sm">This target is archived. Its history is kept; it is no longer watched.</p></div>}
      {isSandbox ? (
        <div className="callout callout-info">
          <div className="grid gap-1 text-sm">
            <p><strong>This is a sandbox copy.</strong> Before anything changes on a production database, the optimizer copies its data here and tests the change. This copy is never watched for slow queries.</p>
            {data.sandbox_for && data.sandbox_for.length > 0
              ? <p className="muted">Used for testing changes to {data.sandbox_for.map((item, index) => <span key={item.id}>{index > 0 ? ", " : ""}<Link to={`/targets/${item.id}`}>{item.name}</Link></span>)}.</p>
              : <p className="muted">Not used yet. Open your production database and choose <strong>{data.target.name}</strong> as its sandbox copy. If this was meant to be the production database, change its connection in <a href="#settings">Settings</a> below.</p>}
          </div>
        </div>
      ) : (
        <Panel title="Setup checklist" aside={data.ready ? <span className="tone-ok">Ready to run</span> : `${checksLeft} of ${data.checks.length} left`}>
          <ul className="grid divide-y divide-[var(--line-soft)]">
            {data.checks.map((check) => <CheckRow key={check.key} ok={check.ok} label={check.label} detail={check.detail} />)}
          </ul>
          {!data.checks.find((check) => check.key === "connection")?.ok && canOperate && (
            <p className="mt-3 text-sm"><a href="#settings">Change the connection in Settings</a> to fix the first step.</p>
          )}
        </Panel>
      )}

      {!isSandbox && (
        <div className="grid gap-5 lg:grid-cols-2">
          <Panel title="Sandbox copy">
            <p className="text-sm muted">Every change is tested on a separate copy first, so this database is never used for experiments.</p>
            <p className="mt-3">{data.evaluation_mapping ? <>Testing on <Link to={`/targets/${data.evaluation_mapping.evaluation_target_id}`}>{data.evaluation_mapping.evaluation_target_name}</Link></> : <span className="tone-warn">No sandbox copy chosen yet</span>}</p>
            {canOperate && sandboxCandidates.length === 0 && !data.evaluation_mapping && (
              <p className="mt-4 text-sm tone-warn" role="status">
                No sandbox copies yet. <Link to="/targets">Add a target</Link>{sandboxLabels.length ? <> using the sandbox connection <code>{sandboxLabels.join(", ")}</code></> : null}, then choose it here.
              </p>
            )}
            {canOperate && sandboxCandidates.length > 0 && (
              <form className="mt-4 flex flex-wrap items-end gap-2" onSubmit={(event) => { event.preventDefault(); if (chosenSandbox && chosenSandbox !== mappedId) mapping.mutate(chosenSandbox); }}>
                <label className="field min-w-[200px] flex-1">Sandbox target
                  <select className="input" value={chosenSandbox} onChange={(event) => setSandbox(event.target.value)}>
                    {!mappedId && <option value="">Choose a sandbox…</option>}
                    {sandboxCandidates.map((target) => <option key={target.id} value={target.id}>{target.name} ({target.connection_label})</option>)}
                  </select>
                </label>
                {(!mappedId || (sandbox && sandbox !== mappedId)) && (
                  <button className="btn" disabled={!chosenSandbox || mapping.isPending} type="submit">{mappedId ? "Change sandbox" : "Map sandbox"}</button>
                )}
              </form>
            )}
            {mapping.isError && <p className="mt-2 text-sm tone-bad">{mapping.error.message}</p>}
          </Panel>
          <Panel title="Database" aside={data.capability ? `checked ${relativeTime(data.capability.observed_at)}` : undefined}>
            {data.capability ? (
              <dl className="kv">
                <dt>Version</dt><dd>MongoDB {String(capability.version ?? "?")}</dd>
                <dt>Setup</dt><dd>{topology(capability.topology)}{capability.primary ? ", connected to the primary" : ""}</dd>
                <dt>Query data</dt><dd>{data.telemetry.source ? `${telemetrySource(data.telemetry.source)}, updated ${relativeTime(data.telemetry.latest_window_at)}` : "none collected yet"}</dd>
              </dl>
            ) : <Empty title="Not reached yet">The worker connects on its next pass (about every 30 seconds) and records the version here.</Empty>}
          </Panel>
        </div>
      )}

      {!isSandbox && (
        <Panel title="Collections" aside="The optimizer may only change collections you allow" flush>
          {namespaces.length ? (
            <table className="table">
              <thead><tr><th>Collection</th><th className="num">Query patterns seen</th><th>Allow changes</th></tr></thead>
              <tbody>
                {namespaces.map((namespace) => {
                  const pending = namespace.id in optimistic;
                  const allowed = pending ? optimistic[namespace.id] : namespace.allowlisted;
                  return (
                  <tr key={namespace.id}>
                    <td><code>{namespace.name}</code></td>
                    <td className="num">{namespace.query_shapes}</td>
                    <td>
                      <label className="inline-flex cursor-pointer items-center gap-2">
                        <input type="checkbox" className="h-4 w-4 accent-[var(--accent)]" checked={allowed} aria-busy={pending} disabled={!canOperate || pending}
                          onChange={(event) => allowlist.mutate({ namespace: namespace.id, allowed: event.target.checked })} />
                        <span className={`text-sm ${allowed ? "tone-ok" : "muted"}`}>{pending ? "Saving…" : allowed ? "Allowed" : "Not allowed"}</span>
                      </label>
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          ) : <div className="panel-body"><Empty title="No collections seen yet">They appear after the worker's first pass over this database (about 30 seconds).</Empty></div>}
        </Panel>
      )}

      {!isSandbox && (
        <Panel title="Recent runs" flush>
          {data.recent_runs.length ? (
            <table className="table">
              <thead><tr><th>Status</th><th>What happened</th><th className="num">Started</th></tr></thead>
              <tbody>
                {data.recent_runs.map((run) => (
                  <tr key={run.run_id}>
                    <td><Link to={`/runs/${run.run_id}`} className="no-underline"><RunStatus status={run.status} reason={run.completion_reason} /></Link></td>
                    <td className="text-sm"><Link to={`/runs/${run.run_id}`}>{runOutcome(run)}</Link></td>
                    <td className="num text-sm muted">{relativeTime(run.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="panel-body"><Empty title="No runs yet" /></div>}
        </Panel>
      )}
      {canOperate && <TargetSettings key={data.target.id} data={data} connections={connections} onSaved={refresh} />}
    </Page>
  );
}
