# Design Ref: §3.3 Firestore Schema + §6.1 SERMON_MODIFIED_CONCURRENTLY +
# §6.1 SERVICE_ALREADY_LINKED. Tests cover the InMemory variant; production
# Firestore behavior is covered by integration tests in module-3.

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.services.multichurch_store import InMemoryMultiChurchStore
from app.sermon_review.models import (
    SermonConflictError,
    SermonNotFoundError,
    ServiceAlreadyLinkedError,
)


def _bootstrap_owner(
    store: InMemoryMultiChurchStore,
    *,
    owner_uid: str = "uid_owner",
    slug: str = "test",
    name: str = "Test Church",
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


def _sermon(org_id: str, sermon_id: str = "srm_test_001",
            *, now: datetime | None = None) -> dict:
    now = now or datetime(2026, 6, 16, tzinfo=timezone.utc)
    return {
        "sermonId": sermon_id,
        "orgId": org_id,
        "title": "Test Sermon",
        "sourceType": "paste",
        "sourceRef": None,
        "segments": [
            {
                "segmentId": "S001",
                "order": 1,
                "original": "오늘 우리는 하나님의 은혜를 보려고 합니다.",
                "appTranslation": "Today we will look at God's grace.",
                "reviewedTranslation": "Today we will look at God's grace.",
                "notes": "",
                "status": "Draft",
            },
            {
                "segmentId": "S002",
                "order": 2,
                "original": "은혜는 단지 좋은 감정이 아닙니다.",
                "appTranslation": "Grace is not merely a good feeling.",
                "reviewedTranslation": "Grace is not merely a good feeling.",
                "notes": "",
                "status": "Draft",
            },
        ],
        "createdBy": "uid_owner",
        "createdAt": now,
        "updatedAt": now,
    }


class CreateGetTests(unittest.TestCase):
    def test_create_then_get(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)
        store.create_review_sermon(_sermon(org_id))

        fetched = store.get_review_sermon(org_id, "srm_test_001")
        assert fetched is not None
        self.assertEqual(fetched["sermonId"], "srm_test_001")
        self.assertEqual(len(fetched["segments"]), 2)

    def test_get_missing_returns_none(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)
        self.assertIsNone(store.get_review_sermon(org_id, "missing"))

    def test_duplicate_create_raises(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)
        store.create_review_sermon(_sermon(org_id))
        with self.assertRaises(SermonConflictError):
            store.create_review_sermon(_sermon(org_id))


class ListTests(unittest.TestCase):
    def test_lists_only_org_sermons(self) -> None:
        store = InMemoryMultiChurchStore()
        org_a = _bootstrap_owner(store, owner_uid="ua", slug="a", name="A")
        org_b = _bootstrap_owner(store, owner_uid="ub", slug="b", name="B")

        store.create_review_sermon(_sermon(org_a, "srm_a1"))
        store.create_review_sermon(_sermon(org_a, "srm_a2"))
        store.create_review_sermon(_sermon(org_b, "srm_b1"))

        listed = store.list_review_sermons(org_a)
        ids = {s["sermonId"] for s in listed}
        self.assertEqual(ids, {"srm_a1", "srm_a2"})

    def test_orders_by_updated_at_desc(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)
        t0 = datetime(2026, 6, 16, tzinfo=timezone.utc)
        store.create_review_sermon(
            _sermon(org_id, "srm_old", now=t0)
        )
        store.create_review_sermon(
            _sermon(org_id, "srm_new", now=t0 + timedelta(hours=1))
        )

        listed = store.list_review_sermons(org_id)
        self.assertEqual(listed[0]["sermonId"], "srm_new")
        self.assertEqual(listed[1]["sermonId"], "srm_old")


class DeleteTests(unittest.TestCase):
    def test_delete_removes_sermon_and_unlinks_services(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)
        store.create_review_sermon(_sermon(org_id))
        service_key = list(store._services.keys())[0][1]
        store.link_review_sermon_to_service(
            org_id, service_key, "srm_test_001"
        )

        result = store.delete_review_sermon(org_id, "srm_test_001")

        self.assertIsNone(store.get_review_sermon(org_id, "srm_test_001"))
        self.assertIsNone(
            store._services[(org_id, service_key)]["linkedSermonId"]
        )
        self.assertEqual(result["unlinkedServiceKeys"], [service_key])

    def test_delete_missing_sermon_raises(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)

        with self.assertRaises(SermonNotFoundError):
            store.delete_review_sermon(org_id, "missing")


class UpdateSegmentsTests(unittest.TestCase):
    def test_update_with_matching_precondition(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)
        t0 = datetime(2026, 6, 16, tzinfo=timezone.utc)
        store.create_review_sermon(_sermon(org_id, now=t0))

        t1 = t0 + timedelta(minutes=5)
        result = store.update_review_sermon_segments(
            org_id,
            "srm_test_001",
            segment_updates=[
                {
                    "segmentId": "S001",
                    "reviewedTranslation": "Today we will look together at God's grace.",
                    "notes": "checked with pastor",
                    "status": "Reviewed",
                },
            ],
            expected_updated_at=t0,
            now=t1,
        )

        self.assertEqual(result["updatedAt"], t1)
        s1 = result["segments"][0]
        self.assertEqual(
            s1["reviewedTranslation"],
            "Today we will look together at God's grace.",
        )
        self.assertEqual(s1["status"], "Reviewed")
        self.assertEqual(s1["notes"], "checked with pastor")
        # S002 untouched
        s2 = result["segments"][1]
        self.assertEqual(s2["status"], "Draft")
        self.assertEqual(s2["notes"], "")

    def test_empty_reviewed_falls_back_to_app_translation(self) -> None:
        # Design FR-13: warnings (empty review) import with fallback.
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)
        t0 = datetime(2026, 6, 16, tzinfo=timezone.utc)
        store.create_review_sermon(_sermon(org_id, now=t0))

        result = store.update_review_sermon_segments(
            org_id,
            "srm_test_001",
            segment_updates=[
                {"segmentId": "S001", "reviewedTranslation": "   ", "status": "Skip"},
            ],
            expected_updated_at=t0,
        )
        s1 = result["segments"][0]
        self.assertEqual(s1["reviewedTranslation"], "Today we will look at God's grace.")
        self.assertEqual(s1["status"], "Skip")

    def test_mismatched_precondition_raises_conflict(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)
        t0 = datetime(2026, 6, 16, tzinfo=timezone.utc)
        store.create_review_sermon(_sermon(org_id, now=t0))

        stale = t0 - timedelta(hours=1)
        with self.assertRaises(SermonConflictError):
            store.update_review_sermon_segments(
                org_id,
                "srm_test_001",
                segment_updates=[
                    {"segmentId": "S001", "reviewedTranslation": "x"},
                ],
                expected_updated_at=stale,
            )

    def test_missing_sermon_raises(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)
        with self.assertRaises(SermonNotFoundError):
            store.update_review_sermon_segments(
                org_id,
                "does_not_exist",
                segment_updates=[],
                expected_updated_at=datetime.now(timezone.utc),
            )


