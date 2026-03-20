# Analysis: Billing & Admin Dashboard

> Feature: billing
> Phase: Check
> Date: 2026-03-20
> Match Rate: **94% (17/18)**
> Status: **PASS**

---

## Summary

| Metric | Value |
|---|---|
| Match Rate | 94% |
| Total items checked | 18 |
| Matched | 17 |
| Gaps | 0 (code) / 1 (stale doc) |
| Lint | Clean (0 errors) |

All 18 design requirements are fully implemented in code. The 1-point deduction is for Section 5 of the design document still listing items as "not yet implemented" — but they are now done.

---

## Item-by-Item Results

| # | Requirement | Status |
|---|---|:---:|
| 1 | `/admin/platform-usage?period=YYYYMM` endpoint with optional `period` param | MATCH |
| 2 | Period param validation (regex `^\d{6}$`) returning HTTP 422 | MATCH |
| 3 | `get_platform_usage_summary(period_key=None)` in multichurch_store | MATCH |
| 4 | `PLAN_MRR` matching billing/models.py prices ($20/$40/$60) | MATCH |
| 5 | `selectedPeriod` state | MATCH |
| 6 | `loadPeriodUsage` callback | MATCH |
| 7 | `periodOptions` (last 6 months) | MATCH |
| 8 | Period `<select>` dropdown in Platform Usage header | MATCH |
| 9 | `UsageBar` component (green/amber/red at 70%/90%) | MATCH |
| 10 | `deltaLabel` helper (▲/▼ % change) | MATCH |
| 11 | `fmtMonthKey` helper (YYYYMM → "Mar 2026") | MATCH |
| 12 | `prevUsage` state with previous month fetch | MATCH |
| 13 | `mrr` computed metric (active orgs only) | MATCH |
| 14 | Trial conversion funnel (trialActive, trialExhausted, paidActive, %) | MATCH |
| 15 | `atRisk` orgs list | MATCH |
| 16 | `sermonWarnings` (>= 80% budget consumed) | MATCH |
| 17 | `topConsumers` (top 5 by minute usage) | MATCH |
| 18 | `growthBuckets` (weekly signup chart, last 5 weeks) | MATCH |

---

## Action Required

**Documentation only**: Update `docs/02-design/features/billing.design.md` Section 5 ("Missing / Not Yet Implemented") to reflect that all items are now complete.
