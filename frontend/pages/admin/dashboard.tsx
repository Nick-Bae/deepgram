import Head from "next/head";
import Link from "next/link";
import { useRouter } from "next/router";
import { useCallback, useEffect, useState } from "react";

import { useAuth } from "../../lib/authContext";
import { fetchAuthMe } from "../../lib/backendAuth";
import { API_URL } from "../../utils/urls";

// ─── Types ───────────────────────────────────────────────────────────────────

type LiveRoom = {
  roomId: string;
  serviceKey: string;
  source: string;
  target: string;
  startedAt: string | null;
  elapsedMinutes: number | null;
  orgId?: string;
  orgName?: string;
};

type OrgRow = {
  orgId: string;
  name: string;
  slug: string;
  plan: string;
  status: string;
  billingStatus: string;
  hardCapReached: boolean;
  memberCount: number;
  serviceCount: number;
  liveRoomCount: number;
  liveRooms: LiveRoom[];
  currentMonthMinutes: number;
  maxMinutesPerMonth: number;
  trialSecondsRemaining: number | null;
  sermonPrepUsageUsd: number;
  sermonPrepBudgetUsd: number;
  createdAt: string | null;
  stripeCustomerId: string;
  stripeSubscriptionId: string;
};

type DashboardData = {
  generatedAt: string;
  summary: {
    totalOrgs: number;
    totalMembers: number;
    liveRoomCount: number;
    planCounts: Record<string, number>;
    billingStatusCounts: Record<string, number>;
  };
  organizations: OrgRow[];
  liveRooms: LiveRoom[];
};

type UsageStat = {
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  estimatedUsd: number;
  requestsCount: number;
};

type PlatformUsage = {
  periodKey: string;
  generatedAt: string;
  liveTranslation: UsageStat;
  sermonPrep: UsageStat;
  deepgram: { audioSeconds: number; estimatedUsd: number };
};

type PlatformConfig = {
  liveTranslationInputCostPerMillion: number;
  liveTranslationOutputCostPerMillion: number;
  deepgramCostPerMinute: number;
  gcpBillingAccountId: string;
  spendAlertEmail: string;
  spendAlertOpenaiThresholdUsd: number;
  spendAlertDeepgramThresholdUsd: number;
};

type GcpUsage = {
  available: boolean;
  error?: string;
  projectId?: string;
  periodKey?: string;
  firestore?: { reads: number; writes: number; deletes: number; estimatedUsd: number };
  billing?: { spendUsd: number | null; budgetUsd: number | null; error: string | null };
  billingAccountId?: string;
  billingConsoleUrl?: string;
};

// ─── Constants ───────────────────────────────────────────────────────────────

// Must match billing/models.py PLAN_SPECS amounts
const PLAN_MRR: Record<string, number> = {
  trial: 0,
  starter: 20,
  growth: 40,
  premium: 60,
};

const PLAN_LABEL: Record<string, string> = {
  trial: "Trial",
  starter: "Starter",
  growth: "Growth",
  premium: "Premium",
};

const PLAN_COLOR: Record<string, string> = {
  trial: "#6b7280",
  starter: "#3b82f6",
  growth: "#8b5cf6",
  premium: "#f59e0b",
};

const BILLING_STATUS_COLOR: Record<string, string> = {
  trialing: "#6b7280",
  active: "#10b981",
  past_due: "#f59e0b",
  canceled: "#ef4444",
  unpaid: "#ef4444",
  incomplete: "#f59e0b",
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

function planBadge(plan: string) {
  const color = PLAN_COLOR[plan] || "#6b7280";
  const label = PLAN_LABEL[plan] || plan;
  return (
    <span
      style={{
        background: color + "20",
        color,
        border: `1px solid ${color}40`,
        borderRadius: 4,
        padding: "2px 8px",
        fontSize: 12,
        fontWeight: 600,
      }}
    >
      {label}
    </span>
  );
}

function statusBadge(status: string) {
  const color = BILLING_STATUS_COLOR[status] || "#6b7280";
  return (
    <span
      style={{
        background: color + "18",
        color,
        border: `1px solid ${color}40`,
        borderRadius: 4,
        padding: "2px 8px",
        fontSize: 12,
        fontWeight: 500,
      }}
    >
      {status}
    </span>
  );
}

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  } catch {
    return iso;
  }
}

function trialMinsRemaining(seconds: number | null): string {
  if (seconds === null) return "—";
  const mins = Math.floor(seconds / 60);
  if (mins <= 0) return "Exhausted";
  return `${mins} min left`;
}

function fmtMonthKey(yyyymm: string): string {
  const year = yyyymm.slice(0, 4);
  const month = parseInt(yyyymm.slice(4));
  return new Date(parseInt(year), month - 1, 1).toLocaleDateString("en-US", { month: "short", year: "numeric" });
}

function deltaLabel(current: number, prev: number): React.ReactElement | null {
  if (prev === 0) return null;
  const pct = ((current - prev) / prev) * 100;
  const up = pct >= 0;
  return (
    <span style={{ fontSize: 11, color: up ? "#10b981" : "#ef4444", fontWeight: 600, marginLeft: 4 }}>
      {up ? "▲" : "▼"} {Math.abs(pct).toFixed(0)}%
    </span>
  );
}

// ─── Sub-components ──────────────────────────────────────────────────────────

