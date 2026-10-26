import { RAIL, type StepState } from "../lib/lifecycle";

const stateText: Record<StepState, string> = {
  done: "completed", current: "in progress", waiting: "waiting for a decision", blocked: "blocked",
  stopped: "stopped here", reverted: "regression detected, change rolled back", pending: "not reached", skipped: "skipped",
};

export default function PipelineRail({ states }: { states: Record<string, StepState> }) {
  return (
    <ol className="rail" aria-label="Lifecycle timeline">
      {RAIL.map((step) => {
        const state = states[step.key] ?? "pending";
        return (
          <li className="rail-step" data-state={state} data-gate={step.gate ? "true" : undefined} key={step.key}>
            <span className="rail-node" aria-hidden>{step.ai && <span className="rail-ai" title="AI gives advice at this step" />}</span>
            <span className="rail-label">{step.label}<span className="sr-only">: {stateText[state]}</span></span>
            {state === "stopped" && <span className="rail-caption" aria-hidden>ended here</span>}
            {state === "waiting" && <span className="rail-caption tone-warn" aria-hidden>waiting</span>}
          </li>
        );
      })}
    </ol>
  );
}