class LinkTests(unittest.TestCase):
    def test_link_to_free_service(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)
        store.create_review_sermon(_sermon(org_id))

        services = list(store._services.keys())
        any_service_key = services[0][1]

        result = store.link_review_sermon_to_service(
            org_id, any_service_key, "srm_test_001"
        )
        self.assertEqual(result["linkedSermonId"], "srm_test_001")
        self.assertEqual(
            store._services[(org_id, any_service_key)]["linkedSermonId"],
            "srm_test_001",
        )

    def test_unlink_with_none(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)
        store.create_review_sermon(_sermon(org_id))
        any_service_key = list(store._services.keys())[0][1]
        store.link_review_sermon_to_service(
            org_id, any_service_key, "srm_test_001"
        )

        result = store.link_review_sermon_to_service(
            org_id, any_service_key, None
        )
        self.assertIsNone(result["linkedSermonId"])

    def test_link_replace_without_flag_raises(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)
        store.create_review_sermon(_sermon(org_id, "srm_a"))
        store.create_review_sermon(_sermon(org_id, "srm_b"))
        any_service_key = list(store._services.keys())[0][1]
        store.link_review_sermon_to_service(org_id, any_service_key, "srm_a")

        with self.assertRaises(ServiceAlreadyLinkedError):
            store.link_review_sermon_to_service(
                org_id, any_service_key, "srm_b"
            )

    def test_link_replace_with_flag_succeeds(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)
        store.create_review_sermon(_sermon(org_id, "srm_a"))
        store.create_review_sermon(_sermon(org_id, "srm_b"))
        any_service_key = list(store._services.keys())[0][1]
        store.link_review_sermon_to_service(org_id, any_service_key, "srm_a")

        result = store.link_review_sermon_to_service(
            org_id, any_service_key, "srm_b", replace=True
        )
        self.assertEqual(result["linkedSermonId"], "srm_b")

    def test_link_missing_sermon_raises(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)
        any_service_key = list(store._services.keys())[0][1]
        with self.assertRaises(SermonNotFoundError):
            store.link_review_sermon_to_service(
                org_id, any_service_key, "missing"
            )

    def test_link_missing_service_raises(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(store)
        store.create_review_sermon(_sermon(org_id))
        with self.assertRaises(LookupError):
            store.link_review_sermon_to_service(
                org_id, "no_such_service", "srm_test_001"
            )


if __name__ == "__main__":
    unittest.main()
