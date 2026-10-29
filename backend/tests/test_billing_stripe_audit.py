"""Stripe billing audit — regression suite.

Each test asserts the INTENDED post-fix behavior. On base commit
`ac32110c` these tests FAIL (reproducing the audit's findings). On
the audit branch's fix commits they PASS.

Isolation: in-memory store, `_FakeStripeClient` subclasses, patched
BILLING_CONFIG on both importers (routes/billing + services/multichurch_store).
No `.env` loading, no live Stripe credentials, no outbound email.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from fastapi import HTTPException

from app.auth.firebase_auth import AuthenticatedUser
from app.billing.config import BillingConfig
from app.billing.stripe_client import StripeClientError
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
    """Shared setup; patches BILLING_CONFIG on BOTH importers so the
    entitlements_v2 flag and price-id mappings reach every call site.

    Each subclass may override `entitlements_v2_enabled`. The fake Stripe
    client is accessible via `self.fake_client`.
    """

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


# ---------------------------------------------------------------------------
# Fix batch 2 — duplicate-subscription prevention + stable idempotency.
# ---------------------------------------------------------------------------

class H1_SecondCheckoutRefusedWhenSubscriptionActive(_AuditBase):
    """Base behavior: `create_checkout_session` allowed a second checkout
    while an active subscription exists. Expected: refuse with 409
    `subscription_already_active`."""

    def test_checkout_refused_when_active_subscription_exists(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-h1", slug="h1-church", name="H1 Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_h1"
        billing["stripeSubscriptionId"] = "sub_existing_active"
        billing["status"] = "active"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        with self.assertRaises(HTTPException) as ctx:
            billing_routes.create_checkout_session(
                billing_routes.CheckoutSessionRequest(
                    orgId=org_id, planKey="growth",
                    successUrl="https://example.com/ok",
                    cancelUrl="https://example.com/cancel",
                    interval="month",
                ),
                user=self._user("owner-h1"),
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "subscription_already_active")


class H2_CheckoutUsesStableLogicalIdempotencyKey(_AuditBase):
    """Base behavior: no idempotency key passed. Expected: the Stripe
    HTTP call includes a stable attemptId (`chkatt_<hex>`), and the SAME
    key is reused when the same user re-POSTs the same (plan, interval)
    within the attempt TTL. A new attempt after expiry gets a new key."""

    def test_checkout_passes_logical_attempt_id_as_idempotency_key(self) -> None:
        captured: List[Dict[str, Any]] = []

        class _CapturingClient(_FakeStripeClient):
            def create_checkout_session(self_inner, **kwargs: Any) -> Dict[str, Any]:
                captured.append(kwargs)
                return super().create_checkout_session(**kwargs)

        with patch.object(billing_routes, "_stripe_client", lambda: _CapturingClient()):
            org_id = self._bootstrap_owner(uid="owner-h2", slug="h2-church", name="H2 Church")
            # First POST — creates attempt, calls Stripe once.
            r1 = billing_routes.create_checkout_session(
                billing_routes.CheckoutSessionRequest(
                    orgId=org_id, planKey="starter",
                    successUrl="https://example.com/ok",
                    cancelUrl="https://example.com/cancel",
                    interval="month",
                ),
                user=self._user("owner-h2"),
            )
            self.assertIn("url", r1)
            # Second POST same intent — reuses the attempt. Must NOT
            # issue a second Stripe call.
            r2 = billing_routes.create_checkout_session(
                billing_routes.CheckoutSessionRequest(
                    orgId=org_id, planKey="starter",
                    successUrl="https://example.com/ok",
                    cancelUrl="https://example.com/cancel",
                    interval="month",
                ),
                user=self._user("owner-h2"),
            )
            self.assertEqual(r1["sessionId"], r2["sessionId"])
            self.assertTrue(bool(r2.get("reused")))

        self.assertEqual(len(captured), 1, "second same-intent POST must not re-call Stripe")
        key1 = str(captured[0].get("idempotency_key") or "")
        self.assertTrue(key1.startswith("chkatt_"), f"expected logical attempt id, got {key1!r}")

    def test_concurrent_checkout_requests_converge_on_one_attempt_id(self) -> None:
        """Two concurrent POSTs for the same (orgId, planKey, interval)
        must converge on ONE logical attempt. Local serialization
        guarantees ONE attemptId. Any Stripe HTTP calls across both
        threads MUST use that same attemptId as the idempotency key
        (which lets Stripe return the already-created session on the
        second call). The number of Stripe HTTP calls can be 1 or 2
        depending on scheduling — the key invariant is that the SAME
        idempotency key is used, which is what makes Stripe-side
        deduplication possible.
        """
        captured: List[Dict[str, Any]] = []
        captured_lock = threading.Lock()
        gate = threading.Barrier(2)

        class _SlowClient(_FakeStripeClient):
            def create_checkout_session(self_inner, **kwargs: Any) -> Dict[str, Any]:
                with captured_lock:
                    captured.append(kwargs)
                time.sleep(0.03)
                return super().create_checkout_session(**kwargs)

        org_id = self._bootstrap_owner(uid="owner-h2c", slug="h2c-church", name="H2c Church")
        results: List[Dict[str, Any]] = [{}, {}]
        with patch.object(billing_routes, "_stripe_client", lambda: _SlowClient()):
            def _call(idx: int) -> None:
                gate.wait()
                try:
                    results[idx] = billing_routes.create_checkout_session(
                        billing_routes.CheckoutSessionRequest(
                            orgId=org_id, planKey="starter",
                            successUrl="https://example.com/ok",
                            cancelUrl="https://example.com/cancel",
                            interval="month",
                        ),
                        user=self._user("owner-h2c"),
                    )
                except Exception as exc:
                    results[idx] = {"error": repr(exc)}

            t1 = threading.Thread(target=_call, args=(0,))
            t2 = threading.Thread(target=_call, args=(1,))
            t1.start(); t2.start()
            t1.join(); t2.join()

        # No caller errored.
        self.assertFalse(any("error" in r for r in results), results)
        # Both callers landed on ONE pending-attempt record. Any Stripe
        # calls used the same idempotency key (the attemptId) — which
        # IS the stable-logical-key contract: Stripe sees the retry.
        attempt_keys = {str(c.get("idempotency_key") or "") for c in captured}
        self.assertEqual(
            len(attempt_keys), 1,
            f"both Stripe calls must share one attemptId key; got {attempt_keys}",
        )
        self.assertTrue(
            next(iter(attempt_keys)).startswith("chkatt_"),
            f"idempotency key must be a stable attemptId, got {attempt_keys}",
        )


class H2b_PendingDifferentPlanRefused(_AuditBase):
    """A pending non-expired attempt for Starter must not be silently
    replaced by a POST for Growth — that could create two Stripe
    sessions for different plans on the same customer. Expected: 409."""

    def test_pending_attempt_for_different_plan_refused(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-h2b", slug="h2b-church", name="H2b Church")
        # First attempt — Starter.
        billing_routes.create_checkout_session(
            billing_routes.CheckoutSessionRequest(
                orgId=org_id, planKey="starter",
                successUrl="https://example.com/ok",
                cancelUrl="https://example.com/cancel",
                interval="month",
            ),
            user=self._user("owner-h2b"),
        )
        # Second attempt within TTL for a DIFFERENT plan.
        with self.assertRaises(HTTPException) as ctx:
            billing_routes.create_checkout_session(
                billing_routes.CheckoutSessionRequest(
                    orgId=org_id, planKey="growth",
                    successUrl="https://example.com/ok",
                    cancelUrl="https://example.com/cancel",
                    interval="month",
                ),
                user=self._user("owner-h2b"),
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "pending_checkout_different_plan")


# ---------------------------------------------------------------------------
# Fix batch 3 — plan consistency.
# ---------------------------------------------------------------------------

class H3_PortalPlanChangeHonorsPriceIdResolution(_AuditBase):
    """Base behavior: subscription metadata's stale `planKey` overrode
    the price-id-resolved plan on `customer.subscription.updated`.
    Expected: planKey + maxServiceKeys + monthly_minutes follow the
    actual price_id."""

    def test_portal_upgrade_resolves_from_price_id(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-h3", slug="h3-church", name="H3 Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_h3"
        billing["stripeSubscriptionId"] = "sub_h3"
        billing["status"] = "active"
        billing["planKey"] = "starter"
        billing["priceId"] = "price_starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        now_ts = int(time.time())
        sub_state = {
            "id": "sub_h3", "customer": "cus_h3", "status": "active",
            "cancel_at_period_end": False,
            "current_period_start": now_ts,
            "current_period_end": now_ts + 30 * 86400,
            "metadata": {"orgId": org_id, "planKey": "starter"},  # STALE on sub
            "items": {"data": [{"price": {"id": "price_growth"}}]},
        }
        self.fake_client.sub_states["sub_h3"] = sub_state
        event = {
            "id": "evt_h3_upgrade",
            "type": "customer.subscription.updated",
            "data": {"object": sub_state},
        }
        raw = json.dumps(event).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        updated = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(updated.get("planKey"), "growth")
        self.assertEqual((updated.get("limits") or {}).get("maxServiceKeys"), 12)
        self.assertEqual(updated.get("priceId"), "price_growth")
        self.assertEqual(self.store._orgs[org_id].get("maxMinutesPerMonth"), 1800)

    def test_portal_change_to_annual_resolves_from_annual_price_mapping(self) -> None:
        """Annual price_ids live in `stripe_price_ids_annual`; the resolver
        must consult BOTH monthly and annual maps."""
        org_id = self._bootstrap_owner(uid="owner-h3a", slug="h3a-church", name="H3a Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_h3a"
        billing["stripeSubscriptionId"] = "sub_h3a"
        billing["status"] = "active"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        now_ts = int(time.time())
        sub_state = {
            "id": "sub_h3a", "customer": "cus_h3a", "status": "active",
            "cancel_at_period_end": False,
            "current_period_start": now_ts,
            "current_period_end": now_ts + 365 * 86400,
            "metadata": {"orgId": org_id, "planKey": "starter"},
            "items": {"data": [{"price": {"id": "price_growth_annual"}}]},
        }
        self.fake_client.sub_states["sub_h3a"] = sub_state
        event = {
            "id": "evt_h3a_annual",
            "type": "customer.subscription.updated",
            "data": {"object": sub_state},
        }
        raw = json.dumps(event).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        updated = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(updated.get("planKey"), "growth")
        self.assertEqual(updated.get("priceId"), "price_growth_annual")


class M3_CheckoutCompletedSyncsMonthlyMinutes(_AuditBase):
    """Base behavior: `checkout.session.completed` set maxServiceKeys
    but not monthly minutes. Expected: both sync."""

    def test_checkout_completed_syncs_minutes(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-m3", slug="m3-church", name="M3 Church")
        before_minutes = self.store._orgs[org_id].get("maxMinutesPerMonth")
        self.assertNotEqual(before_minutes, 1800)
        event = {
            "id": "evt_m3_checkout",
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_m3", "customer": "cus_m3", "subscription": "sub_m3",
                "metadata": {"orgId": org_id, "planKey": "growth", "interval": "month"},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        self.assertEqual(self.store._orgs[org_id].get("maxMinutesPerMonth"), 1800)


# ---------------------------------------------------------------------------
# Fix batch 1 — webhook retry safety (premature-dedup correction).
# ---------------------------------------------------------------------------

class H5_FailedWriteRetryIsNotDeduped(_AuditBase):
    """Original behavior documented by ChatGPT: a billing-persistence
    failure during webhook processing leaves the event marked as
    processed. Stripe's retry sees `deduped=True` and the billing update
    is lost forever.

    Expected: on persistence failure, the event is NOT completed, so a
    subsequent retry can rerun the handler to completion.
    """

    def test_persistence_failure_does_not_mark_event_completed(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-h5", slug="h5-church", name="H5 Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_h5"
        billing["stripeSubscriptionId"] = "sub_h5"
        billing["status"] = "active"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        now_ts = int(time.time())
        sub_state = {
            "id": "sub_h5", "customer": "cus_h5", "status": "past_due",
            "cancel_at_period_end": False,
            "current_period_start": now_ts,
            "current_period_end": now_ts + 30 * 86400,
            "metadata": {"orgId": org_id, "planKey": "starter"},
            "items": {"data": [{"price": {"id": "price_starter"}}]},
        }
        self.fake_client.sub_states["sub_h5"] = sub_state
        event = {
            "id": "evt_h5", "type": "customer.subscription.updated",
            "data": {"object": sub_state},
        }
        raw = json.dumps(event).encode("utf-8")
        sig = _sign_stripe_payload(raw, "whsec_audit")

        # First delivery: force apply_webhook_event_atomic to raise.
        # The fenced apply is the single critical section; its failure
        # must leave the event uncompleted so Stripe's retry can
        # rerun the handler cleanly.
        original_apply = self.store.apply_webhook_event_atomic
        calls = {"n": 0}
        def _flaky_apply(**kwargs: Any) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("persistence_write_failed")
            return original_apply(**kwargs)

        with patch.object(self.store, "apply_webhook_event_atomic", side_effect=_flaky_apply):
            with self.assertRaises(HTTPException):
                asyncio.run(
                    billing_routes.stripe_webhook(
                        request=_DummyRequest(raw),
                        stripe_signature=sig,
                    )
                )
        rec = self.store._billing_events["evt_h5"]
        self.assertNotEqual(rec.get("status"), "completed",
                            "event marked completed despite failed apply")
        rec["claimedAt"] = _utc_now() - timedelta(seconds=999)
        result = asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=sig,
            )
        )
        self.assertNotIn("deduped", result)
        self.assertEqual(result.get("eventId"), "evt_h5")
        final = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(final.get("status"), "past_due")
        self.assertEqual(self.store._billing_events["evt_h5"].get("status"), "completed")


class H5b_ConcurrentDeliveryGetsConflict(_AuditBase):
    """Concurrent duplicate deliveries of the same event: one proceeds
    (fresh claim); the second sees an active lease and returns 409 so
    Stripe retries it later. This prevents BOTH deliveries from running
    the billing write in parallel.
    """

    def test_second_concurrent_delivery_returns_409(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-h5b", slug="h5b-church", name="H5b Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_h5b"
        billing["stripeSubscriptionId"] = "sub_h5b"
        billing["status"] = "active"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        now_ts = int(time.time())
        event = {
            "id": "evt_h5b", "type": "customer.subscription.updated",
            "data": {"object": {
                "id": "sub_h5b", "customer": "cus_h5b", "status": "active",
                "cancel_at_period_end": False,
                "current_period_start": now_ts,
                "current_period_end": now_ts + 30 * 86400,
                "metadata": {"orgId": org_id, "planKey": "starter"},
                "items": {"data": [{"price": {"id": "price_starter"}}]},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        sig = _sign_stripe_payload(raw, "whsec_audit")

        # Simulate the second delivery arriving mid-processing by
        # manually claiming the event (as the first handler would) and
        # then invoking the webhook to simulate the second delivery.
        claim = self.store.try_claim_billing_event(
            event_id="evt_h5b", event_type="customer.subscription.updated",
            payload={"type": "customer.subscription.updated"},
        )
        self.assertEqual(claim.get("status"), "fresh")
        self.assertTrue(bool(claim.get("claimToken")))

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                billing_routes.stripe_webhook(
                    request=_DummyRequest(raw),
                    stripe_signature=sig,
                )
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "webhook_concurrent_delivery")

    def test_completed_event_is_deduped(self) -> None:
        """Positive-dedup regression: a redelivery after successful
        completion still returns {deduped: true}."""
        org_id = self._bootstrap_owner(uid="owner-h5c", slug="h5c-church", name="H5c Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_h5c"
        billing["stripeSubscriptionId"] = "sub_h5c"
        billing["status"] = "active"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        now_ts = int(time.time())
        event = {
            "id": "evt_h5c", "type": "customer.subscription.updated",
            "data": {"object": {
                "id": "sub_h5c", "customer": "cus_h5c", "status": "past_due",
                "cancel_at_period_end": False,
                "current_period_start": now_ts,
                "current_period_end": now_ts + 30 * 86400,
                "metadata": {"orgId": org_id, "planKey": "starter"},
                "items": {"data": [{"price": {"id": "price_starter"}}]},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        sig = _sign_stripe_payload(raw, "whsec_audit")
        first = asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw), stripe_signature=sig,
            )
        )
        self.assertFalse(first.get("deduped"))
        second = asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw), stripe_signature=sig,
            )
        )
        self.assertTrue(second.get("deduped"))


# ---------------------------------------------------------------------------
# M1 — change_plan schedule-attempt behavior.
# ---------------------------------------------------------------------------

class M1_RepeatedChangePlanAttempts(_AuditBase):
    """CORRECTED (previously overstated): a repeated downgrade call
    attempts to create a new Stripe schedule each time; local billing
    only tracks the latest schedule id. Whether Stripe actually CREATES a
    second schedule depends on Stripe-side existing-schedule constraints
    (Stripe refuses `/subscription_schedules?from_subscription=` when a
    non-released schedule already exists on that subscription). This
    test only probes our LOCAL behavior: no release call is made before
    the second attempt. The Stripe-side outcome is a sandbox question.
    """

    def test_second_downgrade_attempt_fires_without_releasing_prior(self) -> None:
        released: List[str] = []
        created: List[str] = []

        class _TrackingClient(_FakeStripeClient):
            def retrieve_subscription(self_inner, *, subscription_id: str) -> Dict[str, Any]:
                now_ts = int(time.time())
                return {
                    "id": subscription_id, "customer": "cus_m1",
                    "status": "active", "cancel_at_period_end": False,
                    "current_period_start": now_ts,
                    "current_period_end": now_ts + 30 * 86400,
                    "items": {"data": [{"id": "si_m1", "price": {"id": "price_growth"}}]},
                }

            def create_subscription_schedule(self_inner, *, subscription_id: str,
                                              current_price_id: str, new_price_id: str,
                                              phase_end_unix: int) -> Dict[str, Any]:
                sid = f"sub_sched_m1_{len(created) + 1}"
                created.append(sid)
                return {"id": sid}

            def cancel_subscription_schedule(self_inner, *, schedule_id: str) -> Dict[str, Any]:
                released.append(schedule_id)
                return {"id": schedule_id, "status": "released"}

        with patch.object(billing_routes, "_stripe_client", lambda: _TrackingClient()):
            org_id = self._bootstrap_owner(uid="owner-m1", slug="m1-church", name="M1 Church")
            billing = self.store.get_org_billing_profile(org_id=org_id)
            billing["stripeCustomerId"] = "cus_m1"
            billing["stripeSubscriptionId"] = "sub_m1"
            billing["status"] = "active"
            billing["planKey"] = "growth"
            self.store.set_org_billing_profile(org_id=org_id, billing=billing)
            billing_routes.change_plan(
                billing_routes.ChangePlanRequest(orgId=org_id, targetPlanKey="starter"),
                user=self._user("owner-m1"),
            )
            billing_routes.change_plan(
                billing_routes.ChangePlanRequest(orgId=org_id, targetPlanKey="starter"),
                user=self._user("owner-m1"),
            )
        self.assertEqual(len(created), 2)
        self.assertEqual(released, [],
                         "no release call made — Stripe may itself refuse the second create "
                         "(sandbox-only question), but LOCALLY we don't gate")


# ---------------------------------------------------------------------------
# M4 — corrected: labeled missing-metadata / unknown-customer edge case.
# ---------------------------------------------------------------------------

class M4_WebhookEdgeCase_SubscriptionWithoutOrgIdOrKnownCustomer(_AuditBase):
    """Previously overstated: the audit claimed the ordering of
    `customer.subscription.created` before `checkout.session.completed`
    would lose state in the normal checkout flow. The deployed code
    actually SETS `stripeCustomerId` on the org BEFORE calling Stripe
    (`_create_missing_stripe_customer` at `routes/billing.py:258-278`)
    AND stamps `subscription_data[metadata][orgId]` on the subscription
    (`billing/stripe_client.py:111-112`), so a subscription event
    arriving first still resolves via metadata or by customer-id lookup.

    This test retains only the realistic edge: a subscription event
    arriving with NEITHER orgId metadata NOR a known customer (e.g.
    because the subscription was created outside the standard checkout
    flow, or the org's billing profile was reset). That path is
    correctly reported as `unmatched`; the regression here is to CONFIRM
    the handler returns `unmatched=true` without side-effects, so an
    operator can resubscribe and reprocess manually.
    """

    def test_unknown_customer_and_missing_metadata_returns_unmatched(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-m4", slug="m4-church", name="M4 Church")
        self.assertFalse(self.store.get_org_billing_profile(org_id=org_id).get("stripeCustomerId"))
        now_ts = int(time.time())
        event = {
            "id": "evt_m4_edge", "type": "customer.subscription.created",
            "data": {"object": {
                "id": "sub_m4_unknown", "customer": "cus_unknown_to_us",
                "status": "active", "cancel_at_period_end": False,
                "current_period_start": now_ts,
                "current_period_end": now_ts + 30 * 86400,
                # Deliberately empty metadata — simulates an abnormal
                # subscription created outside our Checkout flow.
                "metadata": {},
                "items": {"data": [{"price": {"id": "price_growth"}}]},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        result = asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        self.assertTrue(result.get("unmatched"))
        # No org state was mutated:
        final = self.store.get_org_billing_profile(org_id=org_id)
        self.assertFalse(final.get("stripeCustomerId"))
        self.assertFalse(final.get("stripeSubscriptionId"))


class M4b_SubscriptionWithOrgIdMetadataMatches(_AuditBase):
    """Positive case: a subscription event with orgId metadata resolves
    even without a stored stripeCustomerId. Confirms the usual checkout
    flow's design holds."""

    def test_subscription_metadata_orgid_resolves(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-m4b", slug="m4b-church", name="M4b Church")
        now_ts = int(time.time())
        sub_state = {
            "id": "sub_m4b", "customer": "cus_m4b",
            "status": "active", "cancel_at_period_end": False,
            "current_period_start": now_ts,
            "current_period_end": now_ts + 30 * 86400,
            "metadata": {"orgId": org_id, "planKey": "growth"},
            "items": {"data": [{"price": {"id": "price_growth"}}]},
        }
        self.fake_client.sub_states["sub_m4b"] = sub_state
        event = {
            "id": "evt_m4b", "type": "customer.subscription.created",
            "data": {"object": sub_state},
        }
        raw = json.dumps(event).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        final = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(final.get("stripeCustomerId"), "cus_m4b")
        self.assertEqual(final.get("stripeSubscriptionId"), "sub_m4b")
        self.assertEqual(final.get("planKey"), "growth")


# ---------------------------------------------------------------------------
# H4 remains policy-dependent — kept as-is in the audit document.
# No code change proposed in this fix batch per operator directive.
# The policy question is captured in `docs/03-analysis/stripe-billing-audit.md`.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Boundary-coverage regressions (second pass, 2026-10-29).
# Each test maps 1:1 to an operator-named boundary.
# ---------------------------------------------------------------------------


class B1_SlowWorkerAfterLeaseReclaim(_AuditBase):
    """Boundary 1 — lease ownership. A slow worker whose claim has been
    reclaimed by a sibling delivery (because its lease aged out) must
    NOT be allowed to complete or overwrite the newer claim's state.

    Pre-fix: `complete_billing_event` always wrote `status=completed`
    unconditionally; the slow worker's late write stomped on the newer
    delivery's final state.

    Post-fix: `check_claim_still_held` + `complete_billing_event(claim_token=…)`
    reject the stale completion.
    """

    def test_stale_slow_worker_cannot_complete(self) -> None:
        # Delivery A claims.
        a = self.store.try_claim_billing_event(
            event_id="evt_b1", event_type="customer.subscription.updated",
            payload={"type": "customer.subscription.updated"},
        )
        self.assertEqual(a.get("status"), "fresh")
        token_a = a.get("claimToken")
        # Age the lease.
        rec = self.store._billing_events["evt_b1"]
        rec["claimedAt"] = _utc_now() - timedelta(seconds=600)
        # Delivery B reclaims.
        b = self.store.try_claim_billing_event(
            event_id="evt_b1", event_type="customer.subscription.updated",
            payload={"type": "customer.subscription.updated"},
        )
        self.assertEqual(b.get("status"), "fresh")
        self.assertNotEqual(a.get("claimToken"), b.get("claimToken"))

        # B completes successfully.
        self.assertTrue(self.store.complete_billing_event(
            event_id="evt_b1", claim_token=b.get("claimToken"),
        ))

        # A finally returns and attempts to complete with its STALE
        # token. The completion must be rejected — the event is still
        # terminal under B's completion, and A's attempt is ignored.
        a_result = self.store.complete_billing_event(
            event_id="evt_b1", claim_token=token_a,
        )
        self.assertFalse(a_result)

    def test_webhook_apply_refused_when_claim_lost(self) -> None:
        """A slow handler whose lease was reclaimed mid-flight must
        not stomp on the newer claim's work. The fenced apply method
        is the single critical section; when it returns False the
        route surfaces 409 and NO partial writes land.
        """
        org_id = self._bootstrap_owner(uid="owner-b1w", slug="b1w-church", name="B1w Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b1w"
        billing["stripeSubscriptionId"] = "sub_b1w"
        billing["status"] = "active"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        now_ts = int(time.time())
        event = {
            "id": "evt_b1w", "type": "customer.subscription.updated",
            "data": {"object": {
                "id": "sub_b1w", "customer": "cus_b1w", "status": "past_due",
                "cancel_at_period_end": False,
                "current_period_start": now_ts,
                "current_period_end": now_ts + 30 * 86400,
                "metadata": {"orgId": org_id, "planKey": "starter"},
                "items": {"data": [{"price": {"id": "price_starter"}}]},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        sig = _sign_stripe_payload(raw, "whsec_audit")

        # Patch the fenced apply to simulate "another delivery took
        # over after our claim" — the sibling fenced-token check
        # inside the method returns a 'claim_lost' refusal.
        with patch.object(
            self.store, "apply_webhook_event_atomic",
            side_effect=lambda **_: {"applied": False, "reason": "claim_lost"},
        ):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(
                    billing_routes.stripe_webhook(
                        request=_DummyRequest(raw), stripe_signature=sig,
                    )
                )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "webhook_claim_lost")
        # Billing was NOT overwritten by the aborted handler.
        final = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(final.get("status"), "active")


class B3_CheckoutLateResponseDoesNotOverwriteReplacement(_AuditBase):
    """Boundary 3 — checkout ownership. A late Stripe response that
    carries attempt A's session info must NOT overwrite the pending
    record if attempt B has already replaced it.
    """

    def test_record_checkout_attempt_session_only_updates_matching_attempt(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b3", slug="b3-church", name="B3 Church")
        # First attempt — Starter.
        first = self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
        )
        self.assertEqual(first["action"], "create")
        attempt_a = first["attemptId"]
        # Expire attempt A's record (simulate TTL expiry) so attempt B
        # can be created for a different plan.
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["pendingCheckoutAttempt"]["expiresAt"] = _utc_now() - timedelta(seconds=1)
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)
        # Second attempt — Growth, replaces attempt A.
        second = self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="growth", interval="month",
        )
        self.assertEqual(second["action"], "create")
        attempt_b = second["attemptId"]
        self.assertNotEqual(attempt_a, attempt_b)

        # Simulate attempt A's Stripe response arriving LATE — it
        # tries to record its session info. The guard must prevent
        # it from overwriting attempt B's slot.
        self.store.record_checkout_attempt_session(
            org_id=org_id,
            attempt_id=attempt_a,
            session_id="cs_late_from_a",
            session_url="https://late.a/",
        )
        pending = self.store.get_org_billing_profile(org_id=org_id).get("pendingCheckoutAttempt") or {}
        self.assertEqual(
            pending.get("attemptId"), attempt_b,
            "attempt B still owns the slot",
        )
        self.assertIsNone(
            pending.get("sessionId"),
            "late response from A must not populate B's session fields",
        )


class B4_PostCompletionGapRefusesSecondCheckout(_AuditBase):
    """Boundary 4 — checkout lifecycle. After `checkout.session.completed`
    sets `stripeSubscriptionId` but BEFORE `customer.subscription.*`
    syncs the status, a user who re-POSTs /billing/checkout-session
    must be refused. Pre-fix: a prior-canceled customer could slip
    through because the `(stripeSubscriptionId AND status∈{active,
    past_due, trialing})` guard didn't fire with status=canceled.
    Post-fix: `checkout.session.completed` sets status to `trialing`
    as an intermediate marker if the current status is not already
    one of the active-shaped values.
    """

    def test_rapid_second_checkout_after_completion_is_refused(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b4", slug="b4-church", name="B4 Church")
        # Seed as a previously-canceled customer (dead subId remains).
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b4"
        billing["stripeSubscriptionId"] = "sub_old_canceled"
        billing["status"] = "canceled"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)
        # Sanity: canceled customer CAN start a new checkout.
        first = billing_routes.create_checkout_session(
            billing_routes.CheckoutSessionRequest(
                orgId=org_id, planKey="growth",
                successUrl="https://example.com/ok",
                cancelUrl="https://example.com/cancel",
                interval="month",
            ),
            user=self._user("owner-b4"),
        )
        self.assertIn("url", first)
        # User completes the new checkout. Simulate `checkout.session.completed`:
        event = {
            "id": "evt_b4_checkout", "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_b4", "customer": "cus_b4", "subscription": "sub_new",
                "metadata": {"orgId": org_id, "planKey": "growth", "interval": "month"},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        # Immediately re-POST (user double-clicks "Subscribe"). The
        # customer.subscription.* event hasn't landed yet. The guard
        # must still refuse to create a SECOND Stripe subscription.
        with self.assertRaises(HTTPException) as ctx:
            billing_routes.create_checkout_session(
                billing_routes.CheckoutSessionRequest(
                    orgId=org_id, planKey="growth",
                    successUrl="https://example.com/ok",
                    cancelUrl="https://example.com/cancel",
                    interval="month",
                ),
                user=self._user("owner-b4"),
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "subscription_already_active")


class B5_SwallowedPlanLimitsFailureNowRecoverable(_AuditBase):
    """Boundary 5 — required persistence. If
    `set_org_monthly_minutes_limit` fails, the event must NOT be
    marked completed — otherwise Stripe's retry would be deduped and
    the plan-limits inconsistency would be permanent.

    Pre-fix: a bare try/except Exception around the call swallowed
    all failures and the webhook continued to persist billing + mark
    the event completed.

    Post-fix: non-`org_not_found` failures propagate as HTTP 500 so
    Stripe retries.
    """

    def test_apply_failure_is_not_swallowed_followed_by_completion(self) -> None:
        """Any failure inside `apply_webhook_event_atomic` (billing
        profile write OR plan-limit write) must propagate so the
        event does NOT get completed. Stripe's retry (after lease
        aging) reruns the handler cleanly."""
        org_id = self._bootstrap_owner(uid="owner-b5", slug="b5-church", name="B5 Church")

        now_ts = int(time.time())
        event = {
            "id": "evt_b5_checkout", "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_b5", "customer": "cus_b5", "subscription": "sub_b5",
                "metadata": {"orgId": org_id, "planKey": "growth", "interval": "month"},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        sig = _sign_stripe_payload(raw, "whsec_audit")

        def _flaky_apply(**kwargs: Any) -> bool:
            raise RuntimeError("firestore_unavailable")

        with patch.object(self.store, "apply_webhook_event_atomic", side_effect=_flaky_apply):
            with self.assertRaises(Exception):
                asyncio.run(
                    billing_routes.stripe_webhook(
                        request=_DummyRequest(raw), stripe_signature=sig,
                    )
                )
        rec = self.store._billing_events.get("evt_b5_checkout") or {}
        self.assertNotEqual(rec.get("status"), "completed",
                            "event marked completed despite failed apply")


class B4b_LateArrivalCheckoutDoesNotResurrectAccess(_AuditBase):
    """Second-review correction (issue #2): a delayed
    `checkout.session.completed` for a now-canceled subscription
    must NOT fabricate `status=trialing` to block a second checkout;
    doing so grants service-start to a canceled account. The fix
    uses a separate `pendingSubscriptionConfirmation` flag and
    short-circuits the handler for late terminal arrivals.
    """

    entitlements_v2_enabled = True  # exercise the real entitlement-start path

    def test_late_arrival_does_not_grant_service_access(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b4b", slug="b4b-church", name="B4b Church")
        # Seed: subscription was deleted — status=canceled, stripeSubId still on record.
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b4b"
        billing["stripeSubscriptionId"] = "sub_b4b"
        billing["status"] = "canceled"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        # Delayed checkout.session.completed for the SAME sub_b4b.
        event = {
            "id": "evt_b4b_late", "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_b4b", "customer": "cus_b4b", "subscription": "sub_b4b",
                "metadata": {"orgId": org_id, "planKey": "starter", "interval": "month"},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        result = asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        self.assertTrue(result.get("lateArrival"), result)
        final = self.store.get_org_billing_profile(org_id=org_id)
        # NO status flip: canceled stays canceled.
        self.assertEqual(final.get("status"), "canceled",
                         "fabricated trial status would resurrect access")
        self.assertFalse(bool(final.get("pendingSubscriptionConfirmation")))

        # Try to start a service — must be denied (status is canceled).
        self.store._services[(org_id, "sun-11am")] = {
            "title": "Sunday 11am", "timezone": "America/Chicago",
            "rrule": None, "defaultLanguagePair": {"source": "ko", "target": "en"},
            "activeRoomId": None, "lastRoomId": None, "updatedAt": _utc_now(),
        }
        with self.assertRaises(PermissionError):
            self.store.start_service(
                org_id=org_id, service_key="sun-11am",
                host_uid="owner-b4b", source="ko", target="en",
            )


class B4c_LateArrivalDoesNotEraseReplacementAttempt(_AuditBase):
    """Second-review correction (issue #2): a delayed completion for
    an OLD subscription must not erase a REPLACEMENT pending-checkout
    attempt (plan/interval differ)."""

    def test_late_completion_preserves_replacement_attempt(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b4c", slug="b4c-church", name="B4c Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b4c"
        billing["stripeSubscriptionId"] = "sub_old"
        billing["status"] = "canceled"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)
        # The user is now attempting a NEW Growth subscription — a
        # pending attempt exists for Growth.
        self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="growth", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="owner-b4c",
        )
        pending_before = self.store.get_org_billing_profile(org_id=org_id).get("pendingCheckoutAttempt")
        self.assertEqual(pending_before.get("planKey"), "growth")

        # Delayed checkout.session.completed for the OLD sub (Starter).
        event = {
            "id": "evt_b4c_late", "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_b4c_old", "customer": "cus_b4c", "subscription": "sub_old",
                "metadata": {"orgId": org_id, "planKey": "starter", "interval": "month"},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        pending_after = self.store.get_org_billing_profile(org_id=org_id).get("pendingCheckoutAttempt")
        self.assertIsNotNone(pending_after,
                             "replacement attempt was erased by late terminal arrival")
        self.assertEqual(pending_after.get("planKey"), "growth")


class B4d_LegacyEventRecordTreatedAsDeduped(_AuditBase):
    """Second-review correction (issue #3): legacy billingEvents docs
    that lack `status`/`claimedAt` fields must be treated as
    completed to avoid implicit replay."""

    def test_legacy_record_in_memory_is_deduped(self) -> None:
        # Manually seed a legacy-shaped record (no `status`, no `claimedAt`).
        self.store._billing_events["evt_legacy"] = {
            "eventId": "evt_legacy",
            "eventType": "customer.subscription.updated",
            "payload": {"type": "customer.subscription.updated"},
            "createdAt": _utc_now(),
            # NB: no "status", no "claimedAt", no "claimToken"
        }
        r = self.store.try_claim_billing_event(
            event_id="evt_legacy", event_type="customer.subscription.updated",
            payload={"type": "customer.subscription.updated"},
        )
        self.assertEqual(r.get("status"), "deduped")


class B4e_CheckoutAttemptStableParams(_AuditBase):
    """Second-review correction (issue #4): same (plan, interval) with
    DIFFERENT success_url / cancel_url / requested_by_uid must NOT
    reuse the stored Idempotency-Key (Stripe would reject). Refuse
    with 409 pending_checkout_params_mismatch."""

    def test_different_requested_by_uid_refuses_reuse(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b4e-a", slug="b4e-church", name="B4e Church")
        # First admin creates an attempt.
        self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="owner-b4e-a",
        )
        # Second admin (different uid) attempts same plan+interval.
        result = self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="owner-b4e-b",
        )
        self.assertEqual(result.get("action"), "pending_params_mismatch")

    def test_different_success_url_refuses_reuse(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b4e2", slug="b4e2-church", name="B4e2 Church")
        self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="owner-b4e2",
        )
        result = self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://y/ok", cancel_url="https://x/cancel",
            requested_by_uid="owner-b4e2",
        )
        self.assertEqual(result.get("action"), "pending_params_mismatch")


class B4f_StripeIdempotencyContractEnforced(_AuditBase):
    """Second-review correction (issue #4): add a realistic mock of
    Stripe's Idempotency-Key semantics (same key + different params =
    IdempotencyError). The route refuses the mismatched retry at the
    claim step, so the Stripe mock never sees two differing requests
    for the same key — duplicate-purchase protection stays intact.
    """

    def test_mismatched_retry_refused_before_reaching_stripe(self) -> None:
        seen: Dict[str, Dict[str, Any]] = {}

        class _IdempotentClient(_FakeStripeClient):
            def create_checkout_session(self_inner, **kwargs: Any) -> Dict[str, Any]:
                key = kwargs.get("idempotency_key") or ""
                if key:
                    prior = seen.get(key)
                    if prior is None:
                        resp = super().create_checkout_session(**kwargs)
                        # Snapshot the request parameters that MUST
                        # stay stable under this key.
                        seen[key] = {
                            "price_id": kwargs.get("price_id"),
                            "success_url": kwargs.get("success_url"),
                            "cancel_url": kwargs.get("cancel_url"),
                            "customer_id": kwargs.get("customer_id"),
                            "response": resp,
                        }
                        return resp
                    for field in ("price_id", "success_url", "cancel_url", "customer_id"):
                        if kwargs.get(field) != prior.get(field):
                            # Realistic Stripe behaviour on key reuse
                            # with changed body.
                            raise StripeClientError(
                                f"stripe_http_400:Idempotent request conflict on key={key} field={field}"
                            )
                    return prior["response"]
                return super().create_checkout_session(**kwargs)

        with patch.object(billing_routes, "_stripe_client", lambda: _IdempotentClient()):
            org_id = self._bootstrap_owner(uid="owner-b4f", slug="b4f-church", name="B4f Church")
            first = billing_routes.create_checkout_session(
                billing_routes.CheckoutSessionRequest(
                    orgId=org_id, planKey="starter",
                    successUrl="https://example.com/ok",
                    cancelUrl="https://example.com/cancel",
                    interval="month",
                ),
                user=self._user("owner-b4f"),
            )
            self.assertIn("url", first)
            # Same user retries with DIFFERENT success/cancel URLs
            # (e.g. the browser navigated to a different sub-page).
            # The claim step refuses the mismatch so the mocked
            # Stripe client never sees a key-reuse with changed
            # parameters.
            with self.assertRaises(HTTPException) as ctx:
                billing_routes.create_checkout_session(
                    billing_routes.CheckoutSessionRequest(
                        orgId=org_id, planKey="starter",
                        successUrl="https://example.com/ok-different",
                        cancelUrl="https://example.com/cancel",
                        interval="month",
                    ),
                    user=self._user("owner-b4f"),
                )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "pending_checkout_params_mismatch")


class B2_StateReconciliationNotWatermark(_AuditBase):
    """Review-pass v5 fix for issue #1 (reviewer scenario B):
    old-payment-failed retry does NOT regress a recovered
    subscription, because the handler reconciles against the
    SUBSCRIPTION's current state (Stripe retrieve, locally mocked)
    rather than any global event-created watermark.

    Scenario: a `payment_failed` for `sub_b2i` arrives after an
    `invoice.payment_succeeded` already restored the subscription to
    active. The failed handler retrieves the subscription, sees
    `status=active`, and no-ops. Status stays active.
    """

    def test_payment_failed_after_recovery_does_not_regress(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b2i", slug="b2i-church", name="B2i Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b2i"
        billing["stripeSubscriptionId"] = "sub_b2i"
        billing["status"] = "active"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        # Stripe retrieve returns active — the subscription IS
        # recovered. Older payment_failed retry must not override.
        class _ActiveSubClient(_FakeStripeClient):
            def retrieve_subscription(self_inner, *, subscription_id: str) -> Dict[str, Any]:
                now_ts = int(time.time())
                return {
                    "id": subscription_id, "customer": "cus_b2i",
                    "status": "active",
                    "cancel_at_period_end": False,
                    "current_period_start": now_ts,
                    "current_period_end": now_ts + 30 * 86400,
                    "items": {"data": [{"id": "si_x", "price": {"id": "price_starter"}}]},
                }

        with patch.object(billing_routes, "_stripe_client", lambda: _ActiveSubClient()):
            event = {
                "id": "evt_b2i_fail", "type": "invoice.payment_failed",
                "data": {"object": {
                    "id": "in_b2i_fail", "customer": "cus_b2i",
                    "subscription": "sub_b2i",
                    "metadata": {"orgId": org_id},
                }},
            }
            raw = json.dumps(event).encode("utf-8")
            asyncio.run(
                billing_routes.stripe_webhook(
                    request=_DummyRequest(raw),
                    stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
                )
            )
        final = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(final.get("status"), "active",
                         "payment_failed retry regressed subscription that Stripe reports as active")
        self.assertIsNone(final.get("graceEndsAt"))

    def test_equal_timestamp_success_then_failure_no_global_discard(self) -> None:
        """Reviewer scenario B: successful payment and failed payment
        share the same `created` second. Deliver success, then the
        older failure. With a watermark, the older failure would
        either (a) be discarded with `<` or (b) with `<=` discard
        legitimate same-second updates. The v5 fix is state
        reconciliation: the failure retrieves the subscription,
        sees active, and no-ops. Status stays active.
        """
        org_id = self._bootstrap_owner(uid="owner-b2eq", slug="b2eq-church", name="B2eq Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b2eq"
        billing["stripeSubscriptionId"] = "sub_b2eq"
        billing["status"] = "past_due"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)
        same_ts = int(time.time())

        class _ActiveSubClient(_FakeStripeClient):
            def retrieve_subscription(self_inner, *, subscription_id: str) -> Dict[str, Any]:
                return {
                    "id": subscription_id, "customer": "cus_b2eq",
                    "status": "active",
                    "cancel_at_period_end": False,
                    "current_period_start": same_ts,
                    "current_period_end": same_ts + 30 * 86400,
                    "items": {"data": [{"id": "si_x", "price": {"id": "price_starter"}}]},
                }

        with patch.object(billing_routes, "_stripe_client", lambda: _ActiveSubClient()):
            # Success first.
            ok_evt = {
                "id": "evt_b2eq_ok", "type": "invoice.payment_succeeded",
                "created": same_ts,
                "data": {"object": {
                    "id": "in_ok", "customer": "cus_b2eq",
                    "subscription": "sub_b2eq",
                    "metadata": {"orgId": org_id},
                }},
            }
            raw_ok = json.dumps(ok_evt).encode("utf-8")
            asyncio.run(
                billing_routes.stripe_webhook(
                    request=_DummyRequest(raw_ok),
                    stripe_signature=_sign_stripe_payload(raw_ok, "whsec_audit"),
                )
            )
            self.assertEqual(
                self.store.get_org_billing_profile(org_id=org_id).get("status"),
                "active",
            )
            # Then the older failure, SAME timestamp.
            fail_evt = {
                "id": "evt_b2eq_fail", "type": "invoice.payment_failed",
                "created": same_ts,
                "data": {"object": {
                    "id": "in_fail", "customer": "cus_b2eq",
                    "subscription": "sub_b2eq",
                    "metadata": {"orgId": org_id},
                }},
            }
            raw_fail = json.dumps(fail_evt).encode("utf-8")
            asyncio.run(
                billing_routes.stripe_webhook(
                    request=_DummyRequest(raw_fail),
                    stripe_signature=_sign_stripe_payload(raw_fail, "whsec_audit"),
                )
            )
        self.assertEqual(
            self.store.get_org_billing_profile(org_id=org_id).get("status"),
            "active",
            "same-second payment_failed stomped on recovered state",
        )

    def test_subscription_created_before_checkout_completion_is_not_discarded(self) -> None:
        """Reviewer scenario A: subscription.created at T=100,
        checkout.session.completed at T=110 delivered FIRST, then
        subscription.created arrives. With a watermark, the older
        subscription.created would be discarded. With state
        reconciliation, both events apply correctly."""
        org_id = self._bootstrap_owner(uid="owner-b2a", slug="b2a-church", name="B2a Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b2a"
        billing["stripeSubscriptionId"] = "sub_b2a_old"
        billing["status"] = "canceled"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        # First deliver checkout.session.completed for the new sub.
        completed_ts = int(time.time()) + 10
        created_ts = completed_ts - 10
        checkout_evt = {
            "id": "evt_b2a_ck", "type": "checkout.session.completed",
            "created": completed_ts,
            "data": {"object": {
                "id": "cs_b2a", "customer": "cus_b2a", "subscription": "sub_b2a_new",
                "metadata": {"orgId": org_id, "planKey": "growth", "interval": "month"},
            }},
        }
        # Note: the "obsolete-subscription" guard in
        # checkout.session.completed looks at stored_sub == incoming_sub +
        # active-shaped-status; here stored is sub_b2a_old / canceled,
        # incoming is sub_b2a_new → is_obsolete_subscription_arrival
        # is False and is_terminal_late_arrival is False too (sub
        # ids differ). The handler proceeds with the normal
        # completion path.
        raw_ck = json.dumps(checkout_evt).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw_ck),
                stripe_signature=_sign_stripe_payload(raw_ck, "whsec_audit"),
            )
        )
        after_ck = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(after_ck.get("stripeSubscriptionId"), "sub_b2a_new")
        self.assertTrue(bool(after_ck.get("pendingSubscriptionConfirmation")))

        # Now the OLDER subscription.created arrives. Watermark would
        # discard it; reconciliation (Stripe retrieve returns the
        # same trialing-growth state) applies it.
        now_ts = int(time.time())
        sub_state = {
            "id": "sub_b2a_new", "customer": "cus_b2a", "status": "trialing",
            "cancel_at_period_end": False,
            "current_period_start": now_ts,
            "current_period_end": now_ts + 30 * 86400,
            "metadata": {"orgId": org_id, "planKey": "growth"},
            "trial_end": now_ts + 7 * 86400,
            "items": {"data": [{"price": {"id": "price_growth"}}]},
        }
        self.fake_client.sub_states["sub_b2a_new"] = sub_state
        sub_evt = {
            "id": "evt_b2a_sub", "type": "customer.subscription.created",
            "created": created_ts,  # OLDER than the checkout_evt
            "data": {"object": sub_state},
        }
        raw_sub = json.dumps(sub_evt).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw_sub),
                stripe_signature=_sign_stripe_payload(raw_sub, "whsec_audit"),
            )
        )
        final = self.store.get_org_billing_profile(org_id=org_id)
        # Subscription-created merged: trialing status set.
        self.assertEqual(final.get("status"), "trialing")
        # pendingSubscriptionConfirmation cleared by customer.subscription.*.
        self.assertFalse(bool(final.get("pendingSubscriptionConfirmation")))

    def test_obsolete_noop_does_not_discard_later_current_event(self) -> None:
        """Reviewer scenario C: an obsolete-subscription event
        correctly no-ops. With a watermark it would ALSO advance the
        watermark and later discard a legitimate event with a
        slightly older `created` time. With state reconciliation
        (no watermark) the legitimate current-subscription event
        applies normally.
        """
        org_id = self._bootstrap_owner(uid="owner-b2c", slug="b2c-church", name="B2c Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b2c"
        billing["stripeSubscriptionId"] = "sub_current"
        billing["status"] = "past_due"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)
        obs_ts = int(time.time()) + 60
        current_ts = obs_ts - 30  # legitimate event with EARLIER created

        # 1) Obsolete event — processed as no-op.
        obs_evt = {
            "id": "evt_b2c_obs", "type": "checkout.session.completed",
            "created": obs_ts,
            "data": {"object": {
                "id": "cs_obsolete", "customer": "cus_b2c", "subscription": "sub_obsolete",
                "metadata": {"orgId": org_id, "planKey": "starter", "interval": "month"},
            }},
        }
        raw_obs = json.dumps(obs_evt).encode("utf-8")
        # Need an active-shaped current status for the obsolete guard
        # to fire — set status to "active" momentarily.
        billing_a = self.store.get_org_billing_profile(org_id=org_id)
        billing_a["status"] = "active"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing_a)
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw_obs),
                stripe_signature=_sign_stripe_payload(raw_obs, "whsec_audit"),
            )
        )

        # 2) Legitimate payment_succeeded for the CURRENT sub, with
        # an EARLIER created timestamp. Must apply.
        # Flip status back to past_due so the recovery path fires.
        billing_b = self.store.get_org_billing_profile(org_id=org_id)
        billing_b["status"] = "past_due"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing_b)

        class _ActiveSubClient(_FakeStripeClient):
            def retrieve_subscription(self_inner, *, subscription_id: str) -> Dict[str, Any]:
                return {
                    "id": subscription_id, "customer": "cus_b2c",
                    "status": "active",
                    "cancel_at_period_end": False,
                    "current_period_start": current_ts,
                    "current_period_end": current_ts + 30 * 86400,
                    "items": {"data": [{"id": "si_x", "price": {"id": "price_starter"}}]},
                }

        ok_evt = {
            "id": "evt_b2c_ok", "type": "invoice.payment_succeeded",
            "created": current_ts,  # earlier than obs_ts
            "data": {"object": {
                "id": "in_current", "customer": "cus_b2c",
                "subscription": "sub_current",
                "metadata": {"orgId": org_id},
            }},
        }
        raw_ok = json.dumps(ok_evt).encode("utf-8")
        with patch.object(billing_routes, "_stripe_client", lambda: _ActiveSubClient()):
            asyncio.run(
                billing_routes.stripe_webhook(
                    request=_DummyRequest(raw_ok),
                    stripe_signature=_sign_stripe_payload(raw_ok, "whsec_audit"),
                )
            )
        self.assertEqual(
            self.store.get_org_billing_profile(org_id=org_id).get("status"),
            "active",
            "legitimate recovery event with earlier `created` was discarded by a watermark",
        )


