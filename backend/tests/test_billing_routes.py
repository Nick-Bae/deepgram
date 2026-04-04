from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.auth.firebase_auth import AuthenticatedUser
from app.billing.config import BillingConfig
from app.billing.stripe_client import StripeClientError
from app.routes import auth as auth_routes
from app.routes import billing as billing_routes
from app.services.multichurch_store import InMemoryMultiChurchStore


class _DummyRequest:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def body(self) -> bytes:
        return self._payload


def _sign_stripe_payload(payload: bytes, secret: str, *, timestamp: int | None = None) -> str:
    ts = int(timestamp or time.time())
    signed = f"{ts}.{payload.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


class _FakeStripeClient:
    def create_customer(
        self,
        *,
        email: str | None,
        name: str | None,
        metadata: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        return {"id": "cus_test_123", "email": email, "name": name, "metadata": metadata or {}}

    def create_checkout_session(
        self,
        *,
        customer_id: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        trial_days: int,
        allow_no_payment_method: bool = True,
        metadata: dict | None = None,
    ) -> dict:
        return {
            "id": "cs_test_123",
            "url": "https://checkout.stripe.test/session/cs_test_123",
            "customer": customer_id,
            "price_id": price_id,
            "trial_days": trial_days,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "allow_no_payment_method": allow_no_payment_method,
            "metadata": metadata or {},
        }

    def create_billing_portal_session(self, *, customer_id: str, return_url: str) -> dict:
        return {"url": f"https://billing.stripe.test/portal/{customer_id}?return={return_url}"}

    def retrieve_subscription(self, *, subscription_id: str) -> dict:
        now_ts = int(time.time())
        return {
            "id": subscription_id,
            "customer": "cus_test_123",
            "status": "active",
            "cancel_at_period_end": False,
            "current_period_start": now_ts,
            "current_period_end": now_ts + 30 * 86400,
            "items": {"data": [{"price": {"id": "price_starter"}}]},
        }

    def list_subscriptions(self, *, customer_id: str, status: str = "all", limit: int = 5) -> dict:
        return {
            "data": [
                self.retrieve_subscription(subscription_id=f"sub_for_{customer_id}")
            ]
        }


class BillingRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryMultiChurchStore()
        self.auth_patch = patch.object(auth_routes, "multichurch_store", self.store)
        self.billing_patch = patch.object(billing_routes, "multichurch_store", self.store)
        self.stripe_patch = patch.object(billing_routes, "_stripe_client", lambda: _FakeStripeClient())
        self.auth_patch.start()
        self.billing_patch.start()
        self.stripe_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.addCleanup(self.billing_patch.stop)
        self.addCleanup(self.stripe_patch.stop)

        self.billing_config_patch = patch.object(
            billing_routes,
            "BILLING_CONFIG",
            BillingConfig(
                stripe_secret_key="sk_test_123",
                stripe_webhook_secret="whsec_test_123",
                stripe_price_ids={
                    "starter": "price_starter",
                    "growth": "price_growth",
                    "premium": "price_premium",
                },
                trial_days=30,
                trial_minutes=20,
                grace_days=3,
                entitlements_v2_enabled=False,
            ),
        )
        self.billing_config_patch.start()
        self.addCleanup(self.billing_config_patch.stop)

    @staticmethod
    def _user(uid: str) -> AuthenticatedUser:
        return AuthenticatedUser(uid=uid, email=f"{uid}@example.com", displayName=uid)

    def _bootstrap_owner(self, *, uid: str, slug: str, name: str) -> str:
        result = auth_routes.auth_bootstrap_owner(
            auth_routes.BootstrapOwnerRequest(
                churchName=name,
                churchSlug=slug,
                timezone="America/Chicago",
                source="ko",
                target="en",
            ),
            user=self._user(uid),
        )
        org = result.get("org") or {}
        org_id = str(org.get("orgId") or "").strip()
        self.assertTrue(org_id, result)
        return org_id

    def test_checkout_session_creates_customer_and_returns_url(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-billing-1", slug="billing-route-a", name="Billing Route A")
        result = billing_routes.create_checkout_session(
            billing_routes.CheckoutSessionRequest(
                orgId=org_id,
                planKey="starter",
                successUrl="https://example.com/success",
                cancelUrl="https://example.com/cancel",
            ),
            user=self._user("owner-billing-1"),
        )
        self.assertIn("url", result)
        self.assertIn("sessionId", result)
        billing = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(str(billing.get("stripeCustomerId") or ""), "cus_test_123")

    def test_portal_session_creates_customer_when_missing(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-billing-2", slug="billing-route-b", name="Billing Route B")
        result = billing_routes.create_portal_session(
            billing_routes.PortalSessionRequest(orgId=org_id, returnUrl="https://example.com/settings"),
            user=self._user("owner-billing-2"),
        )
        self.assertIn("url", result)
        self.assertIn("cus_test_123", str(result.get("url") or ""))
        billing = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(str(billing.get("stripeCustomerId") or ""), "cus_test_123")

    def test_portal_session_sanitizes_saved_customer_id(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-billing-2a", slug="billing-route-b1", name="Billing Route B1")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_test_portal`"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        test_case = self

        class _SanitizedPortalStripeClient(_FakeStripeClient):
            def retrieve_subscription(self, *, subscription_id: str) -> dict:
                payload = super().retrieve_subscription(subscription_id=subscription_id)
                payload["customer"] = "cus_test_portal"
                return payload

            def create_billing_portal_session(self, *, customer_id: str, return_url: str) -> dict:
                test_case.assertEqual(customer_id, "cus_test_portal")
                return super().create_billing_portal_session(customer_id=customer_id, return_url=return_url)

        with patch.object(billing_routes, "_stripe_client", lambda: _SanitizedPortalStripeClient()):
            result = billing_routes.create_portal_session(
                billing_routes.PortalSessionRequest(orgId=org_id, returnUrl="https://example.com/settings"),
                user=self._user("owner-billing-2a"),
            )

        self.assertIn("url", result)
        updated = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(str(updated.get("stripeCustomerId") or ""), "cus_test_portal")

    def test_portal_session_recreates_stale_customer_and_returns_url(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-billing-2aa", slug="billing-route-b1a", name="Billing Route B1A")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["stripeCustomerId"] = "cus_test_mode_mismatch`"
        billing["stripeSubscriptionId"] = "sub_test_mode_mismatch"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        class _MissingPortalCustomerStripeClient(_FakeStripeClient):
            def create_customer(
                self,
                *,
                email: str | None,
                name: str | None,
                metadata: dict | None = None,
                idempotency_key: str | None = None,
            ) -> dict:
                return {"id": "cus_test_retry_portal", "email": email, "name": name, "metadata": metadata or {}}

            def create_billing_portal_session(self, *, customer_id: str, return_url: str) -> dict:
                if customer_id == "cus_test_retry_portal":
                    return super().create_billing_portal_session(customer_id=customer_id, return_url=return_url)
                raise StripeClientError(
                    "stripe_http_400: No such customer 'cus_test_mode_mismatch' a similar object exists in test mode, but a live mode key was used"
                )

        with patch.object(billing_routes, "_stripe_client", lambda: _MissingPortalCustomerStripeClient()):
            result = billing_routes.create_portal_session(
                billing_routes.PortalSessionRequest(orgId=org_id, returnUrl="https://example.com/settings"),
                user=self._user("owner-billing-2aa"),
            )

        self.assertIn("url", result)
        self.assertIn("cus_test_retry_portal", str(result.get("url") or ""))
        updated = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(str(updated.get("stripeCustomerId") or ""), "cus_test_retry_portal")
        self.assertIsNone(updated.get("stripeSubscriptionId"))

    def test_portal_session_refreshes_customer_from_subscription_before_opening(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-billing-2ab", slug="billing-route-b1b", name="Billing Route B1B")
        billing = self.store.get_org_billing_profile(org_id=org_id)
        billing["planKey"] = "starter"
        billing["status"] = "active"
        billing["stripeCustomerId"] = "cus_test_empty_portal"
        billing["stripeSubscriptionId"] = "sub_test_real_portal"
        self.store.set_org_billing_profile(org_id=org_id, billing=billing)

        test_case = self

        class _PortalHydrateStripeClient(_FakeStripeClient):
            def retrieve_subscription(self, *, subscription_id: str) -> dict:
                now_ts = int(time.time())
                return {
                    "id": subscription_id,
                    "customer": "cus_test_real_portal",
                    "status": "active",
                    "cancel_at_period_end": False,
                    "current_period_start": now_ts,
                    "current_period_end": now_ts + 30 * 86400,
                    "items": {"data": [{"price": {"id": "price_starter"}}]},
                }

            def list_subscriptions(self, *, customer_id: str, status: str = "all", limit: int = 5) -> dict:
                test_case.assertEqual(customer_id, "cus_test_empty_portal")
                return {"data": []}

            def create_billing_portal_session(self, *, customer_id: str, return_url: str) -> dict:
                test_case.assertEqual(customer_id, "cus_test_real_portal")
                return super().create_billing_portal_session(customer_id=customer_id, return_url=return_url)

        with patch.object(billing_routes, "_stripe_client", lambda: _PortalHydrateStripeClient()):
            result = billing_routes.create_portal_session(
                billing_routes.PortalSessionRequest(orgId=org_id, returnUrl="https://example.com/settings"),
                user=self._user("owner-billing-2ab"),
            )

        self.assertIn("cus_test_real_portal", str(result.get("url") or ""))
        updated = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(str(updated.get("stripeCustomerId") or ""), "cus_test_real_portal")
        self.assertEqual(str(updated.get("stripeSubscriptionId") or ""), "sub_test_real_portal")

    def test_checkout_session_recreates_customer_when_saved_customer_missing(self) -> None:
        class _MissingCustomerStripeClient(_FakeStripeClient):
            def __init__(self) -> None:
                self._customer_created_count = 0

            def create_customer(
                self,
                *,
                email: str | None,
                name: str | None,
                metadata: dict | None = None,
                idempotency_key: str | None = None,
            ) -> dict:
                self._customer_created_count += 1
                return {"id": f"cus_test_retry_{self._customer_created_count}"}

            def create_checkout_session(
                self,
                *,
                customer_id: str,
                price_id: str,
                success_url: str,
                cancel_url: str,
                trial_days: int,
                allow_no_payment_method: bool = True,
                metadata: dict | None = None,
            ) -> dict:
                if customer_id == "cus_old_live_mode":
                    raise StripeClientError("stripe_http_400:No such customer: 'cus_old_live_mode'")
                return super().create_checkout_session(
                    customer_id=customer_id,
                    price_id=price_id,
                    success_url=success_url,
                    cancel_url=cancel_url,
                    trial_days=trial_days,
                    allow_no_payment_method=allow_no_payment_method,
                    metadata=metadata,
                )

        org_id = self._bootstrap_owner(uid="owner-billing-2b", slug="billing-route-b2", name="Billing Route B2")
        existing = self.store.get_org_billing_profile(org_id=org_id)
        existing["stripeCustomerId"] = "cus_old_live_mode"
        self.store.set_org_billing_profile(org_id=org_id, billing=existing)

        fake = _MissingCustomerStripeClient()
        with patch.object(billing_routes, "_stripe_client", lambda: fake):
            result = billing_routes.create_checkout_session(
                billing_routes.CheckoutSessionRequest(
                    orgId=org_id,
                    planKey="starter",
                    successUrl="https://example.com/success",
                    cancelUrl="https://example.com/cancel",
                ),
                user=self._user("owner-billing-2b"),
            )
        self.assertIn("url", result)
        updated = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(str(updated.get("stripeCustomerId") or ""), "cus_test_retry_1")

    def test_billing_status_hydrates_subscription_period_when_missing(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-billing-2c", slug="billing-route-b3", name="Billing Route B3")
        existing = self.store.get_org_billing_profile(org_id=org_id)
        existing["planKey"] = "starter"
        existing["status"] = "active"
        existing["stripeCustomerId"] = "cus_test_hydrate"
        existing["stripeSubscriptionId"] = "sub_test_hydrate"
        existing["currentPeriodStart"] = None
        existing["currentPeriodEnd"] = None
        self.store.set_org_billing_profile(org_id=org_id, billing=existing)

        class _HydrateStripeClient(_FakeStripeClient):
            def retrieve_subscription(self, *, subscription_id: str) -> dict:
                now_ts = int(time.time())
                return {
                    "id": subscription_id,
                    "customer": "cus_test_hydrate",
                    "status": "active",
                    "cancel_at_period_end": False,
                    "current_period_start": None,
                    "current_period_end": None,
                    "items": {
                        "data": [
                            {
                                "price": {"id": "price_growth"},
                                "current_period_start": now_ts,
                                "current_period_end": now_ts + 31 * 86400,
                            }
                        ]
                    },
                }

        with patch.object(billing_routes, "_stripe_client", lambda: _HydrateStripeClient()):
            payload = billing_routes.billing_status(org_id=org_id, user=self._user("owner-billing-2c"))

        billing = payload.get("billing") or {}
        self.assertEqual(str(billing.get("planKey") or ""), "growth")
        self.assertEqual(str(billing.get("status") or ""), "active")
        self.assertIsInstance(billing.get("currentPeriodStart"), datetime)
        self.assertIsInstance(billing.get("currentPeriodEnd"), datetime)
        self.assertEqual(str(billing.get("priceId") or ""), "price_growth")

    def test_billing_status_refresh_clears_stale_customer_refs_on_missing_customer(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-billing-2ca", slug="billing-route-b3a", name="Billing Route B3A")
        existing = self.store.get_org_billing_profile(org_id=org_id)
        existing["stripeCustomerId"] = "cus_test_refresh_bad`"
        existing["stripeSubscriptionId"] = "sub_test_refresh_bad`"
        existing["currentPeriodStart"] = datetime.now(timezone.utc)
        existing["currentPeriodEnd"] = datetime.now(timezone.utc)
        self.store.set_org_billing_profile(org_id=org_id, billing=existing)

        class _MissingRefreshCustomerStripeClient(_FakeStripeClient):
            def list_subscriptions(self, *, customer_id: str, status: str = "all", limit: int = 5) -> dict:
                raise StripeClientError(
                    "stripe_http_400: No such customer 'cus_test_refresh_bad' a similar object exists in test mode, but a live mode key was used"
                )

        with patch.object(billing_routes, "_stripe_client", lambda: _MissingRefreshCustomerStripeClient()):
            payload = billing_routes.billing_status(
                org_id=org_id,
                refresh=True,
                user=self._user("owner-billing-2ca"),
            )

        billing = payload.get("billing") or {}
        self.assertIsNone(billing.get("stripeCustomerId"))
        self.assertIsNone(billing.get("stripeSubscriptionId"))
        self.assertIsNone(billing.get("currentPeriodStart"))
        self.assertIsNone(billing.get("currentPeriodEnd"))

    def test_billing_status_hydrates_from_customer_when_subscription_id_missing(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-billing-2d", slug="billing-route-b4", name="Billing Route B4")
        existing = self.store.get_org_billing_profile(org_id=org_id)
        existing["planKey"] = "starter"
        existing["status"] = "active"
        existing["stripeCustomerId"] = "cus_test_lookup"
        existing["stripeSubscriptionId"] = None
        existing["currentPeriodStart"] = None
        existing["currentPeriodEnd"] = None
        self.store.set_org_billing_profile(org_id=org_id, billing=existing)

        test_case = self

        class _CustomerLookupStripeClient(_FakeStripeClient):
            def retrieve_subscription(self, *, subscription_id: str) -> dict:
                now_ts = int(time.time())
                return {
                    "id": subscription_id,
                    "customer": "cus_test_lookup",
                    "status": "active",
                    "cancel_at_period_end": False,
                    "current_period_start": now_ts,
                    "current_period_end": now_ts + 32 * 86400,
                    "items": {"data": [{"price": {"id": "price_premium"}}]},
                }

            def list_subscriptions(self, *, customer_id: str, status: str = "all", limit: int = 5) -> dict:
                test_case.assertEqual(customer_id, "cus_test_lookup")
                test_case.assertEqual(status, "all")
                return {
                    "data": [
                        self.retrieve_subscription(subscription_id="sub_test_lookup"),
                    ]
                }

        with patch.object(billing_routes, "_stripe_client", lambda: _CustomerLookupStripeClient()):
            payload = billing_routes.billing_status(org_id=org_id, user=self._user("owner-billing-2d"))

        billing = payload.get("billing") or {}
        self.assertEqual(str(billing.get("planKey") or ""), "premium")
        self.assertEqual(str(billing.get("stripeSubscriptionId") or ""), "sub_test_lookup")
        self.assertIsInstance(billing.get("currentPeriodStart"), datetime)
        self.assertIsInstance(billing.get("currentPeriodEnd"), datetime)

    def test_billing_status_refresh_forces_stripe_sync(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-billing-2e", slug="billing-route-b5", name="Billing Route B5")
        existing = self.store.get_org_billing_profile(org_id=org_id)
        existing["planKey"] = "starter"
        existing["status"] = "active"
        existing["stripeCustomerId"] = "cus_test_refresh"
        existing["stripeSubscriptionId"] = "sub_test_refresh"
        existing["currentPeriodStart"] = datetime(2024, 1, 1, tzinfo=timezone.utc)
        existing["currentPeriodEnd"] = datetime(2024, 2, 1, tzinfo=timezone.utc)
        existing["priceId"] = "price_starter"
        self.store.set_org_billing_profile(org_id=org_id, billing=existing)

        test_case = self

        class _ForceRefreshStripeClient(_FakeStripeClient):
            def retrieve_subscription(self, *, subscription_id: str) -> dict:
                test_case.assertEqual(subscription_id, "sub_test_refresh")
                return {
                    "id": subscription_id,
                    "customer": "cus_test_refresh",
                    "status": "active",
                    "cancel_at_period_end": True,
                    "current_period_start": int(datetime(2024, 3, 1, tzinfo=timezone.utc).timestamp()),
                    "current_period_end": int(datetime(2024, 4, 1, tzinfo=timezone.utc).timestamp()),
                    "items": {"data": [{"price": {"id": "price_growth"}}]},
                }

            def list_subscriptions(self, *, customer_id: str, status: str = "all", limit: int = 5) -> dict:
                test_case.assertEqual(customer_id, "cus_test_refresh")
                return {
                    "data": [
                        self.retrieve_subscription(subscription_id="sub_test_refresh"),
                    ]
                }

        with patch.object(billing_routes, "_stripe_client", lambda: _ForceRefreshStripeClient()):
            payload = billing_routes.billing_status(
                org_id=org_id,
                refresh=True,
                user=self._user("owner-billing-2e"),
            )

        billing = payload.get("billing") or {}
        self.assertEqual(str(billing.get("planKey") or ""), "growth")
        self.assertEqual(str(billing.get("priceId") or ""), "price_growth")
        self.assertEqual(bool(billing.get("cancelAtPeriodEnd")), True)
        self.assertEqual(
            billing.get("currentPeriodEnd"),
            datetime(2024, 4, 1, tzinfo=timezone.utc),
        )

    def test_billing_status_refresh_falls_back_when_saved_subscription_id_is_stale(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-billing-2f", slug="billing-route-b6", name="Billing Route B6")
        existing = self.store.get_org_billing_profile(org_id=org_id)
        existing["planKey"] = "starter"
        existing["status"] = "active"
        existing["stripeCustomerId"] = "cus_test_stale"
        existing["stripeSubscriptionId"] = "sub_stale_missing"
        existing["currentPeriodStart"] = None
        existing["currentPeriodEnd"] = None
        self.store.set_org_billing_profile(org_id=org_id, billing=existing)

        class _StaleSubscriptionStripeClient(_FakeStripeClient):
            def retrieve_subscription(self, *, subscription_id: str) -> dict:
                if subscription_id == "sub_stale_missing":
                    raise StripeClientError("stripe_http_404:No such subscription: 'sub_stale_missing'")
                return super().retrieve_subscription(subscription_id=subscription_id)

            def list_subscriptions(self, *, customer_id: str, status: str = "all", limit: int = 5) -> dict:
                now_ts = int(time.time())
                return {
                    "data": [
                        {
                            "id": "sub_latest_active",
                            "customer": customer_id,
                            "status": "active",
                            "cancel_at_period_end": False,
                            "current_period_start": now_ts,
                            "current_period_end": now_ts + 28 * 86400,
                            "items": {"data": [{"price": {"id": "price_growth"}}]},
                        }
                    ]
                }

        with patch.object(billing_routes, "_stripe_client", lambda: _StaleSubscriptionStripeClient()):
            payload = billing_routes.billing_status(
                org_id=org_id,
                refresh=True,
                user=self._user("owner-billing-2f"),
            )

        billing = payload.get("billing") or {}
        self.assertEqual(str(billing.get("stripeSubscriptionId") or ""), "sub_latest_active")
        self.assertEqual(str(billing.get("planKey") or ""), "growth")
        self.assertIsInstance(billing.get("currentPeriodStart"), datetime)
        self.assertIsInstance(billing.get("currentPeriodEnd"), datetime)

    def test_webhook_updates_status_and_is_idempotent(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-billing-3", slug="billing-route-c", name="Billing Route C")
        existing = self.store.get_org_billing_profile(org_id=org_id)
        existing["stripeCustomerId"] = "cus_123"
        existing["stripeSubscriptionId"] = "sub_123"
        existing["status"] = "active"
        existing["graceEndsAt"] = None
        self.store.set_org_billing_profile(org_id=org_id, billing=existing)

        event = {
            "id": "evt_123",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_123",
                    "customer": "cus_123",
                    "status": "past_due",
                    "cancel_at_period_end": False,
                    "current_period_start": int(time.time()),
                    "current_period_end": int(time.time()) + 86400,
                    "metadata": {"orgId": org_id, "planKey": "starter"},
                    "items": {"data": [{"price": {"id": "price_starter"}}]},
                }
            },
        }
        raw = json.dumps(event).encode("utf-8")
        signature = _sign_stripe_payload(raw, "whsec_test_123")

        result = asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=signature,
            )
        )
        self.assertTrue(bool(result.get("ok")))
        self.assertEqual(result.get("eventId"), "evt_123")

        updated = self.store.get_org_billing_profile(org_id=org_id)
        self.assertEqual(str(updated.get("status") or ""), "past_due")
        self.assertIsNone(updated.get("trialEndsAt"))
        self.assertIsInstance(updated.get("graceEndsAt"), datetime)
        self.assertEqual(bool((updated.get("entitlements") or {}).get("canStartService")), True)

        second = asyncio.run(
            billing_routes.stripe_webhook(
                request=_DummyRequest(raw),
                stripe_signature=signature,
            )
        )
        self.assertTrue(bool(second.get("deduped")))

    def test_webhook_rejects_invalid_signature(self) -> None:
        event = {"id": "evt_bad", "type": "invoice.payment_failed", "data": {"object": {}}}
        raw = json.dumps(event).encode("utf-8")
        bad_signature = _sign_stripe_payload(raw, "wrong_secret")
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                billing_routes.stripe_webhook(
                    request=_DummyRequest(raw),
                    stripe_signature=bad_signature,
                )
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "invalid_stripe_signature")


if __name__ == "__main__":
    unittest.main()
