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
from typing import Any, Dict, List, Optional

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
        result = self.store.apply_webhook_event_atomic(
            org_id=org_id, billing=next_billing,
            monthly_minutes=1800,
            event_id=eid, claim_token=a.get("claimToken"),
        )
        self.assertTrue(result.get("applied"))
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
        ref = db.collection("billingEvents").document(eid)
        ref.update({"claimedAt": _utc_now() - timedelta(hours=1)})
        self.store.try_claim_billing_event(
            event_id=eid, event_type="customer.subscription.updated",
            payload={"type": "customer.subscription.updated"},
        )
        result = self.store.apply_webhook_event_atomic(
            org_id=org_id,
            billing={
                "planKey": "growth", "status": "active",
                "stripeCustomerId": "cus_x", "stripeSubscriptionId": "sub_x",
            },
            monthly_minutes=1800,
            event_id=eid, claim_token=token_a,
        )
        self.assertFalse(result.get("applied"))
        self.assertEqual(result.get("reason"), "claim_lost")
        org = db.collection("organizations").document(org_id).get().to_dict() or {}
        self.assertEqual(org.get("maxMinutesPerMonth"), 500)
        self.assertEqual((org.get("billing") or {}).get("planKey"), "trial")
        ev = ref.get().to_dict() or {}
        self.assertNotEqual(ev.get("status"), "completed")

    def test_apply_refuses_on_stale_billing_snapshot(self) -> None:
        """CAS protection: a pre-computed `billing` snapshot whose
        `updatedAt` no longer matches the current org state (because
        a sibling delivery updated the org in between) must be
        rejected with reason=stale_snapshot."""
        db = _db()
        org_id = self._seed_org()
        eid = _unique("apply-cas")
        a = self.store.try_claim_billing_event(
            event_id=eid, event_type="invoice.payment_failed",
            payload={"type": "invoice.payment_failed"},
        )
        token = a.get("claimToken")
        # Capture CURRENT billing.updatedAt (may be None since seeded
        # without one). Simulate a sibling update that bumps it.
        bogus_baseline_dt = _utc_now() - timedelta(hours=1)
        result = self.store.apply_webhook_event_atomic(
            org_id=org_id,
            billing={"status": "past_due"},
            monthly_minutes=None,
            event_id=eid, claim_token=token,
            expected_billing_updated_at=bogus_baseline_dt,
        )
        self.assertFalse(result.get("applied"))
        self.assertEqual(result.get("reason"), "stale_snapshot")
        ev = db.collection("billingEvents").document(eid).get().to_dict() or {}
        self.assertNotEqual(ev.get("status"), "completed")

    def test_apply_preserves_soft_cap_reset(self) -> None:
        """softCapReached must reset when the new limit >= currentMonthMinutes."""
        db = _db()
        org_id = self._seed_org()
        db.collection("organizations").document(org_id).set({
            "currentMonthMinutes": 500,
            "maxMinutesPerMonth": 600,
            "softCapReached": True,
        }, merge=True)
        eid = _unique("apply-soft")
        a = self.store.try_claim_billing_event(
            event_id=eid, event_type="customer.subscription.updated",
            payload={"type": "customer.subscription.updated"},
        )
        result = self.store.apply_webhook_event_atomic(
            org_id=org_id,
            billing={"planKey": "growth", "status": "active",
                     "stripeCustomerId": "cus_x", "stripeSubscriptionId": "sub_x"},
            monthly_minutes=1800,
            event_id=eid, claim_token=a.get("claimToken"),
        )
        self.assertTrue(result.get("applied"))
        org = db.collection("organizations").document(org_id).get().to_dict() or {}
        self.assertEqual(org.get("maxMinutesPerMonth"), 1800)
        self.assertFalse(bool(org.get("softCapReached")))


