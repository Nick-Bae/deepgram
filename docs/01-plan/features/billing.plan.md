# Plan: Billing & Admin Dashboard

## Executive Summary

| Perspective | Description |
|---|---|
| **Problem** | Platform operators have no visibility into revenue, usage trends, or cost anomalies — making it impossible to catch over-spending or churn before it becomes critical. |
| **Solution** | Extend the existing Stripe billing system with admin dashboard MRR tracking, trial usage visualization, period-over-period analytics, and spend alerting. |
| **Function / UX Effect** | Admins see live MRR, per-org trial usage bars, % change indicators month-over-month, and email alerts when GCP or OpenAI spend exceeds thresholds. |
| **Core Value** | Operational confidence — the platform can be monitored and financially governed without manual Stripe/GCP console inspection. |

---

## 1. Feature Overview

**Feature Name**: `billing`
**Started**: 2026-03-20
**Level**: Dynamic
**Phase**: Plan

### Background

The platform already has a complete Stripe billing integration:
- Plans: Trial (free, 20 min), Starter ($20, 5 services), Growth ($40, 12 services), Premium ($60, unlimited)
- Stripe webhook sync → Firestore billing state per org
- Email notifications: subscription start/cancel, payment failure/recovery
- Hard cap enforcement at service start when `hardCapReached` is set
- Admin dashboard at `/admin/dashboard` showing live org/room/member counts and GCP billing

### Current Status (as of 2026-03-20)

**Already implemented:**
- Stripe checkout + portal session endpoints
- Webhook handler for all Stripe lifecycle events
- `billing/models.py` — plan specs, billing state normalization
- `billing/stripe_client.py` — Stripe API wrapper
- Admin dashboard — org list, plan breakdown, GCP Monitoring API integration
- Spend alerts (OpenAI + Deepgram) via email on threshold breach

**In progress (uncommitted):**
- `PLAN_MRR` constant in dashboard for MRR calculation per plan
- `UsageBar` component — visual trial usage (used/max minutes + colored bar)
- `deltaLabel` — month-over-month % change indicators (▲/▼)
- `fmtMonthKey` — human-readable period formatting
- `/admin/platform-usage?period=` — optional period parameter for historical comparison
- `get_platform_usage_summary(period_key=...)` — backend period filtering

---

## 2. Goals

### Primary Goals
1. **MRR visibility** — Admin dashboard shows estimated MRR from current plan distribution
2. **Trial usage monitoring** — Visual bars showing each org's trial minute consumption
3. **Period comparison** — View usage for past billing periods, not just current
4. **Spend alerting** — Email alert when GCP/OpenAI costs exceed configured thresholds (done)

### Non-Goals
- Self-serve plan upgrade/downgrade UI for org owners (Stripe portal already handles this)
- Automated churn intervention or dunning logic beyond Stripe's built-in retry
- Per-user billing (billing is per-org only)
- Usage-based billing (flat subscription only)

---

## 3. User Stories

| As a... | I want to... | So that... |
|---|---|---|
| Super admin | See current MRR by plan tier | I can track revenue at a glance |
| Super admin | See each org's trial usage bar | I know which trials are close to limit |
| Super admin | Compare this month's usage to last month | I can spot unusual growth or drops |
| Super admin | Receive email when GCP spend exceeds threshold | I catch cost spikes before the bill arrives |
| Org owner | Check my subscription status and payment history | I can manage my own billing without contacting support |
| Org owner | Upgrade or cancel my plan | I control my spend |

---

## 4. Scope

### In Scope

**Backend (`backend/app/routes/admin.py`)**
- `GET /admin/platform-usage?period=YYYYMM` — accept optional period key, return per-period breakdown
- MRR field in platform summary response

**Backend (`backend/app/services/multichurch_store.py`)**
- `get_platform_usage_summary(period_key=None)` — filter usage by period when provided

**Frontend (`frontend/pages/admin/dashboard.tsx`)**
- `PLAN_MRR` constant mapping plan keys → monthly price
- `UsageBar` component for trial minute visualization
- `deltaLabel` helper for % change display
- `fmtMonthKey` helper for period labels
- Period selector UI to switch between months

### Out of Scope
- Stripe webhook retry logic (handled by Stripe)
- Firestore billing rule changes
- New plan tiers or pricing changes

---

## 5. Technical Approach

### Data Flow

```
Admin browser
  → GET /admin/platform-usage?period=202603
  → admin.py: _require_master() + multichurch_store.get_platform_usage_summary(period_key="202603")
  → Firestore: organizations/{orgId}/usage/{periodKey}
  → Response: { orgs: [...], totalMrr: N, planBreakdown: {...} }
  → Dashboard: renders UsageBar + deltaLabel per org
```

### MRR Calculation

```
totalMrr = sum(PLAN_MRR[org.planKey] for org in active_orgs where billing.status in {active, past_due})
```

Plan prices for MRR display are defined client-side in `PLAN_MRR` (dashboard constant) — not pulled from Stripe, since Stripe prices may vary by promo. Update `PLAN_MRR` if pricing changes.

### Period Key Format

`YYYYMM` string (e.g., `"202603"` for March 2026). Stored in Firestore as the document ID under `organizations/{orgId}/usage/{periodKey}`.

### Trial Usage Display

```
UsageBar: used / max minutes → colored progress bar
  green:  < 70% used
  amber:  70–90% used
  red:    ≥ 90% used (approaching limit)
```

### Spend Alert Thresholds (already implemented)

Configured via admin dashboard. Email sent via Resend when:
- OpenAI daily spend > threshold
- Deepgram daily spend > threshold
- GCP Firestore daily cost > threshold

---

## 6. Affected Files

| File | Change Type | Description |
|---|---|---|
| `backend/app/routes/admin.py` | Modify | Add `period` query param to `/admin/platform-usage` |
| `backend/app/services/multichurch_store.py` | Modify | `get_platform_usage_summary(period_key=None)` |
| `frontend/pages/admin/dashboard.tsx` | Modify | MRR, UsageBar, deltaLabel, period selector |

---

## 7. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Firestore usage docs missing for some orgs (period not tracked yet) | Medium | Default to 0 when doc absent; show "—" not error |
| `PLAN_MRR` gets out of sync with actual Stripe prices | Low | Comment in code to update when pricing changes; consider reading from Stripe prices API later |
| GCP Monitoring API rate limits on admin dashboard refresh | Low | 5-min cache already in place (`_gcp_usage_cache`) |
| Period selector showing too many historical months | Low | Limit to last 12 periods in dropdown |

---

## 8. Acceptance Criteria

- [ ] `/admin/platform-usage` accepts `?period=YYYYMM` and returns data filtered to that period
- [ ] When `period` is omitted, returns current period (existing behavior unchanged)
- [ ] Admin dashboard shows estimated MRR total and per-plan breakdown
- [ ] Trial orgs show `UsageBar` with minute consumption and color coding
- [ ] Period selector allows switching between last 6 months
- [ ] `deltaLabel` shows ▲/▼ % vs previous period for key metrics
- [ ] No regression in spend alert functionality
- [ ] `npm run lint` passes with no errors

---

## 9. Next Steps

```
/pdca design billing    ← Create detailed design document
/pdca do billing        ← Implementation guide
/pdca analyze billing   ← Gap analysis after implementation
```
