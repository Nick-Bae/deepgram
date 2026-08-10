from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services import multichurch_store as multichurch_store_module
from app.services.multichurch_store import (
    InMemoryMultiChurchStore,
    _patch_firestore_stream_retry_metadata,
)


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


class _FakeCallable:
    pass


class _FakeTransport:
    def __init__(self) -> None:
        self.run_query = _FakeCallable()
        self._wrapped_methods = {self.run_query: _FakeCallable()}
        self._wrapped_methods[self.run_query]._retry = object()


class _FakeFirestoreApi:
    def __init__(self) -> None:
        self._transport = _FakeTransport()


class _FakeFirestoreDb:
    def __init__(self) -> None:
        self._firestore_api = _FakeFirestoreApi()


class FirestoreCompatibilityTests(unittest.TestCase):
    def test_stream_retry_metadata_is_backfilled_for_raw_run_query_callable(self) -> None:
        db = _FakeFirestoreDb()
        raw_run_query = db._firestore_api._transport.run_query
        wrapped_run_query = db._firestore_api._transport._wrapped_methods[raw_run_query]

        self.assertFalse(hasattr(raw_run_query, "_retry"))

        _patch_firestore_stream_retry_metadata(db)

        self.assertIs(raw_run_query._retry, wrapped_run_query._retry)


class ServiceManagementTests(unittest.TestCase):
    def test_host_can_create_and_delete_service(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(
            store,
            owner_uid="owner-service-1",
            slug="service-org-a",
            name="Service Org A",
        )

        invite = store.create_invite(
            org_id=org_id,
            created_by_uid="owner-service-1",
            role="host",
            expires_in_hours=24,
        )
        store.redeem_invite(
            code=invite["code"],
            uid="host-service-1",
            email="host-service-1@example.com",
            display_name="Host Service 1",
        )

        created = store.create_service(
            org_id=org_id,
            service_key="sun-9am",
            requested_by_uid="host-service-1",
            title="Sunday 9 AM",
            timezone="America/Chicago",
            source="ko",
            target="en",
        )
        self.assertEqual(created["serviceKey"], "sun-9am")
        self.assertEqual(created["title"], "Sunday 9 AM")

        listed = store.list_services("service-org-a")
        self.assertIsNotNone(listed)
        keys = {str(row.get("serviceKey") or "") for row in (listed or {}).get("services") or []}
        self.assertIn("sun-9am", keys)

        deleted = store.delete_service(
            org_id=org_id,
            service_key="sun-9am",
            requested_by_uid="host-service-1",
        )
        self.assertTrue(bool(deleted.get("deleted")))

        listed_after = store.list_services("service-org-a")
        self.assertIsNotNone(listed_after)
        keys_after = {str(row.get("serviceKey") or "") for row in (listed_after or {}).get("services") or []}
        self.assertNotIn("sun-9am", keys_after)

    def test_cannot_delete_live_service(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(
            store,
            owner_uid="owner-service-2",
            slug="service-org-b",
            name="Service Org B",
        )
        store.create_service(
            org_id=org_id,
            service_key="wed-6pm",
            requested_by_uid="owner-service-2",
            title="Wednesday 6 PM",
            timezone="America/Chicago",
            source="ko",
            target="en",
        )
        store.start_service(
            org_id,
            "wed-6pm",
            host_uid="owner-service-2",
            source="ko",
            target="en",
        )

        with self.assertRaisesRegex(ValueError, "service_active"):
            store.delete_service(
                org_id=org_id,
                service_key="wed-6pm",
                requested_by_uid="owner-service-2",
            )

    def test_org_billing_toggle_allows_start_when_hard_cap_reached(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(
            store,
            owner_uid="owner-service-3",
            slug="service-org-c",
            name="Service Org C",
        )

        store._orgs[org_id]["hardCapReached"] = True

        with self.assertRaisesRegex(PermissionError, "hard_cap_reached"):
            store.start_service(
                org_id,
                "sun-11am",
                host_uid="owner-service-3",
                source="ko",
                target="en",
            )

        with patch.object(multichurch_store_module, "MASTER_USER_UIDS", {"owner-service-3"}):
            updated = store.set_org_billing_limits(
                org_id=org_id,
                requested_by_uid="owner-service-3",
                enabled=False,
            )
        self.assertFalse(bool(updated.get("billingLimitsEnabled")))
        self.assertFalse(bool(updated.get("effectiveBillingLimitsEnabled")))

        started = store.start_service(
            org_id,
            "sun-11am",
            host_uid="owner-service-3",
            source="ko",
            target="en",
        )
        self.assertEqual(started.get("status"), "live")

    def test_live_rooms_lists_only_active_rooms(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(
            store,
            owner_uid="owner-service-4",
            slug="service-org-d",
            name="Service Org D",
        )

        started = store.start_service(
            org_id,
            "sun-11am",
            host_uid="owner-service-4",
            source="ko",
            target="en",
        )
        room_id = str(started.get("roomId") or "")
        self.assertTrue(room_id)

        live_rooms = store.live_rooms()
        matching = [
            row for row in live_rooms
            if row.get("orgId") == org_id
            and row.get("roomId") == room_id
            and row.get("serviceKey") == "sun-11am"
        ]
        self.assertEqual(len(matching), 1)

        store.end_room(org_id, room_id, reason="host_end")
        live_rooms_after = store.live_rooms()
        matching_after = [
            row for row in live_rooms_after
            if row.get("orgId") == org_id and row.get("roomId") == room_id
        ]
        self.assertEqual(matching_after, [])


if __name__ == "__main__":
    unittest.main()
