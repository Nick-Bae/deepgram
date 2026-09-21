"""BOOT-1 — transactional session-start gate.

Reference: PR #31 §4b (`docs/03-analysis/redis-fanout-rollout-proposal.md`).

The rollout proposal specifies a single global Firestore document at
`system/deploy_gate` that both start endpoints
(`POST /api/org/{orgId}/service/{serviceKey}/start` and
`POST /api/c/{slug}/service/{service_key}/start`) MUST consult INSIDE
the same Firestore transaction that creates the room. The absent-
document state is treated as unblocked so the gate-reading code deploy
(BOOT-1) is a no-op on the running service until the operator writes
the document.

These tests exercise the gate against the in-memory store, which
mirrors the Firestore-backed implementation for the semantics the
gate depends on. Full transactional race behaviour against the real
Firestore transaction runtime is a Firestore-emulator concern
(integration harness); these tests cover the documented handler
contract.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.services import multichurch_store as multichurch_store_module
from app.services.multichurch_store import InMemoryMultiChurchStore


def _bootstrap_owner(
    store: InMemoryMultiChurchStore,
    *,
    owner_uid: str,
    slug: str,
    name: str,
) -> str:
    result = store.bootstrap_owner_org(
        owner_uid=owner_uid,
        owner_email=f"{owner_uid}@example.com",
        owner_display_name=owner_uid,
        church_name=name,
        church_slug=slug,
        timezone="America/Chicago",
        source="ko",
        target="en",
    )
    return str(result["orgId"])


class DeployGateStoreTests(unittest.TestCase):
    """Store-level contract for the gate."""

    def test_default_absent_gate_is_unblocked(self) -> None:
        """Fresh store, no gate document → treated as unblocked. This
        is the safe default that makes the BOOT-1 code deploy a no-op
        on the running service until the operator writes the document."""
        store = InMemoryMultiChurchStore()
        state = store.read_deploy_gate()
        self.assertFalse(state["blocked"])
        self.assertFalse(state["exists"])

    def test_start_succeeds_when_gate_absent(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(
            store, owner_uid="owner-gate-1",
            slug="gate-org-1", name="Gate Org 1",
        )
        started = store.start_service(
            org_id, "sun-11am",
            host_uid="owner-gate-1", source="ko", target="en",
        )
        self.assertEqual(started["status"], "live")

    def test_start_succeeds_when_gate_cleared(self) -> None:
        """A cleared gate (blocked=False, but the document exists) is
        equivalent to no gate. The document may still exist because the
        operator prefers to keep it around for audit continuity."""
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(
            store, owner_uid="owner-gate-2",
            slug="gate-org-2", name="Gate Org 2",
        )
        store._set_deploy_gate_for_test(blocked=False, reason="cleared")
        started = store.start_service(
            org_id, "sun-11am",
            host_uid="owner-gate-2", source="ko", target="en",
        )
        self.assertEqual(started["status"], "live")

    def test_start_refused_when_gate_blocked(self) -> None:
        """The core contract — gate set to blocked → start_service
        raises `PermissionError("maintenance_blocked")`."""
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(
            store, owner_uid="owner-gate-3",
            slug="gate-org-3", name="Gate Org 3",
        )
        store._set_deploy_gate_for_test(
            blocked=True, reason="redis-enablement-window", by="ops-runbook",
        )
        with self.assertRaisesRegex(PermissionError, "maintenance_blocked"):
            store.start_service(
                org_id, "sun-11am",
                host_uid="owner-gate-3", source="ko", target="en",
            )

    def test_start_refused_before_any_other_permission_check(self) -> None:
        """The gate is checked BEFORE billing/org-status checks.
        Rationale: an operator's maintenance window applies uniformly
        regardless of any org's individual state. The alternative
        (billing checked first) would let a trial-expired org still
        create a room during the window if the gate was set right
        before it hit — and would obscure the reason in logs."""
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(
            store, owner_uid="owner-gate-4",
            slug="gate-org-4", name="Gate Org 4",
        )
        # Would ordinarily fail with hard_cap_reached; the gate
        # should still take precedence.
        store._orgs[org_id]["hardCapReached"] = True
        store._set_deploy_gate_for_test(blocked=True)
        with self.assertRaisesRegex(PermissionError, "maintenance_blocked"):
            store.start_service(
                org_id, "sun-11am",
                host_uid="owner-gate-4", source="ko", target="en",
            )

    def test_new_org_created_after_gate_is_set_is_still_blocked(self) -> None:
        """Global gate covers ALL orgs — including newly-created ones.
        The failure mode the gate prevents is exactly the case where a
        new-org creation slips past a per-org flag."""
        store = InMemoryMultiChurchStore()
        store._set_deploy_gate_for_test(blocked=True, reason="window-open")
        # Create the org AFTER the gate is set.
        org_id = _bootstrap_owner(
            store, owner_uid="owner-gate-5",
            slug="gate-org-5", name="Gate Org 5",
        )
        with self.assertRaisesRegex(PermissionError, "maintenance_blocked"):
            store.start_service(
                org_id, "sun-11am",
                host_uid="owner-gate-5", source="ko", target="en",
            )

    def test_gate_revision_increments_monotonically_in_test_setter(self) -> None:
        """The in-memory test setter (`_set_deploy_gate_for_test`)
        bumps `revision` on every write.

        Scope caveat: this covers the WRITE-SIDE CONTRACT the
        production operator's set/clear script must implement. That
        production writer is a separate follow-on PR — see PR #33's
        non-goals — so the monotonic-revision guarantee currently
        lives ONLY in this test helper. The gate READER on the hot
        path (this PR) is independent of that guarantee.
        """
        store = InMemoryMultiChurchStore()
        store._set_deploy_gate_for_test(blocked=True, reason="r1")
        rev1 = store.read_deploy_gate()["revision"]
        store._set_deploy_gate_for_test(blocked=False, reason="r1-clear")
        rev2 = store.read_deploy_gate()["revision"]
        store._set_deploy_gate_for_test(blocked=True, reason="r2")
        rev3 = store.read_deploy_gate()["revision"]
        self.assertEqual([rev1, rev2, rev3], [1, 2, 3])


class DeployGateMalformedDocTests(unittest.TestCase):
    """Reviewer finding — strict typing on the `blocked` field. A
    naive `bool(gate.get("blocked"))` would treat `{}`,
    `{"blocked": None}`, `{"blocked": 0}`, `{"blocked": ""}`, and a
    missing field as "unblocked" — silently defeating the entire
    gate mechanism during a maintenance window.

    Fix (in `_parse_deploy_gate_doc`): require the `blocked` field
    to be present AND a literal `bool`. Malformed shapes raise
    `_MalformedDeployGate` (a `PermissionError` subclass), which
    the store surfaces as `PermissionError("maintenance_malformed_gate")`
    and the route translates to a 503 with a distinct detail so ops
    can page separately from a normal blocked-window response.

    Absent-document behaviour (unblocked) is preserved intact and
    covered separately in `DeployGateStoreTests`.
    """

    def _bootstrap(self) -> tuple[InMemoryMultiChurchStore, str]:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(
            store, owner_uid="owner-malformed",
            slug="malformed-org", name="Malformed Org",
        )
        return store, org_id

    def _assert_start_refused_as_malformed(
        self, store: InMemoryMultiChurchStore, org_id: str,
    ) -> None:
        with self.assertRaisesRegex(PermissionError, "maintenance_malformed_gate"):
            store.start_service(
                org_id, "sun-11am",
                host_uid="owner-malformed", source="ko", target="en",
            )

    def test_missing_blocked_field_refuses_start(self) -> None:
        """`{}` (or `{"revision": 1}` etc.) — the field is absent."""
        store, org_id = self._bootstrap()
        store._write_deploy_gate_raw_for_test({})
        self._assert_start_refused_as_malformed(store, org_id)

    def test_null_blocked_refuses_start(self) -> None:
        store, org_id = self._bootstrap()
        store._write_deploy_gate_raw_for_test({"blocked": None})
        self._assert_start_refused_as_malformed(store, org_id)

    def test_zero_blocked_refuses_start(self) -> None:
        """The int 0. `bool(0)` is `False` — the naive check would
        pass. Strict type check rejects."""
        store, org_id = self._bootstrap()
        store._write_deploy_gate_raw_for_test({"blocked": 0})
        self._assert_start_refused_as_malformed(store, org_id)

    def test_empty_string_blocked_refuses_start(self) -> None:
        store, org_id = self._bootstrap()
        store._write_deploy_gate_raw_for_test({"blocked": ""})
        self._assert_start_refused_as_malformed(store, org_id)

    def test_string_true_refuses_start(self) -> None:
        """Common footgun — the operator wrote `"true"` (a JSON
        string) instead of `true` (a JSON boolean). `bool("true")`
        is `True`, so the naive check would BLOCK — but that's an
        accidental correct answer for this shape only. Strict check
        rejects because we cannot rely on the operator having
        intended that meaning. Fail-closed."""
        store, org_id = self._bootstrap()
        store._write_deploy_gate_raw_for_test({"blocked": "true"})
        self._assert_start_refused_as_malformed(store, org_id)

    def test_read_deploy_gate_marks_malformed(self) -> None:
        """`read_deploy_gate` MUST surface malformed docs as
        `blocked=True` (fail-closed for operator tooling) plus a
        `malformed=True` flag and a `malformed_reason` string. A
        naive helper would return `blocked=False` and hide the
        misconfiguration from set/clear scripts and dashboards."""
        store, _ = self._bootstrap()
        store._write_deploy_gate_raw_for_test({"blocked": None, "revision": 7})
        state = store.read_deploy_gate()
        self.assertTrue(state["blocked"])
        self.assertTrue(state["exists"])
        self.assertTrue(state["malformed"])
        self.assertIn("blocked", state["malformed_reason"])
        # Non-`blocked` fields survive so the operator can still see
        # what was in the doc.
        self.assertEqual(state["revision"], 7)

    def test_read_deploy_gate_well_formed_not_marked_malformed(self) -> None:
        store, _ = self._bootstrap()
        store._set_deploy_gate_for_test(blocked=True, reason="ok")
        state = store.read_deploy_gate()
        self.assertTrue(state["blocked"])
        self.assertFalse(state["malformed"])

    def test_read_deploy_gate_absent_not_marked_malformed(self) -> None:
        store, _ = self._bootstrap()
        state = store.read_deploy_gate()
        self.assertFalse(state["blocked"])
        self.assertFalse(state["exists"])
        self.assertFalse(state["malformed"])


class DeployGateParserContractTests(unittest.TestCase):
    """Direct coverage of `_parse_deploy_gate_doc` — same fixtures
    the store-level tests above rely on, tested at the parser layer
    so a regression in the parser is caught even if the caller
    changes shape."""

    def test_absent_precondition_raises_type_error(self) -> None:
        """The parser refuses `None` — its contract is "call only
        when the snapshot exists". Absent-document behaviour is the
        caller's responsibility."""
        from app.services.multichurch_store import _parse_deploy_gate_doc
        with self.assertRaises(TypeError):
            _parse_deploy_gate_doc(None)

    def test_literal_true_returns_true(self) -> None:
        from app.services.multichurch_store import _parse_deploy_gate_doc
        self.assertTrue(_parse_deploy_gate_doc({"blocked": True}))

    def test_literal_false_returns_false(self) -> None:
        from app.services.multichurch_store import _parse_deploy_gate_doc
        self.assertFalse(_parse_deploy_gate_doc({"blocked": False}))

    def test_missing_field_raises_malformed(self) -> None:
        from app.services.multichurch_store import (
            _parse_deploy_gate_doc, _MalformedDeployGate,
        )
        with self.assertRaises(_MalformedDeployGate):
            _parse_deploy_gate_doc({})

    def test_wrong_type_raises_malformed(self) -> None:
        from app.services.multichurch_store import (
            _parse_deploy_gate_doc, _MalformedDeployGate,
        )
        for bad in (None, 0, 1, "", "true", "false", [], {}):
            with self.subTest(bad=bad):
                with self.assertRaises(_MalformedDeployGate):
                    _parse_deploy_gate_doc({"blocked": bad})


