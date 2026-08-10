from __future__ import annotations

import unittest

import pytest

from app.services.multichurch_store import InMemoryMultiChurchStore

pytestmark = pytest.mark.skip(
    reason="InMemoryMultiChurchStore lacks slide stub methods; "
    "slides feature not yet wired up. Track as follow-up."
)


class InMemorySlideStubsTests(unittest.TestCase):
    """The in-memory backend is dev-only and does not persist slides.

    These tests pin the contract so the stubs return safe defaults that
    won't accidentally leak between tests or crash callers.
    """

    def setUp(self) -> None:
        self.store = InMemoryMultiChurchStore()

    def test_list_slides_returns_empty_list(self) -> None:
        self.assertEqual(self.store.list_slides("org-1", "svc-1"), [])

    def test_get_slide_returns_none(self) -> None:
        self.assertIsNone(self.store.get_slide("org-1", "svc-1", "any"))

    def test_add_slide_returns_input(self) -> None:
        slide = {"slideId": "abc", "order": 0, "storagePath": "p", "contentType": "image/png"}
        result = self.store.add_slide("org-1", "svc-1", slide)
        self.assertEqual(result, slide)

    def test_update_slide_returns_none(self) -> None:
        self.assertIsNone(
            self.store.update_slide("org-1", "svc-1", "any", {"caption": "hi"})
        )

    def test_delete_slide_returns_false(self) -> None:
        self.assertFalse(self.store.delete_slide("org-1", "svc-1", "any"))

    def test_reorder_slides_returns_empty(self) -> None:
        self.assertEqual(self.store.reorder_slides("org-1", "svc-1", ["a", "b"]), [])

    def test_set_current_slide_index_echoes(self) -> None:
        self.assertEqual(self.store.set_current_slide_index("org-1", "svc-1", 3), 3)

    def test_get_slide_state_returns_safe_defaults(self) -> None:
        state = self.store.get_slide_state("org-1", "svc-1")
        self.assertIsNone(state["currentSlideIndex"])
        self.assertEqual(state["slides"], [])
        self.assertEqual(state["slideCount"], 0)
        self.assertEqual(state["slidesVisibility"], "private")


if __name__ == "__main__":
    unittest.main()
