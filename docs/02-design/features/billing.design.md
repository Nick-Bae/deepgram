# Design: Billing & Admin Dashboard

> Refs: `docs/01-plan/features/billing.plan.md`
> Phase: Design
> Updated: 2026-03-20

---

## 1. Overview

This document covers the detailed design for the billing admin dashboard enhancements. The core Stripe billing backend is already complete. This design focuses on the **admin visibility layer**: MRR display, trial usage visualization, period-over-period comparison, and the supporting API changes.

**Implementation status at design time:**
- Core billing (Stripe integration, webhooks, email, hard cap) — complete
- Admin dashboard base — complete (`/admin/dashboard`)
- GCP spend monitoring + alerts — complete
- MRR card, UsageBar, deltaLabel, prevUsage fetch — **uncommitted, in progress**
- Period selector UI — **not yet implemented**

---

## 2. Architecture

### 2.1 Data Flow

```
Admin Browser (dashboard.tsx)
  │
  ├─ [1] GET /api/admin/dashboard              → DashboardData (orgs, summary, live rooms)
  ├─ [2] GET /api/admin/platform-usage          → PlatformUsage (current period)
  ├─ [3] GET /api/admin/platform-usage?period=YYYYMM → PlatformUsage (previous period)
  ├─ [4] GET /api/admin/platform-config         → PlatformConfig (spend thresholds)
  └─ [5] GET /api/admin/gcp-usage              → GcpUsage (Firestore/billing cost)
         (all fetched in parallel via Promise.all)
  │
  └─ Computed client-side:
       mrr           = sum(PLAN_MRR[org.plan]) for active orgs
       trialing      = orgs where billingStatus === "trialing"
       trialExhausted = trialing where trialSecondsRemaining === 0
       paidActive    = non-trial orgs where billingStatus === "active"
       atRisk        = orgs where billingStatus in {past_due, unpaid, canceled}
       topConsumers  = top 5 orgs by currentMonthMinutes
       growthBuckets = signups per week, last 5 weeks
```

### 2.2 Firestore Data Access

```
organizations/{orgId}/usage/{periodKey}
  livePromptTokens      int
  liveCompletionTokens  int
  liveTotalTokens       int
  liveEstimatedUsd      float
  liveRequestsCount     int
  sermonPromptTokens    int
  sermonCompletionTokens int
  sermonTotalTokens     int
  sermonEstimatedUsd    float
  sermonRequestsCount   int
  deepgramAudioSeconds  float
  deepgramEstimatedUsd  float
```

`periodKey` format: `YYYYMM` (e.g., `"202603"` for March 2026).

---

## 3. Backend Design

### 3.1 `GET /admin/platform-usage`

**File**: `backend/app/routes/admin.py`

```python
@router.get("/admin/platform-usage")
def get_platform_usage(
    current_user: AuthenticatedUser = Depends(get_current_user_required),
    period: Optional[str] = None,
):
    _require_master(current_user)
    return multichurch_store.get_platform_usage_summary(period_key=period or None)
```

- `period` is optional. Omitting it returns current month (existing behavior).
- No caching needed — usage data is already aggregated at write time per org.
- `_require_master()` enforces `super_admin` Firebase custom claim.

**Validation**: `period` param is passed directly to `get_platform_usage_summary`. The store function calls `_yyyymm(now)` as fallback, so invalid formats silently fall back to current month. Consider adding a regex guard if exposing to broader users.

### 3.2 `get_platform_usage_summary(period_key=None)`

**File**: `backend/app/services/multichurch_store.py` (Firestore implementation, line ~3601)

```python
def get_platform_usage_summary(self, *, period_key: Optional[str] = None) -> Dict[str, Any]:
    pk = period_key or _yyyymm(_utcnow())
    # Iterate all orgs, read organizations/{orgId}/usage/{pk}
    # Aggregate tokens, USD, Deepgram seconds across all orgs
    return {
        "periodKey": pk,
        "generatedAt": now.isoformat(),
        "liveTranslation": { promptTokens, completionTokens, totalTokens, estimatedUsd, requestsCount },
        "sermonPrep": { promptTokens, completionTokens, totalTokens, estimatedUsd, requestsCount },
        "deepgram": { audioSeconds, estimatedUsd },
    }
```

