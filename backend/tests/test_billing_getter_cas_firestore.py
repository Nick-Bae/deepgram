"""Firestore-emulator regression for the transactional
`FirestoreMultiChurchStore.get_org_billing_profile` getter.

Prior-art: the frozen implementation at `bb3ebd8b` used
`org_ref.set({..., SERVER_TIMESTAMP}, merge=True)` with no CAS token
on the writeback branch. A reader-triggered writeback could clobber
a concurrent `set_org_billing_profile` CAS commit. Offline
reproduction confirmed this (tmp/stripe-sandbox/offline-reproduction/
REPRODUCTION-RESULTS-V7.md, Case D).

The replacement wraps read + optional writeback in a Firestore
`@transactional`. Firestore detects conflicts; the transaction
retries against a fresh snapshot. The returned billing's updatedAt
equals what is persisted (same client-side `now`), so callers see a
token that matches the stored doc.

Skips when FIRESTORE_EMULATOR_HOST is unset. CI's
`firestore-emulator-tests` job sets it.
"""
from __future__ import annotations

import copy
import os
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
    raise unittest.SkipTest(
        "FIRESTORE_EMULATOR_HOST not set — Firestore-emulator tests "
        "skipped. CI's firestore-emulator-tests job sets this."
    )

from google.cloud import firestore as gcf
from google.cloud.firestore import DELETE_FIELD

from app.services.multichurch_store import (
    FirestoreMultiChurchStore,
    StaleBillingProfileError,
)

# Matches the hard-coded database name in FirestoreMultiChurchStore.__init__
# (multichurch_store.py:3893). The test db client must point at the same
# named database the store writes to.
_APP_DB_NAME = "worship-translation"
_PROJECT = os.environ.setdefault("GOOGLE_CLOUD_PROJECT",
                                  "stripe-audit-billing-getter-cas-tests")


def _db() -> gcf.Client:
    return gcf.Client(project=_PROJECT, database=_APP_DB_NAME)


def _slug() -> str:
    return f"gcas-{uuid.uuid4().hex[:12]}"


def _snapshot(db: gcf.Client, slug: str) -> Dict[str, Any]:
    d = db.collection("organizations").document(slug).get().to_dict() or {}
    return d.get("billing") or {}


def _bootstrap(store: FirestoreMultiChurchStore, slug: str) -> None:
    db = _db()
    db.collection("organizations").document(slug).delete()
    for sub in ("services", "members"):
        for d in db.collection("organizations").document(slug).collection(sub).stream():
            d.reference.delete()
    store.bootstrap_owner_org(
        owner_uid=f"owner-{slug}",
        owner_email=f"{slug}@fake.invalid",
        owner_display_name=slug,
        church_name=slug,
        church_slug=slug,
        timezone="America/Chicago",
        source="ko",
        target="en",
    )


