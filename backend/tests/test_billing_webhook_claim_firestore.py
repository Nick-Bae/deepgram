"""Firestore-emulator regression for the webhook claim/complete pair.

Skips when FIRESTORE_EMULATOR_HOST is unset (local-dev convenience);
CI's `firestore-emulator-tests` job sets it and runs the suite.
Validates production atomicity that in-memory tests can't demonstrate.

Isolation:
  - FIRESTORE_EMULATOR_HOST must point at an emulator — the google-cloud
    client refuses to talk to production unless the env var is unset.
  - No live Stripe credentials, no outbound email, no production
    Firestore writes (the emulator is a separate process).
"""
from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
    raise unittest.SkipTest(
        "FIRESTORE_EMULATOR_HOST not set — Firestore-emulator tests "
        "skipped. CI's firestore-emulator-tests job sets this."
    )

# Importing the Firestore impl here (not at module top so a missing
# emulator doesn't blow up collection).
from app.services.multichurch_store import FirestoreMultiChurchStore


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _unique(prefix: str = "evt") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class WebhookClaimFirestoreTests(unittest.TestCase):
    """These assertions can ONLY be made under a real Firestore
    transaction. The in-memory lock gives the same contract but doesn't
    prove the production-side Firestore `transactional` behaviour.
    """

    @classmethod
    def setUpClass(cls) -> None:
        # Project is arbitrary under the emulator; use the same
        # convention the harness uses elsewhere.
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "stripe-audit-emulator")
        cls.store = FirestoreMultiChurchStore()

    def test_fresh_then_complete(self) -> None:
        eid = _unique()
        self.assertEqual(
            self.store.try_claim_billing_event(
                event_id=eid, event_type="customer.subscription.updated",
                payload={"type": "customer.subscription.updated"},
            ),
            "fresh",
        )
        self.store.complete_billing_event(event_id=eid)
        self.assertEqual(
            self.store.try_claim_billing_event(
                event_id=eid, event_type="customer.subscription.updated",
                payload={"type": "customer.subscription.updated"},
            ),
            "deduped",
        )

    def test_concurrent_within_lease(self) -> None:
        """Second claim within the lease window returns 'concurrent';
        proves the transaction serializes against parallel claims."""
        eid = _unique()
        self.assertEqual(
            self.store.try_claim_billing_event(
                event_id=eid, event_type="invoice.payment_failed",
                payload={"type": "invoice.payment_failed"},
            ),
            "fresh",
        )
        self.assertEqual(
            self.store.try_claim_billing_event(
                event_id=eid, event_type="invoice.payment_failed",
                payload={"type": "invoice.payment_failed"},
            ),
            "concurrent",
        )

    def test_stale_lease_is_reclaimed(self) -> None:
        """A lease older than BILLING_EVENT_LEASE_SEC is reclaimed. We
        can't easily back-date a server timestamp, so this test is
        skipped unless the emulator allows a direct doc write of
        `claimedAt`.
        """
        from google.cloud import firestore
        db = firestore.Client()
        eid = _unique()
        self.assertEqual(
            self.store.try_claim_billing_event(
                event_id=eid, event_type="invoice.payment_failed",
                payload={"type": "invoice.payment_failed"},
            ),
            "fresh",
        )
        # Force the lease into the past.
        ref = db.collection("billing_events").document(eid)
        ref.update({"claimedAt": _utc_now() - timedelta(hours=1)})
        self.assertEqual(
            self.store.try_claim_billing_event(
                event_id=eid, event_type="invoice.payment_failed",
                payload={"type": "invoice.payment_failed"},
            ),
            "fresh",
        )
        # Now completing works.
        self.store.complete_billing_event(event_id=eid)
        snap = ref.get()
        self.assertEqual((snap.to_dict() or {}).get("status"), "completed")


class CheckoutAttemptFirestoreTests(unittest.TestCase):
    """Transactional validation of the checkout-attempt claim.
    Requires an org doc to exist on the emulator.
    """

    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "stripe-audit-emulator")
        cls.store = FirestoreMultiChurchStore()

    def _seed_org(self) -> str:
        from google.cloud import firestore
        db = firestore.Client()
        org_id = f"org_{uuid.uuid4().hex[:8]}"
        db.collection("organizations").document(org_id).set({
            "orgId": org_id,
            "name": f"Audit Church {org_id}",
            "plan": "trial",
            "status": "active",
            "billing": {
                "planKey": "trial",
                "status": "trialing",
            },
        })
        return org_id

    def test_create_then_reuse(self) -> None:
        org_id = self._seed_org()
        first = self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
        )
        self.assertEqual(first["action"], "create")
        self.assertTrue(first["attemptId"].startswith("chkatt_"))
        second = self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
        )
        self.assertEqual(second["action"], "reuse")
        self.assertEqual(second["attemptId"], first["attemptId"])

    def test_pending_different_plan_refuses(self) -> None:
        org_id = self._seed_org()
        self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
        )
        second = self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="growth", interval="month",
        )
        self.assertEqual(second["action"], "pending_different_plan")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
