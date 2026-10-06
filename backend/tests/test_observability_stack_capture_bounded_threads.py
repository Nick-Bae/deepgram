"""Stack capture must bound thread count — review defect #2 remediation.

Stubs threading.enumerate to return many fake threads and asserts
`stack_capture` reports truncation via `thread_count_captured`,
`thread_count_total`, `truncated`, and `truncated_reason="threads"`.
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


def test_capture_bounds_at_max_threads(capsys, monkeypatch):
    try:
        from app.observability import heartbeat_watchdog as hbw
        from app.observability.constants import (
            STACK_CAPTURE_HOUR_CAP,
            STACK_CAPTURE_MAX_THREADS,
            STACK_CAPTURE_MIN_INTERVAL_S,
            STACK_CAPTURE_WINDOW_S,
        )
    except ImportError as exc:
        pytest.fail(f"stack-size caps missing — implementation gated. {exc}")

    assert STACK_CAPTURE_MAX_THREADS >= 1

    # Build 200 fake threads referenced by fake frames.
    import sys as _sys
    fake_frames: dict[int, object] = {}
    fake_threads: list[_FakeThread] = []
    # Any real frame object works; reuse the current frame for everyone.
    current_frame = _sys._getframe()
    for i in range(1, 201):
        fake_threads.append(_FakeThread(ident=10_000 + i, name=f"fake-thread-{i}"))
        fake_frames[10_000 + i] = current_frame

    monkeypatch.setattr(
        hbw.sys, "_current_frames", lambda: dict(fake_frames)
    )
    monkeypatch.setattr(
        hbw.threading, "enumerate", lambda: list(fake_threads)
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

    assert "thread_count_captured" in event
    assert "thread_count_total" in event
    assert "truncated" in event
    assert "truncated_reason" in event

    assert event["thread_count_captured"] <= STACK_CAPTURE_MAX_THREADS
    assert event["thread_count_total"] >= 200
    assert event["truncated"] is True
    assert event["truncated_reason"] == "threads"
