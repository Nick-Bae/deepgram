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

    def test_holds_auxiliary_fragment_that_would_translate_to_trailing_can(self) -> None:
        self.assertTrue(
            is_strongly_incomplete_korean_segment("죄 아래 무너진 사람이라면 우리를 붙들 수")
        )

    def test_holds_negative_auxiliary_fragment_that_would_translate_to_trailing_cannot(self) -> None:
        self.assertTrue(
            is_strongly_incomplete_korean_segment("그 사람은 우리를 붙들 수 없기")
        )

    def test_holds_negative_verb_stem_until_quoted_thought_arrives(self) -> None:
        self.assertTrue(
            is_strongly_incomplete_korean_segment("어쩌면 우리는 죽음을 두려워하지")
        )

    def test_joins_negative_verb_stem_with_quoted_thought(self) -> None:
        self.assertEqual(
            join_korean_stt_segments(
                "어쩌면 우리는 죽음을 두려워하지",
                "않는다고 생각할지 모릅니다.",
            ),
            "어쩌면 우리는 죽음을 두려워하지 않는다고 생각할지 모릅니다.",
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
