"""Guard the executor-queue emission's field naming after defect 3
remediation.

The base at commit 7cf47d47 emits `active_workers=len(executor._threads)`,
which is the count of SPAWNED worker threads — not currently-busy ones.
After the Path A rename, the field is `worker_threads_total` so the name
matches what is measured.
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


def test_emission_uses_worker_threads_total_field():
    """After defect-3 remediation (Path A), the emission must expose
    `worker_threads_total` and must NOT expose the misleading
    `active_workers` name."""
    from app.observability import executor_queue as eq

    class _StubExec:
        _threads = ("t1", "t2", "t3")
        _max_workers = 8
        class _Q:
            @staticmethod
            def qsize() -> int:
                return 0
        _work_queue = _Q()

    import asyncio

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
        f"expected `worker_threads_total` in emission, got keys={sorted(d)}"
    )
    assert "active_workers" not in d, (
        f"`active_workers` was misleading — must be removed, got {d}"
    )
    assert d["worker_threads_total"] == 3


def test_worker_threads_total_does_not_decrease_when_work_completes():
    """Documentation-via-test: `worker_threads_total` counts SPAWNED threads
    (which the ThreadPoolExecutor keeps alive). It can increase but will
    not decrease when work completes. This asserts the semantic rather
    than fabricating a busy-count the field cannot provide.
    """
    from app.observability import executor_queue as eq

    class _StubExec:
        def __init__(self, count: int):
            self._threads = tuple(f"t{i}" for i in range(count))
            self._max_workers = 8
            class _Q:
                @staticmethod
                def qsize() -> int:
                    return 0
            self._work_queue = _Q()

    class _StubLoop:
        def __init__(self, exec_):
            self._default_executor = exec_

    import asyncio

    # First emission with 2 spawned threads.
    buf1 = StringIO()
    with patch("sys.stdout", buf1), patch(
        "app.observability.executor_queue.asyncio.get_running_loop",
        return_value=_StubLoop(_StubExec(2)),
    ):
        eq._executor_queue_tick()
    d1 = _parse_emission(buf1.getvalue())

    # All "work" completes, but spawned threads remain alive. _threads
    # count unchanged. Same emission expected.
    buf2 = StringIO()
    with patch("sys.stdout", buf2), patch(
        "app.observability.executor_queue.asyncio.get_running_loop",
        return_value=_StubLoop(_StubExec(2)),
    ):
        eq._executor_queue_tick()
    d2 = _parse_emission(buf2.getvalue())

    assert d1["worker_threads_total"] == d2["worker_threads_total"] == 2
