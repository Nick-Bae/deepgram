import { API_URL } from "../utils/urls";

export type OrgMembership = {
  orgId: string;
  slug: string;
  name: string;
  status?: string;
  role?: string;
  hostToken?: string | null;
};

export type AuthMeResponse = {
  user: {
    uid: string;
    email?: string | null;
    displayName?: string | null;
  };
  currentOrgId?: string | null;
  memberships: OrgMembership[];
};

export type BootstrapOwnerResponse = {
  created: boolean;
  org: OrgMembership;
  services: Array<{ serviceKey: string; title: string }>;
  hostToken?: string | null;
  memberships: OrgMembership[];
};

export type InviteRole = "admin" | "host" | "viewer";

export type InvitePreviewResponse = {
  inviteId: string;
  orgId: string;
  slug: string;
  name: string;
  role: InviteRole | string;
  status: string;
  expiresAt?: string | null;
  alreadyMember?: boolean;
};

export type InviteCreateResponse = InvitePreviewResponse & {
  code: string;
  createdAt?: string | null;
};

export type InviteRedeemResponse = {
  orgId: string;
  slug: string;
  name: string;
  role: InviteRole | string;
  created: boolean;
  alreadyMember: boolean;
  currentOrgId: string;
  hostToken?: string | null;
};

export type SetCurrentOrgResponse = {
  ok: boolean;
  currentOrgId: string;
};

export type OrgInviteSummary = {
  inviteId: string;
  orgId: string;
  slug: string;
  name: string;
  role: InviteRole | string;
  status: string;
  expiresAt?: string | null;
  createdBy?: string | null;
  createdAt?: string | null;
  consumedBy?: string | null;
  consumedAt?: string | null;
  revokedBy?: string | null;
  revokedAt?: string | null;
};

export type ListOrgInvitesResponse = {
  orgId: string;
  invites: OrgInviteSummary[];
};

export type OrgServiceSummary = {
  orgId: string;
  serviceKey: string;
  title: string;
  timezone?: string;
  activeRoomId?: string | null;
  roomStatus?: string;
  defaultLanguagePair?: { source?: string; target?: string };
};

export type DeleteOrgServiceResponse = {
  deleted: boolean;
  orgId: string;
  serviceKey: string;
};

export type OrgPromptResponse = {
  orgId: string;
  prompt: string;
  service_prompt: string;
  updatedAt?: string | null;
};

export type ScriptPairPayload = {
  source: string;
  target: string;
};

export type OrgScriptStatusResponse = {
  count: number;
  threshold: number;
  version: number;
};

export type OrgScriptUploadResponse = {
  loaded: number;
  threshold: number;
  version: number;
};

export type OrgScriptClearResponse = {
  cleared: boolean;
  removed: number;
  version: number;
};

export type SermonDraftSegment = {
  id: number;
  ko: string;
  en: string;
};

export type OrgSermonDraftResponse = {
  sermon_id: string;
  threshold: number;
  lang_src: string;
  lang_tgt: string;
  segments: SermonDraftSegment[];
};

export type OrgSermonFinalizeResponse = OrgSermonDraftResponse & {
  saved: boolean;
  loaded: number;
  version: number;
};

const AUTH_FETCH_TIMEOUT_MS = 15000;
const PROMPT_FETCH_TIMEOUT_MS = 30000;
const SERMON_FETCH_TIMEOUT_MS = 120000;
const AUTH_ME_CACHE_TTL_MS = 10000;

type AuthRequestInit = RequestInit & {
  timeoutMs?: number;
};

type AuthMeCacheEntry = {
  expiresAt: number;
  value: AuthMeResponse;
};

const authMeCache = new Map<string, AuthMeCacheEntry>();
const authMeInFlight = new Map<string, Promise<AuthMeResponse>>();

function invalidateAuthMeCache(idToken?: string): void {
  const token = (idToken || "").trim();
  if (!token) {
    authMeCache.clear();
    authMeInFlight.clear();
    return;
  }
  authMeCache.delete(token);
  authMeInFlight.delete(token);
}

function isAbortError(err: unknown): boolean {
  return typeof err === "object" && err !== null && "name" in err && String((err as { name?: string }).name) === "AbortError";
}

function toErrorMessage(status: number, detail: string | undefined): string {
  if (detail === "auth_required") return "Please sign in again.";
  if (detail === "invalid_id_token") return "Your session is invalid. Sign in again.";
  if (detail === "slug_taken") return "That church URL slug is already in use.";
  if (detail === "invalid_slug") return "Church slug is invalid.";
  if (detail === "invalid_name") return "Church name is required.";
  if (detail === "invalid_role") return "Invite role is invalid.";
  if (detail === "invalid_service_key") return "Service key is invalid. Use letters, numbers, and hyphens.";
  if (detail === "invalid_status") return "Invite status filter is invalid.";
  if (detail === "org_not_found") return "Church organization was not found.";
  if (detail === "org_access_denied") return "You do not have access to that church.";
  if (detail === "forbidden") return "You do not have permission to perform that action.";
  if (detail === "service_exists") return "That service key already exists.";
  if (detail === "service_active") return "You cannot delete a service while its room is live.";
  if (detail === "invite_active_limit_reached") return "Too many active invites for this church. Revoke or wait for some to expire.";
  if (detail === "invite_rate_limited") return "Too many invite requests right now. Please wait a moment and try again.";
  if (detail === "invite_not_found") return "Invite link is invalid.";
  if (detail === "invite_expired") return "Invite link has expired.";
  if (detail === "invite_invalid") return "Invite link has already been used or is no longer active.";
  if (detail) return detail;
  return `Request failed (HTTP ${status})`;
}

