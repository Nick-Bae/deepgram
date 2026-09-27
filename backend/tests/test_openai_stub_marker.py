"""Pure-function regression tests for the OpenAI stub's marker
extraction.

Positioned at the top level of `backend/tests/` (NOT under
`integration/resource_cleanup/`) so it runs even when the integration
infra is unavailable. The `resource_cleanup/conftest.py` applies a
skip marker to every test file under that directory when Redis or
Firestore is missing; that guard exists to keep infra-dependent
integration tests from failing on a stripped dev box, but it would
also skip these pure tests. Keeping them here lets both the harness
CI job AND a plain `pytest backend/tests/` run pick them up.

Coverage of the extraction rules:
  1. Production prompt (Previous English sentence + Current text)
     returns the CURRENT marker, not the previous one — this was
     the F-25 regression that hid under a whole-prompt regex.
  2. Case-insensitive `Current text:` header still routes to the
     body of that section.
  3. Multiple `Current text:` sections use the LAST one.
  4. Fallback: when the prompt has no `Current text:` header, the
     whole-text search still finds the marker.
  5. Boundary lookarounds reject tokens that only look plausible
     because of a valid-looking suffix (`no_underscore-abc123`).
  6. Uppercase prefixes are rejected (production markers are all
     lowercase-with-dashes plus optional digits).
  7. Empty and None inputs are safe.
"""
from __future__ import annotations

import os
import sys

# The stub lives under an intentional-skip directory; import it via
# an explicit sys.path insertion so this test file itself sits
# OUTSIDE that directory and its conftest.
_HARNESS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "integration", "resource_cleanup", "harness",
)
if _HARNESS_DIR not in sys.path:
    sys.path.insert(0, _HARNESS_DIR)

from openai_stub import _extract_marker, _translate  # noqa: E402


PREVIOUS_MARKER = "baseline-ab12cd"
CURRENT_MARKER = "isolation-ef3456"


def _production_shaped_prompt(previous: str, current: str) -> str:
    """Mirrors the prompt shape produced by
    `app/utils/translate.py` around L1568-1602."""
    return (
        f"[some recent context lines]\n"
        f"Previous English sentence: [stub-translated] {previous}\n"
        f"IMPORTANT: The subject of this clause is \"speaker\". "
        f"Do NOT introduce a new subject.\n\n"
        f"Current text:\n"
        f"안녕하세요 {current}"
    )


def test_extract_marker_prefers_current_text_section() -> None:
    assert _extract_marker(_production_shaped_prompt(PREVIOUS_MARKER, CURRENT_MARKER)) == CURRENT_MARKER


def test_translate_prefers_current_text_section() -> None:
    prompt = _production_shaped_prompt(PREVIOUS_MARKER, CURRENT_MARKER)
    assert _translate(prompt) == f"[stub-translated] {CURRENT_MARKER}"


def test_extract_marker_falls_back_to_whole_text_when_no_current_section() -> None:
    """`_build_user_content`'s else branch produces a bare
    `masked_text` with no `Current text:` header."""
    assert _extract_marker(f"안녕하세요 {CURRENT_MARKER}") == CURRENT_MARKER


def test_extract_marker_case_insensitive_current_text_header() -> None:
    prompt = (
        f"Previous English sentence: {PREVIOUS_MARKER}\n"
        f"CURRENT TEXT:\n"
        f"안녕 {CURRENT_MARKER}"
    )
    assert _extract_marker(prompt) == CURRENT_MARKER


def test_extract_marker_uses_last_current_text_when_multiple() -> None:
    prompt = (
        f"Current text:\n(prior draft) {PREVIOUS_MARKER}\n"
        f"...\n"
        f"Current text:\n(fresh) {CURRENT_MARKER}"
    )
    assert _extract_marker(prompt) == CURRENT_MARKER


def test_extract_marker_accepts_digits_in_prefix() -> None:
    """`gate2-rollout-*` uses a digit inside the prefix — this is a
    real production identifier shape, not a hex-tail intrusion."""
    assert _extract_marker("Current text:\n안녕 gate2-rollout-ab34") == "gate2-rollout-ab34"


def test_extract_marker_rejects_uppercase_prefix() -> None:
    assert _extract_marker("Current text:\n안녕 UPPER-abc123") == ""


def test_extract_marker_rejects_underscore_in_prefix() -> None:
    """Reviewer's specific regression: the earlier regex returned
    `underscore-abc123` from inside `no_underscore-abc123`, letting
    an invalid token slip through with a valid-looking suffix."""
    assert _extract_marker("Current text:\n안녕 no_underscore-abc123") == ""


def test_extract_marker_empty_and_none_inputs() -> None:
    assert _extract_marker("") == ""
    assert _extract_marker(None) == ""  # type: ignore[arg-type]
