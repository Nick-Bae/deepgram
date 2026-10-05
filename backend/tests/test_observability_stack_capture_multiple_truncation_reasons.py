"""Follow-up remediation: emit a bounded `truncated_reasons` list when
multiple caps apply simultaneously.

Scope 1 shipped a single `truncated_reason` enum (earliest-wins). The follow-
up adds `truncated_reasons: list[str]` reporting ALL caps hit in evaluation
order (threads → frames_per_thread → serialized_bytes). The primary field
`truncated_reason` is retained as `truncated_reasons[0]` for backward
compatibility.

This test triggers BOTH `threads` AND `serialized_bytes` simultaneously by
monkeypatching `sys._current_frames` + `threading.enumerate` to produce
> MAX_THREADS threads AND stacks whose aggregate exceeds the byte cap.

Fails on b947444d (no `truncated_reasons` field), passes on follow-up.
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _parsed_lines(captured: str) -> list[dict]:
    out: list[dict] = []
    for line in captured.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        out.append(obj)
    return out


class _FakeThread:
    def __init__(self, ident: int, name: str) -> None:
        self.ident = ident
        self.name = name


def _make_fake_frames(n_threads: int, depth_per_thread: int):
    """Build a mapping thread_id -> frame object with enough fake depth that
    traceback.extract_stack will yield `depth_per_thread` entries."""
    real_frames: list = []
    def _recursive(remaining: int):
        if remaining <= 0:
            return sys._getframe(0)
        return _recursive(remaining - 1)
    # Build one deep frame once (expensive-ish stack); reuse pointer across all
    # fake threads so threading.enumerate ↔ sys._current_frames line up.
    deep = _recursive(depth_per_thread)
    for i in range(n_threads):
        real_frames.append((i + 10000, deep))
    return dict(real_frames)


def test_both_threads_and_serialized_bytes_truncation(capsys, monkeypatch):
    from app.observability import heartbeat_watchdog as hbw
    from app.observability.constants import (
        STACK_CAPTURE_HOUR_CAP,
        STACK_CAPTURE_MAX_FRAMES_PER_THREAD,
        STACK_CAPTURE_MAX_THREADS,
        STACK_CAPTURE_MIN_INTERVAL_S,
        STACK_CAPTURE_WINDOW_S,
    )

    # Build > MAX_THREADS synthetic threads with enough frame depth that the
    # byte cap also fires during serialization.
    n_threads = STACK_CAPTURE_MAX_THREADS + 20
    fake_frames_map = _make_fake_frames(
        n_threads=n_threads,
        depth_per_thread=STACK_CAPTURE_MAX_FRAMES_PER_THREAD,
    )
    fake_threads = [_FakeThread(ident=i + 10000, name=f"synthetic-{i}") for i in range(n_threads)]

    monkeypatch.setattr(hbw.sys, "_current_frames", lambda: fake_frames_map)
    monkeypatch.setattr(hbw.threading, "enumerate", lambda: fake_threads)

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
        check_interval_s=50.0,
        stall_threshold_s=2.0,
        rate_limiter=rate_limiter,
        time_fn=fake_time,
        sleep_fn=fake_sleep,
    )

    captured = capsys.readouterr().out
    events = _parsed_lines(captured)
    captures = [e for e in events if e.get("event") == "stack_capture"]
    assert len(captures) >= 1, f"no stack_capture emitted; stdout tail: {captured[-800:]!r}"
    header = captures[0]

    assert "truncated_reasons" in header, (
        f"stack_capture missing `truncated_reasons`: keys={list(header)}"
    )
    reasons = header["truncated_reasons"]
    assert isinstance(reasons, list)
    assert "threads" in reasons, f"expected 'threads' in truncated_reasons, got {reasons!r}"
    assert "serialized_bytes" in reasons, (
        f"expected 'serialized_bytes' in truncated_reasons, got {reasons!r}"
    )
    # Evaluation order: threads first, serialized_bytes after.
    assert reasons.index("threads") < reasons.index("serialized_bytes"), (
        f"evaluation order violated; reasons={reasons!r}"
    )
    # Primary reason remains the earliest-wins value.
    assert header["truncated_reason"] == "threads", (
        f"primary truncated_reason must stay as earliest wins; got {header['truncated_reason']!r}"
    )
    assert header["truncated"] is True
