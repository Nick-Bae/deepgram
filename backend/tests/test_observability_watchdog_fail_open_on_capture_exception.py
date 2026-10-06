"""Watchdog fail-open regression — stack-capture exception must not kill the thread.

Independent-review defect #1: `_watchdog_run`'s main loop lacked a broad
try/except, so a RuntimeError raised from `_capture_stack_frames` or from any
emit path terminated the daemon thread. After remediation the thread must
survive the exception, emit a `watchdog_recovery` event (rate-limited), and
remain available for subsequent successful captures.
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


def test_watchdog_survives_capture_exception_and_resumes(capsys, monkeypatch):
    """A raising `_capture_stack_frames` must not terminate the watchdog
    thread. After the raise, a subsequent genuine stall must still produce
    a successful stack_capture emission."""
    try:
        from app.observability import heartbeat_watchdog as hbw
        from app.observability.constants import (
            STACK_CAPTURE_HOUR_CAP,
            STACK_CAPTURE_MIN_INTERVAL_S,
            STACK_CAPTURE_WINDOW_S,
        )
    except ImportError as exc:
        pytest.fail(f"heartbeat_watchdog missing — implementation gated. {exc}")

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
    capture_calls = {"n": 0}

    original_capture = hbw._capture_stack_frames

    def flaky_capture() -> tuple[str, list[dict]]:
        capture_calls["n"] += 1
        # First attempt raises; subsequent attempts return a real capture.
        if capture_calls["n"] == 1:
            raise RuntimeError("synthetic capture failure")
        return original_capture()

    monkeypatch.setattr(hbw, "_capture_stack_frames", flaky_capture)

    def fake_time() -> float:
        return now_container[0]

    def fake_sleep(dt: float) -> None:
        now_container[0] += dt
        sleep_calls["n"] += 1
        # Allow many iterations:
        # iter 1: no stall yet
        # iter 2: stall threshold passed, allow decision, capture RAISES → fail-open
        # iter 3-5: still stalled, min_interval suppresses OR another capture ok
        # Exit at n=12 to allow a second capture window after min_interval.
        if sleep_calls["n"] >= 20:
            state.stop_event.set()

    # Advance "now" far enough so rate limiter's min_interval is cleared.
    # We'll use a long check_interval so each sleep crosses the min_interval
    # of 60s between successive allowed captures.
    hbw._watchdog_run(
        state,
        check_interval_s=50.0,  # each tick advances now by 50s
        stall_threshold_s=2.0,
        rate_limiter=rate_limiter,
        time_fn=fake_time,
        sleep_fn=fake_sleep,
    )

    captured = capsys.readouterr().out

    # The watchdog must have attempted capture at least twice (first failed,
    # second after the sleep cycle); the function was called ≥ 2 times.
    assert capture_calls["n"] >= 2, (
        f"watchdog did not resume after capture exception; capture called {capture_calls['n']} times. "
        f"stdout tail: {captured[-800:]!r}"
    )

    # At least one real stack_capture must have been emitted after the
    # recovery (the fail-open path keeps the thread alive, then a later
    # stall allows a capture).
    successful_captures = _events(captured, "stack_capture")
    assert len(successful_captures) >= 1, (
        f"no successful stack_capture after recovery; stdout: {captured[-800:]!r}"
    )