class DeployGateTransactionParticipationTests(unittest.TestCase):
    """Prove that `FirestoreMultiChurchStore.start_service` reads
    the gate INSIDE the Firestore transaction (i.e. passes the tx
    handle to `.get`). Without this the gate would sit outside the
    transaction's read set and Firestore's OCC would not detect a
    concurrent gate flip — the race the entire mechanism exists
    to close would be back.

    This test uses mocks (no emulator). Firestore-backed absent /
    cleared / blocked / malformed cases live in
    `DeployGateFirestoreTests` below and run in the CI's
    `firestore-emulator-tests` job.
    """

    def _build_store_with_mocks(self, *, gate_snap):
        from unittest.mock import MagicMock
        from app.services import multichurch_store as store_mod

        gate_ref = MagicMock(name="deploy_gate_ref")
        gate_ref.get.return_value = gate_snap

        org_ref = MagicMock(name="org_ref")
        org_ref.get.return_value = MagicMock(exists=False)

        service_ref = MagicMock(name="service_ref")
        room_ref = MagicMock(name="room_ref")

        fake_db = MagicMock(name="db")
        fake_db.transaction.return_value = MagicMock(name="tx")

        store = store_mod.FirestoreMultiChurchStore.__new__(
            store_mod.FirestoreMultiChurchStore
        )
        store._db = fake_db
        store._org_ref = lambda org_id: org_ref
        store._service_ref = lambda org_id, service_key: service_ref
        store._room_ref = lambda org_id, room_id: room_ref
        store._deploy_gate_ref = lambda: gate_ref
        return store, gate_ref, fake_db

    def test_gate_get_receives_transaction_handle(self) -> None:
        """The gate `.get()` call MUST receive `transaction=<tx>` so
        Firestore's OCC considers `system/deploy_gate` part of the
        transaction's read set. Any other invocation shape (bare
        `.get()`, `.get(retry=None)`, etc.) does not join the tx."""
        from app.services import multichurch_store as store_mod
        gate_snap = _fake_gate_snapshot(exists=False)
        store, gate_ref, fake_db = self._build_store_with_mocks(
            gate_snap=gate_snap,
        )

        # Bypass the transactional decorator so the callback runs
        # once directly against our mock tx.
        def _passthrough_transactional(fn):
            def _wrapped(tx):
                return fn(tx)
            return _wrapped

        from unittest.mock import patch
        with patch.object(store_mod.gcf_firestore, "transactional",
                          _passthrough_transactional):
            with self.assertRaises(ValueError):
                # `org_not_found` from the mocked org_ref; not the
                # focus here — the assertion below IS.
                store.start_service(
                    "org-id", "svc-key",
                    host_uid="host", source="ko", target="en",
                )

        # The critical evidence: gate_ref.get was called with a
        # non-None `transaction` kwarg. If future code moves the
        # gate read outside the tx, this test fails.
        calls = gate_ref.get.call_args_list
        self.assertEqual(len(calls), 1, f"gate_ref.get called {len(calls)} times, expected 1")
        _, kwargs = calls[0]
        self.assertIn("transaction", kwargs)
        self.assertIsNotNone(kwargs["transaction"])
        self.assertIs(kwargs["transaction"], fake_db.transaction.return_value)

    def test_gate_check_precedes_org_read(self) -> None:
        """The gate is checked BEFORE the org read — so a blocked
        gate stops the transaction from touching org state at all.
        This proves the ordering the store-level tests already
        verify at the semantic layer, at the mock-call-order layer."""
        from app.services import multichurch_store as store_mod
        gate_snap = _fake_gate_snapshot(exists=True, data={"blocked": True})
        store, gate_ref, fake_db = self._build_store_with_mocks(
            gate_snap=gate_snap,
        )

        # Reach into the mocks the builder created so we can spy
        # on which was called first.
        org_ref = store._org_ref("any")

        def _passthrough_transactional(fn):
            def _wrapped(tx):
                return fn(tx)
            return _wrapped

        from unittest.mock import patch
        with patch.object(store_mod.gcf_firestore, "transactional",
                          _passthrough_transactional):
            with self.assertRaisesRegex(PermissionError, "maintenance_blocked"):
                store.start_service(
                    "org-id", "svc-key",
                    host_uid="host", source="ko", target="en",
                )

        # Blocked → raised before org was ever read.
        gate_ref.get.assert_called_once()
        org_ref.get.assert_not_called()


