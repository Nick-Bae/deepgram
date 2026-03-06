import { useRouter } from "next/router";
import { useCallback, useEffect, useMemo, useState } from "react";

import TranslationBox from "../../../components/TranslationBox";
import { useAuth } from "../../../lib/authContext";
import {
  createOrgService,
  createOrgInvite,
  deleteOrgService,
  fetchAuthMe,
  listOrgInvites,
  revokeOrgInvite,
  setCurrentOrg,
  type InviteRole,
  type OrgInviteSummary,
  type OrgMembership,
} from "../../../lib/backendAuth";
import { API_URL } from "../../../utils/urls";
import { clearAuthToken, clearHostToken, clearRoomInSession, getHostTokenFromSession, persistAuthToken, persistHostToken, persistStreamContext } from "../../../utils/streamContext";

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

const POLL_MS = 8000;
const DEFAULT_SERVICE_KEY = "sun-11am";

type HostAction = "load_services" | "start_service" | "end_service";
type InviteRoleChoice = Extract<InviteRole, "admin" | "host" | "viewer">;
type HostTab = "broadcast" | "settings" | "team";

function resolveHostTab(raw: string): HostTab {
  const token = (raw || "").trim().toLowerCase();
  if (token === "settings" || token === "team") return token;
  return "broadcast";
}

const ERROR_DETAIL_MESSAGES: Record<string, string> = {
  host_auth_failed: "Host authentication failed. Enter a valid Host Token and try again.",
  auth_required: "Please sign in first.",
  invalid_id_token: "Session expired. Please sign in again.",
  hard_cap_reached: "Monthly plan limit reached for this church. Please upgrade or wait for reset.",
  concurrency_limit_reached: "Another service is already live for this plan. End it first, then start this one.",
  org_inactive: "This church account is inactive. Check subscription or billing status.",
  org_not_found: "Church organization was not found.",
  service_not_found: "This service key was not found for the church.",
  service_exists: "That service key already exists.",
  service_active: "You cannot delete a service while a room is live.",
  invalid_service_key: "Service key is invalid. Use letters, numbers, and hyphens.",
  room_not_found: "Live room was not found. Refresh and try again.",
};

function fallbackMessage(action: HostAction): string {
  if (action === "load_services") return "Failed to load services.";
  if (action === "start_service") return "Failed to start service.";
  return "Failed to end service.";
}

