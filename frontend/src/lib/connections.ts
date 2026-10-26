export type ConnectionInfo = { label: string; database: string; roles: string[] };
export type ConnectionKind = "PRODUCTION" | "SANDBOX" | "UNKNOWN";

/** Production connections can be observed and changed; sandbox-only ones can only host trials. */
export function connectionKind(roles: readonly string[] | undefined): ConnectionKind {
  const set = new Set(roles ?? []);
  if (set.has("observer") && set.has("executor")) return "PRODUCTION";
  if (set.has("sandbox")) return "SANDBOX";
  return "UNKNOWN";
}

export function kindOfLabel(label: string, connections: readonly ConnectionInfo[]): ConnectionKind {
  return connectionKind(connections.find((item) => item.label === label)?.roles);
}

export function describeConnection(item: ConnectionInfo): string {
  const kind = connectionKind(item.roles);
  const purpose = kind === "PRODUCTION" ? "production database" : kind === "SANDBOX" ? "sandbox copy" : "unusable: no observer or sandbox role";
  return `${item.label}: ${purpose} (${item.database})`;
}
