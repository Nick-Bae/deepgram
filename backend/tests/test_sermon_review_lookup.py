# Design Ref: §2.2 Live Broadcast hook + FR-14 + FR-15 (Skip semantics).
# Pure unit tests for the fuzzy-match lookup used by the live translation
# pipeline.

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.services.multichurch_store import InMemoryMultiChurchStore
from app.sermon_review import get_reviewed_text


def _bootstrap(store: InMemoryMultiChurchStore) -> tuple[str, str]:
    result = store.bootstrap_owner_org(
        owner_uid="uid_owner",
        owner_email="uid_owner@example.com",
        owner_display_name="uid_owner",
        church_name="Test Church",
        church_slug="test",
        timezone="America/Chicago",
        source="ko",
        target="en",
    )
    org_id = str(result["orgId"])
    service_key = list(store._services.keys())[0][1]
    return org_id, service_key


def _sermon(org_id: str, sermon_id: str = "srm_lookup") -> dict:
    now = datetime(2026, 6, 16, tzinfo=timezone.utc)
    return {
        "sermonId": sermon_id,
        "orgId": org_id,
        "title": "Test",
        "sourceType": "paste",
        "sourceRef": None,
        "segments": [
            {
                "segmentId": "S001",
                "order": 1,
                "original": "오늘 우리는 하나님의 은혜를 보려고 합니다.",
                "appTranslation": "Today we will look at God's grace.",
                "reviewedTranslation": "Today, we will look together at the grace of God.",
                "notes": "",
                "status": "Reviewed",
            },
            {
                "segmentId": "S002",
                "order": 2,
                "original": "은혜는 단지 좋은 감정이 아닙니다.",
                "appTranslation": "Grace is not merely a good feeling.",
                "reviewedTranslation": "Grace is not just a comforting emotion.",
                "notes": "",
                "status": "Reviewed",
            },
            {
                "segmentId": "S003",
                "order": 3,
                "original": "기도합시다.",
                "appTranslation": "Let us pray.",
                "reviewedTranslation": "Let us pray together.",
                "notes": "",
                "status": "Skip",  # FR-15
            },
        ],
        "createdBy": "uid_owner",
        "createdAt": datetime(2026, 6, 16, tzinfo=timezone.utc),
        "updatedAt": datetime(2026, 6, 16, tzinfo=timezone.utc),
    }