class CheckoutMutationsBumpBillingUpdatedAtFirestoreTests(unittest.TestCase):
    """Review-pass v5 fix for issue #2 on Firestore: both
    `claim_checkout_attempt` and `record_checkout_attempt_session`
    bump `billing.updatedAt` so a concurrent webhook handler's CAS
    check catches these mutations.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.store = FirestoreMultiChurchStore()

    def _seed_org(self) -> str:
        db = _db()
        org_id = f"org_{uuid.uuid4().hex[:8]}"
        db.collection("organizations").document(org_id).set({
            "orgId": org_id, "name": f"B9 {org_id}",
            "plan": "trial", "status": "active",
            "billing": {"planKey": "trial", "status": "trialing"},
        })
        return org_id

    def test_claim_bumps_billing_updated_at(self) -> None:
        db = _db()
        org_id = self._seed_org()
        before = (db.collection("organizations").document(org_id).get().to_dict()
                  or {}).get("billing", {}).get("updatedAt")
        self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="u1",
        )
        after = (db.collection("organizations").document(org_id).get().to_dict()
                 or {}).get("billing", {}).get("updatedAt")
        self.assertNotEqual(before, after)

    def test_record_session_bumps_billing_updated_at(self) -> None:
        db = _db()
        org_id = self._seed_org()
        claim = self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="u1",
        )
        before = (db.collection("organizations").document(org_id).get().to_dict()
                  or {}).get("billing", {}).get("updatedAt")
        self.store.record_checkout_attempt_session(
            org_id=org_id, attempt_id=claim["attemptId"],
            session_id="cs_x", session_url="https://x/",
        )
        after = (db.collection("organizations").document(org_id).get().to_dict()
                 or {}).get("billing", {}).get("updatedAt")
        self.assertNotEqual(before, after)

    def test_apply_sees_cas_mismatch_after_concurrent_claim(self) -> None:
        """Fenced apply must refuse (stale_snapshot) when a concurrent
        `claim_checkout_attempt` bumped `billing.updatedAt` between the
        caller's read and the atomic apply."""
        db = _db()
        org_id = self._seed_org()
        # Seed an explicit baseline so the CAS compare has a non-None
        # expected value.
        baseline = _utc_now() - timedelta(seconds=5)
        db.collection("organizations").document(org_id).update({
            "billing.updatedAt": baseline,
        })
        # Interleaving claim bumps updatedAt.
        self.store.claim_checkout_attempt(
            org_id=org_id, plan_key="starter", interval="month",
            success_url="https://x/ok", cancel_url="https://x/cancel",
            requested_by_uid="u1",
        )
        # Now a webhook that captured `baseline` tries to apply.
        eid = _unique("apply-cas-checkout")
        a = self.store.try_claim_billing_event(
            event_id=eid, event_type="customer.subscription.updated",
            payload={"type": "customer.subscription.updated"},
        )
        result = self.store.apply_webhook_event_atomic(
            org_id=org_id,
            billing={"planKey": "growth", "status": "active"},
            monthly_minutes=1800,
            event_id=eid, claim_token=a.get("claimToken"),
            expected_billing_updated_at=baseline,
        )
        self.assertFalse(result.get("applied"))
        self.assertEqual(result.get("reason"), "stale_snapshot")
        # The pending-checkout-attempt from the concurrent claim is
        # NOT erased by the aborted webhook apply.
        after = (db.collection("organizations").document(org_id).get().to_dict()
                 or {}).get("billing", {}).get("pendingCheckoutAttempt")
        self.assertIsNotNone(after)