function UsageBar({ used, max }: { used: number; max: number }) {
  if (max <= 0) {
    return <span style={{ color: "#9ca3af", fontSize: 13 }}>{used > 0 ? `${used} min` : "—"}</span>;
  }
  const pct = Math.min(100, (used / max) * 100);
  const color = pct >= 90 ? "#ef4444" : pct >= 70 ? "#f59e0b" : "#10b981";
  return (
    <div style={{ minWidth: 100 }}>
      <div style={{ fontSize: 12, color: "#374151", marginBottom: 3 }}>
        {used} / {max} min
      </div>
      <div style={{ background: "#e5e7eb", borderRadius: 4, height: 6, overflow: "hidden" }}>
        <div style={{ width: `${pct}%`, background: color, height: "100%", borderRadius: 4, transition: "width 0.3s" }} />
      </div>
    </div>
  );
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function AdminDashboardPage() {
  const router = useRouter();
  const { user, loading: authLoading, getIdToken } = useAuth();
  const [data, setData] = useState<DashboardData | null>(null);
  const [platformUsage, setPlatformUsage] = useState<PlatformUsage | null>(null);
  const [prevUsage, setPrevUsage] = useState<PlatformUsage | null>(null);
  const [selectedPeriod, setSelectedPeriod] = useState<string | null>(null);
  const [platformConfig, setPlatformConfig] = useState<PlatformConfig | null>(null);
  const [configDraft, setConfigDraft] = useState<PlatformConfig | null>(null);
  const [configSaving, setConfigSaving] = useState(false);
  const [configSaved, setConfigSaved] = useState(false);
  const [gcpUsage, setGcpUsage] = useState<GcpUsage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);
  const [search, setSearch] = useState("");
  const [planFilter, setPlanFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");

  const load = useCallback(async () => {
    if (!user) return;
    setLoading(true);
    setError(null);
    try {
      const token = await getIdToken();
      if (!token) throw new Error("Not authenticated");
      const me = await fetchAuthMe(token);
      if (!me.user.isMaster) {
        void router.replace("/admin");
        return;
      }

      // Compute previous month key
      const now = new Date();
      const curYear = now.getFullYear();
      const curMonth = now.getMonth() + 1;
      const prevYear = curMonth === 1 ? curYear - 1 : curYear;
      const prevMonth = curMonth === 1 ? 12 : curMonth - 1;
      const prevPeriodKey = `${prevYear}${String(prevMonth).padStart(2, "0")}`;

      const authHeader = { Authorization: `Bearer ${token as string}` };
      const [res, usageRes, prevUsageRes, cfgRes, gcpRes] = await Promise.all([
        fetch(`${API_URL}/api/admin/dashboard`, { headers: authHeader }),
        fetch(`${API_URL}/api/admin/platform-usage`, { headers: authHeader }),
        fetch(`${API_URL}/api/admin/platform-usage?period=${prevPeriodKey}`, { headers: authHeader }),
        fetch(`${API_URL}/api/admin/platform-config`, { headers: authHeader }),
        fetch(`${API_URL}/api/admin/gcp-usage`, { headers: authHeader }),
      ]);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail || `HTTP ${res.status}`);
      }
      const payload = (await res.json()) as DashboardData;
      setData(payload);
      if (usageRes.ok) setPlatformUsage((await usageRes.json()) as PlatformUsage);
      if (prevUsageRes.ok) setPrevUsage((await prevUsageRes.json()) as PlatformUsage);
      if (cfgRes.ok) {
        const cfg = (await cfgRes.json()) as PlatformConfig;
        setPlatformConfig(cfg);
        setConfigDraft(cfg);
      }
      if (gcpRes.ok) setGcpUsage((await gcpRes.json()) as GcpUsage);
      setLastRefreshed(new Date());
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [user, getIdToken, router]);

  const loadPeriodUsage = useCallback(async (periodKey: string) => {
    if (!user) return;
    try {
      const token = await getIdToken();
      if (!token) return;
      const authHeader = { Authorization: `Bearer ${token as string}` };
      const curYear = parseInt(periodKey.slice(0, 4));
      const curMonth = parseInt(periodKey.slice(4));
      const prevYear = curMonth === 1 ? curYear - 1 : curYear;
      const prevMonth = curMonth === 1 ? 12 : curMonth - 1;
      const prevKey = `${prevYear}${String(prevMonth).padStart(2, "0")}`;
      const [usageRes, prevUsageRes] = await Promise.all([
        fetch(`${API_URL}/api/admin/platform-usage?period=${periodKey}`, { headers: authHeader }),
        fetch(`${API_URL}/api/admin/platform-usage?period=${prevKey}`, { headers: authHeader }),
      ]);
      if (usageRes.ok) setPlatformUsage((await usageRes.json()) as PlatformUsage);
      if (prevUsageRes.ok) setPrevUsage((await prevUsageRes.json()) as PlatformUsage);
    } catch {
      // non-critical — period usage failure doesn't block the dashboard
    }
  }, [user, getIdToken]);

  useEffect(() => {
    if (authLoading) return;
    if (!user) {
      void router.replace(`/login?next=${encodeURIComponent("/admin/dashboard")}`);
      return;
    }
    void load();
  }, [authLoading, user, load, router]);

  const filtered = (data?.organizations ?? []).filter((org) => {
    if (planFilter !== "all" && org.plan !== planFilter) return false;
    if (statusFilter !== "all" && org.billingStatus !== statusFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      return org.name.toLowerCase().includes(q) || org.slug.toLowerCase().includes(q) || org.orgId.toLowerCase().includes(q);
    }
    return true;
  });

  // ─── Computed metrics ────────────────────────────────────────────────────

  const orgs = data?.organizations ?? [];

  // MRR: only count active billing status
  const mrr = orgs
    .filter((o) => o.billingStatus === "active")
    .reduce((sum, o) => sum + (PLAN_MRR[o.plan] || 0), 0);

  // Trial conversion funnel
  const trialing = orgs.filter((o) => o.billingStatus === "trialing");
  const trialActive = trialing.filter((o) => o.trialSecondsRemaining === null || o.trialSecondsRemaining > 0);
  const trialExhausted = trialing.filter((o) => o.trialSecondsRemaining !== null && o.trialSecondsRemaining === 0);
  const paidActive = orgs.filter((o) => o.plan !== "trial" && o.billingStatus === "active");

  // At-risk orgs
  const atRisk = orgs.filter((o) => ["past_due", "unpaid", "canceled"].includes(o.billingStatus));

  // Sermon prep budget warnings (> 80% used)
  const sermonWarnings = orgs.filter(
    (o) => o.sermonPrepBudgetUsd > 0 && o.sermonPrepUsageUsd / o.sermonPrepBudgetUsd >= 0.8
  );

  // Top consumers by minutes
  const topConsumers = [...orgs]
    .filter((o) => o.currentMonthMinutes > 0)
    .sort((a, b) => b.currentMonthMinutes - a.currentMonthMinutes)
    .slice(0, 5);

  // Recent signups
  const recentSignups = [...orgs]
    .filter((o) => o.createdAt)
    .sort((a, b) => new Date(b.createdAt!).getTime() - new Date(a.createdAt!).getTime())
    .slice(0, 8);

  // Growth chart: signups per week over last 5 weeks
  const growthBuckets: { label: string; count: number }[] = [];
  {
    const now = Date.now();
    for (let w = 4; w >= 0; w--) {
      const weekStart = now - (w + 1) * 7 * 86400 * 1000;
      const weekEnd = now - w * 7 * 86400 * 1000;
      const count = orgs.filter((o) => {
        if (!o.createdAt) return false;
        const t = new Date(o.createdAt).getTime();
        return t >= weekStart && t < weekEnd;
      }).length;
      const d = new Date(weekStart);
      const label = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
      growthBuckets.push({ label, count });
    }
  }
  const maxBucketCount = Math.max(1, ...growthBuckets.map((b) => b.count));

  // Period selector: last 6 months
  const periodOptions: { value: string; label: string }[] = [];
  {
    const now = new Date();
    for (let i = 0; i < 6; i++) {
      const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
      const y = d.getFullYear();
      const m = d.getMonth() + 1;
      const key = `${y}${String(m).padStart(2, "0")}`;
      periodOptions.push({ value: key, label: fmtMonthKey(key) });
    }
  }
  const currentPeriodKey = periodOptions[0]?.value ?? "";
  const activePeriodKey = selectedPeriod ?? currentPeriodKey;

  // ─── Render ───────────────────────────────────────────────────────────────

  if (authLoading || (loading && !data)) {
    return (
      <div style={styles.page}>
        <div style={styles.loadingBox}>
          <div style={styles.spinner} />
          <span style={{ color: "#6b7280", marginLeft: 12 }}>Loading dashboard…</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={styles.page}>
        <div style={styles.errorBox}>
          <strong>Error:</strong> {error}
          <button onClick={() => void load()} style={styles.refreshBtn}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  const summary = data?.summary;
  const plans = Object.entries(summary?.planCounts ?? {}).sort((a, b) => b[1] - a[1]);
  const billingStatuses = Object.entries(summary?.billingStatusCounts ?? {}).sort((a, b) => b[1] - a[1]);

  return (
    <>
      <Head>
        <title>Admin Dashboard – Worship Translation Studio</title>
      </Head>
      <div style={styles.page}>
        {/* Header */}
        <div style={styles.header}>
          <div>
            <h1 style={styles.title}>Admin Dashboard</h1>
            <p style={styles.subtitle}>
              System overview · {lastRefreshed ? `Refreshed ${lastRefreshed.toLocaleTimeString()}` : "Loading…"}
            </p>
          </div>
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <Link href="/admin" style={styles.backLink}>
              ← Admin Tools
            </Link>
            <button onClick={() => void load()} style={styles.refreshBtn} disabled={loading}>
              {loading ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        </div>

        {/* Summary Cards */}
        {summary && (
          <div style={styles.cardRow}>
            <div style={styles.card}>
              <div style={styles.cardValue}>{summary.totalOrgs}</div>
              <div style={styles.cardLabel}>Organizations</div>
            </div>
            <div style={styles.card}>
              <div style={styles.cardValue}>{summary.totalMembers}</div>
              <div style={styles.cardLabel}>Total Members</div>
            </div>
            <div style={{ ...styles.card, borderColor: summary.liveRoomCount > 0 ? "#10b981" : "#e5e7eb" }}>
              <div style={{ ...styles.cardValue, color: summary.liveRoomCount > 0 ? "#10b981" : "#111827" }}>
                {summary.liveRoomCount}
              </div>
              <div style={styles.cardLabel}>Live Rooms Now</div>
            </div>
            <div style={{ ...styles.card, borderColor: mrr > 0 ? "#6366f1" : "#e5e7eb" }}>
              <div style={{ ...styles.cardValue, color: "#6366f1" }}>${mrr.toLocaleString()}</div>
              <div style={styles.cardLabel}>Est. MRR</div>
              <div style={{ fontSize: 11, color: "#9ca3af", marginTop: 4 }}>active subscriptions only</div>
            </div>
            <div style={styles.card}>
              <div style={styles.cardBreakdown}>
                {plans.map(([plan, count]) => (
                  <span key={plan} style={{ marginRight: 8 }}>
                    {planBadge(plan)} <strong>{count}</strong>
                  </span>
                ))}
              </div>
              <div style={styles.cardLabel}>Plan Distribution</div>
            </div>
            <div style={styles.card}>
              <div style={styles.cardBreakdown}>
                {billingStatuses.map(([s, count]) => (
                  <span key={s} style={{ marginRight: 8 }}>
                    {statusBadge(s)} <strong>{count}</strong>
                  </span>
                ))}
              </div>
              <div style={styles.cardLabel}>Billing Status</div>
            </div>
          </div>
        )}

        {/* Trial Conversion Funnel */}
        <section style={styles.section}>
          <h2 style={styles.sectionTitle}>Trial Conversion Funnel</h2>
          <div style={styles.cardRow}>
            <div style={{ ...styles.card, borderColor: "#6b7280", flex: "1 1 140px" }}>
              <div style={{ ...styles.cardValue, color: "#6b7280" }}>{trialActive.length}</div>
              <div style={styles.cardLabel}>Active Trials</div>
              <div style={{ fontSize: 11, color: "#9ca3af", marginTop: 4 }}>minutes remaining</div>
            </div>
            <div style={{ display: "flex", alignItems: "center", fontSize: 20, color: "#d1d5db", padding: "0 4px" }}>→</div>
            <div style={{ ...styles.card, borderColor: trialExhausted.length > 0 ? "#f59e0b" : "#e5e7eb", flex: "1 1 140px" }}>
              <div style={{ ...styles.cardValue, color: trialExhausted.length > 0 ? "#f59e0b" : "#374151" }}>
                {trialExhausted.length}
              </div>
              <div style={styles.cardLabel}>Trial Exhausted</div>
              <div style={{ fontSize: 11, color: "#9ca3af", marginTop: 4 }}>need to convert</div>
            </div>
            <div style={{ display: "flex", alignItems: "center", fontSize: 20, color: "#d1d5db", padding: "0 4px" }}>→</div>
            <div style={{ ...styles.card, borderColor: "#10b981", flex: "1 1 140px" }}>
              <div style={{ ...styles.cardValue, color: "#10b981" }}>{paidActive.length}</div>
              <div style={styles.cardLabel}>Paid & Active</div>
              <div style={{ fontSize: 11, color: "#9ca3af", marginTop: 4 }}>
                {trialing.length + paidActive.length > 0
                  ? `${Math.round((paidActive.length / (trialing.length + paidActive.length)) * 100)}% conversion`
                  : "no data"}
              </div>
            </div>
            <div style={{ ...styles.card, flex: "2 1 200px" }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: "#374151", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.04em" }}>
                New Signups — Last 5 Weeks
              </div>
              <div style={{ display: "flex", alignItems: "flex-end", gap: 8, height: 48 }}>
                {growthBuckets.map((b) => (
                  <div key={b.label} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 2 }}>
                    <div style={{ fontSize: 11, fontWeight: 600, color: "#6366f1" }}>{b.count || ""}</div>
                    <div
                      style={{
                        width: "100%",
                        background: b.count > 0 ? "#6366f1" : "#e5e7eb",
                        borderRadius: "3px 3px 0 0",
                        height: b.count > 0 ? `${Math.max(8, (b.count / maxBucketCount) * 36)}px` : "4px",
                        transition: "height 0.3s",
                      }}
                    />
                    <div style={{ fontSize: 10, color: "#9ca3af", whiteSpace: "nowrap" }}>{b.label}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </section>

        {/* Platform Usage */}
        {platformUsage && (
          <section style={styles.section}>
            <h2 style={{ ...styles.sectionTitle, display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
              Platform Usage
              <span style={{ fontSize: 12, color: "#9ca3af", fontWeight: 400 }}>estimates only</span>
              <select
                value={activePeriodKey}
                onChange={(e) => {
                  const key = e.target.value;
                  setSelectedPeriod(key === currentPeriodKey ? null : key);
                  void loadPeriodUsage(key);
                }}
                style={{ fontSize: 12, padding: "2px 6px", borderRadius: 4, border: "1px solid #d1d5db", color: "#374151", fontWeight: 400, cursor: "pointer" }}
              >
                {periodOptions.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}{opt.value === currentPeriodKey ? " (current)" : ""}
                  </option>
                ))}
              </select>
            </h2>
            <div style={styles.cardRow}>
              <div style={styles.usageCard}>
                <div style={styles.usageCardHeader}>
                  <span style={styles.usageCardIcon}>⚡</span>
                  <span style={styles.usageCardTitle}>OpenAI — Live Translation</span>
                </div>
                <div style={styles.usageCardStat}>
                  ${platformUsage.liveTranslation.estimatedUsd.toFixed(4)}
                  {prevUsage && deltaLabel(platformUsage.liveTranslation.estimatedUsd, prevUsage.liveTranslation.estimatedUsd)}
                </div>
                <div style={styles.usageCardMeta}>{platformUsage.liveTranslation.totalTokens.toLocaleString()} tokens · {platformUsage.liveTranslation.requestsCount.toLocaleString()} requests</div>
                <div style={styles.usageCardMeta}>{platformUsage.liveTranslation.promptTokens.toLocaleString()} in · {platformUsage.liveTranslation.completionTokens.toLocaleString()} out</div>
                {prevUsage && (
                  <div style={{ ...styles.usageCardMeta, marginTop: 6, color: "#9ca3af", fontStyle: "italic" }}>
                    {fmtMonthKey(prevUsage.periodKey)}: ${prevUsage.liveTranslation.estimatedUsd.toFixed(4)}
                  </div>
                )}
              </div>
              <div style={styles.usageCard}>
                <div style={styles.usageCardHeader}>
                  <span style={styles.usageCardIcon}>📖</span>
                  <span style={styles.usageCardTitle}>OpenAI — Sermon Prep</span>
                </div>
                <div style={styles.usageCardStat}>
                  ${platformUsage.sermonPrep.estimatedUsd.toFixed(4)}
                  {prevUsage && deltaLabel(platformUsage.sermonPrep.estimatedUsd, prevUsage.sermonPrep.estimatedUsd)}
                </div>
                <div style={styles.usageCardMeta}>{platformUsage.sermonPrep.totalTokens.toLocaleString()} tokens · {platformUsage.sermonPrep.requestsCount.toLocaleString()} requests</div>
                <div style={styles.usageCardMeta}>{platformUsage.sermonPrep.promptTokens.toLocaleString()} in · {platformUsage.sermonPrep.completionTokens.toLocaleString()} out</div>
                {prevUsage && (
                  <div style={{ ...styles.usageCardMeta, marginTop: 6, color: "#9ca3af", fontStyle: "italic" }}>
                    {fmtMonthKey(prevUsage.periodKey)}: ${prevUsage.sermonPrep.estimatedUsd.toFixed(4)}
                  </div>
                )}
              </div>
              <div style={styles.usageCard}>
                <div style={styles.usageCardHeader}>
                  <span style={styles.usageCardIcon}>🎙️</span>
                  <span style={styles.usageCardTitle}>Deepgram — Speech-to-Text</span>
                </div>
                <div style={styles.usageCardStat}>
                  ${platformUsage.deepgram.estimatedUsd.toFixed(4)}
                  {prevUsage && deltaLabel(platformUsage.deepgram.estimatedUsd, prevUsage.deepgram.estimatedUsd)}
                </div>
                <div style={styles.usageCardMeta}>{Math.floor(platformUsage.deepgram.audioSeconds / 60)}m {Math.round(platformUsage.deepgram.audioSeconds % 60)}s audio processed</div>
                {prevUsage && (
                  <div style={{ ...styles.usageCardMeta, marginTop: 6, color: "#9ca3af", fontStyle: "italic" }}>
                    {fmtMonthKey(prevUsage.periodKey)}: ${prevUsage.deepgram.estimatedUsd.toFixed(4)}
                  </div>
                )}
              </div>
              <div style={styles.usageCard}>
                <div style={styles.usageCardHeader}>
                  <span style={styles.usageCardIcon}>☁️</span>
                  <span style={styles.usageCardTitle}>Firestore</span>
                </div>
                {gcpUsage?.available && gcpUsage.firestore ? (
                  <>
                    <div style={styles.usageCardStat}>${gcpUsage.firestore.estimatedUsd.toFixed(4)}</div>
                    <div style={styles.usageCardMeta}>{gcpUsage.firestore.reads.toLocaleString()} reads</div>
                    <div style={styles.usageCardMeta}>{gcpUsage.firestore.writes.toLocaleString()} writes · {gcpUsage.firestore.deletes.toLocaleString()} deletes</div>
                  </>
                ) : (
                  <div style={{ ...styles.usageCardMeta, marginTop: 8, color: "#f59e0b", whiteSpace: "normal", lineHeight: 1.4 }}>
                    {gcpUsage?.error ?? "Loading…"}
                  </div>
                )}
              </div>
              <div style={styles.usageCard}>
                <div style={styles.usageCardHeader}>
                  <span style={styles.usageCardIcon}>🧾</span>
                  <span style={styles.usageCardTitle}>Google Cloud Billing</span>
                </div>
                {gcpUsage?.billing?.spendUsd != null ? (
                  <>
                    <div style={styles.usageCardStat}>${gcpUsage.billing.spendUsd.toFixed(2)}</div>
                    <div style={styles.usageCardMeta}>this month</div>
                    {gcpUsage.billing.budgetUsd != null && (
                      <div style={styles.usageCardMeta}>budget: ${gcpUsage.billing.budgetUsd.toFixed(0)}</div>
                    )}
                  </>
                ) : (
                  <div style={{ ...styles.usageCardMeta, marginTop: 8, color: gcpUsage?.billing?.error ? "#f59e0b" : "#6b7280", whiteSpace: "normal", lineHeight: 1.4 }}>
                    {gcpUsage?.billing?.error ?? (gcpUsage?.billingAccountId ? "Loading…" : "Set billing account ID in Pricing Rates below")}
                  </div>
                )}
                <a href={gcpUsage?.billingConsoleUrl ?? "https://console.cloud.google.com/billing"} target="_blank" rel="noopener noreferrer" style={styles.externalLink}>
                  View Billing Console →
                </a>
              </div>
            </div>
          </section>
        )}

        {/* Pricing Rates */}
        {configDraft && (
          <section style={styles.section}>
            <h2 style={styles.sectionTitle}>Pricing Rates</h2>
            <div style={styles.configBox}>
              <div style={styles.configGrid}>
                <div style={styles.configField}>
                  <label style={styles.configLabel}>OpenAI Input ($/M tokens)</label>
                  <input
                    type="number"
                    step="0.001"
                    min="0"
                    style={styles.configInput}
                    value={configDraft.liveTranslationInputCostPerMillion}
                    onChange={(e) => setConfigDraft((d) => d && { ...d, liveTranslationInputCostPerMillion: parseFloat(e.target.value) || 0 })}
                  />
                  <span style={styles.configHint}>Live translation &amp; sermon prep input tokens</span>
                </div>
                <div style={styles.configField}>
                  <label style={styles.configLabel}>OpenAI Output ($/M tokens)</label>
                  <input
                    type="number"
                    step="0.001"
                    min="0"
                    style={styles.configInput}
                    value={configDraft.liveTranslationOutputCostPerMillion}
                    onChange={(e) => setConfigDraft((d) => d && { ...d, liveTranslationOutputCostPerMillion: parseFloat(e.target.value) || 0 })}
                  />
                  <span style={styles.configHint}>Live translation &amp; sermon prep output tokens</span>
                </div>
                <div style={styles.configField}>
                  <label style={styles.configLabel}>Deepgram ($/min)</label>
                  <input
                    type="number"
                    step="0.0001"
                    min="0"
                    style={styles.configInput}
                    value={configDraft.deepgramCostPerMinute}
                    onChange={(e) => setConfigDraft((d) => d && { ...d, deepgramCostPerMinute: parseFloat(e.target.value) || 0 })}
                  />
                  <span style={styles.configHint}>Nova-2 streaming default: $0.0059/min</span>
                </div>
                <div style={styles.configField}>
                  <label style={styles.configLabel}>GCP Billing Account ID</label>
                  <input
                    type="text"
                    style={styles.configInput}
                    placeholder="XXXXXX-XXXXXX-XXXXXX"
                    value={configDraft.gcpBillingAccountId}
                    onChange={(e) => setConfigDraft((d) => d && { ...d, gcpBillingAccountId: e.target.value })}
                  />
                  <span style={styles.configHint}>Used to generate direct billing report links</span>
                </div>
                <div style={styles.configField}>
                  <label style={styles.configLabel}>Spend Alert Email</label>
                  <input
                    type="email"
                    style={styles.configInput}
                    placeholder="admin@example.com"
                    value={configDraft.spendAlertEmail}
                    onChange={(e) => setConfigDraft((d) => d && { ...d, spendAlertEmail: e.target.value })}
                  />
                  <span style={styles.configHint}>Receive an email when monthly spend exceeds thresholds below</span>
                </div>
                <div style={styles.configField}>
                  <label style={styles.configLabel}>OpenAI Alert Threshold ($)</label>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    style={styles.configInput}
                    placeholder="0 = disabled"
                    value={configDraft.spendAlertOpenaiThresholdUsd}
                    onChange={(e) => setConfigDraft((d) => d && { ...d, spendAlertOpenaiThresholdUsd: parseFloat(e.target.value) || 0 })}
                  />
                  <span style={styles.configHint}>Alert when OpenAI (live + sermon) monthly spend exceeds this amount</span>
                </div>
                <div style={styles.configField}>
                  <label style={styles.configLabel}>Deepgram Alert Threshold ($)</label>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    style={styles.configInput}
                    placeholder="0 = disabled"
                    value={configDraft.spendAlertDeepgramThresholdUsd}
                    onChange={(e) => setConfigDraft((d) => d && { ...d, spendAlertDeepgramThresholdUsd: parseFloat(e.target.value) || 0 })}
                  />
                  <span style={styles.configHint}>Alert when Deepgram monthly spend exceeds this amount</span>
                </div>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 16 }}>
                <button
                  style={{ ...styles.refreshBtn, opacity: configSaving ? 0.6 : 1 }}
                  disabled={configSaving}
                  onClick={async () => {
                    if (!configDraft) return;
                    setConfigSaving(true);
                    setConfigSaved(false);
                    try {
                      const token = await getIdToken();
                      const res = await fetch(`${API_URL}/api/admin/platform-config`, {
                        method: "POST",
                        headers: { Authorization: `Bearer ${token as string}`, "Content-Type": "application/json" },
                        body: JSON.stringify(configDraft),
                      });
                      if (res.ok) {
                        const updated = (await res.json()) as PlatformConfig;
                        setPlatformConfig(updated);
                        setConfigDraft(updated);
                        setConfigSaved(true);
                        setTimeout(() => setConfigSaved(false), 3000);
                      }
                    } finally {
                      setConfigSaving(false);
                    }
                  }}
                >
                  {configSaving ? "Saving…" : "Save Rates"}
                </button>
                {configSaved && <span style={{ color: "#10b981", fontSize: 14, fontWeight: 500 }}>Saved</span>}
                {configDraft && platformConfig && JSON.stringify(configDraft) !== JSON.stringify(platformConfig) && (
                  <button
                    style={{ background: "none", border: "none", color: "#9ca3af", fontSize: 13, cursor: "pointer" }}
                    onClick={() => setConfigDraft(platformConfig)}
                  >
                    Reset
                  </button>
                )}
              </div>
            </div>
          </section>
        )}

        {/* At-Risk / Churn */}
        {atRisk.length > 0 && (
          <section style={styles.section}>
            <h2 style={{ ...styles.sectionTitle, color: "#b91c1c" }}>
              ⚠ At-Risk Organizations ({atRisk.length})
            </h2>
            <div style={styles.tableWrap}>
              <table style={styles.table}>
                <thead>
                  <tr>
                    {["Church", "Plan", "Billing Status", "Members", "Usage (mo)", "Since"].map((h) => (
                      <th key={h} style={styles.th}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {atRisk.map((org) => (
                    <tr key={org.orgId} style={{ background: "#fef2f2" }}>
                      <td style={styles.td}>
                        <div>
                          <Link href={`/host/c/${org.slug}`} style={styles.tableLink}>{org.name}</Link>
                          <div style={{ fontSize: 11, color: "#9ca3af" }}>{org.slug}</div>
                        </div>
                      </td>
                      <td style={styles.td}>{planBadge(org.plan)}</td>
                      <td style={styles.td}>{statusBadge(org.billingStatus)}</td>
                      <td style={{ ...styles.td, textAlign: "center" }}>{org.memberCount}</td>
                      <td style={styles.td}>
                        <UsageBar used={org.currentMonthMinutes} max={org.maxMinutesPerMonth} />
                      </td>
                      <td style={{ ...styles.td, color: "#9ca3af", fontSize: 12 }}>{fmtDate(org.createdAt)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {/* Sermon Prep Budget Warnings */}
        {sermonWarnings.length > 0 && (
          <section style={styles.section}>
            <h2 style={{ ...styles.sectionTitle, color: "#92400e" }}>
              Sermon Prep Near Budget ({sermonWarnings.length})
            </h2>
            <div style={styles.tableWrap}>
              <table style={styles.table}>
                <thead>
                  <tr>
                    {["Church", "Plan", "Sermon Spend", "Budget", "% Used"].map((h) => (
                      <th key={h} style={styles.th}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sermonWarnings
                    .sort((a, b) => b.sermonPrepUsageUsd / b.sermonPrepBudgetUsd - a.sermonPrepUsageUsd / a.sermonPrepBudgetUsd)
                    .map((org) => {
                      const pct = Math.round((org.sermonPrepUsageUsd / org.sermonPrepBudgetUsd) * 100);
                      const color = pct >= 100 ? "#ef4444" : pct >= 90 ? "#f59e0b" : "#d97706";
                      return (
                        <tr key={org.orgId} style={{ background: "#fffbeb" }}>
                          <td style={styles.td}>
                            <Link href={`/host/c/${org.slug}`} style={styles.tableLink}>{org.name}</Link>
                          </td>
                          <td style={styles.td}>{planBadge(org.plan)}</td>
                          <td style={styles.td}>${org.sermonPrepUsageUsd.toFixed(3)}</td>
                          <td style={styles.td}>${org.sermonPrepBudgetUsd.toFixed(2)}</td>
                          <td style={styles.td}>
                            <span style={{ color, fontWeight: 700 }}>{pct}%</span>
                            <div style={{ background: "#e5e7eb", borderRadius: 4, height: 6, overflow: "hidden", marginTop: 4, width: 80 }}>
                              <div style={{ width: `${Math.min(100, pct)}%`, background: color, height: "100%", borderRadius: 4 }} />
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {/* Top Consumers + Recent Signups */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20, marginBottom: 28 }}>
          {/* Top Consumers */}
          <section style={{ ...styles.section, marginBottom: 0 }}>
            <h2 style={styles.sectionTitle}>Top Consumers This Month</h2>
            {topConsumers.length === 0 ? (
              <div style={styles.emptyNote}>No usage data this month.</div>
            ) : (
              <div style={styles.tableWrap}>
                <table style={styles.table}>
                  <thead>
                    <tr>
                      {["Church", "Plan", "Minutes Used"].map((h) => (
                        <th key={h} style={styles.th}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {topConsumers.map((org, i) => (
                      <tr key={org.orgId}>
                        <td style={styles.td}>
                          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                            <span style={{ fontSize: 11, color: "#9ca3af", width: 16 }}>#{i + 1}</span>
                            <Link href={`/host/c/${org.slug}`} style={styles.tableLink}>{org.name}</Link>
                          </div>
                        </td>
                        <td style={styles.td}>{planBadge(org.plan)}</td>
                        <td style={styles.td}>
                          <UsageBar used={org.currentMonthMinutes} max={org.maxMinutesPerMonth} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* Recent Signups */}
          <section style={{ ...styles.section, marginBottom: 0 }}>
            <h2 style={styles.sectionTitle}>Recent Signups</h2>
            {recentSignups.length === 0 ? (
              <div style={styles.emptyNote}>No signups yet.</div>
            ) : (
              <div style={styles.tableWrap}>
                <table style={styles.table}>
                  <thead>
                    <tr>
                      {["Church", "Plan", "Joined"].map((h) => (
                        <th key={h} style={styles.th}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {recentSignups.map((org) => (
                      <tr key={org.orgId}>
                        <td style={styles.td}>
                          <Link href={`/host/c/${org.slug}`} style={styles.tableLink}>{org.name}</Link>
                          <div style={{ fontSize: 11, color: "#9ca3af" }}>{org.slug}</div>
                        </td>
                        <td style={styles.td}>{planBadge(org.plan)}</td>
                        <td style={{ ...styles.td, color: "#6b7280", fontSize: 12 }}>{fmtDate(org.createdAt)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>

        {/* Live Rooms */}
        {(data?.liveRooms ?? []).length > 0 && (
          <section style={styles.section}>
            <h2 style={styles.sectionTitle}>
              <span style={styles.liveDot} /> Live Right Now
            </h2>
            <div style={styles.tableWrap}>
              <table style={styles.table}>
                <thead>
                  <tr>
                    {["Church", "Service", "Languages", "Started At", "Duration"].map((h) => (
                      <th key={h} style={styles.th}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data!.liveRooms.map((r) => (
                    <tr key={r.roomId} style={{ background: "#f0fdf4" }}>
                      <td style={styles.td}>
                        <Link href={`/host/c/${r.orgName}`} style={styles.tableLink}>{r.orgName}</Link>
                      </td>
                      <td style={styles.td}><code style={styles.code}>{r.serviceKey}</code></td>
                      <td style={styles.td}>{r.source} → {r.target}</td>
                      <td style={styles.td}>{r.startedAt ? new Date(r.startedAt).toLocaleTimeString() : "—"}</td>
                      <td style={styles.td}>
                        {r.elapsedMinutes !== null ? (
                          <span style={{ color: "#10b981", fontWeight: 600 }}>{r.elapsedMinutes} min</span>
                        ) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {/* Organizations Table */}
        <section style={styles.section}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12, flexWrap: "wrap", gap: 8 }}>
            <h2 style={{ ...styles.sectionTitle, margin: 0 }}>Organizations ({filtered.length})</h2>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <input
                style={styles.searchInput}
                placeholder="Search name / slug / ID…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <select style={styles.select} value={planFilter} onChange={(e) => setPlanFilter(e.target.value)}>
                <option value="all">All Plans</option>
                <option value="trial">Trial</option>
                <option value="starter">Starter</option>
                <option value="growth">Growth</option>
                <option value="premium">Premium</option>
              </select>
              <select style={styles.select} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                <option value="all">All Statuses</option>
                <option value="trialing">Trialing</option>
                <option value="active">Active</option>
                <option value="past_due">Past Due</option>
                <option value="canceled">Canceled</option>
                <option value="unpaid">Unpaid</option>
              </select>
            </div>
          </div>
          <div style={styles.tableWrap}>
            <table style={styles.table}>
              <thead>
                <tr>
                  {["Church", "Plan", "Billing", "Members", "Services", "Live", "Usage (mo)", "Trial Left", "Sermon $", "Created", "Actions"].map((h) => (
                    <th key={h} style={styles.th}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filtered.map((org) => (
                  <tr
                    key={org.orgId}
                    style={{
                      background: org.liveRoomCount > 0 ? "#f0fdf4" : org.hardCapReached ? "#fef2f2" : "white",
                    }}
                  >
                    <td style={styles.td}>
                      <div>
                        <Link href={`/host/c/${org.slug}`} style={styles.tableLink}>{org.name}</Link>
                        <div style={{ fontSize: 11, color: "#9ca3af" }}>{org.slug}</div>
                      </div>
                    </td>
                    <td style={styles.td}>{planBadge(org.plan)}</td>
                    <td style={styles.td}>
                      <div>
                        {statusBadge(org.billingStatus)}
                        {org.hardCapReached && (
                          <div style={{ fontSize: 11, color: "#ef4444", fontWeight: 600, marginTop: 2 }}>⚠ Cap reached</div>
                        )}
                      </div>
                    </td>
                    <td style={{ ...styles.td, textAlign: "center" }}>{org.memberCount}</td>
                    <td style={{ ...styles.td, textAlign: "center" }}>{org.serviceCount}</td>
                    <td style={{ ...styles.td, textAlign: "center" }}>
                      {org.liveRoomCount > 0 ? (
                        <span style={{ color: "#10b981", fontWeight: 700 }}>● {org.liveRoomCount}</span>
                      ) : (
                        <span style={{ color: "#d1d5db" }}>—</span>
                      )}
                    </td>
                    <td style={styles.td}>
                      <UsageBar used={org.currentMonthMinutes} max={org.maxMinutesPerMonth} />
                    </td>
                    <td style={styles.td}>
                      <span
                        style={{
                          color:
                            org.trialSecondsRemaining === null
                              ? "#d1d5db"
                              : org.trialSecondsRemaining === 0
                              ? "#ef4444"
                              : org.trialSecondsRemaining < 300
                              ? "#f59e0b"
                              : "#374151",
                        }}
                      >
                        {trialMinsRemaining(org.trialSecondsRemaining)}
                      </span>
                    </td>
                    <td style={styles.td}>
                      {org.sermonPrepUsageUsd > 0 ? (
                        <span>
                          ${org.sermonPrepUsageUsd.toFixed(3)}
                          {org.sermonPrepBudgetUsd > 0 && (
                            <span style={{ color: "#9ca3af" }}> / ${org.sermonPrepBudgetUsd.toFixed(2)}</span>
                          )}
                        </span>
                      ) : (
                        <span style={{ color: "#d1d5db" }}>—</span>
                      )}
                    </td>
                    <td style={{ ...styles.td, color: "#9ca3af", fontSize: 12 }}>{fmtDate(org.createdAt)}</td>
                    <td style={styles.td}>
                      <div style={{ display: "flex", gap: 6 }}>
                        {org.stripeCustomerId && (
                          <a
                            href={`https://dashboard.stripe.com/customers/${org.stripeCustomerId}`}
                            target="_blank"
                            rel="noopener noreferrer"
                            style={styles.actionBtn}
                            title="View in Stripe"
                          >
                            Stripe
                          </a>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
                {filtered.length === 0 && (
                  <tr>
                    <td colSpan={11} style={{ ...styles.td, textAlign: "center", color: "#9ca3af", padding: "32px 16px" }}>
                      No organizations match the current filters.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>

        {/* Admin Tool Links */}
        <section style={styles.section}>
          <h2 style={styles.sectionTitle}>Admin Tools</h2>
          <div style={styles.toolGrid}>
            {[
              { href: "/admin/sermon-prep", label: "Sermon Prep", desc: "Draft & finalize sermon translations" },
              { href: "/admin/examples", label: "Translation Examples", desc: "Review, correct, export few-shots" },
              { href: "/admin/prompt", label: "Custom Prompt", desc: "Edit the translator system prompt" },
              { href: "/admin/display", label: "Display Speed", desc: "Adjust caption pacing on public displays" },
            ].map((tool) => (
              <Link key={tool.href} href={tool.href} style={styles.toolCard}>
                <div style={styles.toolLabel}>{tool.label}</div>
                <div style={styles.toolDesc}>{tool.desc}</div>
              </Link>
            ))}
          </div>
        </section>
      </div>
    </>
  );
}

// ─── Styles ──────────────────────────────────────────────────────────────────

const styles: Record<string, React.CSSProperties> = {
  page: {
    padding: "28px 32px",
    fontFamily: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    background: "#f5f6f8",
    minHeight: "100vh",
    maxWidth: 1400,
    margin: "0 auto",
  },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    marginBottom: 24,
    flexWrap: "wrap",
    gap: 12,
  },
  title: {
    fontSize: 26,
    fontWeight: 700,
    margin: 0,
    color: "#111827",
  },
  subtitle: {
    color: "#6b7280",
    fontSize: 14,
    margin: "4px 0 0",
  },
  backLink: {
    color: "#6b7280",
    textDecoration: "none",
    fontSize: 14,
    padding: "6px 12px",
    background: "white",
    borderRadius: 6,
    border: "1px solid #e5e7eb",
  },
  refreshBtn: {
    background: "#111827",
    color: "white",
    border: "none",
    borderRadius: 6,
    padding: "6px 14px",
    fontSize: 14,
    cursor: "pointer",
    fontWeight: 500,
  },
  cardRow: {
    display: "flex",
    gap: 12,
    marginBottom: 24,
    flexWrap: "wrap",
  },
  card: {
    background: "white",
    borderRadius: 10,
    padding: "16px 20px",
    border: "1px solid #e5e7eb",
    flex: "1 1 160px",
    boxShadow: "0 1px 4px rgba(0,0,0,0.04)",
    minWidth: 140,
  },
  cardValue: {
    fontSize: 28,
    fontWeight: 700,
    color: "#111827",
    lineHeight: 1,
    marginBottom: 6,
  },
  cardLabel: {
    fontSize: 12,
    color: "#6b7280",
    textTransform: "uppercase",
    letterSpacing: "0.05em",
    fontWeight: 500,
  },
  cardBreakdown: {
    display: "flex",
    flexWrap: "wrap",
    gap: 4,
    marginBottom: 6,
    alignItems: "center",
  },
  section: {
    marginBottom: 28,
  },
  sectionTitle: {
    fontSize: 16,
    fontWeight: 600,
    color: "#374151",
    marginBottom: 12,
    display: "flex",
    alignItems: "center",
    gap: 8,
  },
  liveDot: {
    display: "inline-block",
    width: 10,
    height: 10,
    borderRadius: "50%",
    background: "#10b981",
    boxShadow: "0 0 0 3px #10b98130",
    animation: "pulse 2s infinite",
  },
  tableWrap: {
    overflowX: "auto",
    background: "white",
    borderRadius: 10,
    border: "1px solid #e5e7eb",
    boxShadow: "0 1px 4px rgba(0,0,0,0.04)",
  },
  table: {
    width: "100%",
    borderCollapse: "collapse",
    fontSize: 14,
  },
  th: {
    textAlign: "left" as const,
    padding: "10px 14px",
    background: "#f9fafb",
    fontWeight: 600,
    color: "#374151",
    fontSize: 12,
    textTransform: "uppercase" as const,
    letterSpacing: "0.04em",
    borderBottom: "1px solid #e5e7eb",
    whiteSpace: "nowrap" as const,
  },
  td: {
    padding: "10px 14px",
    color: "#374151",
    borderBottom: "1px solid #f3f4f6",
    verticalAlign: "middle" as const,
    whiteSpace: "nowrap" as const,
  },
  tableLink: {
    color: "#111827",
    fontWeight: 600,
    textDecoration: "none",
  },
  code: {
    fontFamily: "monospace",
    background: "#f3f4f6",
    borderRadius: 3,
    padding: "1px 5px",
    fontSize: 12,
  },
  searchInput: {
    border: "1px solid #e5e7eb",
    borderRadius: 6,
    padding: "6px 10px",
    fontSize: 13,
    outline: "none",
    width: 200,
  },
  select: {
    border: "1px solid #e5e7eb",
    borderRadius: 6,
    padding: "6px 10px",
    fontSize: 13,
    background: "white",
    cursor: "pointer",
    outline: "none",
  },
  toolGrid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
    gap: 12,
  },
  toolCard: {
    background: "white",
    border: "1px solid #e5e7eb",
    borderRadius: 10,
    padding: "16px 18px",
    textDecoration: "none",
    boxShadow: "0 1px 4px rgba(0,0,0,0.04)",
    display: "block",
  },
  toolLabel: {
    fontWeight: 600,
    color: "#111827",
    fontSize: 14,
    marginBottom: 4,
  },
  toolDesc: {
    color: "#6b7280",
    fontSize: 13,
  },
  loadingBox: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    minHeight: "60vh",
  },
  spinner: {
    width: 24,
    height: 24,
    border: "3px solid #e5e7eb",
    borderTopColor: "#6366f1",
    borderRadius: "50%",
    animation: "spin 0.8s linear infinite",
  },
  usageCard: {
    background: "white",
    borderRadius: 10,
    padding: "16px 18px",
    border: "1px solid #e5e7eb",
    flex: "1 1 180px",
    boxShadow: "0 1px 4px rgba(0,0,0,0.04)",
    minWidth: 160,
  },
  usageCardHeader: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    marginBottom: 8,
  },
  usageCardIcon: {
    fontSize: 16,
  },
  usageCardTitle: {
    fontSize: 12,
    fontWeight: 600,
    color: "#374151",
    textTransform: "uppercase" as const,
    letterSpacing: "0.04em",
  },
  usageCardStat: {
    fontSize: 26,
    fontWeight: 700,
    color: "#111827",
    lineHeight: 1,
    marginBottom: 6,
  },
  usageCardMeta: {
    fontSize: 12,
    color: "#6b7280",
    marginTop: 2,
  },
  configBox: {
    background: "white",
    borderRadius: 10,
    border: "1px solid #e5e7eb",
    padding: "20px 24px",
    boxShadow: "0 1px 4px rgba(0,0,0,0.04)",
  },
  configGrid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
    gap: 20,
  },
  configField: {
    display: "flex",
    flexDirection: "column" as const,
    gap: 4,
  },
  configLabel: {
    fontSize: 12,
    fontWeight: 600,
    color: "#374151",
    textTransform: "uppercase" as const,
    letterSpacing: "0.04em",
  },
  configInput: {
    border: "1px solid #d1d5db",
    borderRadius: 6,
    padding: "8px 10px",
    fontSize: 15,
    fontWeight: 500,
    color: "#111827",
    outline: "none",
    width: "100%",
  },
  configHint: {
    fontSize: 11,
    color: "#9ca3af",
  },
  externalLink: {
    display: "inline-block",
    marginTop: 10,
    fontSize: 13,
    color: "#6366f1",
    textDecoration: "none",
    fontWeight: 500,
  },
  errorBox: {
    background: "#fef2f2",
    border: "1px solid #fecaca",
    color: "#b91c1c",
    borderRadius: 8,
    padding: "16px 20px",
    display: "flex",
    alignItems: "center",
    gap: 12,
    margin: "40px auto",
    maxWidth: 600,
  },
  actionBtn: {
    fontSize: 11,
    fontWeight: 600,
    color: "#6366f1",
    border: "1px solid #e0e7ff",
    borderRadius: 4,
    padding: "2px 8px",
    textDecoration: "none",
    background: "#eef2ff",
    whiteSpace: "nowrap" as const,
  },
  emptyNote: {
    background: "white",
    borderRadius: 10,
    border: "1px solid #e5e7eb",
    padding: "20px",
    color: "#9ca3af",
    fontSize: 14,
    textAlign: "center" as const,
  },
};
