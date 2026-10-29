"""Focused Stripe billing audit — reproduction tests.

Each test here isolates one defect or suspected risk discovered during
the 2026-10-29 audit (`docs/03-analysis/stripe-billing-audit.md`).
Tests reuse the fixtures from `test_billing_routes.py` (in-memory
store, `_FakeStripeClient`, `_DummyRequest`, `_sign_stripe_payload`).

Isolation: no `.env` loading, no live Stripe credentials, no Firestore,
no Resend outbound email, no external HTTP. All Stripe interactions are
through `_FakeStripeClient` subclasses defined in this module.
"""
from __future__ import annotations

import asyncio
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from fastapi import HTTPException

from app.auth.firebase_auth import AuthenticatedUser
from app.billing.config import BillingConfig
from app.routes import auth as auth_routes
from app.routes import billing as billing_routes
from app.services import multichurch_store as multichurch_store_module
from app.services.multichurch_store import InMemoryMultiChurchStore

from tests.test_billing_routes import (
    _DummyRequest,
    _FakeStripeClient,
    _sign_stripe_payload,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _AuditBase(unittest.TestCase):
    """Shared setup; mirrors BillingRouteTests but keeps the two independent."""

    entitlements_v2_enabled: bool = False

    def setUp(self) -> None:
        self.store = InMemoryMultiChurchStore()
        self.auth_patch = patch.object(auth_routes, "multichurch_store", self.store)
        self.billing_patch = patch.object(billing_routes, "multichurch_store", self.store)
        self.fake_client = _FakeStripeClient()
        self.stripe_patch = patch.object(billing_routes, "_stripe_client", lambda: self.fake_client)
        self.auth_patch.start()
        self.billing_patch.start()
        self.stripe_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.addCleanup(self.billing_patch.stop)
        self.addCleanup(self.stripe_patch.stop)
        # Patch BILLING_CONFIG on BOTH importers (routes/billing.py and
        # services/multichurch_store.py) so `_entitlements_v2_effective`
        # (which reads multichurch_store.BILLING_CONFIG) sees the test
        # flag, not the module's import-time value.
        self._config = BillingConfig(
            stripe_secret_key="sk_test_audit",
            stripe_webhook_secret="whsec_audit",
            stripe_price_ids={
                "starter": "price_starter",
                "growth": "price_growth",
                "premium": "price_premium",
            },
            stripe_price_ids_annual={
                "starter": "price_starter_annual",
                "growth": "price_growth_annual",
                "premium": "price_premium_annual",
            },
            trial_days=30,
            trial_minutes=20,
            grace_days=3,
            entitlements_v2_enabled=self.entitlements_v2_enabled,
        )
        self.billing_config_patch = patch.object(billing_routes, "BILLING_CONFIG", self._config)
        self.store_config_patch = patch.object(multichurch_store_module, "BILLING_CONFIG", self._config)
        self.billing_config_patch.start()
        self.store_config_patch.start()
        self.addCleanup(self.billing_config_patch.stop)
        self.addCleanup(self.store_config_patch.stop)

    @staticmethod
    def _user(uid: str) -> AuthenticatedUser:
        return AuthenticatedUser(uid=uid, email=f"{uid}@example.com", displayName=uid)

    def _bootstrap_owner(self, *, uid: str, slug: str, name: str) -> str:
        result = auth_routes.auth_bootstrap_owner(
            auth_routes.BootstrapOwnerRequest(
                churchName=name, churchSlug=slug, timezone="America/Chicago",
                source="ko", target="en",
            ),
            user=self._user(uid),
        )
        org = result.get("org") or {}
        org_id = str(org.get("orgId") or "").strip()
        self.assertTrue(org_id, result)
        return org_id


class H1_DuplicateSubscriptionNotPrevented(_AuditBase):
    """H1: `create_checkout_session` lets a church open a second checkout
    even when it already has an active subscription. Completing both sessions
    would create two concurrent Stripe subscriptions on the same customer;
    the webhook handler then overwrites `stripeSubscriptionId` with the latest
    one, orphaning the first (which keeps billing)."""

    def test_checkout_allowed_while_active_subscription_exists(self) -> None:
        org_id = self._bootstrap_owner(
            uid="owner-h1", slug="h1-church", name="H1 Church",
        )
        # Seed an already-active subscription on the org.
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_h1"
        billing["stripeSubscriptionId"] = "sub_existing_active"
        billing["status"] = "active"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        # Second checkout for a different plan should ideally be refused
        # (point the user at the portal) — currently it proceeds and
        # returns a new session URL.
        result = billing_routes.create_checkout_session(
            billing_routes.CheckoutSessionRequest(
                orgId=org_id,
                planKey="growth",
                successUrl="https://example.com/ok",
                cancelUrl="https://example.com/cancel",
                interval="month",
            ),
            user=self._user("owner-h1"),
        )
        # EXPECTED (desired fix): HTTPException with
        # detail="subscription_already_active".
        # ACTUAL today: no refusal; checkout.session URL returned.
        self.assertIn("url", result)
        self.assertTrue(result["url"].startswith("https://checkout.stripe.test/"))


class H2_ChexkoutIdempotencyMissing(_AuditBase):
    """H2: `StripeBillingClient.create_checkout_session` does not pass an
    `idempotency_key` to Stripe (unlike `create_customer`). Two near-concurrent
    checkout clicks (frontend busy-state defeated by a determined double-click
    or lost by a tab-switch) would create two independent Stripe sessions on
    the same customer — each independently completable, each firing a
    `checkout.session.completed` webhook, the second overwriting the first
    `stripeSubscriptionId` on the billing profile.

    This test asserts the current (unprotected) behavior by capturing the
    Stripe-client call site and confirming no `idempotency_key` header is
    passed."""

    def test_create_checkout_session_does_not_pass_idempotency_key(self) -> None:
        calls: List[Dict[str, Any]] = []

        class _CapturingClient(_FakeStripeClient):
            def create_checkout_session(self_inner, **kwargs: Any) -> Dict[str, Any]:
                calls.append(kwargs)
                return super().create_checkout_session(**kwargs)

        with patch.object(billing_routes, "_stripe_client", lambda: _CapturingClient()):
            org_id = self._bootstrap_owner(
                uid="owner-h2", slug="h2-church", name="H2 Church",
            )
            billing_routes.create_checkout_session(
                billing_routes.CheckoutSessionRequest(
                    orgId=org_id,
                    planKey="starter",
                    successUrl="https://example.com/ok",
                    cancelUrl="https://example.com/cancel",
                    interval="month",
                ),
                user=self._user("owner-h2"),
            )

        self.assertEqual(len(calls), 1, "checkout session should be created exactly once per route call")
        self.assertNotIn(
            "idempotency_key", calls[0],
            "today's behavior: no idempotency key passed — fix should include one",
        )


class H3_PortalPlanChangeStalePlanKey(_AuditBase):
    """H3: When a user changes plan via the Stripe Customer Portal, the
    subscription's `metadata.planKey` (set at initial checkout) is NOT
    updated by Stripe. The resulting `customer.subscription.updated` webhook
    carries the OLD `metadata.planKey` but the NEW `price_id`.

    `_merge_subscription_snapshot` correctly resolves the plan from the new
    `price_id` and sets `next_billing["planKey"]` accordingly. The webhook
    handler then OVERRIDES it at `routes/billing.py:963` with
    `metadata.get("planKey") or next_billing.get("planKey")` — restoring the
    stale plan. `limits.maxServiceKeys` and the org-level monthly minutes
    limit follow the stale plan key.

    Scenario: org on Starter → Portal upgrade to Growth → webhook delivers
    `customer.subscription.updated` with `metadata.planKey=starter` (stale)
    and `price=price_growth` (new).
    """

    def test_portal_upgrade_webhook_keeps_old_plan_key(self) -> None:
        org_id = self._bootstrap_owner(
            uid="owner-h3", slug="h3-church", name="H3 Church",
        )
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_h3"
        billing["stripeSubscriptionId"] = "sub_h3"
        billing["status"] = "active"
        billing["planKey"] = "starter"
        billing["priceId"] = "price_starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        now_ts = int(time.time())
        event = {
            "id": "evt_h3_upgrade",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_h3",
                    "customer": "cus_h3",
                    "status": "active",
                    "cancel_at_period_end": False,
                    "current_period_start": now_ts,
                    "current_period_end": now_ts + 30 * 86400,
                    # Stale checkout metadata — never updated by Portal plan change.
                    "metadata": {"orgId": org_id, "planKey": "starter"},
                    # NEW price — user upgraded to Growth via the Portal.
                    "items": {"data": [{"price": {"id": "price_growth"}}]},
                }
            },
        }
        raw = json.dumps(event).encode("utf-8")
        signature = _sign_stripe_payload(raw, "whsec_audit")

        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=signature,
            )
        )

        updated = self.store.get_org_billing_profile(org_id=org_id)
        # Expected (desired): planKey = "growth" (resolved from the new price_id).
        # Actual today: planKey = "starter" (stale metadata wins).
        self.assertEqual(
            updated.get("planKey"), "starter",
            "repro: stale metadata.planKey from checkout overrides the "
            "price-id-resolved new plan key — webhook line 963",
        )
        # The price_id itself IS updated correctly, so the inconsistency is
        # observable without touching Stripe.
        self.assertEqual(
            updated.get("priceId"), "price_growth",
            "price_id correctly updated — but planKey is stale, "
            "making the stored state internally inconsistent",
        )
        # Service-key cap followed the stale plan (Starter=5 instead of Growth=12).
        self.assertEqual(
            (updated.get("limits") or {}).get("maxServiceKeys"), 5,
            "limits.maxServiceKeys follows the stale plan key — "
            "under-cap for a Growth customer",
        )


