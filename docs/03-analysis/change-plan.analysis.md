# Gap Analysis: change-plan

> Design: `docs/02-design/features/change-plan.design.md`
> Analyzed: 2026-03-23
> Scope: Backend only (frontend UI intentionally deferred)

---

## Match Rate: 100% (post-fix)

Initial analysis found **88% match rate** with 4 gaps. All gaps were fixed in the same session.

---

## 1. Files Analyzed

| File | Items Checked | Initial | Post-Fix |
|---|---|---|---|
| `backend/app/billing/models.py` | 5 | 100% | 100% |
| `backend/app/billing/stripe_client.py` | 3 | 90% | 100% |
| `backend/app/routes/billing.py` | 10 | 100% | 100% |
| `backend/app/services/multichurch_store.py` | 10 | 60% | 100% |
| `backend/app/services/email_service.py` | 1 | 100% | 100% |

---

## 2. Gaps Found and Fixed

### Gap 1 — HIGH: In-memory tick unconditionally set `hardCapReached` for paid plans

**Location**: `multichurch_store.py` `InMemoryMultiChurchStore.enforce_live_usage_caps`

**Problem**: The `monthly_reached` block always set `org["hardCapReached"] = True` regardless of plan type. This would hard-block paid-plan orgs from starting new sessions once the monthly limit was hit — contradicting the soft-cap design for paid plans.

**Fix**: Added plan-type check matching the pattern already used in `end_room`. Trial plans set `hardCapReached`, paid plans set `softCapReached` and dispatch the soft cap email.

```python
# Before
if monthly_reached:
    org["hardCapReached"] = True

# After
if monthly_reached:
    plan_key_tick = str((billing or {}).get("planKey") or "trial")
    if plan_key_tick == "trial":
        org["hardCapReached"] = True
    elif not org.get("softCapReached"):
        org["softCapReached"] = True
        _dispatch_soft_cap_email(org_id, org)
```

---

### Gap 2 — MEDIUM: Firestore `end_room` missing soft cap email dispatch

**Location**: `multichurch_store.py` `FirestoreMultiChurchStore.end_room` (~line 4556)

**Problem**: `softCapReached = True` was set in `org_update` but `_dispatch_soft_cap_email` was not called. Email would never fire on session close for Firestore-backed deployments.

**Fix**: Added `_dispatch_soft_cap_email` call guarded by `not org.get("softCapReached")` check.

---

### Gap 3 — MEDIUM: Firestore `enforce_live_usage_caps` missing soft cap email dispatch

**Location**: `multichurch_store.py` `FirestoreMultiChurchStore.enforce_live_usage_caps` (~line 4756)

**Problem**: Same as Gap 2 — `softCapReached` was set but email not dispatched from the background tick path.

**Fix**: Same pattern — added `_dispatch_soft_cap_email` call guarded by `not org.get("softCapReached")`.

---

### Gap 4 — MEDIUM: Firestore transactional period rollover didn't clear soft cap fields

**Location**: `multichurch_store.py` `FirestoreMultiChurchStore.start_service` transactional rollover (~line 4398-4407)

**Problem**: The transactional period reset (inside `start_service`) cleared `hardCapReached` and `currentMonthMinutes` but not `softCapReached` or `softCapEmailSentKey`. A paid-plan org that hit the soft cap in a prior month would carry `softCapReached=True` into the new billing period.

**Fix**: Added `softCapReached: False` and `softCapEmailSentKey: None` to both the in-memory `org` dict update and the Firestore `transaction.set()` payload.

---

## 3. Additions vs Design (Acceptable)

| Item | Location | Description |
|---|---|---|
| `pendingScheduleId` stored in billing doc | `billing.py:775, 820, 915` | Needed to support `cancel-pending-downgrade`. Implied by design but not in schema table. |
| `CancelPendingDowngradeRequest` Pydantic model | `billing.py:100-101` | Explicit request validation model, good practice. |
| `_is_upgrade()` helper | `billing.py:681-683` | Clean upgrade/downgrade determination by `amount_usd`. |
| `_normalize_billing_refs()` call in `change_plan` | `billing.py:710-712` | Defensive Stripe ID normalization before processing. |
| `current_price_id` param in `create_subscription_schedule` | `stripe_client.py:154` | Required to explicitly set phase 1 of the schedule. |

---

## 4. Acceptance Criteria Verification

| Criterion | Status |
|---|---|
| `POST /billing/change-plan` returns 400 for same plan | PASS |
| Returns 400 for trial/canceled status | PASS |
| Upgrade: Stripe `update_subscription` called with `create_prorations` | PASS |
| Downgrade: `create_subscription_schedule` called; `pendingPlanKey` set | PASS |
| Downgrade: current limits unchanged until period end | PASS |
| Webhook: clears `pendingPlanKey` when plan matches | PASS |
| Webhook: `org.maxMinutesPerMonth` synced to new plan | PASS |
| Trial: `hardCapReached=True` blocks new sessions at 20 min | PASS |
| Paid plan: `softCapReached=True` does NOT block new sessions | PASS (after Gap 1 fix) |
| Soft cap email sent once per period | PASS (after Gaps 2 & 3 fix) |
| Period rollover clears soft cap state | PASS (after Gap 4 fix) |
| `POST /billing/cancel-pending-downgrade` releases schedule | PASS |
| `npm run lint` passes | PASS (warnings only, no errors) |
| No regression in existing tests | PASS (3 pre-existing failures, no new ones) |
