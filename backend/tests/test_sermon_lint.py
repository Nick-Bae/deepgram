from __future__ import annotations

import unittest

from app.sermon_review.lint import lint_sermon_segments


def _seg(sid: str, ko: str) -> dict[str, str]:
    return {"segmentId": sid, "original": ko}


class SermonLintTests(unittest.TestCase):
    def test_detects_scripture_quoted_at_full_and_callback(self) -> None:
        # Real Genesis-17 pattern that caused the S12→S78 cursor jump.
        segments = [
            _seg("S001", "성도 여러분, 우리는 하나님을 믿는다고 고백합니다."),
            _seg(
                "S015",
                "“아브람이 구십구 세 때에 여호와께서 아브람에게 나타나서 "
                "그에게 이르시되 나는 전능한 하나님이라 너는 내 앞에서 "
                "행하여 완전하라.”",
            ),
            _seg("S051", "“나는 전능한 하나님이라.”"),
            _seg("S078", "“나는 전능한 하나님이라.”"),
        ]

        report = lint_sermon_segments(segments)
        self.assertEqual(report["totalSegments"], 4)
        # Expect at least the S015↔S078 pair (biggest gap).
        pairs = {(c["shorterSegmentId"], c["longerSegmentId"]) for c in report["collisions"]}
        self.assertIn(("S051", "S015"), pairs)
        self.assertIn(("S078", "S015"), pairs)

    def test_flags_identical_duplicates_at_large_gap(self) -> None:
        segments = [
            _seg("S001", "우리는 하나님을 믿는다고 고백합니다."),
            _seg("S268", "“내가 너의 하나님이 되겠다.”"),
            _seg("S298", "“내가 너의 하나님이 되겠다.”"),
        ]
        report = lint_sermon_segments(segments)
        pairs = {(c["shorterSegmentId"], c["longerSegmentId"]) for c in report["collisions"]}
        # Either order is fine when they're identical length — but the pair
        # must appear.
        self.assertTrue(
            ("S268", "S298") in pairs or ("S298", "S268") in pairs,
            f"expected S268↔S298 pair, got {pairs}",
        )

    def test_ignores_adjacent_repetition(self) -> None:
        # A call-and-response ("아멘!" x3) shouldn't fire the linter — the
        # cursor won't drift far enough to lose PMM even if fuzzy matches
        # all three.
        segments = [
            _seg("S010", "그러므로 우리는 그분께 나아갑니다."),
            _seg("S011", "그러므로 우리는 그분께 나아갑니다."),
            _seg("S012", "그러므로 우리는 그분께 나아갑니다."),
        ]
        report = lint_sermon_segments(segments)
        self.assertEqual(report["collisions"], [])

    def test_ignores_short_common_phrases(self) -> None:
        # '하나님' by itself repeats endlessly in any sermon — must not
        # generate collisions.
        segments = [
            _seg("S001", "하나님."),
            _seg("S050", "하나님."),
            _seg("S200", "하나님."),
        ]
        report = lint_sermon_segments(segments)
        self.assertEqual(report["collisions"], [])

    def test_ranks_bigger_gaps_first(self) -> None:
        segments = [
            _seg("S001", "성도 여러분, 오늘 본문을 함께 읽겠습니다."),
            _seg("S040", "성도 여러분, 오늘 본문을 함께 읽겠습니다."),
            _seg("S200", "성도 여러분, 오늘 본문을 함께 읽겠습니다."),
        ]
        report = lint_sermon_segments(segments)
        # First collision should have the biggest gap (S001↔S200 = 199 > S040↔S200 = 160 > S001↔S040 = 39).
        self.assertGreaterEqual(len(report["collisions"]), 1)
        self.assertEqual(report["collisions"][0]["gap"], 199)

    def test_handles_empty_and_missing_segments(self) -> None:
        # Robust to Firestore returning partial data.
        segments = [
            {"segmentId": "S001", "original": "정상적인 문장입니다."},
            {"segmentId": "", "original": "잘못된 세그먼트."},
            {"segmentId": "S002"},  # missing original
            {"original": "no segment id"},
        ]
        report = lint_sermon_segments(segments)
        self.assertEqual(report["totalSegments"], 1)
        self.assertEqual(report["collisions"], [])


if __name__ == "__main__":
    unittest.main()
