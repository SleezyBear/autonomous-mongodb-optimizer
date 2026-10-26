import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../api/client";
import { countdown } from "../lib/format";
import { type Tone } from "../lib/lifecycle";
import { runBadge } from "../lib/present";

export function Page({ title, subtitle, actions, children }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <main className="mx-auto w-full max-w-[1180px] px-5 pb-16 pt-8 sm:px-8">
      <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <h1>{title}</h1>
          {subtitle && <p className="mt-1.5 max-w-[68ch] muted">{subtitle}</p>}
        </div>
        {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
      </header>
      <div className="grid grid-cols-[minmax(0,1fr)] gap-5">{children}</div>
    </main>
  );
}

export function Panel({ title, aside, children, flush = false, id }: { title?: ReactNode; aside?: ReactNode; children: ReactNode; flush?: boolean; id?: string }) {
  return (
    <section className="panel min-w-0" aria-labelledby={id}>
      {title && <div className="panel-head"><h2 id={id}>{title}</h2>{aside && <div className="text-sm muted">{aside}</div>}</div>}
      <div className={flush ? "overflow-x-auto" : "panel-body"}>{children}</div>
    </section>
  );
}

export function Pill({ tone, children, label }: { tone: Tone; children: ReactNode; label?: string }) {
  return <span aria-label={label} className={`pill tone-${tone}`}>{children}</span>;
}

export function RunStatus({ status, reason, stalled, label }: { status: string; reason?: string | null; stalled?: boolean; label?: string }) {
  const badge = runBadge({ status, completion_reason: reason }, Boolean(stalled));
  return <span aria-label={label} title={status.toLowerCase().replace(/_/g, " ")} className={`pill tone-${badge.tone}`}>{badge.label}</span>;
}

export function Loading({ what }: { what: string }) {
  return <p className="muted" role="status">Loading {what}…</p>;
}

export function ErrorNote({ error }: { error: Error }) {
  const unauthorized = error instanceof ApiError && error.status === 401;
  return (
    <div className="callout callout-bad" role="alert">
      <div>
        <p className="tone-bad font-medium">{error.message}</p>
        {unauthorized && <p className="mt-1 text-sm muted">Your session is missing or no longer valid. <Link to="/login">Sign in again</Link>.</p>}
      </div>
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="grid gap-2 px-1 py-6">
      <p className="font-medium">{title}</p>
      {children && <div className="max-w-[64ch] text-sm muted">{children}</div>}
    </div>
  );
}

export function Countdown({ until }: { until: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  const value = countdown(until, now);
  return <span className={`mono ${value.expired ? "tone-bad" : "tone-warn"}`}>{value.expired ? "expired" : `${value.text} left`}</span>;
}

export function CheckRow({ ok, label, detail }: { ok: boolean; label: string; detail: string }) {
  return (
    <li className="flex items-start gap-3 py-2">
      <span aria-hidden className={`mt-[3px] grid h-4 w-4 shrink-0 place-items-center rounded-full border ${ok ? "border-[var(--ok)] bg-[var(--ok)]" : "border-[var(--warn)]"}`}>
        {ok && <svg viewBox="0 0 12 12" className="h-2.5 w-2.5" fill="none" stroke="#06261a" strokeWidth="2"><path d="M2.5 6.2 5 8.5l4.5-5" /></svg>}
      </span>
      <div className="min-w-0">
        <p className={ok ? "" : "font-medium"}>{label}<span className="sr-only">{ok ? " — done" : " — needs attention"}</span></p>
        <p className="text-sm muted">{detail}</p>
      </div>
    </li>
  );
}

export function Stat({ value, label, tone }: { value: ReactNode; label: string; tone?: Tone }) {
  return (
    <div className="min-w-0">
      <p className={`text-2xl font-semibold tabular-nums ${tone ? `tone-${tone}` : ""}`}>{value}</p>
      <p className="text-sm muted">{label}</p>
    </div>
  );
}