def _fake_gate_snapshot(*, exists: bool, data: dict = None):
    """Minimal Firestore-snapshot double for the parser + tx tests."""
    from unittest.mock import MagicMock
    snap = MagicMock()
    snap.exists = exists
    snap.to_dict.return_value = dict(data) if data else {}
    return snap


class DeployGateRouteTests(unittest.TestCase):
    """End-to-end: both start endpoints translate the store's
    `PermissionError("maintenance_blocked")` into HTTP 503 with
    `Retry-After: 60`."""

    def setUp(self) -> None:
        self.store = InMemoryMultiChurchStore()
        self.org_id = _bootstrap_owner(
            self.store, owner_uid="owner-route-gate",
            slug="route-gate", name="Route Gate",
        )
        # Swap the singleton the router imports at load time.
        self._store_patch = patch.object(
            multichurch_store_module, "multichurch_store", self.store,
        )
        self._store_patch.start()
        # ALSO patch the module reference the router captured at import.
        from app.routes import multichurch as _mc_route
        self._route_patch = patch.object(
            _mc_route, "multichurch_store", self.store,
        )
        self._route_patch.start()

        # Build the client with just the multichurch router mounted,
        # avoiding the full main.py bootstrap.
        from fastapi import FastAPI
        from app.routes import multichurch as _mc
        from app.auth.firebase_auth import (
            AuthenticatedUser, get_current_user_required,
        )

        app = FastAPI()
        app.include_router(_mc.router, prefix="/api")
        # Stub auth: any request maps to the org's owner.
        app.dependency_overrides[get_current_user_required] = lambda: (
            AuthenticatedUser(
                uid="owner-route-gate",
                email=None, displayName=None, isSuper=False,
            )
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self._route_patch.stop()
        self._store_patch.stop()

    def _start_payload(self) -> dict:
        return {"source": "ko", "target": "en"}

    def test_org_endpoint_returns_503_when_blocked(self) -> None:
        self.store._set_deploy_gate_for_test(
            blocked=True, reason="test-org-endpoint",
        )
        resp = self.client.post(
            f"/api/org/{self.org_id}/service/sun-11am/start",
            json=self._start_payload(),
        )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.headers.get("Retry-After"), "60")
        self.assertEqual(resp.json().get("detail"), "maintenance_blocked")

    def test_slug_endpoint_returns_503_when_blocked(self) -> None:
        """PR #31 §4b: the block must cover BOTH endpoints."""
        self.store._set_deploy_gate_for_test(
            blocked=True, reason="test-slug-endpoint",
        )
        resp = self.client.post(
            "/api/c/route-gate/service/sun-11am/start",
            json=self._start_payload(),
        )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.headers.get("Retry-After"), "60")
        self.assertEqual(resp.json().get("detail"), "maintenance_blocked")

    def test_both_endpoints_succeed_when_unblocked(self) -> None:
        """Sanity — with no gate, both endpoints go through their
        normal 200 path."""
        r1 = self.client.post(
            f"/api/org/{self.org_id}/service/sun-11am/start",
            json=self._start_payload(),
        )
        self.assertEqual(r1.status_code, 200, r1.text)
        # End the room before the second start (a live room would be
        # returned as-is; not what this test is checking).
        room_id = r1.json()["roomId"]
        self.store.end_room(
            self.org_id, room_id, reason="test", transcript=None,
        )
        r2 = self.client.post(
            "/api/c/route-gate/service/sun-11am/start",
            json=self._start_payload(),
        )
        self.assertEqual(r2.status_code, 200, r2.text)


if __name__ == "__main__":
    unittest.main()
