"""TX_LOG print failure must not propagate — review defect #3 remediation.

`_log_translation_example` writes a JSONL record and then prints a
`[TX_LOG] write_ms=...` marker. On pre-remediation the marker lived in a
`finally:` block without a surrounding try/except, so a stdout EPIPE (or any
`print` exception) would escape the function and alter translation failure
handling.

This test monkeypatches `print` so the TX_LOG marker raises, calls the
function, and asserts:

- `_log_translation_example` returns normally (no exception propagated).
- The JSONL file write still happens (observability must not alter
  translation output semantics).
"""
from __future__ import annotations

import builtins
import json
import pathlib
import sys
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def test_tx_log_stdout_epipe_does_not_crash_log_fn(monkeypatch, tmp_path):
    try:
        from app.utils import translate as tx_mod
    except ImportError as exc:
        pytest.fail(f"translate module missing: {exc}")

    scratch = tmp_path / "translation_examples.jsonl"
    monkeypatch.setattr(tx_mod, "_TRANSLATION_LOG_PATH", scratch)

    # Patch print so it ALWAYS raises on the TX_LOG marker (and anything else
    # that prints after the file write completes). The file write itself uses
    # fh.write, not print, so it is unaffected.
    original_print = builtins.print

    def raising_print(*args, **kwargs):
        raise OSError(32, "Broken pipe")  # EPIPE

    monkeypatch.setattr(builtins, "print", raising_print)

    # Call must NOT raise.
    tx_mod._log_translation_example(
        source_lang="ko",
        target_lang="en",
        stt_text="테스트",
        auto_translation="test",
        final_translation=None,
    )

    # Restore print for the file-content assertion.
    monkeypatch.setattr(builtins, "print", original_print)

    # The file write must have happened despite the TX_LOG print failure.
    assert scratch.exists(), "translation_examples.jsonl was not created"
    content = scratch.read_text(encoding="utf-8").strip().splitlines()
    assert content, f"no rows written to {scratch}"
    row = json.loads(content[-1])
    assert row.get("stt_text") == "테스트"
    assert row.get("auto_translation") == "test"