class H4_V2OffViolatesGracePeriod(_AuditBase):
    """H4 (refined): Service-start enforcement under v2-off (production
    default per `config.py:39`) relies on the legacy `_legacy_org_start_allowed`
    check against `org.status`, which `set_org_billing_profile` mirrors from
    `billing.status` at `multichurch_store.py:1493-1495`.

    The legacy check accepts only `{"active","trial"}`. This means a
    `past_due` subscription within its documented grace window is BLOCKED
    under v2-off, violating the grace-period policy that `_can_start_from_status`
    + `_billing_start_denial_reason` implement under v2-on. A church whose
    payment failed yesterday and has `graceEndsAt` set 2 days in the future
    should still be able to broadcast — v2-off blocks them with the
    less-informative `org_inactive` error.

    A further concern (not individually asserted here): `plan_limit_reached`
    (the service-key cap check in `start_service`) is ONLY wired behind
    v2-on. Under v2-off, churches can create more services than their plan
    allows (except through the CREATE path's org-level counts).
    """

    entitlements_v2_enabled = False  # audit baseline — flag OFF

    def test_past_due_within_grace_is_blocked_under_v2_off(self) -> None:
        org_id = self._bootstrap_owner(
            uid="owner-h4a", slug="h4a-church", name="H4a Church",
        )
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_h4a"
        billing["stripeSubscriptionId"] = "sub_h4a"
        billing["status"] = "past_due"
        billing["planKey"] = "starter"
        # Grace window still has 2 days remaining — v2-on would allow start.
        billing["graceEndsAt"] = _utc_now() + timedelta(days=2)
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        self.store._services[(org_id, "sun-11am")] = {
            "title": "Sunday 11am",
            "timezone": "America/Chicago",
            "rrule": None,
            "defaultLanguagePair": {"source": "ko", "target": "en"},
            "activeRoomId": None,
            "lastRoomId": None,
            "updatedAt": _utc_now(),
        }

        with self.assertRaises(PermissionError) as ctx:
            self.store.start_service(
                org_id=org_id, service_key="sun-11am",
                host_uid="owner-h4a", source="ko", target="en",
            )
        self.assertEqual(
            str(ctx.exception), "org_inactive",
            "repro: v2-off legacy check mirrors billing.status→org.status and "
            "rejects past_due regardless of graceEndsAt — grace-period policy "
            "is NOT honored under v2-off",
        )


