"""Failing-before test — scope: prep/observability.

Asserts the event-loop-lag sampler exists and emits parseable JSON events
with a `p99_ms` field. Implementation lands under separate authorization;
this test fails on `d7473ed7` by design.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _parse_event_lines(captured: str) -> list[dict]:
    out: list[dict] = []
    for line in captured.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("event") == "event_loop_lag":
            out.append(obj)
    return out


def test_event_loop_lag_sampler_emits_p99_field(capsys):
    """A sampler task runs for a short interval and emits at least one
    `event_loop_lag` event with `p99_ms` populated."""
    try:
        from app.observability import start_event_loop_lag_sampler
    except ImportError as exc:
        pytest.fail(
            f"app.observability.start_event_loop_lag_sampler missing — "
            f"implementation gated on separate authorization. "
            f"Import error: {exc}"
        )

    async def run_briefly() -> None:
        task = start_event_loop_lag_sampler(emit_interval_s=0.1, sample_interval_s=0.01)
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
    events = _parse_event_lines(captured)
    assert events, f"expected ≥1 event_loop_lag event; got none. stdout={captured!r}"
    first = events[0]
    assert "p99_ms" in first, f"event missing p99_ms: {first}"
    assert isinstance(first["p99_ms"], (int, float)), (
        f"p99_ms must be numeric, got {type(first['p99_ms']).__name__}: {first['p99_ms']!r}"
    )
    assert first.get("schema_version") == "1"
    assert first.get("component") == "event_loop"
