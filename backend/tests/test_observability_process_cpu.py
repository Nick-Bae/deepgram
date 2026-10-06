"""Process-CPU sampler emits a numeric cpu_pct using stdlib timing."""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

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


def test_process_cpu_emits_cpu_pct_numeric(capsys):
    try:
        from app.observability.process_cpu import start_process_cpu_sampler
    except ImportError as exc:
        pytest.fail(f"process_cpu missing — implementation gated. {exc}")

    async def run_briefly() -> None:
        task = start_process_cpu_sampler(emit_interval_s=0.1)
        try:
            await asyncio.sleep(0.35)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(run_briefly())
    events = _events(capsys.readouterr().out, "process_cpu")
    assert events, "expected ≥1 process_cpu event"
    for e in events:
        assert e.get("schema_version") == "1"
        assert e.get("component") == "process_cpu"
        assert "cpu_pct" in e and isinstance(e["cpu_pct"], (int, float))
        assert 0.0 <= float(e["cpu_pct"]) <= 10_000.0, (
            f"cpu_pct outside sane range: {e['cpu_pct']}"
        )
