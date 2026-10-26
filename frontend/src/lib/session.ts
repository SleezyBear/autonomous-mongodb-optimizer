const ACCESS = "access_token";
const REFRESH = "refresh_token";

function storage(): Storage | null {
  try { return typeof window === "undefined" ? null : window.localStorage; } catch { return null; }
}

export function readSession(): { accessToken: string | null; refreshToken: string | null } {
  const store = storage();
  return { accessToken: store?.getItem(ACCESS) ?? null, refreshToken: store?.getItem(REFRESH) ?? null };
}

export function storeTokens(tokens: { access_token: string; refresh_token: string }): void {
  const store = storage();
  store?.setItem(ACCESS, tokens.access_token);
  store?.setItem(REFRESH, tokens.refresh_token);
}

export function clearSession(): void {
  const store = storage();
  store?.removeItem(ACCESS);
  store?.removeItem(REFRESH);
}

/** Decode a JWT payload without verifying it (display and expiry hints only). */
export function decodeClaims(token: string | null): { exp?: number; role?: string; sub?: string } | null {
  if (!token) return null;
  const part = token.split(".")[1];
  if (!part) return null;
  try {
    const json = atob(part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "="));
    const claims = JSON.parse(json) as unknown;
    return claims && typeof claims === "object" ? claims as { exp?: number; role?: string; sub?: string } : null;
  } catch {
    return null;
  }
}

/** Refresh when a well-formed access token expires within the window (or is missing). */
export function shouldRefresh(accessToken: string | null, nowSeconds = Date.now() / 1000, windowSeconds = 60): boolean {
  if (!accessToken) return true;
  const claims = decodeClaims(accessToken);
  if (!claims || typeof claims.exp !== "number") return false; // malformed: let the API reject it visibly
  return claims.exp - nowSeconds < windowSeconds;
}
