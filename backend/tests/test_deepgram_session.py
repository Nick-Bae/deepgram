from __future__ import annotations

from urllib.parse import parse_qs
import unittest
from unittest.mock import patch

from app import deepgram_session


class DeepgramSessionTests(unittest.TestCase):
    def test_nova3_keyterms_are_sanitized_and_bounded(self) -> None:
        query = deepgram_session._qs(
            "nova-3",
            "ko",
            48000,
            ["좋은 단어", "boosted:3", "bad:delimiter", "line\nbreak", "x" * 120],
            500,
            600,
        )

        params = parse_qs(query)
        self.assertIn("좋은 단어", params["keyterm"])
        self.assertIn("boosted", params["keyterm"])
        self.assertNotIn("bad", params["keyterm"])
        self.assertIn("line break", params["keyterm"])
        self.assertTrue(all(len(term) <= 100 for term in params["keyterm"]))

    def test_replacements_skip_ambiguous_find_values(self) -> None:
        pairs = deepgram_session._build_replace_list(
            [
                ("valid", "Valid"),
                ("bad:delimiter", "Bad"),
                ("line\nbreak", "Line Break"),
            ],
            limit=20,
        )

        self.assertIn(("valid", "Valid"), pairs)
        self.assertIn(("line break", "Line Break"), pairs)
        self.assertNotIn(("bad:delimiter", "Bad"), pairs)

    def test_query_respects_url_budget(self) -> None:
        original_budget = deepgram_session.DG_MAX_URL_CHARS
        try:
            deepgram_session.DG_MAX_URL_CHARS = 2200
            query = deepgram_session._qs(
                "nova-3",
                "ko",
                48000,
                [f"긴용어{i}{'가' * 50}" for i in range(100)],
                500,
                600,
                [(f"find{i}", "replacement") for i in range(50)],
            )
        finally:
            deepgram_session.DG_MAX_URL_CHARS = original_budget

        self.assertLessEqual(
            len(f"{deepgram_session.DG_ENDPOINT}?{query}"),
            2200,
        )

    def test_utterance_end_env_rejects_values_below_deepgram_floor(self) -> None:
        with patch.dict("os.environ", {"DG_UTTER_END_MS": "600"}):
            self.assertEqual(
                deepgram_session._int_env(
                    "DG_UTTER_END_MS",
                    1000,
                    min_value=1000,
                    max_value=6000,
                ),
                1000,
            )


if __name__ == "__main__":
    unittest.main()
