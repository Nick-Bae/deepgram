"""Rate-limit guard: within 60 s only one full stack capture is allowed.

Drives the rate limiter directly (deterministic, no real timers) by
invoking `check_and_record` 10 times at tightly-spaced timestamps.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def test_min_interval_suppresses_fast_repeat_captures():
    try:
        from app.observability.heartbeat_watchdog import _RateLimiter
    except ImportError as exc:
        pytest.fail(f"heartbeat_watchdog missing — implementation gated. {exc}")

    rl = _RateLimiter(min_interval_s=60.0, hour_cap=3, window_s=3600.0)

    now = 1_000_000.0
    decisions = []
    for i in range(10):
        # Fire every 5 s — fast enough to trip the 60 s minimum interval.
        decisions.append(rl.check_and_record(now + i * 5.0))

    allows = [d for d in decisions if d == "allow"]
    min_suppressed = [d for d in decisions if d == "min_interval"]
    hour_suppressed = [d for d in decisions if d == "hour_cap"]

    assert len(allows) == 1, f"expected exactly 1 allow in 10 rapid-fire checks; got {decisions}"
    assert len(min_suppressed) == 9, (
        f"expected 9 min_interval suppressions; got {decisions}"
    )
    assert len(hour_suppressed) == 0, (
        f"hour_cap must not have fired at 5 s cadence; got {decisions}"
    )
