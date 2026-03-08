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
    def create_customer(self, *, email: str | None, name: str | None, metadata: dict | None = None) -> dict:
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

    def test_portal_session_requires_customer(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-billing-2", slug="billing-route-b", name="Billing Route B")
        with self.assertRaises(HTTPException) as ctx:
            billing_routes.create_portal_session(
                billing_routes.PortalSessionRequest(orgId=org_id, returnUrl="https://example.com/settings"),
                user=self._user("owner-billing-2"),
            )
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "billing_customer_not_found")

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
