import { API_URL } from "../utils/urls";

export type OrgMembership = {
  orgId: string;
  slug: string;
  name: string;
  status?: string;
  role?: string;
  billingLimitsEnabled?: boolean;
};

export type AuthMeResponse = {
  user: {
    uid: string;
    email?: string | null;
    displayName?: string | null;
    isMaster?: boolean;
  };
  currentOrgId?: string | null;
  memberships: OrgMembership[];
};

export type BootstrapOwnerResponse = {
  created: boolean;
  org: OrgMembership;
  services: Array<{ serviceKey: string; title: string }>;
  memberships: OrgMembership[];
};

export type ChurchSlugAvailabilityResponse = {
  slug: string;
  available: boolean;
  suggestions: string[];
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

export type OrgProfileResponse = {
  orgId: string;
  slug: string;
  name: string;
};

export type OrgPromptResponse = {
  orgId: string;
  prompt: string;
  service_prompt: string;
  updatedAt?: string | null;
};

export type OrgBillingLimitsResponse = {
  orgId: string;
  billingLimitsEnabled: boolean;
  globalBillingLimitsEnabled: boolean;
  effectiveBillingLimitsEnabled: boolean;
  hardCapReached: boolean;
  maxMinutesPerMonth: number;
  currentMonthMinutes: number;
  currentMonthKey: string;
  updatedAt?: string | null;
};

export type BillingPlanKey = "trial" | "starter" | "growth" | "premium";

export type OrgBillingStatus = {
  version?: number;
  provider?: string;
  planKey: BillingPlanKey | string;
  status: string;
  trialEndsAt?: string | null;
  currentPeriodStart?: string | null;
  currentPeriodEnd?: string | null;
  graceEndsAt?: string | null;
  cancelAtPeriodEnd?: boolean;
  stripeCustomerId?: string | null;
  stripeSubscriptionId?: string | null;
  priceId?: string | null;
  trialMinutesLimit?: number;
  trialMinutesUsed?: number;
  trialSecondsUsed?: number;
  trialSecondsRemaining?: number;
  limits?: { maxServiceKeys?: number };
  entitlements?: { canStartService?: boolean };
  updatedAt?: string | null;
};

export type OrgBillingStatusResponse = {
  orgId: string;
  billing: OrgBillingStatus;
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
  usage?: OrgSermonUsageResponse;
};

export type OrgSermonFinalizeResponse = OrgSermonDraftResponse & {
  saved: boolean;
  loaded: number;
  version: number;
};

export type SermonUsageRow = {
  sermonId: string;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  estimatedUsd: number;
  requestsCount: number;
  lastModel?: string;
  lastUsedAt?: string | null;
};

export type OrgSermonUsageResponse = {
  orgId: string;
  currentMonthKey: string;
  budgetUsd: number;
  globalBudgetsEnabled: boolean;
  effectiveBudgetEnabled: boolean;
  capReached: boolean;
  remainingBudgetUsd: number;
  currentMonthEstimatedUsd: number;
  currentMonthPromptTokens: number;
  currentMonthCompletionTokens: number;
  currentMonthTotalTokens: number;
  requestsCount: number;
  inputCostPerMillionTokens: number;
  outputCostPerMillionTokens: number;
  sermons: SermonUsageRow[];
};

const AUTH_FETCH_TIMEOUT_MS = 15000;
const SLUG_FETCH_TIMEOUT_MS = 8000;
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
  if (detail === "anonymous_auth_disabled") return "Anonymous access is disabled. Please create an account and sign in.";
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
  if (detail === "billing_admin_required") return "Only the configured billing admin account can manage billing settings.";
  if (detail === "billing_not_configured") return "Billing is not configured yet. Ask the app owner to set Stripe keys.";
  if (detail === "billing_customer_not_found") return "No billing customer exists yet for this church.";
  if (detail === "invalid_plan") return "Selected billing plan is invalid.";
  if (detail === "sermon_prep_budget_reached") return "Sermon Prep monthly budget cap reached for this church.";
  if (detail === "invalid_budget") return "Budget must be a non-negative amount.";
  if (detail === "service_exists") return "That service key already exists.";
  if (detail === "plan_limit_reached") return "Your current plan has reached its service limit. Upgrade to add more services.";
  if (detail === "trial_expired") return "Your trial has expired. Add billing to continue.";
  if (detail === "grace_expired") return "Your billing grace period has ended. Update payment to continue.";
  if (detail === "subscription_required") return "An active subscription is required for this action.";
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

export async function checkChurchSlugAvailability(slug: string): Promise<ChurchSlugAvailabilityResponse> {
  const cleanSlug = (slug || "").trim();
  if (!cleanSlug) {
    return { slug: "", available: false, suggestions: [] };
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), SLUG_FETCH_TIMEOUT_MS);
  try {
    const params = new URLSearchParams();
    params.set("slug", cleanSlug);
    const res = await fetch(`${API_URL}/api/auth/slug-availability?${params.toString()}`, {
      method: "GET",
      signal: controller.signal,
    });
    if (!res.ok) throw await parseError(res);
    const payload = (await res.json()) as Partial<ChurchSlugAvailabilityResponse>;
    const suggestions = Array.isArray(payload.suggestions)
      ? payload.suggestions.map((row) => String(row || "").trim()).filter((row) => Boolean(row))
      : [];
    return {
      slug: String(payload.slug || cleanSlug),
      available: Boolean(payload.available),
      suggestions,
    };
  } catch (err: unknown) {
    if (isAbortError(err)) {
      throw new Error(`Request timed out after ${Math.floor(SLUG_FETCH_TIMEOUT_MS / 1000)}s. Check backend API at ${API_URL}.`);
    }
    if (err instanceof TypeError) {
      throw new Error(`Cannot reach backend API at ${API_URL}.`);
    }
    throw err;
  } finally {
    clearTimeout(timeout);
  }
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

export function fetchOrgBillingLimits(idToken: string, orgId: string): Promise<OrgBillingLimitsResponse> {
  return authFetch<OrgBillingLimitsResponse>(`/api/auth/org/${encodeURIComponent(orgId)}/billing-limits`, idToken, {
    method: "GET",
  });
}

export function fetchOrgBillingStatus(
  idToken: string,
  orgId: string,
  options?: { refresh?: boolean },
): Promise<OrgBillingStatusResponse> {
  const params = new URLSearchParams();
  if (options?.refresh) params.set("refresh", "true");
  const query = params.toString();
  return authFetch<OrgBillingStatusResponse>(
    `/api/billing/org/${encodeURIComponent(orgId)}/status${query ? `?${query}` : ""}`,
    idToken,
    {
      method: "GET",
      cache: "no-store",
    },
  );
}

export function saveOrgBillingLimits(
  idToken: string,
  orgId: string,
  enabled: boolean,
): Promise<OrgBillingLimitsResponse> {
  return authFetch<OrgBillingLimitsResponse>(`/api/auth/org/${encodeURIComponent(orgId)}/billing-limits`, idToken, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: Boolean(enabled) }),
  }).then((result) => {
    invalidateAuthMeCache(idToken);
    return result;
  });
}

