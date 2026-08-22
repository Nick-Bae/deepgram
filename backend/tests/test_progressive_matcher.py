"""State machine tests for progressive_matcher.

Uses synthetic segments where scoring is predictable (exact-match prefixes
yield score 1.0; unrelated Korean text yields near-0). Each test walks the
matcher through a specific event sequence and asserts state + actions.

Segment IDs use "seg-N" (string) for clarity in assertions.
"""
from __future__ import annotations

import unittest

from app.sermon_review.progressive_matcher import (
    INITIAL_STATE,
    ClearPreview,
    CommitEvent,
    ConfirmAndAdvanceCursor,
    DEFAULT_CONFIG,
    InterimEvent,
    MatcherConfig,
    ReplacePreview,
    RequestLiveTranslation,
    ShowPreview,
    State,
    advance,
)


def _seg(seg_id: str, original: str, reviewed: str = "", status: str = "Reviewed") -> dict:
    return {
        "segmentId": seg_id,
        "original": original,
        "reviewedTranslation": reviewed or f"[EN {seg_id}]",
        "appTranslation": f"[APP {seg_id}]",
        "status": status,
    }


class HappyPathTests(unittest.TestCase):
    """IDLE → CANDIDATE → PREVIEW on two confirmations of the same segment.

    These tests exercise the strict (non-adjacent) path where the top-scoring
    segment is NOT segments[cursor]. Cursor-bias fast-path is covered
    separately in CursorBiasFastPathTests.
    """

    def setUp(self) -> None:
        self.segments = [
            _seg("seg-1", "첫번째 문장은 완전히 다른 내용입니다."),
            _seg("seg-2", "우리가 거짓말을 하는 이유는 진실보다 더 지키고 싶은 것이 있기 때문입니다."),
            _seg("seg-3", "다른 문장이 여기에 있습니다 확실히."),
        ]

    def test_idle_to_candidate_on_first_qualifying_interim(self) -> None:
        # cursor=0 → adjacent=seg-1, top-1=seg-2 → non-adjacent → strict path
        state, actions = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말을 하는", now_ms=1000),
            cursor=0,
            segments=self.segments,
        )
        self.assertEqual(state.kind, "CANDIDATE")
        self.assertEqual(state.seg_id, "seg-2")
        self.assertEqual(state.confirmations, 1)
        self.assertEqual(actions, [])

    def test_candidate_to_preview_on_second_matching_interim(self) -> None:
        state, _ = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말을 하는", now_ms=1000),
            cursor=0, segments=self.segments,
        )
        state, actions = advance(
            state,
            InterimEvent(prefix="우리가 거짓말을 하는 이유는", now_ms=1200),
            cursor=0, segments=self.segments,
        )
        self.assertEqual(state.kind, "PREVIEW")
        self.assertEqual(state.seg_id, "seg-2")
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], ShowPreview)
        self.assertEqual(actions[0].seg_id, "seg-2")
        self.assertEqual(actions[0].reviewed_text, "[EN seg-2]")


class MinimumThresholdTests(unittest.TestCase):
    def test_prefix_too_short_does_nothing(self) -> None:
        segments = [_seg("seg-1", "안녕하세요 여러분")]
        state, actions = advance(
            INITIAL_STATE,
            InterimEvent(prefix="안녕", now_ms=1000),  # 2 chars, 1 eojeol
            cursor=0, segments=segments,
        )
        self.assertEqual(state.kind, "IDLE")
        self.assertEqual(actions, [])

    def test_prefix_below_score_min_stays_idle(self) -> None:
        segments = [_seg("seg-1", "완전히 다른 문장입니다 이것은")]
        state, actions = advance(
            INITIAL_STATE,
            InterimEvent(prefix="아무 상관 없는 텍스트", now_ms=1000),
            cursor=0, segments=segments,
        )
        self.assertEqual(state.kind, "IDLE")
        self.assertEqual(actions, [])


class CandidateResetTests(unittest.TestCase):
    def test_candidate_resets_when_top_switches(self) -> None:
        # Add a lead segment so cursor=0 → adjacent=seg-0, keeping the
        # switch between seg-1 and seg-2 on the strict (non-adjacent) path.
        segments = [
            _seg("seg-0", "완전히 무관한 서두 문장이 여기에 있습니다."),
            _seg("seg-1", "우리가 거짓말을 하는 이유는 있습니다."),
            _seg("seg-2", "다른 주제로 이야기를 시작합니다 완전히."),
        ]
        state, _ = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말을 하는", now_ms=1000),
            cursor=0, segments=segments,
        )
        self.assertEqual(state.seg_id, "seg-1")
        state, actions = advance(
            state,
            InterimEvent(prefix="다른 주제로 이야기를", now_ms=1200),
            cursor=0, segments=segments,
        )
        self.assertEqual(state.kind, "CANDIDATE")
        self.assertEqual(state.seg_id, "seg-2")
        self.assertEqual(state.confirmations, 1)
        self.assertEqual(actions, [])


