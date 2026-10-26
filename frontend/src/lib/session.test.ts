import { describe, expect, it } from "vitest";
import { decodeClaims, shouldRefresh } from "./session";

function token(claims: object): string {
  const encode = (value: object) => btoa(JSON.stringify(value)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  return `${encode({ alg: "HS256" })}.${encode(claims)}.signature`;
}

describe("session token handling", () => {
  it("decodes JWT claims for display only", () => {
    expect(decodeClaims(token({ role: "OPERATOR", exp: 100 }))).toMatchObject({ role: "OPERATOR", exp: 100 });
    expect(decodeClaims("not-a-token")).toBeNull();
  });

  it("refreshes only well-formed tokens that are about to expire", () => {
    expect(shouldRefresh(token({ exp: 1_000 }), 990)).toBe(true);
    expect(shouldRefresh(token({ exp: 10_000 }), 990)).toBe(false);
    // A tampered token is never silently replaced; the API rejection stays visible.
    expect(shouldRefresh("tampered.jwt.value", 990)).toBe(false);
    expect(shouldRefresh(null)).toBe(true);
  });
});
