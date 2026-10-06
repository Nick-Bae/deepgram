"""Stack capture must bound total serialized bytes — review defect #2 remediation.

Builds many fake threads with modest per-thread stacks whose cumulative
JSON-serialized size exceeds STACK_CAPTURE_MAX_SERIALIZED_BYTES.
Asserts truncation triggers via the bytes path specifically
(truncated_reason == "serialized_bytes").
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


class _FakeThread:
    def __init__(self, ident: int, name: str) -> None:
        self.ident = ident
        self.name = name


def _frame_with_depth(depth: int):
    import sys as _sys
    if depth <= 1:
        return _sys._getframe()
    return _frame_with_depth(depth - 1)


def test_capture_bounds_at_serialized_bytes(capsys, monkeypatch):
    try:
        from app.observability import heartbeat_watchdog as hbw
        from app.observability.constants import (
            STACK_CAPTURE_HOUR_CAP,
            STACK_CAPTURE_MAX_FRAMES_PER_THREAD,
            STACK_CAPTURE_MAX_SERIALIZED_BYTES,
            STACK_CAPTURE_MAX_THREADS,
            STACK_CAPTURE_MIN_INTERVAL_S,
            STACK_CAPTURE_WINDOW_S,
        )
    except ImportError as exc:
        pytest.fail(f"stack-size caps missing — implementation gated. {exc}")

    # Priority-ordered truncation: "threads" wins over "frames_per_thread"
    # wins over "serialized_bytes". To force the bytes path specifically:
    #   - thread count MUST NOT exceed STACK_CAPTURE_MAX_THREADS
    #   - frames per thread MUST NOT exceed STACK_CAPTURE_MAX_FRAMES_PER_THREAD
    #   - cumulative serialized bytes MUST exceed STACK_CAPTURE_MAX_SERIALIZED_BYTES
    # Use many threads at the thread cap, moderate stack depth, long thread
    # names to inflate per-frame bytes.
    long_name = "x" * 800  # 800-char thread name inflates per-frame byte count
    # Keep frames per thread at or under the cap (128) so frames_per_thread
    # does NOT trip. 60-deep frame leaves pytest headroom and still well
    # under the cap.
    moderate_frame = _frame_with_depth(60)
    assert 60 < STACK_CAPTURE_MAX_FRAMES_PER_THREAD, (
        "harness invariant: moderate stack must stay under frames_per_thread cap"
    )

    # Thread count exactly at the cap — not over, so "threads" does NOT trip.
    fake_threads = []
    fake_frames: dict[int, object] = {}
    for i in range(1, STACK_CAPTURE_MAX_THREADS + 1):
        tid = 50_000 + i
        fake_threads.append(_FakeThread(ident=tid, name=f"{long_name}-{i}"))
        fake_frames[tid] = moderate_frame

    monkeypatch.setattr(hbw.sys, "_current_frames", lambda: dict(fake_frames))
    monkeypatch.setattr(hbw.threading, "enumerate", lambda: list(fake_threads))

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
    assert captures, f"no stack_capture emitted; stdout: {captured[-800:]!r}"
    event = captures[0]

    assert event["truncated"] is True
    # Must specifically hit the byte-size path first because each frame
    # contributes a thread_name of 800 chars.
    assert event["truncated_reason"] == "serialized_bytes", (
        f"expected serialized_bytes truncation, got {event['truncated_reason']!r}; "
        f"actual={event.get('serialized_bytes_actual')}"
    )
    assert "serialized_bytes_actual" in event
    assert isinstance(event["serialized_bytes_actual"], int)
    assert event["serialized_bytes_actual"] <= STACK_CAPTURE_MAX_SERIALIZED_BYTES, (
        "serialized_bytes_actual must not exceed the configured ceiling"
    )