class SetOrgBillingProfileCASFirestoreTests(unittest.TestCase):
    """Review-pass v7 fix (gap #2): `set_org_billing_profile` on
    Firestore accepts an optional CAS token. When the token no
    longer matches the stored `billing.updatedAt`, the method
    raises StaleBillingProfileError inside the transaction with no
    partial writes. Models the hydrate-vs-webhook reverse race.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.store = FirestoreMultiChurchStore()

    def _seed_org(self) -> str:
        db = _db()
        org_id = f"org_{uuid.uuid4().hex[:8]}"
        now = _utc_now()
        db.collection("organizations").document(org_id).set({
            "orgId": org_id, "name": f"B14 {org_id}",
            "plan": "starter", "status": "active",
            "billing": {"planKey": "starter", "status": "active", "updatedAt": now},
        })
        return org_id

    def test_cas_mismatch_raises_stale_billing_profile_error(self) -> None:
        from app.services.multichurch_store import StaleBillingProfileError
        db = _db()
        org_id = self._seed_org()
        baseline = (db.collection("organizations").document(org_id).get().to_dict()
                    or {}).get("billing", {}).get("updatedAt")
        # Simulate a sibling writer that already updated billing.
        self.store.set_org_billing_profile(
            org_id=org_id,
            billing={"planKey": "growth", "status": "active",
                     "stripeCustomerId": "cus", "stripeSubscriptionId": "sub"},
        )
        # Stale CAS token (baseline) must now be rejected.
        with self.assertRaises(StaleBillingProfileError):
            self.store.set_org_billing_profile(
                org_id=org_id,
                billing={"planKey": "starter", "status": "active",
                         "stripeCustomerId": "cus", "stripeSubscriptionId": "sub"},
                expected_billing_updated_at=baseline,
            )
        # Stored state unchanged by the failed call.
        final = (db.collection("organizations").document(org_id).get().to_dict()
                 or {}).get("billing", {})
        self.assertEqual(final.get("planKey"), "growth")


class PatchOrgBillingFieldsFirestoreTests(unittest.TestCase):
    """Review-pass v8 fix (gap #1): `patch_org_billing_fields` on
    Firestore preserves unrelated newer fields from a concurrent
    writer. Models the customer-create ↔ webhook reverse race
    without full-snapshot overwrite.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.store = FirestoreMultiChurchStore()

    def _seed_org(self) -> str:
        db = _db()
        org_id = f"org_{uuid.uuid4().hex[:8]}"
        now = _utc_now()
        db.collection("organizations").document(org_id).set({
            "orgId": org_id, "name": f"B15fs {org_id}",
            "plan": "trial", "status": "trial",
            "billing": {
                "planKey": "trial", "status": "trialing",
                "stripeCustomerId": None, "stripeSubscriptionId": None,
                "priceId": None, "updatedAt": now,
            },
        })
        return org_id

    def test_patch_preserves_concurrent_webhook_fields(self) -> None:
        db = _db()
        org_id = self._seed_org()
        # Simulate the webhook's commit: Growth + sub + price + active.
        self.store.set_org_billing_profile(
            org_id=org_id,
            billing={
                "planKey": "growth", "status": "active",
                "stripeCustomerId": None,  # customer-create hasn't landed yet
                "stripeSubscriptionId": "sub_b15fs",
                "priceId": "price_growth",
            },
        )
        after_webhook = (db.collection("organizations").document(org_id).get().to_dict()
                         or {}).get("billing") or {}
        self.assertEqual(after_webhook.get("planKey"), "growth")

        # Now the (stale) customer-create helper patches ONLY stripeCustomerId.
        # The old full-snapshot path would have reverted planKey/sub/price;
        # the targeted-field patch must preserve them.
        self.store.patch_org_billing_fields(
            org_id=org_id,
            updates={"stripeCustomerId": "cus_b15fs"},
        )
        final = (db.collection("organizations").document(org_id).get().to_dict()
                 or {}).get("billing") or {}
        self.assertEqual(final.get("stripeCustomerId"), "cus_b15fs")
        self.assertEqual(final.get("planKey"), "growth")
        self.assertEqual(final.get("stripeSubscriptionId"), "sub_b15fs")
        self.assertEqual(final.get("priceId"), "price_growth")
        self.assertEqual(str(final.get("status") or "").lower(), "active")

    def test_patch_merges_limits_key_by_key(self) -> None:
        db = _db()
        org_id = self._seed_org()
        self.store.set_org_billing_profile(
            org_id=org_id,
            billing={
                "planKey": "growth", "status": "active",
                "limits": {"maxServiceKeys": 12, "customFlag": True},
            },
        )
        self.store.patch_org_billing_fields(
            org_id=org_id,
            updates={"limits": {"maxServiceKeys": 20}},
        )
        final = (db.collection("organizations").document(org_id).get().to_dict()
                 or {}).get("billing") or {}
        self.assertEqual((final.get("limits") or {}).get("maxServiceKeys"), 20)
        self.assertEqual(
            (final.get("limits") or {}).get("customFlag"), True,
            "nested limits merge dropped a sibling key",
        )


class PatchOrgBillingFieldsExpectedFirestoreTests(unittest.TestCase):
    """Review-pass v9 fix (gaps #1/#2): the `expected` parameter on
    `patch_org_billing_fields` must enforce per-field identity
    match atomically inside the Firestore transaction. When any
    `expected` key no longer matches current, raises
    `StaleBillingPatchError` and nothing is written.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.store = FirestoreMultiChurchStore()

    def _seed_org(self, planKey: str = "starter") -> str:
        db = _db()
        org_id = f"org_{uuid.uuid4().hex[:8]}"
        now = _utc_now()
        db.collection("organizations").document(org_id).set({
            "orgId": org_id, "name": f"B16fs {org_id}",
            "plan": planKey, "status": "active",
            "billing": {
                "planKey": planKey, "status": "active",
                "stripeCustomerId": "cus_old",
                "stripeSubscriptionId": "sub_old",
                "priceId": "price_starter",
                "updatedAt": now,
            },
        })
        return org_id

    def test_expected_mismatch_raises_and_writes_nothing(self) -> None:
        from app.services.multichurch_store import StaleBillingPatchError
        db = _db()
        org_id = self._seed_org()
        # Simulate the webhook replacing stripeCustomerId with a new id.
        self.store.patch_org_billing_fields(
            org_id=org_id, updates={"stripeCustomerId": "cus_new"},
        )
        # A stale normalizer tries to correct stripeCustomerId → cus_old_norm
        # with expected=cus_old. Must be rejected.
        with self.assertRaises(StaleBillingPatchError) as cm:
            self.store.patch_org_billing_fields(
                org_id=org_id,
                updates={"stripeCustomerId": "cus_old_norm"},
                expected={"stripeCustomerId": "cus_old"},
            )
        self.assertIn("stripeCustomerId", cm.exception.mismatches)
        final = (db.collection("organizations").document(org_id).get().to_dict()
                 or {}).get("billing") or {}
        self.assertEqual(final.get("stripeCustomerId"), "cus_new",
                         "rejected stale patch still wrote to Firestore")

    def test_expected_match_allows_write(self) -> None:
        db = _db()
        org_id = self._seed_org()
        # Patch with expected matching current — must succeed.
        self.store.patch_org_billing_fields(
            org_id=org_id,
            updates={"stripeCustomerId": "cus_new"},
            expected={"stripeCustomerId": "cus_old"},
        )
        final = (db.collection("organizations").document(org_id).get().to_dict()
                 or {}).get("billing") or {}
        self.assertEqual(final.get("stripeCustomerId"), "cus_new")

    def test_clear_stale_refs_abandons_when_identity_differs(self) -> None:
        """Models gap #2 at the Firestore boundary: calling
        patch_org_billing_fields with expected={"stripeCustomerId":
        "cus_obsolete"} + updates clearing all refs is rejected when
        the current customer id is "cus_new"."""
        from app.services.multichurch_store import StaleBillingPatchError
        db = _db()
        org_id = self._seed_org()
        # Webhook commits new identity.
        self.store.patch_org_billing_fields(
            org_id=org_id, updates={
                "stripeCustomerId": "cus_new",
                "stripeSubscriptionId": "sub_new",
                "priceId": "price_growth",
                "planKey": "growth",
            },
        )
        with self.assertRaises(StaleBillingPatchError):
            self.store.patch_org_billing_fields(
                org_id=org_id,
                updates={
                    "stripeCustomerId": None,
                    "stripeSubscriptionId": None,
                    "priceId": None,
                },
                expected={"stripeCustomerId": "cus_obsolete"},
            )
        final = (db.collection("organizations").document(org_id).get().to_dict()
                 or {}).get("billing") or {}
        self.assertEqual(final.get("stripeCustomerId"), "cus_new")
        self.assertEqual(final.get("stripeSubscriptionId"), "sub_new")
        self.assertEqual(final.get("priceId"), "price_growth")
        self.assertEqual(final.get("planKey"), "growth")


class CustomerCreateHelperFirestoreTests(unittest.TestCase):
    """Review-pass v10 fix: the final write in
    `_create_missing_stripe_customer` must be rejected if the stored
    `stripeCustomerId` changed while the Stripe `create_customer`
    call was in flight. At the Firestore boundary, this reduces to
    asserting `patch_org_billing_fields(expected={"stripeCustomerId":
    observed_old}, updates={"stripeCustomerId": helper_made})` is
    rejected when a sibling writer has already set a different id.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.store = FirestoreMultiChurchStore()

    def _seed_org_with_customer(self, customer_id: Optional[str]) -> str:
        db = _db()
        org_id = f"org_{uuid.uuid4().hex[:8]}"
        now = _utc_now()
        db.collection("organizations").document(org_id).set({
            "orgId": org_id, "name": f"B18fs {org_id}",
            "plan": "trial", "status": "trial",
            "billing": {
                "planKey": "trial", "status": "trialing",
                "stripeCustomerId": customer_id,
                "updatedAt": now,
            },
        })
        return org_id

    def test_helper_write_rejected_when_webhook_committed_different_id(self) -> None:
        from app.services.multichurch_store import StaleBillingPatchError
        db = _db()
        org_id = self._seed_org_with_customer(None)
        # Model what the helper OBSERVED pre-Stripe-call: no customer.
        observed_customer_id = None
        # Sibling writer (webhook) committed cus_new in the meantime.
        self.store.patch_org_billing_fields(
            org_id=org_id, updates={"stripeCustomerId": "cus_new"},
        )
        # Helper's final write must be rejected.
        with self.assertRaises(StaleBillingPatchError) as cm:
            self.store.patch_org_billing_fields(
                org_id=org_id,
                updates={"stripeCustomerId": "cus_helper_made"},
                expected={"stripeCustomerId": observed_customer_id},
            )
        self.assertIn("stripeCustomerId", cm.exception.mismatches)
        final = (db.collection("organizations").document(org_id).get().to_dict()
                 or {}).get("billing") or {}
        self.assertEqual(final.get("stripeCustomerId"), "cus_new",
                         "sibling customer id was overwritten by the stale helper patch")

    def test_helper_write_lands_when_observation_still_matches(self) -> None:
        db = _db()
        org_id = self._seed_org_with_customer(None)
        # No sibling writer. Helper's final write should land.
        self.store.patch_org_billing_fields(
            org_id=org_id,
            updates={"stripeCustomerId": "cus_helper_made"},
            expected={"stripeCustomerId": None},
        )
        final = (db.collection("organizations").document(org_id).get().to_dict()
                 or {}).get("billing") or {}
        self.assertEqual(final.get("stripeCustomerId"), "cus_helper_made")


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