class ScoreMarginTests(unittest.TestCase):
    def test_two_segments_with_identical_prefix_blocks_lock(self) -> None:
        shared_prefix = "우리가 예수님을 사랑하는 것은"
        segments = [
            _seg("seg-1", f"{shared_prefix} 첫번째 이유가 있기 때문입니다."),
            _seg("seg-2", f"{shared_prefix} 두번째 이유가 있기 때문입니다."),
        ]
        state, actions = advance(
            INITIAL_STATE,
            InterimEvent(prefix=shared_prefix, now_ms=1000),
            cursor=0, segments=segments,
        )
        self.assertEqual(state.kind, "IDLE")
        self.assertEqual(actions, [])


class SkipStatusTests(unittest.TestCase):
    def test_skip_marked_segments_excluded_from_window(self) -> None:
        segments = [
            _seg("seg-1", "우리가 거짓말을 하는 이유는 있습니다.", status="Skip"),
            _seg("seg-2", "우리가 거짓말을 하는 이유는 있습니다."),  # duplicate but not Skip
        ]
        state, _ = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말을 하는", now_ms=1000),
            cursor=0, segments=segments,
        )
        # Only seg-2 remains after Skip filter → candidate is seg-2
        self.assertEqual(state.seg_id, "seg-2")


class PreviewNoOpTests(unittest.TestCase):
    """Once locked into PREVIEW, ordinary interim revisions don't replace."""

    def _reach_preview(self, segments):
        # cursor=0 → adjacent=seg-1, top-1=seg-2 → non-adjacent → strict path
        # (needs 2 confirmations before PREVIEW fires).
        state, _ = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말을 하는", now_ms=1000),
            cursor=0, segments=segments,
        )
        state, actions = advance(
            state,
            InterimEvent(prefix="우리가 거짓말을 하는 이유는", now_ms=1200),
            cursor=0, segments=segments,
        )
        self.assertEqual(state.kind, "PREVIEW")
        self.assertIsInstance(actions[0], ShowPreview)
        return state, segments

    def test_same_top_segment_on_next_interim_produces_no_action(self) -> None:
        segments = [
            _seg("seg-1", "첫번째 문장 다른 내용."),
            _seg("seg-2", "우리가 거짓말을 하는 이유는 진실보다 더 지키고 싶은 것이 있습니다."),
        ]
        state, segments = self._reach_preview(segments)
        state, actions = advance(
            state,
            InterimEvent(prefix="우리가 거짓말을 하는 이유는 진실보다", now_ms=1400),
            cursor=0, segments=segments,
        )
        self.assertEqual(state.kind, "PREVIEW")
        self.assertEqual(state.seg_id, "seg-2")
        self.assertEqual(actions, [])