async function parseError(res: Response): Promise<Error> {
  try {
    const payload = await res.clone().json();
    const detail = typeof payload?.detail === "string" ? payload.detail : undefined;
    return new Error(toErrorMessage(res.status, detail));
  } catch {
    return new Error(toErrorMessage(res.status, undefined));
  }
}

async function authFetch<T>(path: string, idToken: string, init?: AuthRequestInit): Promise<T> {
  const timeoutMs =
    typeof init?.timeoutMs === "number" && Number.isFinite(init.timeoutMs) && init.timeoutMs > 0
      ? Math.floor(init.timeoutMs)
      : AUTH_FETCH_TIMEOUT_MS;
  const requestInit: RequestInit = { ...(init || {}) };
  if ("timeoutMs" in requestInit) {
    delete (requestInit as AuthRequestInit).timeoutMs;
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  if (requestInit?.signal) {
    if (requestInit.signal.aborted) controller.abort();
    requestInit.signal.addEventListener("abort", () => controller.abort(), { once: true });
  }
  try {
    const res = await fetch(`${API_URL}${path}`, {
      ...requestInit,
      signal: controller.signal,
      headers: {
        ...(requestInit.headers || {}),
        Authorization: `Bearer ${idToken}`,
      },
    });
    if (!res.ok) throw await parseError(res);
    return res.json() as Promise<T>;
  } catch (err: unknown) {
    if (isAbortError(err)) {
      throw new Error(`Request timed out after ${Math.floor(timeoutMs / 1000)}s. Check backend API at ${API_URL}.`);
    }
    if (err instanceof TypeError) {
      throw new Error(`Cannot reach backend API at ${API_URL}.`);
    }
    throw err;
  } finally {
    clearTimeout(timeout);
  }
}

export function fetchAuthMe(idToken: string): Promise<AuthMeResponse> {
  const token = (idToken || "").trim();
  if (!token) return authFetch<AuthMeResponse>("/api/auth/me", idToken, { method: "GET" });

  const cached = authMeCache.get(token);
  const now = Date.now();
  if (cached && cached.expiresAt > now) return Promise.resolve(cached.value);

  const inFlight = authMeInFlight.get(token);
  if (inFlight) return inFlight;

  const request = authFetch<AuthMeResponse>("/api/auth/me", token, { method: "GET" })
    .then((payload) => {
      authMeCache.set(token, { value: payload, expiresAt: Date.now() + AUTH_ME_CACHE_TTL_MS });
      return payload;
    })
    .finally(() => {
      authMeInFlight.delete(token);
    });
  authMeInFlight.set(token, request);
  return request;
}

export function bootstrapOwnerOrg(
  idToken: string,
  payload: { churchName: string; churchSlug: string; timezone?: string; source?: string; target?: string },
): Promise<BootstrapOwnerResponse> {
  return authFetch<BootstrapOwnerResponse>("/api/auth/bootstrap-owner", idToken, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      churchName: payload.churchName,
      churchSlug: payload.churchSlug,
      timezone: payload.timezone || "America/Chicago",
      source: payload.source || "ko",
      target: payload.target || "en",
    }),
  }).then((result) => {
    invalidateAuthMeCache(idToken);
    return result;
  });
}

export function setCurrentOrg(idToken: string, orgId: string): Promise<SetCurrentOrgResponse> {
  return authFetch<SetCurrentOrgResponse>("/api/auth/current-org", idToken, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ orgId }),
  }).then((result) => {
    invalidateAuthMeCache(idToken);
    return result;
  });
}

export function createOrgInvite(
  idToken: string,
  orgId: string,
  payload?: { role?: InviteRole; expiresHours?: number },
): Promise<InviteCreateResponse> {
  return authFetch<InviteCreateResponse>(`/api/auth/org/${encodeURIComponent(orgId)}/invites`, idToken, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      role: payload?.role || "host",
      expiresHours: payload?.expiresHours ?? 24 * 3,
    }),
  });
}

export function previewOrgInvite(idToken: string, code: string): Promise<InvitePreviewResponse> {
  return authFetch<InvitePreviewResponse>(`/api/auth/invites/${encodeURIComponent(code)}/preview`, idToken, {
    method: "GET",
  });
}

export function redeemOrgInvite(idToken: string, code: string): Promise<InviteRedeemResponse> {
  return authFetch<InviteRedeemResponse>(`/api/auth/invites/${encodeURIComponent(code)}/redeem`, idToken, {
    method: "POST",
  }).then((result) => {
    invalidateAuthMeCache(idToken);
    return result;
  });
}