class H4b_V2OnRespectsGracePeriod(_AuditBase):
    """Companion to H4: when the flag is ON, the same past-due-within-grace
    subscription correctly starts service. Demonstrates the policy difference."""

    entitlements_v2_enabled = True

    def test_past_due_within_grace_allows_start_under_v2_on(self) -> None:
        org_id = self._bootstrap_owner(
            uid="owner-h4b", slug="h4b-church", name="H4b Church",
        )
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_h4b"
        billing["stripeSubscriptionId"] = "sub_h4b"
        billing["status"] = "past_due"
        billing["planKey"] = "starter"
        billing["graceEndsAt"] = _utc_now() + timedelta(days=2)
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        self.store._services[(org_id, "sun-11am")] = {
            "title": "Sunday 11am",
            "timezone": "America/Chicago",
            "rrule": None,
            "defaultLanguagePair": {"source": "ko", "target": "en"},
            "activeRoomId": None,
            "lastRoomId": None,
            "updatedAt": _utc_now(),
        }

        result = self.store.start_service(
            org_id=org_id, service_key="sun-11am",
            host_uid="owner-h4b", source="ko", target="en",
        )
        self.assertEqual(result.get("orgId"), org_id)


class M1_ChangePlanDoesNotCancelPendingSchedule(_AuditBase):
    """M1: `change_plan` creates a new subscription schedule for downgrades
    without canceling an existing `pendingScheduleId`. Repeated downgrade
    attempts leave orphan schedules on Stripe."""

    def test_change_plan_leaves_prior_schedule_untouched(self) -> None:
        released_schedules: List[str] = []
        created_schedules: List[str] = []

        class _TrackingClient(_FakeStripeClient):
            # Return non-trivial subscription so the downgrade branch runs.
            def retrieve_subscription(self_inner, *, subscription_id: str) -> Dict[str, Any]:
                now_ts = int(time.time())
                return {
                    "id": subscription_id,
                    "customer": "cus_m1",
                    "status": "active",
                    "cancel_at_period_end": False,
                    "current_period_start": now_ts,
                    "current_period_end": now_ts + 30 * 86400,
                    "items": {"data": [{"id": "si_m1_item", "price": {"id": "price_growth"}}]},
                }

            def create_subscription_schedule(self_inner, *, subscription_id: str,
                                              current_price_id: str, new_price_id: str,
                                              phase_end_unix: int) -> Dict[str, Any]:
                sid = f"sub_sched_m1_{len(created_schedules) + 1}"
                created_schedules.append(sid)
                return {"id": sid}

            def cancel_subscription_schedule(self_inner, *, schedule_id: str) -> Dict[str, Any]:
                released_schedules.append(schedule_id)
                return {"id": schedule_id, "status": "released"}

        with patch.object(billing_routes, "_stripe_client", lambda: _TrackingClient()):
            org_id = self._bootstrap_owner(
                uid="owner-m1", slug="m1-church", name="M1 Church",
            )
            billing = self.store.get_org_billing_profile(org_id=org_id)
            billing["stripeCustomerId"] = "cus_m1"
            billing["stripeSubscriptionId"] = "sub_m1"
            billing["status"] = "active"
            billing["planKey"] = "growth"
            self.store.set_org_billing_profile(org_id=org_id, billing=billing)

            # First downgrade request → creates schedule 1
            billing_routes.change_plan(
                billing_routes.ChangePlanRequest(orgId=org_id, targetPlanKey="starter"),
                user=self._user("owner-m1"),
            )
            after_first = self.store.get_org_billing_profile(org_id=org_id)
            self.assertEqual(after_first.get("pendingScheduleId"), "sub_sched_m1_1")

            # Second downgrade request (user changed their mind to Growth
            # cannot be the target because `same_plan` fires on current;
            # here we re-request Starter — which SHOULD be a no-op or
            # should reuse schedule 1. Observed today: creates schedule 2
            # without releasing schedule 1.)
            billing_routes.change_plan(
                billing_routes.ChangePlanRequest(orgId=org_id, targetPlanKey="starter"),
                user=self._user("owner-m1"),
            )
            after_second = self.store.get_org_billing_profile(org_id=org_id)

        self.assertEqual(
            len(created_schedules), 2,
            "two schedules were created across two downgrade requests",
        )
        self.assertEqual(
            released_schedules, [],
            "repro: no prior schedule was released — the first schedule "
            "is orphaned on Stripe",
        )
        self.assertEqual(
            after_second.get("pendingScheduleId"), "sub_sched_m1_2",
            "billing profile tracks only the latest schedule — the first "
            "is lost from local state too",
        )