class B6_LateCompletionDoesNotEraseReplacementBySessionId(_AuditBase):
    """Review-pass fix for issue #2: identity-based match of pending
    attempt. Two attempts for the same (plan, interval) with different
    session ids — the late completion for session_A must NOT erase
    session_B's pending record.
    """

    def test_late_completion_for_sibling_attempt_preserves_replacement(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b6", slug="b6-church", name="B6 Church")
        # Attempt A — Starter.
        self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="owner-b6",
        )
        # Record a Stripe session for A.
        pending_before = self.store.get_org_billing_profile(org_id=org_id).get("pendingCheckoutAttempt")
        self.store.record_checkout_attempt_session(
            org_id=org_id, attempt_id=pending_before["attemptId"],
            session_id="cs_A", session_url="https://a/",
        )
        # Expire A so B can take its place for the same plan+interval.
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["pendingCheckoutAttempt"]["expiresAt"] = _utc_now() - timedelta(seconds=1)
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)
        # Attempt B — same Starter plan but a new logical attempt.
        self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="owner-b6",
        )
        pending_b = self.store.get_org_billing_profile(org_id=org_id).get("pendingCheckoutAttempt")
        self.store.record_checkout_attempt_session(
            org_id=org_id, attempt_id=pending_b["attemptId"],
            session_id="cs_B", session_url="https://b/",
        )

        # Delayed completion for session cs_A (an obsolete / abandoned attempt).
        event = {
            "id": "evt_b6_late", "type": "checkout.session.completed",
            "created": int(time.time()),
            "data": {"object": {
                "id": "cs_A", "customer": "cus_b6", "subscription": "sub_from_A",
                "metadata": {"orgId": org_id, "planKey": "starter", "interval": "month"},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        after = self.store.get_org_billing_profile(org_id=org_id).get("pendingCheckoutAttempt")
        self.assertIsNotNone(after,
                             "replacement B was erased by late completion for session cs_A")
        self.assertEqual(after.get("sessionId"), "cs_B")


class B7_ObsoleteSubscriptionDoesNotOverwriteCurrent(_AuditBase):
    """Review-pass fix for issue #2: terminal-arrival / obsolete
    reconciliation must guard based on BOTH subscription id AND
    identity. A delayed `customer.subscription.*` for an obsolete
    subscription must not overwrite the current one.
    """

    def test_subscription_event_for_obsolete_sub_leaves_current_untouched(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b7", slug="b7-church", name="B7 Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b7"
        billing["stripeSubscriptionId"] = "sub_current"
        billing["status"] = "active"
        billing["planKey"] = "growth"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        # Delayed subscription event for an OBSOLETE sub.
        now_ts = int(time.time())
        event = {
            "id": "evt_b7_obs", "type": "customer.subscription.updated",
            "created": now_ts,
            "data": {"object": {
                "id": "sub_obsolete", "customer": "cus_b7", "status": "past_due",
                "cancel_at_period_end": False,
                "current_period_start": now_ts,
                "current_period_end": now_ts + 30 * 86400,
                "metadata": {"orgId": org_id, "planKey": "starter"},
                "items": {"data": [{"price": {"id": "price_starter"}}]},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        result = asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        self.assertTrue(result.get("obsoleteSubscription"), result)
        final = self.store.get_org_billing_profile(org_id=org_id)
        # Current subscription id unchanged.
        self.assertEqual(final.get("stripeSubscriptionId"), "sub_current")
        self.assertEqual(final.get("status"), "active")
        self.assertEqual(final.get("planKey"), "growth")


class B13_DelayedCheckoutCompletionDoesNotRevertUpgrade(_AuditBase):
    """Review-pass v7 fix (gap #1): a delayed
    `checkout.session.completed` for an already-active subscription
    must NOT downgrade the plan based on its stale `metadata.planKey`.
    The handler reconciles against the subscription's CURRENT state
    (via Stripe retrieve) and uses that plan key.
    """

    def test_delayed_checkout_for_current_sub_does_not_revert_growth(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b13", slug="b13-church", name="B13 Church")
        # Current state: Growth subscription (via a prior subscription.updated).
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b13"
        billing["stripeSubscriptionId"] = "sub_b13"
        billing["status"] = "active"
        billing["planKey"] = "growth"
        billing["priceId"] = "price_growth"
        billing.setdefault("limits", {})["maxServiceKeys"] = 12
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)
        # Stripe says: still Growth.
        self.fake_client.sub_states["sub_b13"] = {
            "id": "sub_b13", "customer": "cus_b13", "status": "active",
            "cancel_at_period_end": False,
            "current_period_start": int(time.time()),
            "current_period_end": int(time.time()) + 86400,
            "metadata": {"orgId": org_id, "planKey": "growth"},
            "items": {"data": [{"price": {"id": "price_growth"}}]},
        }
        # Delayed completion for the ORIGINAL (starter) checkout session.
        event = {
            "id": "evt_b13_late_ck", "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_b13_original", "customer": "cus_b13",
                "subscription": "sub_b13",
                "metadata": {"orgId": org_id, "planKey": "starter", "interval": "month"},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        final = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(final.get("planKey"), "growth",
                         "stale checkout.session.completed reverted the Growth upgrade")
        self.assertEqual((final.get("limits") or {}).get("maxServiceKeys"), 12,
                         "limits.maxServiceKeys reverted to Starter cap")
        self.assertEqual(self.store._orgs[org_id].get("maxMinutesPerMonth"), 1800,
                         "org-level monthly-minute limit reverted to Starter")
        self.assertEqual(final.get("priceId"), "price_growth")

    def test_checkout_completion_reconciles_stale_priceId_too(self) -> None:
        """Review-pass v8 fix (gap #2): the checkout.session.completed
        handler must persist the reconciled `priceId` alongside the
        reconciled plan/limits. The B13 initial state already had
        `priceId=price_growth`, so a stale priceId bug was invisible
        there. Here we store the OPPOSITE: planKey/priceId=starter,
        then let the webhook retrieve current Growth state and apply
        the completion. All three (planKey, limits.maxServiceKeys,
        priceId) must land Growth."""
        org_id = self._bootstrap_owner(
            uid="owner-b13b", slug="b13b-church", name="B13b Church",
        )
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b13b"
        billing["stripeSubscriptionId"] = "sub_b13b"
        billing["status"] = "active"
        billing["planKey"] = "starter"
        billing["priceId"] = "price_starter"
        billing.setdefault("limits", {})["maxServiceKeys"] = 5
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)
        # Live subscription is Growth now (an unobserved subscription.updated).
        self.fake_client.sub_states["sub_b13b"] = {
            "id": "sub_b13b", "customer": "cus_b13b", "status": "active",
            "cancel_at_period_end": False,
            "current_period_start": int(time.time()),
            "current_period_end": int(time.time()) + 30 * 86400,
            "metadata": {"orgId": org_id, "planKey": "growth"},
            "items": {"data": [{"price": {"id": "price_growth"}}]},
        }
        event = {
            "id": "evt_b13b_ck", "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_b13b", "customer": "cus_b13b",
                "subscription": "sub_b13b",
                "metadata": {"orgId": org_id, "planKey": "starter", "interval": "month"},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        final = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(final.get("planKey"), "growth")
        self.assertEqual((final.get("limits") or {}).get("maxServiceKeys"), 12)
        self.assertEqual(final.get("priceId"), "price_growth",
                         "stored priceId still reflected the old plan")


class B14_HydrateReverseRaceCASGuard(_AuditBase):
    """Review-pass v7 fix (gap #2): a `_hydrate_billing_from_stripe`
    call reads old billing + retrieves Starter from Stripe. A webhook
    retrieves Growth and commits it. The hydrate finishes and tries
    to write Starter back. With CAS, the hydrate write is rejected
    (StaleBillingProfileError); hydrate returns the current (Growth)
    state instead of overwriting.

    This test uses real threads + a `threading.Event` barrier to
    pause the hydrate inside its Stripe retrieve, run the webhook to
    completion, then release the hydrate.
    """

    def test_stale_hydrate_cannot_overwrite_committed_webhook(self) -> None:
        import threading
        org_id = self._bootstrap_owner(uid="owner-b14", slug="b14-church", name="B14 Church")
        # Current state before the race: Starter (old).
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b14"
        billing["stripeSubscriptionId"] = "sub_b14"
        billing["status"] = "active"
        billing["planKey"] = "starter"
        billing["priceId"] = "price_starter"
        billing["currentPeriodStart"] = _utc_now() - timedelta(days=1)
        billing["currentPeriodEnd"] = _utc_now() + timedelta(days=29)
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)
        baseline_updated_at = self.store.get_org_billing_profile(org_id=org_id).get("updatedAt")

        pause_hydrate = threading.Event()
        release_hydrate = threading.Event()
        trace: list = []
        starter_sub = {
            "id": "sub_b14", "customer": "cus_b14", "status": "active",
            "cancel_at_period_end": False,
            "current_period_start": int(time.time()) - 86400,
            "current_period_end": int(time.time()) + 29 * 86400,
            "metadata": {"orgId": org_id, "planKey": "starter"},
            "items": {"data": [{"price": {"id": "price_starter"}}]},
        }
        growth_sub = {
            "id": "sub_b14", "customer": "cus_b14", "status": "active",
            "cancel_at_period_end": False,
            "current_period_start": int(time.time()),
            "current_period_end": int(time.time()) + 30 * 86400,
            "metadata": {"orgId": org_id, "planKey": "growth"},
            "items": {"data": [{"price": {"id": "price_growth"}}]},
        }
        # Both the hydrate helper and the webhook pull their Stripe
        # client via `billing_routes._stripe_client()`, which setUp
        # patches to a single shared `self.fake_client`. We can't
        # safely swap that patch per-thread (patch.object is process-
        # wide), so instead the fake's `retrieve_subscription` behaves
        # DIFFERENTLY per call based on a call-count:
        #   call 1 → hydrate: blocks on `release_hydrate`, returns Starter.
        #   call 2 → webhook: returns Growth immediately.
        call_count = {"n": 0}
        orig_retrieve = self.fake_client.retrieve_subscription

        def _staged_retrieve(*, subscription_id: str) -> Dict[str, Any]:
            call_count["n"] += 1
            n = call_count["n"]
            trace.append(("retrieve", n, subscription_id))
            if n == 1:
                pause_hydrate.set()
                release_hydrate.wait(timeout=5.0)
                return dict(starter_sub)
            return dict(growth_sub)

        self.fake_client.retrieve_subscription = _staged_retrieve  # type: ignore
        try:
            hydrate_result: Dict[str, Any] = {}

            def _hydrate_thread() -> None:
                try:
                    read_billing = self.store.get_org_billing_profile(org_id=org_id)
                    hydrate_result["returned"] = billing_routes._hydrate_billing_from_stripe(
                        org_id=org_id, billing=read_billing, force_refresh=True,
                    )
                except Exception as exc:
                    hydrate_result["error"] = repr(exc)

            t = threading.Thread(target=_hydrate_thread)
            t.start()
            self.assertTrue(pause_hydrate.wait(timeout=5.0),
                            f"hydrate did not reach retrieve_subscription; trace={trace}")

            # Webhook commits Growth while hydrate is paused.
            webhook_event = {
                "id": "evt_b14_webhook", "type": "customer.subscription.updated",
                "data": {"object": growth_sub},
            }
            raw = json.dumps(webhook_event).encode("utf-8")
            asyncio.run(
                billing_routes.stripe_webhook(
                    request=_DummyRequest(raw),
                    stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
                )
            )
            after_webhook = self.store.get_org_billing_profile(org_id=org_id)
            self.assertEqual(
                after_webhook.get("planKey"), "growth",
                f"webhook commit did not land before hydrate release; trace={trace}",
            )
            new_updated_at = after_webhook.get("updatedAt")
            self.assertNotEqual(new_updated_at, baseline_updated_at)

            # Release the hydrate. It computes Starter and tries to write.
            # CAS must reject.
            release_hydrate.set()
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive(), "hydrate thread hung")
            self.assertNotIn("error", hydrate_result, hydrate_result.get("error", ""))
        finally:
            self.fake_client.retrieve_subscription = orig_retrieve  # type: ignore

        final = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(final.get("planKey"), "growth",
                         "stale hydrate overwrote the committed Growth state")
        self.assertEqual(final.get("priceId"), "price_growth")
        # The hydrate's return value should reflect the CURRENT state
        # (post-webhook) rather than its own stale Starter snapshot.
        self.assertEqual(
            (hydrate_result.get("returned") or {}).get("planKey"), "growth",
            "stale hydrate returned its own stale snapshot instead of current state",
        )


class B15_CustomerCreateReverseRaceGuard(_AuditBase):
    """Review-pass v8 fix (gap #1): `_create_missing_stripe_customer`
    previously read the OLD billing snapshot, awaited Stripe's
    create_customer response (potentially slow), then wrote the full
    snapshot back. A `customer.subscription.created` webhook landing
    during that gap would be erased when the helper's write
    overwrote the whole billing dict with its stale Trial snapshot.

    This test uses real threads + a `threading.Event` barrier. The
    helper is paused inside `create_customer`, the webhook commits a
    Growth subscription (new sub id, status=active, priceId), then
    the helper resumes. After the helper completes, the stored
    billing must still carry the Growth fields; the only field the
    helper is allowed to add is `stripeCustomerId`.
    """

    def test_stale_customer_create_does_not_erase_committed_subscription(self) -> None:
        import threading
        org_id = self._bootstrap_owner(
            uid="owner-b15", slug="b15-church", name="B15 Church",
        )
        # Starting state: Trial with NO Stripe customer and no subscription.
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["planKey"] = "trial"
        billing["status"] = "trialing"
        billing["stripeCustomerId"] = None
        billing["stripeSubscriptionId"] = None
        billing["priceId"] = None
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        pause_create = threading.Event()
        release_create = threading.Event()
        trace: list = []

        # Shared fake client (self.fake_client), already patched by setUp.
        orig_create = self.fake_client.create_customer

        def _slow_create_customer(**kwargs: Any) -> Dict[str, Any]:
            trace.append(("create_customer enter", kwargs.get("email")))
            pause_create.set()
            release_create.wait(timeout=5.0)
            trace.append(("create_customer exit",))
            return {
                "id": "cus_b15",
                "email": kwargs.get("email"),
                "name": kwargs.get("name"),
            }

        self.fake_client.create_customer = _slow_create_customer  # type: ignore

        helper_result: Dict[str, Any] = {}

        def _helper_thread() -> None:
            try:
                stripe_customer_id, saved = billing_routes._create_missing_stripe_customer(
                    client=self.fake_client,
                    org_id=org_id,
                    billing=self.store.get_org_billing_profile(org_id=org_id),
                    user=AuthenticatedUser(
                        uid="owner-b15", email="o@x.com", displayName="O",
                    ),
                )
                helper_result["returned"] = (stripe_customer_id, saved)
            except Exception as exc:
                helper_result["error"] = repr(exc)

        try:
            t = threading.Thread(target=_helper_thread)
            t.start()
            self.assertTrue(
                pause_create.wait(timeout=5.0),
                f"helper did not reach create_customer; trace={trace}",
            )

            # Webhook commits Growth while helper is paused.
            growth_sub = {
                "id": "sub_b15",
                "customer": "cus_b15",
                "status": "active",
                "cancel_at_period_end": False,
                "current_period_start": int(time.time()),
                "current_period_end": int(time.time()) + 30 * 86400,
                "metadata": {"orgId": org_id, "planKey": "growth"},
                "items": {"data": [{"price": {"id": "price_growth"}}]},
            }
            self.fake_client.sub_states["sub_b15"] = growth_sub
            webhook_event = {
                "id": "evt_b15",
                "type": "customer.subscription.created",
                "data": {"object": growth_sub},
            }
            raw = json.dumps(webhook_event).encode("utf-8")
            asyncio.run(
                billing_routes.stripe_webhook(
                    request=_DummyRequest(raw),
                    stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
                )
            )
            after_webhook = self.store.get_org_billing_profile(org_id=org_id)
            self.assertEqual(
                after_webhook.get("planKey"), "growth",
                f"webhook did not commit Growth before helper release; trace={trace}",
            )
            self.assertEqual(after_webhook.get("stripeSubscriptionId"), "sub_b15")

            # Release the helper. It writes stripeCustomerId.
            release_create.set()
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive(), "helper thread hung")
            self.assertNotIn("error", helper_result, helper_result.get("error", ""))

            final = self.store.get_org_billing_profile(org_id=org_id)
            # The helper's addition.
            self.assertEqual(final.get("stripeCustomerId"), "cus_b15")
            # The webhook's commit MUST survive.
            self.assertEqual(final.get("planKey"), "growth",
                             f"customer-create overwrote Growth; trace={trace}")
            self.assertEqual(final.get("stripeSubscriptionId"), "sub_b15",
                             "customer-create erased stripeSubscriptionId")
            self.assertEqual(final.get("priceId"), "price_growth",
                             "customer-create erased priceId")
            self.assertEqual(
                str(final.get("status") or "").strip().lower(), "active",
                "customer-create reverted status to trialing",
            )
        finally:
            self.fake_client.create_customer = orig_create  # type: ignore


class B16_NormalizedRefsPatchIsPerFieldAndConditional(_AuditBase):
    """Review-pass v9 fix (gap #1): `_normalize_billing_refs` /
    `_persist_normalized_refs` previously patched ALL three ref
    fields unconditionally from the (possibly old) snapshot. If
    only one field needed formatting cleanup and another (e.g.
    priceId) had already been updated by a concurrent webhook, the
    unchanged old value was still submitted and reverted the
    webhook's write.

    The persister now submits ONLY the fields that actually needed
    correction, AND each is `expected=` the pre-correction stored
    value — so if a concurrent writer has since changed even the
    corrected field, the patch is abandoned.
    """

    def test_only_corrected_fields_are_submitted(self) -> None:
        org_id = self._bootstrap_owner(
            uid="owner-b16a", slug="b16a-church", name="B16a Church",
        )
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "  cus_b16a  "  # formatting cleanup
        billing["stripeSubscriptionId"] = "sub_b16a"
        billing["priceId"] = "price_starter"
        billing["planKey"] = "starter"
        billing["status"] = "active"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        # Meanwhile the webhook commits a Growth update, including
        # a new priceId. Simulate that commit via the SAME helper
        # the real webhook would use.
        self.store.patch_org_billing_fields(
            org_id=org_id,
            updates={
                "planKey": "growth", "priceId": "price_growth",
                "limits": {"maxServiceKeys": 12},
            },
        )

        # Now the "stale" normalizer runs on the OLD read.
        from app.routes import billing as billing_routes
        _, corrections, observed = billing_routes._normalize_billing_refs(billing=billing)
        self.assertEqual(set(corrections.keys()), {"stripeCustomerId"},
                         "normalizer should only correct the whitespace customer id")
        self.assertEqual(corrections["stripeCustomerId"], "cus_b16a")
        self.assertEqual(observed["stripeCustomerId"], "  cus_b16a  ")

        billing_routes._persist_normalized_refs(
            org_id=org_id, corrections=corrections, observed=observed,
        )
        final = self.store.get_org_billing_profile(org_id=org_id)
        # The customer id correction landed:
        self.assertEqual(final.get("stripeCustomerId"), "cus_b16a")
        # The webhook's Growth priceId SURVIVED (previously regressed to price_starter):
        self.assertEqual(final.get("priceId"), "price_growth",
                         "normalize reverted priceId to the stale pre-webhook value")
        self.assertEqual(final.get("planKey"), "growth")
        self.assertEqual((final.get("limits") or {}).get("maxServiceKeys"), 12)

    def test_patch_abandoned_when_corrected_field_itself_changed(self) -> None:
        """If even the field being corrected has since been overwritten
        by another writer, the normalize patch must NOT win."""
        org_id = self._bootstrap_owner(
            uid="owner-b16b", slug="b16b-church", name="B16b Church",
        )
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "  cus_b16b_old  "
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        # Meanwhile the webhook replaces stripeCustomerId with a new id.
        self.store.patch_org_billing_fields(
            org_id=org_id, updates={"stripeCustomerId": "cus_b16b_new"},
        )
        from app.routes import billing as billing_routes
        _, corrections, observed = billing_routes._normalize_billing_refs(billing=billing)
        self.assertEqual(corrections.get("stripeCustomerId"), "cus_b16b_old")

        # Persister should bail (expected=  cus_b16b_old   current=cus_b16b_new).
        billing_routes._persist_normalized_refs(
            org_id=org_id, corrections=corrections, observed=observed,
        )
        final = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(final.get("stripeCustomerId"), "cus_b16b_new",
                         "stale normalize overwrote the webhook's newer customer id")


class B17_ClearStaleRefsVerifiesIdentity(_AuditBase):
    """Review-pass v9 fix (gap #2): `_clear_stale_stripe_refs` is
    invoked when a specific Stripe lookup returns "No such customer:
    cus_X". It must only clear the stored refs when the current
    stripeCustomerId STILL equals cus_X. If a concurrent webhook has
    already replaced the obsolete customer with a new valid one,
    clearing would erase valid state.

    Uses the real hydrate helper on a background thread + a
    `threading.Event` barrier for the interleaving.
    """

    def test_concurrent_webhook_new_customer_survives_cleanup(self) -> None:
        import threading
        from app.routes import billing as billing_routes
        org_id = self._bootstrap_owner(
            uid="owner-b17", slug="b17-church", name="B17 Church",
        )
        # Start with the OBSOLETE customer stored.
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_obsolete"
        billing["stripeSubscriptionId"] = None
        billing["priceId"] = None
        billing["planKey"] = "trial"
        billing["status"] = "trialing"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        pause_lookup = threading.Event()
        release_lookup = threading.Event()
        trace: list = []

        from app.billing.stripe_client import StripeClientError
        orig_list = self.fake_client.list_subscriptions

        def _slow_list_subs(**kwargs: Any) -> Dict[str, Any]:
            trace.append(("list_subs enter", kwargs.get("customer_id")))
            pause_lookup.set()
            release_lookup.wait(timeout=5.0)
            # After release, fail as "No such customer: cus_obsolete".
            trace.append(("list_subs raise",))
            raise StripeClientError("No such customer: cus_obsolete")

        self.fake_client.list_subscriptions = _slow_list_subs  # type: ignore

        hydrate_result: Dict[str, Any] = {}

        def _hydrate_thread() -> None:
            try:
                read_billing = self.store.get_org_billing_profile(org_id=org_id)
                hydrate_result["returned"] = billing_routes._hydrate_billing_from_stripe(
                    org_id=org_id, billing=read_billing, force_refresh=True,
                )
            except Exception as exc:
                hydrate_result["error"] = repr(exc)

        try:
            t = threading.Thread(target=_hydrate_thread)
            t.start()
            self.assertTrue(
                pause_lookup.wait(timeout=5.0),
                f"hydrate did not reach list_subscriptions; trace={trace}",
            )

            # Webhook commits a NEW customer + subscription + price:
            # Growth + active.
            growth_sub = {
                "id": "sub_new",
                "customer": "cus_new",
                "status": "active",
                "cancel_at_period_end": False,
                "current_period_start": int(time.time()),
                "current_period_end": int(time.time()) + 30 * 86400,
                "metadata": {"orgId": org_id, "planKey": "growth"},
                "items": {"data": [{"price": {"id": "price_growth"}}]},
            }
            self.fake_client.sub_states["sub_new"] = growth_sub
            webhook_event = {
                "id": "evt_b17",
                "type": "customer.subscription.created",
                "data": {"object": growth_sub},
            }
            raw = json.dumps(webhook_event).encode("utf-8")
            asyncio.run(
                billing_routes.stripe_webhook(
                    request=_DummyRequest(raw),
                    stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
                )
            )
            after_webhook = self.store.get_org_billing_profile(org_id=org_id)
            self.assertEqual(
                after_webhook.get("stripeCustomerId"), "cus_new",
                f"webhook did not commit new customer; trace={trace}",
            )
            self.assertEqual(after_webhook.get("planKey"), "growth")

            # Release the hydrate: its list_subscriptions raises
            # "No such customer: cus_obsolete". `_clear_stale_stripe_refs`
            # must see cus_obsolete != cus_new and NOT clear.
            release_lookup.set()
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive(), "hydrate thread hung")
            self.assertNotIn("error", hydrate_result, hydrate_result.get("error", ""))

            final = self.store.get_org_billing_profile(org_id=org_id)
            # The NEW identity must survive cleanup.
            self.assertEqual(final.get("stripeCustomerId"), "cus_new",
                             f"cleanup erased new customer; trace={trace}")
            self.assertEqual(final.get("stripeSubscriptionId"), "sub_new",
                             "cleanup erased new subscription")
            self.assertEqual(final.get("priceId"), "price_growth",
                             "cleanup erased new priceId")
            self.assertEqual(final.get("planKey"), "growth")
            self.assertEqual(str(final.get("status") or "").lower(), "active")
        finally:
            self.fake_client.list_subscriptions = orig_list  # type: ignore


class B10_SubscriptionReconciliationAgainstCurrentStripeState(_AuditBase):
    """Review-pass v6 fix (issue #1): on `customer.subscription.*` the
    handler must reconcile against the CURRENT subscription state
    (via Stripe retrieve), not merge the raw payload. Reviewer-named
    scenarios:
    - A: older subscription.updated reporting active after
         subscription.deleted — must not restore access.
    - B: older subscription.updated with Starter after a Growth
         upgrade — must not revert the plan.
    - C: payment_succeeded for an older invoice while sub remains
         past_due for newer debt — must not set active.
    """

    def test_deleted_then_older_updated_active_does_not_restore(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b10a", slug="b10a-church", name="B10a Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b10a"
        billing["stripeSubscriptionId"] = "sub_b10a"
        billing["status"] = "canceled"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)
        # Current Stripe state: canceled (reconciliation returns this).
        self.fake_client.sub_states["sub_b10a"] = {
            "id": "sub_b10a", "customer": "cus_b10a", "status": "canceled",
            "cancel_at_period_end": False,
            "current_period_start": int(time.time()),
            "current_period_end": int(time.time()) + 86400,
            "metadata": {"orgId": org_id, "planKey": "starter"},
            "items": {"data": [{"price": {"id": "price_starter"}}]},
        }
        # Payload falsely reports active (older event).
        event = {
            "id": "evt_b10a_stale", "type": "customer.subscription.updated",
            "data": {"object": {
                "id": "sub_b10a", "customer": "cus_b10a", "status": "active",
                "cancel_at_period_end": False,
                "current_period_start": int(time.time()),
                "current_period_end": int(time.time()) + 86400,
                "metadata": {"orgId": org_id, "planKey": "starter"},
                "items": {"data": [{"price": {"id": "price_starter"}}]},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        final = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(final.get("status"), "canceled",
                         "stale subscription.updated active restored access")

    def test_older_updated_with_starter_after_growth_upgrade_does_not_revert(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b10b", slug="b10b-church", name="B10b Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b10b"
        billing["stripeSubscriptionId"] = "sub_b10b"
        billing["status"] = "active"
        billing["planKey"] = "growth"
        billing["priceId"] = "price_growth"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)
        # Current Stripe state: Growth. Reconciliation returns this.
        self.fake_client.sub_states["sub_b10b"] = {
            "id": "sub_b10b", "customer": "cus_b10b", "status": "active",
            "cancel_at_period_end": False,
            "current_period_start": int(time.time()),
            "current_period_end": int(time.time()) + 86400,
            "metadata": {"orgId": org_id, "planKey": "growth"},
            "items": {"data": [{"price": {"id": "price_growth"}}]},
        }
        # Older payload falsely reports Starter price.
        event = {
            "id": "evt_b10b_stale", "type": "customer.subscription.updated",
            "data": {"object": {
                "id": "sub_b10b", "customer": "cus_b10b", "status": "active",
                "cancel_at_period_end": False,
                "current_period_start": int(time.time()),
                "current_period_end": int(time.time()) + 86400,
                "metadata": {"orgId": org_id, "planKey": "starter"},
                "items": {"data": [{"price": {"id": "price_starter"}}]},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        final = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(final.get("planKey"), "growth",
                         "stale subscription.updated with Starter price reverted the Growth upgrade")
        self.assertEqual((final.get("limits") or {}).get("maxServiceKeys"), 12)

    def test_payment_succeeded_for_older_invoice_does_not_restore_active(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b10c", slug="b10c-church", name="B10c Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b10c"
        billing["stripeSubscriptionId"] = "sub_b10c"
        billing["status"] = "past_due"
        billing["planKey"] = "starter"
        billing["graceEndsAt"] = _utc_now() + timedelta(days=3)
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)
        # Stripe says: still past_due (newer debt not yet paid).
        self.fake_client.sub_states["sub_b10c"] = {
            "id": "sub_b10c", "customer": "cus_b10c", "status": "past_due",
            "cancel_at_period_end": False,
            "current_period_start": int(time.time()),
            "current_period_end": int(time.time()) + 86400,
            "items": {"data": [{"price": {"id": "price_starter"}}]},
        }
        # OLDER payment_succeeded invoice arrives.
        event = {
            "id": "evt_b10c_older_ok", "type": "invoice.payment_succeeded",
            "data": {"object": {
                "id": "in_older_ok", "customer": "cus_b10c",
                "subscription": "sub_b10c",
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        final = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(final.get("status"), "past_due",
                         "older payment_succeeded restored active while newer debt is outstanding")
        self.assertIsNotNone(final.get("graceEndsAt"),
                             "grace timer was cleared despite outstanding newer debt")


class B11_TransientRetrievalErrorRemainsRetryable(_AuditBase):
    """Review-pass v6 fix (issue #2): a transient `retrieve_subscription`
    failure must raise so Stripe retries. The event must NOT be
    marked completed; on retry (with Stripe recovered) the handler
    completes.
    """

    def test_payment_failed_retry_converges_after_transient_retrieve_error(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b11", slug="b11-church", name="B11 Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b11"
        billing["stripeSubscriptionId"] = "sub_b11"
        billing["status"] = "active"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        event = {
            "id": "evt_b11", "type": "invoice.payment_failed",
            "data": {"object": {
                "id": "in_b11", "customer": "cus_b11",
                "subscription": "sub_b11",
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        sig = _sign_stripe_payload(raw, "whsec_audit")

        # First delivery: Stripe retrieve fails transiently.
        class _FlakyThenRecoveredClient(_FakeStripeClient):
            calls = {"n": 0}
            def retrieve_subscription(self_inner, *, subscription_id: str) -> Dict[str, Any]:
                self_inner.calls["n"] += 1
                if self_inner.calls["n"] == 1:
                    raise StripeClientError("stripe_http_503:service_unavailable")
                # Recovered: Stripe reports past_due.
                return {
                    "id": subscription_id, "customer": "cus_b11",
                    "status": "past_due",
                    "cancel_at_period_end": False,
                    "current_period_start": int(time.time()),
                    "current_period_end": int(time.time()) + 86400,
                    "items": {"data": [{"price": {"id": "price_starter"}}]},
                }

        flaky = _FlakyThenRecoveredClient()
        with patch.object(billing_routes, "_stripe_client", lambda: flaky):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(
                    billing_routes.stripe_webhook(
                        request=_DummyRequest(raw), stripe_signature=sig,
                    )
                )
            self.assertEqual(ctx.exception.status_code, 502)
            self.assertEqual(ctx.exception.detail, "stripe_reconcile_failed")
            # Event is NOT completed.
            rec = self.store._billing_events.get("evt_b11") or {}
            self.assertNotEqual(rec.get("status"), "completed",
                                "transient retrieve failure completed the event — not retryable")
            # Age the lease so the retry's claim passes.
            rec["claimedAt"] = _utc_now() - timedelta(seconds=999)
            # Stripe retries.
            result = asyncio.run(
                billing_routes.stripe_webhook(
                    request=_DummyRequest(raw), stripe_signature=sig,
                )
            )
            self.assertNotIn("deduped", result)
            self.assertEqual(result.get("eventId"), "evt_b11")
        final = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(final.get("status"), "past_due",
                         "retry did not reconcile to the recovered subscription state")


class B12_CreateMissingStripeCustomerBumpsCAS(_AuditBase):
    """Review-pass v6 fix (issue #3): `set_org_billing_profile` now
    always bumps `billing.updatedAt`. The `_create_missing_stripe_customer`
    helper (called from the checkout route) persists via
    `set_org_billing_profile`, so a concurrent webhook's CAS catches
    the customer-reference change.
    """

    def test_create_missing_customer_bumps_billing_updated_at(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b12", slug="b12-church", name="B12 Church")
        billing_before = self.store.get_org_billing_profile(org_id=org_id)
        before_updated_at = billing_before.get("updatedAt")
        time.sleep(0.001)
        # Capture a dict that preserves the OLD updatedAt (as the
        # route would after reading billing) — the key assertion is
        # that `set_org_billing_profile` overrides it, no matter what
        # the caller passes.
        snapshot = dict(billing_before)
        snapshot["stripeCustomerId"] = "cus_newly_created"
        snapshot["updatedAt"] = before_updated_at  # explicitly stale
        self.store.set_org_billing_profile(org_id=org_id, billing=snapshot)
        after = self.store.get_org_billing_profile(org_id=org_id)
        self.assertNotEqual(after.get("updatedAt"), before_updated_at,
                            "set_org_billing_profile preserved the stale updatedAt — CAS can't catch concurrent writers")
        self.assertEqual(after.get("stripeCustomerId"), "cus_newly_created")

    def test_webhook_cas_catches_concurrent_customer_create(self) -> None:
        """Deterministic side-effect interleaving (NOT real threads):
        the atomic apply's `side_effect` runs the actual
        `_create_missing_stripe_customer` helper with a mocked client
        between the webhook's billing read and its apply. CAS catches
        the change via the bumped updatedAt. The newly-saved
        stripeCustomerId is NOT overwritten by the stale webhook.

        For the real-thread + barrier-driven version of the reverse
        race (hydrate ↔ webhook), see
        `B14_HydrateReverseRaceCASGuard`.
        """
        org_id = self._bootstrap_owner(uid="owner-b12c", slug="b12c-church", name="B12c Church")
        billing_before = self.store.get_org_billing_profile(org_id=org_id)
        billing_before["stripeSubscriptionId"] = "sub_b12c"
        billing_before["status"] = "canceled"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing_before)

        # Current Stripe state (reconciliation).
        self.fake_client.sub_states["sub_b12c"] = {
            "id": "sub_b12c", "customer": "cus_newly_created",
            "status": "canceled",
            "cancel_at_period_end": False,
            "current_period_start": int(time.time()),
            "current_period_end": int(time.time()) + 86400,
            "items": {"data": [{"price": {"id": "price_starter"}}]},
        }
        event = {
            "id": "evt_b12c", "type": "customer.subscription.updated",
            "data": {"object": {
                "id": "sub_b12c", "customer": "cus_newly_created",
                "status": "canceled", "cancel_at_period_end": False,
                "current_period_start": int(time.time()),
                "current_period_end": int(time.time()) + 86400,
                "metadata": {"orgId": org_id},
                "items": {"data": [{"price": {"id": "price_starter"}}]},
            }},
        }
        raw = json.dumps(event).encode("utf-8")

        # In between the webhook's billing read and its apply, invoke
        # the actual `_create_missing_stripe_customer` helper (which
        # persists via `set_org_billing_profile`).
        original_apply = self.store.apply_webhook_event_atomic
        did_interleave = {"n": 0}
        user = self._user("owner-b12c")

        def _interleaving_apply(**kwargs: Any) -> Dict[str, Any]:
            if did_interleave["n"] == 0:
                did_interleave["n"] = 1

                class _CustClient:
                    def create_customer(self_inner, *, email, name, metadata=None,
                                        idempotency_key=None):
                        return {"id": "cus_newly_created", "email": email, "name": name,
                                "metadata": metadata or {}}

                billing_at_interleave = self.store.get_org_billing_profile(org_id=org_id)
                billing_routes._create_missing_stripe_customer(
                    client=_CustClient(),
                    org_id=org_id,
                    billing=billing_at_interleave,
                    user=user,
                )
            return original_apply(**kwargs)

        with patch.object(self.store, "apply_webhook_event_atomic", side_effect=_interleaving_apply):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(
                    billing_routes.stripe_webhook(
                        request=_DummyRequest(raw),
                        stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
                    )
                )
        self.assertEqual(ctx.exception.status_code, 409)
        final = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(final.get("stripeCustomerId"), "cus_newly_created",
                         "stale webhook overwrote the newly-saved customer reference")


class B9_CheckoutMutationsBumpBillingUpdatedAt(_AuditBase):
    """Review-pass v5 fix for issue #2: `claim_checkout_attempt` and
    `record_checkout_attempt_session` both now bump
    `billing.updatedAt`, so a concurrent webhook handler's CAS check
    on `expected_billing_updated_at` catches these mutations.
    """

    def test_claim_checkout_attempt_bumps_updated_at(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b9a", slug="b9a-church", name="B9a Church")
        before = self.store.get_org_billing_profile(org_id=org_id).get("updatedAt")
        time.sleep(0.001)  # ensure a different timestamp
        self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="owner-b9a",
        )
        after = self.store.get_org_billing_profile(org_id=org_id).get("updatedAt")
        self.assertNotEqual(before, after,
                            "claim_checkout_attempt did not bump billing.updatedAt")

    def test_record_checkout_attempt_session_bumps_updated_at(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b9b", slug="b9b-church", name="B9b Church")
        claim = self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="owner-b9b",
        )
        before = self.store.get_org_billing_profile(org_id=org_id).get("updatedAt")
        time.sleep(0.001)
        self.store.record_checkout_attempt_session(
            org_id=org_id, attempt_id=claim["attemptId"],
            session_id="cs_x", session_url="https://x/",
        )
        after = self.store.get_org_billing_profile(org_id=org_id).get("updatedAt")
        self.assertNotEqual(before, after,
                            "record_checkout_attempt_session did not bump billing.updatedAt")

    def test_webhook_cas_catches_concurrent_claim_checkout_attempt(self) -> None:
        """Pause a webhook after its billing read; create a checkout
        attempt in between; resume the webhook. CAS must fail → 409.
        Without the updatedAt bump the pre-v5 behaviour erased the
        new attempt.

        We seed a canceled-subscription state so `claim_checkout_attempt`
        is allowed through (not blocked by the active-subscription
        guard), exercising the write path that must bump
        `billing.updatedAt`.
        """
        org_id = self._bootstrap_owner(uid="owner-b9c", slug="b9c-church", name="B9c Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b9c"
        billing["stripeSubscriptionId"] = "sub_b9c_old"
        billing["status"] = "canceled"  # canceled → claim passes
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        now_ts = int(time.time())
        # The webhook event references a DIFFERENT subscription id;
        # the obsolete-subscription guard does NOT fire (current
        # status is canceled, not active-shaped), so the handler
        # proceeds to apply.
        event = {
            "id": "evt_b9c", "type": "customer.subscription.updated",
            "data": {"object": {
                "id": "sub_b9c_old", "customer": "cus_b9c", "status": "active",
                "cancel_at_period_end": False,
                "current_period_start": now_ts,
                "current_period_end": now_ts + 30 * 86400,
                "metadata": {"orgId": org_id, "planKey": "starter"},
                "items": {"data": [{"price": {"id": "price_starter"}}]},
            }},
        }
        raw = json.dumps(event).encode("utf-8")

        # Create a checkout attempt inside the apply (simulating a
        # concurrent /checkout-session call between the webhook's
        # read and its atomic apply).
        original_apply = self.store.apply_webhook_event_atomic
        did_interleave = {"n": 0}

        def _interleaving_apply(**kwargs: Any) -> Dict[str, Any]:
            if did_interleave["n"] == 0:
                did_interleave["n"] = 1
                # Simulate a concurrent /checkout-session caller
                # mutating billing. It must bump billing.updatedAt.
                self.store.claim_checkout_attempt(
                    org_id=org_id, plan_key="starter", interval="month",
                    success_url="https://x/ok", cancel_url="https://x/cancel",
                    requested_by_uid="owner-b9c",
                )
            return original_apply(**kwargs)

        with patch.object(self.store, "apply_webhook_event_atomic", side_effect=_interleaving_apply):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(
                    billing_routes.stripe_webhook(
                        request=_DummyRequest(raw),
                        stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
                    )
                )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "webhook_claim_lost")
        # Checkout attempt survived.
        pending_after = self.store.get_org_billing_profile(org_id=org_id).get("pendingCheckoutAttempt")
        self.assertIsNotNone(pending_after, "attempt was erased by stale webhook write")

    def test_webhook_cas_catches_concurrent_record_session(self) -> None:
        """Pause a webhook after its billing read; record a Stripe
        session in between; resume the webhook. CAS must fail → 409.
        Without the updatedAt bump the pre-v5 behaviour restored
        sessionId=None after record completed.
        """
        org_id = self._bootstrap_owner(uid="owner-b9d", slug="b9d-church", name="B9d Church")
        # Seed an existing pending attempt.
        claim = self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="owner-b9d",
        )
        # Advance billing so the webhook read gets a different `updatedAt`.
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b9d"
        billing["stripeSubscriptionId"] = "sub_b9d"
        billing["status"] = "active"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        now_ts = int(time.time())
        event = {
            "id": "evt_b9d", "type": "customer.subscription.updated",
            "data": {"object": {
                "id": "sub_b9d", "customer": "cus_b9d", "status": "active",
                "cancel_at_period_end": False,
                "current_period_start": now_ts,
                "current_period_end": now_ts + 30 * 86400,
                "metadata": {"orgId": org_id, "planKey": "starter"},
                "items": {"data": [{"price": {"id": "price_starter"}}]},
            }},
        }
        raw = json.dumps(event).encode("utf-8")

        original_apply = self.store.apply_webhook_event_atomic
        did_interleave = {"n": 0}

        def _interleaving_apply(**kwargs: Any) -> Dict[str, Any]:
            if did_interleave["n"] == 0:
                did_interleave["n"] = 1
                self.store.record_checkout_attempt_session(
                    org_id=org_id, attempt_id=claim["attemptId"],
                    session_id="cs_concurrent", session_url="https://conc/",
                )
            return original_apply(**kwargs)

        with patch.object(self.store, "apply_webhook_event_atomic", side_effect=_interleaving_apply):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(
                    billing_routes.stripe_webhook(
                        request=_DummyRequest(raw),
                        stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
                    )
                )
        self.assertEqual(ctx.exception.status_code, 409)
        pending_after = self.store.get_org_billing_profile(org_id=org_id).get("pendingCheckoutAttempt")
        self.assertEqual(pending_after.get("sessionId"), "cs_concurrent",
                         "sessionId was reset by stale webhook write")


class B8_AtomicApplyPreservesSoftCapResetBehavior(_AuditBase):
    """Review-pass fix for issue #3: `set_org_monthly_minutes_limit`
    previously reset `softCapReached=False` when the new limit was 0
    or exceeded current usage. The atomic-apply must preserve that
    behavior; without it, an upgrade from an exhausted Starter leaves
    the softCap flag permanently True under Growth."""

    def test_upgrade_resets_soft_cap_flag(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-b8", slug="b8-church", name="B8 Church")
        # Seed org on Starter with usage=500 and softCap on.
        org = self.store._orgs[org_id]
        org["currentMonthMinutes"] = 500
        org["maxMinutesPerMonth"] = 600  # Starter limit
        org["softCapReached"] = True
        org["billing"] = {
            **(org.get("billing") or {}),
            "stripeCustomerId": "cus_b8",
            "stripeSubscriptionId": "sub_b8",
            "status": "active",
            "planKey": "starter",
            "updatedAt": _utc_now(),
        }

        # Simulate an upgrade event to Growth (monthly_minutes=1800).
        now_ts = int(time.time())
        sub_state = {
            "id": "sub_b8", "customer": "cus_b8", "status": "active",
            "cancel_at_period_end": False,
            "current_period_start": now_ts,
            "current_period_end": now_ts + 30 * 86400,
            "metadata": {"orgId": org_id, "planKey": "growth"},
            "items": {"data": [{"price": {"id": "price_growth"}}]},
        }
        self.fake_client.sub_states["sub_b8"] = sub_state
        event = {
            "id": "evt_b8_upgrade", "type": "customer.subscription.updated",
            "created": now_ts,
            "data": {"object": sub_state},
        }
        raw = json.dumps(event).encode("utf-8")
        asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
            )
        )
        self.assertEqual(self.store._orgs[org_id].get("maxMinutesPerMonth"), 1800)
        self.assertFalse(
            self.store._orgs[org_id].get("softCapReached"),
            "softCapReached must reset when new limit exceeds current usage",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
