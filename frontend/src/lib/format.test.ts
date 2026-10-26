import { describe, expect, it } from "vitest";
import { countdown, indexSpec, ms, relativeTime, signedPct } from "./format";

describe("formatters", () => {
  it("formats latency with sensible precision", () => {
    expect(ms(0.1234)).toBe("0.12 ms");
    expect(ms(12.34)).toBe("12.3 ms");
    expect(ms(250)).toBe("250 ms");
    expect(ms(null)).toBe("—");
  });

  it("formats signed relative change", () => {
    expect(signedPct(-0.4123)).toBe("−41.2%");
    expect(signedPct(0.05)).toBe("+5.0%");
  });

  it("counts down approval expiry", () => {
    const now = Date.parse("2026-01-01T00:00:00Z");
    expect(countdown("2026-01-01T00:14:05Z", now)).toEqual({ expired: false, text: "14:05" });
    expect(countdown("2025-12-31T23:59:00Z", now).expired).toBe(true);
  });

  it("describes times relative to now", () => {
    const now = Date.parse("2026-01-01T01:00:00Z");
    expect(relativeTime("2026-01-01T00:59:30Z", now)).toBe("30s ago");
    expect(relativeTime("2026-01-01T00:00:00Z", now)).toBe("1h ago");
    expect(relativeTime(null, now)).toBe("never");
  });

  it("renders index key specifications", () => {
    expect(indexSpec([{ field: "customer_id", direction: 1 }, { field: "created_at", direction: -1 }])).toBe("{ customer_id: 1, created_at: -1 }");
  });
});
