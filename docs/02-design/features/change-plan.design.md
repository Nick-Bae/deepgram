# Design: change-plan

> Refs: `docs/01-plan/features/change-plan.plan.md`
> Phase: Design
> Updated: 2026-03-23

---

## 1. Overview

This document covers the detailed design for subscription plan change (upgrade/downgrade)
and the migration from service-count limits to monthly broadcast-minute limits.

**Key discovery**: The monthly minute tracking infrastructure already exists on the org document:
- `org.maxMinutesPerMonth` — limit (currently set manually via admin API)
- `org.currentMonthMinutes` — rolling counter (incremented by session close + tick)
- `org.currentMonthKey` — YYYYMM period key (resets counter on new month)
- `org.hardCapReached` — boolean that blocks new session starts

The new work is: (1) link `maxMinutesPerMonth` to the plan spec, (2) change hard-cap to
soft-cap for paid plans, (3) add plan change API with Stripe upgrade/downgrade flows.

---

## 2. Architecture

### 2.1 Data Flow — Upgrade

```
POST /billing/change-plan { orgId, targetPlanKey: "growth" }
  │
  ├─ Validate: active subscription, different plan, owner/admin role
  ├─ Retrieve Stripe subscription → get subscription item ID
  ├─ stripe_client.update_subscription(sub_id, item_id, new_price_id,
  │    proration_behavior="create_prorations")
  │
  ├─ Stripe fires customer.subscription.updated immediately
  └─ Webhook: _merge_subscription_snapshot() → planKey="growth"
       └─ also: org.maxMinutesPerMonth updated to 1800
```

### 2.2 Data Flow — Downgrade

```
POST /billing/change-plan { orgId, targetPlanKey: "starter" }
  │
  ├─ Validate: active subscription, different plan, owner/admin role
  ├─ Retrieve Stripe subscription → get item ID + current_period_end
  ├─ stripe_client.create_subscription_schedule(
  │    from_subscription=sub_id,
  │    phase_end_date=current_period_end,   ← phase 1: keep current plan
  │    new_price_id=starter_price_id        ← phase 2: starter at period end
  │  )
  │
  ├─ Store pendingPlanKey="starter", pendingPlanDate=current_period_end in Firestore billing
  ├─ Return 200 { effective: "2026-04-01T...", pendingPlanKey: "starter" }
  │
  └─ At period end: Stripe fires customer.subscription.updated
       └─ Webhook: apply new plan limits + clear pendingPlanKey/pendingPlanDate
            └─ also: org.maxMinutesPerMonth updated to 600
```

### 2.3 Data Flow — Soft Cap (Paid Plans)

```
WebSocket tick / session close
  ├─ currentMonthMinutes += delta
  ├─ If currentMonthMinutes >= maxMinutesPerMonth AND plan != "trial":
  │    ├─ Do NOT set hardCapReached = True     ← key change from existing behavior
  │    ├─ Set softCapReached = True
  │    └─ If first overage this period: send soft cap email to org admins
  └─ If plan == "trial": existing hard cap behavior unchanged
```

---

## 3. Backend Design

### 3.1 `billing/models.py` — Add `monthly_minutes` to `PlanSpec`

```python
@dataclass(frozen=True)
class PlanSpec:
    key: str
    max_service_keys: int   # kept for reference, no longer enforced
    amount_usd: int
    monthly_minutes: int    # NEW: 0 = unlimited

PLAN_SPECS: Dict[str, PlanSpec] = {
    "trial":   PlanSpec(key="trial",   max_service_keys=2,  amount_usd=0,  monthly_minutes=20),
    "starter": PlanSpec(key="starter", max_service_keys=5,  amount_usd=20, monthly_minutes=600),
    "growth":  PlanSpec(key="growth",  max_service_keys=12, amount_usd=40, monthly_minutes=1800),
    "premium": PlanSpec(key="premium", max_service_keys=0,  amount_usd=60, monthly_minutes=0),
}
```

### 3.2 `billing/stripe_client.py` — Two new methods

#### `update_subscription()`
Used for upgrades. Swaps the price on the existing subscription item immediately.

```python
def update_subscription(
    self,
    *,
    subscription_id: str,
    subscription_item_id: str,
    new_price_id: str,
    proration_behavior: str = "create_prorations",  # "create_prorations" | "none"
) -> Dict[str, Any]:
    """
    PATCH /subscriptions/{subscription_id}
    items[0][id]    = subscription_item_id
    items[0][price] = new_price_id
    proration_behavior = proration_behavior
    """
```

#### `create_subscription_schedule()`
Used for downgrades. Wraps the existing subscription in a schedule with two phases.

