import { describe, expect, it } from "vitest";
import { actorName, boundWords, describeShape, isStalled, ledgerEvent, runBadge, speedChange, stepName, telemetrySource } from "./present";

describe("run badges", () => {
  it("names outcomes the way an operator would", () => {
    expect(runBadge({ status: "COMPLETED", completion_reason: "DEPLOYMENT_SUCCEEDED" }).label).toBe("Deployed");
    expect(runBadge({ status: "COMPLETED", completion_reason: "NO_CANDIDATES" }).label).toBe("No change");
    expect(runBadge({ status: "COMPLETED", completion_reason: "APPROVAL_EXPIRED" }).label).toBe("No change");
    expect(runBadge({ status: "ROLLED_BACK" }).label).toBe("Rolled back");
    expect(runBadge({ status: "APPROVAL_PENDING" }).label).toBe("Needs approval");
    expect(runBadge({ status: "EVALUATING" }).label).toBe("Running");
  });
  it("marks a run whose job failed as stalled", () => {
    const run = { status: "CREATED", job_status: "FAILED" };
    expect(isStalled(run)).toBe(true);
    expect(runBadge(run)).toEqual({ label: "Stalled", tone: "bad" });
    expect(isStalled({ status: "COMPLETED", job_status: "FAILED" })).toBe(false);
  });
});

describe("plain-language labels", () => {
  it("describes a query shape without its values", () => {
    expect(describeShape({ operation: "find", namespace: "billing.invoices", shape: { query: { find: "<string>", filter: { account_id: "<string>" }, sort: { created_at: 1 } } } }))
      .toBe("find invoices by account_id, sorted by created_at");
    expect(describeShape({ operation: "find", namespace: "commerce.orders" })).toBe("find orders");
  });
  it("translates steps, sources, ledger events and actors", () => {
    expect(stepName("CREATED")).toBe("Snapshot");
    expect(stepName("EVALUATING")).toBe("Sandbox trials");
    expect(telemetrySource("DIAGNOSTIC_LOG")).toBe("slow-query log");
    expect(ledgerEvent("ROLLBACK_APPLIED")).toBe("Index removed (rollback)");
    expect(actorName("durable-production-worker")).toBe("Optimizer (deploy)");
  });
  it("expresses a latency change as faster or slower", () => {
    expect(speedChange(14.6, 3.47)).toBe("76% faster");
    expect(speedChange(14.2, 253)).toBe("18× slower");
    expect(speedChange(10, 12)).toBe("20% slower");
    expect(speedChange(0, 3)).toBe("");
    expect(speedChange(3.44, 3.44)).toBe("no change");
  });
});

describe("admission bounds in words", () => {
  it("explains benefit and protected bounds", () => {
    expect(boundWords("benefit", 3.842, 0.051)).toBe("at least 98% faster (needs 5%)");
    expect(boundWords("protected", 0.161, 0.030)).toBe("could be up to 17% slower (limit: 3% slower)");
    expect(boundWords("protected", -3.842, 0.049)).toBe("at worst still 98% faster (limit: 5% slower)");
    expect(boundWords("protected", 0.005, 0.030)).toBe("at most 1% slower (limit: 3% slower)");
  });
});
