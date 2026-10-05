"""Failing-before test — scope: prep/observability.

Asserts the asyncio-task-count sampler exists and emits parseable JSON
events with an integer `count`. Fails on `d7473ed7` by design.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _parse_event_lines(captured: str, name: str) -> list[dict]:
    out: list[dict] = []
    for line in captured.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("event") == name:
            out.append(obj)
    return out


def test_task_count_sampler_emits_count_and_delta(capsys):
    try:
        from app.observability import start_task_count_sampler
    except ImportError as exc:
        pytest.fail(
            f"app.observability.start_task_count_sampler missing — "
            f"implementation gated on separate authorization. "
            f"Import error: {exc}"
        )

    async def run_briefly() -> None:
        task = start_task_count_sampler(emit_interval_s=0.1)
        try:
            await asyncio.sleep(0.35)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(run_briefly())
    captured = capsys.readouterr().out
    events = _parse_event_lines(captured, "asyncio_task_count")
    assert events, f"expected ≥1 asyncio_task_count event; got none. stdout={captured!r}"
    first = events[0]
    assert "count" in first, f"event missing count: {first}"
    assert isinstance(first["count"], int), (
        f"count must be int, got {type(first['count']).__name__}: {first['count']!r}"
    )
    assert "delta_30s" in first, f"event missing delta_30s: {first}"
    assert isinstance(first["delta_30s"], int), (
        f"delta_30s must be int, got {type(first['delta_30s']).__name__}"
    )
    assert first.get("component") == "event_loop"
    assert first.get("schema_version") == "1"