```python
def create_subscription_schedule(
    self,
    *,
    subscription_id: str,
    new_price_id: str,
    phase_end_unix: int,   # current_period_end as Unix timestamp
) -> Dict[str, Any]:
    """
    POST /subscription_schedules
    from_subscription = subscription_id

    Then PATCH /subscription_schedules/{schedule_id}
    phases[0][items][0][price] = current_price_id   (phase 1: keep current plan)
    phases[0][end_date]        = phase_end_unix
    phases[1][items][0][price] = new_price_id        (phase 2: new plan, open-ended)
    """
```

> Note: `create_subscription_schedule` with `from_subscription` inherits the current phase
> automatically. We then update the schedule to append phase 2 (new plan after period end).

### 3.3 `routes/billing.py` — New endpoint `POST /billing/change-plan`

#### Request model

```python
class ChangePlanRequest(BaseModel):
    orgId: str = Field(..., min_length=2, max_length=120, pattern=validators.ORG_ID)
    targetPlanKey: str = Field(..., min_length=3, max_length=32, pattern=validators.PLAN_KEY)
```

#### Validation logic

```
1. require_org_role: owner or admin
2. target_plan = plan_spec(targetPlanKey)  → 400 "invalid_plan" if trial
3. current billing = get_org_billing_profile(orgId)
4. current status must be "active" or "past_due"  → 400 "no_active_subscription" otherwise
5. current planKey == targetPlanKey  → 400 "same_plan"
6. price_id = BILLING_CONFIG.stripe_price_ids[targetPlanKey]  → 503 if missing
7. subscription_id from billing  → 400 "no_active_subscription" if empty
```

#### Upgrade path (`target.amount_usd > current.amount_usd`)

```python
# Retrieve subscription to get item_id
subscription = client.retrieve_subscription(subscription_id)
item_id = _first_subscription_item(subscription)["id"]

# Update Stripe immediately
client.update_subscription(
    subscription_id=subscription_id,
    subscription_item_id=item_id,
    new_price_id=new_price_id,
    proration_behavior="create_prorations",
)

# Firestore will be updated by webhook customer.subscription.updated
return {"ok": True, "effective": "immediate", "pendingPlanKey": None, "pendingPlanDate": None}
```

#### Downgrade path (`target.amount_usd < current.amount_usd`)

```python
# Retrieve subscription to get item_id + period end
subscription = client.retrieve_subscription(subscription_id)
item_id = _first_subscription_item(subscription)["id"]
period_end_unix = subscription.get("current_period_end")
period_end_dt = _to_datetime(period_end_unix)

# Create Stripe schedule
client.create_subscription_schedule(
    subscription_id=subscription_id,
    new_price_id=new_price_id,
    phase_end_unix=int(period_end_unix),
)

# Update Firestore with pending state
billing["pendingPlanKey"] = target_plan.key
billing["pendingPlanDate"] = period_end_dt
multichurch_store.set_org_billing_profile(orgId, billing)

return {
    "ok": True,
    "effective": period_end_dt.isoformat(),
    "pendingPlanKey": target_plan.key,
    "pendingPlanDate": period_end_dt.isoformat(),
}
```

#### Response schema

```json
{
  "ok": true,
  "effective": "immediate" | "2026-04-01T00:00:00Z",
  "pendingPlanKey": null | "starter",
  "pendingPlanDate": null | "2026-04-01T00:00:00Z"
}
```

#### Error codes

| HTTP | detail | Condition |
|---|---|---|
| 400 | `invalid_plan` | targetPlanKey is "trial" or unknown |
| 400 | `same_plan` | targetPlanKey equals current planKey |
| 400 | `no_active_subscription` | billing status is trial/canceled/incomplete, or no sub ID |
| 403 | `forbidden` | caller is not owner/admin of the org |
| 404 | `org_not_found` | orgId doesn't exist |
| 503 | `billing_not_configured` | price_id missing for target plan |
| 502 | `stripe_update_failed` | Stripe API error on upgrade |
| 502 | `stripe_schedule_failed` | Stripe API error on downgrade schedule |

### 3.4 `routes/billing.py` — Webhook update

In the `customer.subscription.updated` handler (line 740), after `_merge_subscription_snapshot`:

```python
# Clear pending downgrade if the active plan now matches
pending_plan = _clean(next_billing.get("pendingPlanKey"))
if pending_plan and next_billing.get("planKey") == pending_plan:
    next_billing["pendingPlanKey"] = None
    next_billing["pendingPlanDate"] = None
```

Also: when billing plan changes via webhook, sync `org.maxMinutesPerMonth`:

```python
# After updating billing, sync the org-level minute limit
new_plan = plan_spec(next_billing.get("planKey") or "trial")
multichurch_store.set_org_monthly_minutes_limit(
    org_id=org_id,
    monthly_minutes=new_plan.monthly_minutes,
)
```

