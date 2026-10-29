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
        event = {
            "id": "evt_h3_upgrade",
            "type": "customer.subscription.updated",
            "data": {"object": {
                "id": "sub_h3", "customer": "cus_h3", "status": "active",
                "cancel_at_period_end": False,
                "current_period_start": now_ts,
                "current_period_end": now_ts + 30 * 86400,
                "metadata": {"orgId": org_id, "planKey": "starter"},  # STALE
                "items": {"data": [{"price": {"id": "price_growth"}}]},
            }},
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
        event = {
            "id": "evt_h3a_annual",
            "type": "customer.subscription.updated",
            "data": {"object": {
                "id": "sub_h3a", "customer": "cus_h3a", "status": "active",
                "cancel_at_period_end": False,
                "current_period_start": now_ts,
                "current_period_end": now_ts + 365 * 86400,
                "metadata": {"orgId": org_id, "planKey": "starter"},
                "items": {"data": [{"price": {"id": "price_growth_annual"}}]},
            }},
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
        event = {
            "id": "evt_h5", "type": "customer.subscription.updated",
            "data": {"object": {
                "id": "sub_h5", "customer": "cus_h5", "status": "past_due",
                "cancel_at_period_end": False,
                "current_period_start": now_ts,
                "current_period_end": now_ts + 30 * 86400,
                "metadata": {"orgId": org_id, "planKey": "starter"},
                "items": {"data": [{"price": {"id": "price_starter"}}]},
            }},
        }
        raw = json.dumps(event).encode("utf-8")
        sig = _sign_stripe_payload(raw, "whsec_audit")

        # First delivery: force a persistence failure.
        original_set = self.store.set_org_billing_profile
        calls = {"n": 0}
        def _flaky_set(**kwargs: Any) -> Dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1 or (kwargs.get("billing") or {}).get("status") == "past_due":
                raise ValueError("persistence_write_failed")
            return original_set(**kwargs)

        with patch.object(self.store, "set_org_billing_profile", side_effect=_flaky_set):
            with self.assertRaises(HTTPException):
                asyncio.run(
                    billing_routes.stripe_webhook(
                        request=_DummyRequest(raw),
                        stripe_signature=sig,
                    )
                )

        # The event must NOT be deduped on retry (a lease is still OK,
        # but after the lease ages out the retry must succeed). To make
        # the test deterministic, age the lease by rewriting claimedAt.
        rec = self.store._billing_events["evt_h5"]
        self.assertNotEqual(rec.get("status"), "completed",
                            "event marked completed despite failed persistence")
        rec["claimedAt"] = _utc_now() - timedelta(seconds=999)  # force lease expiry

        # Second delivery: Stripe retried; this time persistence succeeds.
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
        self.assertEqual(claim, "fresh")

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
        event = {
            "id": "evt_m4b", "type": "customer.subscription.created",
            "data": {"object": {
                "id": "sub_m4b", "customer": "cus_m4b",
                "status": "active", "cancel_at_period_end": False,
                "current_period_start": now_ts,
                "current_period_end": now_ts + 30 * 86400,
                "metadata": {"orgId": org_id, "planKey": "growth"},
                "items": {"data": [{"price": {"id": "price_growth"}}]},
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
        self.assertEqual(final.get("stripeCustomerId"), "cus_m4b")
        self.assertEqual(final.get("stripeSubscriptionId"), "sub_m4b")
        self.assertEqual(final.get("planKey"), "growth")


# ---------------------------------------------------------------------------
# H4 remains policy-dependent — kept as-is in the audit document.
# No code change proposed in this fix batch per operator directive.
# The policy question is captured in `docs/03-analysis/stripe-billing-audit.md`.
# ---------------------------------------------------------------------------


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
