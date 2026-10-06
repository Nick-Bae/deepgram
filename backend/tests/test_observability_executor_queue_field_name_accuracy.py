"""Guard the executor-queue emission's field-name accuracy per defect 3.

The directive is explicit:
- The emitted `executor_queue` event MUST expose `worker_threads_total`.
- The event MUST NOT expose the misleading `active_workers` name.
- The SCOPE.md documentation MUST reference the current field name.

The rename is purely cosmetic / documentation accuracy; the measured
value does not change. These assertions pin the field name against future
regressions.
"""
from __future__ import annotations

import json
import pathlib
import sys
from io import StringIO
from unittest.mock import patch

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _parse_emission(stdout: str) -> dict:
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("event") == "executor_queue":
            return d
    raise AssertionError("no executor_queue event emitted")


def test_emitted_field_is_worker_threads_total_not_active_workers():
    """The event payload must contain `worker_threads_total` and must NOT
    contain the stale `active_workers` key."""
    from app.observability import executor_queue as eq

    class _StubExec:
        _threads = ("t1", "t2")
        _max_workers = 4
        class _Q:
            @staticmethod
            def qsize() -> int:
                return 0
        _work_queue = _Q()

    class _StubLoop:
        _default_executor = _StubExec()

    buf = StringIO()
    with patch("sys.stdout", buf), patch(
        "app.observability.executor_queue.asyncio.get_running_loop",
        return_value=_StubLoop(),
    ):
        eq._executor_queue_tick()

    d = _parse_emission(buf.getvalue())
    assert "worker_threads_total" in d, (
        f"expected `worker_threads_total` field in emission, got keys={sorted(d)}"
    )
    assert "active_workers" not in d, (
        f"`active_workers` field must not be present (renamed per defect 3); "
        f"got {d}"
    )
    assert d["worker_threads_total"] == 2


def test_executor_queue_py_source_mentions_rename_rationale():
    """The emission site must carry a comment explaining what
    `worker_threads_total` measures (and what it does NOT measure).
    This guards against future regressions that silently restore the
    misleading `active_workers` name without the explanatory context."""
    source_path = pathlib.Path(
        __file__
    ).resolve().parents[1] / "app" / "observability" / "executor_queue.py"
    text = source_path.read_text(encoding="utf-8")
    # The directive requires explanatory phrases at the emission site that
    # cover (a) what it measures (lazily-spawned threads) and (b) what it
    # does NOT measure (busy-worker count). Normalize whitespace AND strip
    # Python comment markers so a multi-line `#`-prefixed comment counts
    # as a single sentence.
    stripped = "\n".join(
        line.lstrip().lstrip("#").lstrip() for line in text.splitlines()
    )
    normalized = " ".join(stripped.split())
    assert "currently spawned by the default ThreadPoolExecutor" in normalized, (
        "expected the emission-site comment to say the field measures "
        "'worker threads currently spawned by the default ThreadPoolExecutor'"
    )
    assert "True busy-worker count is not measured" in normalized, (
        "expected the emission-site comment to say 'True busy-worker count "
        "is not measured'"
    )
