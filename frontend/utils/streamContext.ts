export type StreamContext = {
  orgId?: string;
  roomId?: string;
  serviceKey?: string;
  churchSlug?: string;
};

const STORAGE_KEYS = {
  orgId: "rt_org_id",
  roomId: "rt_room_id",
  serviceKey: "rt_service_key",
  churchSlug: "rt_church_slug",
  hostToken: "rt_host_token",
  authToken: "rt_auth_token",
} as const;

const SERVICE_KEY_PARAM_KEYS = ["serviceKey", "service_key", "key"] as const;
const ORG_ID_PARAM_KEYS = ["orgId", "org_id"] as const;
const ROOM_ID_PARAM_KEYS = ["roomId", "room_id"] as const;
const CHURCH_SLUG_PARAM_KEYS = ["churchSlug", "church_slug", "slug"] as const;

function clean(raw: unknown): string | undefined {
  if (raw == null) return undefined;
  const txt = String(raw).trim();
  return txt || undefined;
}

function parseUrlMaybe(rawUrl?: string): URL | null {
  if (!rawUrl) return null;
  try {
    if (typeof window !== "undefined") return new URL(rawUrl, window.location.origin);
    return new URL(rawUrl);
  } catch {
    return null;
  }
}

function firstParam(parsed: URL, keys: readonly string[]): string | undefined {
  for (const key of keys) {
    const val = clean(parsed.searchParams.get(key));
    if (val) return val;
  }
  return undefined;
}

export function contextFromUrl(rawUrl?: string): StreamContext {
  const parsed = parseUrlMaybe(rawUrl);
  if (!parsed) return {};
  return {
    orgId: firstParam(parsed, ORG_ID_PARAM_KEYS),
    roomId: firstParam(parsed, ROOM_ID_PARAM_KEYS),
    serviceKey: firstParam(parsed, SERVICE_KEY_PARAM_KEYS),
    churchSlug: firstParam(parsed, CHURCH_SLUG_PARAM_KEYS),
  };
}

export function contextFromSession(): StreamContext {
  if (typeof window === "undefined") return {};
  try {
    return {
      orgId: clean(window.sessionStorage.getItem(STORAGE_KEYS.orgId)),
      roomId: clean(window.sessionStorage.getItem(STORAGE_KEYS.roomId)),
      serviceKey: clean(window.sessionStorage.getItem(STORAGE_KEYS.serviceKey)),
      churchSlug: clean(window.sessionStorage.getItem(STORAGE_KEYS.churchSlug)),
    };
  } catch {
    return {};
  }
}

export function persistStreamContext(ctx: StreamContext): StreamContext {
  const next: StreamContext = {
    orgId: clean(ctx.orgId),
    roomId: clean(ctx.roomId),
    serviceKey: clean(ctx.serviceKey),
    churchSlug: clean(ctx.churchSlug),
  };
  if (typeof window === "undefined") return next;
  try {
    if (next.orgId) window.sessionStorage.setItem(STORAGE_KEYS.orgId, next.orgId);
    if (next.roomId) window.sessionStorage.setItem(STORAGE_KEYS.roomId, next.roomId);
    if (next.serviceKey) window.sessionStorage.setItem(STORAGE_KEYS.serviceKey, next.serviceKey);
    if (next.churchSlug) window.sessionStorage.setItem(STORAGE_KEYS.churchSlug, next.churchSlug);
  } catch {}
  return next;
}

export function clearRoomInSession(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(STORAGE_KEYS.roomId);
  } catch {}
}

export function getHostTokenFromSession(): string | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    return clean(window.sessionStorage.getItem(STORAGE_KEYS.hostToken));
  } catch {
    return undefined;
  }
}

export function persistHostToken(rawToken?: string): string | undefined {
  const token = clean(rawToken);
  if (typeof window === "undefined") return token;
  try {
    if (token) window.sessionStorage.setItem(STORAGE_KEYS.hostToken, token);
    else window.sessionStorage.removeItem(STORAGE_KEYS.hostToken);
  } catch {}
  return token;
}

export function clearHostToken(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(STORAGE_KEYS.hostToken);
  } catch {}
}

export function getAuthTokenFromSession(): string | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    return clean(window.sessionStorage.getItem(STORAGE_KEYS.authToken));
  } catch {
    return undefined;
  }
}

export function persistAuthToken(rawToken?: string): string | undefined {
  const token = clean(rawToken);
  if (typeof window === "undefined") return token;
  try {
    if (token) window.sessionStorage.setItem(STORAGE_KEYS.authToken, token);
    else window.sessionStorage.removeItem(STORAGE_KEYS.authToken);
  } catch {}
  return token;
}

export function clearAuthToken(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(STORAGE_KEYS.authToken);
  } catch {}
}

export function resolveStreamContext(seedUrl?: string): StreamContext {
  const fromSeed = contextFromUrl(seedUrl);
  const fromLocation = typeof window !== "undefined" ? contextFromUrl(window.location.href) : {};
  const fromSession = contextFromSession();

  const merged: StreamContext = {
    orgId: clean(fromSeed.orgId ?? fromLocation.orgId ?? fromSession.orgId),
    roomId: clean(fromSeed.roomId ?? fromLocation.roomId ?? fromSession.roomId),
    serviceKey: clean(fromSeed.serviceKey ?? fromLocation.serviceKey ?? fromSession.serviceKey),
    churchSlug: clean(fromSeed.churchSlug ?? fromLocation.churchSlug ?? fromSession.churchSlug),
  };
  return persistStreamContext(merged);
}

function setIfMissing(parsed: URL, keys: readonly string[], value: string | undefined): void {
  if (!value) return;
  const hasAny = keys.some((k) => parsed.searchParams.has(k));
  if (!hasAny) parsed.searchParams.set(keys[0], value);
}

export function appendStreamContextToUrl(
  rawUrl: string,
  ctx: StreamContext,
  extras?: Record<string, string | undefined>,
): string {
  const parsed = parseUrlMaybe(rawUrl);
  if (!parsed) return rawUrl;
  setIfMissing(parsed, ORG_ID_PARAM_KEYS, clean(ctx.orgId));
  setIfMissing(parsed, ROOM_ID_PARAM_KEYS, clean(ctx.roomId));
  setIfMissing(parsed, SERVICE_KEY_PARAM_KEYS, clean(ctx.serviceKey));
  setIfMissing(parsed, CHURCH_SLUG_PARAM_KEYS, clean(ctx.churchSlug));
  if (extras) {
    for (const [key, val] of Object.entries(extras)) {
      const cleaned = clean(val);
      if (!cleaned) continue;
      if (!parsed.searchParams.has(key)) parsed.searchParams.set(key, cleaned);
    }
  }
  return parsed.toString();
}
