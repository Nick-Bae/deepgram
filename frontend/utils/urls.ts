// utils/urls.ts

/**
 * When the browser is served over HTTPS, upgrade http:// → https:// and
 * ws:// → wss:// so credentials are never sent over unencrypted connections.
 * Safe to call server-side: typeof window guard returns the URL unchanged.
 */
export function enforceSecureProtocol(url: string): string {
  if (typeof window !== "undefined" && window.location.protocol === "https:") {
    return url
      .replace(/^http:\/\//i, "https://")
      .replace(/^ws:\/\//i,   "wss://");
  }
  return url;
}

const api   = process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000";
const rawWs = process.env.NEXT_PUBLIC_WS_URL        || api.replace("http", "ws");

export const API_URL = enforceSecureProtocol(api);
export const WS_URL  = enforceSecureProtocol(
  rawWs.includes("/ws/")
    ? rawWs
    : `${rawWs.replace(/\/+$/, "")}/ws/translate`
);
