from __future__ import annotations

import unittest

from app.utils.korean_segments import (
    ends_with_standalone_da,
    is_strongly_incomplete_korean_segment,
    join_korean_stt_segments,
    split_sermon_korean_text,
)


class KoreanSegmentTests(unittest.TestCase):
    def test_detects_standalone_da_stt_fragment(self) -> None:
        self.assertTrue(ends_with_standalone_da("자자 빨리 사자 다"))
        self.assertTrue(ends_with_standalone_da("자자 빨리 사자 다."))

    def test_formal_da_ending_is_not_standalone_da(self) -> None:
        self.assertFalse(ends_with_standalone_da("늘 중요한 일이 있어서 일찍 가야 합니다"))

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

    def test_rhetorical_question_hanun_ga_is_complete(self) -> None:
        # Final 가 in 하는가 is an interrogative ending, not the subject particle.
        self.assertFalse(
            is_strongly_incomplete_korean_segment("내가 무엇을 가장 의지하는가")
        )
        self.assertFalse(
            is_strongly_incomplete_korean_segment("내가 무엇을 가장 의지하는가?")
        )

    def test_rhetorical_question_neukkinun_ga_is_complete(self) -> None:
        self.assertFalse(
            is_strongly_incomplete_korean_segment(
                "무엇이 있어야 나는 안전하다고 느끼는가"
            )
        )
        self.assertFalse(
            is_strongly_incomplete_korean_segment(
                "무엇이 흔들리면 내 인생도 함께 무너진 것처럼 느끼는가?"
            )
        )

    def test_copular_question_in_ga_is_complete(self) -> None:
        # -인가 / -은가 / -한가 / -ㄴ가 endings must not be flagged as dangling 가.
        self.assertFalse(is_strongly_incomplete_korean_segment("이것이 무엇인가"))
        self.assertFalse(is_strongly_incomplete_korean_segment("이것이 무엇인가?"))
        self.assertFalse(is_strongly_incomplete_korean_segment("우리는 가난한가"))

    def test_deliberative_question_ha_lkka_is_complete(self) -> None:
        # -할까 / -을까 / -ㄹ까 endings are deliberative sentences, not fragments.
        self.assertFalse(is_strongly_incomplete_korean_segment("우리는 어디로 갈까"))
        self.assertFalse(is_strongly_incomplete_korean_segment("우리는 어디로 갈까?"))
        self.assertFalse(is_strongly_incomplete_korean_segment("이제 어떻게 할까"))

    def test_trailing_comma_is_incomplete_mid_list_fragment(self) -> None:
        # STT fragments ending with a comma are almost always mid-list
        # ("내 집, 내 시간, 내 돈,") — holding lets the next Deepgram segment
        # join instead of the fragment getting its own live translation and
        # duplicating the reviewed match once the full sentence lands.
        self.assertTrue(
            is_strongly_incomplete_korean_segment("내 집, 내 시간, 내 돈,")
        )
        # Full-width Korean comma also counts.
        self.assertTrue(
            is_strongly_incomplete_korean_segment("어떤 사람은，")
        )
        # A comma followed by a completed clause should still be complete.
        self.assertFalse(
            is_strongly_incomplete_korean_segment(
                "내 집, 내 시간, 내 돈은 모두 하나님의 것입니다."
            )
        )

    def test_connective_myeo_remains_incomplete(self) -> None:
        # The 는가 fix must not regress the -며/-고 connective hold behavior.
        self.assertTrue(is_strongly_incomplete_korean_segment("마음이 청결하며"))
        self.assertTrue(is_strongly_incomplete_korean_segment("하나님을 사랑하며"))
        self.assertTrue(is_strongly_incomplete_korean_segment("하나님을 사랑하고"))

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

    def test_splits_scripture_quote_from_sermon_explanation(self) -> None:
        text = (
            "“땅과 거기 충만한 것과 세계와 그 가운데 사는 자들은 "
            "다 여호와의 것이로다.” 성도 여러분, 시편 24편은 질문으로 "
            "시작하지 않습니다."
        )

        self.assertEqual(
            split_sermon_korean_text(text),
            [
                "“땅과 거기 충만한 것과 세계와 그 가운데 사는 자들은 "
                "다 여호와의 것이로다.”",
                "성도 여러분, 시편 24편은 질문으로 시작하지 않습니다.",
            ],
        )

    def test_splits_long_parallel_sermon_clause_at_breath_points(self) -> None:
        text = (
            "나를 위해 자신을 내어 주시고, 나를 버려 두지 않고 찾아오시며, "
            "내가 들어갈 수 없던 문 안으로 나를 데리고 들어가시는 "
            "왕이시기 때문입니다."
        )

        self.assertEqual(
            split_sermon_korean_text(text),
            [
                "나를 위해 자신을 내어 주시고,",
                "나를 버려 두지 않고 찾아오시며,",
                "내가 들어갈 수 없던 문 안으로 나를 데리고 들어가시는 "
                "왕이시기 때문입니다.",
            ],
        )

    def test_splits_stage_direction_from_sermon_text(self) -> None:
        self.assertEqual(
            split_sermon_korean_text("여기서 우리는 멈춰야 합니다. (잠시 멈춤) 다음을 보겠습니다."),
            [
                "여기서 우리는 멈춰야 합니다.",
                "(잠시 멈춤)",
                "다음을 보겠습니다.",
            ],
        )
