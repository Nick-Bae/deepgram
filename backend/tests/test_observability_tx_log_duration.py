"""Failing-before test — scope: prep/observability.

Asserts `_log_translation_example` prints a `[TX_LOG] write_ms=<float>` marker
on every call. Fails on `d7473ed7` because the current implementation does not
emit this marker.
"""
from __future__ import annotations

import pathlib
import re
import sys
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


_PATTERN = re.compile(r"\[TX_LOG\]\s+write_ms=([0-9]+(?:\.[0-9]+)?)")


def test_log_translation_example_prints_write_ms(capsys, monkeypatch):
    from app.utils import translate as tx_mod

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="tx_log_"))
    scratch = tmp / "translation_examples.jsonl"
    scratch.touch()
    monkeypatch.setattr(tx_mod, "_TRANSLATION_LOG_PATH", scratch)

    tx_mod._log_translation_example(
        source_lang="ko",
        target_lang="en",
        stt_text="hello",
        auto_translation="hello",
        final_translation="hello",
    )

    captured = capsys.readouterr().out
    match = _PATTERN.search(captured)
    assert match, (
        "expected '[TX_LOG] write_ms=<float>' marker in stdout; "
        f"got: {captured!r}"
    )
    duration = float(match.group(1))
    assert duration >= 0.0
    assert duration < 10_000.0, "write_ms sanity ceiling (10s) exceeded"
