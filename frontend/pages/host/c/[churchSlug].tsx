import Link from "next/link";
import { useRouter } from "next/router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { sendEmailVerification } from "firebase/auth";
import QRCode from "qrcode";

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
import { clearAuthToken, clearHostToken, clearRoomInSession, clearStreamContext, persistAuthToken, persistHostToken, persistStreamContext } from "../../../utils/streamContext";

type ServiceRow = {
  serviceKey: string;
  title: string;
  timezone?: string;
  activeRoomId?: string | null;
  lastRoomId?: string | null;
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


const PLAN_SUMMARIES: Record<PaidPlanKey, { title: string; monthlyPrice: string; minuteLimit: string; description: string }> = {
  starter: {
    title: "Starter",
    monthlyPrice: "$20 / month",
    minuteLimit: "600 min / month (~10 hrs)",
    description: "Great for one weekly service with room to spare.",
  },
  growth: {
    title: "Growth",
    monthlyPrice: "$40 / month",
    minuteLimit: "1,800 min / month (~30 hrs)",
    description: "Fits churches with 2–3 services per week.",
  },
  premium: {
    title: "Premium",
    monthlyPrice: "$60 / month",
    minuteLimit: "Unlimited",
    description: "No limits — ideal for large or multi-campus churches.",
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

async function downloadTranslationLog(
  orgId: string,
  roomId: string,
  getToken: () => Promise<string>,
): Promise<void> {
  const token = await getToken();
  const res = await fetch(`${API_URL}/org/${orgId}/room/${roomId}/segments/export`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    let detail = "";
    try {
      const data = await res.json();
      detail = typeof data?.detail === "string" ? data.detail : "";
    } catch {}
    throw new Error(detail || `Export failed (HTTP ${res.status})`);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `translation_${roomId}.csv`;
  a.click();
  URL.revokeObjectURL(url);
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
  const controlPanelRef = useRef<HTMLDivElement>(null);
  const scrollToPanelRef = useRef(false);
  const roomStartTimeRef = useRef<number | null>(null);
  const [elapsedSec, setElapsedSec] = useState(0);
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
  const [qrDataUrl, setQrDataUrl] = useState("");
  const [copyUrlBusy, setCopyUrlBusy] = useState(false);
  const [copyUrlNotice, setCopyUrlNotice] = useState<string | null>(null);
  const [shareUrlBusy, setShareUrlBusy] = useState(false);
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
  const [selectedPlan, setSelectedPlan] = useState<PaidPlanKey | null>(null);
  const [billingBusy, setBillingBusy] = useState(false);
  const [billingCheckoutBusy, setBillingCheckoutBusy] = useState(false);
  const [billingPortalBusy, setBillingPortalBusy] = useState(false);
  const [billingRefreshBusy, setBillingRefreshBusy] = useState(false);
  const [billingError, setBillingError] = useState<string | null>(null);
  const [billingNotice, setBillingNotice] = useState<string | null>(null);
  const [trialBroadcastNotice, setTrialBroadcastNotice] = useState<string | null>(null);
  const [trialCountdownSeconds, setTrialCountdownSeconds] = useState<number | null>(null);
  const [sermonUsageState, setSermonUsageState] = useState<OrgSermonUsageResponse | null>(null);
  const [downloadingRoom, setDownloadingRoom] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
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
  const billingMonthlyMinutesLimit = billingProfile?.monthlyMinutesLimit ?? null;
  const billingMonthlyMinutesUsed = billingProfile?.monthlyMinutesUsed ?? null;
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
    if (selectedPlan !== null && selectablePaidPlans.includes(selectedPlan)) return;
    // Only auto-select a plan for users without an active paid subscription
    if (!hasPaidPlan || !hasActiveLikeSubscription) {
      setSelectedPlan((selectablePaidPlans[0] as PaidPlanKey) || "starter");
    } else if (selectedPlan !== null && !selectablePaidPlans.includes(selectedPlan)) {
      setSelectedPlan(null);
    }
  }, [selectablePaidPlans, selectedPlan, hasPaidPlan, hasActiveLikeSubscription]);

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
    setSelectedPlan(null);
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
    if (activeRoomId && scrollToPanelRef.current) {
      scrollToPanelRef.current = false;
      controlPanelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [activeRoomId]);

  useEffect(() => {
    if (!activeRoomId) { setElapsedSec(0); return; }
    const t = window.setInterval(() => {
      setElapsedSec(roomStartTimeRef.current ? Math.floor((Date.now() - roomStartTimeRef.current) / 1000) : 0);
    }, 1000);
    return () => clearInterval(t);
  }, [activeRoomId]);

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

  useEffect(() => {
    if (!displayUrl) { setQrDataUrl(""); return; }
    let cancelled = false;
    QRCode.toDataURL(displayUrl, { width: 200, margin: 1, color: { dark: "#0f172a", light: "#ffffff" } }).then((url) => {
      if (!cancelled) setQrDataUrl(url);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [displayUrl]);

  const copyListenerUrl = useCallback(async () => {
    if (!displayUrl) return;
    setCopyUrlBusy(true);
    setCopyUrlNotice(null);
    try {
      await copyTextToClipboard(displayUrl);
      setCopyUrlNotice("Copied!");
      setTimeout(() => setCopyUrlNotice(null), 2500);
    } catch {
      setCopyUrlNotice("Copy failed — select manually.");
    } finally {
      setCopyUrlBusy(false);
    }
  }, [displayUrl]);

  const shareListenerUrl = useCallback(async () => {
    if (!displayUrl) return;
    if (typeof navigator !== "undefined" && navigator.share) {
      setShareUrlBusy(true);
      try {
        await navigator.share({ url: displayUrl, title: "Join live translation" });
      } catch {
        await copyListenerUrl();
      } finally {
        setShareUrlBusy(false);
      }
    } else {
      await copyListenerUrl();
    }
  }, [copyListenerUrl, displayUrl]);

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
      const loadedPlan = payload.billing?.planKey?.trim().toLowerCase();
      if (loadedPlan && PAID_PLAN_KEYS.includes(loadedPlan as PaidPlanKey)) {
        setSelectedPlan(loadedPlan as PaidPlanKey);
      }
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
        const loadedPlan = payload.billing?.planKey?.trim().toLowerCase();
        if (loadedPlan && PAID_PLAN_KEYS.includes(loadedPlan as PaidPlanKey)) {
          setSelectedPlan(loadedPlan as PaidPlanKey);
        }
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
    if (!resolvedOrgId || !canManagePaidBilling || !selectedPlan) return;
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
      scrollToPanelRef.current = true;
      roomStartTimeRef.current = Date.now();
      setElapsedSec(0);
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
    const roomToEnd = activeRoomId || queryRoomId;
    if (!resolvedOrgId || !roomToEnd) return;
    busyRef.current = true;
    setBusy(true);
    try {
      const idToken = await getIdToken();
      if (!idToken) throw new Error("Please sign in again.");
      persistAuthToken(idToken);
      const res = await fetch(`${API_URL}/api/org/${encodeURIComponent(resolvedOrgId)}/room/${encodeURIComponent(roomToEnd)}/end`, {
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

  // ─── design tokens ────────────────────────────────────────────────────────
  const DC = {
    cream:    "#f7f4ef",
    navy:     "#0f1f3d",
    gold:     "#b89a5e",
    goldLight:"#d4b87a",
    charcoal: "#1c1c1c",
    mid:      "#5a5a52",
    border:   "#e4ddd2",
    white:    "#ffffff",
    danger:   "#9f3650",
    dangerBg: "#fdf2f4",
    success:  "#2d6a4f",
    successBg:"#f0faf5",
    warn:     "#92400e",
    warnBg:   "#fffbeb",
  };

  const hostPageStyle = {
    minHeight: "100vh",
    background: "radial-gradient(circle at 18% 15%, rgba(198,192,245,0.48), transparent 18%), radial-gradient(circle at 78% 18%, rgba(255,255,255,0.75), transparent 12%), radial-gradient(circle at 75% 70%, rgba(232,214,219,0.34), transparent 18%), linear-gradient(180deg, #f5efe7 0%, #eee6da 100%)",
    color: "#2e2a28",
  } as const;
  const dashboardCardShadow = "0 1px 3px rgba(0,0,0,0.06)";
  const dashboardCompactShadow = "0 1px 2px rgba(0,0,0,0.04)";
  const hostFieldStyle = {
    border: `1px solid ${DC.border}`,
    background: DC.white,
    color: DC.charcoal,
    padding: "9px 12px",
    borderRadius: 3,
    fontSize: 14,
    outline: "none",
  } as const;
  const hostTabRailStyle = {
    display: "flex",
    flexWrap: "wrap" as const,
    gap: 0,
    margin: "20px 0 0",
    padding: "0",
    background: "transparent",
    borderBottom: "1px solid rgba(255,255,255,0.10)",
  } as const;
  const studioUserPanelStyle = {
    display: "flex",
    alignItems: "center",
    gap: 12,
    padding: 0,
  } as const;
  const studioAdminButtonStyle = {
    border: `1px solid rgba(212,184,122,0.35)`,
    background: "rgba(212,184,122,0.1)",
    color: DC.goldLight,
    fontSize: 11,
    fontWeight: 600,
    letterSpacing: "0.12em",
    textTransform: "uppercase" as const,
    padding: "8px 14px",
    cursor: "pointer",
    textDecoration: "none",
    borderRadius: 3,
  } as const;
  const studioLogoutButtonStyle = {
    border: "1px solid rgba(255,255,255,0.14)",
    background: "transparent",
    color: "rgba(255,255,255,0.55)",
    fontSize: 11,
    fontWeight: 500,
    letterSpacing: "0.1em",
    textTransform: "uppercase" as const,
    padding: "8px 14px",
    cursor: "pointer",
    borderRadius: 3,
  } as const;
  const studioBrandTileStyle = {
    width: 48,
    height: 48,
    borderRadius: 10,
    background: "rgba(255,255,255,0.06)",
    border: "1px solid rgba(255,255,255,0.1)",
    display: "grid",
    placeItems: "center",
    color: DC.goldLight,
    fontSize: 24,
    fontWeight: 800,
    fontStyle: "italic",
    flexShrink: 0,
  } as const;
  const accentPrimaryShadow = "0 4px 12px rgba(184,154,94,0.22)";
  const accentDangerGradient = "#c0392b";
  const accentDangerShadow = "0 4px 12px rgba(192,57,43,0.2)";
  const settingsShellStyle = {
    padding: "28px 0 48px",
    display: "grid",
    gap: 24,
  } as const;
  const settingsGridStyle = {
    display: "grid",
    gap: 24,
    gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 320px), 1fr))",
  } as const;
  const settingsCardStyle = {
    border: `1px solid ${DC.border}`,
    background: DC.white,
    padding: 24,
    display: "grid",
    gap: 16,
    borderRadius: 8,
    boxShadow: dashboardCardShadow,
  } as const;
  const settingsSubscriptionCardStyle = {
    ...settingsCardStyle,
    borderTop: `3px solid ${DC.gold}`,
  } as const;
  const settingsBudgetCardStyle = {
    ...settingsCardStyle,
    borderTop: `3px solid rgba(184,154,94,0.5)`,
  } as const;
  const billingHeroCardStyle = {
    ...settingsCardStyle,
    padding: 28,
    background: DC.navy,
    border: "none",
    color: DC.cream,
  } as const;
  const billingDeckStyle = {
    display: "grid",
    gap: 16,
    gridTemplateColumns: "repeat(auto-fit, minmax(min(100%), 1fr))",
  } as const;
  const billingPlanCardBaseStyle = {
    padding: 20,
    display: "grid",
    gap: 10,
    minHeight: 200,
    background: DC.white,
    border: `1px solid ${DC.border}`,
    borderRadius: 8,
    boxShadow: dashboardCompactShadow,
  } as const;
  const billingAlertStyle = {
    border: "1px solid rgba(192,57,43,0.22)",
    background: DC.dangerBg,
    color: DC.danger,
    padding: "12px 16px",
    fontSize: 13,
    fontWeight: 600,
    lineHeight: 1.5,
    borderRadius: 6,
  } as const;
  const settingsSectionLabelStyle = {
    margin: 0,
    fontSize: 10,
    fontWeight: 600,
    letterSpacing: "0.2em",
    textTransform: "uppercase" as const,
    color: DC.gold,
  } as const;
  const settingsTitleStyle = {
    margin: 0,
    fontSize: 18,
    fontWeight: 700,
    color: DC.navy,
    letterSpacing: "-0.02em",
  } as const;
  const settingsBodyTextStyle = {
    margin: 0,
    fontSize: 13,
    color: DC.mid,
    lineHeight: 1.7,
  } as const;
  const supportFooterStyle = {
    padding: "20px 0",
    borderTop: "1px solid rgba(84,72,61,0.09)",
    display: "flex",
    alignItems: "center",
    gap: 16,
  } as const;
  const supportFooterLinkStyle = {
    border: "none",
    background: "transparent",
    padding: 0,
    color: DC.gold,
    fontSize: 13,
    fontWeight: 700,
    cursor: "pointer",
    textDecoration: "underline",
    textUnderlineOffset: "3px",
    whiteSpace: "nowrap" as const,
  } as const;
  const settingsPillBaseStyle = {
    display: "inline-flex",
    alignItems: "center",
    padding: "4px 10px",
    fontSize: 11,
    fontWeight: 600,
  } as const;
  const settingsButtonPrimaryStyle = {
    border: "none",
    background: DC.gold,
    color: DC.white,
    fontWeight: 700,
    fontSize: 13,
    padding: "10px 18px",
    cursor: "pointer",
    borderRadius: 3,
    letterSpacing: "0.04em",
    boxShadow: accentPrimaryShadow,
  } as const;
  const settingsButtonNeutralStyle = {
    border: `1px solid ${DC.border}`,
    background: DC.white,
    color: DC.charcoal,
    fontWeight: 600,
    fontSize: 13,
    padding: "10px 18px",
    cursor: "pointer",
    borderRadius: 3,
    boxShadow: dashboardCompactShadow,
  } as const;
  const settingsButtonDangerStyle = {
    border: "none",
    background: accentDangerGradient,
    color: DC.white,
    fontWeight: 700,
    fontSize: 13,
    padding: "10px 18px",
    cursor: "pointer",
    borderRadius: 3,
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
    border: `1px solid ${DC.border}`,
    padding: "16px 20px",
    background: DC.cream,
    borderRadius: 6,
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
      <main style={{ ...hostPageStyle, display: "grid", placeItems: "center", minHeight: "100vh" }}>
        <div
          style={{
            width: "100%",
            maxWidth: 480,
            border: `1px solid ${DC.border}`,
            background: DC.white,
            boxShadow: "0 8px 32px rgba(15,31,61,0.08)",
            padding: "36px 32px",
            display: "grid",
            gap: 20,
            color: DC.charcoal,
          }}
        >
          <div style={{ display: "grid", gap: 6 }}>
            <p style={{ margin: 0, fontSize: 11, fontWeight: 700, letterSpacing: "0.18em", textTransform: "uppercase", color: DC.gold }}>
              Account Setup
            </p>
            <h2 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: DC.navy, lineHeight: 1.2 }}>
              Verify your email address
            </h2>
          </div>

          <div
            style={{
              border: `1px solid rgba(184,154,94,0.25)`,
              background: "rgba(184,154,94,0.06)",
              padding: "14px 16px",
              display: "grid",
              gap: 4,
            }}
          >
            <p style={{ margin: 0, fontSize: 12, fontWeight: 700, color: DC.gold, letterSpacing: "0.06em", textTransform: "uppercase" }}>
              Verification email sent to
            </p>
            <p style={{ margin: 0, fontSize: 15, fontWeight: 700, color: DC.navy }}>{user.email}</p>
          </div>

          <p style={{ margin: 0, fontSize: 14, lineHeight: 1.7, color: DC.mid }}>
            Check your inbox and click the <strong style={{ color: DC.charcoal }}>Verify email address</strong> link.
            Once verified, return here and click <strong style={{ color: DC.charcoal }}>I&apos;ve Verified</strong> below.
          </p>

          {verificationError ? (
            <p style={{ margin: 0, fontSize: 13, lineHeight: 1.6, padding: "10px 14px", background: DC.dangerBg, color: DC.danger }}>
              {verificationError}
            </p>
          ) : null}

          <div style={{ display: "grid", gap: 10 }}>
            <button
              onClick={handleCheckVerification}
              disabled={verificationSending}
              style={{
                padding: "12px 18px",
                border: "none",
                background: DC.navy,
                color: DC.cream,
                fontSize: 14,
                fontWeight: 700,
                borderRadius: 3,
                cursor: verificationSending ? "not-allowed" : "pointer",
                opacity: verificationSending ? 0.6 : 1,
                boxShadow: "0 4px 16px rgba(15,31,61,0.18)",
              }}
            >
              {verificationSending ? "Checking..." : "I've Verified — Open Dashboard"}
            </button>
            <button
              onClick={handleResendVerification}
              disabled={verificationSending}
              style={{
                padding: "11px 18px",
                border: `1px solid ${DC.border}`,
                background: DC.white,
                color: DC.mid,
                fontSize: 13,
                fontWeight: 600,
                borderRadius: 3,
                cursor: verificationSending ? "not-allowed" : "pointer",
                opacity: verificationSending ? 0.6 : 1,
              }}
            >
              {verificationSending ? "Sending..." : "Resend Verification Email"}
            </button>
          </div>

          <p style={{ margin: 0, fontSize: 12, color: DC.mid, lineHeight: 1.6 }}>
            Can&apos;t find the email? Check your spam folder. The link expires after 24 hours.
          </p>
        </div>
      </main>
    );
  }

  return (
    <main style={hostPageStyle}>
      <div style={{ maxWidth: 1500, margin: "0 auto", padding: "0 24px 64px" }}>
        {/* ── Glass header (logo + nav + user — all one line) ── */}
        <header style={{
          position: "relative",
          overflow: "hidden",
          background: "linear-gradient(180deg, rgba(255,255,255,0.42), rgba(255,255,255,0.26))",
          border: "1px solid rgba(255,255,255,0.52)",
          boxShadow: "0 22px 55px rgba(122,101,79,0.10), inset 0 1px 0 rgba(255,255,255,0.75)",
          backdropFilter: "blur(22px)",
          WebkitBackdropFilter: "blur(22px)",
          borderRadius: 34,
          padding: "12px 24px",
          marginTop: 16,
        }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16 }}>
            {/* Left: church logo + name */}
            <div style={{ display: "flex", alignItems: "center", gap: 20, flexShrink: 0 }}>
              <div style={{ width: 48, height: 48, borderRadius: 16, background: "#2d3650", color: "#f1d35c", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 20, fontWeight: 600, flexShrink: 0 }}>
                {currentChurchInitial}
              </div>
              <div>
                <div style={{ fontSize: 20, fontWeight: 600, letterSpacing: "-0.02em", color: "#151515", lineHeight: 1.1 }}>{currentChurchLabel}</div>
                <div style={{ marginTop: 2, fontSize: 10, letterSpacing: "0.38em", textTransform: "uppercase" as const, color: "#6f655c" }}>Translation Studio</div>
              </div>
            </div>
            {/* Center: nav pills */}
            <nav style={{ display: "flex", alignItems: "center", borderRadius: 999, background: "rgba(255,255,255,0.35)", padding: "8px", boxShadow: "0 18px 40px rgba(110,93,74,0.10)", flexShrink: 0 }}>
              {(["broadcast", "settings", "billing", "team"] as const).map((tab) => {
                const labels: Record<string, string> = { broadcast: "Live Broadcast", settings: "Church Settings", billing: "Billing & Subscription", team: "Team" };
                const isActive = activeTab === tab;
                const isBillingAlert = tab === "billing" && billingNeedsAttention && !isActive;
                return (
                  <button key={tab} onClick={() => navigateToTab(tab)} style={{ border: "none", background: isActive ? "#ffffff" : "transparent", color: isActive ? "#27211d" : isBillingAlert ? "#c0392b" : "#5f5852", fontSize: 13, fontWeight: isActive ? 600 : 500, padding: "9px 18px", borderRadius: 999, cursor: "pointer", boxShadow: isActive ? "0 2px 8px rgba(0,0,0,0.08)" : "none", whiteSpace: "nowrap" as const, letterSpacing: "-0.01em" }}>
                    {labels[tab]}
                  </button>
                );
              })}
            </nav>
            {/* Right: user + logout */}
            <div style={{ display: "flex", alignItems: "center", gap: 16, flexShrink: 0 }}>
              <div style={{ textAlign: "right" as const }}>
                <div style={{ fontSize: 15, fontWeight: 500, color: "#2f2b28" }}>{currentUserName}</div>
                {isMasterUser && <Link href="/admin" style={{ fontSize: 12, color: DC.gold, textDecoration: "none", fontWeight: 600 }}>Admin</Link>}
              </div>
              <button
                onClick={async () => { clearStreamContext(); clearHostToken(); clearAuthToken(); await logout(); }}
                style={{ border: "none", background: "rgba(41,35,33,0.90)", color: "#ffffff", fontWeight: 600, fontSize: 13, padding: "9px 20px", borderRadius: 999, cursor: "pointer", whiteSpace: "nowrap" as const }}
              >
                Logout
              </button>
            </div>
          </div>
        </header>

        {/* ── Page content ── */}
        <div style={{ marginTop: 20 }}>
          {!backendReachable ? (
            <div style={{ marginBottom: 10, padding: "10px 14px", borderRadius: 12, background: "rgba(251,243,219,0.85)", border: "1px solid rgba(198,165,109,0.40)", color: "#7a5c20", fontSize: 13, fontWeight: 600, display: "flex", alignItems: "center", gap: 8, backdropFilter: "blur(12px)" }}>
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: "#c6a56d", flexShrink: 0, display: "inline-block" }} />
              <span style={{ flex: 1 }}>Server unreachable — reconnecting…</span>
              <button type="button" onClick={() => { void refreshServices().then(() => setBackendReachable(true)).catch(() => {}); }} style={{ background: "rgba(198,165,109,0.18)", border: "1px solid rgba(198,165,109,0.45)", color: "#7a5c20", borderRadius: 6, padding: "3px 10px", fontSize: 12, fontWeight: 700, cursor: "pointer" }}>Retry now</button>
            </div>
          ) : null}
          {errorMsg && activeTab !== "broadcast" ? <p style={{ color: "#9f3650", marginTop: 0, fontSize: 13, fontWeight: 600 }}>Error: {errorMsg}</p> : null}
          {memberships.length > 1 && activeTab !== "broadcast" ? (
            <div style={{ marginBottom: 12, display: "grid", gap: 4, maxWidth: 380 }}>
              <span style={{ fontSize: 11, fontWeight: 600, letterSpacing: "0.12em", textTransform: "uppercase", color: "#7d746c" }}>Current Church</span>
              <select value={selectedOrgId || resolvedOrgId} onChange={(e) => { void switchOrganization(e.target.value); }} disabled={switchingOrg || busy} style={{ border: "1px solid rgba(120,98,78,0.15)", background: "rgba(255,255,255,0.6)", color: "#2e2a28", padding: "9px 12px", borderRadius: 10, fontSize: 14, outline: "none" }}>
                {memberships.map((row) => (<option key={row.orgId} value={row.orgId}>{row.name} ({row.role || "member"})</option>))}
              </select>
              {switchingOrg && <span style={{ fontSize: 12, color: "#7d746c" }}>Switching church...</span>}
            </div>
          ) : null}
          {activeTab === "broadcast" ? (
            <>
              {/* ── Control bar ── */}
              <section style={{ marginTop: 20, background: "linear-gradient(180deg, rgba(255,255,255,0.42), rgba(255,255,255,0.26))", border: "1px solid rgba(255,255,255,0.52)", boxShadow: "0 22px 55px rgba(122,101,79,0.10), inset 0 1px 0 rgba(255,255,255,0.75)", backdropFilter: "blur(22px)", WebkitBackdropFilter: "blur(22px)", borderRadius: 30, padding: "20px 24px" }}>
                {!activeRoomId && (
                  <div style={{ marginBottom: 18 }}>
                    <h1 style={{ margin: "0 0 12px", fontSize: "clamp(22px, 2vw, 28px)", fontWeight: 600, letterSpacing: "-0.02em", color: "#1d1917" }}>Launch Your Broadcast</h1>
                    <div style={{ display: "flex", flexWrap: "wrap" as const, alignItems: "center", gap: 6 }}>
                      {(["Select a service", "Open the Control Panel", "Start Translation"] as const).map((label, i) => (
                        <div key={label} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <span style={{ width: 22, height: 22, borderRadius: "50%", background: "#c6a56d", color: "#ffffff", fontSize: 11, fontWeight: 700, display: "inline-flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>{i + 1}</span>
                          <span style={{ fontSize: 13, color: "#7d746c" }}>{label}</span>
                          {i < 2 && <span style={{ color: "#c4b8ac", fontSize: 13, marginLeft: 2 }}>›</span>}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" as const }}>
                  {/* Service label + selector inline */}
                  <span style={{ fontSize: 13, fontWeight: 600, color: "#746c64", flexShrink: 0 }}>Service</span>
                  {orgData?.services?.length ? (
                    <select value={serviceKey} onChange={(e) => { const k = e.target.value; setServiceKey(k); setActiveRoomId(null); persistStreamContext({ orgId: orgData?.orgId, serviceKey: k, churchSlug: slug }); }} disabled={loading || !!activeRoomId} style={{ flex: 1, minWidth: 200, border: "1px solid rgba(120,98,78,0.12)", background: "rgba(255,255,255,0.60)", color: "#26211f", padding: "10px 14px", borderRadius: 16, fontSize: 14, fontWeight: 500, outline: "none" }}>
                      {orgData.services.map((row) => (<option key={row.serviceKey} value={row.serviceKey}>{row.title} ({row.serviceKey})</option>))}
                    </select>
                  ) : (
                    <input value={serviceKey} onChange={(e) => { const k = e.target.value; setServiceKey(k); setActiveRoomId(null); persistStreamContext({ orgId: orgData?.orgId, serviceKey: k, churchSlug: slug }); }} placeholder={DEFAULT_SERVICE_KEY} style={{ flex: 1, minWidth: 200, border: "1px solid rgba(120,98,78,0.12)", background: "rgba(255,255,255,0.60)", color: "#26211f", padding: "10px 14px", borderRadius: 16, fontSize: 14, outline: "none" }} />
                  )}
                  {!loading && orgData && !orgData.services?.length && <div style={{ fontSize: 12, color: "#7d746c" }}>No services found. Create one in Church Settings.</div>}
                  {/* Action buttons */}
                  <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" as const }}>
                    {activeRoomId ? (
                      <>
                        <button onClick={startService} disabled={startServiceDisabled} style={{ borderRadius: 22, background: "#efe7d6", border: "none", padding: "16px 20px", fontSize: 13, fontWeight: 600, color: "#51463b", cursor: startServiceDisabled ? "not-allowed" : "pointer", opacity: startServiceDisabled ? 0.6 : 1, boxShadow: "0 18px 40px rgba(110,93,74,0.10)", whiteSpace: "nowrap" as const }}>Restart / Rejoin Room</button>
                        <button onClick={endService} disabled={busy} style={{ borderRadius: 22, background: "#d96f67", border: "none", padding: "16px 20px", fontSize: 13, fontWeight: 600, color: "#ffffff", cursor: busy ? "not-allowed" : "pointer", opacity: busy ? 0.6 : 1, boxShadow: "0 18px 40px rgba(110,93,74,0.10)", whiteSpace: "nowrap" as const }}>End Service</button>
                        <div style={{ borderRadius: 22, background: "rgba(255,255,255,0.26)", boxShadow: "0 18px 40px rgba(110,93,74,0.10)", border: "1px solid rgba(120,98,78,0.05)", padding: "16px 20px", fontSize: 13, color: "#726961", whiteSpace: "nowrap" as const }}>Live room: {activeRoomId}</div>
                      </>
                    ) : (
                      <button onClick={startService} disabled={startServiceDisabled} style={{ borderRadius: 22, background: "#c5a263", border: "none", padding: "16px 24px", fontSize: 13, fontWeight: 600, color: "#ffffff", cursor: startServiceDisabled ? "not-allowed" : "pointer", opacity: startServiceDisabled ? 0.6 : 1, boxShadow: "0 18px 40px rgba(110,93,74,0.10)", whiteSpace: "nowrap" as const, letterSpacing: "0.04em" }}>Open Broadcast Console</button>
                    )}
                  </div>
                </div>
                {trialBroadcastNotice && <div style={{ marginTop: 12, borderRadius: 10, border: isTrialExpired ? "1px solid rgba(217,111,103,0.4)" : "1px solid rgba(198,165,109,0.40)", background: isTrialExpired ? "rgba(240,220,218,0.85)" : "rgba(251,243,219,0.85)", color: isTrialExpired ? "#8a2720" : "#7a5c20", fontSize: 13, fontWeight: 600, padding: "10px 14px" }}>{trialBroadcastNotice}</div>}
                {errorMsg && <p style={{ margin: "10px 0 0", color: "#9f3650", fontSize: 13, fontWeight: 600 }}>Error: {errorMsg}</p>}
                {memberships.length > 1 && (
                  <div style={{ marginTop: 14, display: "flex", alignItems: "center", gap: 10 }}>
                    <span style={{ fontSize: 12, color: "#7d746c" }}>Church:</span>
                    <select value={selectedOrgId || resolvedOrgId} onChange={(e) => { void switchOrganization(e.target.value); }} disabled={switchingOrg || busy} style={{ border: "1px solid rgba(120,98,78,0.12)", background: "rgba(255,255,255,0.6)", color: "#2e2a28", padding: "6px 10px", borderRadius: 8, fontSize: 13, outline: "none" }}>
                      {memberships.map((row) => (<option key={row.orgId} value={row.orgId}>{row.name} ({row.role || "member"})</option>))}
                    </select>
                    {switchingOrg && <span style={{ fontSize: 12, color: "#7d746c" }}>Switching…</span>}
                  </div>
                )}
                <div style={{ marginTop: 12, fontSize: 12, color: "#7d746c" }}>Signed-in hosts are authorized by account role. Manual host token entry is not required.</div>
              </section>
              {/* ── Live broadcast section (only when active) ── */}
              {activeRoomId ? (
                isTrialExpired ? (
                  <section style={{ marginTop: 16, borderRadius: 16, border: "1px solid rgba(217,111,103,0.40)", padding: 20, background: "rgba(240,220,218,0.85)", color: "#8a2720", fontSize: 13, fontWeight: 600 }}>
                    Your trial has ended. Broadcasting is blocked until billing is added.
                  </section>
                ) : (
                  <section ref={controlPanelRef} style={{ marginTop: 20, position: "relative", overflow: "hidden", background: "linear-gradient(180deg, rgba(255,255,255,0.42), rgba(255,255,255,0.26))", border: "1px solid rgba(255,255,255,0.52)", boxShadow: "0 22px 55px rgba(122,101,79,0.10), inset 0 1px 0 rgba(255,255,255,0.75)", backdropFilter: "blur(22px)", WebkitBackdropFilter: "blur(22px)", borderRadius: 34, padding: "24px 28px" }}>
                    {/* Halo decorations */}
                    <div aria-hidden="true" style={{ position: "absolute", left: "18%", top: "18%", width: 320, height: 220, borderRadius: "50%", background: "radial-gradient(circle, rgba(169,160,245,0.40) 0%, rgba(169,160,245,0.10) 50%, transparent 72%)", filter: "blur(28px)", pointerEvents: "none" }} />
                    <div aria-hidden="true" style={{ position: "absolute", right: "5%", bottom: "14%", width: 240, height: 180, borderRadius: "50%", background: "radial-gradient(circle, rgba(255,255,255,0.75) 0%, rgba(255,255,255,0.15) 54%, transparent 72%)", filter: "blur(24px)", pointerEvents: "none" }} />
                    <div style={{ position: "relative" }}>
                      {/* Status pills row */}
                      <div style={{ display: "flex", flexWrap: "wrap" as const, alignItems: "center", gap: 8, marginBottom: 16 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 6, borderRadius: 999, background: "rgba(255,255,255,0.44)", padding: "8px 14px", fontSize: 13, fontWeight: 500, color: "#30a173", boxShadow: "0 14px 30px rgba(126,104,81,0.08)" }}>
                          <span style={{ width: 7, height: 7, borderRadius: "50%", background: "#c6a56d", display: "block" }} />
                          Live · Connected
                        </div>
                        <div style={{ borderRadius: 999, background: "rgba(255,255,255,0.38)", padding: "8px 14px", fontSize: 13, fontWeight: 500, color: "#7e746c", boxShadow: "0 14px 30px rgba(126,104,81,0.08)" }}>{formatCountdownSeconds(elapsedSec)} elapsed</div>
                        {displayUrl && (
                          <>
                            <button type="button" onClick={() => { void copyListenerUrl(); }} disabled={copyUrlBusy} style={{ borderRadius: 999, background: "rgba(255,255,255,0.38)", border: "none", padding: "8px 14px", fontSize: 13, fontWeight: 500, color: "#7e746c", cursor: copyUrlBusy ? "default" : "pointer", boxShadow: "0 14px 30px rgba(126,104,81,0.08)" }}>{copyUrlBusy ? "Copying…" : copyUrlNotice || "Copy URL"}</button>
                            <a href={displayUrl} target="_blank" rel="noreferrer" style={{ display: "inline-flex", alignItems: "center", borderRadius: 999, background: "rgba(255,255,255,0.38)", padding: "8px 14px", fontSize: 13, fontWeight: 500, color: "#7e746c", textDecoration: "none", boxShadow: "0 14px 30px rgba(126,104,81,0.08)" }}>Listener Page ↗</a>
                          </>
                        )}
                      </div>
                      {/* TranslationBox */}
                      <div style={{ borderRadius: 24, overflow: "hidden" }}>
                        <TranslationBox />
                      </div>
                      {/* QR code */}
                      {qrDataUrl && displayUrl && (
                        <div style={{ marginTop: 16, display: "flex", alignItems: "center", gap: 12 }}>
                          {/* eslint-disable-next-line @next/next/no-img-element */}
                          <img src={qrDataUrl} alt="Listener QR code" width={64} height={64} style={{ borderRadius: 8 }} />
                          <div>
                            <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: "0.22em", textTransform: "uppercase" as const, color: "#b08b4f", marginBottom: 4 }}>Listener QR</div>
                            <div style={{ fontSize: 12, color: "#7d746c", wordBreak: "break-all" as const }}>{displayUrl}</div>
                          </div>
                        </div>
                      )}
                    </div>
                  </section>
                )
              ) : null}
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
                      <span style={{ fontSize: 12, color: DC.mid }}>Display name</span>
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
                      <span style={{ fontSize: 12, color: DC.mid }}>Church name</span>
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
                      <span style={{ fontSize: 12, color: DC.mid }}>Church URL slug</span>
                      <input
                        readOnly
                        value={slug}
                        style={{
                          ...settingsInlineFieldStyle,
                          width: "100%",
                          background: "rgba(239,244,250,0.92)",
                          color: DC.mid,
                        }}
                      />
                    </label>
                    {churchPublicPath ? (
                      <p style={{ margin: 0, fontSize: 12, color: "#4d607a", wordBreak: "break-all" }}>
                        Public path: <strong>{churchPublicPath}</strong>
                      </p>
                    ) : null}
                    {!canManagePaidBilling ? (
                      <p style={{ margin: 0, fontSize: 13, color: DC.mid }}>
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
                      <span style={{ ...settingsPillBaseStyle, border: `1px solid rgba(184,154,94,0.3)`, background: "rgba(184,154,94,0.1)", color: DC.gold }}>
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
                                : `1px solid ${DC.border}`,
                          background: billingStatusToken === "active"
                            ? "rgba(91,179,130,0.14)"
                            : billingStatusToken === "past_due"
                              ? "rgba(224,163,86,0.16)"
                              : billingNeedsAttention
                                ? "rgba(188,95,111,0.12)"
                                : DC.white,
                          color: billingStatusToken === "active"
                            ? DC.success
                            : billingStatusToken === "past_due"
                              ? DC.warn
                              : billingNeedsAttention
                                ? DC.danger
                                : DC.mid,
                        }}
                      >
                        Status · {formatBillingStatus(billingStatusToken)}
                      </span>
                      <span style={{ ...settingsPillBaseStyle, border: `1px solid ${DC.border}`, background: DC.white, color: DC.mid }}>
                        {billingMonthlyMinutesLimit !== null
                          ? `${billingMonthlyMinutesLimit === 0 ? "Unlimited" : `${billingMonthlyMinutesLimit} min/mo`}${billingMonthlyMinutesUsed !== null ? ` · ${billingMonthlyMinutesUsed} used` : ""}`
                          : `Minutes · ${billingMaxServiceKeys > 0 ? "limited" : "unlimited"}`}
                      </span>
                    </div>

                    {billingAlertMessage ? <div style={billingAlertStyle}>{billingAlertMessage}</div> : null}

                    {billingProfile ? (
                      <div style={{ display: "grid", gap: 8 }}>
                        {hasSubscriptionPeriod ? (
                          <div style={{ border: `1px solid ${DC.border}`, background: DC.white, padding: "12px 14px" }}>
                            <p style={{ ...settingsSectionLabelStyle, fontSize: 10 }}>Subscription Period</p>
                            <p style={{ margin: "6px 0 0", fontSize: 13, color: DC.charcoal }}>
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
                      const downloadRoomId = !isLive ? (row.lastRoomId || null) : null;
                      const isDownloading = downloadingRoom === downloadRoomId;
                      return (
                        <div key={row.serviceKey} style={settingsServiceRowStyle}>
                          <div style={{ minWidth: 0 }}>
                            <p style={{ margin: 0, fontSize: 15, fontWeight: 700, color: DC.navy }}>{row.title}</p>
                            <p style={{ margin: "4px 0 0", fontSize: 13, color: DC.mid }}>{row.serviceKey}</p>
                            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
                              {isSelected ? (
                                <span style={{ ...settingsPillBaseStyle, border: `1px solid rgba(184,154,94,0.3)`, background: "rgba(184,154,94,0.1)", color: DC.gold }}>
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
                          <div style={{ display: "flex", gap: 8, alignItems: "center", flexShrink: 0 }}>
                            {downloadRoomId ? (
                              <button
                                onClick={async () => {
                                  if (!resolvedOrgId || !downloadRoomId) return;
                                  setDownloadingRoom(downloadRoomId);
                                  setDownloadError(null);
                                  try {
                                    await downloadTranslationLog(resolvedOrgId, downloadRoomId, async () => (await getIdToken()) ?? "");
                                  } catch (err) {
                                    setDownloadError(err instanceof Error ? err.message : "Download failed");
                                  } finally {
                                    setDownloadingRoom(null);
                                  }
                                }}
                                disabled={isDownloading}
                                style={{
                                  ...settingsButtonNeutralStyle,
                                  padding: "9px 12px",
                                  opacity: isDownloading ? 0.55 : 1,
                                  cursor: isDownloading ? "not-allowed" : "pointer",
                                }}
                              >
                                {isDownloading ? "Downloading..." : "Download Log"}
                              </button>
                            ) : null}
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
                        </div>
                      );
                    })}
                    {downloadError ? (
                      <p style={{ margin: "8px 0 0", fontSize: 12, color: "#bc5f6f" }}>{downloadError}</p>
                    ) : null}
                  </div>
                </section>
              </div>
            ) : (
              <div style={{ ...settingsCardStyle, marginTop: 12, fontSize: 13, color: DC.mid }}>
                You do not have permission to manage service schedules for this church.
              </div>
            )
          ) : null}
          {activeTab === "billing" ? (
            canManageServices ? (
              <div style={settingsShellStyle}>
                <section style={billingHeroCardStyle}>
                  <div style={{ display: "grid", gap: 8 }}>
                    <p style={{ ...settingsSectionLabelStyle, color: DC.goldLight }}>Billing & Subscription</p>
                    <h3 style={{ ...settingsTitleStyle, fontSize: 28, lineHeight: 1.05, color: DC.cream }}>Review plan, renewal, and subscriptions</h3>
                    <p style={{ ...settingsBodyTextStyle, maxWidth: 760, color: "rgba(247,244,239,0.7)" }}>
                      Review subscription status, renewal timing, plan options, billing limits, and Sermon Prep budget in one place.
                    </p>
                  </div>

                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                    <span style={{ ...settingsPillBaseStyle, border: `1px solid rgba(212,184,122,0.3)`, background: "rgba(212,184,122,0.1)", color: DC.goldLight }}>
                      Plan · {formatPlanLabel(billingPlanToken)}
                    </span>
                    <span
                      style={{
                        ...settingsPillBaseStyle,
                        border: billingStatusToken === "active"
                          ? "1px solid rgba(91,179,130,0.35)"
                          : billingStatusToken === "past_due"
                            ? "1px solid rgba(224,163,86,0.35)"
                            : billingNeedsAttention
                              ? "1px solid rgba(188,95,111,0.35)"
                              : "1px solid rgba(247,244,239,0.2)",
                        background: billingStatusToken === "active"
                          ? "rgba(91,179,130,0.18)"
                          : billingStatusToken === "past_due"
                            ? "rgba(224,163,86,0.18)"
                            : billingNeedsAttention
                              ? "rgba(188,95,111,0.18)"
                              : "rgba(247,244,239,0.08)",
                        color: billingStatusToken === "active"
                          ? "#7dd4a8"
                          : billingStatusToken === "past_due"
                            ? "#f0c070"
                            : billingNeedsAttention
                              ? "#f09090"
                              : "rgba(247,244,239,0.7)",
                      }}
                    >
                      Status · {formatBillingStatus(billingStatusToken)}
                    </span>
                    <span style={{ ...settingsPillBaseStyle, border: "1px solid rgba(247,244,239,0.2)", background: "rgba(247,244,239,0.08)", color: "rgba(247,244,239,0.7)" }}>
                      Service keys · {billingMaxServiceKeys > 0 ? `up to ${billingMaxServiceKeys}` : "unlimited"}
                    </span>
                  </div>

                  {billingAlertMessage ? <div style={billingAlertStyle}>{billingAlertMessage}</div> : null}
                  {billingError ? <p style={{ margin: 0, color: "#f09090", fontSize: 13 }}>Error: {billingError}</p> : null}
                  {billingNotice ? <p style={{ margin: 0, color: "#7dd4a8", fontSize: 13 }}>{billingNotice}</p> : null}

                  {billingProfile ? (
                    <div style={settingsGridStyle}>
                      <div style={{ border: "1px solid rgba(247,244,239,0.15)", background: "rgba(247,244,239,0.06)", padding: "14px 16px" }}>
                        <p style={{ ...settingsSectionLabelStyle, fontSize: 10, color: DC.goldLight }}>Current Term</p>
                        <p style={{ margin: "8px 0 0", fontSize: 14, color: "rgba(247,244,239,0.85)", lineHeight: 1.6 }}>
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
                      <div style={{ border: "1px solid rgba(247,244,239,0.15)", background: "rgba(247,244,239,0.06)", padding: "14px 16px" }}>
                        <p style={{ ...settingsSectionLabelStyle, fontSize: 10, color: DC.goldLight }}>Usage Snapshot</p>
                        <p style={{ margin: "8px 0 0", fontSize: 14, color: "rgba(247,244,239,0.85)", lineHeight: 1.6 }}>
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
                      Plans are billed monthly and measured in broadcast minutes — the time your congregation is actively receiving translation. Upgrades take effect immediately; downgrades apply at the end of your current billing period.
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
                            border: isCurrentPlan
                              ? `2px solid ${DC.navy}`
                              : isSelectedPlan
                                ? `2px solid ${DC.gold}`
                                : `1px solid ${DC.border}`,
                            cursor: canManagePaidBilling ? "pointer" : "default",
                            opacity: canManagePaidBilling ? 1 : 0.82,
                            background: isCurrentPlan
                              ? "rgba(15,31,61,0.04)"
                              : billingPlanCardBaseStyle.background,
                            boxShadow: isCurrentPlan
                              ? "0 4px 16px rgba(15,31,61,0.1)"
                              : isSelectedPlan
                                ? accentPrimaryShadow
                                : billingPlanCardBaseStyle.boxShadow,
                          }}
                        >
                          {isCurrentPlan && (
                            <div style={{
                              display: "flex", alignItems: "center", gap: 6,
                              background: DC.navy,
                              color: DC.cream, fontWeight: 700, fontSize: 11,
                              letterSpacing: "0.1em", textTransform: "uppercase",
                              padding: "4px 10px", borderRadius: 3,
                              marginBottom: 10, alignSelf: "flex-start",
                            }}>
                              ✓ Current Plan
                            </div>
                          )}
                          <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "start" }}>
                            <div style={{ display: "grid", gap: 4 }}>
                              <strong style={{ fontSize: 22, color: DC.navy }}>{PLAN_SUMMARIES[plan].title}</strong>
                              <span style={{ fontSize: 13, color: DC.mid, fontWeight: 600 }}>{PLAN_SUMMARIES[plan].minuteLimit}</span>
                              <span style={{ fontSize: 12, color: DC.mid }}>{PLAN_SUMMARIES[plan].description}</span>
                            </div>
                          </div>
                          <div style={{ display: "grid", gap: 2 }}>
                            <span style={{ fontSize: 30, fontWeight: 900, letterSpacing: "-0.05em", color: DC.navy }}>{PLAN_SUMMARIES[plan].monthlyPrice}</span>
                            <span style={{ fontSize: 12, color: DC.mid }}>
                              {isCurrentPlan ? "Your active plan" : isSelectedPlan ? "Selected for checkout" : "Click to select this plan"}
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
                        disabled={billingCheckoutBusy || !resolvedOrgId || !selectedPlan}
                        style={{
                          ...settingsButtonPrimaryStyle,
                          opacity: billingCheckoutBusy || !resolvedOrgId || !selectedPlan ? 0.6 : 1,
                          cursor: billingCheckoutBusy || !resolvedOrgId || !selectedPlan ? "not-allowed" : "pointer",
                        }}
                      >
                        {billingCheckoutBusy ? "Opening Checkout..." : selectedPlan ? `Open Checkout for ${PLAN_SUMMARIES[selectedPlan].title}` : "Select a plan above"}
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
                        <div style={{ border: `1px solid ${DC.border}`, background: DC.white, padding: "12px 14px", display: "grid", gap: 6 }}>
                          <p style={{ margin: 0, fontSize: 13, color: DC.mid }}>
                            Status:{" "}
                            <strong>{billingState ? (billingState.billingLimitsEnabled ? "Enabled" : "Disabled") : "Loading..."}</strong>
                            {billingState && !billingState.globalBillingLimitsEnabled ? " · Global override currently disables all billing checks" : ""}
                          </p>
                          {billingState ? (
                            <p style={{ margin: 0, fontSize: 12, color: DC.mid }}>
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
                      <div style={{ border: `1px solid ${DC.border}`, background: DC.white, padding: "12px 14px", display: "grid", gap: 6 }}>
                        <p style={{ margin: 0, fontSize: 13, color: DC.mid }}>
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
                              <p key={row.sermonId} style={{ margin: 0, fontSize: 12, color: DC.mid }}>
                                {row.sermonId}: ${row.estimatedUsd.toFixed(4)} · {row.totalTokens.toLocaleString()} tokens
                              </p>
                            ))}
                          </div>
                        ) : (
                          <p style={{ margin: 0, fontSize: 12, color: DC.mid }}>No Sermon Prep usage recorded this month yet.</p>
                        )}
                      </div>
                    ) : (
                      <p style={settingsBodyTextStyle}>Loading usage…</p>
                    )}
                    {canManageBilling ? (
                      <>
                        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "end" }}>
                          <label style={{ display: "grid", gap: 6 }}>
                            <span style={{ fontSize: 12, color: DC.mid }}>Monthly budget (USD)</span>
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
              <div style={{ ...settingsCardStyle, marginTop: 12, fontSize: 13, color: DC.mid }}>
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
                      <span style={{ fontSize: 12, color: DC.mid }}>Role</span>
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
                        border: `1px solid ${DC.border}`,
                        background: DC.white,
                        padding: "12px 14px",
                        display: "grid",
                        gap: 10,
                      }}
                    >
                      <label style={{ display: "grid", gap: 6 }}>
                        <span style={{ fontSize: 13, color: DC.mid, fontWeight: 700 }}>Invite URL</span>
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
                            <p style={{ margin: 0, fontSize: 15, fontWeight: 700, color: DC.navy }}>Role: {row.role}</p>
                            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
                              <span style={{ ...settingsPillBaseStyle, border: `1px solid rgba(184,154,94,0.3)`, background: "rgba(184,154,94,0.1)", color: DC.gold }}>
                                Expires {formatDateTime(row.expiresAt || null)}
                              </span>
                              <span style={{ ...settingsPillBaseStyle, border: `1px solid ${DC.border}`, background: DC.white, color: DC.mid }}>
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
              <div style={{ ...settingsCardStyle, marginTop: 12, fontSize: 13, color: DC.mid }}>
                You do not have permission to manage team invites for this church.
              </div>
            )
          ) : null}

        <section style={supportFooterStyle}>
          <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.18em", textTransform: "uppercase", color: DC.gold }}>
            Support
          </span>
          <button
            onClick={() => { void router.push(dashboardContactHref); }}
            style={supportFooterLinkStyle}
          >
            Contact Us ↗
          </button>
        </section>
        </div>
      </div>
    </main>
  );
}