class M3_CheckoutCompletedDoesNotSyncMonthlyMinutes(_AuditBase):
    """M3: On `checkout.session.completed`, the webhook sets
    `limits.maxServiceKeys` but does NOT call
    `set_org_monthly_minutes_limit`. The monthly-minutes sync happens only
    inside the `customer.subscription.*` branch. If the subscription event
    arrives after `checkout.session.completed` (or is lost), the org has
    the new plan key + maxServiceKeys but the old (trial / zero) monthly
    minutes limit until the next subscription event lands."""

    def test_checkout_completed_leaves_monthly_minutes_at_prior_value(self) -> None:
        org_id = self._bootstrap_owner(
            uid="owner-m3", slug="m3-church", name="M3 Church",
        )
        # Baseline: trial — maxMinutesPerMonth has whatever bootstrap set.
        before_minutes = self.store._orgs[org_id].get("maxMinutesPerMonth")

        now_ts = int(time.time())
        event = {
            "id": "evt_m3_checkout",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_m3",
                    "customer": "cus_m3",
                    "subscription": "sub_m3",
                    "metadata": {"orgId": org_id, "planKey": "growth", "interval": "month"},
                }
            },
        }
        raw = json.dumps(event).encode("utf-8")
        signature = _sign_stripe_payload(raw, "whsec_audit")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=signature,
            )
        )
        after_minutes = self.store._orgs[org_id].get("maxMinutesPerMonth")
        after_billing = self.store.get_org_billing_profile(org_id=org_id)

        self.assertEqual(after_billing.get("planKey"), "growth")
        self.assertEqual(
            (after_billing.get("limits") or {}).get("maxServiceKeys"), 12,
            "maxServiceKeys is correctly synced on checkout.session.completed",
        )
        # Growth plan monthly_minutes = 1800. If checkout.session.completed
        # synced it, after_minutes would be 1800. Observed: unchanged.
        self.assertEqual(
            after_minutes, before_minutes,
            "repro: org-level maxMinutesPerMonth is NOT synced on "
            "checkout.session.completed — only on customer.subscription.*",
        )
        self.assertNotEqual(
            after_minutes, 1800,
            "the Growth plan's 1800 monthly minutes have not been applied",
        )