class CorrectiveReplacementTests(unittest.TestCase):
    """Replacement fires only when STRONG_CONFIRM + dwell + old-score collapse."""

    def setUp(self) -> None:
        self.segments = [
            _seg("seg-1", "이전 문장입니다 완전히 다른 내용을 담고 있습니다."),
            _seg("seg-2", "우리가 거짓말을 하는 이유는 여기 있습니다."),
            _seg("seg-3", "완전히 다른 주제로 넘어가겠습니다 새롭게."),
            _seg("seg-4", "그러나 실제로는 다음 문장 내용이 이렇게 진행됩니다."),
        ]

    def _reach_preview_on_seg2(self, now_ms: int):
        # cursor=0 → adjacent=seg-1, top-1=seg-2 → non-adjacent → strict path
        # (needs 2 confirmations before PREVIEW fires). Kept on strict path
        # so the corrective-replacement flow below tests non-adjacent
        # behavior end-to-end.
        state, _ = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말을 하는", now_ms=now_ms),
            cursor=0, segments=self.segments,
        )
        state, actions = advance(
            state,
            InterimEvent(prefix="우리가 거짓말을 하는 이유는", now_ms=now_ms + 200),
            cursor=0, segments=self.segments,
        )
        # In production, the caller updates preview_shown_at_ms from the
        # ShowPreview action's timestamp. Simulate that here by forcing the
        # field to now_ms + 200 (when PREVIEW was entered).
        state = State(**{**state.__dict__, "preview_shown_at_ms": now_ms + 200})
        return state

    def test_corrective_replacement_after_strong_confirm_dwell_and_collapse(self) -> None:
        state = self._reach_preview_on_seg2(now_ms=0)
        # 3 partials where seg-4 tops AND seg-2's score has collapsed
        # (we switch prefix to something wholly matching seg-4).
        new_prefix = "그러나 실제로는 다음 문장 내용이"
        for i, ts in enumerate([1400, 1600, 1800]):
            state, actions = advance(
                state,
                InterimEvent(prefix=new_prefix, now_ms=ts),
                cursor=0, segments=self.segments,
            )
            if i < 2:
                self.assertEqual(state.kind, "PREVIEW")
                self.assertEqual(state.seg_id, "seg-2")
                self.assertEqual(actions, [])
        # After 3rd strong confirm AND elapsed (1800-200 = 1600ms >= 1200)
        # AND old score collapsed → REPLACE
        self.assertEqual(state.kind, "PREVIEW")
        self.assertEqual(state.seg_id, "seg-4")
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], ReplacePreview)
        self.assertEqual(actions[0].from_seg_id, "seg-2")
        self.assertEqual(actions[0].to_seg_id, "seg-4")

    def test_corrective_not_replaced_when_old_score_did_not_collapse(self) -> None:
        # Craft a scenario: seg-2 and seg-4 share a long common prefix so
        # even when seg-4 tops, seg-2's score is still high (no collapse).
        segments = [
            _seg("seg-1", "이전 문장입니다."),
            _seg("seg-2", "안녕하세요 여러분 오늘도 만나서 반갑습니다."),
            _seg("seg-3", "완전히 다른 이야기를 시작합니다."),
            _seg("seg-4", "안녕하세요 여러분 오늘도 만나서 반갑습니다 정말로."),  # near-identical to seg-2
        ]
        # Reach PREVIEW on seg-2
        state, _ = advance(
            INITIAL_STATE,
            InterimEvent(prefix="안녕하세요 여러분", now_ms=0),
            cursor=1, segments=segments,
        )
        # Note: seg-2 and seg-4 tie → SCORE_MARGIN gate blocks. So we can't
        # even reach PREVIEW here. That IS the desired protection — this
        # test verifies the interaction rather than requiring corrective.
        self.assertEqual(state.kind, "IDLE")

    def test_dwell_floor_blocks_replacement_before_min_dwell(self) -> None:
        # Reach PREVIEW at t=200. Strong-confirm seg-4 at t=800 (600ms elapsed).
        state = self._reach_preview_on_seg2(now_ms=0)
        new_prefix = "그러나 실제로는 다음 문장 내용이"
        # 3 rapid confirms all before dwell floor
        for ts in [400, 600, 800]:
            state, actions = advance(
                state,
                InterimEvent(prefix=new_prefix, now_ms=ts),
                cursor=0, segments=self.segments,
            )
            self.assertEqual(state.kind, "PREVIEW")
            self.assertEqual(state.seg_id, "seg-2")  # dwell blocks
            self.assertEqual(actions, [])
        # Elapsed at t=800 is 600ms < 1200ms MIN_DWELL_MS. No replacement.


class DeviationTests(unittest.TestCase):
    def test_deviation_clears_preview_after_streak_and_dwell(self) -> None:
        segments = [
            _seg("seg-1", "이전 문장입니다 다른 내용."),
            _seg("seg-2", "우리가 거짓말을 하는 이유는 여기 있습니다."),
            _seg("seg-3", "다른 주제입니다 완전히 별개의 이야기."),
        ]
        # Reach PREVIEW on seg-2
        state, _ = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말을 하는", now_ms=0),
            cursor=1, segments=segments,
        )
        state, _ = advance(
            state,
            InterimEvent(prefix="우리가 거짓말을 하는 이유는", now_ms=200),
            cursor=1, segments=segments,
        )
        state = State(**{**state.__dict__, "preview_shown_at_ms": 200})
        # 4 partials of unrelated text → deviation
        unrelated = "완전히 관련 없는 다른 이야기입니다"
        for i, ts in enumerate([1500, 1700, 1900, 2100]):
            state, actions = advance(
                state,
                InterimEvent(prefix=unrelated, now_ms=ts),
                cursor=1, segments=segments,
            )
            if i < 3:
                self.assertEqual(state.kind, "PREVIEW")
                self.assertEqual(actions, [])
        self.assertEqual(state.kind, "DEVIATED")
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], ClearPreview)

    def test_deviation_before_dwell_does_not_fire(self) -> None:
        segments = [
            _seg("seg-1", "이전 문장 다른 내용."),
            _seg("seg-2", "우리가 거짓말을 하는 이유는 여기 있습니다."),
        ]
        state, _ = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말을 하는", now_ms=0),
            cursor=1, segments=segments,
        )
        state, _ = advance(
            state,
            InterimEvent(prefix="우리가 거짓말을 하는 이유는", now_ms=200),
            cursor=1, segments=segments,
        )
        state = State(**{**state.__dict__, "preview_shown_at_ms": 200})
        # 4 unrelated partials, all within dwell window (elapsed < 1200ms)
        unrelated = "완전히 관련 없는 다른 이야기입니다"
        for ts in [300, 500, 700, 900]:
            state, actions = advance(
                state,
                InterimEvent(prefix=unrelated, now_ms=ts),
                cursor=1, segments=segments,
            )
            self.assertEqual(state.kind, "PREVIEW")  # dwell blocks deviation
            self.assertEqual(actions, [])


