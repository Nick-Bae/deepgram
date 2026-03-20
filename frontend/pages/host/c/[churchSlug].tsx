import Link from "next/link";
import { useRouter } from "next/router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { sendEmailVerification } from "firebase/auth";

import TranslationBox from "../../../components/TranslationBox";
import { useAuth } from "../../../lib/authContext";
import { getFirebaseClient } from "../../../lib/firebaseClient";
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
type HostTab = "broadcast" | "settings" | "billing" | "team";
const PAID_PLAN_KEYS: PaidPlanKey[] = ["starter", "growth", "premium"];

const _PLAN_LABELS: Record<PaidPlanKey, string> = {
  starter: "Starter (5 services / $20)",
  growth: "Growth (12 services / $40)",
  premium: "Premium (Unlimited / $60)",
};

const PLAN_SUMMARIES: Record<PaidPlanKey, { title: string; monthlyPrice: string; serviceLimit: string }> = {
  starter: {
    title: "Starter",
    monthlyPrice: "$20 / month",
    serviceLimit: "Up to 5 service keys",
  },
  growth: {
    title: "Growth",
    monthlyPrice: "$40 / month",
    serviceLimit: "Up to 12 service keys",
  },
  premium: {
    title: "Premium",
    monthlyPrice: "$60 / month",
    serviceLimit: "Unlimited service keys",
  },
};

