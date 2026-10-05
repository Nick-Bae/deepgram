"""Observability package — public API.

Exports the samplers each test imports directly, plus the convenience
`start_all` / `stop_all` orchestrators the FastAPI startup/shutdown handlers
in `app.main` call once at process start/end.

Scope boundary: observability ONLY. These samplers never read or alter
request/response data, routing, auth, rate-limiting, provider handlers,
or Cloud Run config. They only observe and emit numeric telemetry.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from .event_loop_lag import start_event_loop_lag_sampler
from .task_count import start_task_count_sampler
from .process_cpu import start_process_cpu_sampler
from .executor_queue import start_executor_queue_sampler
from .heartbeat_watchdog import HeartbeatWatchdog
from ._emit import emit as _emit


__all__ = [
    "start_event_loop_lag_sampler",
    "start_task_count_sampler",
    "start_process_cpu_sampler",
    "start_executor_queue_sampler",
    "HeartbeatWatchdog",
    "start_all",
    "stop_all",
]


# Process-level handles for the start_all / stop_all pair. These are
# intentionally module-level so a double start_all() call is detectable (and
# a no-op) and shutdown can idempotently clean up partial state.
_sampler_tasks: list[asyncio.Task] = []
_watchdog: Optional[HeartbeatWatchdog] = None
_started: bool = False


def start_all(app=None) -> None:
    """Spawn all samplers + the heartbeat watchdog on the running event loop.

    Idempotent — a second call is a no-op. `app` is accepted for symmetry
    with FastAPI startup conventions and is not used; observability
    attaches to the current event loop, not to the FastAPI app object.
    """
    global _sampler_tasks, _watchdog, _started
    if _started:
        return
    _started = True
    _sampler_tasks = [
        start_event_loop_lag_sampler(),
        start_task_count_sampler(),
        start_process_cpu_sampler(),
        start_executor_queue_sampler(),
    ]
    _watchdog = HeartbeatWatchdog()
    _watchdog.start()
    _emit(
        "observability_started",
        severity="INFO",
        component="observability",
        sampler_count=len(_sampler_tasks),
        watchdog_enabled=True,
    )


async def stop_all() -> None:
    """Cancel all samplers and signal the watchdog to exit.

    Safe to call from the FastAPI shutdown handler. Bounded join on the
    watchdog thread (see `HeartbeatWatchdog.stop`).
    """
    global _sampler_tasks, _watchdog, _started
    if not _started:
        return
    _started = False
    for task in _sampler_tasks:
        if not task.done():
            task.cancel()
    if _sampler_tasks:
        await asyncio.gather(*_sampler_tasks, return_exceptions=True)
    _sampler_tasks = []
    if _watchdog is not None:
        try:
            await _watchdog.stop()
        except Exception:
            pass
        _watchdog = None
    _emit(
        "observability_stopped",
        severity="INFO",
        component="observability",
    )