class M4_OutOfOrderSubscriptionBeforeCheckout(_AuditBase):
    """M4: Stripe does not guarantee webhook ordering. If
    `customer.subscription.created` arrives BEFORE
    `checkout.session.completed`, the org has no `stripeCustomerId`
    stored yet, so `find_org_id_by_billing_refs` can't match the event.
    The handler returns `unmatched=true` (HTTP 200), which tells Stripe
    not to retry. The subscription's `trialing/active` + period info is
    LOST on that revision — only re-populated when a later subscription
    event arrives.

    Reproducer: send `customer.subscription.created` with
    `customer="cus_new"` BEFORE `checkout.session.completed`. The org's
    billing status is NOT updated. Then `checkout.session.completed`
    arrives and sets `stripeCustomerId`. By then, Stripe has moved on
    and no retry fires."""

    def test_subscription_created_arriving_first_is_lost(self) -> None:
        org_id = self._bootstrap_owner(
            uid="owner-m4", slug="m4-church", name="M4 Church",
        )
        # Pre-condition: no stripe refs on the org.
        billing_before = self.store.get_org_billing_profile(org_id=org_id)
        self.assertFalse(billing_before.get("stripeCustomerId"))
        self.assertFalse(billing_before.get("stripeSubscriptionId"))

        now_ts = int(time.time())
        subscription_event = {
            "id": "evt_m4_sub",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "id": "sub_m4",
                    "customer": "cus_m4",
                    "status": "active",
                    "cancel_at_period_end": False,
                    "current_period_start": now_ts,
                    "current_period_end": now_ts + 30 * 86400,
                    "metadata": {},  # no orgId metadata on bare subscription object
                    "items": {"data": [{"price": {"id": "price_growth"}}]},
                }
            },
        }
        raw = json.dumps(subscription_event).encode("utf-8")
        signature = _sign_stripe_payload(raw, "whsec_audit")
        result = asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=signature,
            )
        )
        self.assertTrue(result.get("unmatched"),
                        "subscription.created arriving first is unmatched — "
                        "returns 200 so Stripe will not retry")

        # Second delivery of the same event (Stripe retry OR dedup check):
        # the dedup store already marked evt_m4_sub as processed, so the
        # retry would be deduped — the data stays lost even if Stripe
        # retried.
        second = asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=signature,
            )
        )
        self.assertTrue(
            second.get("deduped"),
            "repro: dedup fires on retry even though we couldn't resolve "
            "the org on the first delivery — the subscription snapshot is "
            "lost until a future event (updated/payment_succeeded) arrives",
        )

        # Now checkout.session.completed arrives — stripeCustomerId gets set
        # but the subscription period / status fields that only
        # _merge_subscription_snapshot populates are NOT backfilled from
        # the checkout event alone.
        checkout_event = {
            "id": "evt_m4_checkout",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_m4",
                    "customer": "cus_m4",
                    "subscription": "sub_m4",
                    "metadata": {"orgId": org_id, "planKey": "growth", "interval": "month"},
                }
            },
        }
        raw2 = json.dumps(checkout_event).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw2),
                stripe_signature=_sign_stripe_payload(raw2, "whsec_audit"),
            )
        )
        after = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(after.get("stripeCustomerId"), "cus_m4")
        self.assertEqual(after.get("stripeSubscriptionId"), "sub_m4")
        self.assertEqual(after.get("planKey"), "growth")
        # Subscription-snapshot fields (currentPeriodStart/End, status
        # from Stripe, trialEndsAt) are NOT populated by checkout.session
        # — only by _merge_subscription_snapshot which was deduped.
        self.assertIsNone(
            after.get("currentPeriodStart"),
            "repro: subscription period is not populated — the lost "
            "subscription.created event is the only path that would have "
            "set it",
        )
        self.assertIsNone(after.get("currentPeriodEnd"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