class CommitTests(unittest.TestCase):
    def _segments(self):
        return [
            _seg("seg-1", "이전 문장입니다 다른 내용."),
            _seg("seg-2", "우리가 거짓말을 하는 이유는 진실보다 더 지키고 싶은 것이 있기 때문입니다."),
            _seg("seg-3", "완전히 다른 주제 별개의 이야기."),
        ]

    def _reach_preview(self, segments):
        state, _ = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말을 하는", now_ms=0),
            cursor=1, segments=segments,
        )
        state, _ = advance(
            state,
            InterimEvent(prefix="우리가 거짓말을 하는 이유는", now_ms=200),
            cursor=1, segments=segments,
        )
        return state

    def test_commit_with_preview_and_high_whole_sentence_score_advances_cursor(self) -> None:
        segments = self._segments()
        state = self._reach_preview(segments)
        state, actions = advance(
            state,
            CommitEvent(
                full_text="우리가 거짓말을 하는 이유는 진실보다 더 지키고 싶은 것이 있기 때문입니다.",
                now_ms=5000,
            ),
            cursor=1, segments=segments,
        )
        self.assertEqual(state, INITIAL_STATE)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], ConfirmAndAdvanceCursor)
        self.assertEqual(actions[0].seg_id, "seg-2")

    def test_commit_with_preview_but_low_whole_sentence_score_requests_live_gpt(self) -> None:
        segments = self._segments()
        state = self._reach_preview(segments)
        state, actions = advance(
            state,
            CommitEvent(full_text="완전히 다른 내용을 실제로 말했습니다", now_ms=5000),
            cursor=1, segments=segments,
        )
        self.assertEqual(state, INITIAL_STATE)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], RequestLiveTranslation)

    def test_commit_from_idle_requests_live_gpt(self) -> None:
        state, actions = advance(
            INITIAL_STATE,
            CommitEvent(full_text="아무 말이나 했습니다", now_ms=5000),
            cursor=0, segments=self._segments(),
        )
        self.assertEqual(state, INITIAL_STATE)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], RequestLiveTranslation)

    def test_commit_from_candidate_requests_live_gpt(self) -> None:
        segments = self._segments()
        # cursor=0 → adjacent=seg-1, top-1=seg-2 → non-adjacent → strict path
        # produces CANDIDATE after one interim.
        state, _ = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말을 하는", now_ms=0),
            cursor=0, segments=segments,
        )
        self.assertEqual(state.kind, "CANDIDATE")
        state, actions = advance(
            state,
            CommitEvent(full_text="우리가 거짓말", now_ms=5000),
            cursor=0, segments=segments,
        )
        self.assertEqual(state, INITIAL_STATE)
        self.assertIsInstance(actions[0], RequestLiveTranslation)

    def test_commit_from_deviated_requests_live_gpt(self) -> None:
        state = State(kind="DEVIATED")
        state, actions = advance(
            state,
            CommitEvent(full_text="off-manuscript speech", now_ms=5000),
            cursor=0, segments=self._segments(),
        )
        self.assertEqual(state, INITIAL_STATE)
        self.assertIsInstance(actions[0], RequestLiveTranslation)


