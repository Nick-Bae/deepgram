# Plan: change-plan

## Executive Summary

| Perspective | Description |
|---|---|
| **Problem** | Users on paid plans have no self-serve way to upgrade or downgrade their subscription — and the current plan model (service count limits) doesn't reflect real church usage patterns, making plan tiers feel arbitrary. |
| **Solution** | Replace service-count limits with monthly broadcast-minute limits, and add a `POST /billing/change-plan` endpoint: upgrades apply immediately with proration, downgrades are scheduled via Stripe Subscription Schedule to take effect at period end. |
| **Function / UX Effect** | Org owners can upgrade or downgrade directly from the billing UI; upgrades take effect instantly, downgrades show a clear "effective on [date]" confirmation — no surprise charges, no cut-off broadcasts. |
| **Core Value** | Billing that matches how churches actually use the platform — measured in time, not arbitrary service counts — with a transparent, low-friction plan change experience. |

---

## 1. Feature Overview

**Feature Name**: `change-plan`
**Started**: 2026-03-23
**Level**: Dynamic
**Phase**: Plan

### Background

The platform has three paid plans (Starter $20, Growth $40, Premium $60) plus a free Trial.
Current limits are based on `maxServiceKeys` (number of service definitions: 5 / 12 / unlimited).
Through product discussion, this was identified as a poor fit: a "service" in Firestore is a
configuration entry, not a broadcast event — the 12-service limit on Growth doesn't translate
to a meaningful usage boundary for churches.

The new model replaces service-count caps with **monthly broadcast minutes**, which directly
reflects infrastructure cost (Deepgram STT, OpenAI translation). Plan changes (upgrades and
downgrades) are not currently supported except through Stripe's hosted portal, which is a poor UX.

### Current State

**Already implemented:**
- `POST /billing/checkout-session` — creates a new subscription (trial → paid)
- `POST /billing/portal-session` — redirects to Stripe's hosted portal
- Stripe webhook handler — `customer.subscription.updated`, `invoice.payment_succeeded`, etc.
- `billing/stripe_client.py` — Stripe API wrapper (`create_customer`, `create_checkout_session`, `create_billing_portal_session`, `retrieve_subscription`, `list_subscriptions`)
- `billing/models.py` — `PlanSpec`, `PLAN_SPECS`, `plan_spec()` helper
- Trial minute tracking — `trialSecondsUsed`, `trialMinutesLimit` in Firestore billing state

