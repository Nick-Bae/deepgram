# Stripe billing audit — 2026-10-29

**Base commit**: `ac32110c7c71dbdedb65111ee25c92a2720ff267` (`origin/main`).
**Audit branch**: `prep/stripe-billing-audit` (worktree at `tmp/stripe-audit/wt`).
**Scope**: Stripe integration — correctness of checkout, subscription lifecycle, plan changes, cancellation, webhook delivery, authorization, enforcement, and billing-state consistency.
**Isolation**: in-memory `InMemoryMultiChurchStore`, `_FakeStripeClient` for all Stripe calls, `BillingConfig` test-fixture (empty `STRIPE_SECRET_KEY` at import, patched to `"sk_test_audit"` per-test). Resend bail: `email_service` short-circuits on missing `RESEND_API_KEY` (logs `email_skipped` warning; no HTTP). No `backend/.env` loading in the tests. No live Stripe credentials, no production Firestore/Redis, no outbound email.

Stripe REQUEST version pinned to **`2023-10-16`** in the `Stripe-Version` header on each outbound call (`backend/app/billing/stripe_client.py:20,38`). This governs what Stripe's REST API returns to this backend. It does **not** independently establish the Stripe-API version used when Stripe DELIVERS webhook events — that version is set on the webhook endpoint configuration in the Stripe dashboard and is not visible from this codebase. Audit assumptions about webhook payload shapes (`current_period_start/end`, `items.data[0].price.id`, etc.) must be re-verified against the actually-configured delivery version in a Stripe sandbox.

## Boundary-coverage eighth pass (2026-10-29, after 53373836)

Reviewer flagged two remaining gaps.

### Gap 1 — Targeted field updates for all production billing writers
- **Problem**: `_hydrate_billing_from_stripe` is CAS-protected, but every other full-snapshot writer (`_create_missing_stripe_customer`, `_clear_stale_stripe_refs`, `_normalize_billing_refs` persisters in checkout/portal/change-plan, `change_plan` pending-plan writes, `cancel_pending_downgrade` clears) still read the OLD billing, mutated a few fields, and wrote the whole dict back — overwriting unrelated newer fields a concurrent webhook had committed. The setter's "legacy callers must not race" comment was aspirational, not enforced.
- **Fix**: new `patch_org_billing_fields(org_id, updates)` on BOTH stores. Reads current billing, applies a `{field: value}` dict update (None clears), merges nested `limits` key-by-key, bumps `billing.updatedAt`, writes — all atomic under `_lock` (in-memory) or `@gcf_firestore.transactional` (Firestore). No full-snapshot write occurs; unrelated fields from a concurrent writer survive. All affected billing writers in `routes/billing.py` migrated:
  - `_create_missing_stripe_customer` → patches only `stripeCustomerId`.
  - `_clear_stale_stripe_refs` → patches only the stale ref fields (set to None).
  - `_normalize_billing_refs` persisters (`billing_status`, `create_checkout_session`, `create_portal_session`, `change_plan`) → new `_persist_normalized_refs` helper patches only the three normalized ref fields.
  - `change_plan` pending-plan writer → patches only `{pendingPlanKey, pendingPlanDate, pendingScheduleId}`.
  - `cancel_pending_downgrade` → patches those three fields back to None.

  `_hydrate_billing_from_stripe` keeps CAS'd `set_org_billing_profile` — it still writes a reconciled full snapshot from Stripe retrieval (that's the whole point of hydrate), and the CAS token prevents stale overwrite there.
- **Regressions**:
  - `B15_CustomerCreateReverseRaceGuard::test_stale_customer_create_does_not_erase_committed_subscription` — real threads + `threading.Event` barrier: helper paused inside `create_customer`, webhook commits Growth subscription, helper released. After helper completes, stored billing retains Growth's planKey/sub id/priceId/status and gains `stripeCustomerId`.
  - Firestore emulator: `PatchOrgBillingFieldsFirestoreTests::test_patch_preserves_concurrent_webhook_fields` and `::test_patch_merges_limits_key_by_key`.

### Gap 2 — Checkout completion must persist the reconciled priceId
- **Problem**: v7's delayed-checkout reconciliation resolved `requested_plan` from the live subscription's `price_id` but did not write that `price_id` to `next_billing["priceId"]`. Result: planKey=growth, maxServiceKeys=12, priceId=price_starter (inconsistent).
- **Fix**: capture `reconciled_price_id` in the reconciliation branch; after the plan/limits merge, when a reconciled price id is present, persist it: `next_billing["priceId"] = reconciled_price_id`.
- **Regression**: `B13_DelayedCheckoutCompletionDoesNotRevertUpgrade::test_checkout_completion_reconciles_stale_priceId_too` — starting state planKey=starter, priceId=price_starter; live Stripe=Growth; delivered checkout.session.completed with metadata.planKey=starter. All three (planKey, limits.maxServiceKeys, priceId) must land Growth.

## Boundary-coverage seventh pass (2026-10-29, after 8b9d656b)

Reviewer flagged two remaining gaps. Both addressed.

### Gap 1 — Delayed Checkout completion reconciles against current subscription
- **Problem**: `checkout.session.completed` arriving for an already-current subscription derived `requested_plan` from `metadata.planKey` without reconciling against the subscription's current price. A delayed completion for a Starter checkout session could downgrade a Growth-upgraded subscription back to Starter.
- **Fix**: when `payload_obj.subscription == billing.stripeSubscriptionId` (same sub id), retrieve the subscription from Stripe and resolve `requested_plan` from the live `price_id` via `_resolve_plan_from_price_id`. Initial-checkout path (no current sub) continues to trust `metadata.planKey`. Also suppresses the duplicate `subscription_started` email on delayed completions for an already-recorded sub.
- **Regression**: `B13_DelayedCheckoutCompletionDoesNotRevertUpgrade::test_delayed_checkout_for_current_sub_does_not_revert_growth`.

