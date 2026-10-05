"""Regression guard: TX_LOG stdout failure (OSError) is still contained.

An EPIPE on the TX_LOG print must NOT alter translation output, exception
propagation, or the file write that precedes it. The underlying JSONL file
should contain the appended record even when the TX_LOG print fails.

Note: the fewshot in-memory cache (`_FEWSHOT_ROWS_CACHE`) is NOT updated by
`_log_translation_example` on this branch (that behavior is from Phase 1 on
`investigation/cpu-pin-rc@94833263`, which is a separate perf-fix scope).
This test verifies the file-write side effect instead.
"""
from __future__ import annotations

import builtins
import json
import pathlib
import sys
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


@pytest.fixture
def scratch_log(monkeypatch):
    from app.utils import translate as tx_mod
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="tx_log_cont_epipe_"))
    scratch = tmpdir / "translation_examples.jsonl"
    scratch.touch()
    monkeypatch.setattr(tx_mod, "_TRANSLATION_LOG_PATH", scratch)
    monkeypatch.setattr(tx_mod, "_FEWSHOT_ROWS_CACHE", {"mtime": None, "rows": []})
    yield scratch
    try:
        scratch.unlink(missing_ok=True)
        tmpdir.rmdir()
    except OSError:
        pass


def test_tx_log_contains_oserror_epipe(scratch_log, monkeypatch):
    from app.utils import translate as tx_mod

    real_print = builtins.print

    def flaky_print(*args, **kwargs):
        if args and isinstance(args[0], str) and args[0].startswith("[TX_LOG]"):
            raise OSError(32, "Broken pipe")
        return real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", flaky_print)

    # Must NOT raise.
    tx_mod._log_translation_example(
        source_lang="ko",
        target_lang="en",
        stt_text="dummy source",
        auto_translation="dummy translation",
        final_translation="dummy final",
    )

    # File write must still have happened.
    text = scratch_log.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one appended record, got: {text!r}"
    record = json.loads(lines[0])
    assert record["stt_text"] == "dummy source"
    assert record["auto_translation"] == "dummy translation"
