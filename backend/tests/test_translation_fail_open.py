from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.modules.setdefault("app.services.google_tts", types.SimpleNamespace(synthesize_async=None))
sys.modules.setdefault("app.services.gemini_flash_tts", types.SimpleNamespace(synthesize_async=None))

from app import main as main_module


class TranslationFailOpenTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_guard_retries_with_non_streaming_translation(self) -> None:
        async def failed_stream(*args, **kwargs):
            usage_out = kwargs.get("usage_out")
            if isinstance(usage_out, dict):
                usage_out.update(
                    {
                        "totalTokens": 0,
                        "failOpen": True,
                        "errorMessage": "stream timeout",
                    }
                )
            if False:
                yield ""

        emitted: list[str] = []
        with patch.object(
            main_module,
            "_reserve_translation_budget",
            return_value=([], None),
        ), patch.object(
            main_module,
            "_settle_translation_budget",
        ), patch.object(
            main_module,
            "translate_text_streaming",
            failed_stream,
        ), patch.object(
            main_module,
            "_translate_text_guarded",
            AsyncMock(return_value=("The Son of God became truly human.", None)),
        ):
            translated, meta = await main_module._translate_streaming_guarded(
                "그래서 하나님의 아들은 참 사람이 되셨습니다.",
                "ko-KR",
                "en-US",
                org_id="test-org",
                host_uid="test-host",
                ctx=None,
                on_token=lambda text: _append_async(emitted, text),
            )

        self.assertEqual(translated, "The Son of God became truly human.")
        self.assertIsNone(meta)
        self.assertEqual(emitted, ["The Son of God became truly human."])

    async def test_translate_text_guarded_holds_output_when_translator_fails_open(self) -> None:
        async def fake_translate_text(*args, **kwargs):
            usage_out = kwargs.get("usage_out")
            if isinstance(usage_out, dict):
                usage_out.update(
                    {
                        "promptTokens": 0,
                        "completionTokens": 0,
                        "totalTokens": 0,
                        "failOpen": True,
                        "errorMessage": "timeout",
                    }
                )
            return "큰 소리로 외치는 것을"

        with patch.object(main_module, "_reserve_translation_budget", return_value=([], None)), patch.object(
            main_module,
            "_settle_translation_budget",
        ), patch.object(
            main_module,
            "translate_text",
            AsyncMock(side_effect=fake_translate_text),
        ):
            translated, meta = await main_module._translate_text_guarded(
                "큰 소리로 외치는 것을",
                "ko-KR",
                "en-US",
                org_id="test-org",
                host_uid="test-host",
                ctx=None,
            )

        self.assertEqual(translated, "")
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertTrue(bool(meta.get("fail_open")))

    async def test_translate_text_guarded_holds_output_when_rate_limited(self) -> None:
        blocked = {
            "scope": "org",
            "kind": "requests",
            "used": 300,
            "limit": 300,
            "windowSeconds": 60,
            "estimatedTokens": 42,
        }
        with patch.object(main_module, "_reserve_translation_budget", return_value=([], blocked)), patch.object(
            main_module,
            "_settle_translation_budget",
        ):
            translated, meta = await main_module._translate_text_guarded(
                "큰 소리로 외치는 것을",
                "ko-KR",
                "en-US",
                org_id="test-org",
                host_uid="test-host",
                ctx=None,
            )

        self.assertEqual(translated, "")
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertTrue(bool(meta.get("fail_open")))
        self.assertEqual(meta.get("reason"), "translation_rate_limited")

    async def test_translate_text_guarded_holds_output_when_english_contains_korean(self) -> None:
        async def fake_translate_text(*args, **kwargs):
            usage_out = kwargs.get("usage_out")
            if isinstance(usage_out, dict):
                usage_out.update(
                    {
                        "promptTokens": 10,
                        "completionTokens": 4,
                        "totalTokens": 14,
                    }
                )
            return "God loves 우리."

        with patch.object(main_module, "_reserve_translation_budget", return_value=([], None)), patch.object(
            main_module,
            "_settle_translation_budget",
        ), patch.object(
            main_module,
            "translate_text",
            AsyncMock(side_effect=fake_translate_text),
        ):
            translated, meta = await main_module._translate_text_guarded(
                "하나님은 우리를 사랑하십니다",
                "ko-KR",
                "en-US",
                org_id="test-org",
                host_uid="test-host",
                ctx=None,
            )

        self.assertEqual(translated, "")
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertTrue(bool(meta.get("fail_open")))
        self.assertEqual(meta.get("reason"), "target_language_mismatch")

    async def test_translate_text_guarded_allows_clean_english(self) -> None:
        async def fake_translate_text(*args, **kwargs):
            usage_out = kwargs.get("usage_out")
            if isinstance(usage_out, dict):
                usage_out.update(
                    {
                        "promptTokens": 10,
                        "completionTokens": 4,
                        "totalTokens": 14,
                    }
                )
            return "God loves us."

        with patch.object(main_module, "_reserve_translation_budget", return_value=([], None)), patch.object(
            main_module,
            "_settle_translation_budget",
        ), patch.object(
            main_module,
            "translate_text",
            AsyncMock(side_effect=fake_translate_text),
        ):
            translated, meta = await main_module._translate_text_guarded(
                "하나님은 우리를 사랑하십니다",
                "ko-KR",
                "en-US",
                org_id="test-org",
                host_uid="test-host",
                ctx=None,
            )

        self.assertEqual(translated, "God loves us.")
        self.assertIsNone(meta)


if __name__ == "__main__":
    unittest.main()


async def _append_async(items: list[str], value: str) -> None:
    items.append(value)
