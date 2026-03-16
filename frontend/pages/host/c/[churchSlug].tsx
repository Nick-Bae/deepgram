import { useRouter } from "next/router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import TranslationBox from "../../../components/TranslationBox";
import { useAuth } from "../../../lib/authContext";
import {
  createBillingCheckoutSession,
  createBillingPortalSession,
  createOrgService,
  createOrgInvite,
  deleteOrgService,
  fetchAuthMe,
  fetchOrgBillingLimits,
  fetchOrgBillingStatus,
  fetchOrgSermonUsage,
  listOrgInvites,
  revokeOrgInvite,
  saveOrgProfile,
  saveOrgBillingLimits,
  saveOrgSermonBudget,
  setCurrentOrg,
  type BillingPlanKey,
  type InviteRole,
  type OrgBillingStatus,
  type OrgBillingLimitsResponse,
  type OrgInviteSummary,
  type OrgMembership,
  type OrgSermonUsageResponse,
} from "../../../lib/backendAuth";
import { API_URL } from "../../../utils/urls";
import { clearAuthToken, clearHostToken, clearRoomInSession, persistAuthToken, persistHostToken, persistStreamContext } from "../../../utils/streamContext";

type ServiceRow = {
  serviceKey: string;
  title: string;
  timezone?: string;
  activeRoomId?: string | null;
  roomStatus?: string;
  defaultLanguagePair?: { source?: string; target?: string };
};

type ServicesResponse = {
  orgId: string;
  slug: string;
  name: string;
  services: ServiceRow[];
};

type StartResponse = {
  orgId: string;
  serviceKey: string;
  roomId: string;
  status: string;
  languagePair?: { source?: string; target?: string };
};

const POLL_MS = 12000;
const DEFAULT_SERVICE_KEY = "sun-11am";
const BILLING_ADMIN_EMAILS = new Set(
  `${process.env.NEXT_PUBLIC_BILLING_ADMIN_EMAILS || ""},${process.env.NEXT_PUBLIC_BILLING_ADMIN_EMAIL || ""}`
    .split(",")
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean),
);

type HostAction = "load_services" | "start_service" | "end_service";
type InviteRoleChoice = Extract<InviteRole, "admin" | "host">;
type PaidPlanKey = Exclude<BillingPlanKey, "trial">;
type HostTab = "broadcast" | "settings" | "team";
const PAID_PLAN_KEYS: PaidPlanKey[] = ["starter", "growth", "premium"];

const PLAN_LABELS: Record<PaidPlanKey, string> = {
  starter: "Starter (5 services / $20)",
  growth: "Growth (12 services / $40)",
  premium: "Premium (Unlimited / $60)",
};

function resolveHostTab(raw: string): HostTab {
  const token = (raw || "").trim().toLowerCase();
  if (token === "settings" || token === "team") return token;
  return "broadcast";
}

const ERROR_DETAIL_MESSAGES: Record<string, string> = {
  host_auth_failed: "Host authorization failed. Sign in with an owner/admin/host account.",
  auth_required: "Please sign in first.",
  anonymous_auth_disabled: "Anonymous access is disabled. Please create an account and sign in.",
  invalid_id_token: "Session expired. Please sign in again.",
  hard_cap_reached: "Monthly plan limit reached for this church. Please upgrade or wait for reset.",
  plan_limit_reached: "Service limit reached for this plan. Upgrade to add another service.",
  trial_expired: "Trial period has ended. Add billing to continue broadcasting.",
  grace_expired: "Billing grace period has ended. Update payment to continue broadcasting.",
  subscription_required: "An active subscription is required for this action.",
  concurrency_limit_reached: "Another service is already live for this plan. End it first, then start this one.",
  org_inactive: "This church account is inactive. Check subscription or billing status.",
  org_not_found: "Church organization was not found.",
  service_not_found: "This service key was not found for the church.",
  service_exists: "That service key already exists.",
  service_active: "You cannot delete a service while a room is live.",
  billing_not_configured: "Billing is not configured yet. Ask the app owner to configure Stripe.",
  billing_customer_not_found: "No billing customer exists yet for this church.",
  invalid_plan: "Selected billing plan is invalid.",
  invalid_service_key: "Service key is invalid. Use letters, numbers, and hyphens.",
  room_not_found: "Live room was not found. Refresh and try again.",
};

function fallbackMessage(action: HostAction): string {
  if (action === "load_services") return "Failed to load services.";
  if (action === "start_service") return "Failed to start service.";
  return "Failed to end service.";
}

function mapStatusMessage(status: number, action: HostAction): string | null {
  if (status === 402) return "Billing limit reached. Upgrade or update billing to continue.";
  if (status === 403) return ERROR_DETAIL_MESSAGES.host_auth_failed;
  if (status === 404 && action === "load_services") return ERROR_DETAIL_MESSAGES.org_not_found;
  if (status === 404 && action === "start_service") return ERROR_DETAIL_MESSAGES.service_not_found;
  if (status === 404 && action === "end_service") return ERROR_DETAIL_MESSAGES.room_not_found;
  if (status === 429) return ERROR_DETAIL_MESSAGES.concurrency_limit_reached;
  if (status === 401) return ERROR_DETAIL_MESSAGES.auth_required;
  return null;
}

async function readErrorMessage(res: Response, action: HostAction): Promise<string> {
  try {
    const data = await res.clone().json();
    const detail = typeof data?.detail === "string" ? data.detail : "";
    if (detail && ERROR_DETAIL_MESSAGES[detail]) return ERROR_DETAIL_MESSAGES[detail];
    if (detail) return detail;
  } catch {}
  return mapStatusMessage(res.status, action) || `${fallbackMessage(action)} (HTTP ${res.status})`;
}

function formatDateTime(raw?: string | null): string {
  const txt = (raw || "").trim();
  if (!txt) return "-";
  const parsed = new Date(txt);
  if (Number.isNaN(parsed.getTime())) return txt;
  return parsed.toLocaleString();
}

function formatPlanLabel(planKey: string): string {
  const token = (planKey || "").trim().toLowerCase();
  if (token === "trial") return "Trial";
  if (token === "starter") return "Starter";
  if (token === "growth") return "Growth";
  if (token === "premium") return "Premium";
  return token || "Unknown";
}

function formatBillingStatus(status: string): string {
  const token = (status || "").trim().toLowerCase();
  if (!token) return "unknown";
  return token.replace(/_/g, " ");
}

