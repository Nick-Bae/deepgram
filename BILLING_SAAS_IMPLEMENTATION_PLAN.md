# Billing SaaS Implementation Plan (No-Card Trial + Tiered Plans)

Last updated: March 8, 2026

## 1) Goal

Ship production billing for churches with:

- No-card trial onboarding.
- Tiered monthly plans by service capacity.
- Automatic lifecycle handling (trialing, active, past_due, canceled, grace).
- Hard entitlement enforcement in backend.

## 2) Current State (This Repo)

Already implemented:

- Authenticated org + role system (`owner/admin/host`).
- Start/end service APIs and room lifecycle.
- Minute-based monthly cap enforcement (`currentMonthMinutes`, `hardCapReached`).
- Billing toggles and billing-admin access control.

Important behavior to change:

- `start_service` currently auto-creates unknown `service_key` (can bypass plan limits).

## 3) Target Product Rules

Plans:

- `trial`: 2 services, no card, one-time, 14 or 30 days.
- `starter`: 5 services, $10/month.
- `growth`: 12 services, $20/month.
- `premium`: unlimited services, $50+/month.

Enforcement policy:

- Primary meter: number of active service definitions (`services` docs), not minutes.
- Secondary protection: keep existing minute cap logic as fail-safe.
- Grace period: 3 days for `past_due`.

## 4) Architecture Decision

Use direct Stripe integration in backend (webhooks + Checkout + Portal), with Stripe as billing source-of-truth and Firestore as cached entitlement state.

Reason for this app:

- Existing custom FastAPI/Cloud Run backend already owns access guards.
- Firestore named DB (`worship-translation`) is already used by app.
- Minimizes coupling risk and keeps entitlement logic in one place.

## 5) Firestore Data Model

`organizations/{orgId}` add:

```json
{
  "billing": {
    "version": 1,
    "provider": "stripe",
    "planKey": "trial",
    "status": "trialing",
    "trialEndsAt": "<timestamp|null>",
    "currentPeriodStart": "<timestamp|null>",
    "currentPeriodEnd": "<timestamp|null>",
    "graceEndsAt": "<timestamp|null>",
    "cancelAtPeriodEnd": false,
    "stripeCustomerId": "<string|null>",
    "stripeSubscriptionId": "<string|null>",
    "priceId": "<string|null>",
    "limits": {
      "maxServiceKeys": 2
    },
    "entitlements": {
      "canStartService": true
    },
    "updatedAt": "<timestamp>"
  }
}
```

Keep legacy fields during migration:

- `maxMinutesPerMonth`
- `currentMonthMinutes`
- `hardCapReached`
- `billingLimitsEnabled`

## 6) API Additions

Add new backend routes (`/api/billing`):

1. `POST /api/billing/checkout-session`
- Input: `{ "orgId": "...", "planKey": "starter|growth|premium" }`
- Output: `{ "url": "https://checkout.stripe.com/..." }`

2. `POST /api/billing/portal-session`
- Input: `{ "orgId": "..." }`
- Output: `{ "url": "https://billing.stripe.com/..." }`

3. `POST /api/billing/webhook`
- Stripe signature verified.
- Handles subscription/customer lifecycle events.
- Idempotent by `event.id`.

4. `GET /api/billing/org/{orgId}/status`
- Returns normalized billing + entitlement snapshot for UI.

## 7) Backend Ticket Plan

## Phase A: Foundation (2-3 days)

- [ ] `BILL-001` Add typed billing models/constants (`app/billing/models.py`).
- [ ] `BILL-002` Add env config for Stripe + plan map (`app/billing/config.py`).
- [ ] `BILL-003` Add Stripe client wrapper (`app/billing/stripe_client.py`).
- [ ] `BILL-004` Add org billing read/write helpers in store (`multichurch_store.py`).

Acceptance:

- Can read/write `organization.billing` with defaults.
- No behavior change for existing start/end APIs yet.

## Phase B: Stripe Flow (3-4 days)

- [ ] `BILL-101` Implement `POST /api/billing/checkout-session`.
- [ ] `BILL-102` Implement `POST /api/billing/portal-session`.
- [ ] `BILL-103` Implement `POST /api/billing/webhook` with signature verification.
- [ ] `BILL-104` Add `billingEvents/{eventId}` idempotency store.
- [ ] `BILL-105` Add retry-safe webhook updates (transactional update).

