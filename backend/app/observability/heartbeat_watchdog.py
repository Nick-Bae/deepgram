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

import json as _size_json

from .constants import (
    HEARTBEAT_STALL_THRESHOLD_S,
    HEARTBEAT_TICK_INTERVAL_S,
    HEARTBEAT_WATCHDOG_CHECK_INTERVAL_S,
    STACK_CAPTURE_HOUR_CAP,
    STACK_CAPTURE_MAX_FRAMES_PER_THREAD,
    STACK_CAPTURE_MAX_SERIALIZED_BYTES,
    STACK_CAPTURE_MAX_THREADS,
    STACK_CAPTURE_MIN_INTERVAL_S,
    STACK_CAPTURE_WINDOW_S,
    WATCHDOG_RECOVERY_HOUR_CAP,
    WATCHDOG_RECOVERY_MIN_INTERVAL_S,
    WATCHDOG_RECOVERY_WINDOW_S,
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


def _frame_json_size(frame: dict) -> int:
    """Return the number of bytes a frame will add to the serialized burst.

    Approximate: use json.dumps with sort_keys so the estimate is stable.
    We care about ordering of magnitude, not exact byte count.
    """
    try:
        return len(_size_json.dumps(frame, ensure_ascii=False, sort_keys=True)) + 1  # +1 for newline
    except Exception:
        # Fallback: assume a modest frame size if serialization fails.
        return 256


class _CaptureResult:
    """Bounded-capture staging record. Return value of `_capture_stack_frames`.

    Fields:
      - digest: short sha256 of the canonical frame listing (correlation id).
      - frames: the per-frame dicts that will be emitted (after bounds).
      - thread_count_total: actual threads at capture time.
      - thread_count_captured: threads whose frames made it into `frames`.
      - frames_captured_total: len(frames).
      - truncated: whether any cap was hit.
      - truncated_reason: enum, one of none|threads|frames_per_thread|serialized_bytes.
      - serialized_bytes_estimate: int, byte accumulator at stop.
    """

    __slots__ = (
        "digest",
        "frames",
        "thread_count_total",
        "thread_count_captured",
        "frames_captured_total",
        "truncated",
        "truncated_reason",
        "serialized_bytes_estimate",
    )

    def __init__(self) -> None:
        self.digest: str = ""
        self.frames: list[dict] = []
        self.thread_count_total: int = 0
        self.thread_count_captured: int = 0
        self.frames_captured_total: int = 0
        self.truncated: bool = False
        self.truncated_reason: str = "none"
        self.serialized_bytes_estimate: int = 0


def _capture_stack_frames() -> _CaptureResult:
    """Capture a redacted stack-frame record, bounded by thread count,
    frames per thread, and total serialized bytes.

    Review defect #2 remediation. Returns a `_CaptureResult` with all counts
    populated so the caller can emit the `stack_capture` header with the
    cardinality + truncation information before the per-frame lines.
    """
    result = _CaptureResult()
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

    result.thread_count_total = len(current_frames)

    canonical_lines: list[str] = []
    frame_index = 0

    # Deterministic ordering: by thread id, so repeated captures of the
    # same wedge produce the same sha256 (correlation ID stable).
    ordered_items = sorted(current_frames.items(), key=lambda kv: kv[0])

    # Truncation-reason priority (ordered — earliest-wins):
    #   1. "threads"            — thread count exceeded the cap. Earliest signal.
    #   2. "frames_per_thread"  — one or more threads had their frame list cut.
    #   3. "serialized_bytes"   — total bytes forced early stop during serialization.
    # Multiple caps can apply at once; the earliest-stage cap wins so the
    # operator sees the first point at which data was lost. "none" otherwise.
    threads_truncated = len(ordered_items) > STACK_CAPTURE_MAX_THREADS
    frames_truncated_any = False
    bytes_truncated = False

    captured_items = ordered_items[:STACK_CAPTURE_MAX_THREADS]

    for thread_id, frame in captured_items:
        thread_name = name_by_id.get(thread_id, f"<unknown-{thread_id}>")
        try:
            extracted = traceback.extract_stack(frame)
        except Exception:
            continue

        # Cap 2: frames per thread.
        thread_frames = extracted[:STACK_CAPTURE_MAX_FRAMES_PER_THREAD]
        if len(extracted) > STACK_CAPTURE_MAX_FRAMES_PER_THREAD:
            frames_truncated_any = True

        thread_contributed = False
        for file, line, func, _code in thread_frames:
            rel = _rel_path(file)
            frame_dict = {
                "frame_index": frame_index,
                "thread_name": thread_name,
                "file": rel,
                "function": func,
                "line": int(line),
            }
            # Cap 3: cumulative serialized bytes.
            frame_bytes = _frame_json_size(frame_dict)
            projected = result.serialized_bytes_estimate + frame_bytes
            if projected > STACK_CAPTURE_MAX_SERIALIZED_BYTES:
                bytes_truncated = True
                # Record the digest from what we have so far and return.
                result.digest = hashlib.sha256(
                    "\n".join(canonical_lines).encode("utf-8")
                ).hexdigest()[:16]
                result.frames_captured_total = len(result.frames)
                if thread_contributed:
                    result.thread_count_captured += 1
                _assign_truncation(
                    result,
                    threads_truncated=threads_truncated,
                    frames_truncated_any=frames_truncated_any,
                    bytes_truncated=bytes_truncated,
                )
                return result

            result.frames.append(frame_dict)
            canonical_lines.append(f"{thread_name}|{rel}|{func}|{line}")
            result.serialized_bytes_estimate = projected
            frame_index += 1
            thread_contributed = True

        if thread_contributed:
            result.thread_count_captured += 1

    result.frames_captured_total = len(result.frames)
    result.digest = hashlib.sha256(
        "\n".join(canonical_lines).encode("utf-8")
    ).hexdigest()[:16]
    _assign_truncation(
        result,
        threads_truncated=threads_truncated,
        frames_truncated_any=frames_truncated_any,
        bytes_truncated=bytes_truncated,
    )
    return result


def _assign_truncation(
    result: "_CaptureResult",
    *,
    threads_truncated: bool,
    frames_truncated_any: bool,
    bytes_truncated: bool,
) -> None:
    """Apply the earliest-wins priority to the truncation flags."""
    if threads_truncated:
        result.truncated = True
        result.truncated_reason = "threads"
        return
    if frames_truncated_any:
        result.truncated = True
        result.truncated_reason = "frames_per_thread"
        return
    if bytes_truncated:
        result.truncated = True
        result.truncated_reason = "serialized_bytes"
        return
    result.truncated = False
    result.truncated_reason = "none"


def _emit_stack_capture(
    stall_seconds: float,
    result: Optional["_CaptureResult"] = None,
    *,
    thread_count: Optional[int] = None,
    digest: Optional[str] = None,
) -> None:
    """Emit a `stack_capture` header event.

    Primary form: pass a `_CaptureResult` from `_capture_stack_frames` so the
    header carries the full bounded-capture cardinality + truncation info.

    Legacy form (kept for the pre-remediation no-sensitive-labels test and
    any ad-hoc caller that only had `thread_count` + `digest`): pass those
    kwargs instead. Truncation fields default to the "no truncation known"
    representation.
    """
    if result is not None:
        emit(
            "stack_capture",
            severity="WARNING",
            component="heartbeat_watchdog",
            stall_seconds=round(stall_seconds, 3),
            thread_count=int(result.thread_count_captured),
            thread_count_captured=int(result.thread_count_captured),
            thread_count_total=int(result.thread_count_total),
            frames_captured_total=int(result.frames_captured_total),
            truncated=bool(result.truncated),
            truncated_reason=str(result.truncated_reason),
            serialized_bytes_estimate=int(result.serialized_bytes_estimate),
            frames_sha256=result.digest,
        )
        return
    emit(
        "stack_capture",
        severity="WARNING",
        component="heartbeat_watchdog",
        stall_seconds=round(stall_seconds, 3),
        thread_count=int(thread_count or 0),
        thread_count_captured=int(thread_count or 0),
        thread_count_total=int(thread_count or 0),
        frames_captured_total=0,
        truncated=False,
        truncated_reason="none",
        serialized_bytes_estimate=0,
        frames_sha256=str(digest or ""),
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


def _emit_watchdog_recovery(
    recovery_rate_limiter: "_RateLimiter",
    recovery_count: int,
    now: float,
) -> None:
    """Emit a rate-limited `watchdog_recovery` event with low-cardinality payload.

    Review defect #1 remediation. The event carries ONLY the fixed envelope
    plus `recovery_count_since_start`. No exception text, no exception type,
    no args, no locals, no stack text, no identifiers of any kind. If the
    rate limiter suppresses, emit nothing (silent).
    """
    try:
        decision = recovery_rate_limiter.check_and_record(now)
        if decision != "allow":
            return
        emit(
            "watchdog_recovery",
            severity="WARNING",
            component="heartbeat_watchdog",
            recovery_count_since_start=int(recovery_count),
        )
    except BaseException:
        # Recovery emission failure is also swallowed — the whole point is
        # that nothing from this path can terminate the thread.
        pass


def _watchdog_run(
    state: HeartbeatState,
    *,
    check_interval_s: float,
    stall_threshold_s: float,
    rate_limiter: _RateLimiter,
    time_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
    recovery_rate_limiter: Optional["_RateLimiter"] = None,
) -> None:
    """Watchdog main loop — runs in a daemon thread.

    `time_fn` and `sleep_fn` are injectable for deterministic testing.

    Review defect #1 remediation: every iteration is wrapped in a broad
    try/except. Any exception from stack capture, emission, or any other
    inner call is swallowed; the thread emits a rate-limited
    `watchdog_recovery` event and continues. The sleep is unconditional so a
    repeated-failure path cannot turn into a tight CPU loop.
    """
    recovery_limiter = recovery_rate_limiter or _RateLimiter(
        min_interval_s=WATCHDOG_RECOVERY_MIN_INTERVAL_S,
        hour_cap=WATCHDOG_RECOVERY_HOUR_CAP,
        window_s=WATCHDOG_RECOVERY_WINDOW_S,
    )
    last_seen_counter = state.counter
    last_seen_ts = state.last_tick_ts
    recovery_count = 0
    pending_cooldown = False
    while not state.stop_event.is_set():
        # ----- normal check interval sleep (always unconditional) -----
        try:
            sleep_fn(check_interval_s)
        except BaseException:
            # A sleep failure is itself unrecoverable (clock corruption etc.)
            # Return cleanly; the daemon thread exits.
            return
        if state.stop_event.is_set():
            return

        # ----- iteration body — any exception is swallowed -----
        try:
            if pending_cooldown:
                # Post-capture cooldown: observe a longer wait before
                # re-detecting the SAME stall. The outer loop's
                # `sleep_fn(check_interval_s)` ran once; add the stall-
                # threshold delta here. Still unconditional sleep.
                try:
                    sleep_fn(stall_threshold_s)
                except BaseException:
                    return
                pending_cooldown = False
                # Re-baseline so the next pass measures a fresh stall window.
                last_seen_counter = state.counter
                last_seen_ts = state.last_tick_ts
                continue

            current_counter = state.counter
            current_last_tick_ts = state.last_tick_ts
            now = time_fn()
            if current_counter != last_seen_counter:
                # Loop made progress — reset detection baseline, keep observing.
                last_seen_counter = current_counter
                last_seen_ts = current_last_tick_ts
                continue
            # Counter has NOT advanced. How long has the heartbeat been silent?
            stall_s = max(0.0, now - current_last_tick_ts)
            if stall_s < stall_threshold_s:
                continue
            # Loop is stalled — attempt a capture, honouring rate limits.
            decision = rate_limiter.check_and_record(now)
            if decision == "allow":
                result = _capture_stack_frames()
                _emit_stack_capture(stall_seconds=stall_s, result=result)
                _emit_stack_frames(result.digest, result.frames)
            else:
                _emit_suppressed(reason=decision)
            # Flag that the next loop iteration should perform the cooldown
            # sleep (keeps the sleep unconditional + bounded).
            pending_cooldown = True
        except BaseException:
            # Any failure inside the iteration body (capture raise, emission
            # raise, enumerate raise, serialization raise) must NOT terminate
            # the thread. Record a recovery event and continue to the next
            # iteration. The recovery emission is itself best-effort.
            recovery_count += 1
            try:
                _emit_watchdog_recovery(
                    recovery_limiter,
                    recovery_count,
                    time_fn() if callable(time_fn) else time.time(),
                )
            except BaseException:
                pass


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
