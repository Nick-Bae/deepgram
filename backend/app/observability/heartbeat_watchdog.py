"""Separate-thread heartbeat watchdog.

The async samplers can only emit when the event loop makes progress. During
a true event-loop pin (CPU-bound synchronous work hogging the loop), those
samplers go silent. This watchdog runs in a **separate OS thread** so it
keeps emitting even when the loop is wedged.

Mechanics:
  * An async heartbeat task bumps a shared monotonic counter + timestamp
    every `HEARTBEAT_TICK_INTERVAL_S` seconds.
  * The watchdog thread wakes every `HEARTBEAT_WATCHDOG_CHECK_INTERVAL_S`
    seconds and reads the counter/timestamp.
  * If the counter hasn't advanced in `HEARTBEAT_STALL_THRESHOLD_S` seconds,
    the loop is stalled — capture a stack of every thread.

Stack-capture redaction (hard rules):
  * Only `(file, function, line)` per frame — NEVER local variable values,
    NEVER source-code line content, NEVER argument values.
  * File paths repo-relative.
  * Stack text used only for sha256 digest; the digest is a correlation id,
    not a leak vector.

Rate-limiting:
  * Minimum 60 s between full captures.
  * Hour cap of 3 captures per rolling 3600 s window.
  * On suppress, emit `stack_capture_suppressed` with reason only.

Nothing in this module reaches HTTP, Firestore, or any user data.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import pathlib
import sys
import threading
import time
import traceback
from collections import deque
from typing import Callable, Deque, Optional

from .constants import (
    HEARTBEAT_STALL_THRESHOLD_S,
    HEARTBEAT_TICK_INTERVAL_S,
    HEARTBEAT_WATCHDOG_CHECK_INTERVAL_S,
    STACK_CAPTURE_HOUR_CAP,
    STACK_CAPTURE_MIN_INTERVAL_S,
    STACK_CAPTURE_WINDOW_S,
)
from ._emit import emit


# Repo root — stripped from frame paths so emissions are low-cardinality and
# never leak absolute operator paths.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _rel_path(abs_path: str) -> str:
    try:
        return str(pathlib.Path(abs_path).resolve().relative_to(_REPO_ROOT))
    except Exception:
        # Not under the repo (stdlib, site-packages, etc.) — return basename
        # only to avoid leaking operator paths.
        try:
            return pathlib.Path(abs_path).name
        except Exception:
            return "<unknown>"


class HeartbeatState:
    """Shared state read by the watchdog thread, written by the async heartbeat.

    `counter` monotonically increases; `last_tick_ts` is the wall-clock time
    (seconds since epoch) at which it was last bumped. Both are plain ints/
    floats — Python guarantees atomic reads/writes at the GIL level for these.
    """

    __slots__ = ("counter", "last_tick_ts", "stop_event")

    def __init__(self) -> None:
        self.counter: int = 0
        self.last_tick_ts: float = time.time()
        self.stop_event: threading.Event = threading.Event()


class _RateLimiter:
    """Rate-limit stack captures per `heartbeat_watchdog` config.

    Enforces two independent constraints:
      1. Minimum interval since last capture.
      2. Sliding-window hourly cap.

    `check_and_record(now)` returns one of:
      * `"allow"`         — capture permitted; call records this timestamp.
      * `"min_interval"`  — suppressed, too soon since last capture.
      * `"hour_cap"`      — suppressed, cap reached in rolling window.
    """

    def __init__(
        self,
        *,
        min_interval_s: float,
        hour_cap: int,
        window_s: float,
    ) -> None:
        self._min_interval_s = min_interval_s
        self._hour_cap = hour_cap
        self._window_s = window_s
        self._captures: Deque[float] = deque()
        self._lock = threading.Lock()

    def check_and_record(self, now: float) -> str:
        with self._lock:
            # Drop captures that fell out of the sliding window.
            while self._captures and (now - self._captures[0]) > self._window_s:
                self._captures.popleft()
            if self._captures and (now - self._captures[-1]) < self._min_interval_s:
                return "min_interval"
            if len(self._captures) >= self._hour_cap:
                return "hour_cap"
            self._captures.append(now)
            return "allow"


def _capture_stack_frames() -> tuple[str, list[dict]]:
    """Capture a redacted stack-frame record for every live thread.

    Returns `(frames_sha256_16hex, frames_list)` where each frame is
    `{thread_name, file, function, line, frame_index}`. The sha256 is of the
    canonical concatenation of frames; it serves as a stable correlation id
    across captures of the same stack shape.
    """
    frames: list[dict] = []
    try:
        current_frames = sys._current_frames()
    except Exception:
        current_frames = {}
    name_by_id: dict[int, str] = {}
    try:
        for t in threading.enumerate():
            name_by_id[t.ident or 0] = t.name
    except Exception:
        pass

    canonical_lines: list[str] = []
    frame_index = 0
    for thread_id, frame in current_frames.items():
        thread_name = name_by_id.get(thread_id, f"<unknown-{thread_id}>")
        for file, line, func, _code in traceback.extract_stack(frame):
            rel = _rel_path(file)
            frames.append(
                {
                    "frame_index": frame_index,
                    "thread_name": thread_name,
                    "file": rel,
                    "function": func,
                    "line": int(line),
                }
            )
            canonical_lines.append(f"{thread_name}|{rel}|{func}|{line}")
            frame_index += 1

    digest = hashlib.sha256("\n".join(canonical_lines).encode("utf-8")).hexdigest()[:16]
    return digest, frames


def _emit_stack_capture(stall_seconds: float, thread_count: int, digest: str) -> None:
    emit(
        "stack_capture",
        severity="WARNING",
        component="heartbeat_watchdog",
        stall_seconds=round(stall_seconds, 3),
        thread_count=int(thread_count),
        frames_sha256=digest,
    )


def _emit_stack_frames(digest: str, frames: list[dict]) -> None:
    for f in frames:
        emit(
            "stack_frames",
            severity="WARNING",
            component="heartbeat_watchdog",
            frames_sha256=digest,
            frame_index=f["frame_index"],
            thread_name=f["thread_name"],
            file=f["file"],
            function=f["function"],
            line=f["line"],
        )


def _emit_suppressed(reason: str) -> None:
    emit(
        "stack_capture_suppressed",
        severity="NOTICE",
        component="heartbeat_watchdog",
        reason=reason,
    )


def _watchdog_run(
    state: HeartbeatState,
    *,
    check_interval_s: float,
    stall_threshold_s: float,
    rate_limiter: _RateLimiter,
    time_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Watchdog main loop — runs in a daemon thread.

    `time_fn` and `sleep_fn` are injectable for deterministic testing.
    """
    last_seen_counter = state.counter
    last_seen_ts = state.last_tick_ts
    while not state.stop_event.is_set():
        try:
            sleep_fn(check_interval_s)
        except Exception:
            return
        if state.stop_event.is_set():
            return
        current_counter = state.counter
        current_last_tick_ts = state.last_tick_ts
        now = time_fn()
        if current_counter != last_seen_counter:
            # Loop made progress since our last check — reset the detection
            # baseline but keep observing.
            last_seen_counter = current_counter
            last_seen_ts = current_last_tick_ts
            continue
        # Counter has NOT advanced since the previous watchdog iteration.
        # Compute how long the heartbeat has been silent, measured from the
        # heartbeat's own last tick timestamp.
        stall_s = max(0.0, now - current_last_tick_ts)
        if stall_s < stall_threshold_s:
            continue
        # Loop is stalled — attempt a capture, honouring rate limits.
        decision = rate_limiter.check_and_record(now)
        if decision == "allow":
            digest, frames = _capture_stack_frames()
            _emit_stack_capture(
                stall_seconds=stall_s,
                thread_count=sum(1 for _ in threading.enumerate()),
                digest=digest,
            )
            _emit_stack_frames(digest, frames)
        else:
            _emit_suppressed(reason=decision)
        # After emission (or suppression) wait at least `stall_threshold_s`
        # before considering the SAME stall again — avoids a tight re-check
        # loop while the loop is still wedged. The rate limiter would also
        # suppress a flood of captures, but adding this delay keeps the
        # watchdog thread's own CPU footprint negligible.
        try:
            sleep_fn(stall_threshold_s)
        except Exception:
            return
        # Re-baseline so the next pass measures a fresh stall window.
        last_seen_counter = state.counter
        last_seen_ts = state.last_tick_ts


