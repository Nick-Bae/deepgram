"""Guard the heartbeat-watchdog stall-detection path.

Simulates a stalled event loop by freezing the heartbeat state's counter +
timestamp relative to the watchdog's injectable clock. Asserts exactly one
`stack_capture` event is emitted with a correct `stall_seconds` field.
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading
import time
from typing import Iterable

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


def test_watchdog_fires_stack_capture_on_simulated_stall(capsys):
    """A frozen heartbeat triggers exactly one `stack_capture` event with
    a stall_seconds field matching the simulated gap."""
    try:
        from app.observability.heartbeat_watchdog import (
            HeartbeatState,
            _RateLimiter,
            _watchdog_run,
        )
        from app.observability.constants import (
            STACK_CAPTURE_HOUR_CAP,
            STACK_CAPTURE_MIN_INTERVAL_S,
            STACK_CAPTURE_WINDOW_S,
        )
    except ImportError as exc:
        pytest.fail(f"heartbeat_watchdog missing — implementation gated. {exc}")

    state = HeartbeatState()
    # Freeze state at t=100.0 and never advance it.
    state.counter = 1
    state.last_tick_ts = 100.0

    rate_limiter = _RateLimiter(
        min_interval_s=STACK_CAPTURE_MIN_INTERVAL_S,
        hour_cap=STACK_CAPTURE_HOUR_CAP,
        window_s=STACK_CAPTURE_WINDOW_S,
    )

    # Deterministic clock: advance 10 s per sleep call so the watchdog
    # observes a growing gap vs state.last_tick_ts without real time passing.
    now_container = [100.0]

    def fake_time() -> float:
        return now_container[0]

    sleep_calls = {"n": 0}

    def fake_sleep(dt: float) -> None:
        now_container[0] += dt
        sleep_calls["n"] += 1
        # Give the watchdog enough iterations to (a) accumulate past the
        # stall threshold, (b) fire the capture, and (c) complete the
        # post-capture cooldown sleep before exit. With check_interval=1 s
        # and stall_threshold=2 s, the stall fires on iteration 3; capture
        # emission + cooldown = iterations 4-5; we stop at n=6 so a clean
        # exit path runs.
        if sleep_calls["n"] >= 6:
            state.stop_event.set()

    _watchdog_run(
        state,
        check_interval_s=1.0,
        stall_threshold_s=2.0,
        rate_limiter=rate_limiter,
        time_fn=fake_time,
        sleep_fn=fake_sleep,
    )

    captured = capsys.readouterr().out
    captures = _events(captured, "stack_capture")
    assert len(captures) == 1, (
        f"expected exactly 1 stack_capture, got {len(captures)}. stdout={captured[:800]!r}"
    )
    event = captures[0]
    assert event["schema_version"] == "1"
    assert event["component"] == "heartbeat_watchdog"
    assert event["severity"] == "WARNING"
    assert "stall_seconds" in event and float(event["stall_seconds"]) >= 2.0
    assert "frames_sha256" in event and len(event["frames_sha256"]) == 16

    # And each frame emitted independently.
    frames = _events(captured, "stack_frames")
    assert frames, "expected ≥1 stack_frames emission accompanying the capture"
    # Each frame must reference the SAME frames_sha256.
    frame_digests = {f["frames_sha256"] for f in frames}
    assert frame_digests == {event["frames_sha256"]}
    # Frames must NOT contain forbidden fields — only enum-ish metadata.
    forbidden = {"locals", "code", "args", "source", "arguments"}
    for f in frames:
        assert not (forbidden & set(f.keys())), f"frame leaked forbidden field: {f}"
