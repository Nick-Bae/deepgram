"""Firestore-emulator regression for the claim-and-complete pair,
the fenced apply, the checkout-attempt transactional claim, and
legacy-record compatibility.

Skips when FIRESTORE_EMULATOR_HOST is unset (local-dev convenience);
CI's `firestore-emulator-tests` job sets it and runs the suite.

Fixture notes (fixed per audit review):
  - The store opens Firestore with `database="worship-translation"`.
    The test helpers consistently use THAT same database when they
    seed or inspect so the test and the store look at the same
    documents.
  - The implementation stores events in the `billingEvents`
    collection (camelCase). Earlier test code used `billing_events`
    — the mismatch meant lease back-dating never reached the real
    doc. Fixed.
  - Parallel-claim tests use real threads over the real transactional
    paths; the old sequential tests did not demonstrate contention
    safety.
"""
from __future__ import annotations

import os
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
    raise unittest.SkipTest(
        "FIRESTORE_EMULATOR_HOST not set — Firestore-emulator tests "
        "skipped. CI's firestore-emulator-tests job sets this."
    )

# Importing the Firestore impl here (not at module top so a missing
# emulator doesn't blow up collection).
from app.services.multichurch_store import FirestoreMultiChurchStore


_DATABASE_NAME = "worship-translation"
_PROJECT_ID = os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "stripe-audit-emulator")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _unique(prefix: str = "evt") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _db():
    """Open the SAME database the store opens — mismatched defaults
    silently produced empty reads in the earlier fixture."""
    from google.cloud import firestore
    return firestore.Client(project=_PROJECT_ID, database=_DATABASE_NAME)


class WebhookClaimFirestoreTests(unittest.TestCase):
    """Transactional claim-and-complete + token-fence + legacy compat."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.store = FirestoreMultiChurchStore()

    def test_fresh_then_complete_and_dedup(self) -> None:
        eid = _unique()
        a = self.store.try_claim_billing_event(
            event_id=eid, event_type="customer.subscription.updated",
            payload={"type": "customer.subscription.updated"},
        )
        self.assertEqual(a.get("status"), "fresh")
        self.assertTrue(bool(a.get("claimToken")))
        self.assertTrue(self.store.check_claim_still_held(
            event_id=eid, claim_token=a.get("claimToken"),
        ))
        self.assertTrue(self.store.complete_billing_event(
            event_id=eid, claim_token=a.get("claimToken"),
        ))
        b = self.store.try_claim_billing_event(
            event_id=eid, event_type="customer.subscription.updated",
            payload={"type": "customer.subscription.updated"},
        )
        self.assertEqual(b.get("status"), "deduped")

    def test_concurrent_within_lease(self) -> None:
        """Two sequential claims within the lease window: second is 'concurrent'."""
        eid = _unique()
        a = self.store.try_claim_billing_event(
            event_id=eid, event_type="invoice.payment_failed",
            payload={"type": "invoice.payment_failed"},
        )
        self.assertEqual(a.get("status"), "fresh")
        b = self.store.try_claim_billing_event(
            event_id=eid, event_type="invoice.payment_failed",
            payload={"type": "invoice.payment_failed"},
        )
        self.assertEqual(b.get("status"), "concurrent")

    def test_actual_parallel_claims_serialize(self) -> None:
        """REAL parallel claims (two threads): exactly ONE returns
        'fresh'; the other returns 'concurrent'. Sequential tests
        don't demonstrate transaction serialization."""
        eid = _unique("parallel")
        gate = threading.Barrier(4)
        results: List[Dict[str, Any]] = []
        results_lock = threading.Lock()

        def _claim() -> None:
            gate.wait()
            r = self.store.try_claim_billing_event(
                event_id=eid, event_type="invoice.payment_failed",
                payload={"type": "invoice.payment_failed"},
            )
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=_claim) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()

        fresh = [r for r in results if r.get("status") == "fresh"]
        concurrent = [r for r in results if r.get("status") == "concurrent"]
        self.assertEqual(
            len(fresh), 1,
            f"exactly one parallel claim must win 'fresh'; got {results}",
        )
        self.assertEqual(len(concurrent) + len(fresh), 4, results)

    def test_stale_lease_reclaim_and_slow_worker_rejected(self) -> None:
        """A lease older than BILLING_EVENT_LEASE_SEC is reclaimed by
        a sibling delivery; the slow worker's late `complete` with
        its original token must be refused (returns False) so it
        doesn't overwrite the newer claim's state.

        FIXED FIXTURE: collection is `billingEvents` (camelCase); the
        store's database is `worship-translation`.
        """
        db = _db()
        eid = _unique()
        a = self.store.try_claim_billing_event(
            event_id=eid, event_type="invoice.payment_failed",
            payload={"type": "invoice.payment_failed"},
        )
        self.assertEqual(a.get("status"), "fresh")
        token_a = a.get("claimToken")
        # Force the lease into the past — correct collection name,
        # correct database.
        ref = db.collection("billingEvents").document(eid)
        ref.update({"claimedAt": _utc_now() - timedelta(hours=1)})
        b = self.store.try_claim_billing_event(
            event_id=eid, event_type="invoice.payment_failed",
            payload={"type": "invoice.payment_failed"},
        )
        self.assertEqual(b.get("status"), "fresh")
        self.assertNotEqual(token_a, b.get("claimToken"))
        self.assertTrue(self.store.complete_billing_event(
            event_id=eid, claim_token=b.get("claimToken"),
        ))
        self.assertFalse(self.store.complete_billing_event(
            event_id=eid, claim_token=token_a,
        ))
        snap = ref.get()
        self.assertEqual((snap.to_dict() or {}).get("status"), "completed")

    def test_legacy_record_treated_as_deduped(self) -> None:
        """A pre-claim/complete record (no `status` field) must be
        treated as already-processed to avoid replaying historical
        events."""
        db = _db()
        eid = _unique("legacy")
        # Simulate a legacy mark-at-start record.
        db.collection("billingEvents").document(eid).set({
            "eventId": eid,
            "eventType": "invoice.payment_succeeded",
            "payload": {"type": "invoice.payment_succeeded"},
            "createdAt": _utc_now(),
            # no "status", no "claimedAt", no "claimToken"
        })
        r = self.store.try_claim_billing_event(
            event_id=eid, event_type="invoice.payment_succeeded",
            payload={"type": "invoice.payment_succeeded"},
        )
        self.assertEqual(r.get("status"), "deduped")


