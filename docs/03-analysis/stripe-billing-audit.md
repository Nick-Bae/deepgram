# Stripe billing audit — 2026-10-29

**Base commit**: `ac32110c7c71dbdedb65111ee25c92a2720ff267` (`origin/main`).
**Audit branch**: `prep/stripe-billing-audit` (worktree at `tmp/stripe-audit/wt`).
**Scope**: Stripe integration — correctness of checkout, subscription lifecycle, plan changes, cancellation, webhook delivery, authorization, enforcement, and billing-state consistency.
**Isolation**: in-memory `InMemoryMultiChurchStore`, `_FakeStripeClient` for all Stripe calls, `BillingConfig` test-fixture (empty `STRIPE_SECRET_KEY` at import, patched to `"sk_test_audit"` per-test). Resend bail: `email_service` short-circuits on missing `RESEND_API_KEY` (logs `email_skipped` warning; no HTTP). No `backend/.env` loading in the tests. No live Stripe credentials, no production Firestore/Redis, no outbound email.

Stripe API version pinned to **`2023-10-16`** (`backend/app/billing/stripe_client.py:20`). Custom thin HTTP client over `httpx`, not the official SDK.

## Test commands and results

Baseline (existing suites):

```
cd backend
backend/venv/bin/python -m pytest tests/test_billing_routes.py tests/test_billing_entitlements.py -q
28 passed in 1.19s
```

Audit reproductions (added `tests/test_billing_stripe_audit.py`):

```
backend/venv/bin/python -m pytest tests/test_billing_stripe_audit.py -v
8 passed in 0.60s
```

Combined (baseline + audit, same session):

```
backend/venv/bin/python -m pytest tests/test_billing_routes.py tests/test_billing_entitlements.py tests/test_billing_stripe_audit.py -q
36 passed in 0.85s
```

Every assertion in `test_billing_stripe_audit.py` is a positive reproduction of the behavior described below. No tests were weakened or skipped.

---

## 1. Confirmed defects — ranked by customer impact

### H1 — Second checkout session is NOT blocked when an active subscription already exists

**Impact**: HIGH — a church with an active subscription can start a second checkout for a different plan. If they complete it, Stripe creates a SECOND concurrent subscription on the same customer. The `checkout.session.completed` webhook overwrites `stripeSubscriptionId` with the new one; the first subscription is orphaned and keeps billing until the customer notices and cancels through the portal. The church is double-charged.

- **File / line**: `backend/app/routes/billing.py:581` (`create_checkout_session`). No check against `billing.get("status")` / `billing.get("stripeSubscriptionId")` before calling `client.create_checkout_session`.
- **Reproduction**: `test_billing_stripe_audit.py::H1_DuplicateSubscriptionNotPrevented`.
- **Expected**: when `billing.status in {"active","past_due","trialing"}` AND `billing.stripeSubscriptionId` is set, the endpoint should refuse with 409 or redirect to the billing portal — plan changes belong in `change_plan` or the portal, not a new checkout.
- **Actual**: endpoint proceeds and returns a new checkout URL.
- **Proposed fix** (not applied): in `create_checkout_session`, before building the Stripe request, add:
  ```python
  current_status = _clean(billing.get("status")).lower()
  existing_sub = _clean_subscription_id(billing.get("stripeSubscriptionId"))
  if existing_sub and current_status in {"active","past_due","trialing"}:
      raise HTTPException(status_code=409, detail="subscription_already_active")
  ```
  The frontend's "Change plan" button is already wired to `change_plan`, so the UX path is intact.

### H2 — `create_checkout_session` HTTP call does NOT pass an `idempotency_key`

**Impact**: HIGH — a determined double-click or a lost tab reload can create two Stripe checkout sessions for the same intent. Each is independently completable; if both complete, same shape as H1.

