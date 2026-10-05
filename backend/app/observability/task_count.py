"""asyncio.all_tasks() count sampler.

Emits a gauge of currently-known asyncio tasks every N seconds, with a
signed delta against the previous sample for cheap "unbounded growth"
detection. No per-task labels — intentionally low cardinality.

Severity (SCOPE.md § M2): WARNING ONLY after the asyncio task count has
increased by at least `TASK_COUNT_WARNING_DELTA` from a defined BASELINE
for `TASK_COUNT_WARNING_CONSECUTIVE` CONSECUTIVE samples. This is a
delta-FROM-BASELINE rule — distinct from a per-sample absolute-threshold
rule on `delta_30s`.

BASELINE SEMANTICS (chosen: rolling baseline, resets on recovery):

- First sample: `baseline = count`; `consecutive_above = 0`; emit INFO.
- Subsequent samples:
    - If `count >= baseline + TASK_COUNT_WARNING_DELTA`:
      `consecutive_above += 1`.
      Baseline does NOT move while the count stays elevated — even after
      the WARNING fires. Only a recovery sample rebases.
    - Else (recovery):
      `consecutive_above = 0` AND `baseline = count`. The next
      above-delta sample starts a fresh "sample 1 of 3" — the counter
      cannot carry over from before the recovery.
- Severity is WARNING on the sample where `consecutive_above` reaches
  `TASK_COUNT_WARNING_CONSECUTIVE` (and on subsequent samples as long as
  the condition holds). Severity is INFO otherwise.

Why rolling-baseline-with-recovery-rebase and NOT process-start-fixed:

A process-start-fixed baseline would make the WARNING a one-shot — once
the process has steady-state settled above the initial count, every
sample would be WARNING forever, drowning the signal. The rolling
baseline treats each "flat period" as a new reference, so WARNING fires
on genuine new growth, not on steady-state level.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from .constants import (
    TASK_COUNT_EMIT_INTERVAL_S,
    TASK_COUNT_WARNING_CONSECUTIVE,
    TASK_COUNT_WARNING_DELTA,
)
from ._emit import emit


_prev_count: Optional[int] = None
_baseline: Optional[int] = None
_consecutive_above: int = 0


def _reset_state_for_tests() -> None:
    """Deterministic state reset — used by test fixtures only."""
    global _prev_count, _baseline, _consecutive_above
    _prev_count = None
    _baseline = None
    _consecutive_above = 0


def _task_count_tick() -> None:
    """One sampler tick. See module docstring for the full state machine."""
    global _prev_count, _baseline, _consecutive_above
    try:
        tasks = asyncio.all_tasks()
        count = len(tasks)
    except RuntimeError:
        count = 0

    if _prev_count is None:
        _baseline = count
        _consecutive_above = 0
        emit(
            "asyncio_task_count",
            severity="INFO",
            component="event_loop",
            count=int(count),
            delta_30s=0,
        )
        _prev_count = count
        return

    delta = count - _prev_count
    assert _baseline is not None
    if count >= _baseline + TASK_COUNT_WARNING_DELTA:
        _consecutive_above += 1
    else:
        _consecutive_above = 0
        _baseline = count

    severity = (
        "WARNING"
        if _consecutive_above >= TASK_COUNT_WARNING_CONSECUTIVE
        else "INFO"
    )

    emit(
        "asyncio_task_count",
        severity=severity,
        component="event_loop",
        count=int(count),
        delta_30s=int(delta),
    )

    _prev_count = count


async def _task_count_loop(emit_interval_s: float) -> None:
    while True:
        _task_count_tick()
        try:
            await asyncio.sleep(emit_interval_s)
        except asyncio.CancelledError:
            raise


def start_task_count_sampler(
    *,
    emit_interval_s: float = TASK_COUNT_EMIT_INTERVAL_S,
) -> asyncio.Task:
    _reset_state_for_tests()
    coro = _task_count_loop(emit_interval_s)
    return asyncio.create_task(coro, name="observability-task-count")
