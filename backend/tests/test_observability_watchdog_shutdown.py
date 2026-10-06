"""Shutdown-lifecycle guard: the watchdog thread must exit on stop + join
within a bounded timeout, and emit nothing after shutdown returns.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _events(captured: str) -> list[dict]:
    out: list[dict] = []
    for line in captured.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        out.append(obj)
    return out


def test_watchdog_thread_terminates_on_shutdown(capsys):
    try:
        from app.observability.heartbeat_watchdog import HeartbeatWatchdog
    except ImportError as exc:
        pytest.fail(f"HeartbeatWatchdog missing — implementation gated. {exc}")

    async def run_and_shutdown() -> None:
        wd = HeartbeatWatchdog(
            tick_interval_s=0.05,
            check_interval_s=0.05,
            stall_threshold_s=10.0,
        )
        wd.start()
        thread = wd._thread
        task = wd._task
        assert thread is not None and thread.is_alive(), "watchdog thread not alive after start()"
        assert task is not None and not task.done(), "heartbeat task not running"

        # Let it run briefly so the tick advances at least once.
        await asyncio.sleep(0.2)

        # Shutdown — must terminate within the bounded join timeout.
        await wd.stop()
        # Give the OS a tiny moment to reap the thread descriptor.
        for _ in range(20):
            if not thread.is_alive():
                break
            time.sleep(0.05)
        assert not thread.is_alive(), "watchdog thread did not terminate within join timeout"
        assert task.done(), "heartbeat task did not stop on shutdown"

        # After shutdown, no further emissions should appear from this watchdog.
        pre_count = len(_events(capsys.readouterr().out))
        await asyncio.sleep(0.3)
        post_count = len(_events(capsys.readouterr().out))
        assert post_count == 0, (
            f"watchdog emitted {post_count} events after shutdown; expected 0"
        )
        # pre_count is informational — zero captures expected since stall was never reached.

    asyncio.run(run_and_shutdown())