### Gap 2 — CAS covers reverse writer direction (hydrate → set_org_billing_profile)
- **Problem**: `set_org_billing_profile` wrote unconditionally with a bumped `updatedAt`. The hydrate path (`_hydrate_billing_from_stripe`) reads billing, retrieves Stripe (slow), then writes — a webhook completing in between is overwritten by the hydrate's stale snapshot.
- **Fix**: `set_org_billing_profile` on both stores accepts an optional `expected_billing_updated_at`. When provided, the read+compare+write is transactional (Firestore) or inside the `_lock` (in-memory) and raises `StaleBillingProfileError` on mismatch. `_hydrate_billing_from_stripe` passes the CAS token from its initial read; on mismatch, abandons the stale snapshot and returns the current stored billing instead of overwriting.
- **Regressions**:
  - `B14_HydrateReverseRaceCASGuard::test_stale_hydrate_cannot_overwrite_committed_webhook` — real threads + `threading.Event` barrier: hydrate paused inside `retrieve_subscription`, webhook commits Growth, hydrate released with stale Starter snapshot → CAS rejects; stored state remains Growth; hydrate returns current state (not stale).
  - Firestore emulator: `SetOrgBillingProfileCASFirestoreTests::test_cas_mismatch_raises_stale_billing_profile_error`.

### B12c label correction
The `B12::test_webhook_cas_catches_concurrent_customer_create` test was previously labeled "barrier-driven." It is a deterministic `side_effect` interleaving on the atomic apply — docstring corrected. The real-thread + barrier case lives in `B14`.

## Boundary-coverage sixth pass (2026-10-29, after aaaca878)

Reviewer flagged three remaining blockers against aaaca878. All three addressed.