function resolveHostTab(raw: string): HostTab {
  const token = (raw || "").trim().toLowerCase();
  if (token === "settings" || token === "billing" || token === "team") return token;
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

function isNetworkError(err: unknown): boolean {
  return err instanceof TypeError && /fetch|network|failed/i.test((err as TypeError).message);
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

function formatDateTime(raw?: string | null, options?: Intl.DateTimeFormatOptions): string {
  const txt = (raw || "").trim();
  if (!txt) return "-";
  const parsed = new Date(txt);
  if (Number.isNaN(parsed.getTime())) return txt;
  return parsed.toLocaleString(undefined, options);
}

function formatPlanLabel(planKey: string): string {
  const token = (planKey || "").trim().toLowerCase();
  if (token === "trial") return "Trial";
  if (token === "starter") return "Starter";
  if (token === "growth") return "Growth";
  if (token === "premium") return "Premium";
  return token || "Unknown";
}

const subscriptionPeriodDateTimeOptions: Intl.DateTimeFormatOptions = {
  dateStyle: "medium",
  timeStyle: "short",
};

function formatBillingStatus(status: string): string {
  const token = (status || "").trim().toLowerCase();
  if (!token) return "unknown";
  return token.replace(/_/g, " ");
}

function formatCountdownSeconds(rawSeconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(Number.isFinite(rawSeconds) ? rawSeconds : 0));
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
  const busyRef = useRef(false);
  const [backendReachable, setBackendReachable] = useState(true);
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
  const [trialCountdownSeconds, setTrialCountdownSeconds] = useState<number | null>(null);
  const [sermonUsageState, setSermonUsageState] = useState<OrgSermonUsageResponse | null>(null);
  const [sermonBudgetInput, setSermonBudgetInput] = useState("0");
  const [sermonBudgetBusy, setSermonBudgetBusy] = useState(false);
  const [sermonBudgetError, setSermonBudgetError] = useState<string | null>(null);
  const [sermonBudgetNotice, setSermonBudgetNotice] = useState<string | null>(null);
  const [verificationRequired, setVerificationRequired] = useState(false);
  const [verificationSending, setVerificationSending] = useState(false);
  const [verificationError, setVerificationError] = useState<string | null>(null);
  const normalizedServiceKey = serviceKey.trim();
  const normalizedQueryServiceKey = queryServiceKey.trim();
  const activeTab = resolveHostTab(querySection);
  const isBillingTab = activeTab === "billing";
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
  const trialSecondsLimit = trialMinutesLimit > 0 ? trialMinutesLimit * 60 : 0;
  const trialSecondsUsed = Math.max(
    0,
    Number.isFinite(Number(billingProfile?.trialSecondsUsed))
      ? Number(billingProfile?.trialSecondsUsed || 0)
      : trialMinutesUsed * 60,
  );
  const trialSecondsRemaining = Number.isFinite(Number(billingProfile?.trialSecondsRemaining))
    ? Math.max(0, Number(billingProfile?.trialSecondsRemaining || 0))
    : trialSecondsLimit > 0
      ? Math.max(0, trialSecondsLimit - trialSecondsUsed)
      : null;
  const trialCountdownKeyRef = useRef("");
  const effectiveTrialCountdownSeconds = useMemo(() => {
    if (!isTrialPlan || trialSecondsRemaining === null) return null;
    if (trialCountdownSeconds !== null) return trialCountdownSeconds;
    return trialSecondsRemaining;
  }, [isTrialPlan, trialCountdownSeconds, trialSecondsRemaining]);
  const isTrialExpired = isTrialPlan && effectiveTrialCountdownSeconds !== null && effectiveTrialCountdownSeconds <= 0;
  const startServiceDisabled = busy || isTrialExpired;
  const trialNoticeCheckpointRef = useRef<"" | "warn5" | "warn1" | "expired">("");
  const hasPaidPlan = PAID_PLAN_KEYS.includes(billingPlanToken as PaidPlanKey);
  const hasActiveLikeSubscription = billingStatusToken === "active" || billingStatusToken === "trialing" || billingStatusToken === "past_due";
  const billingNeedsAttention = isTrialExpired || ["past_due", "canceled", "unpaid", "incomplete"].includes(billingStatusToken);
  const billingAlertMessage = isTrialExpired
    ? "Trial access has ended. Review billing before the next broadcast."
    : billingStatusToken === "past_due"
      ? "Billing is past due. Update payment details to avoid losing access."
      : billingStatusToken === "canceled"
        ? "This subscription is canceled. Confirm the period end and next steps."
        : billingStatusToken === "unpaid" || billingStatusToken === "incomplete"
          ? "Subscription setup is incomplete. Open billing to finish activation."
          : "";
  const selectablePaidPlans = useMemo(() => {
    if (!hasPaidPlan || !hasActiveLikeSubscription) return PAID_PLAN_KEYS;
    return PAID_PLAN_KEYS.filter((plan) => plan !== (billingPlanToken as PaidPlanKey));
  }, [billingPlanToken, hasActiveLikeSubscription, hasPaidPlan]);
  const serviceKeyForStart = useMemo(() => {
    const serviceRows = orgData?.services || [];
    if (!serviceRows.length) return (normalizedServiceKey || normalizedQueryServiceKey).trim();
    if (normalizedServiceKey && serviceRows.some((row) => row.serviceKey === normalizedServiceKey)) return normalizedServiceKey;
    if (normalizedQueryServiceKey && serviceRows.some((row) => row.serviceKey === normalizedQueryServiceKey)) return normalizedQueryServiceKey;
    return (serviceRows[0]?.serviceKey || "").trim();
  }, [normalizedQueryServiceKey, normalizedServiceKey, orgData?.services]);

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
    setTrialCountdownSeconds(null);
    trialCountdownKeyRef.current = "";
    trialNoticeCheckpointRef.current = "";
    setSermonUsageState(null);
    setSermonBudgetInput("0");
    setSermonBudgetBusy(false);
    setSermonBudgetError(null);
    setSermonBudgetNotice(null);
  }, [resolvedOrgId]);

  useEffect(() => {
    if (!isTrialPlan || trialSecondsRemaining === null) {
      setTrialCountdownSeconds(null);
      trialCountdownKeyRef.current = "";
      return;
    }

    const nextSeconds = trialSecondsRemaining;
    const countdownKey = `${resolvedOrgId}:${billingPlanToken}:${trialSecondsLimit}`;
    const keyChanged = trialCountdownKeyRef.current !== countdownKey;
    trialCountdownKeyRef.current = countdownKey;

    setTrialCountdownSeconds((prev) => {
      if (keyChanged || prev === null) return nextSeconds;
      if (activeRoomId) {
        const liveDelta = prev - nextSeconds;
        // While the room stays open, trust the local second timer unless the backend
        // is materially lower. This avoids immediate one-minute jumps on room start.
        if (liveDelta >= 0 && liveDelta <= 60) return prev;
      }
      return Math.min(prev, nextSeconds);
    });
  }, [activeRoomId, billingPlanToken, isTrialPlan, resolvedOrgId, trialSecondsLimit, trialSecondsRemaining]);

  useEffect(() => {
    if (!isTrialPlan || !activeRoomId) return;
    const timer = window.setInterval(() => {
      setTrialCountdownSeconds((prev) => {
        if (prev === null) return prev;
        return Math.max(0, prev - 1);
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [activeRoomId, isTrialPlan]);

  useEffect(() => {
    if (authLoading) return;
    if (user) return;
    const nextPath = router.asPath || `/host/c/${encodeURIComponent(slug || "demo")}/broadcast`;
    router.replace(`/login?next=${encodeURIComponent(nextPath)}`);
  }, [authLoading, router, slug, user]);

  useEffect(() => {
    if (authLoading || !user) return;
    setVerificationRequired(!user.emailVerified);
  }, [authLoading, user]);

  const handleResendVerification = useCallback(async () => {
    if (!user) return;
    setVerificationSending(true);
    setVerificationError(null);
    try {
      await sendEmailVerification(user);
    } catch (err) {
      setVerificationError(err instanceof Error ? err.message : "Failed to send verification email.");
    } finally {
      setVerificationSending(false);
    }
  }, [user]);

  const handleCheckVerification = useCallback(async () => {
    if (!user) return;
    setVerificationSending(true);
    setVerificationError(null);
    try {
      await user.reload();
      const fresh = getFirebaseClient()?.auth.currentUser;
      if (fresh?.emailVerified) {
        setVerificationRequired(false);
      } else {
        setVerificationError("Email not yet verified. Check your inbox and click the link.");
      }
    } catch (err) {
      setVerificationError(err instanceof Error ? err.message : "Failed to check verification status.");
    } finally {
      setVerificationSending(false);
    }
  }, [user]);

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
      // Don't poll while a user action is in flight — avoids overwriting action errors.
      if (busyRef.current) return;
      try {
        await refreshServices();
        if (!disposed) setBackendReachable(true);
      } catch (err: unknown) {
        if (disposed) return;
        if (isNetworkError(err)) {
          setBackendReachable(false);
        } else {
          setBackendReachable(true);
          const message = err instanceof Error ? err.message : String(err);
          setErrorMsg(message || "services_failed");
        }
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
    if (!orgData?.services?.length) return;
    if (normalizedServiceKey) return;
    if (!serviceKeyForStart || serviceKey === serviceKeyForStart) return;
    setServiceKey(serviceKeyForStart);
  }, [normalizedServiceKey, orgData?.services?.length, serviceKey, serviceKeyForStart]);

  useEffect(() => {
    if (!selectedService) return;
    if (selectedService.defaultLanguagePair?.source) setSourceLang(selectedService.defaultLanguagePair.source);
    if (selectedService.defaultLanguagePair?.target) setTargetLang(selectedService.defaultLanguagePair.target);
  }, [selectedService?.serviceKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const displayUrl = useMemo(() => {
    const listenerServiceKey = (normalizedServiceKey || serviceKeyForStart).trim();
    if (!origin || !slug || !listenerServiceKey) return "";
    return `${origin}/c/${encodeURIComponent(slug)}/s/${encodeURIComponent(listenerServiceKey)}`;
  }, [normalizedServiceKey, origin, serviceKeyForStart, slug]);

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
      if (forceRefresh || isBillingTab) {
        setBillingError(message || "Failed to load billing profile.");
      }
    } finally {
      if (forceRefresh) setBillingRefreshBusy(false);
    }
  }, [canManageServices, getIdToken, isBillingTab, resolvedOrgId]);

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
    if (!isBillingTab || !canManageBilling || !resolvedOrgId || authLoading || !user) return;
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
  }, [authLoading, canManageBilling, getIdToken, isBillingTab, resolvedOrgId, user]);

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
        if (!cancelled && isBillingTab) {
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
  }, [authLoading, canManageServices, getIdToken, isBillingTab, resolvedOrgId, user]);

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
    if (billingPlanToken !== "trial" || effectiveTrialCountdownSeconds === null) {
      trialNoticeCheckpointRef.current = "";
      setTrialBroadcastNotice(null);
      return;
    }
    if (effectiveTrialCountdownSeconds <= 0) {
      if (trialNoticeCheckpointRef.current !== "expired") {
        trialNoticeCheckpointRef.current = "expired";
        setTrialBroadcastNotice("Your 30-minute trial has ended. Upgrade to continue broadcasting.");
      }
      return;
    }
    if (effectiveTrialCountdownSeconds <= 60) {
      if (trialNoticeCheckpointRef.current !== "warn1" && trialNoticeCheckpointRef.current !== "expired") {
        trialNoticeCheckpointRef.current = "warn1";
        setTrialBroadcastNotice("Trial: 1 minute remaining. Broadcast will stop automatically when time runs out.");
      }
      return;
    }
    if (effectiveTrialCountdownSeconds <= 5 * 60) {
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
  }, [billingPlanToken, effectiveTrialCountdownSeconds]);

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
      const params = new URLSearchParams();
      params.set("code", created.code);
      params.set("church", (orgData?.name || slug || "church").trim());
      const joinPath = `/join?${params.toString()}`;
      const link = originBase ? `${originBase}${joinPath}` : joinPath;
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
        buildTabHref("billing", {
          orgId: resolvedOrgId,
          serviceKey: normalizedServiceKey || undefined,
          roomId: activeRoomId || undefined,
        }) || `/host/c/${encodeURIComponent(slug || "demo")}/billing`;
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
        buildTabHref("billing", {
          orgId: resolvedOrgId,
          serviceKey: normalizedServiceKey || undefined,
          roomId: activeRoomId || undefined,
        }) || `/host/c/${encodeURIComponent(slug || "demo")}/billing`;
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
    const startKey = serviceKeyForStart.trim();
    if (!resolvedOrgId && !slug) {
      setErrorMsg("Church slug is missing. Refresh the page.");
      return;
    }
    if (isTrialExpired && !activeRoomId) {
      setErrorMsg(ERROR_DETAIL_MESSAGES.trial_expired);
      return;
    }
    if (!startKey) {
      setErrorMsg("Enter a service key before starting.");
      return;
    }
    if (startKey !== serviceKey) setServiceKey(startKey);
    busyRef.current = true;
    setBusy(true);
    try {
      const idToken = await getIdToken();
      if (!idToken) throw new Error("Please sign in again.");
      persistAuthToken(idToken);
      const path = resolvedOrgId
        ? `/api/org/${encodeURIComponent(resolvedOrgId)}/service/${encodeURIComponent(startKey)}/start`
        : `/api/c/${encodeURIComponent(slug)}/service/${encodeURIComponent(startKey)}/start`;
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
        if (res.status === 402 && resolvedOrgId) {
          void loadBillingProfile({ refresh: true });
        }
        throw new Error(msg);
      }
      const data: StartResponse = await res.json();
      const nextOrgId = (data.orgId || resolvedOrgId || queryOrgId || "").trim();
      setActiveRoomId(data.roomId);
      if (data.serviceKey && data.serviceKey !== serviceKey) setServiceKey(data.serviceKey);
      persistStreamContext({
        orgId: nextOrgId || undefined,
        roomId: data.roomId,
        serviceKey: data.serviceKey || startKey,
        churchSlug: slug,
      });
      if (nextOrgId && nextOrgId !== queryOrgId) {
        const href = buildTabHref("broadcast", {
          serviceKey: data.serviceKey || startKey,
          orgId: nextOrgId,
          roomId: data.roomId,
        });
        if (href) {
          void router.replace(href, undefined, { shallow: true });
        }
      } else {
        syncHostUrl(data.roomId, data.serviceKey || startKey);
      }
      setErrorMsg(null);
      if (resolvedOrgId) {
        void loadBillingProfile({ refresh: true });
      }
    } catch (err: unknown) {
      const message = isNetworkError(err) ? "Server unreachable. Check your connection and try again." : err instanceof Error ? err.message : String(err);
      setErrorMsg(message || "start_failed");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const endService = async () => {
    if (!resolvedOrgId || !activeRoomId) return;
    busyRef.current = true;
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
      void loadBillingProfile({ refresh: true });
    } catch (err: unknown) {
      const message = isNetworkError(err) ? "Server unreachable. Check your connection and try again." : err instanceof Error ? err.message : String(err);
      setErrorMsg(message || "end_failed");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const hostPageStyle = {
    minHeight: "100vh",
    background: "linear-gradient(180deg, #e9edf4 0%, #dde4ee 56%, #d4dce8 100%)",
    color: "#0f172a",
    padding: "20px 14px 34px",
  } as const;
  const _dashboardPanelShadow = "0 18px 38px rgba(122,138,163,0.12)";
  const dashboardCardShadow = "0 14px 30px rgba(122,138,163,0.1)";
  const dashboardCompactShadow = "0 10px 22px rgba(122,138,163,0.08)";
  const hostTopPanelStyle = {
    borderRadius: 24,
    background: "linear-gradient(160deg, rgba(240,246,255,0.82) 0%, rgba(225,234,248,0.6) 100%)",
    boxShadow: "0 8px 32px rgba(79,115,170,0.08)",
    padding: 16,
  } as const;
  const hostTopIntroStyle = {
    margin: "0 0 18px",
    width: "100%",
    padding: "28px 28px 24px",
    borderRadius: 18,
    position: "relative" as const,
    overflow: "hidden" as const,
    background: "transparent",
  } as const;
  const hostTopIntroGlowStyle = {
    position: "absolute" as const,
    inset: "auto -40px -80px auto",
    width: 320,
    height: 220,
    borderRadius: 999,
    background: "radial-gradient(circle, rgba(127,165,219,0.18) 0%, rgba(79,115,170,0) 70%)",
    filter: "blur(18px)",
    pointerEvents: "none" as const,
  } as const;
  const _hostTopIntroRailStyle = { display: "none" } as const;
  const hostTopIntroContentStyle = {
    position: "relative" as const,
    display: "grid",
    gap: 10,
  } as const;
  const hostTopIntroBadgeStyle = {
    display: "inline-flex",
    alignItems: "center",
    width: "fit-content",
    padding: "4px 10px",
    borderRadius: 999,
    background: "rgba(79,115,170,0.1)",
    color: "#4f73aa",
    fontFamily: "Inter, 'Segoe UI', system-ui, -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif",
    fontSize: 11,
    lineHeight: 1,
    fontWeight: 800,
    letterSpacing: "0.14em",
    textTransform: "uppercase" as const,
  } as const;
  const hostTopIntroHeadlineStyle = {
    margin: 0,
    color: "#16324f",
    fontFamily: "Inter, 'Segoe UI', system-ui, -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif",
    fontSize: "clamp(24px, 3.2vw, 34px)",
    lineHeight: 1.05,
    fontWeight: 900,
    letterSpacing: "-0.05em",
  } as const;
  const hostTopIntroAccentStyle = {
    color: "#4f73aa",
    background: "linear-gradient(135deg, #7fa5db, #4f73aa)",
    backgroundClip: "text" as const,
    WebkitBackgroundClip: "text" as const,
    WebkitTextFillColor: "transparent",
  } as const;
  const hostTopIntroSubtextStyle = {
    margin: 0,
    maxWidth: 760,
    color: "#45607d",
    fontFamily: "Inter, 'Segoe UI', system-ui, -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif",
    fontSize: "clamp(13px, 1.6vw, 15px)",
    lineHeight: 1.55,
    fontWeight: 500,
    letterSpacing: "-0.01em",
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
    boxShadow: dashboardCompactShadow,
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
    padding: 0,
  } as const;
  const studioAdminButtonStyle = {
    borderRadius: 18,
    border: "1px solid rgba(99,102,241,0.5)",
    background: "rgba(99,102,241,0.15)",
    color: "#a5b4fc",
    fontSize: 12,
    fontWeight: 800,
    letterSpacing: "0.16em",
    textTransform: "uppercase" as const,
    padding: "12px 22px",
    cursor: "pointer",
    textDecoration: "none",
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
    fontSize: 40,
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
    boxShadow: dashboardCardShadow,
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
  const billingHeroCardStyle = {
    ...settingsCardStyle,
    padding: 22,
    background: "linear-gradient(145deg, rgba(247,241,232,0.99), rgba(232,220,203,0.95))",
    boxShadow: "0 18px 36px rgba(131,109,82,0.12)",
  } as const;
  const billingDeckStyle = {
    display: "grid",
    gap: 16,
    gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))",
  } as const;
  const billingPlanCardBaseStyle = {
    borderRadius: 22,
    padding: 18,
    display: "grid",
    gap: 10,
    minHeight: 220,
    background: "linear-gradient(145deg, rgba(255,255,255,0.9), rgba(242,236,229,0.9))",
    boxShadow: dashboardCompactShadow,
  } as const;
  const billingAlertStyle = {
    borderRadius: 18,
    border: "1px solid rgba(188,95,111,0.28)",
    background: "linear-gradient(145deg, rgba(255,244,245,0.94), rgba(252,231,236,0.92))",
    color: "#9f3650",
    padding: "12px 14px",
    fontSize: 13,
    fontWeight: 700,
    lineHeight: 1.5,
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
  const supportFooterStyle = {
    padding: "4px 2px 0",
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
  } as const;
  const supportFooterLinkStyle = {
    border: "none",
    background: "transparent",
    padding: 0,
    color: "#4f73aa",
    fontSize: 13,
    fontWeight: 800,
    cursor: "pointer",
    textDecoration: "underline",
    textUnderlineOffset: "3px",
    whiteSpace: "nowrap" as const,
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
    boxShadow: "0 8px 16px rgba(122,138,163,0.08)",
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
  const currentChurchLabel = (orgData?.name || slug || "Current Church").trim();
  const currentChurchInitial = currentChurchLabel.charAt(0).toUpperCase() || "C";
  const churchPublicPath = slug ? `${origin || ""}/c/${slug}` : "";
  const dashboardContactHref = useMemo(() => {
    const topic = activeTab === "billing" ? "billing" : activeTab === "broadcast" ? "translation" : "setup";
    const sectionLabel = activeTab === "billing"
      ? "Billing & Subscription"
      : activeTab === "team"
        ? "Team"
        : activeTab === "settings"
          ? "Church Settings"
          : "Live Broadcast";
    const params = new URLSearchParams();
    params.set("topic", topic);
    params.set("organization", currentChurchLabel);
    params.set(
      "message",
      [
        `Church: ${currentChurchLabel}`,
        `User: ${currentUserName}`,
        `Current section: ${sectionLabel}`,
        churchPublicPath ? `Public page: ${churchPublicPath}` : "",
        "",
        "How can we help?",
      ]
        .filter(Boolean)
        .join("\n"),
    );
    return `/contact?${params.toString()}`;
  }, [activeTab, churchPublicPath, currentChurchLabel, currentUserName]);

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

  if (!authLoading && user && verificationRequired) {
    return (
      <main style={{ ...hostPageStyle, display: "grid", placeItems: "center" }}>
        <div
          style={{
            background: "rgba(255,255,255,0.06)",
            border: "1px solid rgba(255,255,255,0.12)",
            borderRadius: 16,
            padding: "32px 28px",
            maxWidth: 440,
            display: "grid",
            gap: 16,
            color: "#e8e8e8",
            fontFamily: "inherit",
          }}
        >
          <h2 style={{ margin: 0, fontSize: 20, fontWeight: 700 }}>Verify Your Email</h2>
          <p style={{ margin: 0, fontSize: 14, lineHeight: 1.6, color: "#b0b8c8" }}>
            A verification link was sent to <strong style={{ color: "#e8e8e8" }}>{user.email}</strong>.
            Please click the link in that email before accessing the host console.
          </p>
          {verificationError ? (
            <p style={{ margin: 0, fontSize: 13, color: "#e88a8a" }}>{verificationError}</p>
          ) : null}
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            <button
              onClick={handleResendVerification}
              disabled={verificationSending}
              style={{
                padding: "9px 16px",
                borderRadius: 8,
                border: "1px solid rgba(255,255,255,0.2)",
                background: "rgba(255,255,255,0.08)",
                color: "#e8e8e8",
                fontSize: 13,
                cursor: verificationSending ? "not-allowed" : "pointer",
                opacity: verificationSending ? 0.6 : 1,
              }}
            >
              {verificationSending ? "Sending..." : "Resend Verification Email"}
            </button>
            <button
              onClick={handleCheckVerification}
              disabled={verificationSending}
              style={{
                padding: "9px 16px",
                borderRadius: 8,
                border: "1px solid rgba(255,255,255,0.2)",
                background: "rgba(255,255,255,0.08)",
                color: "#e8e8e8",
                fontSize: 13,
                cursor: verificationSending ? "not-allowed" : "pointer",
                opacity: verificationSending ? 0.6 : 1,
              }}
            >
              {verificationSending ? "Checking..." : "Check Again"}
            </button>
          </div>
        </div>
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
              <div style={studioBrandTileStyle}>{currentChurchInitial}</div>
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
                  {currentChurchLabel}
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

            <div style={studioUserPanelStyle}>
              <div style={{ display: "flex", alignItems: "center", gap: 12, minWidth: 0 }}>
                <span
                  style={{
                    color: "#e1e8f4",
                    fontSize: 20,
                    fontWeight: 500,
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    maxWidth: 280,
                  }}
                >
                  {currentUserName}
                </span>
              </div>
              {isMasterUser && (
                <Link href="/admin" style={studioAdminButtonStyle}>
                  Admin
                </Link>
              )}
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
          <div style={hostTopIntroStyle}>
            <div aria-hidden="true" style={hostTopIntroGlowStyle} />
            <div style={hostTopIntroContentStyle}>
              <span style={hostTopIntroBadgeStyle}>How to broadcast</span>
              <h2 style={hostTopIntroHeadlineStyle}>
                Launch Your <span style={hostTopIntroAccentStyle}>Broadcast</span>
              </h2>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 4, alignItems: "center" }}>
                {(["Select a service", "Open the Control Panel", "Start Translation"] as const).map((label, i) => (
                  <div key={label} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 7, padding: "6px 14px 6px 8px", borderRadius: 999, background: "rgba(79,115,170,0.08)" }}>
                      <span style={{ width: 22, height: 22, borderRadius: "50%", background: accentPrimaryGradient, color: "#fff", fontSize: 11, fontWeight: 800, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, boxShadow: "0 2px 8px rgba(79,115,170,0.28)" }}>{i + 1}</span>
                      <span style={hostTopIntroSubtextStyle}>{label}</span>
                    </div>
                    {i < 2 && <span style={{ color: "rgba(79,115,170,0.4)", fontSize: 16, fontWeight: 300, lineHeight: 1 }}>→</span>}
                  </div>
                ))}
              </div>
            </div>
          </div>
          {!backendReachable ? (
            <div style={{ marginBottom: 8, padding: "8px 12px", borderRadius: 10, background: "rgba(120,53,15,0.18)", border: "1px solid rgba(251,191,36,0.5)", color: "#92400e", fontSize: 13, fontWeight: 600, display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: "#f59e0b", flexShrink: 0, display: "inline-block" }} />
              <span style={{ flex: 1 }}>Server unreachable — reconnecting…</span>
              <button
                type="button"
                onClick={() => {
                  void refreshServices().then(() => setBackendReachable(true)).catch(() => {});
                }}
                style={{ background: "rgba(245,158,11,0.18)", border: "1px solid rgba(245,158,11,0.45)", color: "#92400e", borderRadius: 6, padding: "3px 10px", fontSize: 12, fontWeight: 700, cursor: "pointer" }}
              >
                Retry now
              </button>
            </div>
          ) : null}
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
              onClick={() => navigateToTab("billing")}
              style={{
                borderRadius: 10,
                border: activeTab === "billing"
                  ? "1px solid rgba(79,115,170,0.38)"
                  : billingNeedsAttention
                    ? "1px solid rgba(188,95,111,0.34)"
                    : "1px solid rgba(189,200,217,0.92)",
                background: activeTab === "billing"
                  ? accentPrimaryGradient
                  : billingNeedsAttention
                    ? "linear-gradient(145deg, rgba(255,245,245,0.92), rgba(252,229,229,0.9))"
                    : "rgba(247,250,253,0.78)",
                color: activeTab === "billing" ? "#f8fafc" : billingNeedsAttention ? "#a94457" : "#42556f",
                fontSize: 13,
                fontWeight: 800,
                padding: "8px 12px",
                cursor: "pointer",
                boxShadow: activeTab === "billing"
                  ? accentPrimaryShadow
                  : billingNeedsAttention
                    ? "0 12px 24px rgba(188,95,111,0.14)"
                    : "8px 8px 18px rgba(122,138,163,0.12), -8px -8px 18px rgba(255,255,255,0.78)",
              }}
            >
              Billing & Subscription
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
                
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 12, alignItems: "center" }}>
                <button
                  onClick={startService}
                  disabled={startServiceDisabled}
                  style={{
                    borderRadius: 10,
                    border: "1px solid rgba(79,115,170,0.3)",
                    background: accentPrimaryGradient,
                    color: "#f8fafc",
                    fontWeight: 700,
                    padding: "9px 14px",
                    cursor: startServiceDisabled ? "not-allowed" : "pointer",
                    opacity: startServiceDisabled ? 0.6 : 1,
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
                {billingPlanToken === "trial" && effectiveTrialCountdownSeconds !== null ? (
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
                    Trial remaining: {formatCountdownSeconds(effectiveTrialCountdownSeconds)}
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
              {displayUrl ? (
                <div style={{ marginTop: 12, padding: "10px 14px", borderRadius: 10, background: "rgba(79,115,170,0.07)", display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
                  <span style={{ fontSize: 11, fontWeight: 800, letterSpacing: "0.1em", textTransform: "uppercase", color: "#4f73aa", flexShrink: 0 }}>Display URL</span>
                  <a href={displayUrl} target="_blank" rel="noreferrer" style={{ fontSize: 13, color: "#2563eb", wordBreak: "break-all", lineHeight: 1.4 }}>{displayUrl}</a>
                </div>
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
                    <p style={settingsSectionLabelStyle}>Billing Snapshot</p>
                    <h3 style={settingsTitleStyle}>Billing & Subscription</h3>
                    <p style={settingsBodyTextStyle}>
                      View the current plan and renewal timing here. Open Billing for plan changes, payment details, and billing controls.
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
                              : billingNeedsAttention
                                ? "1px solid rgba(188,95,111,0.26)"
                                : "1px solid rgba(189,200,217,0.9)",
                          background: billingStatusToken === "active"
                            ? "rgba(91,179,130,0.14)"
                            : billingStatusToken === "past_due"
                              ? "rgba(224,163,86,0.16)"
                              : billingNeedsAttention
                                ? "rgba(188,95,111,0.12)"
                                : "rgba(247,250,253,0.8)",
                          color: billingStatusToken === "active"
                            ? "#3b7d5c"
                            : billingStatusToken === "past_due"
                              ? "#9a6433"
                              : billingNeedsAttention
                                ? "#a94457"
                                : "#55657d",
                        }}
                      >
                        Status · {formatBillingStatus(billingStatusToken)}
                      </span>
                      <span style={{ ...settingsPillBaseStyle, border: "1px solid rgba(189,200,217,0.95)", background: "rgba(247,250,253,0.8)", color: "#55657d" }}>
                        Services · {billingMaxServiceKeys > 0 ? `up to ${billingMaxServiceKeys}` : "unlimited"}
                      </span>
                    </div>

                    {billingAlertMessage ? <div style={billingAlertStyle}>{billingAlertMessage}</div> : null}

                    {billingProfile ? (
                      <div style={{ display: "grid", gap: 8 }}>
                        {hasSubscriptionPeriod ? (
                          <div style={{ borderRadius: 16, border: "1px solid rgba(189,200,217,0.8)", background: "rgba(255,255,255,0.7)", padding: "12px 14px" }}>
                            <p style={{ ...settingsSectionLabelStyle, fontSize: 10 }}>Subscription Period</p>
                            <p style={{ margin: "6px 0 0", fontSize: 13, color: "#334155" }}>
                              <strong>{formatDateTime(billingProfile.currentPeriodStart, subscriptionPeriodDateTimeOptions)}</strong>
                              {" → "}
                              <strong>{formatDateTime(billingProfile.currentPeriodEnd, subscriptionPeriodDateTimeOptions)}</strong>
                              {billingProfile.cancelAtPeriodEnd ? " · Cancels at period end" : ""}
                            </p>
                          </div>
                        ) : null}
                        {isTrialPlan && !hasSubscriptionPeriod && effectiveTrialCountdownSeconds !== null ? (
                          <p style={settingsBodyTextStyle}>
                            Trial usage: <strong>{trialMinutesUsed}</strong> / <strong>{trialMinutesLimit}</strong> minutes
                            {" · "}
                            Remaining: <strong>{formatCountdownSeconds(effectiveTrialCountdownSeconds)}</strong>
                          </p>
                        ) : null}
                        {isTrialPlan && !hasSubscriptionPeriod && effectiveTrialCountdownSeconds === null ? (
                          <p style={settingsBodyTextStyle}>Trial usage details will appear after the next usage tick.</p>
                        ) : null}
                        {!isTrialPlan && !hasSubscriptionPeriod ? (
                          <p style={settingsBodyTextStyle}>Subscription period is syncing from Stripe. Open billing to review the latest details.</p>
                        ) : null}
                      </div>
                    ) : (
                      <p style={settingsBodyTextStyle}>Loading billing summary…</p>
                    )}

                    <div>
                      <button
                        onClick={() => navigateToTab("billing")}
                        style={settingsButtonPrimaryStyle}
                      >
                        Open Billing & Subscription
                      </button>
                    </div>
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
          {activeTab === "billing" ? (
            canManageServices ? (
              <div style={settingsShellStyle}>
                <section style={billingHeroCardStyle}>
                  <div style={{ display: "grid", gap: 8 }}>
                    <p style={{ ...settingsSectionLabelStyle, color: "#8a6441" }}>Billing & Subscription</p>
                    <h3 style={{ ...settingsTitleStyle, fontSize: 28, lineHeight: 1.05 }}>Review plan, renewal, and subscriptions</h3>
                    <p style={{ ...settingsBodyTextStyle, maxWidth: 760, color: "#6f5a43" }}>
                      Review subscription status, renewal timing, plan options, billing limits, and Sermon Prep budget in one place.
                    </p>
                  </div>

                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                    <span style={{ ...settingsPillBaseStyle, border: "1px solid rgba(138,100,65,0.18)", background: "rgba(170,128,91,0.12)", color: "#845f3d" }}>
                      Plan · {formatPlanLabel(billingPlanToken)}
                    </span>
                    <span
                      style={{
                        ...settingsPillBaseStyle,
                        border: billingStatusToken === "active"
                          ? "1px solid rgba(91,179,130,0.22)"
                          : billingStatusToken === "past_due"
                            ? "1px solid rgba(224,163,86,0.26)"
                            : billingNeedsAttention
                              ? "1px solid rgba(188,95,111,0.26)"
                              : "1px solid rgba(189,200,217,0.9)",
                        background: billingStatusToken === "active"
                          ? "rgba(91,179,130,0.14)"
                          : billingStatusToken === "past_due"
                            ? "rgba(224,163,86,0.16)"
                            : billingNeedsAttention
                              ? "rgba(188,95,111,0.12)"
                              : "rgba(247,250,253,0.8)",
                        color: billingStatusToken === "active"
                          ? "#3b7d5c"
                          : billingStatusToken === "past_due"
                            ? "#9a6433"
                            : billingNeedsAttention
                              ? "#a94457"
                              : "#55657d",
                      }}
                    >
                      Status · {formatBillingStatus(billingStatusToken)}
                    </span>
                    <span style={{ ...settingsPillBaseStyle, border: "1px solid rgba(189,200,217,0.95)", background: "rgba(247,250,253,0.8)", color: "#55657d" }}>
                      Service keys · {billingMaxServiceKeys > 0 ? `up to ${billingMaxServiceKeys}` : "unlimited"}
                    </span>
                  </div>

                  {billingAlertMessage ? <div style={billingAlertStyle}>{billingAlertMessage}</div> : null}
                  {billingError ? <p style={{ margin: 0, color: "#b95567", fontSize: 13 }}>Error: {billingError}</p> : null}
                  {billingNotice ? <p style={{ margin: 0, color: "#3b7d5c", fontSize: 13 }}>{billingNotice}</p> : null}

                  {billingProfile ? (
                    <div style={settingsGridStyle}>
                      <div style={{ borderRadius: 18, border: "1px solid rgba(189,200,217,0.8)", background: "rgba(255,255,255,0.68)", padding: "14px 16px" }}>
                        <p style={{ ...settingsSectionLabelStyle, fontSize: 10 }}>Current Term</p>
                        <p style={{ margin: "8px 0 0", fontSize: 14, color: "#334155", lineHeight: 1.6 }}>
                          {hasSubscriptionPeriod ? (
                            <>
                              <strong>{formatDateTime(billingProfile.currentPeriodStart, subscriptionPeriodDateTimeOptions)}</strong>
                              {" → "}
                              <strong>{formatDateTime(billingProfile.currentPeriodEnd, subscriptionPeriodDateTimeOptions)}</strong>
                              {billingProfile.cancelAtPeriodEnd ? " · Cancels at period end" : ""}
                            </>
                          ) : isTrialPlan && effectiveTrialCountdownSeconds !== null ? (
                            <>
                              Trial time remaining: <strong>{formatCountdownSeconds(effectiveTrialCountdownSeconds)}</strong>
                            </>
                          ) : isTrialPlan ? (
                            "Trial usage details will appear after the next usage tick."
                          ) : (
                            "Subscription period is syncing from Stripe."
                          )}
                        </p>
                      </div>
                      <div style={{ borderRadius: 18, border: "1px solid rgba(189,200,217,0.8)", background: "rgba(255,255,255,0.68)", padding: "14px 16px" }}>
                        <p style={{ ...settingsSectionLabelStyle, fontSize: 10 }}>Usage Snapshot</p>
                        <p style={{ margin: "8px 0 0", fontSize: 14, color: "#334155", lineHeight: 1.6 }}>
                          {isTrialPlan ? (
                            <>
                              Trial usage: <strong>{trialMinutesUsed}</strong> / <strong>{trialMinutesLimit}</strong> minutes
                            </>
                          ) : (
                            <>
                              Billing state is tracked per church and applies to all recurring services in this workspace.
                            </>
                          )}
                        </p>
                      </div>
                    </div>
                  ) : (
                    <p style={settingsBodyTextStyle}>Loading billing profile…</p>
                  )}
                </section>

                <section style={settingsCardStyle}>
                  <div style={{ display: "grid", gap: 8 }}>
                    <p style={settingsSectionLabelStyle}>Plans</p>
                    <h3 style={settingsTitleStyle}>Choose the next plan</h3>
                    <p style={settingsBodyTextStyle}>
                      Review plan size and pricing here before opening Stripe checkout.
                    </p>
                  </div>

                  <div style={billingDeckStyle}>
                    {PAID_PLAN_KEYS.map((plan) => {
                      const isSelectedPlan = selectedPlan === plan;
                      const isCurrentPlan = hasPaidPlan && billingPlanToken === plan && hasActiveLikeSubscription;
                      return (
                        <button
                          key={plan}
                          type="button"
                          onClick={() => {
                            if (!canManagePaidBilling) return;
                            setSelectedPlan(plan);
                          }}
                          disabled={!canManagePaidBilling}
                          style={{
                            ...billingPlanCardBaseStyle,
                            textAlign: "left",
                            border: isSelectedPlan
                              ? "1px solid rgba(79,115,170,0.3)"
                              : isCurrentPlan
                                ? "1px solid rgba(91,179,130,0.28)"
                                : "1px solid rgba(214,220,229,0.92)",
                            cursor: canManagePaidBilling ? "pointer" : "default",
                            opacity: canManagePaidBilling ? 1 : 0.82,
                            boxShadow: isSelectedPlan
                              ? accentPrimaryShadow
                              : isCurrentPlan
                                ? "0 14px 28px rgba(91,179,130,0.18)"
                                : billingPlanCardBaseStyle.boxShadow,
                          }}
                        >
                          <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "start" }}>
                            <div style={{ display: "grid", gap: 4 }}>
                              <strong style={{ fontSize: 22, color: "#20324a" }}>{PLAN_SUMMARIES[plan].title}</strong>
                              <span style={{ fontSize: 13, color: "#5f6f86" }}>{PLAN_SUMMARIES[plan].serviceLimit}</span>
                            </div>
                            {isCurrentPlan ? (
                              <span style={{ ...settingsPillBaseStyle, border: "1px solid rgba(91,179,130,0.24)", background: "rgba(91,179,130,0.14)", color: "#3b7d5c" }}>
                                Current
                              </span>
                            ) : null}
                          </div>
                          <div style={{ display: "grid", gap: 2 }}>
                            <span style={{ fontSize: 30, fontWeight: 900, letterSpacing: "-0.05em", color: "#20324a" }}>{PLAN_SUMMARIES[plan].monthlyPrice}</span>
                            <span style={{ fontSize: 12, color: "#6a7a91" }}>
                              {isSelectedPlan ? "Selected for checkout" : "Click to select this plan"}
                            </span>
                          </div>
                        </button>
                      );
                    })}
                  </div>

                  {canManagePaidBilling ? (
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
                      <button
                        onClick={openUpgradeCheckout}
                        disabled={billingCheckoutBusy || !resolvedOrgId}
                        style={{
                          ...settingsButtonPrimaryStyle,
                          opacity: billingCheckoutBusy || !resolvedOrgId ? 0.6 : 1,
                          cursor: billingCheckoutBusy || !resolvedOrgId ? "not-allowed" : "pointer",
                        }}
                      >
                        {billingCheckoutBusy ? "Opening Checkout..." : `Open Checkout for ${PLAN_SUMMARIES[selectedPlan].title}`}
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
                        {billingPortalBusy ? "Opening Portal..." : "Open Stripe Billing Portal"}
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
                      Owner or admin role is required to change plans or open Stripe billing actions.
                    </p>
                  )}
                </section>

                <div style={settingsGridStyle}>
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
              </div>
            ) : (
              <div style={{ ...settingsCardStyle, marginTop: 12, fontSize: 13, color: "#5f6f86" }}>
                You do not have permission to view billing for this church.
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
            isTrialExpired ? (
              <section style={{ border: "1px solid rgba(252,165,165,0.55)", borderRadius: 14, padding: 16, background: "rgba(127,29,29,0.18)", color: "#fecaca" }}>
                Your trial has ended. Broadcasting is blocked until billing is added.
              </section>
            ) : (
            <section>
              <TranslationBox />
            </section>
            )
          ) : (
            <section
              style={{
                borderRadius: 16,
                background: "linear-gradient(160deg, rgba(240,246,255,0.7) 0%, rgba(225,234,248,0.45) 100%)",
                padding: "32px 24px",
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                gap: 12,
                textAlign: "center",
              }}
            >
              <div style={{ width: 48, height: 48, borderRadius: 14, background: "rgba(79,115,170,0.1)", display: "flex", alignItems: "center", justifyContent: "center" }}>
                <svg width="28" height="28" viewBox="0 0 28 28" fill="none" aria-hidden="true">
                  {/* broadcast signal arcs */}
                  <circle cx="14" cy="18" r="2.5" fill="#4f73aa" />
                  <path d="M9.5 14.5 a6.5 6.5 0 0 1 9 0" stroke="#4f73aa" strokeWidth="2" strokeLinecap="round" fill="none" opacity="0.7" />
                  <path d="M6 11 a11 11 0 0 1 16 0" stroke="#4f73aa" strokeWidth="2" strokeLinecap="round" fill="none" opacity="0.4" />
                </svg>
              </div>
              <p style={{ margin: 0, fontSize: 15, fontWeight: 700, color: "#2c3e5a", letterSpacing: "-0.02em" }}>
                Select a service and press <span style={{ color: "#4f73aa" }}>Start Service</span> to open the controls
              </p>
              <p style={{ margin: 0, fontSize: 13, color: "#7a8da8", lineHeight: 1.55, maxWidth: 380 }}>
                Translation, audio controls, and live monitoring will appear here once a room is active.
              </p>
            </section>
          )
        ) : null}

        <section style={supportFooterStyle}>
          <div style={{ display: "grid", gap: 4 }}>
            <p style={{ margin: 0, fontSize: 16, fontWeight: 800, letterSpacing: "0.16em", textTransform: "uppercase", color: "#7c8ba3" }}>
              Support
            </p>
            <p style={{ margin: 0, fontSize: 13, color: "#5f6f86", lineHeight: 1.6 }}>
              Need help with billing, setup, team access, or translation quality? Contact support from here.
            </p>
          </div>
          <button
            onClick={() => {
              void router.push(dashboardContactHref);
            }}
            style={supportFooterLinkStyle}
          >
            Contact Us
          </button>
        </section>
      </div>
    </main>
  );
}