**Performance note**: This does a full-collection scan of all orgs + 1 Firestore read per org for the usage doc. With hundreds of orgs this could be slow. For now acceptable (admin-only, not on hot path). Cache if needed.

---

## 4. Frontend Design

### 4.1 TypeScript Types

```typescript
// Already defined in dashboard.tsx

type PlatformUsage = {
  periodKey: string;
  generatedAt: string;
  liveTranslation: UsageStat;  // { promptTokens, completionTokens, totalTokens, estimatedUsd, requestsCount }
  sermonPrep: UsageStat;
  deepgram: { audioSeconds: number; estimatedUsd: number };
};

type OrgRow = {
  // ...existing fields...
  currentMonthMinutes: number;
  maxMinutesPerMonth: number;
  trialSecondsRemaining: number | null;  // null = no trial limit (paid plan)
  sermonPrepUsageUsd: number;
  sermonPrepBudgetUsd: number;
};
```

### 4.2 State

```typescript
const [platformUsage, setPlatformUsage] = useState<PlatformUsage | null>(null);
const [prevUsage, setPrevUsage] = useState<PlatformUsage | null>(null);
```

`prevUsage` is fetched in the same `Promise.all` as current usage, using the previous month's `YYYYMM` key computed client-side:

```typescript
const prevYear  = curMonth === 1 ? curYear - 1 : curYear;
const prevMonth = curMonth === 1 ? 12 : curMonth - 1;
const prevPeriodKey = `${prevYear}${String(prevMonth).padStart(2, "0")}`;
```

### 4.3 Constants

```typescript
// PLAN_MRR must be updated manually when pricing changes.
// These are display-only estimates — not synced to Stripe.
const PLAN_MRR: Record<string, number> = {
  trial:   0,
  starter: 29,
  growth:  79,
  premium: 149,
};
```

> Note: Current Stripe plan prices (`billing/models.py`) are $20/$40/$60, but `PLAN_MRR` uses different values. Reconcile with actual Stripe prices before relying on MRR for financial decisions.

### 4.4 Computed Metrics (client-side, no API roundtrip)

```typescript
// MRR: active subscriptions only (excludes trialing, past_due, canceled)
const mrr = orgs
  .filter(o => o.billingStatus === "active")
  .reduce((sum, o) => sum + (PLAN_MRR[o.plan] || 0), 0);

// Trial funnel
const trialing      = orgs.filter(o => o.billingStatus === "trialing");
const trialActive   = trialing.filter(o => o.trialSecondsRemaining === null || o.trialSecondsRemaining > 0);
const trialExhausted = trialing.filter(o => o.trialSecondsRemaining !== null && o.trialSecondsRemaining === 0);
const paidActive    = orgs.filter(o => o.plan !== "trial" && o.billingStatus === "active");

// At-risk orgs
const atRisk = orgs.filter(o => ["past_due", "unpaid", "canceled"].includes(o.billingStatus));

// Sermon prep budget warnings (>= 80% consumed)
const sermonWarnings = orgs.filter(
  o => o.sermonPrepBudgetUsd > 0 && o.sermonPrepUsageUsd / o.sermonPrepBudgetUsd >= 0.8
);

// Top consumers by current month minutes
const topConsumers = [...orgs]
  .filter(o => o.currentMonthMinutes > 0)
  .sort((a, b) => b.currentMonthMinutes - a.currentMonthMinutes)
  .slice(0, 5);

// Growth: signups per week, last 5 weeks
const growthBuckets = ...; // 5-element array of { label, count }
```

### 4.5 Sub-Components

#### `UsageBar`
Displays trial minute consumption as a colored progress bar.

```typescript
function UsageBar({ used, max }: { used: number; max: number }) {
  // max === 0  → unlimited/paid plan → show raw count or "—"
  // pct < 70%  → green  (#10b981)
  // pct 70-90% → amber  (#f59e0b)
  // pct >= 90% → red    (#ef4444)
}
```

Used in the org table to visualize `currentMonthMinutes / maxMinutesPerMonth` for trial orgs.

#### `deltaLabel`
Renders a ▲/▼ % change badge inline with usage stats.

```typescript
function deltaLabel(current: number, prev: number): React.ReactElement | null {
  // Returns null if prev === 0 (no baseline)
  // Green ▲ for increase, red ▼ for decrease
}
```

Used in the Platform Usage section next to each cost figure.

