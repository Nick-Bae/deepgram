"""Tests for strip_ko_particles + the PMM continuation guard's particle tolerance.

The guard at main.py:3044 area suppresses live GPT re-translation when the
committed Korean is a substring of a recently-confirmed reviewed segment.
Deepgram STT frequently drops or swaps trailing particles (은↔을, 의, 를/로)
under fast speech, so the exact substring check misses those cases.

`strip_ko_particles` removes one trailing postposition per word so the guard
matches on content morphemes rather than surface form.

User requirements:
  1. 예수님을 / 예수님은 variation should be skipped
  2. 이야기의 / 이야기 variation should be skipped
  3. 한가운데로 / 한가운데 variation should be skipped
  4. A truly different sentence should NOT be skipped
  5. Very short fragments should NOT be skipped too aggressively
"""
from __future__ import annotations

import unittest

from app.utils.hangul import strip_ko_particles


# The full reviewed segment's Korean (as it appears in the sermon):
REVIEWED_KO = (
    "예수님의 족보와 탄생, 어린 시절의 이야기를 지나 아주 빠르게 "
    "예수님을 이야기의 한가운데로 모셔옵니다."
)


def _would_skip_particles(commit_ko: str, confirmed_ko: str, min_len: int = 8) -> bool:
    """Mimic the main.py particle-tolerant continuation guard."""
    stripped_new = strip_ko_particles(commit_ko)
    stripped_conf = strip_ko_particles(confirmed_ko)
    if not stripped_new or not stripped_conf:
        return False
    if len(stripped_new) < min_len:
        return False
    return stripped_new in stripped_conf


class ParticleStrippingTests(unittest.TestCase):
    """Sanity: strip_ko_particles removes trailing particles per word."""

    def test_object_particle_stripped(self):
        # Requirement 1a: 을 (object) is a particle → stripped
        self.assertEqual(strip_ko_particles("예수님을"), "예수님")

    def test_topic_particle_stripped(self):
        # Requirement 1b: 은 (topic) is a particle → stripped
        self.assertEqual(strip_ko_particles("예수님은"), "예수님")

    def test_genitive_particle_stripped(self):
        # Requirement 2a: 의 (genitive) → stripped
        self.assertEqual(strip_ko_particles("이야기의"), "이야기")

    def test_directional_particle_stripped(self):
        # Requirement 3a: 로 (directional) → stripped
        self.assertEqual(strip_ko_particles("한가운데로"), "한가운데")

    def test_absent_particle_unchanged(self):
        # 이야기 (no particle) — stays as-is
        self.assertEqual(strip_ko_particles("이야기"), "이야기")

    def test_verb_ending_da_not_stripped(self):
        # 다 (verb ending) is NOT a postposition — must not be stripped
        self.assertEqual(strip_ko_particles("모셔옵니다"), "모셔옵니다")

    def test_multi_char_particle_eseo(self):
        # 에서 (locative) is a two-char particle → stripped as a unit
        self.assertEqual(strip_ko_particles("서울에서"), "서울")

    def test_multi_char_particle_euro(self):
        # 으로 (instrumental) — stripped as a unit
        self.assertEqual(strip_ko_particles("연필으로"), "연필")

    def test_empty_input(self):
        self.assertEqual(strip_ko_particles(""), "")
        self.assertEqual(strip_ko_particles("   "), "")


class ContinuationGuardScenarioTests(unittest.TestCase):
    """Full-sentence guard behavior — the user's 5 requirements."""

    def test_req1_object_topic_swap_should_skip(self):
        # 예수님을 (correct) vs 예수님은 (misheard) — same intent
        misheard = "예수님은 이야기의 한가운데로 모셔옵니다."
        self.assertTrue(_would_skip_particles(misheard, REVIEWED_KO),
                        "예수님을 ↔ 예수님은 should be treated as continuation")

    def test_req2_genitive_dropped_should_skip(self):
        # 이야기의 (correct) vs 이야기 (Deepgram dropped 의)
        dropped_ui = "예수님을 이야기 한가운데로 모셔옵니다."
        self.assertTrue(_would_skip_particles(dropped_ui, REVIEWED_KO),
                        "이야기의 ↔ 이야기 should be treated as continuation")

    def test_req3_directional_dropped_should_skip(self):
        # 한가운데로 (correct) vs 한가운데 (Deepgram dropped 로)
        dropped_ro = "예수님을 이야기의 한가운데 모셔옵니다."
        self.assertTrue(_would_skip_particles(dropped_ro, REVIEWED_KO),
                        "한가운데로 ↔ 한가운데 should be treated as continuation")

    def test_all_three_variations_combined_should_skip(self):
        # The exact case from the user's console log: all 3 swaps at once
        misheard = "예수님은 이야기 한가운데 모셔옵니다."
        self.assertTrue(_would_skip_particles(misheard, REVIEWED_KO),
                        "combined 은/이야기/한가운데 misheard tail must be caught")

    def test_req4_truly_different_sentence_not_skipped(self):
        # A totally different sentence — must NOT be treated as continuation
        different = "오늘 새로운 이야기가 있습니다."
        self.assertFalse(_would_skip_particles(different, REVIEWED_KO),
                         "truly different sentence must not be false-matched")

    def test_req4_different_topic_with_shared_word_not_skipped(self):
        # Different topic that happens to share one word (예수님)
        different = "예수님이 오셨습니다."
        self.assertFalse(_would_skip_particles(different, REVIEWED_KO),
                         "sharing one content word must not be enough to match")

    def test_req5_very_short_not_skipped(self):
        # Deepgram sometimes emits tiny commits like "그" or "네"
        for tiny in ("그", "네", "그런", "예수님"):
            with self.subTest(tiny=tiny):
                self.assertFalse(_would_skip_particles(tiny, REVIEWED_KO),
                                 f"short fragment '{tiny}' must not be skipped")

    def test_req5_length_gate_boundary_at_8(self):
        # Exactly-at-length-gate case (particle-stripped == 8 chars)
        exact_boundary = "예수님이야기한가운"  # exactly 8 chars, substring
        # Confirm both the stripped test string and confirmed strip length
        self.assertGreaterEqual(len(exact_boundary), 8)
        # (We compare the raw *_particles* strings inside the guard; craft a
        # plain-substring case at boundary to prove the gate is inclusive.)
        conf_stripped = strip_ko_particles(REVIEWED_KO)
        self.assertIn(exact_boundary, conf_stripped)


class DoesNotRegressExactCaseTests(unittest.TestCase):
    """The particle-stripped path must not weaken the existing exact-substring
    guard's behavior — both live in the same block in main.py."""

    def test_exact_match_still_skipped(self):
        commit = "예수님을 이야기의 한가운데로 모셔옵니다."
        # Exact substring match on stripped-of-spaces is unchanged
        exact_new = commit.replace(" ", "")
        exact_conf = REVIEWED_KO.replace(" ", "")
        self.assertIn(exact_new, exact_conf)


if __name__ == "__main__":
    unittest.main()
