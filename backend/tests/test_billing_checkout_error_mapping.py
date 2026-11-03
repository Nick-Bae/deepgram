"""Regression tests for the checkout-session error-mapping fix
(`fix/billing-checkout-aborted-maps-503`).

Scope — strictly the route handler's try/except around
`multichurch_store.claim_checkout_attempt`. Nothing in these tests
exercises the accepted CAS getter, its regression suite, or the
parallel-claims reproducer; all three remain preserved byte-for-byte.

What the fix added:
- `google.api_core.exceptions.Aborted` raised by the transactional
  claim is mapped to HTTP 503 with stable detail
  `"checkout_backend_unavailable"`, logged with structured
  org-id/cause-type context.
- `google.api_core.exceptions.RetryError` whose `.cause` is an
  Aborted is mapped the same way.
- A `RetryError` with a non-Aborted cause (e.g. `DeadlineExceeded`)
  is re-raised unmodified; the fix does NOT reclassify unrelated
  retry failures.
- Pre-existing 404 (`org_not_found`) and 400 (other `ValueError`)
  handling is preserved as-is.
- The 503 is raised BEFORE any Stripe operation, so a half-created
  Stripe session cannot leak from a retry-exhausted store
  transaction.

What the fix did NOT do:
- No application-level retry added.
- No SDK retry-limit change.
- No message-string pattern matching (the discrimination is by
  exception TYPE / cause-type).
- No `claim_checkout_attempt` or CAS getter signature change.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from google.api_core import exceptions as gexc

from app.auth.firebase_auth import AuthenticatedUser
from app.billing.config import BillingConfig
from app.routes import auth as auth_routes
from app.routes import billing as billing_routes
from app.services.multichurch_store import InMemoryMultiChurchStore


class _StripeCallTracker:
    """Records whether create_checkout_session was called at all.

    The purpose is to assert — on the 503 paths — that the Stripe
    client was never invoked. If any test on the 503 path ever
    produces `.calls > 0`, the fix has regressed: Stripe would have
    been called after a store retry-exhaustion.
    """

    def __init__(self) -> None:
        self.calls: int = 0
        self.last_kwargs: dict = {}

    def create_customer(self, **_kwargs) -> dict:
        # Not touched on the 503 path, but kept callable in case
        # another test path reaches it.
        return {"id": "cus_test_tracker", "email": _kwargs.get("email"), "name": _kwargs.get("name")}

    def create_checkout_session(self, **kwargs) -> dict:
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        return {
            "id": "cs_test_tracker",
            "url": "https://checkout.stripe.test/session/cs_test_tracker",
            "customer": kwargs.get("customer_id"),
            "price_id": kwargs.get("price_id"),
            "success_url": kwargs.get("success_url"),
            "cancel_url": kwargs.get("cancel_url"),
            "idempotency_key": kwargs.get("idempotency_key"),
        }


class CheckoutErrorMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryMultiChurchStore()
        self.auth_patch = patch.object(auth_routes, "multichurch_store", self.store)
        self.billing_patch = patch.object(billing_routes, "multichurch_store", self.store)
        self.stripe_tracker = _StripeCallTracker()
        self.stripe_patch = patch.object(
            billing_routes, "_stripe_client", lambda: self.stripe_tracker,
        )
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
                stripe_price_ids_annual={
                    "starter": "price_starter_annual",
                    "growth": "price_growth_annual",
                    "premium": "price_premium_annual",
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

    def _request(self, org_id: str) -> billing_routes.CheckoutSessionRequest:
        return billing_routes.CheckoutSessionRequest(
            orgId=org_id,
            planKey="starter",
            successUrl="https://example.com/success",
            cancelUrl="https://example.com/cancel",
        )

    # -----------------------------------------------------------------
    # 1 — SDK retry exhaustion (Aborted) → 503 + Stripe NOT called.
    # -----------------------------------------------------------------
    def test_sdk_retry_exhaustion_aborted_returns_503_before_any_stripe_operation(self) -> None:
        org_id = self._bootstrap_owner(
            uid="owner-cem-1", slug="billing-cem-aborted", name="CEM Aborted",
        )
        # Force the transactional store call to raise Aborted —
        # same exception class the Firestore client surfaces after
        # a retry-exhausted ABORTED commit.
        with patch.object(
            self.store, "claim_checkout_attempt",
            side_effect=gexc.Aborted("Transaction lock timeout."),
        ):
            with self.assertRaises(HTTPException) as ctx:
                billing_routes.create_checkout_session(
                    self._request(org_id), user=self._user("owner-cem-1"),
                )
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "checkout_backend_unavailable")
        self.assertEqual(
            self.stripe_tracker.calls, 0,
            "Stripe create_checkout_session MUST NOT be called when the "
            "store's claim transaction has exhausted its retries.",
        )

    # -----------------------------------------------------------------
    # 2 — RetryError wrapping Aborted → 503 + Stripe NOT called.
    # -----------------------------------------------------------------
    def test_retry_error_wrapping_aborted_returns_503(self) -> None:
        org_id = self._bootstrap_owner(
            uid="owner-cem-2", slug="billing-cem-retry-aborted",
            name="CEM RetryError Aborted",
        )
        cause = gexc.Aborted("Transaction lock timeout.")
        retry_err = gexc.RetryError("Deadline of 60.0s exceeded", cause=cause)
        with patch.object(
            self.store, "claim_checkout_attempt",
            side_effect=retry_err,
        ):
            with self.assertRaises(HTTPException) as ctx:
                billing_routes.create_checkout_session(
                    self._request(org_id), user=self._user("owner-cem-2"),
                )
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "checkout_backend_unavailable")
        self.assertEqual(self.stripe_tracker.calls, 0)

    # -----------------------------------------------------------------
    # 3 — Matching error-MESSAGE string, WITHOUT the Aborted cause,
    # must NOT be misclassified as 503. Verifies the discrimination
    # is by exception TYPE / cause-type, not string matching.
    # -----------------------------------------------------------------
    def test_matching_message_string_without_aborted_cause_not_misclassified(self) -> None:
        org_id = self._bootstrap_owner(
            uid="owner-cem-3", slug="billing-cem-string-only",
            name="CEM Message String Only",
        )
        # Same English text as the Aborted class carries, but
        # raised as a plain RuntimeError. The route handler must
        # NOT reclassify this to 503; it should propagate (FastAPI
        # would map it to 500) rather than silently masquerade as
        # a retry-exhausted store transaction.
        with patch.object(
            self.store, "claim_checkout_attempt",
            side_effect=RuntimeError("Transaction lock timeout."),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                billing_routes.create_checkout_session(
                    self._request(org_id), user=self._user("owner-cem-3"),
                )
        self.assertIn("Transaction lock timeout", str(ctx.exception))
        self.assertEqual(
            self.stripe_tracker.calls, 0,
            "Stripe must not be reached on the unmatched-exception path "
            "either — the exception propagates from the store call site.",
        )

    # -----------------------------------------------------------------
    # 4 — RetryError with a non-Aborted cause (e.g. DeadlineExceeded)
    # must NOT be reclassified as 503.
    # -----------------------------------------------------------------
    def test_retry_error_with_non_aborted_cause_propagates(self) -> None:
        org_id = self._bootstrap_owner(
            uid="owner-cem-4", slug="billing-cem-retry-deadline",
            name="CEM RetryError Deadline",
        )
        cause = gexc.DeadlineExceeded("Deadline exceeded reading snapshot.")
        retry_err = gexc.RetryError("Deadline of 60.0s exceeded", cause=cause)
        with patch.object(
            self.store, "claim_checkout_attempt",
            side_effect=retry_err,
        ):
            with self.assertRaises(gexc.RetryError):
                billing_routes.create_checkout_session(
                    self._request(org_id), user=self._user("owner-cem-4"),
                )
        self.assertEqual(self.stripe_tracker.calls, 0)

    # -----------------------------------------------------------------
    # 5 — Preserve existing 404 for org_not_found.
    # -----------------------------------------------------------------
    def test_org_not_found_still_returns_404(self) -> None:
        org_id = self._bootstrap_owner(
            uid="owner-cem-5", slug="billing-cem-404",
            name="CEM Preserve 404",
        )
        with patch.object(
            self.store, "claim_checkout_attempt",
            side_effect=ValueError("org_not_found"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                billing_routes.create_checkout_session(
                    self._request(org_id), user=self._user("owner-cem-5"),
                )
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "org_not_found")
        self.assertEqual(self.stripe_tracker.calls, 0)

    # -----------------------------------------------------------------
    # 6 — Preserve existing 400 for other ValueError.
    # -----------------------------------------------------------------
    def test_genuine_validation_error_still_returns_400(self) -> None:
        org_id = self._bootstrap_owner(
            uid="owner-cem-6", slug="billing-cem-400",
            name="CEM Preserve 400",
        )
        with patch.object(
            self.store, "claim_checkout_attempt",
            side_effect=ValueError("invalid_plan_params"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                billing_routes.create_checkout_session(
                    self._request(org_id), user=self._user("owner-cem-6"),
                )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "invalid_plan_params")
        self.assertEqual(self.stripe_tracker.calls, 0)

    # -----------------------------------------------------------------
    # 7 — SDK-generated exhaustion (the shape the Firestore Python
    #     client's @firestore.transactional decorator actually surfaces
    #     after N failed ABORTED commits): a ValueError whose
    #     __cause__ is the underlying Aborted. Must be mapped to 503,
    #     BEFORE any Stripe operation.
    #
    #     Reviewer-reproduced path (2026-11-02): five stubbed ABORTED
    #     commits → client wrapper exhausts and raises
    #     ValueError("Failed to commit transaction in 5 attempts.")
    #     with `__cause__` set to google.api_core.exceptions.Aborted.
    # -----------------------------------------------------------------
    def test_sdk_exhaustion_valueerror_caused_by_aborted_returns_503(self) -> None:
        org_id = self._bootstrap_owner(
            uid="owner-cem-7", slug="billing-cem-valueerror-aborted",
            name="CEM ValueError-Caused-By-Aborted",
        )
        cause = gexc.Aborted("Transaction lock timeout.")
        # Build the exact shape the SDK actually produces: a ValueError
        # whose chained __cause__ is Aborted. The simplest way to
        # construct that in Python is `raise ValueError(...) from cause`,
        # which sets `__cause__` on the ValueError. We mimic that here
        # with a side_effect callable so the chaining is preserved
        # exactly as the SDK's own `raise ... from ...` would set it.
        def _raise_valueerror_from_aborted(*_a, **_kw):
            try:
                raise cause
            except gexc.Aborted as inner:
                raise ValueError(
                    "Failed to commit transaction in 5 attempts."
                ) from inner

        with patch.object(
            self.store, "claim_checkout_attempt",
            side_effect=_raise_valueerror_from_aborted,
        ):
            with self.assertRaises(HTTPException) as ctx:
                billing_routes.create_checkout_session(
                    self._request(org_id), user=self._user("owner-cem-7"),
                )
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "checkout_backend_unavailable")
        self.assertEqual(
            self.stripe_tracker.calls, 0,
            "Stripe create_checkout_session MUST NOT be called when the "
            "Firestore client's transactional wrapper has exhausted its "
            "retries and surfaced the exhaustion as a cause-chained "
            "ValueError.",
        )

    # -----------------------------------------------------------------
    # 8 — Message-only negative control: a ValueError whose message
    #     EXACTLY matches the SDK's exhaustion text but whose __cause__
    #     is None (or any non-Aborted type) must RETAIN its existing
    #     400 behavior. The discriminator is `__cause__`'s type, NOT
    #     message substring.
    # -----------------------------------------------------------------
    def test_valueerror_matching_message_without_aborted_cause_stays_400(self) -> None:
        org_id = self._bootstrap_owner(
            uid="owner-cem-8", slug="billing-cem-message-only",
            name="CEM Message-Only Negative Control",
        )
        # Plain ValueError (no `raise ... from ...`), so __cause__ is None.
        # The message is the one the SDK uses on exhaustion.
        with patch.object(
            self.store, "claim_checkout_attempt",
            side_effect=ValueError("Failed to commit transaction in 5 attempts."),
        ):
            with self.assertRaises(HTTPException) as ctx:
                billing_routes.create_checkout_session(
                    self._request(org_id), user=self._user("owner-cem-8"),
                )
        # Must NOT be reclassified to 503 — the discriminator is
        # __cause__'s TYPE, not the message text.
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(
            ctx.exception.detail,
            "Failed to commit transaction in 5 attempts.",
            "Existing 400 behavior must be preserved for a ValueError "
            "that merely has a matching message but no Aborted cause.",
        )
        self.assertEqual(self.stripe_tracker.calls, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
