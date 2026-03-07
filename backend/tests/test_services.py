from __future__ import annotations

import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