export function createBillingCheckoutSession(
  idToken: string,
  payload: { orgId: string; planKey: Exclude<BillingPlanKey, "trial">; successUrl: string; cancelUrl: string },
): Promise<{ url: string; sessionId: string }> {
  return authFetch<{ url: string; sessionId: string }>("/api/billing/checkout-session", idToken, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      orgId: payload.orgId,
      planKey: payload.planKey,
      successUrl: payload.successUrl,
      cancelUrl: payload.cancelUrl,
    }),
  });
}

export function createBillingPortalSession(
  idToken: string,
  payload: { orgId: string; returnUrl: string },
): Promise<{ url: string }> {
  return authFetch<{ url: string }>("/api/billing/portal-session", idToken, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      orgId: payload.orgId,
      returnUrl: payload.returnUrl,
    }),
  });
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

export function saveOrgProfile(
  idToken: string,
  orgId: string,
  payload: { name: string },
): Promise<OrgProfileResponse> {
  return authFetch<OrgProfileResponse>(`/api/org/${encodeURIComponent(orgId)}/profile`, idToken, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: payload.name }),
  }).then((result) => {
    invalidateAuthMeCache(idToken);
    return result;
  });
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

export function fetchOrgSermonUsage(
  idToken: string,
  orgId: string,
  options?: { periodKey?: string; sermonId?: string; limit?: number },
): Promise<OrgSermonUsageResponse> {
  const params = new URLSearchParams();
  if (options?.periodKey) params.set("periodKey", options.periodKey);
  if (options?.sermonId) params.set("sermonId", options.sermonId);
  if (typeof options?.limit === "number" && Number.isFinite(options.limit)) {
    params.set("limit", String(Math.max(1, Math.floor(options.limit))));
  }
  const query = params.toString();
  return authFetch<OrgSermonUsageResponse>(
    `/api/org/${encodeURIComponent(orgId)}/sermon/usage${query ? `?${query}` : ""}`,
    idToken,
    { method: "GET" },
  );
}

export function saveOrgSermonBudget(idToken: string, orgId: string, budgetUsd: number): Promise<OrgSermonUsageResponse> {
  return authFetch<OrgSermonUsageResponse>(`/api/org/${encodeURIComponent(orgId)}/sermon/budget`, idToken, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ budget_usd: Math.max(0, Number.isFinite(budgetUsd) ? budgetUsd : 0) }),
  });
}
