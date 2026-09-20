"""Regression tests for the OpenAI stub's marker extraction.

The reviewer flagged F-25 failing because the stub's whole-prompt
regex returned the previous marker from the `Previous English
sentence:` line, not the fresh marker after `Current text:`. That
made the fresh-marker assertion time out on the A-side witness
BEFORE the isolation window was reached.

These tests exercise the fix directly against the pure helper —
no backend processes, no infra required. They run under the same
`resource_cleanup` conftest as the integration tests but do not
trip the infra-skip guard because they never touch Redis or
Firestore.
"""
from __future__ import annotations

from .harness.openai_stub import _extract_marker, _translate


PREVIOUS_MARKER = "baseline-ab12cd"
CURRENT_MARKER = "isolation-ef3456"


def _production_shaped_prompt(previous: str, current: str) -> str:
    """Mirrors the prompt shape produced by
    `app/utils/translate.py` (see ~L1568-1602)."""
    return (
        f"[some recent context lines]\n"
        f"Previous English sentence: [stub-translated] {previous}\n"
        f"IMPORTANT: The subject of this clause is \"speaker\". "
        f"Do NOT introduce a new subject.\n\n"
        f"Current text:\n"
        f"안녕하세요 {current}"
    )


def test_extract_marker_prefers_current_text_section() -> None:
    prompt = _production_shaped_prompt(PREVIOUS_MARKER, CURRENT_MARKER)
    assert _extract_marker(prompt) == CURRENT_MARKER


def test_translate_prefers_current_text_section() -> None:
    prompt = _production_shaped_prompt(PREVIOUS_MARKER, CURRENT_MARKER)
    assert _translate(prompt) == f"[stub-translated] {CURRENT_MARKER}"


def test_extract_marker_falls_back_to_whole_text_when_no_current_section() -> None:
    """The `else` branch in `_build_user_content` produces a prompt
    that is just the masked text, with no `Current text:` header.
    _extract_marker must still find the marker in that case."""
    prompt = f"안녕하세요 {CURRENT_MARKER}"
    assert _extract_marker(prompt) == CURRENT_MARKER


def test_extract_marker_case_insensitive_current_text_header() -> None:
    """Defensive: if the prompt template ever changes the case of
    the header, the stub should still route to the current-text
    body — not silently regress to whole-text matching."""
    prompt = (
        f"Previous English sentence: {PREVIOUS_MARKER}\n"
        f"CURRENT TEXT:\n"
        f"안녕 {CURRENT_MARKER}"
    )
    assert _extract_marker(prompt) == CURRENT_MARKER


def test_extract_marker_uses_last_current_text_when_multiple() -> None:
    """If the prompt somehow contains multiple `Current text:`
    sections, the LAST one is the operative one — that is the one
    the translation is about."""
    prompt = (
        f"Current text:\n(prior draft) {PREVIOUS_MARKER}\n"
        f"...\n"
        f"Current text:\n(fresh) {CURRENT_MARKER}"
    )
    assert _extract_marker(prompt) == CURRENT_MARKER


def test_extract_marker_empty_prompt() -> None:
    assert _extract_marker("") == ""
    assert _extract_marker(None) == ""  # type: ignore[arg-type]


def test_extract_marker_rejects_unshaped_prefix() -> None:
    """A prefix that doesn't fit the shape (lowercase, dashes only
    before the hex tail) shouldn't match."""
    assert _extract_marker("Current text:\n안녕 UPPER-abc123") == ""
    assert _extract_marker("Current text:\n안녕 no_underscore-abc123") == ""