class TestGetterCASTransactional(unittest.TestCase):
    """Confirmed regression suite for the transactional getter patch.

    Covers:
      - control: fresh bootstrap (no writeback needed) — returned == stored.
      - missing billing.updatedAt — candidate synthesizes; returned == stored.
      - legacy dotted-key lift (billing.priceId as a dotted top-level key) —
        lifted into nested billing, writeback fires, returned == stored.
      - normalization that changes an existing document (invalid status) —
        writeback fires, returned.updatedAt == stored.updatedAt.
      - missing org — ValueError('org_not_found') propagates.
      - parallel race: concurrent CAS writer's commit SURVIVES the getter's
        writeback. Measured retry evidence via normalize-invocation counter.
      - exception propagation: a sentinel RuntimeError raised inside the
        transaction body propagates unmasked.
    """

    def setUp(self) -> None:
        self.store = FirestoreMultiChurchStore()
        self.db = _db()
        self.slug = _slug()

    def tearDown(self) -> None:
        try:
            self.db.collection("organizations").document(self.slug).delete()
        except Exception:
            pass

    # ── Control: no-writeback path ──────────────────────────────────────────
    def test_no_writeback_returns_fresh_bootstrap_state_unchanged(self) -> None:
        _bootstrap(self.store, self.slug)
        pre = _snapshot(self.db, self.slug)
        returned = self.store.get_org_billing_profile(org_id=self.slug)
        post = _snapshot(self.db, self.slug)
        # No change needed → stored unchanged, returned carries pre-existing token.
        self.assertEqual(pre.get("updatedAt"), post.get("updatedAt"))
        self.assertEqual(returned.get("updatedAt"), post.get("updatedAt"))

    # ── Missing billing.updatedAt ───────────────────────────────────────────
    def test_missing_updatedAt_synthesized_and_persisted(self) -> None:
        _bootstrap(self.store, self.slug)
        self.db.collection("organizations").document(self.slug).update({
            "billing.updatedAt": DELETE_FIELD,
        })
        # Confirm absence before invocation.
        pre_raw = (self.db.collection("organizations").document(self.slug)
                      .get().to_dict() or {}).get("billing") or {}
        self.assertNotIn("updatedAt", pre_raw)
        returned = self.store.get_org_billing_profile(org_id=self.slug)
        post = _snapshot(self.db, self.slug)
        # updatedAt now present AND returned matches stored.
        self.assertIsNotNone(post.get("updatedAt"))
        self.assertEqual(returned.get("updatedAt"), post.get("updatedAt"))

    # ── Legacy dotted-key lift ──────────────────────────────────────────────
    def test_legacy_dotted_key_lifted_into_nested_billing(self) -> None:
        _bootstrap(self.store, self.slug)
        self.db.collection("organizations").document(self.slug).set({
            "billing.priceId": "price_lifted_test",
        }, merge=True)
        returned = self.store.get_org_billing_profile(org_id=self.slug)
        post = _snapshot(self.db, self.slug)
        self.assertEqual(returned.get("priceId"), "price_lifted_test")
        self.assertEqual(post.get("priceId"), "price_lifted_test")
        self.assertEqual(returned.get("updatedAt"), post.get("updatedAt"))

    # ── Normalization CHANGES existing doc (writeback path) ─────────────────
    def test_normalization_resets_invalid_status_and_writes_back(self) -> None:
        _bootstrap(self.store, self.slug)
        self.db.collection("organizations").document(self.slug).update({
            "billing.status": "invalid_status_xyz",
        })
        returned = self.store.get_org_billing_profile(org_id=self.slug)
        post = _snapshot(self.db, self.slug)
        self.assertNotEqual(post.get("status"), "invalid_status_xyz")
        self.assertEqual(returned.get("status"), post.get("status"))
        self.assertEqual(returned.get("updatedAt"), post.get("updatedAt"))

    # ── Exception propagation (missing org) ─────────────────────────────────
    def test_missing_org_raises_ValueError_unmasked(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.store.get_org_billing_profile(org_id="does-not-exist")
        self.assertEqual(str(ctx.exception), "org_not_found")

    # ── Exception propagation (inside-transaction raise) ────────────────────
    def test_runtime_error_inside_transaction_propagates_unmasked(self) -> None:
        _bootstrap(self.store, self.slug)
        self.db.collection("organizations").document(self.slug).set({
            "billing.priceId": "price_tx_fail_dotted",
        }, merge=True)
        import app.services.multichurch_store as mcs
        orig = mcs._normalize_billing_state
        sentinel = RuntimeError("tx-fail-propagation-test")
        def _raise(_payload, *, plan_key, now):
            raise sentinel
        mcs._normalize_billing_state = _raise
        try:
            with self.assertRaises(RuntimeError) as ctx:
                self.store.get_org_billing_profile(org_id=self.slug)
            self.assertIs(ctx.exception, sentinel)
        finally:
            mcs._normalize_billing_state = orig

    # ── Parallel race: concurrent CAS writer's commit survives ──────────────
    def test_concurrent_cas_commit_survives_getter_writeback(self) -> None:
        """Reader A and writer B race. A's getter must not clobber B's CAS
        commit. Measured retry evidence: A's normalize invocation count
        is captured via a thread-name-narrowed monkeypatch.

        Expected invariant: `B_committed=True` and final state carries
        B's identity fields (`active`, correct priceId, cus_B, sub_B).
        """
        _bootstrap(self.store, self.slug)
        self.db.collection("organizations").document(self.slug).set({
            "billing.priceId": "price_race_dotted_initial",
        }, merge=True)

        import app.services.multichurch_store as mcs
        orig_normalize = mcs._normalize_billing_state
        A_THREAD = f"race_A_{self.slug}"
        normalize_counts = {"A": 0, "other": 0}
        def _counting(payload, *, plan_key, now):
            if threading.current_thread().name == A_THREAD:
                normalize_counts["A"] += 1
            else:
                normalize_counts["other"] += 1
            return orig_normalize(payload, plan_key=plan_key, now=now)
        mcs._normalize_billing_state = _counting
        try:
            barrier = threading.Barrier(2, timeout=15.0)
            A_result: Dict[str, Any] = {"returned": None, "exc": None}
            B_result: Dict[str, Any] = {"committed": False, "outcome": None, "exc": None}

            def _run_A() -> None:
                try:
                    barrier.wait()
                    A_result["returned"] = self.store.get_org_billing_profile(
                        org_id=self.slug,
                    )
                except BaseException as exc:
                    A_result["exc"] = (type(exc).__name__, repr(exc))

            def _run_B() -> None:
                try:
                    barrier.wait()
                    doc = (self.db.collection("organizations").document(self.slug)
                               .get().to_dict() or {})
                    cur = dict(doc.get("billing") or {})
                    token = cur.get("updatedAt")
                    new_billing = dict(cur)
                    new_billing.update({
                        "status": "active",
                        "priceId": "price_race_committed",
                        "stripeCustomerId": "cus_race_B",
                        "stripeSubscriptionId": "sub_race_B",
                        "planKey": "starter",
                        "limits": {"maxServiceKeys": 5},
                    })
                    self.store.set_org_billing_profile(
                        org_id=self.slug, billing=new_billing,
                        expected_billing_updated_at=token,
                    )
                    B_result["committed"] = True
                    B_result["outcome"] = "commit"
                except StaleBillingProfileError as exc:
                    B_result["outcome"] = "stale_cas_expected"
                    B_result["exc"] = (type(exc).__name__, repr(exc))
                except BaseException as exc:
                    B_result["outcome"] = "unexpected"
                    B_result["exc"] = (type(exc).__name__, repr(exc))

            tA = threading.Thread(target=_run_A, name=A_THREAD, daemon=False)
            tB = threading.Thread(target=_run_B,
                                   name=f"race_B_{self.slug}", daemon=False)
            tA.start(); tB.start()
            tA.join(timeout=30.0); tB.join(timeout=30.0)

            self.assertFalse(tA.is_alive(), "A thread must terminate within 30s")
            self.assertFalse(tB.is_alive(), "B thread must terminate within 30s")
            self.assertIsNone(A_result["exc"],
                              f"A must not raise: {A_result['exc']}")
            self.assertIn(B_result["outcome"], {"commit", "stale_cas_expected"},
                          f"B outcome must be commit or stale_cas, got: "
                          f"{B_result['outcome']} ({B_result['exc']})")
            self.assertGreaterEqual(normalize_counts["A"], 1,
                                     "A's transactional body must execute at least once")

            # Token ordering (unconditional presence + monotonic).
            returned_token = (A_result["returned"] or {}).get("updatedAt")
            raw_final = (self.db.collection("organizations").document(self.slug)
                           .get().to_dict() or {})
            final_billing = raw_final.get("billing") or {}
            final_token = final_billing.get("updatedAt")
            self.assertIsNotNone(returned_token, "A's returned token must be present")
            self.assertIsNotNone(final_token, "final stored token must be present")
            self.assertGreaterEqual(final_token, returned_token,
                                     f"final_token must be monotonic >= A_token "
                                     f"(final={final_token!r} vs A={returned_token!r})")

            # If B committed, every one of its identity fields must survive.
            if B_result["outcome"] == "commit":
                self.assertEqual(final_billing.get("stripeCustomerId"), "cus_race_B")
                self.assertEqual(final_billing.get("stripeSubscriptionId"), "sub_race_B")
                self.assertEqual(final_billing.get("status"), "active")
                self.assertEqual(final_billing.get("priceId"), "price_race_committed")
            elif B_result["outcome"] == "stale_cas_expected":
                # A won; B rejected via CAS. Final state must reflect A's
                # transactional writeback: the lifted priceId from the
                # dotted key is now present in the nested billing. The
                # original top-level dotted key remains as dust (migration
                # does not force-delete legacy aliases).
                self.assertEqual(final_billing.get("priceId"),
                                 "price_race_dotted_initial",
                                 "Stale-CAS branch: final priceId must "
                                 "reflect A's writeback (lifted from dotted)")
                # B's identity fields must NOT be present (because B's
                # CAS commit was rejected).
                self.assertNotEqual(final_billing.get("stripeCustomerId"), "cus_race_B")
                self.assertNotEqual(final_billing.get("stripeSubscriptionId"), "sub_race_B")
        finally:
            mcs._normalize_billing_state = orig_normalize


    # ── Idempotence regression (NOT overwrite protection) ─────────────────
    # Reviewer-corrected label (v3): the sequential test exercises the
    # token-churn property that distinguishes v2 (fixed) from v1
    # (unfixed) patches, NOT the pre-patch overwrite bug. A separate
    # deterministic overwrite test (below) covers that specific case.
    def test_sequential_second_read_after_external_write_is_idempotent(self) -> None:
        """Idempotence: external write between two getter calls, no
        token rotation on the second read. Fails against the v1 patch
        (which inherited the `lifted_legacy_fields` always-trigger).
        Passes against v2+. Does NOT exercise the pre-patch stale-read
        overwrite bug — see `test_patched_getter_retries_on_stale_read`."""
        _bootstrap(self.store, self.slug)
        self.db.collection("organizations").document(self.slug).set({
            "billing.priceId": "price_dotted_initial",
        }, merge=True)

        # First getter call — migrates the dotted lift.
        first = self.store.get_org_billing_profile(org_id=self.slug)

        # External writer commits new state via raw .set (not through a
        # transaction, so no lock contention). Advance the timestamp past
        # the first getter's token to simulate a real commit happening
        # after the migration.
        external_time = datetime.now(timezone.utc) + timedelta(seconds=1)
        external_billing = dict(first)
        external_billing.update({
            "status": "active",
            "priceId": "price_external_committed",
            "stripeCustomerId": "cus_external_committed",
            "stripeSubscriptionId": "sub_external_committed",
            "planKey": "starter",
            "limits": {"maxServiceKeys": 5},
            "updatedAt": external_time,
        })
        self.db.collection("organizations").document(self.slug).set(
            {"billing": external_billing, "updatedAt": external_time},
            merge=True,
        )
        external_state = _snapshot(self.db, self.slug)
        self.assertEqual(external_state.get("stripeCustomerId"),
                         "cus_external_committed")

        # Second getter call.
        second = self.store.get_org_billing_profile(org_id=self.slug)
        final = _snapshot(self.db, self.slug)

        # Distinguishing assertions. The pre-patch getter's second call
        # would:
        #   - fire writeback (lifted_legacy_fields=True from the still-
        #     present dotted key),
        #   - `set({"billing": normalized_payload, "updatedAt":
        #     SERVER_TIMESTAMP}, merge=True)` where `normalized_payload`
        #     is the external state (dotted lift is a no-op fill-in),
        #   - rotate the token to a new SERVER_TIMESTAMP (different from
        #     external's committed time).
        # Both patched and original preserve external's identity fields
        # (because the lifted payload mirrors external). The STRONG
        # distinguisher is the TOKEN: the patched getter preserves it;
        # the original rotates it.
        self.assertEqual(final.get("stripeCustomerId"),
                         "cus_external_committed",
                         "External state's stripeCustomerId must survive")
        self.assertEqual(final.get("stripeSubscriptionId"),
                         "sub_external_committed")
        self.assertEqual(final.get("status"), "active")
        self.assertEqual(final.get("priceId"), "price_external_committed")

        # KEY distinguisher: token preserved across the second read.
        self.assertEqual(
            final.get("updatedAt"), external_state.get("updatedAt"),
            f"Stored token must equal external writer's committed token "
            f"(no writeback on a stable doc). The pre-patch getter would "
            f"rotate the token to a new SERVER_TIMESTAMP because "
            f"`lifted_legacy_fields=True` triggers unconditional writeback. "
            f"(final={final.get('updatedAt')!r} vs "
            f"external={external_state.get('updatedAt')!r})",
        )
        self.assertEqual(
            second.get("updatedAt"), external_state.get("updatedAt"),
            "Second getter's returned token must equal external's "
            "committed token (returned == stored)",
        )

    # ── Idempotence: repeated reads of stable doc preserve CAS token ─────
    def test_idempotent_second_read_preserves_updatedAt(self) -> None:
        """Two consecutive getter calls on a doc without pending changes
        must return the SAME `updatedAt` — the normalization writeback
        must not rotate the CAS token on a no-op."""
        _bootstrap(self.store, self.slug)
        first = self.store.get_org_billing_profile(org_id=self.slug)
        second = self.store.get_org_billing_profile(org_id=self.slug)
        self.assertEqual(
            first.get("updatedAt"), second.get("updatedAt"),
            f"Second read must preserve the CAS token when nothing has "
            f"changed (first={first.get('updatedAt')!r}, "
            f"second={second.get('updatedAt')!r}). The pre-patch getter "
            f"would rotate the token on every read of a doc with dotted "
            f"legacy keys."
        )

    def test_idempotent_dotted_key_matching_nested_no_churn(self) -> None:
        """A dotted top-level key whose value is already represented in
        the nested billing block must NOT trigger writeback — repeated
        reads preserve the CAS token."""
        _bootstrap(self.store, self.slug)
        # First get to establish the normalized state.
        first = self.store.get_org_billing_profile(org_id=self.slug)
        # Write a dotted-legacy alias of a value that's ALREADY in nested.
        current_price = first.get("priceId")
        self.db.collection("organizations").document(self.slug).set({
            "billing.priceId": current_price,  # ← same as nested
        }, merge=True)
        # Read again. The dotted key lifts to a value matching nested, so
        # normalization is a no-op; no writeback; same token.
        second = self.store.get_org_billing_profile(org_id=self.slug)
        self.assertEqual(
            first.get("updatedAt"), second.get("updatedAt"),
            "Dotted key matching nested value must not rotate updatedAt",
        )

    # ── Monotonic write-time: a writeback cannot move the stored
    # updatedAt backward past an existing (future-ish) token ─────────────
    def test_writeback_advances_past_future_updatedAt(self) -> None:
        """Pre-stage a doc where the on-disk `billing.updatedAt` is
        ahead of wall clock, plus a dotted-legacy value that forces
        a getter writeback. The patched getter must advance its write
        timestamp past the snapshot's token (NOT move it backward).

        This exercises the per-attempt timestamp advancement at
        `multichurch_store.py:get_org_billing_profile` (snapshot_token
        guard). Pre-patch behavior would use `_utcnow()` directly,
        producing a timestamp OLDER than the already-stored token."""
        _bootstrap(self.store, self.slug)
        # Force a normalization-driven writeback by writing an INVALID
        # status (which normalize rewrites to a valid default), AND set
        # the nested updatedAt ahead of wall clock so the patched
        # getter MUST advance its write timestamp past the snapshot.
        future_time = datetime.now(timezone.utc) + timedelta(seconds=30)
        self.db.collection("organizations").document(self.slug).update({
            "billing.status": "invalid_status_xyz",  # normalize rewrites → writeback fires
            "billing.updatedAt": future_time,
        })
        pre_raw = (self.db.collection("organizations").document(self.slug)
                       .get().to_dict() or {}).get("billing") or {}
        self.assertEqual(pre_raw.get("updatedAt"), future_time)
        self.assertEqual(pre_raw.get("status"), "invalid_status_xyz")

        returned = self.store.get_org_billing_profile(org_id=self.slug)
        post = _snapshot(self.db, self.slug)

        # Writeback fired (dotted key added a value). The stored token
        # must have advanced past `future_time`, not moved backward.
        self.assertGreater(
            post.get("updatedAt"), future_time,
            f"Stored updatedAt rolled BACKWARD past the existing "
            f"future-ish token (post={post.get('updatedAt')!r} not > "
            f"future={future_time!r}). Pre-patch getter captures `now` "
            f"outside the transaction and uses raw `_utcnow()` for the "
            f"write, which produces a timestamp older than the already-"
            f"stored future token."
        )
        # Returned token equals stored token.
        self.assertEqual(
            returned.get("updatedAt"), post.get("updatedAt"),
            "returned token must equal stored token after writeback",
        )


    # ── Deep-copy regression — nested legacy alias actually migrates ──
    # Reviewer-reproduced (v2 defect): the frozen helper
    # `_billing_payload_from_org_row` shallow-copies the nested `billing`
    # dict and then overwrites a nested sub-dict in place (e.g. `limits`).
    # If the getter retains a reference to the snapshot's `billing` block
    # as the comparison baseline, the shared sub-dict mutation causes
    # `billing == raw_nested` to incorrectly return True → writeback
    # skipped → migration lost. The patch deep-copies the baseline.
    #
    # This test uses the versioned in-memory adapter so we can place a
    # LITERAL dotted top-level key (`"billing.limits.maxServiceKeys"`)
    # which the Firestore Python client's string-path parsing would
    # otherwise interpret as a nested path.
    def test_nested_legacy_alias_actually_migrates_and_persists(self) -> None:
        """Nested legacy alias `billing.limits.maxServiceKeys` must
        migrate into the stored nested billing block. The getter's
        returned value AND the stored value must both reflect the lift,
        proving the deep-copy fix.
        """
        import google.api_core.exceptions as gax_exc
        import google.cloud.firestore as gcf_firestore_mod
        import app.services.multichurch_store as mcs

        # Inline the same versioned adapter used in the overwrite test.
        class _VersionedDocRef:
            def __init__(self, adapter, path):
                self._adapter = adapter
                self.path = path
            def get(self, transaction=None, timeout=None):
                with self._adapter._lock:
                    data, version = self._adapter._docs.get(self.path, (None, 0))
                snap = type("Snap", (), {})()
                snap.exists = data is not None
                snap._data = copy.deepcopy(data) if data else {}
                snap.to_dict = lambda s=snap: dict(s._data)
                if transaction is not None:
                    transaction._observed[self.path] = version
                return snap

        class _VersionedTransaction:
            def __init__(self, adapter):
                self._adapter = adapter
                self._observed: Dict[str, int] = {}
                self._pending: list = []
            def get(self, doc_ref, timeout=None):
                return doc_ref.get(transaction=self)
            def set(self, doc_ref, data, merge=False):
                self._pending.append(("set", doc_ref, data, merge))
            def _commit(self):
                with self._adapter._lock:
                    for p, v_obs in self._observed.items():
                        _, cur_v = self._adapter._docs.get(p, (None, 0))
                        if cur_v > v_obs:
                            raise gax_exc.Aborted(
                                f"version mismatch at {p}: observed={v_obs}, current={cur_v}")
                    for op, ref, data, merge in self._pending:
                        cur_data, cur_v = self._adapter._docs.get(ref.path, ({}, 0))
                        if merge:
                            new_data = copy.deepcopy(cur_data or {})
                            for k, v in data.items():
                                new_data[k] = v
                        else:
                            new_data = dict(data)
                        self._adapter._docs[ref.path] = (new_data, cur_v + 1)

        class _VersionedAdapter:
            def __init__(self):
                self._docs: Dict[str, tuple] = {}
                self._lock = threading.Lock()
            def transaction(self):
                return _VersionedTransaction(self)

        def _shim_transactional(fn):
            def _call(tx, *args, **kwargs):
                last_exc = None
                for _ in range(5):
                    tx._observed = {}; tx._pending = []
                    try:
                        result = fn(tx, *args, **kwargs); tx._commit()
                        return result
                    except gax_exc.Aborted as exc:
                        last_exc = exc; continue
                raise ValueError(f"Failed to commit transaction: {last_exc!r}")
            return _call

        adapter = _VersionedAdapter()
        initial_time = datetime.now(timezone.utc)
        # Pre-stage: nested billing has limits.maxServiceKeys = None,
        # PLUS a literal top-level dotted alias with value 5.
        initial_billing = {
            "version": 1, "provider": "stripe", "planKey": "trial",
            "status": "trialing",
            "trialEndsAt": initial_time + timedelta(days=30),
            "currentPeriodStart": None, "currentPeriodEnd": None,
            "graceEndsAt": None, "cancelAtPeriodEnd": False,
            "stripeCustomerId": None, "stripeSubscriptionId": None,
            "priceId": None, "trialMinutesLimit": 20,
            "trialMinutesUsed": 0, "trialSecondsUsed": 0,
            "limits": {"maxServiceKeys": None},  # ← NULL in nested
            "entitlements": {"canStartService": True},
            "updatedAt": initial_time,
        }
        adapter._docs[f"organizations/{self.slug}"] = (
            {
                "billing": initial_billing,
                # LITERAL dotted top-level key — the helper's path-walk
                # lifts it into billing_payload['limits']['maxServiceKeys'].
                "billing.limits.maxServiceKeys": 5,
                "plan": "trial", "slug": self.slug,
            },
            1,
        )

        orig_db = self.store._db
        orig_org_ref = type(self.store)._org_ref
        orig_transactional = gcf_firestore_mod.transactional
        try:
            gcf_firestore_mod.transactional = _shim_transactional
            self.store._db = adapter
            def _adapter_org_ref(self_store, slug_arg):
                return _VersionedDocRef(adapter, f"organizations/{slug_arg}")
            type(self.store)._org_ref = _adapter_org_ref

            returned = self.store.get_org_billing_profile(org_id=self.slug)
            final_data, _ = adapter._docs[f"organizations/{self.slug}"]
            final_billing = final_data.get("billing") or {}

            # Getter must RETURN 5 (lifted from the alias).
            self.assertEqual(
                returned.get("limits", {}).get("maxServiceKeys"), 5,
                "returned limits.maxServiceKeys must reflect the alias lift",
            )
            # Stored nested MUST ALSO be 5. Pre-fix: shared-reference
            # mutation caused the comparison to miss the lift → stored
            # stayed None.
            self.assertEqual(
                final_billing.get("limits", {}).get("maxServiceKeys"), 5,
                f"nested limits.maxServiceKeys must be migrated to 5 on disk "
                f"(got {final_billing.get('limits', {}).get('maxServiceKeys')!r}; "
                f"reviewer-reproduced v2 defect: shared reference between "
                f"raw_nested and billing_payload caused the comparison to "
                f"miss the lift)",
            )

            # Second read: idempotent — writeback must not fire because
            # the migration has already landed.
            T1 = final_billing.get("updatedAt")
            second = self.store.get_org_billing_profile(org_id=self.slug)
            final2_data, _ = adapter._docs[f"organizations/{self.slug}"]
            final2_billing = final2_data.get("billing") or {}
            self.assertEqual(
                final2_billing.get("updatedAt"), T1,
                "Second read of a migrated doc must preserve the token "
                "(migration is idempotent)",
            )
            self.assertEqual(second.get("updatedAt"), T1,
                              "Second read's returned token must equal stored")
        finally:
            gcf_firestore_mod.transactional = orig_transactional
            type(self.store)._org_ref = orig_org_ref
            self.store._db = orig_db

    # ── Deterministic overwrite coverage — distinguisher ──────────────────
    # Reviewer-corrected (v3): the sequential test exercises idempotence,
    # not the pre-patch overwrite bug. This test establishes A's stale
    # read, B's intervening commit, and A's attempted writeback via a
    # versioned transaction adapter (in-memory simulation), proving the
    # patched getter's transactional writeback aborts and retries while
    # the original getter's non-transactional writeback does not.
    #
    # The adapter is required because the real Firestore emulator's
    # pessimistic doc-locking would deadlock a controlled "A reads, B
    # writes, A commits" sequence on the same document.
    def test_patched_getter_retries_on_stale_read(self) -> None:
        """Deterministic overwrite coverage via in-memory versioned
        transaction adapter. The real Firestore emulator's pessimistic
        doc-locking would deadlock a controlled "A reads, B writes,
        A commits" sequence (reviewer-acknowledged); an adapter is
        appropriate.

        Scenario (reviewer-specified v4 corrections):
          1. Pre-stage doc at V1 with an invalid status so A's
             normalization MUST change the state → A's `_tx` queues
             a write (not a no-op observation).
          2. A opens tx, reads V1 (observed=V1).
          3. B commits a raw write bumping to V2 during A's first
             `_normalize_billing_state` call.
          4. A's `_tx` queues `tx.set(...)` with normalized state.
          5. A's `_commit` attempts to apply the queued write but
             detects V_observed(V1) < V_current(V2) → raises `Aborted`.
          6. Shim retries. Second attempt reads V2, normalization is
             a no-op (B's state is valid), returns B's state unchanged.

        Distinguishers:
          - A QUEUED a pending write on first attempt (not just a
            version-only observation).
          - The ADAPTER rejected A's commit on first attempt.
          - On retry, B's identity fields survived.
          - EMBEDDED ORIGINAL GETTER against the same scenario LOSES
            B's identity fields via its non-transactional merge-set.
        """
        import google.api_core.exceptions as gax_exc
        import google.cloud.firestore as gcf_firestore_mod
        import app.services.multichurch_store as mcs

        # ─── Minimal versioned in-memory adapter ──────────────────────
        class _VersionedDocRef:
            def __init__(self, adapter, path):
                self._adapter = adapter
                self.path = path

            def get(self, transaction=None, timeout=None):
                with self._adapter._lock:
                    data, version = self._adapter._docs.get(self.path, (None, 0))
                snap = type("Snap", (), {})()
                snap.exists = data is not None
                snap._data = copy.deepcopy(data) if data else {}
                snap.to_dict = lambda s=snap: dict(s._data)
                if transaction is not None:
                    transaction._observed[self.path] = version
                return snap

            # Non-transactional .set() — supports the ORIGINAL getter
            # which uses `org_ref.set({..., SERVER_TIMESTAMP}, merge=True,
            # timeout=...)` directly, not inside a tx.
            def set(self, data, merge=False, timeout=None):
                with self._adapter._lock:
                    cur_data, cur_v = self._adapter._docs.get(self.path, ({}, 0))
                    if merge:
                        new_data = copy.deepcopy(cur_data or {})
                        for k, v in data.items():
                            new_data[k] = v
                    else:
                        new_data = dict(data)
                    self._adapter._docs[self.path] = (new_data, cur_v + 1)

        class _VersionedTransaction:
            def __init__(self, adapter):
                self._adapter = adapter
                self._observed: Dict[str, int] = {}
                self._pending: list = []

            def get(self, doc_ref, timeout=None):
                return doc_ref.get(transaction=self)

            def set(self, doc_ref, data, merge=False):
                self._pending.append(("set", doc_ref, data, merge))

            def _commit(self):
                # Reviewer-corrected (v4): verify observed versions ONLY
                # when there are pending writes. A read-only transaction
                # has no commit step; retrying such a tx would reward
                # a stale observation without exercising writeback.
                if not self._pending:
                    return
                with self._adapter._lock:
                    for p, v_obs in self._observed.items():
                        _, cur_v = self._adapter._docs.get(p, (None, 0))
                        if cur_v > v_obs:
                            raise gax_exc.Aborted(
                                f"version mismatch at {p}: "
                                f"observed={v_obs}, current={cur_v}"
                            )
                    for op, ref, data, merge in self._pending:
                        cur_data, cur_v = self._adapter._docs.get(
                            ref.path, ({}, 0),
                        )
                        if merge:
                            new_data = copy.deepcopy(cur_data or {})
                            for k, v in data.items():
                                new_data[k] = v
                        else:
                            new_data = dict(data)
                        self._adapter._docs[ref.path] = (new_data, cur_v + 1)

        class _VersionedAdapter:
            def __init__(self):
                self._docs: Dict[str, tuple] = {}
                self._lock = threading.Lock()

            def transaction(self):
                return _VersionedTransaction(self)

        # Shim: counts aborted-commit events for assertion visibility.
        aborts_recorded: list = []
        def _shim_transactional(fn):
            def _call(tx, *args, **kwargs):
                last_exc = None
                for _ in range(5):
                    tx._observed = {}
                    tx._pending = []
                    try:
                        result = fn(tx, *args, **kwargs)
                        # Capture whether A queued pending writes on
                        # this attempt — for the pending-write assertion.
                        pending_count = len(tx._pending)
                        tx._commit()
                        return result, pending_count
                    except gax_exc.Aborted as exc:
                        aborts_recorded.append(repr(exc))
                        last_exc = exc
                        continue
                raise ValueError(
                    f"Failed to commit transaction in 5 attempts: {last_exc!r}"
                )
            return _call

        # Pre-stage: nested billing has INVALID status so normalize MUST
        # change it → tx.set fires on first attempt (not a no-op).
        adapter = _VersionedAdapter()
        initial_time = datetime.now(timezone.utc)
        initial_billing = {
            "version": 1, "provider": "stripe", "planKey": "trial",
            "status": "invalid_status_force_writeback",  # ← forces normalize
            "trialEndsAt": initial_time + timedelta(days=30),
            "currentPeriodStart": None, "currentPeriodEnd": None,
            "graceEndsAt": None, "cancelAtPeriodEnd": False,
            "stripeCustomerId": None, "stripeSubscriptionId": None,
            "priceId": None, "trialMinutesLimit": 20,
            "trialMinutesUsed": 0, "trialSecondsUsed": 0,
            "limits": {"maxServiceKeys": 2},
            "entitlements": {"canStartService": True},
            "updatedAt": initial_time,
        }
        adapter._docs[f"organizations/{self.slug}"] = (
            {"billing": initial_billing, "plan": "trial", "slug": self.slug},
            1,
        )

        # Injection: during A's FIRST normalize invocation, B commits
        # new state via raw adapter mutation.
        orig_normalize = mcs._normalize_billing_state
        A_count = {"n": 0}
        pending_writes_per_attempt: list = []
        def _norm_with_B_injection(payload, *, plan_key, now):
            A_count["n"] += 1
            if A_count["n"] == 1:
                B_time = initial_time + timedelta(seconds=1)
                B_billing = dict(initial_billing)
                B_billing.update({
                    "status": "active",  # VALID — normalize won't change
                    "priceId": "price_B_won_race",
                    "stripeCustomerId": "cus_B_won",
                    "stripeSubscriptionId": "sub_B_won",
                    "planKey": "starter",
                    "limits": {"maxServiceKeys": 5},
                    "updatedAt": B_time,
                })
                with adapter._lock:
                    cur_data, cur_v = adapter._docs[
                        f"organizations/{self.slug}"
                    ]
                    new_data = dict(cur_data)
                    new_data["billing"] = B_billing
                    new_data["updatedAt"] = B_time
                    adapter._docs[f"organizations/{self.slug}"] = (
                        new_data, cur_v + 1,
                    )
            return orig_normalize(payload, plan_key=plan_key, now=now)

        # ─── Phase 1: run the PATCHED getter against the adapter ──────
        orig_db = self.store._db
        orig_org_ref = type(self.store)._org_ref
        orig_transactional = gcf_firestore_mod.transactional
        patched_result = None
        try:
            mcs._normalize_billing_state = _norm_with_B_injection
            # Shim: wrap transactional to also unwrap the (result, pending_count) tuple.
            def _wrapping_transactional(fn):
                inner = _shim_transactional(fn)
                def _unwrap(tx, *args, **kwargs):
                    result, pending_count = inner(tx, *args, **kwargs)
                    pending_writes_per_attempt.append(pending_count)
                    return result
                return _unwrap
            gcf_firestore_mod.transactional = _wrapping_transactional
            self.store._db = adapter
            def _adapter_org_ref(self_store, slug_arg):
                return _VersionedDocRef(adapter, f"organizations/{slug_arg}")
            type(self.store)._org_ref = _adapter_org_ref

            patched_result = self.store.get_org_billing_profile(org_id=self.slug)
        finally:
            mcs._normalize_billing_state = orig_normalize
            gcf_firestore_mod.transactional = orig_transactional
            type(self.store)._org_ref = orig_org_ref
            self.store._db = orig_db

        final_data, _ = adapter._docs[f"organizations/{self.slug}"]
        final_billing = final_data.get("billing") or {}

        # (a) A queued a pending write on the FIRST attempt — we did
        # NOT just observe a version. Prevents the "retry fires on
        # version-only observation" false positive.
        # The SUCCESSFUL attempt had 0 pending writes (B's state is
        # already normalized). But the FIRST attempt (before abort)
        # queued the write. Our `aborts_recorded` carries that.
        self.assertGreaterEqual(
            len(aborts_recorded), 1,
            f"A's first attempt must have been ABORTED by the adapter "
            f"(got {len(aborts_recorded)} aborts; adapter should have "
            f"detected version mismatch when A tried to commit its "
            f"pending normalization write)",
        )

        # (b) A retried: >=2 normalize invocations.
        self.assertGreaterEqual(
            A_count["n"], 2,
            f"A's transaction body must have retried (observed "
            f"{A_count['n']} normalize invocations)",
        )

        # (c) B's identity fields survived on disk.
        self.assertEqual(final_billing.get("stripeCustomerId"), "cus_B_won",
                         "B's stripeCustomerId must survive")
        self.assertEqual(final_billing.get("stripeSubscriptionId"), "sub_B_won")
        self.assertEqual(final_billing.get("status"), "active")
        self.assertEqual(final_billing.get("priceId"), "price_B_won_race")

        # (d) Returned reflects B's state.
        self.assertEqual(patched_result.get("stripeCustomerId"), "cus_B_won")

        # ─── Phase 2: EMBEDDED ORIGINAL getter against same scenario ──
        # Reset the adapter to the pre-A state.
        aborts_recorded.clear()
        adapter._docs[f"organizations/{self.slug}"] = (
            {"billing": initial_billing, "plan": "trial", "slug": self.slug},
            1,
        )
        A_count["n"] = 0

        def _original_pre_patch_get_org_billing_profile(store, *, org_id):
            """Faithful copy of the frozen (bb3ebd8b) getter's logic.
            Non-transactional read + optional non-transactional writeback
            via `org_ref.set({..., SERVER_TIMESTAMP}, merge=True)`. This
            is what the patch replaced; running it against the same
            scenario must demonstrate B's identity fields being lost."""
            clean_org_id = mcs._clean_token(org_id)
            if not clean_org_id:
                raise ValueError("org_not_found")
            now = mcs._utcnow()
            org_ref = store._org_ref(clean_org_id)
            org_snap = org_ref.get(timeout=60)
            if not org_snap.exists:
                raise ValueError("org_not_found")
            org = org_snap.to_dict() or {}
            billing_payload, lifted_legacy_fields = mcs._billing_payload_from_org_row(org)
            billing = mcs._normalize_billing_state(
                billing_payload,
                plan_key=str(org.get("plan") or "trial"),
                now=now,
            )
            if lifted_legacy_fields or billing != billing_payload:
                # SERVER_TIMESTAMP is Firestore-specific; use `now` for
                # the adapter path. The clobber mechanism is wholesale
                # replacement of the nested `billing` block via
                # merge=True set.
                org_ref.set(
                    {"billing": billing, "updatedAt": now},
                    merge=True,
                    timeout=60,
                )
            return dict(billing)

        try:
            mcs._normalize_billing_state = _norm_with_B_injection
            self.store._db = adapter
            type(self.store)._org_ref = _adapter_org_ref

            original_result = _original_pre_patch_get_org_billing_profile(
                self.store, org_id=self.slug,
            )
        finally:
            mcs._normalize_billing_state = orig_normalize
            type(self.store)._org_ref = orig_org_ref
            self.store._db = orig_db

        orig_final_data, _ = adapter._docs[f"organizations/{self.slug}"]
        orig_final_billing = orig_final_data.get("billing") or {}

        # The pre-patch getter MUST have lost B's identity fields.
        # (Normalization set status→trialing from the invalid value,
        # so the writeback dict's billing block LACKS B's identity
        # fields and merge=True REPLACES the entire billing block.)
        self.assertNotEqual(
            orig_final_billing.get("stripeCustomerId"), "cus_B_won",
            f"EMBEDDED ORIGINAL getter must have CLOBBERED B's "
            f"stripeCustomerId (demonstrates the stale-read overwrite "
            f"bug the patch fixes). Observed: "
            f"stripeCustomerId={orig_final_billing.get('stripeCustomerId')!r}; "
            f"if this field survived, the test does not actually "
            f"distinguish patched vs original behavior.",
        )
        # Confirm the pre-patch getter's writeback replaced the whole
        # billing block with its stale normalized version (status=trialing).
        self.assertEqual(
            orig_final_billing.get("status"), "trialing",
            f"Pre-patch getter should have written back normalized "
            f"stale state (status reset from invalid to default "
            f"trialing); observed {orig_final_billing.get('status')!r}",
        )


if __name__ == "__main__":
    unittest.main()
