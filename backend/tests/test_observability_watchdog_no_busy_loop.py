"""Watchdog must not enter a CPU-busy retry loop after repeated failures.

Review defect #1 remediation: every iteration sleeps the configured check
interval regardless of whether capture succeeded or raised. This test records
every `sleep_fn` call duration and asserts every one is >= check_interval - eps.
On the pre-remediation code the thread dies after the first raise, which also
fails this assertion (never reaches iteration ≥ 2).
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def test_sleep_intervals_never_shortcut_on_repeated_capture_failures(capsys, monkeypatch):
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

    def always_raise() -> tuple[str, list[dict]]:
        raise RuntimeError("synthetic capture failure #%d")

    monkeypatch.setattr(hbw, "_capture_stack_frames", always_raise)

    now_container = [100.0]
    sleep_durations: list[float] = []

    def fake_time() -> float:
        return now_container[0]

    def fake_sleep(dt: float) -> None:
        sleep_durations.append(dt)
        now_container[0] += dt
        if len(sleep_durations) >= 15:
            state.stop_event.set()

    check_interval_s = 1.0
    hbw._watchdog_run(
        state,
        check_interval_s=check_interval_s,
        stall_threshold_s=2.0,
        rate_limiter=rate_limiter,
        time_fn=fake_time,
        sleep_fn=fake_sleep,
    )

    # Pre-remediation path: the thread dies on the first raise → fewer than 2
    # sleep calls observed → this assertion fails.
    # Post-remediation path: every iteration sleeps check_interval_s (the
    # cooldown sleep may use stall_threshold_s which is also >= eps).
    assert len(sleep_durations) >= 10, (
        f"watchdog produced only {len(sleep_durations)} sleep calls — "
        f"likely died after first capture exception. durations={sleep_durations}"
    )

    # No sleep call shorter than the check interval. Allow the stall_threshold
    # cooldown sleep too (which is >= check_interval in default config).
    eps = 1e-6
    min_allowed = min(check_interval_s, 2.0) - eps  # 2.0 is stall_threshold_s
    short_sleeps = [d for d in sleep_durations if d < min_allowed]
    assert not short_sleeps, (
        f"watchdog entered a shorter-than-{min_allowed:.3f}s sleep (busy-loop risk). "
        f"short_sleeps={short_sleeps}"
    )