### Issue 1 — Reconcile against the current subscription on every event path
- **Problem**: v5 only reconciled `invoice.payment_failed`. The `customer.subscription.*` branch merged `payload_obj` directly, so a stale `subscription.updated` reporting `active` could restore access after `subscription.deleted`, revert a Growth upgrade to Starter, etc. `invoice.payment_succeeded` checked identity + local status but not current Stripe state, so an older succeeded invoice set `active` while newer debt was still outstanding.
- **Fix**: on `customer.subscription.created`/`updated`, retrieve the subscription from Stripe and use that payload (not the event's). `subscription.deleted` is terminal; retrieve is skipped (the event IS the current state). `invoice.payment_succeeded` and `invoice.payment_failed` both retrieve the subscription and only act when Stripe's status matches the direction of the event. Retrieval failures raise `HTTPException(502, "stripe_reconcile_failed")` so Stripe retries; the event is NOT completed.
- **Regressions** (`B10_SubscriptionReconciliationAgainstCurrentStripeState`):
  - `test_deleted_then_older_updated_active_does_not_restore`
  - `test_older_updated_with_starter_after_growth_upgrade_does_not_revert`
  - `test_payment_succeeded_for_older_invoice_does_not_restore_active`

### Issue 2 — Transient retrieval failures remain retryable
- **Problem**: v5 swallowed `StripeClientError` by assigning `stripe_status="active"` and completing the event. A transient Stripe outage would permanently drop a `payment_failed`.
- **Fix**: every reconciliation retrieval now raises `HTTPException(502)` on failure. The event stays claimed (and ages out of its lease per the existing claim/complete logic), so Stripe retries succeed once Stripe is reachable again.
- **Regression** (`B11_TransientRetrievalErrorRemainsRetryable`):
  - `test_payment_failed_retry_converges_after_transient_retrieve_error` — first delivery raises 502 (retrieve fails); event stays uncompleted; lease aged out; second delivery (retrieve recovers, reports past_due) completes and persists past_due.

### Issue 3 — CAS covers every billing writer
- **Problem**: `set_org_billing_profile` preserved the input dict's `updatedAt`. `_create_missing_stripe_customer` (called from `/checkout-session`) passes a dict whose `updatedAt` is unchanged, so a concurrent webhook's CAS passed and overwrote the new customer reference.
- **Fix**: both stores' `set_org_billing_profile` now **always** stamp `billing["updatedAt"] = now` AFTER normalization — covering every caller regardless of what they passed. Combined with v5's checkout-mutation bumps, the full set of billing writers (claim, record, set, atomic apply) uniformly participate in CAS.
- **Regressions** (`B12_CreateMissingStripeCustomerBumpsCAS`):
  - `test_create_missing_customer_bumps_billing_updated_at` — unit-level: `set_org_billing_profile` overrides any supplied `updatedAt`.
  - `test_webhook_cas_catches_concurrent_customer_create` — barrier-driven interleaving: webhook paused after billing read; `_create_missing_stripe_customer` actually runs with a mocked client; webhook resumes → 409 → customer reference preserved.

## Boundary-coverage fifth pass (2026-10-29, after 02546a70)

Reviewer flagged two remaining root problems. Both addressed here.

### Issue 1 — Replaced the org-wide event-created watermark with state reconciliation
- **Problem**: `lastProcessedEventCreatedAt` is an invalid ordering mechanism per Stripe's own documentation (`created` must not determine order). Reproductions:
  - **A**. Older `customer.subscription.created` arriving after newer `checkout.session.completed` would be discarded as `staleEvent`; on a previously canceled account this left `pendingSubscriptionConfirmation=True` and status=canceled forever.
  - **B**. Same-second success+fail — deliver success then older failure → failure passed the `<` watermark check and restored past_due.
  - **C**. Obsolete-event no-op advanced the watermark; a legitimate current-sub event with earlier `created` was then discarded.
- **Fix**: removed `event_created_at` parameter + `lastProcessedEventCreatedAt` field entirely. State reconciliation now drives decisions:
  - `invoice.payment_failed`: retrieve the subscription from Stripe (`StripeBillingClient.retrieve_subscription`, mocked in tests); apply only when Stripe itself still reports `past_due`/`unpaid`/`incomplete`. Older failed-payment retries after Stripe has recovered the subscription are no-ops.
  - `invoice.payment_succeeded`: already correctly gated on the current stored `status` AND now also on the invoice's subscription id matching the current subscription id (obsolete-invoice no-op).
  - `customer.subscription.*` and `checkout.session.completed`: existing obsolete-subscription-id guards from pass v3 cover these.
- **No global suppression** anywhere; event-ID dedup (claim/complete) remains separate from the state decision.
- **Regressions** (`B2_StateReconciliationNotWatermark`):
  - `test_payment_failed_after_recovery_does_not_regress`
  - `test_equal_timestamp_success_then_failure_no_global_discard`
  - `test_subscription_created_before_checkout_completion_is_not_discarded`
  - `test_obsolete_noop_does_not_discard_later_current_event`

### Issue 2 — CAS now covers checkout mutations
- **Problem**: `claim_checkout_attempt` and `record_checkout_attempt_session` mutated billing fields but did NOT bump `billing.updatedAt`. A webhook that captured the pre-mutation `updatedAt` passed CAS and overwrote the checkout mutation.
- **Fix**: both methods now set `billing["updatedAt"] = now` on write — in both the in-memory store and the Firestore transactional path.
- **Regressions** (`B9_CheckoutMutationsBumpBillingUpdatedAt`):
  - `test_claim_checkout_attempt_bumps_updated_at`
  - `test_record_checkout_attempt_session_bumps_updated_at`
  - `test_webhook_cas_catches_concurrent_claim_checkout_attempt` — webhook pauses after read; a concurrent claim bumps billing; webhook resumes → CAS mismatch → 409; the claim is NOT erased.
  - `test_webhook_cas_catches_concurrent_record_session` — same shape for `record_checkout_attempt_session`; sessionId is NOT reset to None.
- **Firestore emulator parallels**: `CheckoutMutationsBumpBillingUpdatedAtFirestoreTests` (3 tests) running against the real emulator.

## Boundary-coverage fourth pass (2026-10-29, after 27b92941)

Reviewer flagged four remaining issues against the 27b92941 bundle. All four are addressed in this pass.

### Issue 1 — Stale organization snapshots (CAS + event ordering)
- **Problem**: `apply_webhook_event_atomic` took a pre-computed `billing` dict. A sibling delivery could update the org between the handler's read and the handler's apply; the Firestore transaction then wrote the stale snapshot. The reviewer's reproduction: older `invoice.payment_failed` handler pauses, newer `invoice.payment_succeeded` runs to completion, older handler resumes — final status `past_due` even though both events "completed."
- **Fix**: `apply_webhook_event_atomic` now accepts `expected_billing_updated_at` (CAS token) and `event_created_at` (Stripe event's `created` timestamp). Inside the transaction:
  - If `expected_billing_updated_at` ≠ current `billing.updatedAt` → return `{"applied": False, "reason": "stale_snapshot"}`. The route surfaces 409 so Stripe retries; on retry the handler reads the current state.
  - If `event_created_at` is strictly older than `lastProcessedEventCreatedAt` on the org → mark the event completed (identity-level processing recorded) WITHOUT any billing mutation. Prevents retries of an older event from stomping on newer state.
  - On success, `lastProcessedEventCreatedAt` is bumped to the current event's timestamp.
- **Late-arrival branches now pass `billing=None`**: the previous code passed its full stale snapshot into `apply_webhook_event_atomic`, which could overwrite a replacement attempt. `billing=None` is a NO-OP write — the atomic apply still verifies CAS + claim-token + event-order but writes nothing to the billing profile.
- **Deterministic interleaving regression**: `B2_InterleavedDifferentEventsRespectCASAndEventOrder::test_interleaved_pay_failed_then_succeeded_does_not_regress_to_past_due`. Uses a `side_effect` patch on the atomic apply to simulate "SUCCEEDED completes while FAILED is paused mid-flight" by directly bumping `billing.updatedAt` + setting `lastProcessedEventCreatedAt`. FAILED's first apply returns `stale_snapshot` → 409. Retry sees `stale_event` → marks completed without billing mutation. Final status: `active` (not regressed to `past_due`).
- **Emulator parallel**: `test_apply_refuses_on_stale_billing_snapshot` and `test_apply_refuses_stale_event_timestamp` exercise CAS + event-ordering inside real Firestore transactions.
- **Removed misleading claim**: earlier audit text said in-memory `_lock` serializes "whole handlers." That is **not true** — the lock serializes individual store calls only. The B2 test used to assert sequential last-write-wins; it has been replaced by the deterministic interleaving regression above.

### Issue 2 — Match checkout completion by identity (session_id + obsolete subscription)
- **Problem**:
  - `pending_plan_matches` compared only plan + interval. Distinct attempts can share both; a completion for session `cs_A` could erase pending session `cs_B`.
  - Terminal-arrival guard only checked matching subscription IDs. A delayed completion for `sub_obsolete` could overwrite the current `sub_current`.
- **Fix**:
  - Completion-matching is now by `sessionId`: `pending_session_matches` requires `pendingCheckoutAttempt.sessionId == payload.id`. Clear the pending record only on this match.
  - Added **obsolete-subscription reconciliation** on BOTH event families:
    - `checkout.session.completed`: if the incoming subscription id ≠ the current one AND the current status is active-shaped, treat as late arrival (no mutation; complete event).
    - `customer.subscription.*`: same check — if the incoming subscription id ≠ the current one AND current status is active-shaped, mark completed with no billing mutation.
- **Regressions**: `B6_LateCompletionDoesNotEraseReplacementBySessionId::test_late_completion_for_sibling_attempt_preserves_replacement`, `B7_ObsoleteSubscriptionDoesNotOverwriteCurrent::test_subscription_event_for_obsolete_sub_leaves_current_untouched`.

### Issue 3 — Preserve `softCapReached` reset in the atomic apply
- **Problem**: the original `set_org_monthly_minutes_limit` reset `softCapReached=False` when the new limit was 0 or exceeded `currentMonthMinutes`. The atomic-apply dropped that behavior; an upgrade from an exhausted Starter left the softCap flag permanently True under Growth.
- **Fix**: both stores' `apply_webhook_event_atomic` now reset `softCapReached=False` when `monthly_minutes` is provided AND (limit == 0 OR currentMonthMinutes < limit) — matching the setter's semantics inside the same critical section.
- **Regressions**: `B8_AtomicApplyPreservesSoftCapResetBehavior` (in-memory) + `test_apply_preserves_soft_cap_reset` (Firestore emulator).

### Issue 4 — CI emulator discovery for ApplyWebhookEventAtomicFirestoreTests
- **Fix**: `.github/workflows/ci.yml` now selects the WHOLE module `tests/test_billing_webhook_claim_firestore.py` in the firestore-emulator-tests invocation. Any future test class added to that file is automatically discovered.

## Boundary-coverage third pass (2026-10-29, after 817635e8)

Reviewer flagged six concrete issues in the previous bundle; this pass addresses each with code changes and regression tests. Head commit on `prep/stripe-billing-audit` is now `<new-sha>`.

### Issue 1 — Fenced persistence (true transactional apply)
- **Problem**: `check_claim_still_held` and `set_org_billing_profile` were separate operations with a race window; `set_org_monthly_minutes_limit` ran BEFORE the ownership check, so a rejected claimant could still mutate `maxMinutesPerMonth` to 1800 while the stored plan was Starter.
- **Fix**: new `apply_webhook_event_atomic(org_id, billing, monthly_minutes, event_id, claim_token)` on BOTH stores. In-memory holds `_lock` for the whole critical section; Firestore uses `@gcf_firestore.transactional` with event-claim verification + org read + billing update + minute-limit update + event completion all in one transaction. The route no longer calls `check_claim_still_held` + `set_org_billing_profile` + `set_org_monthly_minutes_limit` + `complete_billing_event` separately. Any failure during the apply leaves the event uncompleted → Stripe retries.
- **Regression**: `H5_FailedWriteRetryIsNotDeduped` (now patches the atomic apply), `B1_SlowWorkerAfterLeaseReclaim::test_webhook_apply_refused_when_claim_lost`, `B5_SwallowedPlanLimitsFailureNowRecoverable::test_apply_failure_is_not_swallowed_followed_by_completion`.

### Issue 2 — Remove fabricated trial status
- **Problem**: the previous `checkout.session.completed` branch set `status=trialing` to block second-checkout. This changed entitlement status: a delayed completion for a cancelled subscription resurrected service access.
- **Fix**: no fabricated status change. A `pendingSubscriptionConfirmation` boolean is set on the billing profile when `subscription_id` arrives on `checkout.session.completed`; `claim_checkout_attempt` reads this flag to refuse duplicate checkouts. The real status flows in via `customer.subscription.*` which also clears the flag. A late-arrival completion for an already-terminal subscription short-circuits with `lateArrival=true` and NO billing mutation.
- **Regression**: `B4b_LateArrivalCheckoutDoesNotResurrectAccess::test_late_arrival_does_not_grant_service_access` (asserts `start_service` still raises `PermissionError` after a late terminal completion); `B4c_LateArrivalDoesNotEraseReplacementAttempt` (asserts a REPLACEMENT attempt for a different plan is NOT cleared by a late terminal completion for the OLD subscription); `B4_PostCompletionGapRefusesSecondCheckout` adjusted to work with the new `pendingSubscriptionConfirmation` flag.

### Issue 3 — Legacy event-record compatibility
- **Problem**: original `billingEvents` docs had `{eventId, eventType, payload, createdAt}` with no `status`/`claimedAt`. The new claim treated them as fresh, which would implicitly replay historical events.
- **Fix**: both stores now treat a record missing the `status` field as `completed` under the pre-existing "existence = processed" semantics.
- **Regression**: `B4d_LegacyEventRecordTreatedAsDeduped::test_legacy_record_in_memory_is_deduped` + Firestore-emulator `WebhookClaimFirestoreTests::test_legacy_record_treated_as_deduped`.

### Issue 4 — Stable checkout request parameters
- **Problem**: on uncertain response, a different admin could POST with a different `requestedByUid`, `successUrl`, or `cancelUrl` but the SAME attemptId. The route rebuilt parameters from the new request → Stripe would see a key-reuse with changed body (Idempotency-Key contract violation).
- **Fix**: `claim_checkout_attempt` persists all immutable params (`successUrl`, `cancelUrl`, `requestedByUid`) on the pending record. On reuse, it enforces that every parameter matches; mismatch returns `pending_params_mismatch` → the route surfaces 409 `pending_checkout_params_mismatch`. The claim refuses BEFORE invoking Stripe, so a realistic Idempotency-Key mock (which raises on same-key-different-body) never sees two differing requests.
- **Regression**: `B4e_CheckoutAttemptStableParams` (two tests covering uid-change and successUrl-change); `B4f_StripeIdempotencyContractEnforced::test_mismatched_retry_refused_before_reaching_stripe` (realistic Stripe mock that would raise on key+param conflict; test asserts the refusal fires at the claim step so Stripe never sees the mismatch).

### Issue 5 — Emulator fixtures
- **Problem**: tests opened Firestore via `firestore.Client()` without `database="worship-translation"`, so they read/wrote a different database than the store. The lease test wrote to `billing_events`, store uses `billingEvents`.
- **Fix**: test helpers now open the SAME database and reference the SAME collection name. Added REAL parallel-claim and parallel-checkout-attempt tests (4 threads converge on one attempt / one fresh claim).
- **Firestore-emulator run**: portable Temurin 21 JRE downloaded to `/home/namju/.cache/jre21-stripe-audit`; `gcloud emulators firestore start` runs on 127.0.0.1:8085. The suite executes **11/11 passing** on the real emulator including the parallel-claim and parallel-checkout tests, the stale-lease / slow-worker ownership test, the fenced-apply with-and-without stale token, and the legacy-record dedup test.

### Issue 6 — Clean the branch
- `backend/app/services/__pycache__/__init__.cpython-312.pyc` was tracked (committed in `53e51d8470691ef1619d71b75bba079af4673b88` long ago; `.gitignore` includes it but the pre-existing track overrode the pattern). Removed via `git rm --cached` on the branch.

## Boundary-coverage second pass (2026-10-29, after fc147a07 + 719fb775)

Operator-named boundaries + where coverage lives + any additional defects reproduced + fixes applied.

### Boundary 1 — lease ownership (slow worker must not overwrite newer claim)

- **Existing coverage**: `H5_FailedWriteRetryIsNotDeduped` + `H5b_ConcurrentDeliveryGetsConflict` cover persistence-failure recovery and parallel-delivery serialization, but DO NOT cover the slow-worker-returns-after-lease-reclaim case.
- **Added regression**: `B1_SlowWorkerAfterLeaseReclaim::test_stale_slow_worker_cannot_complete` + `test_webhook_rejects_write_when_claim_lost`. Fail on base; pass on fix.
- **Defect reproduced**: `complete_billing_event` on base writes `status=completed` unconditionally; a slow worker (lease aged out, sibling delivery already took over and finished) stomps on the newer claim's `completedAt`. More severely, the slow worker's eventual `set_org_billing_profile` write (which pre-fix was gated only by the lease *at claim time*) can race against the newer claim's write.
- **Fix applied**: claim/complete now carry an opaque `claimToken`. Return of `try_claim_billing_event` is now `{"status": …, "claimToken": …}`. New `check_claim_still_held(event_id, claim_token)` is called in the webhook before `set_org_billing_profile`; a 409 `webhook_claim_lost` aborts the write when the token no longer matches. `complete_billing_event(event_id, claim_token)` returns `False` on mismatch instead of overwriting state.

### Boundary 2 — concurrent different events on the same org

- **Existing coverage**: in-memory store serializes ALL writes via a single `_lock`; cross-event sequential processing is correct and `B2_ConcurrentDifferentEventsSameOrg_DocumentedGap::test_sequential_different_events_both_persist_last_write_wins` asserts that last-write-wins is deterministic in-memory.
- **Defect not reproduced locally**: the Firestore impl uses `set_org_billing_profile → ref.get()+ref.set(merge=True)` with no transaction and no optimistic-concurrency precondition. Two concurrent `invoice.payment_*` events on the same org could race and the later-ordered write wins unpredictably. **Per operator directive**: "reproduce an actual defect before changing it." Local reproduction requires the Firestore emulator — not available in this audit session. Logged as Suspected risk S8 below for the Stripe sandbox / Firestore-emulator scenarios.
- **No code change** in this pass. The fix would require either a transactional read-modify-write for `set_org_billing_profile` or an `updatedAt`-based optimistic-concurrency precondition. Operator-approved separately.

### Boundary 3 — checkout ownership (late Stripe response must not overwrite a replacement attempt)

- **Existing coverage**: `record_checkout_attempt_session` ONLY updates the pending record if the `attemptId` matches (guard in both stores). No test exercised this.
- **Added regression**: `B3_CheckoutLateResponseDoesNotOverwriteReplacement::test_record_checkout_attempt_session_only_updates_matching_attempt`. Fails on base (the method doesn't exist); passes on fix.
- **No additional defect**: the pre-existing guard in `claim_checkout_attempt`/`record_checkout_attempt_session` is sufficient. The test confirms it.

### Boundary 4 — checkout lifecycle (post-completion gap must not reopen duplicate purchasing)

- **Existing coverage**: `H1_SecondCheckoutRefusedWhenSubscriptionActive` covers the active-subscription case. `H2b_PendingDifferentPlanRefused` covers the pending-attempt case.
- **Defect reproduced**: after `checkout.session.completed` clears `pendingCheckoutAttempt` and sets `stripeSubscriptionId` on a previously-canceled org, a rapid second POST could slip through the `(stripeSubscriptionId AND status∈{active,past_due,trialing})` guard because `status` was still `canceled` (the real status comes with the subsequent `customer.subscription.*` event). Second POST → new attempt → second Stripe subscription → H1's shape returns.
- **Added regression**: `B4_PostCompletionGapRefusesSecondCheckout::test_rapid_second_checkout_after_completion_is_refused`. Fails on base; passes on fix.
- **Fix applied**: in the `checkout.session.completed` branch, if the current `status` is NOT in `{"active","past_due","trialing"}`, promote to `"trialing"` as an intermediate marker. The real status overwrites on the subsequent `customer.subscription.*` event.

### Boundary 5 — required persistence (failures must be recoverable, not swallowed)

- **Existing coverage**: `H5_FailedWriteRetryIsNotDeduped` covers `set_org_billing_profile` failure recovery. It did NOT cover `set_org_monthly_minutes_limit` failure (the earlier code bare-caught + swallowed everything from that call).
- **Defect reproduced**: `set_org_monthly_minutes_limit` was wrapped in `try/except Exception → logger.exception` on both webhook branches; a failure left the billing profile written with the new plan key, event marked completed, and the monthly-minutes cap stale forever (no retry possible). Operator-named contract violated.
- **Added regression**: `B5_SwallowedPlanLimitsFailureNowRecoverable::test_monthly_minutes_failure_is_not_swallowed_followed_by_completion`. Fails on base; passes on fix.
- **Fix applied**: both call sites now split exception handling: `ValueError("org_not_found")` logs a warning and continues (unrecoverable), anything else re-raises as `HTTPException(500, "billing_plan_limits_sync_failed")` so Stripe retries. Event is NOT completed; retry runs the handler again.

## Fix batch 1 — applied on this branch (local only; nothing pushed)

Three fix sub-batches landed in commit `prep/stripe-billing-audit` on top of `fc147a07` (the audit-tests + review-doc commit). Nothing is pushed, merged, deployed, or configured.

### Sub-batch (A) — Webhook retry safety (addresses the newly-added **H5** finding)
- `backend/app/services/multichurch_store.py` — new `try_claim_billing_event(event_id, event_type, payload) → {"fresh","deduped","concurrent"}` + `complete_billing_event(event_id)` on BOTH stores (in-memory + Firestore). Firestore impl runs inside `@gcf_firestore.transactional`. `BILLING_EVENT_LEASE_SEC` env-tunable (default 60 s). Legacy `try_mark_billing_event` preserved as a claim-then-complete wrapper.
- `backend/app/routes/billing.py::stripe_webhook` — reorganised so (1) claim happens up front and may return `409 webhook_concurrent_delivery` for parallel sibling deliveries, (2) emails are collected in a `pending_emails` list and dispatched ONLY after `set_org_billing_profile` succeeds, (3) `complete_billing_event` is the LAST step. If persistence raises, the handler returns 400 and the lease stays pending; after `BILLING_EVENT_LEASE_SEC`, Stripe's retry reclaims and runs to completion.

### Sub-batch (B) — Duplicate-subscription prevention + stable idempotency (addresses **H1** + **H2** together)
- `backend/app/services/multichurch_store.py` — new `claim_checkout_attempt(org_id, plan_key, interval)` returning one of `{"already_subscribed","pending_different_plan","reuse","create"}` and persisting a `pendingCheckoutAttempt` field on the org's billing. Transactional on both stores. `BILLING_CHECKOUT_ATTEMPT_TTL_SEC` env-tunable (default 24 h to match Stripe's Checkout Session default). Companion `record_checkout_attempt_session(org_id, attempt_id, session_id, session_url)` persists the Stripe-returned URL on the attempt record only if it matches the current attempt id.
- `backend/app/routes/billing.py::create_checkout_session` — refuses with 409 if a current subscription is active-shaped; refuses with 409 if a pending attempt for a different plan is in flight; reuses the prior session URL/ID when the same plan is re-requested within TTL; otherwise generates a stable `chkatt_<uuid>` and passes it to Stripe as the `Idempotency-Key`. Retries after an uncertain network outcome use the SAME attempt id. A new logical attempt (e.g. after TTL expiry or after `checkout.session.completed` clears the pending record) gets a new id.
- `backend/app/billing/stripe_client.py::create_checkout_session` — accepts `idempotency_key: Optional[str]` and threads it to the HTTP layer.

### Sub-batch (C) — Plan consistency (addresses **H3** + **M3**)
- `backend/app/routes/billing.py::_resolve_plan_from_price_id` — now consults BOTH `stripe_price_ids` AND `stripe_price_ids_annual` so a Portal-driven monthly↔annual change resolves correctly.
- `backend/app/routes/billing.py::stripe_webhook` (`customer.subscription.*` branch) — the stale `metadata.get("planKey")` override removed. The plan key resolved from the price_id by `_merge_subscription_snapshot` is now authoritative. Prevents H3's internal-inconsistency (`priceId=new, planKey=stale`).
- `backend/app/routes/billing.py::stripe_webhook` (`checkout.session.completed` branch) — adds `multichurch_store.set_org_monthly_minutes_limit` and clears any now-superseded `pendingCheckoutAttempt`. Prevents M3's "service-key cap advanced, monthly-minutes cap stale".

Note on scope: `change_plan` STILL reads only `BILLING_CONFIG.stripe_price_ids`, not the annual map (S7 below). Explicitly left pending per operator directive — annual plan-change policy and Stripe-side existing-schedule behaviour are deferred.

## Test commands and results

Combined (baseline + regression + boundary) on the fix branch:

```
cd backend
backend/venv/bin/python -m pytest tests/test_billing_routes.py tests/test_billing_entitlements.py \
    tests/test_billing_stripe_audit.py tests/test_billing_webhook_claim_firestore.py -q
47 passed, 1 skipped in 0.71s
```

Fix-branch breakdown:
- `tests/test_billing_routes.py` — 13 pre-existing (unchanged in substance; the test-local `_FakeStripeClient` accepts the new `idempotency_key` kwarg).
- `tests/test_billing_entitlements.py` — 15 pre-existing (unchanged).
- `tests/test_billing_stripe_audit.py` — 19 regression + boundary tests (asserting INTENDED post-fix behavior).
- `tests/test_billing_webhook_claim_firestore.py` — 5 emulator tests; skipped locally (no Java), CI emulator job included.

Base-commit validation (`ac32110c`, fresh worktree, same regression file + fixture patched for the new kwarg):

```
backend/venv/bin/python -m pytest tests/test_billing_stripe_audit.py -q --tb=no
14 failed, 5 passed in 0.83s
```

The 14 failures on base map 1:1 to the HIGH/MEDIUM findings and the four boundary defects: H1, H2 (2), H2b, H3 (2), M3, H5, H5b-concurrent, **B1 (2), B3, B4, B5**. The 5 passes on base document behaviour unchanged by this fix (`H5b::test_completed_event_is_deduped`, `M1_RepeatedChangePlanAttempts`, `M4` edge, `M4b` positive, `B2` documented-gap in-memory serialization). Each regression test is a specific assertion of intended behavior; none are weakened or skipped on the fix branch, and none pass trivially on base.

Firestore atomicity: `backend/tests/test_billing_webhook_claim_firestore.py` exercises `try_claim_billing_event`, `check_claim_still_held`, `complete_billing_event` with `claim_token`, and `claim_checkout_attempt` through the Firestore transactional path. The module skips on any environment without `FIRESTORE_EMULATOR_HOST` set.

CI discovery wiring: the file is now included in the pytest invocation inside `.github/workflows/ci.yml::firestore-emulator-tests` (added `tests/test_billing_webhook_claim_firestore.py::WebhookClaimFirestoreTests` and `::CheckoutAttemptFirestoreTests` to the explicit test list). Prior fix batch 1 had set `FIRESTORE_EMULATOR_HOST` but failed to add the file to the discovery list — the operator correctly flagged this. The job's "Fail if emulator tests skipped or none ran" guard catches the mistake of leaving a file out going forward.

**Unresolved local-verification gap**: the Firestore emulator requires Java (Firestore emulator ≥ v1.19 needs JRE 21 per the CI setup step). Java is not installed in this audit session and cannot be installed without `sudo` (apt-get prompted for a password). The transactional implementation is thus validated by code review + in-memory parity + the CI emulator job's upcoming run on push, **not** by local execution. **The transactional implementation is not characterized as validated locally; the authoritative check is CI.**

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

### M1 (CORRECTED) — `change_plan` does not RELEASE a prior pending schedule before attempting to create a new one

**Impact (corrected)**: MEDIUM. The local behaviour is unambiguous: `change_plan` does not call `cancel_subscription_schedule` on an existing `pendingScheduleId` before invoking `/subscription_schedules?from_subscription=<sub>`. **What Stripe does with the second create call is a Stripe-side question**: Stripe refuses to create a second subscription schedule on a subscription that already has a non-released schedule attached, so the second call may fail with an HTTP 400 and this backend surfaces `stripe_schedule_failed` to the user. The earlier audit text "two schedules created on Stripe" was overstated — only ONE schedule-create ATTEMPT per request is modelled; the Stripe-side outcome depends on sandbox verification.

- **File / line**: `backend/app/routes/billing.py:815-836` (`change_plan`, downgrade branch).
- **Reproduction**: `test_billing_stripe_audit.py::M1_RepeatedChangePlanAttempts` — asserts the LOCAL fact that no release call is made before a second create attempt. The Stripe response (whether the second create succeeds or 400s) is NOT asserted by this test and is deferred to the sandbox scenarios below.
- **Proposed fix** (NOT applied in fix batch 1 per operator scope): at the top of the downgrade branch, if `billing.get("pendingScheduleId")` is set, call `client.cancel_subscription_schedule(schedule_id=<prior>)` first (idempotent on an already-released schedule), then proceed. This also prevents the user-facing `stripe_schedule_failed` noise when a customer rapidly re-submits the same downgrade.

### M3 — `checkout.session.completed` sets `maxServiceKeys` but does NOT sync `maxMinutesPerMonth`

**Impact**: MEDIUM — on completion of a paid-plan checkout, the service-key cap updates immediately but the monthly-minutes cap stays at its prior value until the subsequent `customer.subscription.*` event lands. If that event is lost or delayed, the org operates with inconsistent limits (new plan key, new service-key cap, old minutes cap).

- **File / line**: `backend/app/routes/billing.py:942-954` (`checkout.session.completed` branch) — writes `limits.maxServiceKeys` but never calls `set_org_monthly_minutes_limit`. The sync happens at `backend/app/routes/billing.py:976-982` which is inside the `customer.subscription.*` branch only.
- **Reproduction**: `test_billing_stripe_audit.py::M3_CheckoutCompletedDoesNotSyncMonthlyMinutes` — after `checkout.session.completed` for Growth, `maxServiceKeys=12` (correct) but `org.maxMinutesPerMonth` remains at the bootstrap default (500) rather than `1800` (Growth plan).
- **Proposed fix** (not applied): add a `set_org_monthly_minutes_limit(org_id=org_id, monthly_minutes=requested_plan.monthly_minutes)` call in the `checkout.session.completed` branch, matching the `customer.subscription.*` branch at line 977.

### H5 — Premature webhook deduplication: failed billing writes become permanently lost

**Impact**: HIGH — the webhook handler calls `try_mark_billing_event(event_id, ...)` BEFORE attempting the billing write. If the write fails (e.g. Firestore throws, the normalisation raises, the org doc has been reset mid-flight), the handler raises `HTTPException(400)` and returns a non-2xx to Stripe. Stripe retries. The dedup store already recorded the event, so the retry sees `fresh=False` and returns `deduped=True` — the retry never runs the billing update. The intended change (status transition, grace timer, plan key, etc.) is lost permanently; nothing emits a signal that anything is wrong.

- **File / line**: `backend/app/routes/billing.py:909-925` on base — `try_mark_billing_event` is called immediately after signature verification and payload decode; the handler then does `_resolve_org_id_from_event` + `multichurch_store.set_org_billing_profile(...)` at the end. Any raise between the dedup mark and the final set loses the update on retry.
- **Reproduction**: `test_billing_stripe_audit.py::H5_FailedWriteRetryIsNotDeduped` — forces `set_org_billing_profile` to raise on the first delivery, then confirms a second delivery of the same event (after lease aging) succeeds and persists the intended state.
- **Expected**: the event must NOT be marked completed until billing persistence succeeds. Concurrent duplicate deliveries must be serialized so two parallel handlers don't both attempt a write.
- **Fix (APPLIED on this branch — fix batch 1)**: split dedup into **claim-and-complete**. New store methods:
  - `try_claim_billing_event(event_id, event_type, payload)` → returns `"fresh"` | `"deduped"` | `"concurrent"`.
    - `"fresh"`: caller holds a lease (default 60 s); caller MUST call `complete_billing_event` after successful persistence.
    - `"deduped"`: a prior delivery completed; return 200.
    - `"concurrent"`: a prior delivery still holds a non-expired lease; return 409 so Stripe retries after its back-off. Prevents parallel double-write.
  - `complete_billing_event(event_id)` — flips the record to `completed` with a server timestamp; idempotent.
  - A stale lease (older than `BILLING_EVENT_LEASE_SEC`, default 60 s, env-tunable in `[15, 600]`) is reclaimed on the next delivery so a crashed handler doesn't permanently lock an event.
  - Webhook handler reorganised: notifications collected during processing but DISPATCHED ONLY AFTER `set_org_billing_profile(...)` succeeds; `complete_billing_event(...)` is the LAST step. The legacy `try_mark_billing_event` is preserved as a wrapper (claim + immediate complete) so unmigrated callers are unaffected.
- **Regression tests added**: `H5_FailedWriteRetryIsNotDeduped`, `H5b_ConcurrentDeliveryGetsConflict::test_second_concurrent_delivery_returns_409`, `H5b_ConcurrentDeliveryGetsConflict::test_completed_event_is_deduped` (positive-dedup still works).
- **Firestore atomicity**: the claim-decide-write runs inside a `@gcf_firestore.transactional` function in `FirestoreMultiChurchStore.try_claim_billing_event`. In-memory success alone does not prove Firestore atomicity; the Firestore-emulator CI job (`.github/workflows/ci.yml::firestore-emulator-tests`) is the authoritative check. Local Firestore-emulator tests under `backend/tests/test_billing_webhook_claim_firestore.py` are added below the main suite with a module-level skip when `FIRESTORE_EMULATOR_HOST` is unset.

### M4 (CORRECTED) — Webhook ordering: edge case only, not the normal checkout flow

**Impact (corrected)**: the earlier audit characterised this as a normal-flow risk. That premise is wrong. The deployed code SETS `stripeCustomerId` on the org inside `_create_missing_stripe_customer` BEFORE calling `create_checkout_session` (`backend/app/routes/billing.py:258-278`), and the Stripe client stamps `subscription_data[metadata][orgId]` on the SUBSCRIPTION itself (`backend/app/billing/stripe_client.py:111-112`). So a `customer.subscription.created` arriving before `checkout.session.completed` resolves via EITHER the subscription-metadata `orgId` OR a lookup by `stripeCustomerId` — the handler does not drop the event. The claim covered the sentence "the subscription's `trialing/active` + `cancelAtPeriodEnd` + `currentPeriodEnd` wouldn't be set until a subsequent `subscription.updated` arrives", which was based on the wrong premise.

- **Remaining EDGE case**: a subscription created OUTSIDE the standard Checkout flow (e.g. in the Stripe dashboard directly, with no `orgId` metadata, for a customer the org doesn't yet own) still returns `{"unmatched": true}`. This is the correct behaviour — the system has no way to attribute the event. Operator-reset or manually-created subscriptions must be hand-reconciled.
- **Regression tests (positive + edge)**: `M4_WebhookEdgeCase_SubscriptionWithoutOrgIdOrKnownCustomer::test_unknown_customer_and_missing_metadata_returns_unmatched` (asserts `unmatched=true` with no state mutation), and `M4b_SubscriptionWithOrgIdMetadataMatches::test_subscription_metadata_orgid_resolves` (positive — subscription metadata lets the handler resolve the org without a prior `checkout.session.completed`).

---

## 3. Suspected risks (not individually asserted but worth operator review)

- **S1 — `_lookup_customer_subscription` picks "best" subscription when multiple exist**: `backend/app/routes/billing.py:388-394` lists up to 10 subscriptions and sorts by `_subscription_sort_key` (status rank, then `-current_period_end`, then `-created_at`). Fix batch 2 prevents H1 from creating two active subscriptions, which was the primary concern.
- **S2 — WITHDRAWN (incorrect in prior audit)**: `_pick_customer_subscription` uses Python `min(..., key=_subscription_sort_key)` with the tuple `(status_rank, -current_period_end, -created_at)`. `min` returns the SMALLEST tuple; for ties on `(status_rank, -current_period_end)`, the smallest `-created_at` is the LARGEST `created_at`, i.e. the NEWEST subscription. The earlier text claiming "picks the OLDER one" was wrong — no change needed.
- **S3 — Payment-recovered email only fires on `invoice.payment_succeeded` → `active` transition** (`routes/billing.py:1003-1008`). If recovery is reflected via a `customer.subscription.updated` with `status=active` directly (e.g. operator manually fixes in Stripe dashboard), no recovery email is sent. Not changed in fix batch 1.
- **S4 — Webhook signature tolerance window**: default 60 s, configurable 30–300 (`routes/billing.py:81-83`). Cloud Run cold-starts under load have been observed in the 5–15 s range; well within tolerance. No action.
- **S5 — Trial is local-only, not Stripe-managed**: `create_checkout_session` always passes `trial_days=0` (`routes/billing.py:629,649`); the 30-day local trial lives in `multichurch_store` + `BILLING_CONFIG.trial_days`. Stripe treats the paid subscription as immediate. Design decision; documented in code; not a defect.
- **S6 — Changing plans from `past_due` is allowed** by `change_plan` (`routes/billing.py:770`: `if current_status not in {"active", "past_due"}: raise ...`). This matches typical Stripe UX (upgrade to settle the balance), but confirm business policy.
- **S7 — Annual plan-change path still uses only `stripe_price_ids` map in `change_plan`** (`routes/billing.py:751`). A monthly→annual change via `change_plan` would silently route to the monthly price. Fix batch 3 added annual-mapping support ONLY to the price-id→plan RESOLVER (webhook direction). The `change_plan` DIRECTION (plan→price-id) still ignores `stripe_price_ids_annual`. Explicitly left PENDING per operator directive — annual plan-change policy and the Stripe-side existing-schedule interaction are deferred.
- **S8 — Concurrent writes to the same org billing doc have no Firestore-level serialization**: `FirestoreMultiChurchStore.set_org_billing_profile` reads then writes with `merge=True`, no transaction, no `updatedAt` precondition. Two concurrent `invoice.payment_*` events on the same org can both read the old state, each modify, and race on write — last write wins unpredictably. In-memory serializes via `_lock`, so the gap is Firestore-only. **Local reproduction requires the Firestore emulator** (unavailable in this audit session; see verification gap below). Fix would be a transactional read-modify-write or an optimistic-concurrency precondition on `updatedAt`. **Operator-stated principle "reproduce an actual defect before changing it" applied**: no code change in this pass; documented for the Stripe-sandbox / emulator scenarios.

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
