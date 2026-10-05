"""Process CPU-percent sampler — stdlib only, no psutil.

Computes `cpu_pct = (process_time_delta / wall_time_delta) * 100.0` across
sampling intervals using `time.process_time()` (user+system CPU time for
the current process, including all threads) and `time.monotonic()` (wall
time). On a single-core machine the value will cap near 100.0; on a
multi-core machine it can exceed 100.0 when threads/subprocesses truly run
in parallel — e.g., N cores at full tilt reports N*100.0.

Trade-off vs psutil: we lose per-cpu breakdowns and RSS. The incident
diagnostic only needs a process-wide CPU trend, which this provides without
a new runtime dependency. See REPORT.md § Remaining Limitations.

The first interval returns 0.0 (we need two samples to compute a delta),
which is harmless — the second emission onward is the useful signal.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from .constants import PROCESS_CPU_EMIT_INTERVAL_S
from ._emit import emit


async def _process_cpu_loop(emit_interval_s: float) -> None:
    prev_wall: Optional[float] = None
    prev_cpu: Optional[float] = None
    while True:
        wall_now = time.monotonic()
        cpu_now = time.process_time()
        if prev_wall is None or prev_cpu is None:
            cpu_pct = 0.0
        else:
            dw = max(1e-9, wall_now - prev_wall)
            dc = max(0.0, cpu_now - prev_cpu)
            cpu_pct = max(0.0, (dc / dw) * 100.0)
        emit(
            "process_cpu",
            severity="INFO",
            component="process_cpu",
            cpu_pct=round(cpu_pct, 3),
            wall_delta_s=round((wall_now - prev_wall) if prev_wall is not None else 0.0, 6),
            cpu_delta_s=round((cpu_now - prev_cpu) if prev_cpu is not None else 0.0, 6),
        )
        prev_wall = wall_now
        prev_cpu = cpu_now
        try:
            await asyncio.sleep(emit_interval_s)
        except asyncio.CancelledError:
            raise


def start_process_cpu_sampler(
    *,
    emit_interval_s: float = PROCESS_CPU_EMIT_INTERVAL_S,
) -> asyncio.Task:
    coro = _process_cpu_loop(emit_interval_s)
    return asyncio.create_task(coro, name="observability-process-cpu")
