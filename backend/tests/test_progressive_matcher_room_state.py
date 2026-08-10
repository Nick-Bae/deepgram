from __future__ import annotations

import unittest
from dataclasses import replace

from app.sermon_review.progressive_matcher import INITIAL_STATE, State
from app.sermon_review.room_state import (
    ProgressiveMatcherRoomState,
    build_room_state,
)


class _FakeStore:
    def __init__(self, *, enabled=True, service=None, sermon=None, raise_on=None):
        self._enabled = enabled
        self._service = service
        self._sermon = sermon
        self._raise_on = raise_on or set()

    def get_org_progressive_manuscript_matching_enabled(self, org_id):
        if "flag" in self._raise_on:
            raise RuntimeError("boom")
        return self._enabled

    def get_service(self, org_id, service_key):
        if "service" in self._raise_on:
            raise RuntimeError("boom")
        return self._service

    def get_review_sermon(self, org_id, sermon_id):
        if "sermon" in self._raise_on:
            raise RuntimeError("boom")
        return self._sermon


class BuildRoomStateTests(unittest.TestCase):
    def test_no_org_id_returns_disabled(self) -> None:
        rs = build_room_state(store=_FakeStore(), org_id=None, room_id="r", service_key="s")
        self.assertFalse(rs.enabled)

    def test_flag_off_returns_disabled(self) -> None:
        store = _FakeStore(enabled=False)
        rs = build_room_state(store=store, org_id="org-1", room_id="r", service_key="s")
        self.assertFalse(rs.enabled)
        self.assertEqual(rs.org_id, "org-1")

    def test_flag_read_error_returns_disabled_and_does_not_raise(self) -> None:
        store = _FakeStore(enabled=True, raise_on={"flag"})
        rs = build_room_state(store=store, org_id="org-1", room_id="r", service_key="s")
        self.assertFalse(rs.enabled)

    def test_no_service_key_returns_disabled(self) -> None:
        store = _FakeStore(enabled=True)
        rs = build_room_state(store=store, org_id="org-1", room_id="r", service_key=None)
        self.assertFalse(rs.enabled)

    def test_no_linked_sermon_returns_disabled(self) -> None:
        store = _FakeStore(enabled=True, service={"linkedSermonId": None})
        rs = build_room_state(store=store, org_id="org-1", room_id="r", service_key="s")
        self.assertFalse(rs.enabled)

    def test_service_read_error_returns_disabled(self) -> None:
        store = _FakeStore(enabled=True, raise_on={"service"})
        rs = build_room_state(store=store, org_id="org-1", room_id="r", service_key="s")
        self.assertFalse(rs.enabled)

    def test_enabled_state_caches_segments(self) -> None:
        segments = [
            {"segmentId": "seg-1", "original": "안녕"},
            {"segmentId": "seg-2", "original": "여러분"},
        ]
        store = _FakeStore(
            enabled=True,
            service={"linkedSermonId": "sermon-42"},
            sermon={"segments": segments},
        )
        rs = build_room_state(store=store, org_id="org-1", room_id="r", service_key="s")
        self.assertTrue(rs.enabled)
        self.assertEqual(rs.sermon_id, "sermon-42")
        self.assertEqual(rs.segments, segments)
        self.assertEqual(rs.cursor, 0)


class RoomStateHelpersTests(unittest.TestCase):
    def test_advance_cursor_moves_past_named_segment(self) -> None:
        rs = ProgressiveMatcherRoomState(
            enabled=True,
            segments=[
                {"segmentId": "seg-1"},
                {"segmentId": "seg-2"},
                {"segmentId": "seg-3"},
            ],
            cursor=0,
        )
        rs.advance_cursor_to("seg-2")
        self.assertEqual(rs.cursor, 2)

    def test_advance_cursor_falls_back_to_increment_when_seg_id_missing(self) -> None:
        rs = ProgressiveMatcherRoomState(enabled=True, segments=[], cursor=5)
        rs.advance_cursor_to("seg-999")
        self.assertEqual(rs.cursor, 6)

    def test_reset_for_next_utterance_clears_state_and_trace(self) -> None:
        rs = ProgressiveMatcherRoomState(
            enabled=True, segments=[{"segmentId": "seg-1"}], cursor=0,
        )
        rs.state = State(kind="PREVIEW", seg_id="seg-1", confirmations=2, lock_score=0.9)
        rs.preview_shown_at_ms = 1234
        rs.ensure_trace()
        self.assertIsNotNone(rs.trace)
        rs.reset_for_next_utterance()
        self.assertEqual(rs.state, INITIAL_STATE)
        self.assertIsNone(rs.preview_shown_at_ms)
        self.assertIsNone(rs.trace)
        # cursor/segments unaffected
        self.assertEqual(rs.cursor, 0)
        self.assertEqual(len(rs.segments), 1)

    def test_ensure_trace_is_idempotent(self) -> None:
        rs = ProgressiveMatcherRoomState(enabled=True, org_id="o", room_id="r")
        t1 = rs.ensure_trace()
        t2 = rs.ensure_trace()
        self.assertIs(t1, t2)
        self.assertEqual(t1.org_id, "o")
        self.assertEqual(t1.room_id, "r")


if __name__ == "__main__":
    unittest.main()