- **File / line**: `backend/app/billing/stripe_client.py:113` — `create_checkout_session` returns `self._request("POST", "/checkout/sessions", data=payload)` with no `idempotency_key=`. Compare `create_customer` at `line 84` which DOES pass one.
- **Reproduction**: `test_billing_stripe_audit.py::H2_ChexkoutIdempotencyMissing` (confirms no `idempotency_key` kwarg reaches the HTTP layer).
- **Expected**: a stable idempotency key per `(orgId, planKey, interval)` within a short window so Stripe collapses duplicate POSTs.
- **Actual**: no key; each call is a fresh checkout session.
- **Proposed fix** (not applied):
  ```python
  # routes/billing.py — compute a key from the request shape:
  idempotency_key = f"checkout-{body.orgId}-{target_plan.key}-{body.interval}-{int(_utcnow().timestamp()) // 60}"
  session = client.create_checkout_session(..., idempotency_key=idempotency_key)
  # billing/stripe_client.py — thread it through:
  def create_checkout_session(self, *, ..., idempotency_key: Optional[str] = None) -> Dict[str, Any]:
      ...
      return self._request("POST", "/checkout/sessions", data=payload, idempotency_key=idempotency_key)
  ```
  The minute-bucket in the key gives one checkout per plan+interval per minute; adjust as the operator wishes.

### H3 — Portal-driven plan change leaves stored `planKey` STALE

**Impact**: HIGH — when a customer changes plan via the Stripe Customer Portal (the officially intended path for self-serve plan changes), the webhook `customer.subscription.updated` resolves the new plan correctly from the price_id in `_merge_subscription_snapshot` — but the webhook handler **immediately overrides it** with `metadata.get("planKey") or next_billing.get("planKey")` at `routes/billing.py:963`. Because `subscription_data[metadata][planKey]` was set at the ORIGINAL checkout and Stripe does NOT update subscription metadata when the price changes, `metadata.planKey` is the ORIGINAL plan — which wins. The stored state becomes self-inconsistent: `priceId` reflects the new plan, `planKey` reflects the old, `limits.maxServiceKeys` follows the stale `planKey`.

