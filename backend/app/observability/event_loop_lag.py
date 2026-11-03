"""Event-loop lag sampler.

Measures `asyncio.sleep(0)` wall-clock latency on a cooperative schedule,
keeps a bounded ring of recent samples, and emits a summary every N seconds.

A climbing p99 is the signal that other coroutines are hogging the loop —
the primary offline-diagnostic metric for a CPU-pin.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Optional

from .constants import (
    LAG_EMIT_INTERVAL_S,
    LAG_P99_ERROR_MS,
    LAG_P99_WARN_MS,
    LAG_SAMPLE_INTERVAL_S,
    LAG_WINDOW_SAMPLES,
)
from ._emit import emit


# Module-level clock aliases so tests may inject deterministic clocks
# WITHOUT patching the global `time.monotonic` / `time.time`
# (which would also perturb `asyncio.sleep`'s internal clock).
# Production code never rebinds these.
_now_mono = time.monotonic
_now_wall = time.time


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round((pct / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def _severity_for(p99_ms: float) -> str:
    if p99_ms >= LAG_P99_ERROR_MS:
        return "ERROR"
    if p99_ms >= LAG_P99_WARN_MS:
        return "WARNING"
    return "INFO"


def _iso_utc(epoch_seconds: float) -> str:
    """UTC ISO-8601 with microsecond precision + 'Z' suffix.

    The input is wall-clock epoch seconds captured at sample time, used ONLY
    for log correlation (not for duration arithmetic — see monotonic path).
    """
    return (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


async def _lag_loop(
    emit_interval_s: float,
    sample_interval_s: float,
    window_samples: int,
) -> None:
    # Each sample carries (lag_ms, start_mono, end_mono, start_wall, end_wall):
    # - lag_ms is the lag value (unchanged semantics).
    # - start_mono / end_mono bound the sample's measurement interval; all
    #   duration arithmetic uses these (monotonic, immune to wall-clock jumps).
    # - start_wall / end_wall are wall-clock epochs captured at the same two
    #   instants, surfaced as UTC ISO strings for Cloud Logging correlation.
    # The buffer's oldest start-of-measurement and newest end-of-measurement
    # define the full observation window (see emit block below).
    samples: Deque[tuple[float, float, float, float, float]] = deque(
        maxlen=max(1, window_samples)
    )
    last_emit = _now_mono()
    while True:
        start_mono = _now_mono()
        start_wall = _now_wall()
        await asyncio.sleep(sample_interval_s)
        # The elapsed time BEYOND the requested sample interval is the lag —
        # i.e., how long the loop took to resume us after the sleep expired.
        end_mono = _now_mono()
        end_wall = _now_wall()
        elapsed = end_mono - start_mono
        lag_ms = max(0.0, (elapsed - sample_interval_s) * 1000.0)
        samples.append((lag_ms, start_mono, end_mono, start_wall, end_wall))

        now = _now_mono()
        if (now - last_emit) >= emit_interval_s:
            last_emit = now
            if not samples:
                continue
            vals = sorted(s[0] for s in samples)
            if not vals:
                continue
            # Observation-boundary fields derived from the live buffer:
            # - oldest sample's measurement START through newest sample's
            #   measurement END. For a single buffered sample, this collapses
            #   to that one sample's own interval — NOT zero.
            # - elapsed comes from monotonic deltas (immune to clock jumps).
            # - UTC boundaries come from wall-clock stamps; they are informative
            #   for log correlation and MUST NOT be used for duration arithmetic.
            first_start_mono = samples[0][1]
            last_end_mono = samples[-1][2]
            first_start_wall = samples[0][3]
            last_end_wall = samples[-1][4]
            observed_window_elapsed_seconds = max(0.0, last_end_mono - first_start_mono)
            p50 = _percentile(vals, 50)
            p95 = _percentile(vals, 95)
            p99 = _percentile(vals, 99)
            max_ms = vals[-1]
            emit(
                "event_loop_lag",
                severity=_severity_for(p99),
                component="event_loop",
                samples=len(vals),
                p50_ms=round(p50, 3),
                p95_ms=round(p95, 3),
                p99_ms=round(p99, 3),
                max_ms=round(max_ms, 3),
                window_seconds=round(sample_interval_s * len(vals), 3),
                observed_window_start_utc=_iso_utc(first_start_wall),
                observed_window_end_utc=_iso_utc(last_end_wall),
                observed_window_elapsed_seconds=round(
                    observed_window_elapsed_seconds, 6
                ),
            )


def start_event_loop_lag_sampler(
    *,
    emit_interval_s: float = LAG_EMIT_INTERVAL_S,
    sample_interval_s: float = LAG_SAMPLE_INTERVAL_S,
    window_samples: int = LAG_WINDOW_SAMPLES,
) -> asyncio.Task:
    """Spawn the sampler as an asyncio Task. Caller owns cancellation."""
    coro = _lag_loop(emit_interval_s, sample_interval_s, window_samples)
    return asyncio.create_task(coro, name="observability-event-loop-lag")
