"""Rate-limit guard: at most 3 full captures in a rolling 3600 s window.

Fires 4 stalls spaced just over the 60 s min-interval (60.01 s) so the
min-interval rule allows each one, and the hour cap trips on the 4th.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def test_hour_cap_suppresses_fourth_capture():
    try:
        from app.observability.heartbeat_watchdog import _RateLimiter
    except ImportError as exc:
        pytest.fail(f"heartbeat_watchdog missing — implementation gated. {exc}")

    rl = _RateLimiter(min_interval_s=60.0, hour_cap=3, window_s=3600.0)

    now = 1_000_000.0
    decisions = []
    for i in range(4):
        decisions.append(rl.check_and_record(now + i * 60.01))

    assert decisions[0] == "allow"
    assert decisions[1] == "allow"
    assert decisions[2] == "allow"
    assert decisions[3] == "hour_cap", (
        f"4th capture must be hour-cap-suppressed; got {decisions}"
    )


def test_hour_cap_resets_after_window():
    try:
        from app.observability.heartbeat_watchdog import _RateLimiter
    except ImportError as exc:
        pytest.fail(f"heartbeat_watchdog missing — implementation gated. {exc}")

    rl = _RateLimiter(min_interval_s=60.0, hour_cap=3, window_s=3600.0)

    now = 1_000_000.0
    for i in range(3):
        assert rl.check_and_record(now + i * 60.01) == "allow"
    # Advance past the full window; cap should reset.
    assert rl.check_and_record(now + 3601.0) == "allow"
