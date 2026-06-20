from __future__ import annotations

import unittest

from app.utils.korean_segments import (
    is_strongly_incomplete_korean_segment,
    join_korean_stt_segments,
)


class KoreanSegmentTests(unittest.TestCase):
    def test_holds_location_particle_until_predicate_arrives(self) -> None:
        self.assertTrue(
            is_strongly_incomplete_korean_segment("예수님은 내가 설 수 없는 자리에")
        )

    def test_complete_formal_sentence_is_not_held(self) -> None:
        self.assertFalse(
            is_strongly_incomplete_korean_segment(
                "예수님은 내가 설 수 없는 자리에 서시는 분입니다."
            )
        )

    def test_joins_incomplete_segment_with_following_predicate(self) -> None:
        self.assertEqual(
            join_korean_stt_segments(
                "예수님은 내가 설 수 없는 자리에",
                "서시는 분입니다.",
            ),
            "예수님은 내가 설 수 없는 자리에 서시는 분입니다.",
        )

    def test_does_not_duplicate_cumulative_deepgram_segment(self) -> None:
        self.assertEqual(
            join_korean_stt_segments(
                "예수님은 내가 설 수 없는 자리에",
                "예수님은 내가 설 수 없는 자리에 서시는 분입니다.",
            ),
            "예수님은 내가 설 수 없는 자리에 서시는 분입니다.",
        )
