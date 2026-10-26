import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { kindOfLabel } from "../lib/connections";
import { dateTime } from "../lib/format";
import ConnectionField from "../ui/ConnectionField";
import { Empty, ErrorNote, Loading, Page, Panel } from "../ui/primitives";

function CreateTarget() {
  const navigate = useNavigate();
  const client = useQueryClient();
  const connections = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const [name, setName] = useState("");
  const [label, setLabel] = useState("");
  const [mode, setMode] = useState("APPROVAL_CONTROLLED");
  const create = useMutation({
    mutationFn: api.createTarget,
    onSuccess: async (target) => { await client.invalidateQueries({ queryKey: ["targets"] }); navigate(`/targets/${target.id}`); },
  });
  const labels = connections.data?.connections ?? [];
  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate({ name: name.trim(), connection_label: label.trim(), deployment_mode: mode });
  }
  return (
    <Panel title="Add a target">
      <form className="grid gap-4" onSubmit={submit}>
        <p className="text-sm muted">Add two targets: the production database you want to speed up, and a sandbox copy where changes are tested first. Credentials stay with the worker; here you only pick a connection.</p>
        <div className="grid items-start gap-4 sm:grid-cols-3">
          <label className="field">Name
            <input className="input" required maxLength={128} value={name} onChange={(event) => setName(event.target.value)} placeholder="orders-production" />
          </label>
          <ConnectionField value={label} onChange={setLabel} connections={labels} />
          {kindOfLabel(label, labels) !== "SANDBOX" && (
            <label className="field">Deployment mode
              <select className="input" value={mode} onChange={(event) => setMode(event.target.value)}>
                <option value="APPROVAL_CONTROLLED">Approval controlled</option>
                <option value="FULL_AUTONOMOUS">Full autonomous</option>
              </select>
            </label>
          )}
        </div>
        <div className="flex items-center gap-3">
          <button className="btn btn-primary" disabled={create.isPending} type="submit">{create.isPending ? "Adding…" : "Add target"}</button>
          {create.isError && <span className="text-sm tone-bad">{create.error.message}</span>}
        </div>
      </form>
    </Panel>
  );
}

export default function Targets() {
  const targets = useQuery({ queryKey: ["targets"], queryFn: api.listTargets });
  const connections = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const overview = useQuery({ queryKey: ["overview"], queryFn: api.overview, refetchInterval: 15_000 });
  const readiness = new Map((overview.data?.targets ?? []).map((target) => [target.id, target]));
  const [showArchived, setShowArchived] = useState(false);
  const known = connections.data?.connections ?? [];
  const archivedCount = (targets.data ?? []).filter((target) => !target.is_active).length;
  const visible = (targets.data ?? []).filter((target) => showArchived || target.is_active);
  const me = useQuery({ queryKey: ["me"], queryFn: api.me, staleTime: 60_000 });
  const canOperate = me.data?.role === "OPERATOR" || me.data?.role === "ADMIN";
  return (
    <Page title="Targets" subtitle="The databases the optimizer knows about: each production database you want to speed up, and the sandbox copy it tests changes on.">
      {targets.isPending ? <Loading what="targets" /> : targets.isError ? <ErrorNote error={targets.error} /> : (
        <Panel title="All targets" aside={archivedCount > 0 && (
          <label className="inline-flex cursor-pointer items-center gap-2"><input type="checkbox" className="accent-[var(--accent)]" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} />Show archived ({archivedCount})</label>
        )} flush>
          {visible.length ? (
            <table className="table">
              <thead><tr><th>Name</th><th>Role</th><th>Status</th><th>Connection</th><th>Mode</th><th className="num">Added</th></tr></thead>
              <tbody>
                {visible.map((target) => {
                  const kind = kindOfLabel(target.connection_label, known);
                  return (
                  <tr key={target.id}>
                    <td><Link to={`/targets/${target.id}`} className="font-medium">{target.name}</Link></td>
                    <td className="text-sm">{kind === "SANDBOX" ? "Sandbox copy" : kind === "PRODUCTION" ? "Production" : <span className="tone-warn">Unknown connection</span>}</td>
                    <td className="text-sm">{(() => {
                      if (!target.is_active) return <span className="muted">Archived</span>;
                      if (kind === "SANDBOX") return <span className="muted">Used for testing</span>;
                      const info = readiness.get(target.id);
                      if (!info) return <span className="muted">Checking…</span>;
                      const left = info.checks.filter((check) => !check.ok).length;
                      return info.ready ? <span className="tone-ok">Ready to run</span> : <span className="tone-warn">{left} setup step{left === 1 ? "" : "s"} left</span>;
                    })()}</td>
                    <td><code>{target.connection_label}</code></td>
                    <td className="text-sm">{kind === "SANDBOX" ? <span className="faint">n/a</span> : target.deployment_mode === "FULL_AUTONOMOUS" ? "Full autonomous" : "Approval controlled"}</td>
                    <td className="num text-sm muted">{dateTime(target.created_at)}</td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          ) : <div className="panel-body"><Empty title="No targets yet">Add the production database first, then its sandbox copy.</Empty></div>}
        </Panel>
      )}
      {canOperate && <CreateTarget />}
    </Page>
  );
}