class NoLinkedSermonTests(unittest.TestCase):
    def test_returns_none_when_no_service_key(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id, _ = _bootstrap(store)
        store.create_review_sermon(_sermon(org_id))
        self.assertIsNone(
            get_reviewed_text(
                store=store,
                org_id=org_id,
                service_key=None,
                korean_text="오늘 우리는 하나님의 은혜를 보려고 합니다.",
            )
        )

    def test_uses_public_service_getter_for_firestore_style_store(self) -> None:
        sermon = _sermon("org-firestore")

        class FirestoreStyleStore:
            def get_service(self, org_id: str, service_key: str) -> dict:
                self.requested_service = (org_id, service_key)
                return {"linkedSermonId": "srm_lookup"}

            def get_review_sermon(self, org_id: str, sermon_id: str) -> dict:
                self.requested_sermon = (org_id, sermon_id)
                return sermon

        store = FirestoreStyleStore()
        result = get_reviewed_text(
            store=store,
            org_id="org-firestore",
            service_key="sun-11am",
            korean_text="오늘 우리는 하나님의 은혜를 보려고 합니다.",
        )

        self.assertEqual(
            result,
            "Today, we will look together at the grace of God.",
        )
        self.assertEqual(store.requested_service, ("org-firestore", "sun-11am"))
        self.assertEqual(store.requested_sermon, ("org-firestore", "srm_lookup"))

    def test_returns_none_when_service_has_no_linked_sermon(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id, service_key = _bootstrap(store)
        store.create_review_sermon(_sermon(org_id))
        # Service exists but no linkedSermonId set.
        self.assertIsNone(
            get_reviewed_text(
                store=store,
                org_id=org_id,
                service_key=service_key,
                korean_text="오늘 우리는 하나님의 은혜를 보려고 합니다.",
            )
        )

    def test_returns_none_when_korean_empty(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id, service_key = _bootstrap(store)
        store.create_review_sermon(_sermon(org_id))
        store.link_review_sermon_to_service(org_id, service_key, "srm_lookup")
        self.assertIsNone(
            get_reviewed_text(
                store=store,
                org_id=org_id,
                service_key=service_key,
                korean_text="",
            )
        )


class MatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryMultiChurchStore()
        self.org_id, self.service_key = _bootstrap(self.store)
        self.store.create_review_sermon(_sermon(self.org_id))
        self.store.link_review_sermon_to_service(
            self.org_id, self.service_key, "srm_lookup"
        )

    def test_exact_match_returns_reviewed_text(self) -> None:
        out = get_reviewed_text(
            store=self.store,
            org_id=self.org_id,
            service_key=self.service_key,
            korean_text="오늘 우리는 하나님의 은혜를 보려고 합니다.",
        )
        self.assertEqual(
            out,
            "Today, we will look together at the grace of God.",
        )

    def test_close_paraphrase_above_threshold_returns_match(self) -> None:
        # Slight phrasing variation
        out = get_reviewed_text(
            store=self.store,
            org_id=self.org_id,
            service_key=self.service_key,
            korean_text="오늘 우리는 하나님의 은혜를 보려고 합니다",
        )
        self.assertEqual(
            out,
            "Today, we will look together at the grace of God.",
        )

    def test_unrelated_korean_returns_none(self) -> None:
        out = get_reviewed_text(
            store=self.store,
            org_id=self.org_id,
            service_key=self.service_key,
            korean_text="저는 오늘 새로운 음식을 먹었어요.",
        )
        self.assertIsNone(out)


class SkipSemanticsTests(unittest.TestCase):
    def test_skip_segment_is_ignored(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id, service_key = _bootstrap(store)
        store.create_review_sermon(_sermon(org_id))
        store.link_review_sermon_to_service(org_id, service_key, "srm_lookup")

        # The Skip segment is exactly "기도합시다." — even an exact match
        # should NOT return its reviewed text. Returns None so the broadcast
        # falls back to machine translation (FR-15).
        out = get_reviewed_text(
            store=store,
            org_id=org_id,
            service_key=service_key,
            korean_text="기도합시다.",
        )
        self.assertIsNone(out)


class ThresholdTests(unittest.TestCase):
    def test_custom_threshold_allows_looser_match(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id, service_key = _bootstrap(store)
        store.create_review_sermon(_sermon(org_id))
        store.link_review_sermon_to_service(org_id, service_key, "srm_lookup")

        weak_input = "오늘 우리는 하나님의 은혜"
        self.assertIsNone(
            get_reviewed_text(
                store=store,
                org_id=org_id,
                service_key=service_key,
                korean_text=weak_input,
                threshold=0.95,
            )
        )
        looser = get_reviewed_text(
            store=store,
            org_id=org_id,
            service_key=service_key,
            korean_text=weak_input,
            threshold=0.3,
        )
        self.assertIsNotNone(looser)


class ReviewedFallbackTests(unittest.TestCase):
    def test_empty_reviewed_falls_back_to_app_translation(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id, service_key = _bootstrap(store)

        sermon = _sermon(org_id)
        sermon["segments"][0]["reviewedTranslation"] = ""
        store.create_review_sermon(sermon)
        store.link_review_sermon_to_service(org_id, service_key, "srm_lookup")

        out = get_reviewed_text(
            store=store,
            org_id=org_id,
            service_key=service_key,
            korean_text="오늘 우리는 하나님의 은혜를 보려고 합니다.",
        )
        self.assertEqual(out, "Today we will look at God's grace.")


if __name__ == "__main__":
    unittest.main()
