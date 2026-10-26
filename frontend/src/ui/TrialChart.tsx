import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Candidate } from "../api/client";
import { ms } from "../lib/format";

/** Paired sandbox trials: each x is one AB/BA pair measured on identical starting state. */
export default function TrialChart({ trials, metric }: { trials: Candidate["trials"]; metric: string }) {
  const data = trials.map((trial) => ({ pair: trial.pair_number, order: trial.arm_order, baseline: trial.baseline[metric], candidate: trial.candidate[metric] }));
  if (!data.length) return null;
  return (
    <figure className="m-0">
      <div className="h-48 w-full">
        <ResponsiveContainer>
          <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="var(--line-soft)" vertical={false} />
            <XAxis dataKey="pair" stroke="var(--faint)" tick={{ fontSize: 12 }} tickLine={false} />
            <YAxis stroke="var(--faint)" tick={{ fontSize: 12 }} tickLine={false} width={56} tickFormatter={(value: number) => ms(value)} />
            <Tooltip
              contentStyle={{ background: "var(--ink-2)", border: "1px solid var(--line)", borderRadius: 6, color: "var(--text)", fontSize: 12 }}
              labelFormatter={(pair) => `Pair ${pair}`}
              formatter={(value: number, name: string) => [ms(value), name]}
            />
            <Legend wrapperStyle={{ fontSize: 12, color: "var(--muted)" }} />
            <Line type="monotone" dataKey="baseline" name="Without index" stroke="var(--muted)" strokeWidth={1.6} dot={{ r: 2.5 }} isAnimationActive={false} />
            <Line type="monotone" dataKey="candidate" name="With index" stroke="var(--accent)" strokeWidth={2} dot={{ r: 2.5 }} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <figcaption className="mt-1 text-xs faint">{metric.startsWith("write")
        ? "Insert time (p95) in each trial. A single spike usually means the database paused to flush to disk, not that the index slowed writes."
        : `${metric.replace(/_ms$/, "").replace(/_/g, " ")} in each trial. The two versions take turns going first, so a machine speeding up or slowing down cannot bias the result.`}</figcaption>
    </figure>
  );
}
