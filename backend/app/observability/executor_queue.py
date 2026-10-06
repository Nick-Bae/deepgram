"""Default-executor queue depth sampler.

The event loop's default ThreadPoolExecutor is what `run_in_executor(None,
...)` hits. A growing queue there is a strong signal that CPU-bound work
offloaded via `run_in_executor` is piling up faster than the pool drains —
one of the hypotheses in the 2026-10-04 CPU-pin incident.

Uses private `ThreadPoolExecutor._work_queue` because the public API does
not expose queue depth. This is explicitly flagged in SCOPE and the report
as a Python-version-fragile attribute; the sampler guards every access and
degrades gracefully if the attribute is missing.

Defect 3 remediation (2026-10-05 review): the previous emission used
`active_workers=len(executor._threads)`, which is the count of SPAWNED
worker threads — not currently-busy ones. ThreadPoolExecutor keeps workers
alive after their futures complete, so `_threads` grows monotonically up
to `max_workers`. We rename the field to `worker_threads_total` so the
name matches what is actually measured. A reliable "busy workers" metric
would require instrumenting the submit/future-done path, which is out of
Scope 1.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .constants import EXECUTOR_QUEUE_EMIT_INTERVAL_S
from ._emit import emit


def _resolve_default_executor(loop: asyncio.AbstractEventLoop) -> Any:
    """Return the loop's default executor if one exists. Does NOT force a
    pool creation — a loop with no executor-backed `run_in_executor` yet
    will show max_workers=0, which is itself a useful signal."""
    getter = getattr(loop, "_default_executor", None)
    return getter  # may be None


def _queue_depth(executor: Any) -> int:
    q = getattr(executor, "_work_queue", None)
    if q is None:
        return -1
    try:
        return int(q.qsize())
    except Exception:
        return -1


def _max_workers(executor: Any) -> int:
    mw = getattr(executor, "_max_workers", None)
    if isinstance(mw, int):
        return mw
    return -1


def _worker_threads_total(executor: Any) -> int:
    """Count of SPAWNED worker threads in the pool, not currently-busy
    workers. `ThreadPoolExecutor` keeps spawned workers alive until the
    pool is shut down; this value grows monotonically up to `max_workers`
    and does not decrease when work completes. Name reflects reality
    (defect 3 remediation)."""
    threads = getattr(executor, "_threads", None)
    if threads is None:
        return -1
    try:
        return len(threads)
    except Exception:
        return -1


def _executor_queue_tick() -> None:
    """One sampler tick. Extracted from the async loop so tests can
    exercise emission deterministically."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover
        return
    executor = _resolve_default_executor(loop)
    if executor is None:
        emit(
            "executor_queue",
            severity="INFO",
            component="executor",
            queue_depth=0,
            worker_threads_total=0,
            max_workers=0,
            executor_initialized=False,
        )
        return
    emit(
        "executor_queue",
        severity="INFO",
        component="executor",
        queue_depth=_queue_depth(executor),
        # `worker_threads_total` measures the number of worker threads
        # currently spawned by the default ThreadPoolExecutor (lazy up to
        # `max_workers`); these threads may be idle. True busy-worker count
        # is not measured — see Defect 3 remediation note. The field was
        # previously named `active_workers`, which was misleading because
        # ThreadPoolExecutor keeps spawned workers alive after their futures
        # complete.
        worker_threads_total=_worker_threads_total(executor),
        max_workers=_max_workers(executor),
        executor_initialized=True,
    )


async def _executor_queue_loop(emit_interval_s: float) -> None:
    while True:
        _executor_queue_tick()
        try:
            await asyncio.sleep(emit_interval_s)
        except asyncio.CancelledError:
            raise


def start_executor_queue_sampler(
    *,
    emit_interval_s: float = EXECUTOR_QUEUE_EMIT_INTERVAL_S,
) -> asyncio.Task:
    coro = _executor_queue_loop(emit_interval_s)
    return asyncio.create_task(coro, name="observability-executor-queue")