Acceptance:

- Checkout URL generated for valid org + plan.
- Portal opens for org with Stripe customer.
- Webhooks update billing state correctly and idempotently.

## Phase C: Entitlement Guards (2-3 days)

- [ ] `BILL-201` Add `ensure_org_can_start_service(org_id)` in store.
- [ ] `BILL-202` Call entitlement guard inside `start_service`.
- [ ] `BILL-203` Enforce service-key cap in `create_service`.
- [ ] `BILL-204` Remove auto-create behavior from `start_service` for unknown service key.
- [ ] `BILL-205` Add error codes:
  - `subscription_required`
  - `trial_expired`
  - `grace_expired`
  - `plan_limit_reached`

Acceptance:

- Over-limit org cannot create/start extra service keys.
- Past-due beyond grace cannot start service.
- Existing in-cap org behavior unchanged.

## Phase D: Frontend UX (2-3 days)

- [ ] `BILL-301` Add billing status panel in host settings.
- [ ] `BILL-302` Add “Upgrade plan” + “Manage billing” actions.
- [ ] `BILL-303` Add trial/countdown banner in broadcast tab.
- [ ] `BILL-304` Map new backend error codes to user messages.

Acceptance:

- Hosts see plan/trial status.
- Upgrade path works end-to-end via Checkout/Portal.

## Phase E: Migration + Rollout (2 days)

- [ ] `BILL-401` Backfill existing orgs with `billing` defaults.
- [ ] `BILL-402` Add feature flag `BILLING_ENTITLEMENTS_V2`.
- [ ] `BILL-403` Canary rollout for 1 org, then 10%, then 100%.
- [ ] `BILL-404` Add dashboards + alerts (webhook failures, entitlement denies).

Acceptance:

- No downtime.
- Rollback path available via feature flag.

## 8) Stripe Event Mapping (Must Handle)

Required:

- `checkout.session.completed`
- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- `invoice.payment_succeeded`
- `invoice.payment_failed`

Mapping rules:

- `trialing` -> `billing.status = "trialing"`.
- `active` -> `billing.status = "active"`.
- `past_due` -> set `billing.status = "past_due"` and `graceEndsAt = now + 3 days`.
- `canceled|unpaid|incomplete_expired` -> `billing.status = "canceled"` and block starts.

## 9) Guard Semantics (Backend Source of Truth)

`canStartService = true` when:

- Billing status in `{trialing, active}`; OR
- Status `past_due` and `now <= graceEndsAt`.

Also require:

- Service key exists.
- `service_count < limits.maxServiceKeys` (unless unlimited).
- Existing org/role/host auth checks.

## 10) Test Plan

Unit:

- Billing state transitions by Stripe event.
- Entitlement guard matrix by status + grace + limits.
- Service-cap enforcement.

Integration:

- Signup -> trial initialized -> start service works.
- Trial expiry -> blocked -> upgrade -> unblocked.
- Payment failure -> grace window works -> then blocked.

Regression:

- Existing auth, invites, start/end flows still pass.

## 11) Implementation Order In This Codebase

1. Add `app/routes/billing.py` and include router in `app/main.py`.
2. Add billing helpers to both in-memory + Firestore store classes.
3. Update `start_service` and `create_service` guard paths in `multichurch_store.py`.
4. Update frontend host settings page and `frontend/lib/backendAuth.ts`.
5. Add migration script under `backend/scripts/migrate_billing_v1.py`.
6. Add tests under `backend/tests/test_billing_routes.py` and `backend/tests/test_billing_entitlements.py`.

## 12) Environment Variables

Backend:

- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `STRIPE_PRICE_STARTER`
- `STRIPE_PRICE_GROWTH`
- `STRIPE_PRICE_PREMIUM`
- `BILLING_TRIAL_DAYS` (14 or 30)
- `BILLING_GRACE_DAYS` (default 3)
- `BILLING_ENTITLEMENTS_V2` (`0|1`)

Frontend:

- `NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY` (only if needed for client Stripe usage)

## 13) Immediate Next Action

Start Phase A + B first. Do not switch enforcement in production until Phase C tests pass and Phase E canary is complete.