function mapStatusMessage(status: number, action: HostAction): string | null {
  if (status === 402) return ERROR_DETAIL_MESSAGES.hard_cap_reached;
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
  const { user, loading: authLoading, getIdToken, logout } = useAuth();
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
  const [hostToken, setHostToken] = useState("");
  const [memberships, setMemberships] = useState<OrgMembership[]>([]);
  const [switchingOrg, setSwitchingOrg] = useState(false);
  const [selectedOrgId, setSelectedOrgId] = useState("");
  const [membershipRole, setMembershipRole] = useState("");
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
  const normalizedServiceKey = serviceKey.trim();
  const activeTab = resolveHostTab(querySection);
  const resolvedOrgId = (orgData?.orgId || queryOrgId || "").trim();
  const canManageInvites = useMemo(() => {
    const lowered = membershipRole.trim().toLowerCase();
    return lowered === "owner" || lowered === "admin";
  }, [membershipRole]);
  const canManageServices = useMemo(() => {
    const lowered = membershipRole.trim().toLowerCase();
    return lowered === "owner" || lowered === "admin" || lowered === "host";
  }, [membershipRole]);

  useEffect(() => {
    if (resolvedOrgId) setSelectedOrgId(resolvedOrgId);
  }, [resolvedOrgId]);

  useEffect(() => {
    setInviteLink("");
    setInviteError(null);
    setInviteNotice(null);
    setInviteRows([]);
    setRevokingInviteId("");
    setCopyBusy(false);
    setShareBusy(false);
    setNewServiceKey("");
    setNewServiceTitle("");
    setServiceManageError(null);
    setDeletingServiceKey("");
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
      const preferredOrgId = (me.currentOrgId || "").trim();
      const rows = me.memberships || [];
      setMemberships(rows);
      setSelectedOrgId(preferredOrgId || rows[0]?.orgId || "");
      const match =
        rows.find((row) => row.orgId === resolvedOrgId || row.slug === slug) ||
        rows.find((row) => row.orgId === preferredOrgId) ||
        rows[0];
      setMembershipRole((match?.role || "").trim());
      if (match?.hostToken) {
        const normalized = persistHostToken(match.hostToken) || "";
        setHostToken((prev) => (normalized !== prev ? normalized : prev));
      }
    };
    hydrateMembershipToken().catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [authLoading, getIdToken, resolvedOrgId, slug, user]);

  useEffect(() => {
    if (typeof window !== "undefined") setOrigin(window.location.origin);
  }, []);

  useEffect(() => {
    if (queryHostToken) {
      const normalized = persistHostToken(queryHostToken) || "";
      setHostToken(normalized);
      return;
    }
    const saved = getHostTokenFromSession();
    if (saved) setHostToken(saved);
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
        if (targetMembership.hostToken) {
          const normalized = persistHostToken(targetMembership.hostToken) || "";
          setHostToken(normalized);
        }
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
      if (idToken) persistAuthToken(idToken);
      const normalizedHostToken = persistHostToken(hostToken) || "";
      if (normalizedHostToken !== hostToken) setHostToken(normalizedHostToken);
      const path = resolvedOrgId
        ? `/api/org/${encodeURIComponent(resolvedOrgId)}/service/${encodeURIComponent(normalizedServiceKey)}/start`
        : `/api/c/${encodeURIComponent(slug)}/service/${encodeURIComponent(normalizedServiceKey)}/start`;
      const res = await fetch(`${API_URL}${path}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(idToken ? { Authorization: `Bearer ${idToken}` } : {}),
          ...(normalizedHostToken ? { "x-host-token": normalizedHostToken, "x-host-api-token": normalizedHostToken } : {}),
        },
        body: JSON.stringify({
          source: sourceLang,
          target: targetLang,
          hostUid: user?.uid || undefined,
          hostToken: normalizedHostToken || undefined,
          host_token: normalizedHostToken || undefined,
          token: normalizedHostToken || undefined,
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
      if (idToken) persistAuthToken(idToken);
      const normalizedHostToken = persistHostToken(hostToken) || "";
      if (normalizedHostToken !== hostToken) setHostToken(normalizedHostToken);
      const res = await fetch(`${API_URL}/api/org/${encodeURIComponent(resolvedOrgId)}/room/${encodeURIComponent(activeRoomId)}/end`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(idToken ? { Authorization: `Bearer ${idToken}` } : {}),
          ...(normalizedHostToken ? { "x-host-token": normalizedHostToken, "x-host-api-token": normalizedHostToken } : {}),
        },
        body: JSON.stringify({
          reason: "host_end",
          hostUid: user?.uid || undefined,
          hostToken: normalizedHostToken || undefined,
          host_token: normalizedHostToken || undefined,
          token: normalizedHostToken || undefined,
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

  if (!user && !authLoading) {
    return (
      <main style={{ minHeight: "100vh", display: "grid", placeItems: "center", background: "#0b1220", color: "#f8fafc" }}>
        Redirecting to login...
      </main>
    );
  }

  return (
    <main style={{ minHeight: "100vh", background: "#0b1220", color: "#f8fafc", padding: "20px 14px 34px" }}>
      <div style={{ maxWidth: 1240, margin: "0 auto", display: "grid", gap: 18 }}>
        <section style={{ border: "1px solid rgba(255,255,255,0.12)", borderRadius: 16, background: "rgba(255,255,255,0.05)", padding: 16 }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center", marginBottom: 8 }}>
            <span style={{ fontSize: 12, opacity: 0.78 }}>{user?.email || user?.uid || "Host"}</span>
            <button
              onClick={async () => {
                clearHostToken();
                clearAuthToken();
                await logout();
              }}
              style={{
                borderRadius: 8,
                border: "1px solid rgba(255,255,255,0.2)",
                background: "transparent",
                color: "#fff",
                fontSize: 12,
                padding: "6px 10px",
                cursor: "pointer",
              }}
            >
              Logout
            </button>
          </div>
          <h1 style={{ marginTop: 0, marginBottom: 8, fontSize: 26 }}>{orgData?.name || slug || "Host"}</h1>
          <p style={{ marginTop: 0, opacity: 0.8, marginBottom: 14 }}>
            Start a room for a recurring service, then begin microphone streaming.
          </p>
          <p style={{ marginTop: 0, marginBottom: 10, opacity: 0.76, fontSize: 13 }}>
            {resolvedOrgId ? `Org: ${resolvedOrgId}` : loading ? "Loading church organization..." : "Organization not loaded yet."}
          </p>
          {errorMsg ? <p style={{ color: "#fca5a5", marginTop: 0 }}>Error: {errorMsg}</p> : null}
          {memberships.length > 1 ? (
            <div style={{ marginBottom: 12, display: "grid", gap: 4, maxWidth: 380 }}>
              <span style={{ fontSize: 12, opacity: 0.75 }}>Current Church</span>
              <select
                value={selectedOrgId || resolvedOrgId}
                onChange={(e) => {
                  void switchOrganization(e.target.value);
                }}
                disabled={switchingOrg || busy}
                style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.25)", background: "#0f172a", color: "#fff", padding: "9px 10px" }}
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
          <div
            style={{
              position: "sticky",
              top: 8,
              zIndex: 5,
              display: "flex",
              flexWrap: "wrap",
              gap: 8,
              marginBottom: 12,
              padding: 6,
              borderRadius: 12,
              border: "1px solid rgba(56,189,248,0.35)",
              background: "rgba(8,16,35,0.92)",
              backdropFilter: "blur(8px)",
            }}
          >
            <button
              onClick={() => navigateToTab("broadcast")}
              style={{
                borderRadius: 10,
                border: activeTab === "broadcast" ? "2px solid rgba(56,189,248,1)" : "1px solid rgba(255,255,255,0.2)",
                background: activeTab === "broadcast" ? "rgba(56,189,248,0.95)" : "rgba(15,23,42,0.8)",
                color: activeTab === "broadcast" ? "#082f49" : "#e2e8f0",
                fontSize: 13,
                fontWeight: 800,
                padding: "8px 12px",
                cursor: "pointer",
                boxShadow: activeTab === "broadcast" ? "inset 0 -3px 0 rgba(8,47,73,0.5)" : "none",
              }}
            >
              Live Broadcast
            </button>
            <button
              onClick={() => navigateToTab("settings")}
              style={{
                borderRadius: 10,
                border: activeTab === "settings" ? "2px solid rgba(56,189,248,1)" : "1px solid rgba(255,255,255,0.2)",
                background: activeTab === "settings" ? "rgba(56,189,248,0.95)" : "rgba(15,23,42,0.8)",
                color: activeTab === "settings" ? "#082f49" : "#e2e8f0",
                fontSize: 13,
                fontWeight: 800,
                padding: "8px 12px",
                cursor: "pointer",
                boxShadow: activeTab === "settings" ? "inset 0 -3px 0 rgba(8,47,73,0.5)" : "none",
              }}
            >
              Church Settings
            </button>
            <button
              onClick={() => navigateToTab("team")}
              style={{
                borderRadius: 10,
                border: activeTab === "team" ? "2px solid rgba(56,189,248,1)" : "1px solid rgba(255,255,255,0.2)",
                background: activeTab === "team" ? "rgba(56,189,248,0.95)" : "rgba(15,23,42,0.8)",
                color: activeTab === "team" ? "#082f49" : "#e2e8f0",
                fontSize: 13,
                fontWeight: 800,
                padding: "8px 12px",
                cursor: "pointer",
                boxShadow: activeTab === "team" ? "inset 0 -3px 0 rgba(8,47,73,0.5)" : "none",
              }}
            >
              Team
            </button>
          </div>
          {activeTab === "broadcast" ? (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(180px,1fr))", gap: 10 }}>
                <label style={{ display: "grid", gap: 4 }}>
                  <span style={{ fontSize: 12, opacity: 0.75 }}>Service</span>
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
                      style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.25)", background: "#0f172a", color: "#fff", padding: "9px 10px" }}
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
                      style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.25)", background: "#0f172a", color: "#fff", padding: "9px 10px" }}
                    />
                  )}
                  {!loading && orgData && !orgData.services?.length ? (
                    <span style={{ fontSize: 12, opacity: 0.75 }}>No predefined services found. Enter a service key to create/start one.</span>
                  ) : null}
                </label>
                <label style={{ display: "grid", gap: 4 }}>
                  <span style={{ fontSize: 12, opacity: 0.75 }}>Source</span>
                  <input value={sourceLang} onChange={(e) => setSourceLang(e.target.value)} style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.25)", background: "#0f172a", color: "#fff", padding: "9px 10px" }} />
                </label>
                <label style={{ display: "grid", gap: 4 }}>
                  <span style={{ fontSize: 12, opacity: 0.75 }}>Target</span>
                  <input value={targetLang} onChange={(e) => setTargetLang(e.target.value)} style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.25)", background: "#0f172a", color: "#fff", padding: "9px 10px" }} />
                </label>
                <label style={{ display: "grid", gap: 4 }}>
                  <span style={{ fontSize: 12, opacity: 0.75 }}>Host Token</span>
                  <input
                    value={hostToken}
                    onChange={(e) => {
                      const next = e.target.value;
                      setHostToken(next);
                      persistHostToken(next);
                    }}
                    placeholder="Required if org hostToken is set"
                    style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.25)", background: "#0f172a", color: "#fff", padding: "9px 10px" }}
                  />
                </label>
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 12, alignItems: "center" }}>
                <button
                  onClick={startService}
                  disabled={busy}
                  style={{
                    borderRadius: 10,
                    border: "none",
                    background: "#22c55e",
                    color: "#052e16",
                    fontWeight: 700,
                    padding: "9px 14px",
                    cursor: busy ? "not-allowed" : "pointer",
                    opacity: busy ? 0.6 : 1,
                  }}
                >
                  {activeRoomId ? "Restart / Rejoin Room" : "Start Service"}
                </button>
                <button
                  onClick={endService}
                  disabled={busy || !orgData?.orgId || !activeRoomId}
                  style={{
                    borderRadius: 10,
                    border: "none",
                    background: "#f43f5e",
                    color: "#fff",
                    fontWeight: 700,
                    padding: "9px 14px",
                    cursor: busy || !orgData?.orgId || !activeRoomId ? "not-allowed" : "pointer",
                    opacity: busy || !orgData?.orgId || !activeRoomId ? 0.6 : 1,
                  }}
                >
                  End Service
                </button>
                <span style={{ opacity: 0.84, fontSize: 14 }}>
                  {activeRoomId ? `Live room: ${activeRoomId}` : "No live room"}
                </span>
              </div>
              {listenerUrl ? (
                <p style={{ marginTop: 10, marginBottom: 0, fontSize: 13, opacity: 0.84 }}>
                  Listener URL: <a href={listenerUrl} target="_blank" rel="noreferrer" style={{ color: "#93c5fd" }}>{listenerUrl}</a>
                </p>
              ) : null}
            </>
          ) : null}
          {activeTab === "settings" ? (
            canManageServices ? (
              <div style={{ marginTop: 12, border: "1px solid rgba(255,255,255,0.16)", borderRadius: 12, padding: 12, display: "grid", gap: 8 }}>
                <p style={{ margin: 0, fontWeight: 700, fontSize: 14 }}>Service Schedule</p>
                <p style={{ margin: 0, fontSize: 13, opacity: 0.8 }}>
                  Add service times for this church. Added services appear in the dropdown for all members.
                </p>
                {resolvedOrgId ? (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                    <button
                      onClick={() => {
                        const qs = new URLSearchParams();
                        qs.set("orgId", resolvedOrgId);
                        if (slug) qs.set("churchSlug", slug);
                        void router.push(`/admin/prompt?${qs.toString()}`);
                      }}
                      style={{
                        borderRadius: 8,
                        border: "1px solid rgba(255,255,255,0.24)",
                        background: "rgba(148,163,184,0.18)",
                        color: "#e2e8f0",
                        fontWeight: 600,
                        padding: "7px 10px",
                        cursor: "pointer",
                      }}
                    >
                      Open Prompt Settings
                    </button>
                    <button
                      onClick={() => {
                        const qs = new URLSearchParams();
                        qs.set("orgId", resolvedOrgId);
                        if (slug) qs.set("churchSlug", slug);
                        void router.push(`/admin/sermon-prep?${qs.toString()}`);
                      }}
                      style={{
                        borderRadius: 8,
                        border: "1px solid rgba(255,255,255,0.24)",
                        background: "rgba(148,163,184,0.18)",
                        color: "#e2e8f0",
                        fontWeight: 600,
                        padding: "7px 10px",
                        cursor: "pointer",
                      }}
                    >
                      Open Sermon Prep
                    </button>
                  </div>
                ) : null}
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                  <input
                    value={newServiceKey}
                    onChange={(e) => setNewServiceKey(e.target.value)}
                    placeholder="service key (example: sun-9am)"
                    style={{ flex: "1 1 220px", borderRadius: 8, border: "1px solid rgba(255,255,255,0.25)", background: "#0f172a", color: "#fff", padding: "8px 10px" }}
                  />
                  <input
                    value={newServiceTitle}
                    onChange={(e) => setNewServiceTitle(e.target.value)}
                    placeholder="title (optional)"
                    style={{ flex: "1 1 220px", borderRadius: 8, border: "1px solid rgba(255,255,255,0.25)", background: "#0f172a", color: "#fff", padding: "8px 10px" }}
                  />
                  <button
                    onClick={addService}
                    disabled={serviceManageBusy || deletingServiceKey.length > 0 || !resolvedOrgId}
                    style={{
                      borderRadius: 8,
                      border: "none",
                      background: "#38bdf8",
                      color: "#082f49",
                      fontWeight: 700,
                      padding: "8px 12px",
                      cursor: serviceManageBusy || deletingServiceKey.length > 0 || !resolvedOrgId ? "not-allowed" : "pointer",
                      opacity: serviceManageBusy || deletingServiceKey.length > 0 || !resolvedOrgId ? 0.6 : 1,
                    }}
                  >
                    {serviceManageBusy ? "Adding..." : "Add Service"}
                  </button>
                </div>
                {serviceManageError ? <p style={{ margin: 0, color: "#fca5a5", fontSize: 13 }}>Error: {serviceManageError}</p> : null}
                <div style={{ display: "grid", gap: 6 }}>
                  {(orgData?.services || []).map((row) => {
                    const isSelected = row.serviceKey === serviceKey;
                    const isLive = Boolean(row.activeRoomId);
                    const deleting = deletingServiceKey === row.serviceKey;
                    return (
                      <div
                        key={row.serviceKey}
                        style={{
                          display: "grid",
                          gridTemplateColumns: "1fr auto",
                          gap: 8,
                          alignItems: "center",
                          border: "1px solid rgba(255,255,255,0.12)",
                          borderRadius: 9,
                          padding: "7px 9px",
                        }}
                      >
                        <p style={{ margin: 0, fontSize: 13, opacity: 0.88 }}>
                          <strong>{row.title}</strong> ({row.serviceKey})
                          {isSelected ? " • selected" : ""}
                          {isLive ? " • live" : ""}
                        </p>
                        <button
                          onClick={() => removeService(row.serviceKey)}
                          disabled={serviceManageBusy || deletingServiceKey.length > 0 || isLive}
                          style={{
                            borderRadius: 8,
                            border: "1px solid rgba(252,165,165,0.55)",
                            background: "rgba(127,29,29,0.36)",
                            color: "#fecaca",
                            fontWeight: 700,
                            padding: "6px 10px",
                            cursor: serviceManageBusy || deletingServiceKey.length > 0 || isLive ? "not-allowed" : "pointer",
                            opacity: serviceManageBusy || deletingServiceKey.length > 0 || isLive ? 0.55 : 1,
                          }}
                        >
                          {deleting ? "Deleting..." : "Delete"}
                        </button>
                      </div>
                    );
                  })}
                </div>
              </div>
            ) : (
              <div style={{ marginTop: 12, border: "1px solid rgba(255,255,255,0.12)", borderRadius: 12, padding: 12, fontSize: 13, opacity: 0.82 }}>
                You do not have permission to manage service schedules for this church.
              </div>
            )
          ) : null}
          {activeTab === "team" ? (
            canManageInvites ? (
              <div style={{ marginTop: 14, border: "1px solid rgba(255,255,255,0.16)", borderRadius: 12, padding: 12, display: "grid", gap: 8 }}>
                <p style={{ margin: 0, fontWeight: 700 }}>Invite Team Member</p>
                <p style={{ margin: 0, fontSize: 13, opacity: 0.8 }}>
                  Create a one-time invite link for another user to join this church.
                </p>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                  <label style={{ display: "grid", gap: 4 }}>
                    <span style={{ fontSize: 12, opacity: 0.75 }}>Role</span>
                    <select
                      value={inviteRole}
                      onChange={(e) => setInviteRole(e.target.value as InviteRoleChoice)}
                      style={{ borderRadius: 8, border: "1px solid rgba(255,255,255,0.25)", background: "#0f172a", color: "#fff", padding: "8px 10px" }}
                    >
                      <option value="host">host</option>
                      <option value="admin">admin</option>
                      <option value="viewer">viewer</option>
                    </select>
                  </label>
                  <button
                    onClick={generateInviteLink}
                    disabled={inviteBusy || !resolvedOrgId}
                    style={{
                      marginTop: 18,
                      borderRadius: 8,
                      border: "none",
                      background: "#38bdf8",
                      color: "#082f49",
                      fontWeight: 700,
                      padding: "8px 12px",
                      cursor: inviteBusy || !resolvedOrgId ? "not-allowed" : "pointer",
                      opacity: inviteBusy || !resolvedOrgId ? 0.6 : 1,
                    }}
                  >
                    {inviteBusy ? "Generating..." : "Generate Invite Link"}
                  </button>
                </div>
                {inviteError ? <p style={{ margin: 0, color: "#fca5a5", fontSize: 13 }}>Error: {inviteError}</p> : null}
                {inviteNotice ? <p style={{ margin: 0, color: "#86efac", fontSize: 13 }}>{inviteNotice}</p> : null}
                {inviteLink ? (
                  <div style={{ display: "grid", gap: 8 }}>
                    <label style={{ display: "grid", gap: 4 }}>
                      <span style={{ margin: 0, fontSize: 13 }}>Invite URL</span>
                      <input
                        readOnly
                        value={inviteLink}
                        style={{
                          borderRadius: 8,
                          border: "1px solid rgba(255,255,255,0.25)",
                          background: "rgba(15,23,42,0.8)",
                          color: "#cbd5e1",
                          fontSize: 13,
                          padding: "8px 10px",
                        }}
                      />
                    </label>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                      <button
                        onClick={copyInviteLink}
                        disabled={copyBusy || shareBusy || inviteBusy}
                        style={{
                          borderRadius: 8,
                          border: "1px solid rgba(255,255,255,0.22)",
                          background: "rgba(148,163,184,0.18)",
                          color: "#e2e8f0",
                          fontWeight: 600,
                          padding: "7px 10px",
                          cursor: copyBusy || shareBusy || inviteBusy ? "not-allowed" : "pointer",
                          opacity: copyBusy || shareBusy || inviteBusy ? 0.6 : 1,
                        }}
                      >
                        {copyBusy ? "Copying..." : "Copy Link"}
                      </button>
                      <button
                        onClick={shareInviteLink}
                        disabled={copyBusy || shareBusy || inviteBusy}
                        style={{
                          borderRadius: 8,
                          border: "1px solid rgba(56,189,248,0.52)",
                          background: "rgba(56,189,248,0.22)",
                          color: "#e0f2fe",
                          fontWeight: 600,
                          padding: "7px 10px",
                          cursor: copyBusy || shareBusy || inviteBusy ? "not-allowed" : "pointer",
                          opacity: copyBusy || shareBusy || inviteBusy ? 0.6 : 1,
                        }}
                      >
                        {shareBusy ? "Sharing..." : "Share via..."}
                      </button>
                    </div>
                  </div>
                ) : null}
                <div style={{ marginTop: 4, borderTop: "1px solid rgba(255,255,255,0.12)", paddingTop: 10 }}>
                  <p style={{ margin: 0, fontSize: 13, opacity: 0.82 }}>Active Invites</p>
                  {invitesLoading ? (
                    <p style={{ marginTop: 6, marginBottom: 0, fontSize: 13, opacity: 0.8 }}>Loading invites...</p>
                  ) : null}
                  {!invitesLoading && !inviteRows.length ? (
                    <p style={{ marginTop: 6, marginBottom: 0, fontSize: 13, opacity: 0.8 }}>No active invites.</p>
                  ) : null}
                  {inviteRows.length ? (
                    <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
                      {inviteRows.map((row) => (
                        <div
                          key={row.inviteId}
                          style={{
                            display: "grid",
                            gridTemplateColumns: "1fr auto",
                            gap: 8,
                            alignItems: "center",
                            border: "1px solid rgba(255,255,255,0.12)",
                            borderRadius: 10,
                            padding: "8px 10px",
                          }}
                        >
                          <div style={{ minWidth: 0 }}>
                            <p style={{ margin: 0, fontSize: 13 }}>
                              Role: <strong>{row.role}</strong>
                            </p>
                            <p style={{ margin: 0, fontSize: 12, opacity: 0.75 }}>
                              Expires: {formatDateTime(row.expiresAt || null)}
                            </p>
                            <p style={{ margin: 0, fontSize: 12, opacity: 0.75 }}>
                              Created: {formatDateTime(row.createdAt || null)}
                            </p>
                          </div>
                          <button
                            onClick={() => revokeInvite(row.inviteId)}
                            disabled={Boolean(revokingInviteId) || inviteBusy}
                            style={{
                              borderRadius: 8,
                              border: "1px solid rgba(252,165,165,0.6)",
                              background: "rgba(127,29,29,0.35)",
                              color: "#fecaca",
                              fontWeight: 700,
                              padding: "7px 10px",
                              cursor: Boolean(revokingInviteId) || inviteBusy ? "not-allowed" : "pointer",
                              opacity: Boolean(revokingInviteId) || inviteBusy ? 0.6 : 1,
                            }}
                          >
                            {revokingInviteId === row.inviteId ? "Revoking..." : "Revoke"}
                          </button>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              </div>
            ) : (
              <div style={{ marginTop: 12, border: "1px solid rgba(255,255,255,0.12)", borderRadius: 12, padding: 12, fontSize: 13, opacity: 0.82 }}>
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
