import { describe, expect, it } from "vitest";
import { connectionKind, describeConnection, kindOfLabel } from "./connections";

const connections = [
  { label: "mongo-monitored", database: "commerce", roles: ["observer", "executor"] },
  { label: "mongo-evaluation", database: "commerce", roles: ["sandbox"] },
];

describe("connection kinds", () => {
  it("classifies production and sandbox labels", () => {
    expect(connectionKind(["observer", "executor"])).toBe("PRODUCTION");
    expect(connectionKind(["sandbox"])).toBe("SANDBOX");
    expect(connectionKind([])).toBe("UNKNOWN");
    expect(kindOfLabel("mongo-evaluation", connections)).toBe("SANDBOX");
    expect(kindOfLabel("missing", connections)).toBe("UNKNOWN");
  });
  it("describes a label in operator terms", () => {
    expect(describeConnection(connections[0])).toBe("mongo-monitored: production database (commerce)");
    expect(describeConnection(connections[1])).toBe("mongo-evaluation: sandbox copy (commerce)");
  });
});