### 3.5 `services/multichurch_store.py` — New helper + soft cap change

#### New helper: `set_org_monthly_minutes_limit()`

```python
def set_org_monthly_minutes_limit(self, *, org_id: str, monthly_minutes: int) -> None:
    """Update maxMinutesPerMonth on the org document to match the plan spec."""
    # Sets org.maxMinutesPerMonth = monthly_minutes
    # If monthly_minutes == 0 (Premium): sets maxMinutesPerMonth = 0
    # Also clears softCapReached if limit increased
```

#### Soft cap change — paid plans do not set `hardCapReached`

In the two places where `hardCapReached = True` is set (session close ~line 2291 and tick ~line 4472):

```python
# BEFORE (existing):
if next_month_minutes >= int(org.get("maxMinutesPerMonth") or 0):
    org_update["hardCapReached"] = True

# AFTER:
if next_month_minutes >= int(org.get("maxMinutesPerMonth") or 0):
    plan_key = str((org.get("billing") or {}).get("planKey") or "trial")
    if plan_key == "trial":
        org_update["hardCapReached"] = True   # trial: hard cap (blocks new sessions)
    else:
        org_update["softCapReached"] = True   # paid: soft cap (notify only)
        # Send soft cap email if not already sent this period
        if not org.get("softCapEmailSentKey") or org.get("softCapEmailSentKey") != current_month_key:
            org_update["softCapEmailSentKey"] = current_month_key
            _enqueue_soft_cap_email(org_id)   # fire-and-forget
```

New org fields:
- `softCapReached: bool` — cleared on `_roll_billing_period_if_needed()`
- `softCapEmailSentKey: str` — YYYYMM of last soft cap email; prevents repeat sends

#### `_roll_billing_period_if_needed()` — clear new fields on month rollover

```python
update = {
    "currentMonthKey": current_key,
    "currentMonthMinutes": 0,
    "hardCapReached": False,
    "softCapReached": False,      # NEW
    "softCapEmailSentKey": None,  # NEW
    ...
}
```

### 3.6 Soft cap email

Add to `services/email_service.py`:

```python
def send_soft_cap_reached_email(
    *,
    admin_emails: List[str],
    org_name: str,
    minutes_used: int,
    minutes_limit: int,
    plan_key: str,
) -> None:
    """
    Subject: "You've reached your monthly broadcast limit — [OrgName]"
    Body: usage summary, current plan, upgrade CTA
    Sent once per billing period via softCapEmailSentKey guard.
    """
```

Add dispatch in `billing.py` `_send_billing_email`:

```python
elif event == "soft_cap_reached":
    email_service.send_soft_cap_reached_email(
        admin_emails=admin_emails, org_name=org_name,
        minutes_used=..., minutes_limit=..., plan_key=plan_key
    )
```

---

## 4. Firestore Schema Changes

### Org document (`organizations/{orgId}`)

| Field | Type | Change | Notes |
|---|---|---|---|
| `maxMinutesPerMonth` | int | **existing** | Now driven by plan spec, not admin-set |
| `currentMonthMinutes` | int | **existing** | No change |
| `currentMonthKey` | str | **existing** | No change |
| `hardCapReached` | bool | **modified** | Only set for trial plan now |
| `softCapReached` | bool | **NEW** | Set when paid plan hits minute limit |
| `softCapEmailSentKey` | str | **NEW** | YYYYMM; prevents repeat soft cap emails |

### Billing sub-document (`organizations/{orgId}.billing`)

| Field | Type | Change | Notes |
|---|---|---|---|
| `pendingPlanKey` | str\|null | **NEW** | Target plan key for scheduled downgrade |
| `pendingPlanDate` | datetime\|null | **NEW** | When downgrade takes effect (period end) |

---

## 5. Frontend Design

### 5.1 API call

```typescript
// POST /api/billing/change-plan
async function changePlan(orgId: string, targetPlanKey: string): Promise<ChangePlanResult> {
  const res = await apiFetch("/billing/change-plan", {
    method: "POST",
    body: { orgId, targetPlanKey },
  });
  return res.json();
}

type ChangePlanResult = {
  ok: boolean;
  effective: "immediate" | string;   // ISO date string for downgrade
  pendingPlanKey: string | null;
  pendingPlanDate: string | null;
};
```

### 5.2 Plan Change Confirmation Modal

Triggered when user clicks an upgrade or downgrade button.

**Upgrade modal copy:**
```
Upgrade to [Growth]
──────────────────────────────────────
You'll be charged a prorated amount today for the
remainder of your current billing period.

New limits apply immediately:
  • 1,800 minutes/month (was 600)

[Cancel]  [Confirm Upgrade →]
```

