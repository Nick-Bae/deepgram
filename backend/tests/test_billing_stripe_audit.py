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
        # inside the method returns False.
        with patch.object(self.store, "apply_webhook_event_atomic", side_effect=lambda **_: False):
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


class B2_ConcurrentDifferentEventsSameOrg_DocumentedGap(_AuditBase):
    """Boundary 2 — per-event dedup does NOT prevent conflicting
    billing updates when DIFFERENT events on the SAME org arrive
    concurrently. The in-memory store serializes via the global
    `_lock`, so this test documents the LOCAL serialization property.
    Firestore does not serialize cross-event writes to the same org
    doc; that remains a Stripe-sandbox / Firestore-emulator question
    (new audit §3 Suspected risk S8). This test is intentionally
    local-only — see `tests/test_billing_webhook_claim_firestore.py`
    for the emulator-side parallel we would add once the emulator is
    available locally.
    """

    def test_sequential_different_events_both_persist_last_write_wins(self) -> None:
        """When two different events for the same org are processed
        sequentially (as the in-memory store serializes them), the
        LAST write wins. This is the correct per-event behaviour; the
        documented gap is that concurrent Firestore writes do not
        serialize automatically.
        """
        org_id = self._bootstrap_owner(uid="owner-b2", slug="b2-church", name="B2 Church")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_b2"
        billing["stripeSubscriptionId"] = "sub_b2"
        billing["status"] = "active"
        billing["planKey"] = "starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        now_ts = int(time.time())
        payment_failed = {
            "id": "evt_b2_fail", "type": "invoice.payment_failed",
            "data": {"object": {
                "id": "in_b2_fail", "customer": "cus_b2",
                "subscription": "sub_b2",
                "metadata": {"orgId": org_id},
            }},
        }
        payment_succeeded = {
            "id": "evt_b2_ok", "type": "invoice.payment_succeeded",
            "data": {"object": {
                "id": "in_b2_ok", "customer": "cus_b2",
                "subscription": "sub_b2",
                "metadata": {"orgId": org_id},
            }},
        }
        for ev in (payment_failed, payment_succeeded):
            raw = json.dumps(ev).encode("utf-8")
            asyncio.run(
                billing_routes.stripe_webhook(
                    request=_DummyRequest(raw),
                    stripe_signature=_sign_stripe_payload(raw, "whsec_audit"),
                )
            )
        final = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(final.get("status"), "active")  # last write wins


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