class ApplyWebhookEventAtomicFirestoreTests(unittest.TestCase):
    """Fenced-apply (boundary 1): billing + minutes + event completion
    inside ONE Firestore transaction, aborting if the claim is lost."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.store = FirestoreMultiChurchStore()

    def _seed_org(self) -> str:
        db = _db()
        org_id = f"org_{uuid.uuid4().hex[:8]}"
        db.collection("organizations").document(org_id).set({
            "orgId": org_id,
            "name": f"Audit Church {org_id}",
            "plan": "trial",
            "status": "active",
            "billing": {
                "planKey": "trial",
                "status": "trialing",
                "stripeCustomerId": "cus_x",
                "stripeSubscriptionId": "sub_x",
            },
            "maxMinutesPerMonth": 500,
        })
        return org_id

    def test_apply_updates_billing_minutes_and_completes_event(self) -> None:
        db = _db()
        org_id = self._seed_org()
        eid = _unique("apply")
        a = self.store.try_claim_billing_event(
            event_id=eid, event_type="customer.subscription.updated",
            payload={"type": "customer.subscription.updated"},
        )
        self.assertEqual(a.get("status"), "fresh")
        next_billing = {
            "planKey": "growth", "status": "active",
            "stripeCustomerId": "cus_x", "stripeSubscriptionId": "sub_x",
            "limits": {"maxServiceKeys": 12},
        }
        ok = self.store.apply_webhook_event_atomic(
            org_id=org_id, billing=next_billing,
            monthly_minutes=1800,
            event_id=eid, claim_token=a.get("claimToken"),
        )
        self.assertTrue(ok)
        org = db.collection("organizations").document(org_id).get().to_dict() or {}
        self.assertEqual(org.get("maxMinutesPerMonth"), 1800)
        self.assertEqual((org.get("billing") or {}).get("planKey"), "growth")
        ev = db.collection("billingEvents").document(eid).get().to_dict() or {}
        self.assertEqual(ev.get("status"), "completed")

    def test_apply_refuses_when_claim_token_stale(self) -> None:
        """Slow worker whose lease was reclaimed must NOT be allowed
        to write billing or complete the event — the fenced apply
        aborts the transaction with no partial writes."""
        db = _db()
        org_id = self._seed_org()
        eid = _unique("apply-stale")
        a = self.store.try_claim_billing_event(
            event_id=eid, event_type="customer.subscription.updated",
            payload={"type": "customer.subscription.updated"},
        )
        token_a = a.get("claimToken")
        # Reclaim.
        ref = db.collection("billingEvents").document(eid)
        ref.update({"claimedAt": _utc_now() - timedelta(hours=1)})
        self.store.try_claim_billing_event(
            event_id=eid, event_type="customer.subscription.updated",
            payload={"type": "customer.subscription.updated"},
        )
        # Slow worker A attempts fenced-apply with its STALE token.
        ok = self.store.apply_webhook_event_atomic(
            org_id=org_id,
            billing={
                "planKey": "growth", "status": "active",
                "stripeCustomerId": "cus_x", "stripeSubscriptionId": "sub_x",
            },
            monthly_minutes=1800,
            event_id=eid, claim_token=token_a,
        )
        self.assertFalse(ok)
        # Verify no partial writes.
        org = db.collection("organizations").document(org_id).get().to_dict() or {}
        self.assertEqual(org.get("maxMinutesPerMonth"), 500)
        self.assertEqual((org.get("billing") or {}).get("planKey"), "trial")
        ev = ref.get().to_dict() or {}
        self.assertNotEqual(ev.get("status"), "completed")


class CheckoutAttemptFirestoreTests(unittest.TestCase):
    """Transactional validation of the checkout-attempt claim,
    including stable-request-parameter guard and parallel claims."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.store = FirestoreMultiChurchStore()

    def _seed_org(self) -> str:
        db = _db()
        org_id = f"org_{uuid.uuid4().hex[:8]}"
        db.collection("organizations").document(org_id).set({
            "orgId": org_id,
            "name": f"Audit Church {org_id}",
            "plan": "trial",
            "status": "active",
            "billing": {"planKey": "trial", "status": "trialing"},
        })
        return org_id

    def test_create_then_reuse_identical_params(self) -> None:
        org_id = self._seed_org()
        first = self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="uid_1",
        )
        self.assertEqual(first["action"], "create")
        second = self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="uid_1",
        )
        self.assertEqual(second["action"], "reuse")
        self.assertEqual(second["attemptId"], first["attemptId"])

    def test_reuse_refused_when_params_differ(self) -> None:
        org_id = self._seed_org()
        self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="uid_1",
        )
        # Same plan+interval, DIFFERENT uid → mismatch.
        second = self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="uid_2",
        )
        self.assertEqual(second["action"], "pending_params_mismatch")

    def test_pending_different_plan_refuses(self) -> None:
        org_id = self._seed_org()
        self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="uid_1",
        )
        second = self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="growth", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="uid_1",
        )
        self.assertEqual(second["action"], "pending_different_plan")

    def test_actual_parallel_claims_converge(self) -> None:
        """Four parallel threads for the same (plan, interval) must
        land on ONE attemptId. Only one thread sees 'create'; others
        see 'reuse' with the same id."""
        org_id = self._seed_org()
        gate = threading.Barrier(4)
        results: List[Dict[str, Any]] = []
        rlock = threading.Lock()

        def _call() -> None:
            gate.wait()
            r = self.store.claim_checkout_attempt(
                org_id=org_id, plan_key="starter", interval="month",
                success_url="https://x/ok", cancel_url="https://x/cancel",
                requested_by_uid="uid_1",
            )
            with rlock:
                results.append(r)

        threads = [threading.Thread(target=_call) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()

        creates = [r for r in results if r.get("action") == "create"]
        reuses = [r for r in results if r.get("action") == "reuse"]
        self.assertEqual(len(creates), 1, results)
        self.assertEqual(len(creates) + len(reuses), 4, results)
        ids = {r["attemptId"] for r in results}
        self.assertEqual(len(ids), 1, ids)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