function formatCountdownMinutes(rawMinutes: number): string {
  const minutes = Math.max(0, Math.floor(Number.isFinite(rawMinutes) ? rawMinutes : 0));
  const totalSeconds = minutes * 60;
  const mm = Math.floor(totalSeconds / 60);
  const ss = totalSeconds % 60;
  return `${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
}

async function copyTextToClipboard(value: string): Promise<void> {
  const text = value.trim();
  if (!text) throw new Error("empty_text");
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  if (typeof document === "undefined") throw new Error("clipboard_unavailable");

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.top = "-9999px";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  const copied = document.execCommand("copy");
  document.body.removeChild(textarea);
  if (!copied) throw new Error("clipboard_unavailable");
}

export default function HostChurchPage() {
  const router = useRouter();
  const { user, loading: authLoading, getIdToken, logout, updateDisplayName } = useAuth();
  const slug = typeof router.query.churchSlug === "string" ? router.query.churchSlug : "";
  const querySection = typeof router.query.section === "string" ? router.query.section : "";
  const queryServiceKey = typeof router.query.serviceKey === "string" ? router.query.serviceKey : "";
  const queryHostToken = typeof router.query.hostToken === "string" ? router.query.hostToken : "";
  const queryOrgId = typeof router.query.orgId === "string" ? router.query.orgId : "";
  const queryRoomId = typeof router.query.roomId === "string" ? router.query.roomId : "";

  const [origin, setOrigin] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [orgData, setOrgData] = useState<ServicesResponse | null>(null);
  const [serviceKey, setServiceKey] = useState("");
  const [sourceLang, setSourceLang] = useState("ko");
  const [targetLang, setTargetLang] = useState("en");
  const [activeRoomId, setActiveRoomId] = useState<string | null>(null);
  const [memberships, setMemberships] = useState<OrgMembership[]>([]);
  const [switchingOrg, setSwitchingOrg] = useState(false);
  const [selectedOrgId, setSelectedOrgId] = useState("");
  const [membershipRole, setMembershipRole] = useState("");
  const [isMasterUser, setIsMasterUser] = useState(false);
  const [inviteRole, setInviteRole] = useState<InviteRoleChoice>("host");
  const [inviteBusy, setInviteBusy] = useState(false);
  const [inviteLink, setInviteLink] = useState("");
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [inviteNotice, setInviteNotice] = useState<string | null>(null);
  const [inviteRows, setInviteRows] = useState<OrgInviteSummary[]>([]);
  const [invitesLoading, setInvitesLoading] = useState(false);
  const [revokingInviteId, setRevokingInviteId] = useState("");
  const [copyBusy, setCopyBusy] = useState(false);
  const [shareBusy, setShareBusy] = useState(false);
  const [newServiceKey, setNewServiceKey] = useState("");
  const [newServiceTitle, setNewServiceTitle] = useState("");
  const [serviceManageBusy, setServiceManageBusy] = useState(false);
  const [serviceManageError, setServiceManageError] = useState<string | null>(null);
  const [deletingServiceKey, setDeletingServiceKey] = useState("");
  const [billingState, setBillingState] = useState<OrgBillingLimitsResponse | null>(null);
  const [billingProfile, setBillingProfile] = useState<OrgBillingStatus | null>(null);
  const [accountDisplayNameInput, setAccountDisplayNameInput] = useState("");
  const [accountProfileBusy, setAccountProfileBusy] = useState(false);
  const [accountProfileError, setAccountProfileError] = useState<string | null>(null);
  const [accountProfileNotice, setAccountProfileNotice] = useState<string | null>(null);
  const [churchNameInput, setChurchNameInput] = useState("");
  const [churchProfileBusy, setChurchProfileBusy] = useState(false);
  const [churchProfileError, setChurchProfileError] = useState<string | null>(null);
  const [churchProfileNotice, setChurchProfileNotice] = useState<string | null>(null);
  const [selectedPlan, setSelectedPlan] = useState<PaidPlanKey>("starter");
  const [billingBusy, setBillingBusy] = useState(false);
  const [billingCheckoutBusy, setBillingCheckoutBusy] = useState(false);
  const [billingPortalBusy, setBillingPortalBusy] = useState(false);
  const [billingRefreshBusy, setBillingRefreshBusy] = useState(false);
  const [billingError, setBillingError] = useState<string | null>(null);
  const [billingNotice, setBillingNotice] = useState<string | null>(null);
  const [trialBroadcastNotice, setTrialBroadcastNotice] = useState<string | null>(null);
  const [sermonUsageState, setSermonUsageState] = useState<OrgSermonUsageResponse | null>(null);
  const [sermonBudgetInput, setSermonBudgetInput] = useState("0");
  const [sermonBudgetBusy, setSermonBudgetBusy] = useState(false);
  const [sermonBudgetError, setSermonBudgetError] = useState<string | null>(null);
  const [sermonBudgetNotice, setSermonBudgetNotice] = useState<string | null>(null);
  const normalizedServiceKey = serviceKey.trim();
  const activeTab = resolveHostTab(querySection);
  const resolvedOrgId = (orgData?.orgId || queryOrgId || "").trim();
  const canManageInvites = useMemo(() => {
    const lowered = membershipRole.trim().toLowerCase();
    return lowered === "owner" || lowered === "admin";
  }, [membershipRole]);
  const billingAdminEmailAllowed = useMemo(() => {
    if (BILLING_ADMIN_EMAILS.size === 0) return true;
    const email = (user?.email || "").trim().toLowerCase();
    if (!email) return false;
    return BILLING_ADMIN_EMAILS.has(email);
  }, [user?.email]);
  const canManageBilling = useMemo(() => {
    return isMasterUser && billingAdminEmailAllowed;
  }, [billingAdminEmailAllowed, isMasterUser]);
  const canManageServices = useMemo(() => {
    const lowered = membershipRole.trim().toLowerCase();
    return lowered === "owner" || lowered === "admin" || lowered === "host";
  }, [membershipRole]);
  const canManagePaidBilling = useMemo(() => {
    const lowered = membershipRole.trim().toLowerCase();
    return lowered === "owner" || lowered === "admin";
  }, [membershipRole]);
  const billingStatusToken = (billingProfile?.status || "").trim().toLowerCase();
  const billingPlanToken = (billingProfile?.planKey || "trial").trim().toLowerCase();
  const isTrialPlan = billingPlanToken === "trial";
  const hasSubscriptionPeriod = Boolean(billingProfile?.currentPeriodStart && billingProfile?.currentPeriodEnd);
  const billingMaxServiceKeys = Number(billingProfile?.limits?.maxServiceKeys || 0);
  const trialMinutesLimit = Number(billingProfile?.trialMinutesLimit || 0);
  const trialMinutesUsed = Number(billingProfile?.trialMinutesUsed || 0);
  const trialMinutesRemaining = trialMinutesLimit > 0 ? Math.max(0, trialMinutesLimit - trialMinutesUsed) : null;
  const isTrialExpired = isTrialPlan && trialMinutesRemaining !== null && trialMinutesRemaining <= 0;
  const trialNoticeCheckpointRef = useRef<"" | "warn5" | "warn1" | "expired">("");
  const hasPaidPlan = PAID_PLAN_KEYS.includes(billingPlanToken as PaidPlanKey);
  const hasActiveLikeSubscription = billingStatusToken === "active" || billingStatusToken === "trialing" || billingStatusToken === "past_due";
  const selectablePaidPlans = useMemo(() => {
    if (!hasPaidPlan || !hasActiveLikeSubscription) return PAID_PLAN_KEYS;
    return PAID_PLAN_KEYS.filter((plan) => plan !== (billingPlanToken as PaidPlanKey));
  }, [billingPlanToken, hasActiveLikeSubscription, hasPaidPlan]);

  useEffect(() => {
    if (!resolvedOrgId) return;
    if (!memberships.length) {
      setSelectedOrgId(resolvedOrgId);
      return;
    }
    const matchedMembership = memberships.find((row) => row.orgId === resolvedOrgId || row.slug === slug);
    if (matchedMembership) setSelectedOrgId(matchedMembership.orgId);
  }, [memberships, resolvedOrgId, slug]);

  useEffect(() => {
    if (selectablePaidPlans.includes(selectedPlan)) return;
    setSelectedPlan(selectablePaidPlans[0] || "starter");
  }, [selectablePaidPlans, selectedPlan]);

  useEffect(() => {
    setInviteLink("");
    setInviteError(null);
    setInviteNotice(null);
    setInviteRows([]);
    setRevokingInviteId("");
    setCopyBusy(false);
    setShareBusy(false);
    setChurchProfileBusy(false);
    setChurchProfileError(null);
    setChurchProfileNotice(null);
    setNewServiceKey("");
    setNewServiceTitle("");
    setServiceManageError(null);
    setDeletingServiceKey("");
    setBillingState(null);
    setBillingProfile(null);
    setSelectedPlan("starter");
    setBillingBusy(false);
    setBillingCheckoutBusy(false);
    setBillingPortalBusy(false);
    setBillingRefreshBusy(false);
    setBillingError(null);
    setBillingNotice(null);
    setTrialBroadcastNotice(null);
    trialNoticeCheckpointRef.current = "";
    setSermonUsageState(null);
    setSermonBudgetInput("0");
    setSermonBudgetBusy(false);
    setSermonBudgetError(null);
    setSermonBudgetNotice(null);
  }, [resolvedOrgId]);

  useEffect(() => {
    if (authLoading) return;
    if (user) return;
    const nextPath = router.asPath || `/host/c/${encodeURIComponent(slug || "demo")}/broadcast`;
    router.replace(`/login?next=${encodeURIComponent(nextPath)}`);
  }, [authLoading, router, slug, user]);

  useEffect(() => {
    if (authLoading || !user) return;
    let cancelled = false;
    const cacheAuthToken = async () => {
      const idToken = await getIdToken();
      if (cancelled) return;
      persistAuthToken(idToken || undefined);
    };
    cacheAuthToken();
    return () => {
      cancelled = true;
    };
  }, [authLoading, getIdToken, user]);

  useEffect(() => {
    if (authLoading || !user) return;
    let cancelled = false;
    const hydrateMembershipToken = async () => {
      const idToken = await getIdToken();
      if (!idToken || cancelled) return;
      persistAuthToken(idToken);
      const me = await fetchAuthMe(idToken);
      if (cancelled) return;
      setIsMasterUser(Boolean(me.user?.isMaster));
      const preferredOrgId = (me.currentOrgId || "").trim();
      const rows = me.memberships || [];
      setMemberships(rows);
      const preferredMembership = rows.find((row) => row.orgId === preferredOrgId) || rows[0];
      const routeMembership = rows.find((row) => row.orgId === resolvedOrgId || row.slug === slug);
      const effectiveMembership = routeMembership || preferredMembership;
      setSelectedOrgId(effectiveMembership?.orgId || "");
      setMembershipRole((effectiveMembership?.role || "").trim());

      if (!preferredMembership) return;
      const cleanSlug = (slug || "").trim();
      const cleanQueryOrgId = (queryOrgId || "").trim();
      const preferredSlug = (preferredMembership.slug || "").trim();
      const preferredOrg = (preferredMembership.orgId || "").trim();
      if (!preferredSlug || !preferredOrg) return;
      if (cleanSlug === preferredSlug && cleanQueryOrgId === preferredOrg) return;

      persistStreamContext({
        orgId: preferredOrg,
        serviceKey: queryServiceKey || undefined,
        churchSlug: preferredSlug,
        roomId: undefined,
      });
      const params = new URLSearchParams();
      params.set("orgId", preferredOrg);
      if (queryServiceKey) params.set("serviceKey", queryServiceKey);
      const query = params.toString();
      await router.replace(
        `/host/c/${encodeURIComponent(preferredSlug)}/${activeTab}${query ? `?${query}` : ""}`,
      );
    };
    hydrateMembershipToken().catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [activeTab, authLoading, getIdToken, queryOrgId, queryServiceKey, resolvedOrgId, router, slug, user]);

  useEffect(() => {
    if (typeof window !== "undefined") setOrigin(window.location.origin);
  }, []);

  useEffect(() => {
    if (queryHostToken) {
      persistHostToken(queryHostToken);
    }
  }, [queryHostToken]);

  const refreshServices = useCallback(
    async (preferredServiceKey?: string) => {
      if (!slug) return;
      const res = await fetch(`${API_URL}/api/c/${encodeURIComponent(slug)}/services`);
      if (!res.ok) {
        const msg = await readErrorMessage(res, "load_services");
        throw new Error(msg);
      }
      const data: ServicesResponse = await res.json();
      setOrgData(data);

      const preferredKey = (preferredServiceKey || normalizedServiceKey || queryServiceKey || data.services[0]?.serviceKey || DEFAULT_SERVICE_KEY).trim();
      const selected = data.services.find((row) => row.serviceKey === preferredKey) || data.services[0];
      const selectedKey = (selected?.serviceKey || preferredKey || "").trim();
      if (selectedKey && selectedKey !== serviceKey) setServiceKey(selectedKey);

      const rowRoomId = selected?.activeRoomId || null;
      setActiveRoomId(rowRoomId);
      persistStreamContext({
        orgId: data.orgId,
        serviceKey: selectedKey || undefined,
        roomId: rowRoomId || undefined,
        churchSlug: slug,
      });
      if (!rowRoomId) clearRoomInSession();
      setErrorMsg(null);
    },
    [normalizedServiceKey, queryServiceKey, serviceKey, slug],
  );

  useEffect(() => {
    if (!slug) return;
    let disposed = false;

    const run = async () => {
      if (typeof document !== "undefined" && document.hidden) return;
      try {
        await refreshServices();
      } catch (err: unknown) {
        if (disposed) return;
        const message = err instanceof Error ? err.message : String(err);
        setErrorMsg(message || "services_failed");
      } finally {
        if (!disposed) setLoading(false);
      }
    };

    run();
    const timer = window.setInterval(run, POLL_MS);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, [refreshServices, slug]);

  const selectedService = useMemo(() => {
    if (!orgData) return null;
    return orgData.services.find((s) => s.serviceKey === serviceKey) || null;
  }, [orgData, serviceKey]);

  useEffect(() => {
    if (!selectedService) return;
    if (selectedService.defaultLanguagePair?.source) setSourceLang(selectedService.defaultLanguagePair.source);
    if (selectedService.defaultLanguagePair?.target) setTargetLang(selectedService.defaultLanguagePair.target);
  }, [selectedService?.serviceKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const listenerUrl = useMemo(() => {
    if (!origin || !slug || !normalizedServiceKey) return "";
    return `${origin}/c/${encodeURIComponent(slug)}/s/${encodeURIComponent(normalizedServiceKey)}`;
  }, [normalizedServiceKey, origin, slug]);

  const saveAccountProfile = useCallback(async () => {
    const nextName = accountDisplayNameInput.trim();
    if (nextName.length < 2) {
      setAccountProfileError("Display name must be at least 2 characters.");
      setAccountProfileNotice(null);
      return;
    }
    setAccountProfileBusy(true);
    setAccountProfileError(null);
    setAccountProfileNotice(null);
    try {
      await updateDisplayName(nextName);
      const freshToken = await getIdToken(true);
      if (freshToken) {
        persistAuthToken(freshToken);
        const me = await fetchAuthMe(freshToken);
        setIsMasterUser(Boolean(me.user?.isMaster));
        setMemberships(me.memberships || []);
      }
      setAccountProfileNotice("Display name updated.");
    } catch (err) {
      setAccountProfileError(err instanceof Error ? err.message : "Failed to update display name.");
    } finally {
      setAccountProfileBusy(false);
    }
  }, [accountDisplayNameInput, getIdToken, updateDisplayName]);

  const saveChurchProfile = useCallback(async () => {
    const nextName = churchNameInput.trim();
    if (nextName.length < 2) {
      setChurchProfileError("Church name must be at least 2 characters.");
      setChurchProfileNotice(null);
      return;
    }
    if (!resolvedOrgId) {
      setChurchProfileError("Church organization is missing.");
      setChurchProfileNotice(null);
      return;
    }
    setChurchProfileBusy(true);
    setChurchProfileError(null);
    setChurchProfileNotice(null);
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");
      persistAuthToken(idToken);
      const updated = await saveOrgProfile(idToken, resolvedOrgId, { name: nextName });
      setOrgData((current) => (current ? { ...current, name: updated.name } : current));
      setMemberships((rows) => rows.map((row) => (row.orgId === updated.orgId ? { ...row, name: updated.name } : row)));
      setChurchProfileNotice("Church name updated.");
    } catch (err) {
      setChurchProfileError(err instanceof Error ? err.message : "Failed to update church name.");
    } finally {
      setChurchProfileBusy(false);
    }
  }, [churchNameInput, getIdToken, resolvedOrgId]);

  const buildTabHref = useCallback(
    (
      tab: HostTab,
      options?: {
        serviceKey?: string;
        orgId?: string;
        roomId?: string | null;
        churchSlug?: string;
      },
    ) => {
      const churchSlug = (options?.churchSlug || slug || "").trim();
      if (!churchSlug) return "";
      const serviceToken = (options?.serviceKey || normalizedServiceKey || queryServiceKey || "").trim();
      const orgToken = (options?.orgId || orgData?.orgId || queryOrgId || "").trim();
      const hasExplicitRoom = Boolean(options && Object.prototype.hasOwnProperty.call(options, "roomId"));
      const roomCandidate = hasExplicitRoom ? options?.roomId : activeRoomId || queryRoomId || "";
      const roomToken = typeof roomCandidate === "string" ? roomCandidate.trim() : "";
      const params = new URLSearchParams();
      if (serviceToken) params.set("serviceKey", serviceToken);
      if (orgToken) params.set("orgId", orgToken);
      if (roomToken) params.set("roomId", roomToken);
      const query = params.toString();
      const base = `/host/c/${encodeURIComponent(churchSlug)}/${tab}`;
      return query ? `${base}?${query}` : base;
    },
    [activeRoomId, normalizedServiceKey, orgData?.orgId, queryOrgId, queryRoomId, queryServiceKey, slug],
  );

  const navigateToTab = useCallback(
    (tab: HostTab) => {
      const href = buildTabHref(tab);
      if (!href) return;
      void router.push(href);
    },
    [buildTabHref, router],
  );

  useEffect(() => {
    if (!router.isReady || !slug || querySection) return;
    const href = buildTabHref("broadcast");
    if (!href) return;
    void router.replace(href, undefined, { shallow: true });
  }, [buildTabHref, querySection, router, slug]);

  const syncHostUrl = (nextRoomId?: string | null, nextServiceKey?: string) => {
    const key = (nextServiceKey || normalizedServiceKey || queryServiceKey).trim();
    const effectiveOrgId = (orgData?.orgId || queryOrgId || "").trim();
    const href = buildTabHref(activeTab, {
      serviceKey: key || undefined,
      orgId: effectiveOrgId || undefined,
      roomId: nextRoomId ?? null,
    });
    if (!href) return;
    void router.replace(href, undefined, { shallow: true });
  };

  const switchOrganization = useCallback(
    async (nextOrgId: string) => {
      const cleanOrgId = (nextOrgId || "").trim();
      setSelectedOrgId(cleanOrgId);
      if (!cleanOrgId || cleanOrgId === resolvedOrgId) return;
      const targetMembership = memberships.find((row) => row.orgId === cleanOrgId);
      if (!targetMembership) return;
      setSwitchingOrg(true);
      setErrorMsg(null);
      try {
        const idToken = await getIdToken(true);
        if (!idToken) throw new Error("Please sign in again.");
        persistAuthToken(idToken);
        await setCurrentOrg(idToken, cleanOrgId);
        setMembershipRole((targetMembership.role || "").trim());
        persistStreamContext({
          orgId: cleanOrgId,
          serviceKey: normalizedServiceKey || undefined,
          churchSlug: targetMembership.slug,
          roomId: undefined,
        });
        const params = new URLSearchParams();
        params.set("orgId", cleanOrgId);
        if (normalizedServiceKey) params.set("serviceKey", normalizedServiceKey);
        const query = params.toString();
        await router.replace(
          `/host/c/${encodeURIComponent(targetMembership.slug)}/${activeTab}${query ? `?${query}` : ""}`,
        );
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : String(err);
        setErrorMsg(message || "Failed to switch church.");
      } finally {
        setSwitchingOrg(false);
      }
    },
    [activeTab, getIdToken, memberships, normalizedServiceKey, resolvedOrgId, router],
  );

  const loadInvites = useCallback(async () => {
    if (!resolvedOrgId || !canManageInvites) {
      setInviteRows([]);
      return;
    }
    const idToken = await getIdToken();
    if (!idToken) throw new Error("Please sign in again.");
    persistAuthToken(idToken);
    const listed = await listOrgInvites(idToken, resolvedOrgId, "active");
    setInviteRows(listed.invites || []);
  }, [canManageInvites, getIdToken, resolvedOrgId]);

  const loadBillingLimits = useCallback(async () => {
    if (!resolvedOrgId || !canManageBilling) {
      setBillingState(null);
      return;
    }
    const idToken = await getIdToken();
    if (!idToken) throw new Error("Please sign in again.");
    persistAuthToken(idToken);
    const payload = await fetchOrgBillingLimits(idToken, resolvedOrgId);
    setBillingState(payload);
  }, [canManageBilling, getIdToken, resolvedOrgId]);

  const loadBillingProfile = useCallback(async (options?: { refresh?: boolean }) => {
    if (!resolvedOrgId || !canManageServices) {
      setBillingProfile(null);
      return;
    }
    const forceRefresh = Boolean(options?.refresh);
    if (forceRefresh) {
      setBillingRefreshBusy(true);
      setBillingError(null);
      setBillingNotice(null);
    }
    try {
      const idToken = await getIdToken();
      if (!idToken) throw new Error("Please sign in again.");
      persistAuthToken(idToken);
      const payload = await fetchOrgBillingStatus(idToken, resolvedOrgId, { refresh: forceRefresh });
      setBillingProfile(payload.billing || null);
      if (forceRefresh) {
        setBillingNotice("Billing details refreshed.");
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      if (forceRefresh || activeTab === "settings") {
        setBillingError(message || "Failed to load billing profile.");
      }
    } finally {
      if (forceRefresh) setBillingRefreshBusy(false);
    }
  }, [activeTab, canManageServices, getIdToken, resolvedOrgId]);

  const loadSermonUsage = useCallback(async () => {
    if (!resolvedOrgId || !canManageBilling) {
      setSermonUsageState(null);
      return;
    }
    const idToken = await getIdToken();
    if (!idToken) throw new Error("Please sign in again.");
    persistAuthToken(idToken);
    const payload = await fetchOrgSermonUsage(idToken, resolvedOrgId, { limit: 8 });
    setSermonUsageState(payload);
    setSermonBudgetInput(String(payload.budgetUsd ?? 0));
  }, [canManageBilling, getIdToken, resolvedOrgId]);

  useEffect(() => {
    if (activeTab !== "settings" || !canManageBilling || !resolvedOrgId || authLoading || !user) return;
    let cancelled = false;
    const run = async () => {
      setBillingError(null);
      setSermonBudgetError(null);
      try {
        const idToken = await getIdToken();
        if (!idToken || cancelled) return;
        persistAuthToken(idToken);
        const [billingPayload, sermonPayload] = await Promise.all([
          fetchOrgBillingLimits(idToken, resolvedOrgId),
          fetchOrgSermonUsage(idToken, resolvedOrgId, { limit: 8 }),
        ]);
        if (cancelled) return;
        setBillingState(billingPayload);
        setSermonUsageState(sermonPayload);
        setSermonBudgetInput(String(sermonPayload.budgetUsd ?? 0));
      } catch (err: unknown) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : String(err);
          setBillingError(message || "Failed to load billing limits.");
          setSermonBudgetError(message || "Failed to load Sermon Prep usage.");
        }
      }
    };
    run();
    return () => {
      cancelled = true;
    };
  }, [activeTab, authLoading, canManageBilling, getIdToken, resolvedOrgId, user]);

  useEffect(() => {
    if (!canManageServices || !resolvedOrgId || authLoading || !user) return;
    let cancelled = false;
    const run = async () => {
      try {
        const idToken = await getIdToken();
        if (!idToken || cancelled) return;
        persistAuthToken(idToken);
        const payload = await fetchOrgBillingStatus(idToken, resolvedOrgId);
        if (cancelled) return;
        setBillingProfile(payload.billing || null);
      } catch (err: unknown) {
        if (!cancelled && activeTab === "settings") {
          const message = err instanceof Error ? err.message : String(err);
          setBillingError(message || "Failed to load billing profile.");
        }
      }
    };
    void run();
    const timer = window.setInterval(() => {
      void run();
    }, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [activeTab, authLoading, canManageServices, getIdToken, resolvedOrgId, user]);

  useEffect(() => {
    if (!canManageInvites || !resolvedOrgId || authLoading || !user) return;
    let cancelled = false;
    const run = async () => {
      setInvitesLoading(true);
      setInviteError(null);
      try {
        const idToken = await getIdToken();
        if (!idToken || cancelled) return;
        persistAuthToken(idToken);
        const listed = await listOrgInvites(idToken, resolvedOrgId, "active");
        if (cancelled) return;
        setInviteRows(listed.invites || []);
      } catch (err: unknown) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : String(err);
          setInviteError(message || "Failed to load invites.");
        }
      } finally {
        if (!cancelled) setInvitesLoading(false);
      }
    };
    run();
    return () => {
      cancelled = true;
    };
  }, [authLoading, canManageInvites, getIdToken, resolvedOrgId, user]);

  useEffect(() => {
    if (billingPlanToken !== "trial" || trialMinutesRemaining === null) {
      trialNoticeCheckpointRef.current = "";
      setTrialBroadcastNotice(null);
      return;
    }
    if (trialMinutesRemaining <= 0) {
      if (trialNoticeCheckpointRef.current !== "expired") {
        trialNoticeCheckpointRef.current = "expired";
        setTrialBroadcastNotice("Your 30-minute trial has ended. Upgrade to continue broadcasting.");
      }
      return;
    }
    if (trialMinutesRemaining <= 1) {
      if (trialNoticeCheckpointRef.current !== "warn1" && trialNoticeCheckpointRef.current !== "expired") {
        trialNoticeCheckpointRef.current = "warn1";
        setTrialBroadcastNotice("Trial: 1 minute remaining. Broadcast will stop automatically when time runs out.");
      }
      return;
    }
    if (trialMinutesRemaining <= 5) {
      if (trialNoticeCheckpointRef.current === "") {
        trialNoticeCheckpointRef.current = "warn5";
        setTrialBroadcastNotice("Trial: 5 minutes remaining. Upgrade anytime to avoid interruption.");
      }
      return;
    }
    if (trialNoticeCheckpointRef.current) {
      trialNoticeCheckpointRef.current = "";
      setTrialBroadcastNotice(null);
    }
  }, [billingPlanToken, trialMinutesRemaining]);

  const generateInviteLink = async () => {
    if (!resolvedOrgId) {
      setInviteError("Organization is not loaded yet.");
      return;
    }
    setInviteBusy(true);
    setInviteError(null);
    setInviteNotice(null);
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");
      persistAuthToken(idToken);
      const created = await createOrgInvite(idToken, resolvedOrgId, { role: inviteRole, expiresHours: 24 * 3 });
      const originBase = typeof window !== "undefined" ? window.location.origin : "";
      const link = originBase ? `${originBase}/join?code=${encodeURIComponent(created.code)}` : `/join?code=${encodeURIComponent(created.code)}`;
      setInviteLink(link);
      setInviteNotice("Invite link created. Use Copy Link or Share via...");
      await loadInvites();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setInviteError(message || "Failed to create invite link.");
    } finally {
      setInviteBusy(false);
    }
  };

  const copyInviteLink = async () => {
    const link = inviteLink.trim();
    if (!link) return;
    setCopyBusy(true);
    setInviteError(null);
    setInviteNotice(null);
    try {
      await copyTextToClipboard(link);
      setInviteNotice("Invite link copied.");
    } catch {
      setInviteError("Could not copy the invite link. Copy it manually from the URL.");
    } finally {
      setCopyBusy(false);
    }
  };

  const shareInviteLink = async () => {
    const link = inviteLink.trim();
    if (!link) return;
    setShareBusy(true);
    setInviteError(null);
    setInviteNotice(null);
    try {
      const shareText = `Join ${orgData?.name || "our church"} translation team.`;
      if (typeof navigator !== "undefined" && typeof navigator.share === "function") {
        await navigator.share({
          title: "Church Team Invite",
          text: shareText,
          url: link,
        });
        setInviteNotice("Share menu opened.");
        return;
      }
      await copyTextToClipboard(link);
      setInviteNotice("Share is not available here. Link copied instead.");
    } catch (err: unknown) {
      const name = err instanceof Error ? err.name : "";
      if (name === "AbortError") {
        setInviteNotice("Share canceled.");
      } else {
        setInviteError("Could not open share. Use Copy Link instead.");
      }
    } finally {
      setShareBusy(false);
    }
  };

  const revokeInvite = async (inviteId: string) => {
    if (!resolvedOrgId) return;
    if (typeof window !== "undefined") {
      const ok = window.confirm("Revoke this invite? Anyone with this link will no longer be able to join.");
      if (!ok) return;
    }
    setRevokingInviteId(inviteId);
    setInviteError(null);
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");
      persistAuthToken(idToken);
      await revokeOrgInvite(idToken, resolvedOrgId, inviteId);
      await loadInvites();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setInviteError(message || "Failed to revoke invite.");
    } finally {
      setRevokingInviteId("");
    }
  };

  const toggleBillingLimits = async () => {
    if (!resolvedOrgId || !canManageBilling) return;
    if (!billingState) {
      await loadBillingLimits();
      return;
    }
    const nextEnabled = !billingState.billingLimitsEnabled;
    setBillingBusy(true);
    setBillingError(null);
    setBillingNotice(null);
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");
      persistAuthToken(idToken);
      const updated = await saveOrgBillingLimits(idToken, resolvedOrgId, nextEnabled);
      setBillingState(updated);
      setMemberships((rows) =>
        rows.map((row) =>
          row.orgId === resolvedOrgId
            ? {
                ...row,
                billingLimitsEnabled: updated.billingLimitsEnabled,
              }
            : row,
        ),
      );
      setBillingNotice(
        updated.billingLimitsEnabled
          ? "Monthly billing-limit enforcement is enabled for this church."
          : "Monthly billing-limit enforcement is disabled for this church.",
      );
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setBillingError(message || "Failed to update billing limits.");
    } finally {
      setBillingBusy(false);
    }
  };

  const openUpgradeCheckout = async () => {
    if (!resolvedOrgId || !canManagePaidBilling) return;
    setBillingCheckoutBusy(true);
    setBillingError(null);
    setBillingNotice(null);
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");
      persistAuthToken(idToken);
      const settingsPath =
        buildTabHref("settings", {
          orgId: resolvedOrgId,
          serviceKey: normalizedServiceKey || undefined,
          roomId: activeRoomId || undefined,
        }) || `/host/c/${encodeURIComponent(slug || "demo")}/settings`;
      const browserOrigin = (typeof window !== "undefined" && window.location.origin) || origin;
      const returnUrl = browserOrigin ? `${browserOrigin}${settingsPath}` : settingsPath;
      const checkout = await createBillingCheckoutSession(idToken, {
        orgId: resolvedOrgId,
        planKey: selectedPlan,
        successUrl: returnUrl,
        cancelUrl: returnUrl,
      });
      if (!checkout.url) throw new Error("Stripe checkout URL was not returned.");
      if (typeof window !== "undefined") {
        window.location.assign(checkout.url);
      } else {
        setBillingNotice(checkout.url);
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setBillingError(message || "Failed to start Stripe checkout.");
    } finally {
      setBillingCheckoutBusy(false);
    }
  };

  const openBillingPortal = async () => {
    if (!resolvedOrgId || !canManagePaidBilling) return;
    setBillingPortalBusy(true);
    setBillingError(null);
    setBillingNotice(null);
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");
      persistAuthToken(idToken);
      const settingsPath =
        buildTabHref("settings", {
          orgId: resolvedOrgId,
          serviceKey: normalizedServiceKey || undefined,
          roomId: activeRoomId || undefined,
        }) || `/host/c/${encodeURIComponent(slug || "demo")}/settings`;
      const browserOrigin = (typeof window !== "undefined" && window.location.origin) || origin;
      const returnUrl = browserOrigin ? `${browserOrigin}${settingsPath}` : settingsPath;
      const portal = await createBillingPortalSession(idToken, {
        orgId: resolvedOrgId,
        returnUrl,
      });
      if (!portal.url) throw new Error("Stripe billing portal URL was not returned.");
      if (typeof window !== "undefined") {
        window.location.assign(portal.url);
      } else {
        setBillingNotice(portal.url);
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setBillingError(message || "Failed to open Stripe billing portal.");
    } finally {
      setBillingPortalBusy(false);
    }
  };

  const saveSermonBudget = async () => {
    if (!resolvedOrgId || !canManageBilling) return;
    const parsed = Number(sermonBudgetInput);
    if (!Number.isFinite(parsed) || parsed < 0) {
      setSermonBudgetError("Enter a valid non-negative budget amount.");
      return;
    }
    setSermonBudgetBusy(true);
    setSermonBudgetError(null);
    setSermonBudgetNotice(null);
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");
      persistAuthToken(idToken);
      const updated = await saveOrgSermonBudget(idToken, resolvedOrgId, parsed);
      setSermonUsageState(updated);
      setSermonBudgetInput(String(updated.budgetUsd ?? parsed));
      setSermonBudgetNotice("Sermon Prep monthly budget updated.");
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setSermonBudgetError(message || "Failed to update Sermon Prep budget.");
    } finally {
      setSermonBudgetBusy(false);
    }
  };

  const addService = async () => {
    if (!resolvedOrgId) {
      setServiceManageError("Organization is not loaded yet.");
      return;
    }
    const key = newServiceKey.trim().toLowerCase();
    if (!key) {
      setServiceManageError("Enter a service key first (example: sun-9am).");
      return;
    }
    setServiceManageBusy(true);
    setServiceManageError(null);
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");
      persistAuthToken(idToken);
      await createOrgService(idToken, resolvedOrgId, {
        serviceKey: key,
        title: newServiceTitle.trim() || undefined,
        timezone: selectedService?.timezone || "America/Chicago",
        source: sourceLang,
        target: targetLang,
      });
      setNewServiceKey("");
      setNewServiceTitle("");
      await refreshServices(key);
      setServiceKey(key);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setServiceManageError(message || "Failed to add service.");
    } finally {
      setServiceManageBusy(false);
    }
  };

  const removeService = async (serviceKeyToDelete: string) => {
    if (!resolvedOrgId) return;
    const key = serviceKeyToDelete.trim();
    if (!key) return;
    if (selectedService?.serviceKey === key && activeRoomId) {
      setServiceManageError("Stop the live room first before deleting this service.");
      return;
    }
    setDeletingServiceKey(key);
    setServiceManageError(null);
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");
      persistAuthToken(idToken);
      await deleteOrgService(idToken, resolvedOrgId, key);

      const fallbackKey =
        serviceKey === key
          ? (orgData?.services.find((row) => row.serviceKey !== key)?.serviceKey || "")
          : serviceKey;
      await refreshServices(fallbackKey || undefined);
      if (serviceKey === key) {
        setServiceKey(fallbackKey || DEFAULT_SERVICE_KEY);
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setServiceManageError(message || "Failed to delete service.");
    } finally {
      setDeletingServiceKey("");
    }
  };

  const startService = async () => {
    if (!resolvedOrgId && !slug) {
      setErrorMsg("Church slug is missing. Refresh the page.");
      return;
    }
    if (!normalizedServiceKey) {
      setErrorMsg("Enter a service key before starting.");
      return;
    }
    if (normalizedServiceKey !== serviceKey) setServiceKey(normalizedServiceKey);
    setBusy(true);
    try {
      const idToken = await getIdToken();
      if (!idToken) throw new Error("Please sign in again.");
      persistAuthToken(idToken);
      const path = resolvedOrgId
        ? `/api/org/${encodeURIComponent(resolvedOrgId)}/service/${encodeURIComponent(normalizedServiceKey)}/start`
        : `/api/c/${encodeURIComponent(slug)}/service/${encodeURIComponent(normalizedServiceKey)}/start`;
      const res = await fetch(`${API_URL}${path}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${idToken}`,
        },
        body: JSON.stringify({
          source: sourceLang,
          target: targetLang,
        }),
      });
      if (!res.ok) {
        const msg = await readErrorMessage(res, "start_service");
        throw new Error(msg);
      }
      const data: StartResponse = await res.json();
      const nextOrgId = (data.orgId || resolvedOrgId || queryOrgId || "").trim();
      setActiveRoomId(data.roomId);
      if (data.serviceKey && data.serviceKey !== serviceKey) setServiceKey(data.serviceKey);
      persistStreamContext({
        orgId: nextOrgId || undefined,
        roomId: data.roomId,
        serviceKey: data.serviceKey || normalizedServiceKey,
        churchSlug: slug,
      });
      if (nextOrgId && nextOrgId !== queryOrgId) {
        const href = buildTabHref("broadcast", {
          serviceKey: data.serviceKey || normalizedServiceKey,
          orgId: nextOrgId,
          roomId: data.roomId,
        });
        if (href) {
          void router.replace(href, undefined, { shallow: true });
        }
      } else {
        syncHostUrl(data.roomId, data.serviceKey || normalizedServiceKey);
      }
      setErrorMsg(null);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setErrorMsg(message || "start_failed");
    } finally {
      setBusy(false);
    }
  };

  const endService = async () => {
    if (!resolvedOrgId || !activeRoomId) return;
    setBusy(true);
    try {
      const idToken = await getIdToken();
      if (!idToken) throw new Error("Please sign in again.");
      persistAuthToken(idToken);
      const res = await fetch(`${API_URL}/api/org/${encodeURIComponent(resolvedOrgId)}/room/${encodeURIComponent(activeRoomId)}/end`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${idToken}`,
        },
        body: JSON.stringify({
          reason: "host_end",
        }),
      });
      if (!res.ok) {
        const msg = await readErrorMessage(res, "end_service");
        throw new Error(msg);
      }
      setActiveRoomId(null);
      clearRoomInSession();
      persistStreamContext({
        orgId: resolvedOrgId,
        roomId: undefined,
        serviceKey: normalizedServiceKey || undefined,
        churchSlug: slug,
      });
      syncHostUrl(undefined, normalizedServiceKey);
      setErrorMsg(null);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setErrorMsg(message || "end_failed");
    } finally {
      setBusy(false);
    }
  };

  const hostPageStyle = {
    minHeight: "100vh",
    background: "linear-gradient(180deg, #e9edf4 0%, #dde4ee 56%, #d4dce8 100%)",
    color: "#0f172a",
    padding: "20px 14px 34px",
  } as const;
  const hostTopPanelStyle = {
    border: "1px solid rgba(255,255,255,0.88)",
    borderRadius: 24,
    background: "linear-gradient(145deg, rgba(242,246,251,0.98), rgba(220,228,240,0.94))",
    boxShadow: "24px 24px 56px rgba(122,138,163,0.18), -18px -18px 38px rgba(255,255,255,0.78)",
    padding: 16,
  } as const;
  const hostFieldStyle = {
    borderRadius: 10,
    border: "1px solid rgba(189,200,217,0.92)",
    background: "rgba(247,250,253,0.9)",
    color: "#20324a",
    padding: "9px 10px",
    boxShadow: "inset 2px 2px 6px rgba(122,138,163,0.1)",
  } as const;
  const hostTabRailStyle = {
    position: "sticky" as const,
    top: 8,
    zIndex: 5,
    display: "flex",
    flexWrap: "wrap" as const,
    gap: 8,
    marginBottom: 12,
    padding: 6,
    borderRadius: 14,
    border: "1px solid rgba(255,255,255,0.88)",
    background: "rgba(230,236,244,0.94)",
    backdropFilter: "blur(8px)",
    boxShadow: "inset 5px 5px 12px rgba(122,138,163,0.12), inset -5px -5px 12px rgba(255,255,255,0.82)",
  } as const;
  const studioHeaderStyle = {
    borderRadius: 34,
    position: "relative" as const,
    overflow: "hidden",
    border: "1px solid rgba(255,255,255,0.14)",
    background: "linear-gradient(135deg, #4a5d79 0%, #2f3b4f 32%, #1d2430 68%, #161b24 100%)",
    boxShadow: "0 24px 54px rgba(15,23,42,0.22), inset 0 1px 0 rgba(255,255,255,0.08)",
    padding: "18px 20px",
    color: "#f8fafc",
  } as const;
  const studioUserPanelStyle = {
    display: "flex",
    alignItems: "center",
    gap: 16,
    borderRadius: 26,
    background: "linear-gradient(145deg, rgba(255,255,255,0.1), rgba(255,255,255,0.04))",
    padding: "10px 14px 10px 18px",
    boxShadow: "inset 0 1px 0 rgba(255,255,255,0.08), 0 12px 24px rgba(15,23,42,0.14)",
  } as const;
  const studioLogoutButtonStyle = {
    borderRadius: 18,
    border: "1px solid rgba(255,255,255,0.18)",
    background: "rgba(13,20,32,0.2)",
    color: "#f8fafc",
    fontSize: 12,
    fontWeight: 800,
    letterSpacing: "0.16em",
    textTransform: "uppercase" as const,
    padding: "12px 22px",
    cursor: "pointer",
  } as const;
  const studioBrandTileStyle = {
    height: 72,
    width: 72,
    borderRadius: 22,
    background: "linear-gradient(145deg, rgba(255,255,255,0.08), rgba(255,255,255,0.02))",
    boxShadow: "inset 0 0 0 1px rgba(255,255,255,0.1), 0 12px 24px rgba(15,23,42,0.18)",
    display: "grid",
    placeItems: "center",
    color: "#ffb703",
    fontSize: 22,
    fontWeight: 900,
    fontStyle: "italic",
  } as const;
  const accentPrimaryGradient = "linear-gradient(145deg, #7fa5db, #4f73aa)";
  const accentPrimaryShadow = "0 14px 30px rgba(79,115,170,0.28)";
  const accentDangerGradient = "linear-gradient(145deg, #e38888, #bc5f6f)";
  const accentDangerShadow = "0 14px 28px rgba(188,95,111,0.22)";
  const settingsShellStyle = {
    marginTop: 12,
    display: "grid",
    gap: 16,
  } as const;
  const settingsGridStyle = {
    display: "grid",
    gap: 16,
    gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 320px), 1fr))",
  } as const;
  const settingsCardStyle = {
    border: "1px solid rgba(255,255,255,0.88)",
    borderRadius: 22,
    padding: 18,
    background: "linear-gradient(145deg, rgba(248,250,253,0.96), rgba(230,236,244,0.92))",
    boxShadow: "20px 20px 40px rgba(122,138,163,0.12), -14px -14px 28px rgba(255,255,255,0.72)",
    display: "grid",
    gap: 12,
  } as const;
  const settingsSubscriptionCardStyle = {
    ...settingsCardStyle,
    background: "linear-gradient(145deg, rgba(237,243,251,0.98), rgba(216,227,243,0.94))",
  } as const;
  const settingsBudgetCardStyle = {
    ...settingsCardStyle,
    background: "linear-gradient(145deg, rgba(240,246,243,0.98), rgba(219,231,226,0.94))",
  } as const;
  const settingsSectionLabelStyle = {
    margin: 0,
    fontSize: 11,
    fontWeight: 800,
    letterSpacing: "0.22em",
    textTransform: "uppercase" as const,
    color: "#7c8ba3",
  } as const;
  const settingsTitleStyle = {
    margin: 0,
    fontSize: 18,
    fontWeight: 800,
    color: "#20324a",
  } as const;
  const settingsBodyTextStyle = {
    margin: 0,
    fontSize: 13,
    color: "#5f6f86",
    lineHeight: 1.6,
  } as const;
  const settingsPillBaseStyle = {
    display: "inline-flex",
    alignItems: "center",
    borderRadius: 999,
    padding: "6px 10px",
    fontSize: 12,
    fontWeight: 700,
  } as const;
  const settingsButtonPrimaryStyle = {
    borderRadius: 12,
    border: "1px solid rgba(79,115,170,0.28)",
    background: accentPrimaryGradient,
    color: "#f8fafc",
    fontWeight: 800,
    padding: "10px 14px",
    cursor: "pointer",
    boxShadow: accentPrimaryShadow,
  } as const;
  const settingsButtonNeutralStyle = {
    borderRadius: 12,
    border: "1px solid rgba(189,200,217,0.95)",
    background: "rgba(247,250,253,0.82)",
    color: "#42556f",
    fontWeight: 700,
    padding: "10px 14px",
    cursor: "pointer",
    boxShadow: "8px 8px 18px rgba(122,138,163,0.1), -8px -8px 18px rgba(255,255,255,0.76)",
  } as const;
  const settingsButtonDangerStyle = {
    borderRadius: 12,
    border: "1px solid rgba(188,95,111,0.3)",
    background: accentDangerGradient,
    color: "#f8fafc",
    fontWeight: 800,
    padding: "10px 14px",
    cursor: "pointer",
    boxShadow: accentDangerShadow,
  } as const;
  const settingsInlineFieldStyle = {
    ...hostFieldStyle,
    minWidth: "min(100%, 160px)",
  } as const;
  const settingsServiceRowStyle = {
    display: "flex",
    flexWrap: "wrap" as const,
    justifyContent: "space-between",
    gap: 12,
    alignItems: "center",
    border: "1px solid rgba(215,223,235,0.9)",
    borderRadius: 16,
    padding: "14px 16px",
    background: "rgba(255,255,255,0.72)",
  } as const;
  const currentUserName = (
    user?.displayName ||
    (user?.email ? user.email.split("@")[0].replace(/[._-]+/g, " ") : "") ||
    "Host"
  ).trim();
  const currentUserInitial = currentUserName.charAt(0).toUpperCase() || "H";
  const currentChurchLabel = (orgData?.name || slug || "Current Church").trim();
  const churchPublicPath = slug ? `${origin || ""}/c/${slug}` : "";

  useEffect(() => {
    setAccountDisplayNameInput(currentUserName);
  }, [currentUserName]);

  useEffect(() => {
    setChurchNameInput((orgData?.name || "").trim());
  }, [orgData?.name, orgData?.orgId]);

  if (!user && !authLoading) {
    return (
      <main style={{ ...hostPageStyle, display: "grid", placeItems: "center" }}>
        Redirecting to login...
      </main>
    );
  }

  return (
    <main style={hostPageStyle}>
      <div style={{ maxWidth: 1240, margin: "0 auto", display: "grid", gap: 18 }}>
        <section style={studioHeaderStyle}>
          <div
            aria-hidden="true"
            style={{
              position: "absolute",
              inset: "-40% auto auto -8%",
              height: 180,
              width: 260,
              borderRadius: 999,
              background: "radial-gradient(circle, rgba(145, 177, 220, 0.34) 0%, rgba(145, 177, 220, 0) 72%)",
              filter: "blur(8px)",
            }}
          />
          <div
            aria-hidden="true"
            style={{
              position: "absolute",
              inset: "auto -6% -120px auto",
              height: 220,
              width: 280,
              borderRadius: 999,
              background: "radial-gradient(circle, rgba(226, 234, 246, 0.18) 0%, rgba(226, 234, 246, 0) 74%)",
              filter: "blur(10px)",
            }}
          />
          <div style={{ position: "relative", display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 18 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 22 }}>
              <div style={studioBrandTileStyle}>{currentUserInitial}</div>
              <div style={{ fontFamily: "Inter, 'Segoe UI', system-ui, sans-serif" }}>
                <h1
                  style={{
                    margin: 0,
                    color: "#f8fafc",
                    fontSize: "clamp(30px, 3.8vw, 35px)",
                    lineHeight: 1,
                    fontWeight: 800,
                    letterSpacing: "-0.05em",
                  }}
                >
                  {currentUserName}
                </h1>
                <p
                  style={{
                    margin: "8px 0 0",
                    color: "#b8c4da",
                    fontSize: 12,
                    letterSpacing: "0.34em",
                    textTransform: "uppercase",
                  }}
                >
                  Translation Studio
                </p>
              </div>
            </div>

            <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 12, minWidth: 0 }}>
                <span
                  style={{
                    color: "#e1e8f4",
                    fontSize: 16,
                    fontWeight: 700,
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    maxWidth: 280,
                  }}
                >
                  {currentChurchLabel}
                </span>
              </div>

              <button
                onClick={async () => {
                  clearHostToken();
                  clearAuthToken();
                  await logout();
                }}
                style={studioLogoutButtonStyle}
              >
                Logout
              </button>
            </div>
          </div>
        </section>

        <section style={hostTopPanelStyle}>
          <h1 style={{ marginTop: 0, marginBottom: 8, fontSize: 26, color: "#0f172a" }}>{orgData?.name || slug || "Host"}</h1>
          <p style={{ marginTop: 0, opacity: 0.75, marginBottom: 14, color: "#475569" }}>
            Start a room for a recurring service, then begin microphone streaming.
          </p>
          <p style={{ marginTop: 0, marginBottom: 10, opacity: 0.76, fontSize: 13, color: "#475569" }}>
            {resolvedOrgId ? `Org: ${resolvedOrgId}` : loading ? "Loading church organization..." : "Organization not loaded yet."}
          </p>
          {errorMsg ? <p style={{ color: "#b91c1c", marginTop: 0 }}>Error: {errorMsg}</p> : null}
          {memberships.length > 1 ? (
            <div style={{ marginBottom: 12, display: "grid", gap: 4, maxWidth: 380 }}>
              <span style={{ fontSize: 12, opacity: 0.75, color: "#475569" }}>Current Church</span>
              <select
                value={selectedOrgId || resolvedOrgId}
                onChange={(e) => {
                  void switchOrganization(e.target.value);
                }}
                disabled={switchingOrg || busy}
                style={hostFieldStyle}
              >
                {memberships.map((row) => (
                  <option key={row.orgId} value={row.orgId}>
                    {row.name} ({row.role || "member"})
                  </option>
                ))}
              </select>
              {switchingOrg ? <span style={{ fontSize: 12, opacity: 0.75 }}>Switching church...</span> : null}
            </div>
          ) : null}
          <div style={hostTabRailStyle}>
            <button
              onClick={() => navigateToTab("broadcast")}
              style={{
                borderRadius: 10,
                border: activeTab === "broadcast" ? "1px solid rgba(79,115,170,0.38)" : "1px solid rgba(189,200,217,0.92)",
                background: activeTab === "broadcast" ? accentPrimaryGradient : "rgba(247,250,253,0.78)",
                color: activeTab === "broadcast" ? "#f8fafc" : "#42556f",
                fontSize: 13,
                fontWeight: 800,
                padding: "8px 12px",
                cursor: "pointer",
                boxShadow: activeTab === "broadcast"
                  ? accentPrimaryShadow
                  : "8px 8px 18px rgba(122,138,163,0.12), -8px -8px 18px rgba(255,255,255,0.78)",
              }}
            >
              Live Broadcast
            </button>
            <button
              onClick={() => navigateToTab("settings")}
              style={{
                borderRadius: 10,
                border: activeTab === "settings" ? "1px solid rgba(79,115,170,0.38)" : "1px solid rgba(189,200,217,0.92)",
                background: activeTab === "settings" ? accentPrimaryGradient : "rgba(247,250,253,0.78)",
                color: activeTab === "settings" ? "#f8fafc" : "#42556f",
                fontSize: 13,
                fontWeight: 800,
                padding: "8px 12px",
                cursor: "pointer",
                boxShadow: activeTab === "settings"
                  ? accentPrimaryShadow
                  : "8px 8px 18px rgba(122,138,163,0.12), -8px -8px 18px rgba(255,255,255,0.78)",
              }}
            >
              Church Settings
            </button>
            <button
              onClick={() => navigateToTab("team")}
              style={{
                borderRadius: 10,
                border: activeTab === "team" ? "1px solid rgba(79,115,170,0.38)" : "1px solid rgba(189,200,217,0.92)",
                background: activeTab === "team" ? accentPrimaryGradient : "rgba(247,250,253,0.78)",
                color: activeTab === "team" ? "#f8fafc" : "#42556f",
                fontSize: 13,
                fontWeight: 800,
                padding: "8px 12px",
                cursor: "pointer",
                boxShadow: activeTab === "team"
                  ? accentPrimaryShadow
                  : "8px 8px 18px rgba(122,138,163,0.12), -8px -8px 18px rgba(255,255,255,0.78)",
              }}
            >
              Team
            </button>
          </div>
          {activeTab === "broadcast" ? (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(180px,1fr))", gap: 10 }}>
                <label style={{ display: "grid", gap: 4 }}>
                  <span style={{ fontSize: 12, opacity: 0.75, color: "#475569" }}>Service</span>
                  {orgData?.services?.length ? (
                    <select
                      value={serviceKey}
                      onChange={(e) => {
                        const nextKey = e.target.value;
                        setServiceKey(nextKey);
                        setActiveRoomId(null);
                        persistStreamContext({ orgId: orgData?.orgId, serviceKey: nextKey, churchSlug: slug });
                      }}
                      disabled={loading}
                      style={hostFieldStyle}
                    >
                      {orgData?.services?.map((row) => (
                        <option key={row.serviceKey} value={row.serviceKey}>
                          {row.title} ({row.serviceKey})
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      value={serviceKey}
                      onChange={(e) => {
                        const nextKey = e.target.value;
                        setServiceKey(nextKey);
                        setActiveRoomId(null);
                        persistStreamContext({ orgId: orgData?.orgId, serviceKey: nextKey, churchSlug: slug });
                      }}
                      placeholder={DEFAULT_SERVICE_KEY}
                      style={hostFieldStyle}
                    />
                  )}
                  {!loading && orgData && !orgData.services?.length ? (
                    <span style={{ fontSize: 12, opacity: 0.75, color: "#64748b" }}>No services found. Create a service in Church Settings first, then start it here.</span>
                  ) : null}
                </label>
                <label style={{ display: "grid", gap: 4 }}>
                  <span style={{ fontSize: 12, opacity: 0.75, color: "#475569" }}>Source</span>
                  <input value={sourceLang} onChange={(e) => setSourceLang(e.target.value)} style={hostFieldStyle} />
                </label>
                <label style={{ display: "grid", gap: 4 }}>
                  <span style={{ fontSize: 12, opacity: 0.75, color: "#475569" }}>Target</span>
                  <input value={targetLang} onChange={(e) => setTargetLang(e.target.value)} style={hostFieldStyle} />
                </label>
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 12, alignItems: "center" }}>
                <button
                  onClick={startService}
                  disabled={busy}
                  style={{
                    borderRadius: 10,
                    border: "1px solid rgba(79,115,170,0.3)",
                    background: accentPrimaryGradient,
                    color: "#f8fafc",
                    fontWeight: 700,
                    padding: "9px 14px",
                    cursor: busy ? "not-allowed" : "pointer",
                    opacity: busy ? 0.6 : 1,
                    boxShadow: accentPrimaryShadow,
                  }}
                >
                  {activeRoomId ? "Restart / Rejoin Room" : "Start Service"}
                </button>
                <button
                  onClick={endService}
                  disabled={busy || !orgData?.orgId || !activeRoomId}
                  style={{
                    borderRadius: 10,
                    border: "1px solid rgba(188,95,111,0.3)",
                    background: accentDangerGradient,
                    color: "#f8fafc",
                    fontWeight: 700,
                    padding: "9px 14px",
                    cursor: busy || !orgData?.orgId || !activeRoomId ? "not-allowed" : "pointer",
                    opacity: busy || !orgData?.orgId || !activeRoomId ? 0.6 : 1,
                    boxShadow: accentDangerShadow,
                  }}
                >
                  End Service
                </button>
                <span style={{ opacity: 0.84, fontSize: 14 }}>
                  {activeRoomId ? `Live room: ${activeRoomId}` : "No live room"}
                </span>
                {billingPlanToken === "trial" && trialMinutesRemaining !== null ? (
                  <span
                    style={{
                      borderRadius: 999,
                      border: isTrialExpired ? "1px solid rgba(252,165,165,0.8)" : "1px solid rgba(251,191,36,0.7)",
                      background: isTrialExpired ? "rgba(127,29,29,0.35)" : "rgba(120,53,15,0.35)",
                      color: isTrialExpired ? "#fecaca" : "#fde68a",
                      fontSize: 12,
                      fontWeight: 700,
                      padding: "6px 10px",
                    }}
                  >
                    Trial remaining: {formatCountdownMinutes(trialMinutesRemaining)}
                  </span>
                ) : null}
              </div>
              {trialBroadcastNotice ? (
                <div
                  style={{
                    marginTop: 10,
                    borderRadius: 10,
                    border: isTrialExpired ? "1px solid rgba(252,165,165,0.7)" : "1px solid rgba(251,191,36,0.65)",
                    background: isTrialExpired ? "rgba(127,29,29,0.28)" : "rgba(120,53,15,0.28)",
                    color: isTrialExpired ? "#fecaca" : "#fde68a",
                    fontSize: 13,
                    fontWeight: 600,
                    padding: "9px 11px",
                  }}
                >
                  {trialBroadcastNotice}
                </div>
              ) : null}
              {listenerUrl ? (
                <p style={{ marginTop: 10, marginBottom: 0, fontSize: 13, opacity: 0.84 }}>
                  Listener URL: <a href={listenerUrl} target="_blank" rel="noreferrer" style={{ color: "#2563eb" }}>{listenerUrl}</a>
                </p>
              ) : null}
              <p style={{ marginTop: 8, marginBottom: 0, fontSize: 12, opacity: 0.72 }}>
                Signed-in hosts are authorized by account role. Manual host token entry is not required.
              </p>
            </>
          ) : null}
          {activeTab === "settings" ? (
            canManageServices ? (
              <div style={settingsShellStyle}>
                <div style={settingsGridStyle}>
                  <section style={settingsCardStyle}>
                    <p style={settingsSectionLabelStyle}>Account</p>
                    <h3 style={settingsTitleStyle}>Your Profile</h3>
                    <p style={settingsBodyTextStyle}>
                      Update the name shown in the host console and team-facing flows.
                    </p>
                    <label style={{ display: "grid", gap: 6 }}>
                      <span style={{ fontSize: 12, color: "#5f6f86" }}>Display name</span>
                      <input
                        value={accountDisplayNameInput}
                        onChange={(e) => {
                          setAccountDisplayNameInput(e.target.value);
                          setAccountProfileError(null);
                          setAccountProfileNotice(null);
                        }}
                        style={{ ...settingsInlineFieldStyle, width: "100%" }}
                      />
                    </label>
                    <p style={{ margin: 0, fontSize: 12, color: "#6b7b92" }}>
                      This can be changed later.
                    </p>
                    {accountProfileError ? <p style={{ margin: 0, color: "#b95567", fontSize: 13 }}>Error: {accountProfileError}</p> : null}
                    {accountProfileNotice ? <p style={{ margin: 0, color: "#3b7d5c", fontSize: 13 }}>{accountProfileNotice}</p> : null}
                    <div>
                      <button
                        onClick={() => {
                          void saveAccountProfile();
                        }}
                        disabled={accountProfileBusy}
                        style={{
                          ...settingsButtonPrimaryStyle,
                          opacity: accountProfileBusy ? 0.6 : 1,
                          cursor: accountProfileBusy ? "not-allowed" : "pointer",
                        }}
                      >
                        {accountProfileBusy ? "Saving..." : "Save Display Name"}
                      </button>
                    </div>
                  </section>

                  <section style={settingsCardStyle}>
                    <p style={settingsSectionLabelStyle}>Church Identity</p>
                    <h3 style={settingsTitleStyle}>Church Name & URL</h3>
                    <p style={settingsBodyTextStyle}>
                      The church name can be updated later. The church slug is public-facing and stays fixed after creation.
                    </p>
                    <label style={{ display: "grid", gap: 6 }}>
                      <span style={{ fontSize: 12, color: "#5f6f86" }}>Church name</span>
                      <input
                        value={churchNameInput}
                        onChange={(e) => {
                          setChurchNameInput(e.target.value);
                          setChurchProfileError(null);
                          setChurchProfileNotice(null);
                        }}
                        disabled={!canManagePaidBilling}
                        style={{
                          ...settingsInlineFieldStyle,
                          width: "100%",
                          opacity: canManagePaidBilling ? 1 : 0.7,
                        }}
                      />
                    </label>
                    <label style={{ display: "grid", gap: 6 }}>
                      <span style={{ fontSize: 12, color: "#5f6f86" }}>Church URL slug</span>
                      <input
                        readOnly
                        value={slug}
                        style={{
                          ...settingsInlineFieldStyle,
                          width: "100%",
                          background: "rgba(239,244,250,0.92)",
                          color: "#5f6f86",
                        }}
                      />
                    </label>
                    {churchPublicPath ? (
                      <p style={{ margin: 0, fontSize: 12, color: "#4d607a", wordBreak: "break-all" }}>
                        Public path: <strong>{churchPublicPath}</strong>
                      </p>
                    ) : null}
                    {!canManagePaidBilling ? (
                      <p style={{ margin: 0, fontSize: 13, color: "#5f6f86" }}>
                        Owner or admin role is required to rename the church. The slug remains locked for all roles.
                      </p>
                    ) : null}
                    {churchProfileError ? <p style={{ margin: 0, color: "#b95567", fontSize: 13 }}>Error: {churchProfileError}</p> : null}
                    {churchProfileNotice ? <p style={{ margin: 0, color: "#3b7d5c", fontSize: 13 }}>{churchProfileNotice}</p> : null}
                    <div>
                      <button
                        onClick={() => {
                          void saveChurchProfile();
                        }}
                        disabled={churchProfileBusy || !canManagePaidBilling || !resolvedOrgId}
                        style={{
                          ...settingsButtonPrimaryStyle,
                          opacity: churchProfileBusy || !canManagePaidBilling || !resolvedOrgId ? 0.6 : 1,
                          cursor: churchProfileBusy || !canManagePaidBilling || !resolvedOrgId ? "not-allowed" : "pointer",
                        }}
                      >
                        {churchProfileBusy ? "Saving..." : "Save Church Name"}
                      </button>
                    </div>
                  </section>

                  <section style={settingsSubscriptionCardStyle}>
                    <p style={settingsSectionLabelStyle}>Billing Overview</p>
                    <h3 style={settingsTitleStyle}>Subscription</h3>
                    <p style={settingsBodyTextStyle}>
                      Review the current plan, Stripe sync status, and billing actions for this church.
                    </p>

                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                      <span style={{ ...settingsPillBaseStyle, border: "1px solid rgba(79,115,170,0.18)", background: "rgba(127,165,219,0.16)", color: "#3e5d8d" }}>
                        Plan · {formatPlanLabel(billingPlanToken)}
                      </span>
                      <span
                        style={{
                          ...settingsPillBaseStyle,
                          border: billingStatusToken === "active"
                            ? "1px solid rgba(91,179,130,0.22)"
                            : billingStatusToken === "past_due"
                              ? "1px solid rgba(224,163,86,0.26)"
                              : "1px solid rgba(189,200,217,0.9)",
                          background: billingStatusToken === "active"
                            ? "rgba(91,179,130,0.14)"
                            : billingStatusToken === "past_due"
                              ? "rgba(224,163,86,0.16)"
                              : "rgba(247,250,253,0.8)",
                          color: billingStatusToken === "active"
                            ? "#3b7d5c"
                            : billingStatusToken === "past_due"
                              ? "#9a6433"
                              : "#55657d",
                        }}
                      >
                        Status · {formatBillingStatus(billingStatusToken)}
                      </span>
                      <span style={{ ...settingsPillBaseStyle, border: "1px solid rgba(189,200,217,0.95)", background: "rgba(247,250,253,0.8)", color: "#55657d" }}>
                        Services · {billingMaxServiceKeys > 0 ? `up to ${billingMaxServiceKeys}` : "unlimited"}
                      </span>
                    </div>

                    {billingProfile ? (
                      <div style={{ display: "grid", gap: 8 }}>
                        {hasSubscriptionPeriod ? (
                          <div style={{ borderRadius: 16, border: "1px solid rgba(189,200,217,0.8)", background: "rgba(255,255,255,0.7)", padding: "12px 14px" }}>
                            <p style={{ ...settingsSectionLabelStyle, fontSize: 10 }}>Subscription Period</p>
                            <p style={{ margin: "6px 0 0", fontSize: 13, color: "#334155" }}>
                              <strong>{formatDateTime(billingProfile.currentPeriodStart)}</strong>
                              {" → "}
                              <strong>{formatDateTime(billingProfile.currentPeriodEnd)}</strong>
                              {billingProfile.cancelAtPeriodEnd ? " · Cancels at period end" : ""}
                            </p>
                          </div>
                        ) : null}
                        {isTrialPlan && !hasSubscriptionPeriod && trialMinutesRemaining !== null ? (
                          <p style={settingsBodyTextStyle}>
                            Trial usage: <strong>{trialMinutesUsed}</strong> / <strong>{trialMinutesLimit}</strong> minutes
                            {" · "}
                            Remaining: <strong>{trialMinutesRemaining}</strong> minutes
                          </p>
                        ) : null}
                        {isTrialPlan && !hasSubscriptionPeriod && trialMinutesRemaining === null ? (
                          <p style={settingsBodyTextStyle}>Trial usage details will appear after the next usage tick.</p>
                        ) : null}
                        {!isTrialPlan && !hasSubscriptionPeriod ? (
                          <p style={settingsBodyTextStyle}>Subscription period is syncing from Stripe. Click refresh in a few seconds.</p>
                        ) : null}
                      </div>
                    ) : (
                      <p style={settingsBodyTextStyle}>Loading billing profile…</p>
                    )}

                    {canManagePaidBilling ? (
                      <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
                        <select
                          value={selectedPlan}
                          onChange={(e) => setSelectedPlan(e.target.value as PaidPlanKey)}
                          style={{ ...settingsInlineFieldStyle, flex: "1 1 240px" }}
                        >
                          {selectablePaidPlans.map((plan) => (
                            <option key={plan} value={plan}>
                              {PLAN_LABELS[plan]}
                            </option>
                          ))}
                        </select>
                        <button
                          onClick={openUpgradeCheckout}
                          disabled={billingCheckoutBusy || !resolvedOrgId}
                          style={{
                            ...settingsButtonPrimaryStyle,
                            opacity: billingCheckoutBusy || !resolvedOrgId ? 0.6 : 1,
                            cursor: billingCheckoutBusy || !resolvedOrgId ? "not-allowed" : "pointer",
                          }}
                        >
                          {billingCheckoutBusy ? "Opening Checkout..." : "Upgrade Plan"}
                        </button>
                        <button
                          onClick={openBillingPortal}
                          disabled={billingPortalBusy || !resolvedOrgId}
                          style={{
                            ...settingsButtonNeutralStyle,
                            opacity: billingPortalBusy || !resolvedOrgId ? 0.6 : 1,
                            cursor: billingPortalBusy || !resolvedOrgId ? "not-allowed" : "pointer",
                          }}
                        >
                          {billingPortalBusy ? "Opening Portal..." : "Manage Billing"}
                        </button>
                        <button
                          onClick={() => {
                            void loadBillingProfile({ refresh: true });
                          }}
                          disabled={billingPortalBusy || billingCheckoutBusy || billingRefreshBusy || !resolvedOrgId}
                          style={{
                            ...settingsButtonNeutralStyle,
                            opacity: billingPortalBusy || billingCheckoutBusy || billingRefreshBusy || !resolvedOrgId ? 0.6 : 1,
                            cursor: billingPortalBusy || billingCheckoutBusy || billingRefreshBusy || !resolvedOrgId ? "not-allowed" : "pointer",
                          }}
                        >
                          {billingRefreshBusy ? "Refreshing..." : "Refresh Billing"}
                        </button>
                      </div>
                    ) : (
                      <p style={settingsBodyTextStyle}>
                        Owner or admin role is required to manage subscription checkout and billing portal.
                      </p>
                    )}
                  </section>

                  <section style={settingsCardStyle}>
                    <p style={settingsSectionLabelStyle}>Policy Controls</p>
                    <h3 style={settingsTitleStyle}>Billing Limits</h3>
                    <p style={settingsBodyTextStyle}>
                      Toggle monthly hard-cap enforcement for this church only.
                    </p>
                    {canManageBilling ? (
                      <>
                        <div style={{ borderRadius: 16, border: "1px solid rgba(189,200,217,0.84)", background: "rgba(255,255,255,0.68)", padding: "12px 14px", display: "grid", gap: 6 }}>
                          <p style={{ margin: 0, fontSize: 13, color: "#42556f" }}>
                            Status:{" "}
                            <strong>{billingState ? (billingState.billingLimitsEnabled ? "Enabled" : "Disabled") : "Loading..."}</strong>
                            {billingState && !billingState.globalBillingLimitsEnabled ? " · Global override currently disables all billing checks" : ""}
                          </p>
                          {billingState ? (
                            <p style={{ margin: 0, fontSize: 12, color: "#5f6f86" }}>
                              Usage this month: {billingState.currentMonthMinutes} / {billingState.maxMinutesPerMonth > 0 ? billingState.maxMinutesPerMonth : "unlimited"} minutes
                              {" · "}
                              Hard cap: {billingState.hardCapReached ? "reached" : "not reached"}
                            </p>
                          ) : null}
                        </div>
                        {billingError ? <p style={{ margin: 0, color: "#b95567", fontSize: 13 }}>Error: {billingError}</p> : null}
                        {billingNotice ? <p style={{ margin: 0, color: "#3b7d5c", fontSize: 13 }}>{billingNotice}</p> : null}
                        <div>
                          <button
                            onClick={toggleBillingLimits}
                            disabled={billingBusy || !resolvedOrgId}
                            style={{
                              ...(billingState?.billingLimitsEnabled ? settingsButtonDangerStyle : settingsButtonPrimaryStyle),
                              opacity: billingBusy || !resolvedOrgId ? 0.6 : 1,
                              cursor: billingBusy || !resolvedOrgId ? "not-allowed" : "pointer",
                            }}
                          >
                            {billingBusy
                              ? "Saving..."
                              : billingState?.billingLimitsEnabled
                                ? "Disable Billing Limits"
                                : "Enable Billing Limits"}
                          </button>
                        </div>
                      </>
                    ) : (
                      <p style={settingsBodyTextStyle}>Billing admin account is required to change billing limits.</p>
                    )}
                  </section>

                  <section style={settingsBudgetCardStyle}>
                    <p style={settingsSectionLabelStyle}>AI Budget</p>
                    <h3 style={settingsTitleStyle}>Sermon Prep Budget</h3>
                    <p style={settingsBodyTextStyle}>
                      Track OpenAI token usage by sermon and cap monthly Sermon Prep spend for this church.
                    </p>
                    {sermonUsageState ? (
                      <>
                        <div style={{ borderRadius: 16, border: "1px solid rgba(184,204,194,0.88)", background: "rgba(255,255,255,0.68)", padding: "12px 14px", display: "grid", gap: 6 }}>
                          <p style={{ margin: 0, fontSize: 13, color: "#42556f" }}>
                            Month {sermonUsageState.currentMonthKey}: <strong>${sermonUsageState.currentMonthEstimatedUsd.toFixed(4)}</strong>
                            {sermonUsageState.effectiveBudgetEnabled ? ` / $${sermonUsageState.budgetUsd.toFixed(2)}` : " (budget disabled)"}
                            {" · "}
                            Tokens: {sermonUsageState.currentMonthTotalTokens.toLocaleString()}
                            {" · "}
                            Cap: {sermonUsageState.capReached ? "reached" : "not reached"}
                          </p>
                          {sermonUsageState.sermons.length ? (
                            <div style={{ display: "grid", gap: 4 }}>
                              {sermonUsageState.sermons.slice(0, 4).map((row) => (
                                <p key={row.sermonId} style={{ margin: 0, fontSize: 12, color: "#5f6f86" }}>
                                  {row.sermonId}: ${row.estimatedUsd.toFixed(4)} · {row.totalTokens.toLocaleString()} tokens
                                </p>
                              ))}
                            </div>
                          ) : (
                            <p style={{ margin: 0, fontSize: 12, color: "#5f6f86" }}>No Sermon Prep usage recorded this month yet.</p>
                          )}
                        </div>
                      </>
                    ) : (
                      <p style={settingsBodyTextStyle}>Loading usage…</p>
                    )}
                    {canManageBilling ? (
                      <>
                        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "end" }}>
                          <label style={{ display: "grid", gap: 6 }}>
                            <span style={{ fontSize: 12, color: "#5f6f86" }}>Monthly budget (USD)</span>
                            <input
                              type="number"
                              min={0}
                              step={0.01}
                              value={sermonBudgetInput}
                              onChange={(e) => setSermonBudgetInput(e.target.value)}
                              style={{ ...settingsInlineFieldStyle, minWidth: 150 }}
                            />
                          </label>
                          <button
                            onClick={saveSermonBudget}
                            disabled={sermonBudgetBusy || !resolvedOrgId}
                            style={{
                              ...settingsButtonPrimaryStyle,
                              opacity: sermonBudgetBusy || !resolvedOrgId ? 0.6 : 1,
                              cursor: sermonBudgetBusy || !resolvedOrgId ? "not-allowed" : "pointer",
                            }}
                          >
                            {sermonBudgetBusy ? "Saving..." : "Save Sermon Budget"}
                          </button>
                          <button
                            onClick={() => {
                              void loadSermonUsage();
                            }}
                            disabled={sermonBudgetBusy || !resolvedOrgId}
                            style={{
                              ...settingsButtonNeutralStyle,
                              opacity: sermonBudgetBusy || !resolvedOrgId ? 0.6 : 1,
                              cursor: sermonBudgetBusy || !resolvedOrgId ? "not-allowed" : "pointer",
                            }}
                          >
                            Refresh Usage
                          </button>
                        </div>
                        {sermonBudgetError ? <p style={{ margin: 0, color: "#b95567", fontSize: 13 }}>Error: {sermonBudgetError}</p> : null}
                        {sermonBudgetNotice ? <p style={{ margin: 0, color: "#3b7d5c", fontSize: 13 }}>{sermonBudgetNotice}</p> : null}
                      </>
                    ) : null}
                  </section>
                </div>

                <section style={settingsCardStyle}>
                  <div style={{ display: "flex", flexWrap: "wrap", alignItems: "start", justifyContent: "space-between", gap: 12 }}>
                    <div style={{ display: "grid", gap: 8 }}>
                      <p style={settingsSectionLabelStyle}>Operations</p>
                      <h3 style={settingsTitleStyle}>Service Schedule</h3>
                      <p style={settingsBodyTextStyle}>
                        Add service times for this church. Added services appear in the dropdown for all members.
                      </p>
                    </div>
                    {resolvedOrgId ? (
                      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                        <button
                          onClick={() => {
                            const qs = new URLSearchParams();
                            qs.set("orgId", resolvedOrgId);
                            if (slug) qs.set("churchSlug", slug);
                            void router.push(`/admin/prompt?${qs.toString()}`);
                          }}
                          style={settingsButtonNeutralStyle}
                        >
                          Open Prompt Settings
                        </button>
                        <button
                          onClick={() => {
                            const qs = new URLSearchParams();
                            qs.set("orgId", resolvedOrgId);
                            if (slug) qs.set("churchSlug", slug);
                            const returnTo = (router.asPath || "").trim();
                            if (returnTo.startsWith("/") && !returnTo.startsWith("//")) qs.set("returnTo", returnTo);
                            void router.push(`/admin/sermon-prep?${qs.toString()}`);
                          }}
                          style={settingsButtonNeutralStyle}
                        >
                          Open Sermon Prep
                        </button>
                      </div>
                    ) : null}
                  </div>

                  <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
                    <input
                      value={newServiceKey}
                      onChange={(e) => setNewServiceKey(e.target.value)}
                      placeholder="service key (example: sun-9am)"
                      style={{ ...settingsInlineFieldStyle, flex: "1 1 240px" }}
                    />
                    <input
                      value={newServiceTitle}
                      onChange={(e) => setNewServiceTitle(e.target.value)}
                      placeholder="title (optional)"
                      style={{ ...settingsInlineFieldStyle, flex: "1 1 240px" }}
                    />
                    <button
                      onClick={addService}
                      disabled={serviceManageBusy || deletingServiceKey.length > 0 || !resolvedOrgId}
                      style={{
                        ...settingsButtonPrimaryStyle,
                        opacity: serviceManageBusy || deletingServiceKey.length > 0 || !resolvedOrgId ? 0.6 : 1,
                        cursor: serviceManageBusy || deletingServiceKey.length > 0 || !resolvedOrgId ? "not-allowed" : "pointer",
                      }}
                    >
                      {serviceManageBusy ? "Adding..." : "Add Service"}
                    </button>
                  </div>
                  {serviceManageError ? <p style={{ margin: 0, color: "#b95567", fontSize: 13 }}>Error: {serviceManageError}</p> : null}

                  <div style={{ display: "grid", gap: 10 }}>
                    {(orgData?.services || []).map((row) => {
                      const isSelected = row.serviceKey === serviceKey;
                      const isLive = Boolean(row.activeRoomId);
                      const deleting = deletingServiceKey === row.serviceKey;
                      return (
                        <div key={row.serviceKey} style={settingsServiceRowStyle}>
                          <div style={{ minWidth: 0 }}>
                            <p style={{ margin: 0, fontSize: 15, fontWeight: 800, color: "#23354d" }}>{row.title}</p>
                            <p style={{ margin: "4px 0 0", fontSize: 13, color: "#5f6f86" }}>{row.serviceKey}</p>
                            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
                              {isSelected ? (
                                <span style={{ ...settingsPillBaseStyle, border: "1px solid rgba(79,115,170,0.18)", background: "rgba(127,165,219,0.16)", color: "#3e5d8d" }}>
                                  Selected
                                </span>
                              ) : null}
                              {isLive ? (
                                <span style={{ ...settingsPillBaseStyle, border: "1px solid rgba(91,179,130,0.24)", background: "rgba(91,179,130,0.14)", color: "#3b7d5c" }}>
                                  Live
                                </span>
                              ) : null}
                            </div>
                          </div>
                          <button
                            onClick={() => removeService(row.serviceKey)}
                            disabled={serviceManageBusy || deletingServiceKey.length > 0 || isLive}
                            style={{
                              ...settingsButtonDangerStyle,
                              padding: "9px 12px",
                              opacity: serviceManageBusy || deletingServiceKey.length > 0 || isLive ? 0.55 : 1,
                              cursor: serviceManageBusy || deletingServiceKey.length > 0 || isLive ? "not-allowed" : "pointer",
                            }}
                          >
                            {deleting ? "Deleting..." : "Delete"}
                          </button>
                        </div>
                      );
                    })}
                  </div>
                </section>
              </div>
            ) : (
              <div style={{ ...settingsCardStyle, marginTop: 12, fontSize: 13, color: "#5f6f86" }}>
                You do not have permission to manage service schedules for this church.
              </div>
            )
          ) : null}
          {activeTab === "team" ? (
            canManageInvites ? (
              <div style={settingsShellStyle}>
                <section style={settingsCardStyle}>
                  <div style={{ display: "grid", gap: 8 }}>
                    <p style={settingsSectionLabelStyle}>Team Access</p>
                    <h3 style={settingsTitleStyle}>Invite Team Member</h3>
                    <p style={settingsBodyTextStyle}>
                      Create a one-time invite link for another user to join this church as host or admin.
                    </p>
                  </div>

                  <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "end" }}>
                    <label style={{ display: "grid", gap: 6 }}>
                      <span style={{ fontSize: 12, color: "#5f6f86" }}>Role</span>
                      <select
                        value={inviteRole}
                        onChange={(e) => setInviteRole(e.target.value as InviteRoleChoice)}
                        style={{ ...settingsInlineFieldStyle, minWidth: 180 }}
                      >
                        <option value="host">host</option>
                        <option value="admin">admin</option>
                      </select>
                    </label>
                    <button
                      onClick={generateInviteLink}
                      disabled={inviteBusy || !resolvedOrgId}
                      style={{
                        ...settingsButtonPrimaryStyle,
                        opacity: inviteBusy || !resolvedOrgId ? 0.6 : 1,
                        cursor: inviteBusy || !resolvedOrgId ? "not-allowed" : "pointer",
                      }}
                    >
                      {inviteBusy ? "Generating..." : "Generate Invite Link"}
                    </button>
                  </div>

                  {inviteError ? <p style={{ margin: 0, color: "#b95567", fontSize: 13 }}>Error: {inviteError}</p> : null}
                  {inviteNotice ? <p style={{ margin: 0, color: "#3b7d5c", fontSize: 13 }}>{inviteNotice}</p> : null}

                  {inviteLink ? (
                    <div
                      style={{
                        borderRadius: 16,
                        border: "1px solid rgba(189,200,217,0.84)",
                        background: "rgba(255,255,255,0.68)",
                        padding: "12px 14px",
                        display: "grid",
                        gap: 10,
                      }}
                    >
                      <label style={{ display: "grid", gap: 6 }}>
                        <span style={{ fontSize: 13, color: "#42556f", fontWeight: 700 }}>Invite URL</span>
                        <input readOnly value={inviteLink} style={{ ...settingsInlineFieldStyle, width: "100%" }} />
                      </label>
                      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                        <button
                          onClick={copyInviteLink}
                          disabled={copyBusy || shareBusy || inviteBusy}
                          style={{
                            ...settingsButtonNeutralStyle,
                            opacity: copyBusy || shareBusy || inviteBusy ? 0.6 : 1,
                            cursor: copyBusy || shareBusy || inviteBusy ? "not-allowed" : "pointer",
                          }}
                        >
                          {copyBusy ? "Copying..." : "Copy Link"}
                        </button>
                        <button
                          onClick={shareInviteLink}
                          disabled={copyBusy || shareBusy || inviteBusy}
                          style={{
                            ...settingsButtonPrimaryStyle,
                            opacity: copyBusy || shareBusy || inviteBusy ? 0.6 : 1,
                            cursor: copyBusy || shareBusy || inviteBusy ? "not-allowed" : "pointer",
                          }}
                        >
                          {shareBusy ? "Sharing..." : "Share via..."}
                        </button>
                      </div>
                    </div>
                  ) : null}
                </section>

                <section style={settingsCardStyle}>
                  <div style={{ display: "grid", gap: 8 }}>
                    <p style={settingsSectionLabelStyle}>Active Access</p>
                    <h3 style={settingsTitleStyle}>Active Invites</h3>
                    <p style={settingsBodyTextStyle}>
                      Review one-time invite links that are still active for this church.
                    </p>
                  </div>

                  {invitesLoading ? <p style={settingsBodyTextStyle}>Loading invites...</p> : null}
                  {!invitesLoading && !inviteRows.length ? <p style={settingsBodyTextStyle}>No active invites.</p> : null}
                  {inviteRows.length ? (
                    <div style={{ display: "grid", gap: 10 }}>
                      {inviteRows.map((row) => (
                        <div key={row.inviteId} style={settingsServiceRowStyle}>
                          <div style={{ minWidth: 0 }}>
                            <p style={{ margin: 0, fontSize: 15, fontWeight: 800, color: "#23354d" }}>Role: {row.role}</p>
                            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
                              <span style={{ ...settingsPillBaseStyle, border: "1px solid rgba(79,115,170,0.18)", background: "rgba(127,165,219,0.16)", color: "#3e5d8d" }}>
                                Expires {formatDateTime(row.expiresAt || null)}
                              </span>
                              <span style={{ ...settingsPillBaseStyle, border: "1px solid rgba(189,200,217,0.66)", background: "rgba(247,250,253,0.74)", color: "#5f6f86" }}>
                                Created {formatDateTime(row.createdAt || null)}
                              </span>
                            </div>
                          </div>
                          <button
                            onClick={() => revokeInvite(row.inviteId)}
                            disabled={Boolean(revokingInviteId) || inviteBusy}
                            style={{
                              ...settingsButtonDangerStyle,
                              padding: "9px 12px",
                              opacity: Boolean(revokingInviteId) || inviteBusy ? 0.6 : 1,
                              cursor: Boolean(revokingInviteId) || inviteBusy ? "not-allowed" : "pointer",
                            }}
                          >
                            {revokingInviteId === row.inviteId ? "Revoking..." : "Revoke"}
                          </button>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </section>
              </div>
            ) : (
              <div style={{ ...settingsCardStyle, marginTop: 12, fontSize: 13, color: "#5f6f86" }}>
                You do not have permission to manage team invites for this church.
              </div>
            )
          ) : null}
        </section>

        {activeTab === "broadcast" ? (
          activeRoomId ? (
            <section>
              <TranslationBox />
            </section>
          ) : (
            <section style={{ border: "1px dashed rgba(255,255,255,0.2)", borderRadius: 14, padding: 16, opacity: 0.82 }}>
              Start a service to enable producer controls.
            </section>
          )
        ) : null}
      </div>
    </main>
  );
}
