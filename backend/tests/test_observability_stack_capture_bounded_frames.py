"""Stack capture must bound frames per thread — review defect #2 remediation.

Builds a thread with a 1000-deep stack. Asserts frames emitted ≤ 128 and that
truncated_reason is one of ("frames_per_thread", "serialized_bytes").
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading as _real_threading

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


class _FakeThread:
    def __init__(self, ident: int, name: str) -> None:
        self.ident = ident
        self.name = name


def _deep_frame(depth: int):
    """Return a frame object that, when extract_stack'd, has approximately
    `depth` frames. Recursively calls itself to grow the real stack.

    Caller should keep `depth` well below Python's default recursion limit
    (1000) and leave headroom for pytest's own call stack.
    """
    import sys as _sys
    if depth <= 1:
        return _sys._getframe()
    return _deep_frame(depth - 1)


def test_capture_bounds_at_max_frames_per_thread(capsys, monkeypatch):
    try:
        from app.observability import heartbeat_watchdog as hbw
        from app.observability.constants import (
            STACK_CAPTURE_HOUR_CAP,
            STACK_CAPTURE_MAX_FRAMES_PER_THREAD,
            STACK_CAPTURE_MIN_INTERVAL_S,
            STACK_CAPTURE_WINDOW_S,
        )
    except ImportError as exc:
        pytest.fail(f"stack-size caps missing — implementation gated. {exc}")

    assert STACK_CAPTURE_MAX_FRAMES_PER_THREAD >= 1

    # Build a stack much deeper than MAX_FRAMES_PER_THREAD (128), well below
    # Python's recursion limit (1000) to leave headroom for pytest's own frames.
    deep_frame = _deep_frame(300)
    fake_thread = _FakeThread(ident=99_999, name="deep-stack-thread")
    monkeypatch.setattr(
        hbw.sys, "_current_frames", lambda: {99_999: deep_frame}
    )
    monkeypatch.setattr(
        hbw.threading, "enumerate", lambda: [fake_thread]
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

    def fake_time() -> float:
        return now_container[0]

    def fake_sleep(dt: float) -> None:
        now_container[0] += dt
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 6:
            state.stop_event.set()

    hbw._watchdog_run(
        state,
        check_interval_s=1.0,
        stall_threshold_s=2.0,
        rate_limiter=rate_limiter,
        time_fn=fake_time,
        sleep_fn=fake_sleep,
    )

    captured = capsys.readouterr().out
    captures = _events(captured, "stack_capture")
    assert captures, f"no stack_capture event emitted; stdout: {captured[-800:]!r}"
    event = captures[0]

    assert event["truncated"] is True
    assert event["truncated_reason"] in ("frames_per_thread", "serialized_bytes"), (
        f"expected truncated_reason frames_per_thread or serialized_bytes, got {event['truncated_reason']!r}"
    )

    frames = _events(captured, "stack_frames")
    # Frames belong to the single fake thread; must be ≤ cap.
    for_fake = [f for f in frames if f.get("thread_name") == "deep-stack-thread"]
    assert for_fake, "no frames emitted for deep-stack-thread"
    assert len(for_fake) <= STACK_CAPTURE_MAX_FRAMES_PER_THREAD, (
        f"emitted {len(for_fake)} frames for one thread; cap is "
        f"{STACK_CAPTURE_MAX_FRAMES_PER_THREAD}"
    )
