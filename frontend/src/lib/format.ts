export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "never";
  const seconds = Math.round((now - new Date(iso).getTime()) / 1000);
  if (Number.isNaN(seconds)) return "unknown";
  const future = seconds < 0;
  const value = Math.abs(seconds);
  const text = value < 45 ? `${value}s` : value < 3600 ? `${Math.round(value / 60)}m` : value < 86_400 ? `${Math.round(value / 3600)}h` : `${Math.round(value / 86_400)}d`;
  return future ? `in ${text}` : `${text} ago`;
}

export function countdown(iso: string, now = Date.now()): { expired: boolean; text: string } {
  const remaining = Math.floor((new Date(iso).getTime() - now) / 1000);
  if (remaining <= 0) return { expired: true, text: "expired" };
  const minutes = Math.floor(remaining / 60);
  const seconds = remaining % 60;
  return { expired: false, text: `${minutes}:${String(seconds).padStart(2, "0")}` };
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (value >= 100) return `${value.toFixed(0)} ms`;
  if (value >= 10) return `${value.toFixed(1)} ms`;
  return `${value.toFixed(2)} ms`;
}

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function signedPct(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return "—";
  const value = ratio * 100;
  return `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toFixed(1)}%`;
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : "—";
}

export function indexSpec(fields: { field: string; direction: number }[]): string {
  return `{ ${fields.map((item) => `${item.field}: ${item.direction}`).join(", ")} }`;
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