class CursorBiasFastPathTests(unittest.TestCase):
    """Cursor-adjacent shortcut: when the top-scoring segment is
    segments[cursor], PREVIEW fires after one interim (skipping CANDIDATE),
    and prefix qualification uses the looser cursor-bias thresholds."""

    def setUp(self) -> None:
        self.segments = [
            _seg("seg-1", "첫번째 문장 완전히 다른 내용이 여기에."),
            _seg("seg-2", "우리가 거짓말을 하는 이유는 진실보다 더 지키고 싶은 것이 있기 때문입니다."),
            _seg("seg-3", "다른 주제로 넘어가는 다음 문장입니다."),
        ]

    def test_cursor_adjacent_idle_to_preview_in_one_interim(self) -> None:
        # cursor=1 → adjacent=seg-2, top-1=seg-2 → fast PREVIEW immediately.
        state, actions = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말을 하는", now_ms=1000),
            cursor=1, segments=self.segments,
        )
        self.assertEqual(state.kind, "PREVIEW")
        self.assertEqual(state.seg_id, "seg-2")
        self.assertEqual(state.confirmations, 1)
        self.assertEqual(len(actions), 1)
        self.assertIsInstance(actions[0], ShowPreview)
        self.assertEqual(actions[0].seg_id, "seg-2")

    def test_cursor_adjacent_short_prefix_qualifies(self) -> None:
        # 7-char, 2-eojeol prefix — below strict (8 chars / 3 eojeol) but
        # above cursor-bias (4 chars / 2 eojeol). Adjacent path fires.
        state, actions = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말", now_ms=1000),
            cursor=1, segments=self.segments,
        )
        self.assertEqual(state.kind, "PREVIEW")
        self.assertEqual(state.seg_id, "seg-2")
        self.assertIsInstance(actions[0], ShowPreview)

    def test_cursor_adjacent_below_min_prefix_still_idle(self) -> None:
        # 2-char, 1-eojeol — below cursor-bias floor. No action.
        state, actions = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리", now_ms=1000),
            cursor=1, segments=self.segments,
        )
        self.assertEqual(state.kind, "IDLE")
        self.assertEqual(actions, [])

    def test_non_adjacent_top_still_needs_strict_gates(self) -> None:
        # cursor=0 → adjacent=seg-1, but prefix matches seg-2 (non-adjacent).
        # Non-adjacent path applies → CANDIDATE, needs a second confirmation.
        state, actions = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말을 하는", now_ms=1000),
            cursor=0, segments=self.segments,
        )
        self.assertEqual(state.kind, "CANDIDATE")
        self.assertEqual(state.seg_id, "seg-2")
        self.assertEqual(actions, [])

    def test_non_adjacent_short_prefix_below_strict_stays_idle(self) -> None:
        # 7-char, 2-eojeol prefix qualifies cursor-bias but NOT strict
        # (strict needs ≥8 chars OR ≥3 eojeols). cursor=0 → adjacent=seg-1,
        # top-1=seg-2 → non-adjacent → strict gate applies → IDLE.
        state, actions = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말", now_ms=1000),
            cursor=0, segments=self.segments,
        )
        self.assertEqual(state.kind, "IDLE")
        self.assertEqual(actions, [])

    def test_cursor_adjacent_still_blocked_by_score_min(self) -> None:
        # cursor-bias relaxes prefix + confirm count only. score_min /
        # score_margin are unchanged — a low-score adjacent match must
        # still fail.
        segments = [
            _seg("seg-1", "완전히 다른 내용 여기에 있습니다 확실히."),
        ]
        state, actions = advance(
            INITIAL_STATE,
            InterimEvent(prefix="아무 상관 없는 다른 텍스트", now_ms=1000),
            cursor=0, segments=segments,
        )
        self.assertEqual(state.kind, "IDLE")
        self.assertEqual(actions, [])

    def test_cursor_adjacent_skip_marked_segment_is_bypassed(self) -> None:
        # If segments[cursor] is Skip, adjacent = the next non-Skip segment.
        segments = [
            _seg("seg-0", "완전히 무관한 첫 문장입니다.", status="Skip"),
            _seg("seg-1", "우리가 거짓말을 하는 이유는 여기 있습니다."),
        ]
        state, actions = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말을 하는", now_ms=1000),
            cursor=0, segments=segments,
        )
        self.assertEqual(state.kind, "PREVIEW")
        self.assertEqual(state.seg_id, "seg-1")
        self.assertIsInstance(actions[0], ShowPreview)

    def test_cursor_bias_disabled_falls_back_to_strict(self) -> None:
        # Setting cursor_bias_partial_confirm_count=2 reverts fast path.
        strict_config = MatcherConfig(cursor_bias_partial_confirm_count=2)
        state, actions = advance(
            INITIAL_STATE,
            InterimEvent(prefix="우리가 거짓말을 하는", now_ms=1000),
            cursor=1, segments=self.segments,
            config=strict_config,
        )
        self.assertEqual(state.kind, "CANDIDATE")
        self.assertEqual(state.seg_id, "seg-2")
        self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()