async def _heartbeat_tick(state: HeartbeatState, tick_interval_s: float) -> None:
    while True:
        state.counter += 1
        state.last_tick_ts = time.time()
        try:
            await asyncio.sleep(tick_interval_s)
        except asyncio.CancelledError:
            raise


class HeartbeatWatchdog:
    """Public handle for a running watchdog + heartbeat pair.

    Call `start()` to spawn the async heartbeat task AND the daemon thread.
    Call `stop()` from an async context to cancel the task and signal the
    thread to exit (joining with a bounded timeout).
    """

    def __init__(
        self,
        *,
        tick_interval_s: float = HEARTBEAT_TICK_INTERVAL_S,
        check_interval_s: float = HEARTBEAT_WATCHDOG_CHECK_INTERVAL_S,
        stall_threshold_s: float = HEARTBEAT_STALL_THRESHOLD_S,
        rate_limiter: Optional[_RateLimiter] = None,
    ) -> None:
        self._tick_interval_s = tick_interval_s
        self._check_interval_s = check_interval_s
        self._stall_threshold_s = stall_threshold_s
        self._rate_limiter = rate_limiter or _RateLimiter(
            min_interval_s=STACK_CAPTURE_MIN_INTERVAL_S,
            hour_cap=STACK_CAPTURE_HOUR_CAP,
            window_s=STACK_CAPTURE_WINDOW_S,
        )
        self.state = HeartbeatState()
        self._thread: Optional[threading.Thread] = None
        self._task: Optional[asyncio.Task] = None

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(
            _heartbeat_tick(self.state, self._tick_interval_s),
            name="observability-heartbeat-tick",
        )
        self._thread = threading.Thread(
            target=_watchdog_run,
            args=(self.state,),
            kwargs={
                "check_interval_s": self._check_interval_s,
                "stall_threshold_s": self._stall_threshold_s,
                "rate_limiter": self._rate_limiter,
            },
            daemon=True,
            name="observability-watchdog",
        )
        self._thread.start()
        return self._task

    async def stop(self) -> None:
        self.state.stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self._check_interval_s * 2))
