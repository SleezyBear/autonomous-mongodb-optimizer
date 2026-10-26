import { useMutation, useQuery } from "@tanstack/react-query";
import { type FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { kindOfLabel } from "../lib/connections";
import { CheckRow, ErrorNote, Page, Panel } from "../ui/primitives";

export default function NewRun() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const targets = useQuery({ queryKey: ["targets"], queryFn: api.listTargets });
  const connections = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const [targetId, setTargetId] = useState(params.get("target") ?? "");
  const [mode, setMode] = useState("APPROVAL_CONTROLLED");
  const [metric, setMetric] = useState("p99_latency_ms");
  const readiness = useQuery({ queryKey: ["target", targetId], queryFn: () => api.getTarget(targetId), enabled: /^[0-9a-f-]{36}$/.test(targetId) });
  const create = useMutation({ mutationFn: api.createRun, onSuccess: (run) => navigate(`/runs/${run.run_id}`) });
  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate({ target_id: targetId, deployment_mode: mode, primary_metric_key: metric });
  }
  const known = connections.data?.connections ?? [];
  // Sandboxes only host trials; runs are started on the production target.
  const monitored = (targets.data ?? []).filter((target) => target.is_active && kindOfLabel(target.connection_label, known) !== "SANDBOX");
  const notReady = Boolean(readiness.data && !readiness.data.ready);
  const chosen = monitored.find((target) => target.id === targetId);
  // A run starts in the target's own mode; the operator can still choose the stricter one.
  useEffect(() => { if (chosen) setMode(chosen.deployment_mode); }, [chosen?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Page title="Start a run" subtitle="The optimizer looks at the database's recent slow queries, suggests indexes, and tests them on the sandbox copy. Nothing changes on the database without passing those tests.">
      <div className="grid gap-5 lg:grid-cols-[1fr_360px]">
        <Panel title="Run settings">
          <form className="grid gap-4" onSubmit={submit}>
            <label className="field">Database to speed up
              <select aria-label="Target ID" className="input" required value={targetId} onChange={(event) => setTargetId(event.target.value)}>
                <option value="">Choose a database…</option>
                {monitored.map((target) => <option key={target.id} value={target.id}>{target.name} ({target.connection_label})</option>)}
              </select>
            </label>
            <fieldset className="grid gap-2">
              <legend className="mb-1 text-sm muted">Deployment mode</legend>
              {[
                ["APPROVAL_CONTROLLED", "Approval controlled", "If a change passes the tests, a second person must approve it before it is applied."],
                ["FULL_AUTONOMOUS", "Full autonomous", "A change that passes stricter tests is applied automatically. Anything less certain still goes to a person for approval."],
              ].map(([value, label, help]) => (
                <label key={value} className={`flex cursor-pointer gap-3 rounded-[var(--radius)] border p-3 ${mode === value ? "border-[var(--accent)] bg-[var(--ink-2)]" : "border-[var(--line-soft)]"}`}>
                  <input type="radio" name="mode" value={value} checked={mode === value} onChange={() => setMode(value)} className="mt-1 accent-[var(--accent)]" />
                  <span><span className="block font-medium">{label}</span><span className="text-sm muted">{help}</span></span>
                </label>
              ))}
            </fieldset>
            <label className="field">What to improve
              <select className="input" value={metric} onChange={(event) => setMetric(event.target.value)}>
                <option value="p99_latency_ms">p99 latency: the slowest 1% of queries (recommended)</option>
                <option value="p95_latency_ms">p95 latency: the slowest 5% of queries</option>
                <option value="p50_latency_ms">p50 latency: a typical query</option>
                <option value="throughput_ops_per_second">Throughput: queries per second</option>
              </select>
            </label>
            <p className="text-sm faint">Whatever you pick, a change is rejected if it makes p95 or p99 latency (or write speed) worse.</p>
            <div className="flex items-center gap-3">
              <button className={notReady ? "btn" : "btn btn-primary"} disabled={create.isPending || !targetId} type="submit">{create.isPending ? "Creating…" : notReady ? "Create run anyway" : "Create run"}</button>
              <Link to="/runs" className="text-sm">Cancel</Link>
            </div>
            {create.isError && <ErrorNote error={create.error} />}
          </form>
        </Panel>
        <Panel title="Setup checklist">
          {!targetId ? <p className="text-sm muted">Choose a database to check it is ready.</p> : readiness.isPending ? <p className="text-sm muted">Checking…</p> : readiness.isError ? <ErrorNote error={readiness.error} /> : (
            <>
              <ul className="grid divide-y divide-[var(--line-soft)]">{readiness.data.checks.map((check) => <CheckRow key={check.key} ok={check.ok} label={check.label} detail={check.detail} />)}</ul>
              {!readiness.data.ready && <p className="mt-3 text-sm tone-warn">This target is not ready, so a run would stop at the first unchecked step. <Link to={`/targets/${targetId}`}>Finish setup first</Link>.</p>}
            </>
          )}
        </Panel>
      </div>
    </Page>
  );
}
