import type { AuthMeResponse, OrgMembership } from "./backendAuth";
import { clearHostToken, persistStreamContext } from "../utils/streamContext";

export function pickPreferredMembership(me: AuthMeResponse): OrgMembership | undefined {
  const memberships = me.memberships || [];
  const preferredOrgId = (me.currentOrgId || "").trim();
  return memberships.find((row) => row.orgId === preferredOrgId) || memberships[0];
}

export function buildDashboardHref(membership: OrgMembership): string {
  const params = new URLSearchParams();
  params.set("orgId", membership.orgId);
  return `/host/c/${encodeURIComponent(membership.slug)}/broadcast?${params.toString()}`;
}

export function persistDashboardContext(membership: OrgMembership): void {
  clearHostToken();
  persistStreamContext({
    orgId: membership.orgId,
    roomId: undefined,
    serviceKey: undefined,
    churchSlug: membership.slug,
  });
}