**Downgrade modal copy:**
```
Downgrade to [Starter]
──────────────────────────────────────
Your plan changes on [April 1, 2026].
You'll keep your current Growth limits until then.

Starting [April 1, 2026]:
  • 600 minutes/month (currently 1,800)

[Cancel]  [Confirm Downgrade →]
```

### 5.3 Billing page state

```typescript
type BillingState = {
  planKey: string;
  status: string;
  currentPeriodEnd: string | null;
  pendingPlanKey: string | null;
  pendingPlanDate: string | null;
  monthlyMinutesUsed: number;      // = org.currentMonthMinutes
  monthlyMinutesLimit: number;     // = org.maxMinutesPerMonth (0 = unlimited)
  softCapReached: boolean;
};
```

### 5.4 Current plan display

```
┌─ Your Plan ──────────────────────────────┐
│  Growth  •  $40/month                    │
│                                          │
│  Monthly broadcast time                  │
│  [████████░░░░░░░] 1,200 / 1,800 min     │
│                                          │
│  Next billing: April 1, 2026             │
│                                          │
│  [Upgrade to Premium]  [Downgrade ▾]     │
└──────────────────────────────────────────┘
```

If `pendingPlanKey` is set:
```
│  ⚠ Downgrade to Starter scheduled for April 1, 2026  [Cancel downgrade] │
```

If `softCapReached`:
```
│  ⚠ You've reached your 1,800 min limit. Broadcasts continue.  [Upgrade] │
```

### 5.5 Pending downgrade cancellation

Add `POST /billing/cancel-pending-downgrade`:
- Cancels the Stripe subscription schedule (releases the schedule, restores normal subscription)
- Clears `pendingPlanKey` / `pendingPlanDate` in Firestore

```python
def cancel_subscription_schedule(self, *, schedule_id: str) -> Dict[str, Any]:
    """POST /subscription_schedules/{schedule_id}/release"""
```

> The `scheduleId` must be stored in Firestore when the downgrade is created.
> Add `pendingScheduleId: str | null` to billing document.

---

## 6. Affected Files

| File | Change | Description |
|---|---|---|
| `backend/app/billing/models.py` | Modify | Add `monthly_minutes` to `PlanSpec`, update `PLAN_SPECS` |
| `backend/app/billing/stripe_client.py` | Modify | Add `update_subscription()`, `create_subscription_schedule()`, `cancel_subscription_schedule()` |
| `backend/app/routes/billing.py` | Modify | Add `POST /billing/change-plan`, `POST /billing/cancel-pending-downgrade`, update webhook handler |
| `backend/app/services/multichurch_store.py` | Modify | Add `set_org_monthly_minutes_limit()`, soft cap logic (2 locations), clear new fields on period rollover |
| `backend/app/services/email_service.py` | Modify | Add `send_soft_cap_reached_email()` |
| `frontend/pages/` (billing page) | Modify | Plan change UI, confirmation modal, usage bar, soft cap + pending downgrade banners |

---

## 7. Implementation Order

1. **`billing/models.py`** — add `monthly_minutes` (no side effects, safe first step)
2. **`billing/stripe_client.py`** — add `update_subscription()`, `create_subscription_schedule()`, `cancel_subscription_schedule()`
3. **`routes/billing.py`** — add `POST /billing/change-plan` + `cancel-pending-downgrade`, update webhook
4. **`multichurch_store.py`** — add `set_org_monthly_minutes_limit()`, soft cap logic
5. **`email_service.py`** — add `send_soft_cap_reached_email()`
6. **Frontend** — billing page UI

---

## 8. Testing Checklist

- [ ] `POST /billing/change-plan` with same plan returns 400 `same_plan`
- [ ] `POST /billing/change-plan` with trial/canceled status returns 400 `no_active_subscription`
- [ ] Upgrade: Stripe `update_subscription` called with `proration_behavior=create_prorations`
- [ ] Downgrade: Stripe `create_subscription_schedule` called; `pendingPlanKey` set in Firestore
- [ ] Downgrade: current plan limits unchanged until period end
- [ ] Webhook `customer.subscription.updated`: clears `pendingPlanKey` when plan matches
- [ ] Webhook: `org.maxMinutesPerMonth` synced to new plan's `monthly_minutes`
- [ ] Trial: `hardCapReached=True` still blocks new sessions at 20 min
- [ ] Paid plan: `softCapReached=True` does NOT block new sessions
- [ ] Soft cap email sent once per period; not resent on subsequent ticks
- [ ] Period rollover clears `softCapReached`, `softCapEmailSentKey`, `currentMonthMinutes`
- [ ] `POST /billing/cancel-pending-downgrade` releases Stripe schedule, clears pending fields
- [ ] Frontend confirmation modal shows correct copy for upgrade vs downgrade
- [ ] `npm run lint` passes
