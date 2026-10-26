import { describe, expect, it } from "vitest";
import { explainAdmission, explainBlocker, railStates, runOutcome, statusTone } from "./lifecycle";

const run = (status: string, completion_reason: string | null = null, job_status: string | null = "RUNNING") => ({ status, completion_reason, job_status, deployment_mode: "APPROVAL_CONTROLLED" });

describe("lifecycle rail", () => {
  it("marks earlier stages done and the active stage current", () => {
    const states = railStates(run("EVALUATING"));
    expect(states.snapshot).toBe("done");
    expect(states.rank).toBe("done");
    expect(states.evaluate).toBe("current");
    expect(states.admit).toBe("pending");
  });

  it("shows a waiting approval gate and a blocked stalled stage", () => {
    expect(railStates(run("APPROVAL_PENDING")).approve).toBe("waiting");
    expect(railStates(run("SNAPSHOTTING", null, "FAILED")).snapshot).toBe("blocked");
  });

  it("advances past finished sub-steps", () => {
    const states = railStates(run("ADMITTED"));
    expect(states.admit).toBe("done");
    expect(states.approve).toBe("current");
  });

  it("stops the rail where a safe completion ended the run", () => {
    const noCandidates = railStates(run("COMPLETED", "NO_CANDIDATES"));
    expect(noCandidates.candidates).toBe("stopped");
    expect(noCandidates.rank).toBe("skipped");
    const rejected = railStates(run("COMPLETED", "NO_ADMITTED_CANDIDATE"));
    expect(rejected.admit).toBe("stopped");
    const deployed = railStates(run("COMPLETED", "DEPLOYMENT_SUCCEEDED"));
    expect(Object.values(deployed).every((state) => state === "done")).toBe(true);
    expect(railStates(run("COMPLETED", "DEPLOYMENT_SUCCEEDED"), true).approve).toBe("skipped");
    expect(railStates(run("ROLLBACK_BLOCKED")).monitor).toBe("blocked");
  });
});

describe("run outcome copy", () => {
  it("renders deployment success as a human outcome", () => {
    expect(runOutcome({ status: "COMPLETED", completion_reason: "DEPLOYMENT_SUCCEEDED" })).toBe("Deployment completed successfully.");
    expect(runOutcome({ status: "COMPLETED", completion_reason: "NO_CANDIDATES" })).toBe("Completed safely: no candidates were generated.");
  });

  it("uses semantic tones", () => {
    expect(statusTone("COMPLETED", "DEPLOYMENT_SUCCEEDED")).toBe("ok");
    expect(statusTone("APPROVAL_PENDING")).toBe("warn");
    expect(statusTone("EVALUATING", null, true)).toBe("bad");
    expect(statusTone("DIAGNOSING")).toBe("ai");
  });

  it("explains known blockers and names unknown ones", () => {
    expect(explainBlocker("NO_COMPLETED_TELEMETRY")).toMatch(/slow-query data/);
    expect(explainBlocker("SOMETHING_NEW")).toBe("The worker recorded SOMETHING_NEW.");
  });
});

describe("explainAdmission", () => {
  it("explains a noise refusal in plain language", () => {
    const [text] = explainAdmission(["AA_CALIBRATION_EXCEEDS_PROFILE_MAXIMUM"]);
    expect(text).toMatch(/too noisy/);
    expect(text).toMatch(/nothing was deployed/);
  });
  it("falls back for unknown codes", () => {
    expect(explainAdmission(["NEW_CODE"])).toEqual(["Admission recorded NEW_CODE."]);
  });
});

describe("approval expiry", () => {
  it("ends the rail at the approval gate and explains it", () => {
    const run = { status: "COMPLETED", completion_reason: "APPROVAL_EXPIRED", job_status: "COMPLETED", deployment_mode: "APPROVAL_CONTROLLED" };
    const states = railStates(run);
    expect(states.approve).toBe("stopped");
    expect(states.deploy).toBe("skipped");
    expect(runOutcome(run)).toMatch(/nothing was deployed/);
  });
});
