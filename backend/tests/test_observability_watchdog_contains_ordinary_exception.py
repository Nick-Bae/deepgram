"""Regression guard: ordinary Exception from capture is still contained.

The watchdog's fail-open path must continue to swallow and emit a recovery
event when the capture function raises a *subclass of Exception* (e.g.,
RuntimeError). This is intentional containment and must NOT regress during
the propagation narrowing.
"""
from __future__ import annotations

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


def test_watchdog_contains_runtime_error_and_emits_recovery(capsys, monkeypatch):
    from app.observability import heartbeat_watchdog as hbw
    from app.observability.constants import (
        STACK_CAPTURE_HOUR_CAP,
        STACK_CAPTURE_MIN_INTERVAL_S,
        STACK_CAPTURE_WINDOW_S,
    )

    state = hbw.HeartbeatState()
    state.counter = 1
    state.last_tick_ts = 100.0

    rate_limiter = hbw._RateLimiter(
        min_interval_s=STACK_CAPTURE_MIN_INTERVAL_S,
        hour_cap=STACK_CAPTURE_HOUR_CAP,
        window_s=STACK_CAPTURE_WINDOW_S,
    )

    now_container = [100.0]
    sleep_calls = {"n": 0}

    def raising_capture() -> hbw._CaptureResult:
        raise RuntimeError("ordinary runtime error")

    monkeypatch.setattr(hbw, "_capture_stack_frames", raising_capture)

    def fake_time() -> float:
        return now_container[0]

    def fake_sleep(dt: float) -> None:
        now_container[0] += dt
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 10:
            state.stop_event.set()

    # Must NOT raise.
    hbw._watchdog_run(
        state,
        check_interval_s=50.0,
        stall_threshold_s=2.0,
        rate_limiter=rate_limiter,
        time_fn=fake_time,
        sleep_fn=fake_sleep,
    )

    captured = capsys.readouterr().out
    recoveries = _events(captured, "watchdog_recovery")
    assert len(recoveries) >= 1, (
        "RuntimeError must trigger at least one watchdog_recovery emission; "
        f"stdout tail: {captured[-800:]!r}"
    )
    for ev in recoveries:
        # Low-cardinality shape — no exception text / message / args.
        for forbidden in ("exception", "message", "args", "error_text", "traceback"):
            assert forbidden not in ev, f"recovery event leaked {forbidden!r}: {ev!r}"
        assert "recovery_count_since_start" in ev
