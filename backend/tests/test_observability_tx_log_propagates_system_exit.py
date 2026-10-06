"""Follow-up remediation: TX_LOG print must NOT swallow SystemExit.

The current `_log_translation_example` wraps the `[TX_LOG] write_ms=` print
in `except BaseException`, which incorrectly swallows SystemExit. The follow-
up narrows the catch to `except Exception` so process-control exceptions
propagate as intended.

This test monkeypatches `builtins.print` so only `[TX_LOG] ...` lines raise
SystemExit, calls `_log_translation_example(...)`, and asserts the exception
escapes (fails on b947444d, passes on follow-up).
"""
from __future__ import annotations

import builtins
import pathlib
import sys
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


@pytest.fixture
def scratch_log(monkeypatch):
    from app.utils import translate as tx_mod
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="tx_log_prop_sysexit_"))
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


def test_tx_log_propagates_system_exit(scratch_log, monkeypatch):
    from app.utils import translate as tx_mod

    real_print = builtins.print

    def flaky_print(*args, **kwargs):
        if args and isinstance(args[0], str) and args[0].startswith("[TX_LOG]"):
            raise SystemExit("test: TX_LOG stdout failure")
        return real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", flaky_print)

    with pytest.raises(SystemExit):
        tx_mod._log_translation_example(
            source_lang="ko",
            target_lang="en",
            stt_text="dummy source",
            auto_translation="dummy translation",
            final_translation="dummy final",
        )
