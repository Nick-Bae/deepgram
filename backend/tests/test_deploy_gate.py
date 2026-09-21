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

    def test_gate_revision_increments_monotonically(self) -> None:
        """Every set/clear bumps `revision`. The operator uses this in
        the audit trail (PR #31 §4b) to prove the gate was set before
        the drain check and cleared after the post-deploy check."""
        store = InMemoryMultiChurchStore()
        store._set_deploy_gate_for_test(blocked=True, reason="r1")
        rev1 = store.read_deploy_gate()["revision"]
        store._set_deploy_gate_for_test(blocked=False, reason="r1-clear")
        rev2 = store.read_deploy_gate()["revision"]
        store._set_deploy_gate_for_test(blocked=True, reason="r2")
        rev3 = store.read_deploy_gate()["revision"]
        self.assertEqual([rev1, rev2, rev3], [1, 2, 3])


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
