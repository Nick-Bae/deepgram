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

function toErrorMessage(status: number, detail: string | undefined): string {
  if (detail === "auth_required") return "Please sign in again.";
  if (detail === "invalid_id_token") return "Your session is invalid. Sign in again.";
  if (detail === "slug_taken") return "That church URL slug is already in use.";
  if (detail === "invalid_slug") return "Church slug is invalid.";
  if (detail === "invalid_name") return "Church name is required.";
  if (detail === "invalid_role") return "Invite role is invalid.";
  if (detail === "invalid_status") return "Invite status filter is invalid.";
  if (detail === "org_not_found") return "Church organization was not found.";
  if (detail === "org_access_denied") return "You do not have access to that church.";
  if (detail === "forbidden") return "You do not have permission to perform that action.";
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

async function authFetch<T>(path: string, idToken: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      ...(init?.headers || {}),
      Authorization: `Bearer ${idToken}`,
    },
  });
  if (!res.ok) throw await parseError(res);
  return res.json() as Promise<T>;
}

export function fetchAuthMe(idToken: string): Promise<AuthMeResponse> {
  return authFetch<AuthMeResponse>("/api/auth/me", idToken, { method: "GET" });
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
  });
}

export function setCurrentOrg(idToken: string, orgId: string): Promise<SetCurrentOrgResponse> {
  return authFetch<SetCurrentOrgResponse>("/api/auth/current-org", idToken, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ orgId }),
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
      expiresHours: payload?.expiresHours ?? 24 * 7,
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
