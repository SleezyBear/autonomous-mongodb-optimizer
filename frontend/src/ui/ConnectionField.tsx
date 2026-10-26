import { connectionKind, describeConnection, type ConnectionInfo } from "../lib/connections";

/** Pick a worker connection label. A dropdown when the worker has reported its labels, free text otherwise. */
export default function ConnectionField({ value, onChange, connections, id = "connection-label" }: { value: string; onChange: (label: string) => void; connections: readonly ConnectionInfo[]; id?: string }) {
  const production = connections.filter((item) => connectionKind(item.roles) === "PRODUCTION");
  const sandboxes = connections.filter((item) => connectionKind(item.roles) === "SANDBOX");
  const kind = connectionKind(connections.find((item) => item.label === value)?.roles);
  return (
    <label className="field" htmlFor={id}>Connection label
      {connections.length ? (
        <select id={id} className="input" required value={value} onChange={(event) => onChange(event.target.value)}>
          <option value="">Choose a connection…</option>
          {production.length > 0 && <optgroup label="Production databases (watched, can be changed)">{production.map((item) => <option key={item.label} value={item.label}>{describeConnection(item)}</option>)}</optgroup>}
          {sandboxes.length > 0 && <optgroup label="Sandbox copies (for trials only)">{sandboxes.map((item) => <option key={item.label} value={item.label}>{describeConnection(item)}</option>)}</optgroup>}
        </select>
      ) : (
        <input id={id} className="input" required maxLength={256} value={value} onChange={(event) => onChange(event.target.value)} placeholder="mongo-monitored" />
      )}
      <span className="text-sm font-normal faint">
        {!connections.length ? "The worker has not reported its connections yet, so this label cannot be checked."
          : kind === "PRODUCTION" ? "Production: the optimizer watches this database and deploys approved indexes to it."
          : kind === "SANDBOX" ? "Sandbox: candidate indexes are tested here. Map it from a production target."
          : "Pick the production database to optimize, or a sandbox copy to test on."}
      </span>
    </label>
  );
}