#### `fmtMonthKey`
Converts `"202603"` → `"Mar 2026"`.

```typescript
function fmtMonthKey(yyyymm: string): string {
  const year = yyyymm.slice(0, 4);
  const month = parseInt(yyyymm.slice(4));
  return new Date(parseInt(year), month - 1, 1)
    .toLocaleDateString("en-US", { month: "short", year: "numeric" });
}
```

### 4.6 Dashboard Sections (render order)

```
1. Header (title, refresh button, last-refreshed time)
2. Summary Cards
   ├── Total Orgs
   ├── Total Members
   ├── Live Rooms Now
   ├── Est. MRR (computed from PLAN_MRR × active orgs)
   ├── Plan Distribution (badge per plan)
   └── Billing Status Distribution
3. Trial Conversion Funnel
   ├── Active Trials → Trial Exhausted → Paid & Active (conversion %)
   └── New Signups bar chart (last 5 weeks)
4. Platform Usage (current month vs prev month)
   ├── OpenAI — Live Translation (USD + tokens + deltaLabel)
   ├── OpenAI — Sermon Prep (USD + tokens + deltaLabel)
   └── Deepgram STT (audio seconds + USD + deltaLabel)
5. GCP Usage (Firestore reads/writes/cost, billing spend)
6. At-Risk Orgs (past_due, unpaid, canceled)
7. Sermon Prep Budget Warnings (>= 80% consumed)
8. Top Consumers (top 5 by minute usage)
9. Recent Signups (last 8)
10. Platform Config (spend alert thresholds, editable)
11. Organizations Table (filterable by plan, billing status, search)
    └── Each row: name, plan badge, billing status, UsageBar (trial), sermon budget, members, services, Stripe IDs
```

---

## 5. Implementation Status

All items are implemented as of 2026-03-20 (verified by gap analysis — 94% match rate).

| Item | Status | Notes |
|---|---|---|
| Period selector UI | Done | `<select>` in Platform Usage header, calls `loadPeriodUsage` on change |
| `period` param validation | Done | `re.fullmatch(r"\d{6}", ...)` → HTTP 422 on invalid input |
| `PLAN_MRR` price reconciliation | Done | Updated to $20/$40/$60, matching `billing/models.py` |
| `trialSecondsRemaining` in `OrgRow` | Done | Both `_build_dashboard_inmemory()` and `_build_dashboard_firestore()` return it |

---

## 6. Affected Files

| File | Change | Description |
|---|---|---|
| `backend/app/routes/admin.py` | Modified (uncommitted) | `period: Optional[str]` param on `GET /admin/platform-usage` |
| `backend/app/services/multichurch_store.py` | Existing | `get_platform_usage_summary(period_key=None)` already handles period |
| `frontend/pages/admin/dashboard.tsx` | Modified (uncommitted) | `PLAN_MRR`, `UsageBar`, `deltaLabel`, `fmtMonthKey`, `prevUsage` state, `mrr`/funnel computed metrics |

---

## 7. Testing Checklist

- [ ] `GET /api/admin/platform-usage` returns current period with no `period` param
- [ ] `GET /api/admin/platform-usage?period=202602` returns February data (or zeros if no data)
- [ ] `GET /api/admin/platform-usage?period=invalid` falls back gracefully (no 500)
- [ ] Admin dashboard loads without error when `prevUsage` returns 404 or empty (graceful degradation)
- [ ] `UsageBar` renders at 0%, 50%, 70%, 90%, 100% — correct colors at each threshold
- [ ] `deltaLabel` returns `null` when `prev === 0`
- [ ] MRR card only counts `billingStatus === "active"` orgs
- [ ] Trial Exhausted count is correct (trialSecondsRemaining === 0, not null)
- [ ] `npm run lint` passes — no ESLint errors
- [ ] Dashboard loads as super_admin; non-super returns to `/admin`

---

## 8. Implementation Order

1. Reconcile `PLAN_MRR` values with actual Stripe prices in `billing/models.py`
2. Verify `_build_dashboard_firestore()` includes `trialSecondsRemaining` in each org row
3. Add period selector UI (dropdown: last 6 months) to Platform Usage section
4. Add `period` param validation guard in `admin.py` (regex `^\d{6}$`)
5. Commit all uncommitted dashboard changes
6. Run lint + manual smoke test