**Not yet implemented:**
- Plan upgrade / downgrade API
- Monthly minute limits for paid plans
- Soft cap enforcement (notify but don't cut off paid broadcasts)
- `update_subscription()` and `create_subscription_schedule()` in Stripe client

---

## 2. Goals

### Primary Goals
1. **Time-based plan limits** — Replace `maxServiceKeys` with `monthlyMinutesLimit` as the primary billing enforcement axis
2. **Immediate upgrades** — Upgrading a plan takes effect instantly with Stripe proration
3. **Scheduled downgrades** — Downgrading takes effect at the end of the current billing period via Stripe Subscription Schedule
4. **Soft cap for paid plans** — Broadcasts continue past the monthly limit; admin is notified to upgrade
5. **Plan change API** — `POST /billing/change-plan` for org owners/admins

### Non-Goals
- Removing `maxServiceKeys` from the data model entirely (keep as soft reference, just don't enforce)
- Proration refund on downgrades (no credit issued — user keeps current plan until period end)
- Mid-broadcast cutoff for any paid plan
- Cancellation flow (handled by Stripe portal)
- Per-user billing

---

## 3. New Plan Limits

| Plan | Price | Monthly Minutes | Monthly Hours | Fits |
|---|---|---|---|---|
| Trial | Free | 20 min | — | One-time test |
| Starter | $20 | 600 min | ~10 hrs | 1 service/week with buffer |
| Growth | $40 | 1,800 min | ~30 hrs | 2–3 services/week |
| Premium | $60 | Unlimited (0) | — | Large / multi-campus |

> `0` means unlimited (same convention as existing `maxServiceKeys=0` for Premium).

---

## 4. User Stories

| As a... | I want to... | So that... |
|---|---|---|
| Org owner | Upgrade from Starter to Growth immediately | I get more broadcast time right now without waiting |
| Org owner | Downgrade from Growth to Starter at period end | I don't pay for capacity I no longer need, without losing what I paid for |
| Org owner | See a clear confirmation before changing plans | I understand the cost and timing implications |
| Org owner | Continue broadcasting past my monthly limit | My service is never cut off mid-sermon |
| Org admin | Receive a notification when approaching the monthly minute limit | I know to budget time or upgrade before a big event |
| Super admin | See each org's monthly minute usage | I can monitor infrastructure cost exposure |

---

## 5. Scope

### In Scope

**Backend — `backend/app/billing/models.py`**
- Add `monthly_minutes` field to `PlanSpec` (600 / 1800 / 0 for unlimited)
- Update `PLAN_SPECS` with new values

**Backend — `backend/app/billing/stripe_client.py`**
- `update_subscription(subscription_id, new_price_id, subscription_item_id, proration_behavior)` — for upgrades
- `create_subscription_schedule(subscription_id, new_price_id, phase_end_date)` — for downgrades

**Backend — `backend/app/routes/billing.py`**
- `POST /billing/change-plan` — new endpoint
  - Auth: `owner` or `admin` role required
  - Validates: not same plan, not trial/canceled (use checkout-session for those)
  - Upgrade path: `update_subscription()` → `proration_behavior=create_prorations`
  - Downgrade path: `create_subscription_schedule()` → phase 1 = current plan until `current_period_end`, phase 2 = target plan
  - Stores `pendingPlanKey` + `pendingPlanDate` in Firestore for UI display
- Update webhook handler: clear `pendingPlanKey`/`pendingPlanDate` on `customer.subscription.updated` when plan matches pending

**Backend — `backend/app/services/multichurch_store.py`**
- Add `monthlyMinutesUsed` tracking alongside existing `trialSecondsUsed`
- Add `monthlyMinutesLimit` to billing state (derived from plan)
- `get_org_monthly_minutes_remaining(org_id)` helper

**Backend — soft cap enforcement** (in WebSocket / service start logic)
- Paid plans: allow broadcast past limit, log warning, notify admin via email
- Trial: existing hard cap behavior unchanged

**Frontend — `frontend/pages/billing.tsx` or existing billing page**
- Current plan display with upgrade/downgrade buttons
- Plan change confirmation modal:
  - Upgrade: "You'll be charged a prorated amount today. New limits apply immediately."
  - Downgrade: "Your plan changes to [X] on [date]. You'll keep [current] limits until then."
- Usage bar: monthly minutes used / limit (reuse `UsageBar` pattern from billing dashboard)
- Soft cap banner: "You've used 100% of your 600 min this month. Broadcasts continue — upgrade to avoid this."

### Out of Scope
- Removing `maxServiceKeys` from Firestore documents (leave in place, just deprioritize enforcement)
- Stripe promo codes / coupons
- Downgrade service-count blocking (since downgrade is at period end, user retains current limits until transition)
- Annual billing

---

## 6. Technical Approach

### Upgrade Flow

```
User clicks "Upgrade to Growth"
  → POST /billing/change-plan { orgId, targetPlanKey: "growth" }
  → Retrieve current subscription from Stripe (get item ID)
  → stripe_client.update_subscription(sub_id, new_price_id, item_id, proration_behavior="create_prorations")
  → Stripe fires customer.subscription.updated immediately
  → Webhook: _merge_subscription_snapshot() → planKey="growth", limits updated
  → Firestore updated, frontend refreshes billing status
```

### Downgrade Flow

```
User clicks "Downgrade to Starter"
  → POST /billing/change-plan { orgId, targetPlanKey: "starter" }
  → Retrieve current subscription (get current_period_end)
  → stripe_client.create_subscription_schedule(sub_id, new_price_id, phase_end=current_period_end)
  → Stripe schedules phase transition — no immediate subscription.updated event
  → Store pendingPlanKey="starter", pendingPlanDate=current_period_end in Firestore
  → At period end: Stripe fires customer.subscription.updated with new plan
  → Webhook: apply new limits, clear pendingPlanKey/pendingPlanDate
```

### Soft Cap Flow (Paid Plans)

```
WebSocket session accumulates broadcast minutes
  → On each minute tick: check monthlyMinutesUsed vs monthlyMinutesLimit
  → If limit == 0 (Premium): no check
  → If used >= limit AND plan != trial:
      → Log warning, continue broadcast (no cutoff)
      → If first overage in this period: send soft cap email to org admins
  → Trial hard cap behavior unchanged
```

### Request/Response

```
POST /billing/change-plan
{
  "orgId": "abc123",
  "targetPlanKey": "growth"
}

200 OK
{
  "ok": true,
  "effective": "immediate",          // or "2026-04-01T00:00:00Z"
  "pendingPlanKey": null,            // or "starter"
  "pendingPlanDate": null            // or ISO datetime
}

400 same_plan | no_active_subscription | invalid_plan
502 stripe_update_failed | stripe_schedule_failed
```

---

## 7. Affected Files

| File | Change Type | Description |
|---|---|---|
| `backend/app/billing/models.py` | Modify | Add `monthly_minutes` to `PlanSpec`, update `PLAN_SPECS` |
| `backend/app/billing/stripe_client.py` | Modify | Add `update_subscription()`, `create_subscription_schedule()` |
| `backend/app/routes/billing.py` | Modify | Add `POST /billing/change-plan`, update webhook handler |
| `backend/app/services/multichurch_store.py` | Modify | `monthlyMinutesUsed` tracking, `get_org_monthly_minutes_remaining()` |
| `backend/app/main.py` | Modify | Soft cap check in WebSocket broadcast loop |
| `frontend/pages/` (billing page) | Modify | Plan change UI, confirmation modal, usage bar, soft cap banner |

---

## 8. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Stripe Subscription Schedule API more complex than direct update | Medium | Isolate in `stripe_client.py`; test in Stripe test mode before prod |
| Webhook fires immediately for downgrade (Stripe creates schedule.created event, not subscription.updated) | Medium | Only update Firestore plan on `customer.subscription.updated`; ignore `subscription_schedule.*` for now |
| `pendingPlanKey` in Firestore gets stale if schedule is cancelled in Stripe dashboard | Low | On `billing_status` refresh, re-check Stripe subscription for active schedule; clear pending if none found |
| Org has active broadcast when downgrade transition fires | Low | Soft cap is already in play; active sessions are unaffected by Firestore limit update |
| `monthlyMinutesUsed` counter drift (WebSocket crash mid-session) | Medium | Persist usage on session end AND periodically every N minutes; same pattern as `trialSecondsUsed` |

---

## 9. Acceptance Criteria

- [ ] `POST /billing/change-plan` returns 400 when same plan or non-active subscription
- [ ] Upgrade to higher plan: Stripe subscription updated immediately, Firestore `planKey` and `monthlyMinutesLimit` reflect new plan within webhook processing
- [ ] Downgrade to lower plan: `pendingPlanKey` set in Firestore, Stripe schedule created, current limits unchanged until period end
- [ ] At period end: Stripe webhook triggers plan transition, `pendingPlanKey` cleared
- [ ] Paid plans: broadcast continues past monthly minute limit (soft cap, no cutoff)
- [ ] Trial: hard cap behavior unchanged
- [ ] Soft cap email sent once per period when limit first exceeded (no repeat sends)
- [ ] Frontend confirmation modal shows correct messaging for upgrade vs downgrade
- [ ] `npm run lint` passes
- [ ] No regression in existing checkout-session or portal-session flows

---

## 10. Next Steps

```
/pdca design change-plan    ← Detailed design (API contracts, Firestore schema, component specs)
/pdca do change-plan        ← Implementation guide
/pdca analyze change-plan   ← Gap analysis after implementation
```