- **File / line**: `backend/app/routes/billing.py:963` (`stripe_webhook` → `customer.subscription.*` branch). `_merge_subscription_snapshot` at `backend/app/routes/billing.py:304` correctly resolves `planKey` from `_resolve_plan_from_price_id(price_id, fallback=billing.planKey)`; the override at line 963 defeats this.
- **Reproduction**: `test_billing_stripe_audit.py::H3_PortalPlanChangeStalePlanKey` — asserts `planKey=starter` + `priceId=price_growth` + `maxServiceKeys=5` after an upgrade from Starter → Growth via Portal.
- **Expected**: `planKey=growth` + `priceId=price_growth` + `maxServiceKeys=12`.
- **Actual**: `planKey=starter` + `priceId=price_growth` + `maxServiceKeys=5`.
- **Business consequences**:
  - A Starter→Growth upgrader is under-provisioned: `maxServiceKeys` stays at 5 instead of 12.
  - A Growth→Starter downgrader keeps `maxServiceKeys=12` after the downgrade (over-provisioned, lost revenue).
  - `monthly_minutes` follows `planKey` (plan_spec), so monthly usage limits are also wrong.
  - Correct state is restored only on a `?refresh=true` call to `GET /api/billing/org/{org_id}/status` (which calls `_hydrate_billing_from_stripe(force_refresh=True)`, where `_merge_subscription_snapshot` runs WITHOUT the webhook's line-963 override and the correct `planKey` is written). The UI calls this on portal open, not on arrival from Stripe.
- **Proposed fix** (not applied): remove the override at line 963 entirely. `_merge_subscription_snapshot` already does the right thing. Specifically:
  ```python
  # routes/billing.py:962-966 CURRENT:
  stripe_status = _clean(next_billing.get("status")).lower()
  requested_plan = plan_spec(str(metadata.get("planKey") or next_billing.get("planKey") or "trial"))
  next_billing["planKey"] = requested_plan.key
  next_billing.setdefault("limits", {})
  next_billing["limits"]["maxServiceKeys"] = requested_plan.max_service_keys

  # Proposed — trust _merge_subscription_snapshot's price-id-resolved plan:
  stripe_status = _clean(next_billing.get("status")).lower()
  requested_plan = plan_spec(str(next_billing.get("planKey") or "trial"))
  # (lines below unchanged — set_org_monthly_minutes_limit still fires)
  ```
  Keep `metadata.planKey` as a fallback ONLY when no price could be resolved, which `_resolve_plan_from_price_id` already handles via its `fallback_plan_key=next_billing.get("planKey")` argument.

### H4 — v2-off path violates the grace-period policy

**Impact**: HIGH (depends on production configuration) — a `past_due` subscription within its documented grace window is BLOCKED from starting services under `BILLING_ENTITLEMENTS_V2=false` (the production default per `backend/app/billing/config.py:39`). The `_billing_start_denial_reason` logic in `multichurch_store.py:605-607` correctly honors `graceEndsAt`, but `_set_org_billing_profile` at `multichurch_store.py:1493-1495` mirrors `billing.status → org.status`, and the legacy fallback `_legacy_org_start_allowed(org)` at `multichurch_store.py:395-396` rejects anything other than `{"active","trial"}`. Grace-period traffic is denied with `org_inactive`.

- **File / line**: `backend/app/services/multichurch_store.py:1493-1495, 395-396, 2722, 1544`. The guard chain in `start_service` and `ensure_org_can_start_service` has the legacy path as the `elif` branch for when v2 is off; the legacy path doesn't know about `graceEndsAt`.
- **Reproduction**: pair of tests
  - `H4_V2OffViolatesGracePeriod::test_past_due_within_grace_is_blocked_under_v2_off` — denies with `org_inactive`.
  - `H4b_V2OnRespectsGracePeriod::test_past_due_within_grace_allows_start_under_v2_on` — allows start.
- **Expected business behavior**: within the configured `BILLING_GRACE_DAYS` window (default 3), a past-due church should still be able to broadcast; this is what gives customers time to update their card.
- **Actual (v2 off)**: service start denied immediately upon transition to `past_due`.
- **Actual (v2 on)**: honored correctly.
- **Related secondary gap under v2-off**: `plan_limit_reached` (service-key cap) is ONLY wired on the v2-on branch at `multichurch_store.py:1228-1231` — under v2-off, churches could create more services than the plan allows via the `start_service` creation path.
- **Policy question for the operator**: is `BILLING_ENTITLEMENTS_V2` intended to be ON in production? If yes, confirming the env var is set on the serving Cloud Run revision is the first fix. If no, the grace-period semantics need to be re-wired into the legacy path (or the legacy path removed).

---

## 2. Confirmed medium-severity risks

### M1 — `change_plan` does not cancel a prior pending schedule before creating a new one

**Impact**: MEDIUM — repeated downgrade attempts create orphan subscription schedules on Stripe. The local billing profile only tracks the most recent `pendingScheduleId`; prior schedules remain active on Stripe and can fire unexpected plan transitions.

- **File / line**: `backend/app/routes/billing.py:815-836` (`change_plan`, downgrade branch).
- **Reproduction**: `test_billing_stripe_audit.py::M1_ChangePlanDoesNotCancelPendingSchedule` — two calls to `change_plan` create schedules `sched_1` and `sched_2`; no `cancel_subscription_schedule` call is made.
- **Proposed fix** (not applied): at the top of the downgrade branch, if `billing.get("pendingScheduleId")` is set, call `client.cancel_subscription_schedule(schedule_id=<prior>)` first, then proceed.

### M3 — `checkout.session.completed` sets `maxServiceKeys` but does NOT sync `maxMinutesPerMonth`

**Impact**: MEDIUM — on completion of a paid-plan checkout, the service-key cap updates immediately but the monthly-minutes cap stays at its prior value until the subsequent `customer.subscription.*` event lands. If that event is lost or delayed, the org operates with inconsistent limits (new plan key, new service-key cap, old minutes cap).

- **File / line**: `backend/app/routes/billing.py:942-954` (`checkout.session.completed` branch) — writes `limits.maxServiceKeys` but never calls `set_org_monthly_minutes_limit`. The sync happens at `backend/app/routes/billing.py:976-982` which is inside the `customer.subscription.*` branch only.
- **Reproduction**: `test_billing_stripe_audit.py::M3_CheckoutCompletedDoesNotSyncMonthlyMinutes` — after `checkout.session.completed` for Growth, `maxServiceKeys=12` (correct) but `org.maxMinutesPerMonth` remains at the bootstrap default (500) rather than `1800` (Growth plan).
- **Proposed fix** (not applied): add a `set_org_monthly_minutes_limit(org_id=org_id, monthly_minutes=requested_plan.monthly_minutes)` call in the `checkout.session.completed` branch, matching the `customer.subscription.*` branch at line 977.

### M4 — Out-of-order webhook: `customer.subscription.created` before `checkout.session.completed` is silently lost

**Impact**: MEDIUM — Stripe does not guarantee webhook ordering. If `customer.subscription.created` arrives before `checkout.session.completed`, `_resolve_org_id_from_event` cannot match (no `orgId` metadata on bare subscription objects; no `stripeCustomerId` stored on any org yet), so the handler returns `{"unmatched": true, "ok": true}`. The webhook idempotency store (`try_mark_billing_event`) has already marked the event as processed, so Stripe's retry (if any) is deduped. The subscription snapshot (`currentPeriodStart/End`, status, `cancelAtPeriodEnd`) is lost until a FUTURE `customer.subscription.updated` or `invoice.payment_succeeded` arrives.

- **File / line**: `backend/app/routes/billing.py:909-925` (webhook idempotency ⨯ org-resolution ordering). The `try_mark_billing_event` call at line 910 fires BEFORE the org-resolution attempt at line 923, so a successful dedup write happens even if the org can't be found.
- **Reproduction**: `test_billing_stripe_audit.py::M4_OutOfOrderSubscriptionBeforeCheckout` — delivers `customer.subscription.created` first, confirms `unmatched=true`, then confirms a redelivery of the same event is `deduped=true` (not retried for real), then confirms the subsequent `checkout.session.completed` sets `stripeCustomerId`/`stripeSubscriptionId` but leaves `currentPeriodStart/End` as `None`.
- **Proposed fix** (not applied, two options):
  - Option A (preferred): defer `try_mark_billing_event` until AFTER org resolution succeeds — move it below the `_resolve_org_id_from_event` call. Unmatched events return `unmatched=true` as today but are NOT recorded as processed, so Stripe's retry can succeed later once state has caught up.
  - Option B: on a later `checkout.session.completed` or `invoice.payment_succeeded`, call `_hydrate_billing_from_stripe(force_refresh=True)` as part of the handler so the subscription snapshot is backfilled from the Stripe API.

---

## 3. Suspected risks (not individually asserted but worth operator review)

- **S1 — `_lookup_customer_subscription` picks "best" subscription when multiple exist**: `backend/app/routes/billing.py:388-394` lists up to 10 subscriptions and sorts by `_subscription_sort_key` (status rank, then `-current_period_end`, then `-created_at`). If H1 or H2 above let two concurrent active subscriptions exist, the "best" pick is arbitrary. Not a defect in isolation; downstream of H1/H2.
- **S2 — `_subscription_sort_key` tie-break**: for two `active` subscriptions with identical periods, `-created_at` picks the OLDER one. Depending on intent this may want to be the newer one. Confirm with operator.
- **S3 — Payment-recovered email only fires on `invoice.payment_succeeded` → `active` transition** (`routes/billing.py:1003-1008`). If recovery is reflected via a `customer.subscription.updated` with `status=active` directly (e.g. operator manually fixes in Stripe dashboard), no recovery email is sent.
- **S4 — Webhook signature tolerance window**: default 60 s, configurable 30–300 (`routes/billing.py:81-83`). Cloud Run cold-starts under load have been observed in the 5–15 s range; well within tolerance. No action.
- **S5 — Trial is local-only, not Stripe-managed**: `create_checkout_session` always passes `trial_days=0` (`routes/billing.py:629,649`); the 30-day local trial lives in `multichurch_store` + `BILLING_CONFIG.trial_days`. Stripe treats the paid subscription as immediate. Design decision; documented in code; not a defect.
- **S6 — Changing plans from `past_due` is allowed** by `change_plan` (`routes/billing.py:770`: `if current_status not in {"active", "past_due"}: raise ...`). This matches typical Stripe UX (upgrade to settle the balance), but confirm business policy.

## 4. Missing coverage

- No existing test asserts cross-org isolation of billing-sensitive endpoints (one org's admin trying to access another org's billing status / checkout / portal / webhook-matched state). The `require_org_role` dependency makes this unlikely to be exploitable, but no test proves it. Documented as a gap; not reproduced here (operator correction #4 of the earlier Redis prep: add tests only for demonstrated gaps — this one is not demonstrated as exploitable, just unverified).
- No existing test exercises `invoice.payment_failed` → grace-expiry → `customer.subscription.deleted` lifecycle end-to-end on a single org. Audit relied on the per-event tests in `test_billing_routes.py` which cover each event in isolation.
- No tests for duplicate / concurrent webhook delivery arriving within the same request (true concurrency); the dedup store serializes per-event by `event_id`, which is Stripe-unique, so true collisions are rare — but no test proves the lock scope.
- No tests for webhook event types this audit did NOT look at: `customer.updated`, `customer.deleted`, `invoice.upcoming`, `invoice.finalized`, `payment_intent.*`, `charge.*`, `charge.dispute.*`. If any of these are subscribed in the Stripe dashboard but not handled in `routes/billing.py`, they are silently ignored (the `event_type` not matched falls through to the `_resolve_org_id_from_event`/profile update path but writes `updatedAt` only). Suggest a lightweight sanity test to confirm no unexpected mutation on an unhandled event type.

## 5. Business-rule / policy questions

1. **`BILLING_ENTITLEMENTS_V2`** — is this intended to be enabled in production? The default is OFF, which has the H4 grace-period consequence. The audit cannot answer this; needs operator decision.
2. **Duplicate-subscription policy (H1)** — should the second checkout be refused with 409, redirected to the portal, or allowed as explicit "replacement" (requires more surgery to cancel the prior)? Needs operator decision.
3. **Idempotency window (H2)** — the proposed per-minute bucket is a guess. If an operator wants a different window (e.g. per 5-minute bucket, or per `successUrl` to key by browser session), confirm first.
4. **Portal metadata update (H3)** — do we also want to WRITE `subscription.metadata.planKey` on upgrade/downgrade via the backend so the subscription's own metadata stays accurate? This is a separate hygiene fix; the price-id-resolved plan is still authoritative.
5. **Change-plan from `past_due` (S6)** — allowed today. Confirm the business policy.
6. **Yearly intervals** — `stripe_price_ids_annual` is wired through `create_checkout_session` but NOT through `change_plan` (`routes/billing.py:751` only reads `BILLING_CONFIG.stripe_price_ids`, not `_annual`). A monthly-to-annual change via `change_plan` would silently route to the monthly price. Likely a defect; the operator confirms whether annual plans are supported today.

---

## 6. Separation: mocks vs. sandbox

Every finding above is reproduced with mocks (in-memory store + `_FakeStripeClient`). The CLAIMS that still require Stripe sandbox validation before being treated as production facts:

- **H1 shape under Stripe reality**: whether Stripe actually creates two concurrent subscriptions on the same customer when two checkout sessions are completed back-to-back for different prices, or whether Stripe deduplicates / warns. The reproduction here only demonstrates that OUR CODE does not block the second checkout. The Stripe-side consequence is strongly indicated by the Stripe docs but not independently confirmed in this audit.
- **H2 under Stripe reality**: whether two near-concurrent POSTs to `/v1/checkout/sessions` without an idempotency key produce two sessions or whether Stripe's internal deduplication catches them. Stripe's documentation says duplicate detection requires idempotency keys — but confirm.
- **H3 under Stripe reality**: whether Stripe's Customer Portal ACTUALLY leaves subscription `metadata.planKey` unchanged after a plan change via the UI, or whether it preserves/removes/updates it. The reproduction above assumes "preserved as stale"; if Stripe clears subscription metadata on portal-driven plan changes, the webhook override would silently fall through to `next_billing.get("planKey")` (the already-correct value) and the bug would be benign in production. Confirm by changing a plan via the Stripe test-mode portal and inspecting the `customer.subscription.updated` event payload.
- **M4 under Stripe reality**: whether Stripe actually retries webhooks returning 200 (it does not), and the exact ordering frequency of `customer.subscription.created` vs. `checkout.session.completed` under normal conditions.

**Minimum sandbox scenarios to add after this audit (operator-approved separately):**

1. One Stripe test-mode checkout for Starter → observe the sequence of webhooks delivered to a local backend (confirm M4 ordering and whether `subscription.created` precedes `checkout.session.completed`).
2. One Stripe test-mode plan change via the Customer Portal → observe `customer.subscription.updated` event payload, confirm whether `metadata.planKey` is stale, cleared, or updated. This single scenario validates H3.
3. One Stripe test-mode checkout with a double-POST → observe whether Stripe creates two sessions on the same `(customer, price)` without an idempotency key. This validates H2.
4. Trigger `invoice.payment_failed` on a Stripe test-mode subscription via a decline-card → confirm grace timer and recovery path end-to-end.

None of these require live customer traffic; all are Stripe-sandbox + a local backend with a public webhook forwarder (e.g. `stripe listen --forward-to localhost`).

---

## 7. Not performed by this audit
No production write, no live Stripe call, no outbound email, no push, no PR publication, no merge, no deployment, no scaling change, no provisioning. All production code changes above are PROPOSALS; the only code change in the audit worktree is the new test file `backend/tests/test_billing_stripe_audit.py` and this document. The frozen Redis preparation (`prep/max-instances-two@5acf81a3`, `prep/fanout-correlation-logs@e005861e`) is untouched.
