"""Follow-up remediation: `serialized_bytes_actual` must equal the exact
UTF-8 JSON byte count across the full `stack_capture` + `stack_frames` burst.

Scope 1 remediation shipped `serialized_bytes_estimate`, a staging-time byte
accumulator. The follow-up replaces it with `serialized_bytes_actual`, computed
by re-serializing each emitted event and summing the UTF-8-encoded lengths.

This test:
  1. Drives a stall through `_watchdog_run` to produce an emission burst.
  2. Parses every emitted JSON line.
  3. Re-serializes each parsed object exactly as `_emit` would and sums bytes.
  4. Asserts the sum equals the `serialized_bytes_actual` field in the
     `stack_capture` header AND is ≤ `STACK_CAPTURE_MAX_SERIALIZED_BYTES`.

Fails on b947444d (field is `serialized_bytes_estimate`), passes on follow-up.
"""
from __future__ import annotations

import json
import pathlib
import sys

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


def _reserialize_size(obj: dict) -> int:
    """Serialize exactly as `_emit` does: json.dumps with ensure_ascii=False
    and sort_keys=True, then UTF-8 encode, then +1 for the trailing newline
    `print` adds."""
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return len(text.encode("utf-8")) + 1


def test_stack_capture_serialized_bytes_actual_matches_aggregate(capsys, monkeypatch):
    from app.observability import heartbeat_watchdog as hbw
    from app.observability.constants import (
        STACK_CAPTURE_HOUR_CAP,
        STACK_CAPTURE_MAX_SERIALIZED_BYTES,
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
    # The watchdog loop may emit multiple captures. If two captures happen
    # over an identical thread set, their frames share the same digest, so
    # filtering by `frames_sha256` alone would over-match. Instead, walk the
    # event stream in order and take the stack_frames entries that follow
    # THIS header until the next stack_capture (or until frames_captured_total
    # entries are collected). This mirrors the emission order.
    frames: list[dict] = []
    found_header = False
    expected = int(header.get("frames_captured_total", 0))
    for ev in events:
        if not found_header and ev is header:
            found_header = True
            continue
        if not found_header:
            continue
        if ev.get("event") == "stack_capture":
            break
        if ev.get("event") == "stack_frames":
            frames.append(ev)
            if len(frames) >= expected:
                break

    # NEW field must be present; OLD field must be gone.
    assert "serialized_bytes_actual" in header, (
        f"stack_capture missing `serialized_bytes_actual`: keys={list(header)}"
    )
    assert "serialized_bytes_estimate" not in header, (
        "stack_capture must no longer emit `serialized_bytes_estimate`; "
        f"keys={list(header)}"
    )

    aggregate = sum(_reserialize_size(e) for e in [header] + frames)
    assert aggregate == header["serialized_bytes_actual"], (
        f"serialized_bytes_actual ({header['serialized_bytes_actual']}) must match "
        f"the re-serialized aggregate ({aggregate}); frame_count={len(frames)}"
    )
    assert aggregate <= STACK_CAPTURE_MAX_SERIALIZED_BYTES, (
        f"serialized aggregate {aggregate} exceeded cap {STACK_CAPTURE_MAX_SERIALIZED_BYTES}"
    )