export function listOrgInvites(
  idToken: string,
  orgId: string,
  status: "active" | "consumed" | "expired" | "revoked" | "" = "active",
): Promise<ListOrgInvitesResponse> {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  const query = params.toString();
  const path = `/api/auth/org/${encodeURIComponent(orgId)}/invites${query ? `?${query}` : ""}`;
  return authFetch<ListOrgInvitesResponse>(path, idToken, { method: "GET" });
}

export function revokeOrgInvite(idToken: string, orgId: string, inviteId: string): Promise<OrgInviteSummary> {
  return authFetch<OrgInviteSummary>(
    `/api/auth/org/${encodeURIComponent(orgId)}/invites/${encodeURIComponent(inviteId)}/revoke`,
    idToken,
    { method: "POST" },
  );
}

export function createOrgService(
  idToken: string,
  orgId: string,
  payload: { serviceKey: string; title?: string; timezone?: string; source?: string; target?: string },
): Promise<OrgServiceSummary> {
  return authFetch<OrgServiceSummary>(`/api/org/${encodeURIComponent(orgId)}/services`, idToken, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      serviceKey: payload.serviceKey,
      title: payload.title,
      timezone: payload.timezone,
      source: payload.source || "ko",
      target: payload.target || "en",
    }),
  });
}

export function deleteOrgService(idToken: string, orgId: string, serviceKey: string): Promise<DeleteOrgServiceResponse> {
  return authFetch<DeleteOrgServiceResponse>(
    `/api/org/${encodeURIComponent(orgId)}/services/${encodeURIComponent(serviceKey)}`,
    idToken,
    { method: "DELETE" },
  );
}

export function fetchOrgPrompt(idToken: string, orgId: string): Promise<OrgPromptResponse> {
  return authFetch<OrgPromptResponse>(`/api/org/${encodeURIComponent(orgId)}/prompt`, idToken, {
    method: "GET",
    timeoutMs: PROMPT_FETCH_TIMEOUT_MS,
  });
}

export function saveOrgPrompt(
  idToken: string,
  orgId: string,
  payload: { prompt: string; service_prompt: string },
): Promise<OrgPromptResponse> {
  return authFetch<OrgPromptResponse>(`/api/org/${encodeURIComponent(orgId)}/prompt`, idToken, {
    method: "POST",
    timeoutMs: PROMPT_FETCH_TIMEOUT_MS,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt: payload.prompt || "",
      service_prompt: payload.service_prompt || "",
    }),
  });
}

export function fetchOrgScriptStatus(idToken: string, orgId: string): Promise<OrgScriptStatusResponse> {
  return authFetch<OrgScriptStatusResponse>(`/api/org/${encodeURIComponent(orgId)}/script`, idToken, {
    method: "GET",
  });
}

export function uploadOrgScript(
  idToken: string,
  orgId: string,
  payload: { pairs: ScriptPairPayload[]; threshold?: number },
): Promise<OrgScriptUploadResponse> {
  return authFetch<OrgScriptUploadResponse>(`/api/org/${encodeURIComponent(orgId)}/script/upload`, idToken, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      payload: { pairs: payload.pairs || [] },
      cfg: payload.threshold === undefined ? undefined : { threshold: payload.threshold },
    }),
  });
}

export function clearOrgScript(idToken: string, orgId: string): Promise<OrgScriptClearResponse> {
  return authFetch<OrgScriptClearResponse>(`/api/org/${encodeURIComponent(orgId)}/script`, idToken, {
    method: "DELETE",
  });
}

export function draftOrgSermon(
  idToken: string,
  orgId: string,
  payload: {
    sermon_id: string;
    korean: string;
    auto_split?: boolean;
    threshold?: number;
    lang_src?: string;
    lang_tgt?: string;
  },
): Promise<OrgSermonDraftResponse> {
  return authFetch<OrgSermonDraftResponse>(`/api/org/${encodeURIComponent(orgId)}/sermon/draft`, idToken, {
    method: "POST",
    timeoutMs: SERMON_FETCH_TIMEOUT_MS,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      sermon_id: payload.sermon_id,
      korean: payload.korean,
      auto_split: payload.auto_split !== false,
      threshold: payload.threshold ?? 0.8,
      lang_src: payload.lang_src || "ko",
      lang_tgt: payload.lang_tgt || "en",
    }),
  });
}

export function finalizeOrgSermon(
  idToken: string,
  orgId: string,
  payload: {
    sermon_id: string;
    threshold?: number;
    lang_src?: string;
    lang_tgt?: string;
    segments: SermonDraftSegment[];
  },
): Promise<OrgSermonFinalizeResponse> {
  return authFetch<OrgSermonFinalizeResponse>(`/api/org/${encodeURIComponent(orgId)}/sermon/finalize`, idToken, {
    method: "POST",
    timeoutMs: SERMON_FETCH_TIMEOUT_MS,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      sermon_id: payload.sermon_id,
      threshold: payload.threshold ?? 0.8,
      lang_src: payload.lang_src || "ko",
      lang_tgt: payload.lang_tgt || "en",
      segments: payload.segments || [],
    }),
  });
}
