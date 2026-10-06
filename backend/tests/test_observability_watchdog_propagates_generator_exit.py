"""Follow-up remediation: watchdog must NOT swallow GeneratorExit.

Companion propagation test — GeneratorExit is BaseException-level in CPython
and `except Exception` leaves it alone. Verifies the narrowed catch.
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


def test_watchdog_propagates_generator_exit(capsys, monkeypatch):
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
        raise GeneratorExit()

    monkeypatch.setattr(hbw, "_capture_stack_frames", raising_capture)

    def fake_time() -> float:
        return now_container[0]

    def fake_sleep(dt: float) -> None:
        now_container[0] += dt
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 20:
            state.stop_event.set()

    with pytest.raises(GeneratorExit):
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
    assert len(recoveries) == 0, (
        "GeneratorExit must NOT trigger the recovery path; "
        f"found {len(recoveries)} recovery events in stdout"
    )
