"""asyncio.all_tasks() count sampler.

Emits a gauge of currently-known asyncio tasks every N seconds, with a
signed delta against the previous sample for cheap "unbounded growth"
detection. No per-task labels — intentionally low cardinality.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from .constants import TASK_COUNT_EMIT_INTERVAL_S
from ._emit import emit


async def _task_count_loop(emit_interval_s: float) -> None:
    prev: Optional[int] = None
    while True:
        try:
            # Count tasks on the current running loop. On Python < 3.10 this
            # may need an explicit loop= kwarg; 3.10+ picks the running loop
            # automatically — the backend targets 3.12.
            tasks = asyncio.all_tasks()
            count = len(tasks)
        except RuntimeError:
            # No running loop (shouldn't happen inside this coroutine, but
            # be defensive — emit a zero sample rather than crash the loop).
            count = 0
        delta = (count - prev) if prev is not None else 0
        emit(
            "asyncio_task_count",
            severity="INFO",
            component="event_loop",
            count=int(count),
            delta_30s=int(delta),
        )
        prev = count
        try:
            await asyncio.sleep(emit_interval_s)
        except asyncio.CancelledError:
            raise


def start_task_count_sampler(
    *,
    emit_interval_s: float = TASK_COUNT_EMIT_INTERVAL_S,
) -> asyncio.Task:
    coro = _task_count_loop(emit_interval_s)
    return asyncio.create_task(coro, name="observability-task-count")
