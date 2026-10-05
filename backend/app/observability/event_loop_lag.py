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
from typing import Deque, Optional

from .constants import (
    LAG_EMIT_INTERVAL_S,
    LAG_P99_ERROR_MS,
    LAG_P99_WARN_MS,
    LAG_SAMPLE_INTERVAL_S,
    LAG_WINDOW_SAMPLES,
)
from ._emit import emit


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


async def _lag_loop(
    emit_interval_s: float,
    sample_interval_s: float,
    window_samples: int,
) -> None:
    samples: Deque[float] = deque(maxlen=max(1, window_samples))
    last_emit = time.monotonic()
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(sample_interval_s)
        # The elapsed time BEYOND the requested sample interval is the lag —
        # i.e., how long the loop took to resume us after the sleep expired.
        elapsed = time.monotonic() - t0
        lag_ms = max(0.0, (elapsed - sample_interval_s) * 1000.0)
        samples.append(lag_ms)

        now = time.monotonic()
        if (now - last_emit) >= emit_interval_s:
            last_emit = now
            vals = sorted(samples)
            if not vals:
                continue
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
