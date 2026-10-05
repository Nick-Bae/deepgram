"""Default-executor queue depth sampler.

The event loop's default ThreadPoolExecutor is what `run_in_executor(None,
...)` hits. A growing queue there is a strong signal that CPU-bound work
offloaded via `run_in_executor` is piling up faster than the pool drains —
one of the hypotheses in the 2026-10-04 CPU-pin incident.

Uses private `ThreadPoolExecutor._work_queue` because the public API does
not expose queue depth. This is explicitly flagged in SCOPE and the report
as a Python-version-fragile attribute; the sampler guards every access and
degrades gracefully if the attribute is missing.
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


def _active_threads(executor: Any) -> int:
    threads = getattr(executor, "_threads", None)
    if threads is None:
        return -1
    try:
        return len(threads)
    except Exception:
        return -1


async def _executor_queue_loop(emit_interval_s: float) -> None:
    while True:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover
            return
        executor = _resolve_default_executor(loop)
        if executor is None:
            # No default executor materialised yet — report that explicitly.
            emit(
                "executor_queue",
                severity="INFO",
                component="executor",
                queue_depth=0,
                active_workers=0,
                max_workers=0,
                executor_initialized=False,
            )
        else:
            emit(
                "executor_queue",
                severity="INFO",
                component="executor",
                queue_depth=_queue_depth(executor),
                active_workers=_active_threads(executor),
                max_workers=_max_workers(executor),
                executor_initialized=True,
            )
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
