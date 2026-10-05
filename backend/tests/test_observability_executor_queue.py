"""Executor-queue sampler emits numeric queue_depth and active_workers."""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _events(captured: str, name: str) -> list[dict]:
    out: list[dict] = []
    for line in captured.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("event") == name:
            out.append(obj)
    return out


def test_executor_queue_emits_queue_depth_and_active_workers(capsys):
    try:
        from app.observability.executor_queue import start_executor_queue_sampler
    except ImportError as exc:
        pytest.fail(f"executor_queue missing — implementation gated. {exc}")

    async def run_briefly() -> None:
        # Prime the default executor so the sampler sees a non-None pool.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: time.sleep(0.01))
        task = start_executor_queue_sampler(emit_interval_s=0.1)
        try:
            await asyncio.sleep(0.35)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(run_briefly())
    events = _events(capsys.readouterr().out, "executor_queue")
    assert events, "expected ≥1 executor_queue event"
    for e in events:
        assert e.get("schema_version") == "1"
        assert e.get("component") == "executor"
        assert "queue_depth" in e and isinstance(e["queue_depth"], int)
        assert "active_workers" in e and isinstance(e["active_workers"], int)
        assert "max_workers" in e and isinstance(e["max_workers"], int)
