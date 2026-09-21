"""BOOT-1 deploy-gate — Firestore-emulator-backed coverage.

Reference: PR #31 §4b, PR #33 review round 2.

Scope: prove the gate check runs against the real Firestore admin
SDK (not just an in-memory shortcut) for absent / cleared / blocked
/ malformed cases, and that a refused start leaves BOTH the room
collection AND the service's `activeRoomId` unchanged.

The transaction-participation guarantee (that the gate is IN the
`start_service` transaction's read set, so Firestore's OCC detects
a concurrent flip) has its own mock-based test in
`test_deploy_gate.py::DeployGateTransactionParticipationTests`;
the reviewer's caveat that the emulator uses simplified locking and
does not reproduce every production concurrency semantics
(https://cloud.google.com/firestore/docs/emulator) is why the strict
"call receives `transaction=` kwarg" evidence lives in that mock
test rather than being asserted through the emulator here.

Runs in CI under `firestore-emulator-tests` — the workflow appends
this file's target to the `pytest` invocation.
"""
from __future__ import annotations

import os
import unittest


from app.services import multichurch_store as store_mod


@unittest.skipUnless(
    os.getenv("FIRESTORE_EMULATOR_HOST"),
    "Firestore emulator required — set FIRESTORE_EMULATOR_HOST and "
    "MULTICHURCH_STORE_MODE=firestore.",
)
class DeployGateFirestoreTests(unittest.TestCase):
    """Absent / cleared / blocked / malformed cases against the
    real Firestore admin SDK (emulator).

    Each test constructs its own isolated org + service so the
    tests can run in parallel without cross-contamination."""

    def setUp(self):
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "cleanup-track1-emulator")
        os.environ["MULTICHURCH_STORE_MODE"] = "firestore"
        import importlib
        importlib.reload(store_mod)
        self.store = store_mod.FirestoreMultiChurchStore()
        suffix = os.urandom(4).hex()
        self.org_id = f"org-gate-{suffix}"
        self.service_key = "sunday"
        # Bootstrap: create an org + service the way the API would.
        # bootstrap_owner_org sets up the org + one default service;
        # add our own service_key on top.
        self.store.bootstrap_owner_org(
            owner_uid=f"host-{suffix}",
            owner_email=f"host-{suffix}@example.com",
            owner_display_name="Owner",
            church_name=f"Gate Church {suffix}",
            church_slug=f"gate-church-{suffix}",
            timezone="America/Chicago",
            source="ko",
            target="en",
        )
        # Force the org_id to the deterministic value we chose above
        # so subsequent asserts can locate it.
        self.org_id_actual = self._resolve_bootstrapped_org_id(
            slug=f"gate-church-{suffix}",
        )
        self.store.create_service(
            org_id=self.org_id_actual,
            service_key=self.service_key,
            requested_by_uid=f"host-{suffix}",
            title="Sunday",
            timezone="America/Chicago",
            source="ko", target="en",
        )
        self.host_uid = f"host-{suffix}"

    def tearDown(self):
        # Clean up the gate doc so the next test starts absent.
        try:
            self.store._deploy_gate_ref().delete()
        except Exception:
            pass

    def _resolve_bootstrapped_org_id(self, *, slug: str) -> str:
        data = self.store.list_services(slug=slug) or {}
        return str(data.get("orgId") or "")

    def _service_ref(self):
        return self.store._service_ref(self.org_id_actual, self.service_key)

    def _rooms_col(self):
        return self.store._org_ref(self.org_id_actual).collection("rooms")

    def _count_rooms(self) -> int:
        return len(list(self._rooms_col().stream()))

    def _active_room_id(self) -> str:
        snap = self._service_ref().get()
        if not snap.exists:
            return ""
        return str((snap.to_dict() or {}).get("activeRoomId") or "")

    def _assert_start_did_not_touch_service_or_rooms(self):
        """A refused start MUST NOT create a room doc AND MUST NOT
        set `activeRoomId` on the service. This is the reviewer's
        explicit invariant for a rejected start."""
        self.assertEqual(self._count_rooms(), 0)
        self.assertEqual(self._active_room_id(), "")

    def test_absent_gate_permits_start(self):
        """Absent-document = unblocked (the safe default). Start
        succeeds and creates one room."""
        # Ensure the gate doc doesn't exist.
        self.store._deploy_gate_ref().delete()
        result = self.store.start_service(
            self.org_id_actual, self.service_key,
            host_uid=self.host_uid, source="ko", target="en",
        )
        self.assertEqual(result["status"], "live")
        self.assertEqual(self._count_rooms(), 1)
        self.assertEqual(self._active_room_id(), result["roomId"])

    def test_cleared_gate_permits_start(self):
        """`blocked: false` document present (revision auditable)
        = unblocked. Cleared state is the normal post-window
        state; must not accidentally block."""
        self.store._deploy_gate_ref().set({
            "blocked": False,
            "revision": 7,
            "reason": "post-window-clear",
            "blocked_by": "test",
        })
        result = self.store.start_service(
            self.org_id_actual, self.service_key,
            host_uid=self.host_uid, source="ko", target="en",
        )
        self.assertEqual(result["status"], "live")
        self.assertEqual(self._count_rooms(), 1)

    def test_blocked_gate_refuses_start_and_leaves_state_untouched(self):
        """The core BOOT-1 contract at the Firestore layer. A
        `blocked: true` gate → start_service raises
        `PermissionError("maintenance_blocked")` AND no room is
        created AND `activeRoomId` on the service remains unset."""
        self.store._deploy_gate_ref().set({
            "blocked": True,
            "revision": 3,
            "reason": "test-window-open",
            "blocked_by": "test",
        })
        with self.assertRaisesRegex(PermissionError, "maintenance_blocked"):
            self.store.start_service(
                self.org_id_actual, self.service_key,
                host_uid=self.host_uid, source="ko", target="en",
            )
        self._assert_start_did_not_touch_service_or_rooms()

    def test_malformed_missing_blocked_field_refuses(self):
        self.store._deploy_gate_ref().set({"revision": 1})
        with self.assertRaisesRegex(PermissionError, "maintenance_malformed_gate"):
            self.store.start_service(
                self.org_id_actual, self.service_key,
                host_uid=self.host_uid, source="ko", target="en",
            )
        self._assert_start_did_not_touch_service_or_rooms()

    def test_malformed_null_blocked_refuses(self):
        self.store._deploy_gate_ref().set({"blocked": None})
        with self.assertRaisesRegex(PermissionError, "maintenance_malformed_gate"):
            self.store.start_service(
                self.org_id_actual, self.service_key,
                host_uid=self.host_uid, source="ko", target="en",
            )
        self._assert_start_did_not_touch_service_or_rooms()

    def test_malformed_string_true_refuses(self):
        """Operator wrote `"true"` (JSON string) instead of `true`
        (JSON boolean). Fail-closed; the intent is undetermined."""
        self.store._deploy_gate_ref().set({"blocked": "true"})
        with self.assertRaisesRegex(PermissionError, "maintenance_malformed_gate"):
            self.store.start_service(
                self.org_id_actual, self.service_key,
                host_uid=self.host_uid, source="ko", target="en",
            )
        self._assert_start_did_not_touch_service_or_rooms()

    def test_read_deploy_gate_surfaces_firestore_state(self):
        """`read_deploy_gate` must return the same information
        against the emulator as it does in-memory (this is the
        surface the operator's set/clear script and dashboards
        read against, and its wrong answer during a window is
        arguably worse than a wrong gate write)."""
        # Absent.
        state = self.store.read_deploy_gate()
        self.assertFalse(state["blocked"])
        self.assertFalse(state["exists"])
        self.assertFalse(state["malformed"])

        # Blocked well-formed.
        self.store._deploy_gate_ref().set({
            "blocked": True,
            "revision": 5,
            "reason": "test",
        })
        state = self.store.read_deploy_gate()
        self.assertTrue(state["blocked"])
        self.assertTrue(state["exists"])
        self.assertFalse(state["malformed"])
        self.assertEqual(state["revision"], 5)

        # Malformed.
        self.store._deploy_gate_ref().set({"revision": 6})
        state = self.store.read_deploy_gate()
        self.assertTrue(state["blocked"])  # fail-closed
        self.assertTrue(state["malformed"])
        self.assertIn("blocked", state["malformed_reason"])


if __name__ == "__main__":
    unittest.main()
