from __future__ import annotations

import unittest

from app.utils import translate as translate_module


class TranslationContextTests(unittest.TestCase):
    def test_openai_chat_options_omits_sampling_controls_for_gpt5_family(self) -> None:
        self.assertEqual(translate_module._openai_chat_options("gpt-5-mini"), {})
        self.assertEqual(translate_module._openai_chat_options("gpt-5"), {})

    def test_openai_chat_options_keeps_sampling_controls_for_4o_family(self) -> None:
        options = translate_module._openai_chat_options("gpt-4o")
        self.assertEqual(options.get("temperature"), 0.2)
        self.assertEqual(options.get("top_p"), 1.0)

    def test_translation_context_remember_keeps_recent_pairs_bounded(self) -> None:
        ctx = translate_module.TranslationContext()
        ctx.remember("첫 문장", "First sentence")
        ctx.remember("둘째 문장", "Second sentence")
        ctx.remember("셋째 문장", "Third sentence")
        ctx.remember("넷째 문장", "Fourth sentence")

        self.assertEqual(len(ctx.recent_pairs), 3)
        self.assertEqual(ctx.recent_pairs[0]["source"], "둘째 문장")
        self.assertEqual(ctx.last_english, "Fourth sentence")

    def test_build_recent_context_block_uses_recent_bilingual_history(self) -> None:
        ctx = translate_module.TranslationContext()
        ctx.remember("원문을 말하고", "It is talking about the original text.")
        ctx.remember("소리를 지르거나 하는 부분은", "The parts where they shout or make noise are loud.")

        block = translate_module._build_recent_context_block(
            ctx,
            current_source_text="큰 소리로 외치는 것을",
        )

        self.assertIn("Recent translated context:", block)
        self.assertIn("Korean: 원문을 말하고", block)
        self.assertIn("English: It is talking about the original text.", block)
        self.assertIn("Korean: 소리를 지르거나 하는 부분은", block)

    def test_needs_recent_context_detects_ambiguous_short_clause(self) -> None:
        self.assertTrue(translate_module._needs_recent_context("큰 소리로 외치는 것을"))
        self.assertFalse(translate_module._needs_recent_context("우리는 함께 기도합니다"))

    def test_preprocess_source_text_collapses_compacted_repeat_suffix(self) -> None:
        cleaned = translate_module._preprocess_source_text(
            "하나님이 맡기신 높은 자리 사명의 하나님이맡기신높은자리사명의자리,",
            "ko",
        )
        self.assertEqual(cleaned, "하나님이 맡기신 높은 자리 사명의 자리,")

    def test_preprocess_source_text_collapses_compacted_repeat_clause_tail(self) -> None:
        cleaned = translate_module._preprocess_source_text(
            "순종의 자리에서 내려가는 것을 거절하고 있는 순종의자리에서내려가는것을거절하고있는것입니다.",
            "ko",
        )
        self.assertEqual(cleaned, "순종의 자리에서 내려가는 것을 거절하고 있는 것입니다.")

    def test_should_include_prompt_context_only_for_previews_or_ambiguous_clauses(self) -> None:
        self.assertFalse(
            translate_module._should_include_prompt_context(
                "하나님이 맡기신 높은 자리와 사명의 자리에서 끝까지 순종해야 합니다",
                update_ctx=True,
            )
        )
        self.assertTrue(
            translate_module._should_include_prompt_context(
                "큰 소리로 외치는 것을",
                update_ctx=True,
            )
        )
        self.assertTrue(
            translate_module._should_include_prompt_context(
                "하나님이 맡기신 높은 자리와 사명의 자리에서 끝까지 순종해야 합니다",
                update_ctx=False,
            )
        )

    def test_mask_hard_glossary_does_not_mask_bare_genitive_marker(self) -> None:
        masked, mapping = translate_module._mask_hard_glossary("순종의 자리", "ko")
        self.assertEqual(masked, "순종의 자리")
        self.assertEqual(mapping, {})

    def test_enforce_subject_guardrails_keeps_explicit_they_over_we_history(self) -> None:
        ctx = translate_module.TranslationContext(subject="the congregation", pronoun="we")
        source = "더 놀라운 것은 그들이 이 초청을 한 번만 보낸 것이 아니라 네 번이나 반복했다는 사실입니다."
        raw = (
            "The amazing thing is that they did not send this invitation just once.\n"
            "The fact is that we repeated it four times."
        )

        guarded = translate_module._enforce_subject_guardrails(raw, source, ctx)
        guarded = translate_module._enforce_we_guardrails(guarded, source, ctx)

        self.assertIn("they repeated it four times", guarded.lower())
        self.assertNotIn("we repeated it four times", guarded.lower())
        self.assertEqual(ctx.pronoun, "they")
        self.assertEqual(ctx.subject, "the people being described")

    def test_context_for_prompt_overrides_we_history_for_explicit_they_clause(self) -> None:
        ctx = translate_module.TranslationContext(
            subject="the congregation",
            pronoun="we",
            last_english="Let us pray together.",
        )

        prompt_ctx = translate_module._context_for_prompt(
            ctx,
            "더 놀라운 것은 그들이 이 초청을 한 번만 보낸 것이 아니라 네 번이나 반복했다는 사실입니다.",
        )

        self.assertIsNotNone(prompt_ctx)
        assert prompt_ctx is not None
        self.assertEqual(prompt_ctx.pronoun, "they")
        self.assertEqual(prompt_ctx.subject, "the people being described")

    def test_normalize_english_pronoun_case_uppercases_standalone_i(self) -> None:
        normalized = translate_module._normalize_english_pronoun_case(
            "It is a word of faith that i hold dear. i am grateful, and i’ll keep it."
        )
        self.assertEqual(
            normalized,
            "It is a word of faith that I hold dear. I am grateful, and I’ll keep it.",
        )

    def test_infer_subject_from_english_keeps_this_god_as_he(self) -> None:
        subject, pronoun = translate_module._infer_subject_from_english(
            "This God here is the One who made the heavens and the earth.",
            "the congregation",
            "we",
        )
        self.assertEqual(subject, "This God here is the One who made the heavens and the earth")
        self.assertEqual(pronoun, "he")

    def test_infer_subject_from_context_history_uses_noun_led_prior_subject(self) -> None:
        ctx = translate_module.TranslationContext(subject="the congregation", pronoun="we")
        ctx.remember("이 하나님은 하늘과 땅을 지으신 분입니다.", "This God here is the One who made the heavens and the earth.")

        subject, pronoun = translate_module._infer_subject_from_context_history(ctx)

        self.assertEqual(subject, "This God here is the One who made the heavens and the earth")
        self.assertEqual(pronoun, "he")

    def test_enforce_subject_guardrails_keeps_divine_third_person_without_honorific(self) -> None:
        ctx = translate_module.TranslationContext(
            subject="This God here is the One who made the heavens and the earth",
            pronoun="he",
        )

        guarded = translate_module._enforce_subject_guardrails(
            "Even now, I do not ignore the groaning of my people.",
            "지금도 자기 백성의 신음을 모른 척하지",
            ctx,
        )

        self.assertEqual(
            guarded,
            "Even now, he does not ignore the groaning of his people.",
        )


if __name__ == "__main__":
    unittest.main()
