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
from typing import Any, Callable, Deque, Optional

import json as _size_json

from .constants import (
    HEARTBEAT_STALL_THRESHOLD_S,
    HEARTBEAT_TICK_INTERVAL_S,
    HEARTBEAT_WATCHDOG_CHECK_INTERVAL_S,
    STACK_CAPTURE_HEADER_BYTES_BUDGET,
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
from ._emit import emit, instance_id as _emit_instance_id


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


# 16-char placeholder for the sha256[:16] frames_sha256 digest. Used during
# staging ONLY — the real digest replaces it before emission. 16 hex chars
# in both cases, so byte counts are identical.
_FRAMES_SHA256_PLACEHOLDER = "0" * 16


def _frame_envelope_size(frame: dict) -> int:
    """Return the exact UTF-8 byte count for the `stack_frames` event this
    frame will emit, matching the envelope `emit()` produces (plus +1 for
    newline). Used by the staging-time truncation decision so the aggregate
    aligns with the post-emit measurement.
    """
    # Build the full envelope that `_emit_stack_frames` will pass to `emit()`,
    # then serialize with the same rules `emit()` uses. We don't yet know the
    # real digest; use a 16-char placeholder (same width as the real digest).
    env = {
        "component": "heartbeat_watchdog",
        "event": "stack_frames",
        "frame_index": frame.get("frame_index", 0),
        "frames_sha256": _FRAMES_SHA256_PLACEHOLDER,
        "function": frame.get("function", ""),
        "file": frame.get("file", ""),
        "instance_id": _emit_instance_id(),
        "line": int(frame.get("line", 0)),
        "schema_version": "1",
        "severity": "WARNING",
        "thread_name": frame.get("thread_name", ""),
    }
    try:
        return len(_size_json.dumps(env, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")) + 1
    except Exception:
        # Fallback: assume a modest full-envelope size if serialization fails.
        return 512


def _frame_json_size(frame: dict) -> int:
    """Back-compat shim — forwards to the full-envelope size calculation so
    the staging-time accumulator aligns with post-emit UTF-8 bytes."""
    return _frame_envelope_size(frame)


class _CaptureResult:
    """Bounded-capture staging record. Return value of `_capture_stack_frames`.

    Fields:
      - digest: short sha256 of the canonical frame listing (correlation id).
      - frames: the per-frame dicts that will be emitted (after bounds).
      - thread_count_total: actual threads at capture time.
      - thread_count_captured: threads whose frames made it into `frames`.
      - frames_captured_total: len(frames).
      - truncated: whether any cap was hit.
      - truncated_reason: PRIMARY reason, enum one of
        {none|threads|frames_per_thread|serialized_bytes}. Retained for
        backward compatibility — readers of this telemetry should prefer
        `truncated_reasons` when present.
      - truncated_reasons: list of all caps that fired, in evaluation order
        (threads > frames_per_thread > serialized_bytes). Empty when
        `truncated=False`. Added by the follow-up remediation.
      - staged_bytes_accumulator: internal byte-count during capture staging,
        used ONLY to decide when to stop staging frames. The emitted
        `serialized_bytes_actual` is the authoritative post-emit measurement
        (see `_emit_stack_capture`).
    """

    __slots__ = (
        "digest",
        "frames",
        "thread_count_total",
        "thread_count_captured",
        "frames_captured_total",
        "truncated",
        "truncated_reason",
        "truncated_reasons",
        "staged_bytes_accumulator",
    )

    def __init__(self) -> None:
        self.digest: str = ""
        self.frames: list[dict] = []
        self.thread_count_total: int = 0
        self.thread_count_captured: int = 0
        self.frames_captured_total: int = 0
        self.truncated: bool = False
        self.truncated_reason: str = "none"
        self.truncated_reasons: list[str] = []
        self.staged_bytes_accumulator: int = 0


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

        # Cap 2: frames per thread. Retain the INNERMOST (deepest) frames —
        # those are the ones currently executing, which carry the actual
        # diagnostic signal for a CPU-pin. `traceback.extract_stack` returns
        # frames outermost-first, so a tail slice `[-N:]` keeps the deepest
        # N (defect 4 remediation; the previous slice `[:N]` dropped exactly
        # the frames we needed).
        thread_frames = extracted[-STACK_CAPTURE_MAX_FRAMES_PER_THREAD:]
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
            # Cap 3: cumulative serialized bytes (staging-time accumulator
            # drives truncation; the emitted `serialized_bytes_actual` is
            # the post-emit measurement computed in `_emit_stack_capture`).
            # Reserve HEADER_BYTES_BUDGET for the stack_capture header so the
            # final aggregate (header + frames) stays under the cap.
            frame_bytes = _frame_json_size(frame_dict)
            projected = result.staged_bytes_accumulator + frame_bytes
            effective_max = STACK_CAPTURE_MAX_SERIALIZED_BYTES - STACK_CAPTURE_HEADER_BYTES_BUDGET
            if projected > effective_max:
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
            result.staged_bytes_accumulator = projected
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
    """Populate `truncated`, `truncated_reason` (primary, earliest-wins) and
    `truncated_reasons` (bounded list, all caps that fired, in evaluation
    order).

    Primary reason retained for backward compatibility; readers of this
    telemetry should prefer `truncated_reasons` when present.
    """
    reasons: list[str] = []
    if threads_truncated:
        reasons.append("threads")
    if frames_truncated_any:
        reasons.append("frames_per_thread")
    if bytes_truncated:
        reasons.append("serialized_bytes")
    if reasons:
        result.truncated = True
        result.truncated_reason = reasons[0]
        result.truncated_reasons = reasons
    else:
        result.truncated = False
        result.truncated_reason = "none"
        result.truncated_reasons = []


def _envelope_bytes(
    event: str,
    *,
    severity: str,
    component: str,
    **fields: Any,
) -> int:
    """Return the exact UTF-8 byte count that `emit()` will write for the
    given event + fields, including the trailing newline.

    Mirrors the `emit()` serialization contract (json.dumps with
    ensure_ascii=False + sort_keys=True + default=str, then +1 for newline).
    Reserved envelope keys cannot be overridden by caller fields; same
    precedence as `emit()`.
    """
    env: dict[str, Any] = {
        "event": event,
        "schema_version": "1",
        "severity": severity if severity in {"DEBUG", "INFO", "NOTICE", "WARNING", "ERROR"} else "INFO",
        "component": component,
        "instance_id": _emit_instance_id(),
    }
    for k, v in fields.items():
        if k in env:
            continue
        env[k] = v
    try:
        return len(_size_json.dumps(env, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")) + 1
    except Exception:
        # Serialization failure here is itself unlikely; return a sentinel
        # that will tend to overestimate rather than overflow silently.
        return 1024


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

    `serialized_bytes_actual` is the post-measurement aggregate UTF-8 byte
    count across THIS header event plus every `stack_frames` event that will
    follow. Replaces the pre-emit staging estimate shipped in the Scope 1
    remediation. The value is computed by fixed-point iteration: the header's
    own byte length depends on the digit count of `serialized_bytes_actual`,
    so we rebuild the header with the running aggregate until the length
    stabilizes (converges in at most a handful of iterations because
    digit-count grows only on crossing powers of ten and the aggregate is
    bounded above by STACK_CAPTURE_MAX_SERIALIZED_BYTES).

    Legacy form (kept for the pre-remediation no-sensitive-labels test and
    any ad-hoc caller that only had `thread_count` + `digest`): pass those
    kwargs instead. Truncation fields default to the "no truncation known"
    representation.
    """
    if result is not None:
        base_fields: dict[str, Any] = {
            "stall_seconds": round(stall_seconds, 3),
            "thread_count": int(result.thread_count_captured),
            "thread_count_captured": int(result.thread_count_captured),
            "thread_count_total": int(result.thread_count_total),
            "frames_captured_total": int(result.frames_captured_total),
            "truncated": bool(result.truncated),
            "truncated_reason": str(result.truncated_reason),
            "truncated_reasons": list(result.truncated_reasons),
            "frames_sha256": result.digest,
        }
        # Sum the exact bytes every stack_frames event will emit.
        frame_bytes_total = 0
        for f in result.frames:
            frame_bytes_total += _envelope_bytes(
                "stack_frames",
                severity="WARNING",
                component="heartbeat_watchdog",
                frames_sha256=result.digest,
                frame_index=f["frame_index"],
                thread_name=f["thread_name"],
                file=f["file"],
                function=f["function"],
                line=f["line"],
            )
        # Fixed-point iteration on the aggregate — the header's byte length
        # depends on the digit count of `serialized_bytes_actual`.
        aggregate = 0
        for _ in range(8):
            header_bytes = _envelope_bytes(
                "stack_capture",
                severity="WARNING",
                component="heartbeat_watchdog",
                serialized_bytes_actual=int(aggregate),
                **base_fields,
            )
            next_aggregate = header_bytes + frame_bytes_total
            if next_aggregate == aggregate:
                break
            aggregate = next_aggregate
        emit(
            "stack_capture",
            severity="WARNING",
            component="heartbeat_watchdog",
            serialized_bytes_actual=int(aggregate),
            **base_fields,
        )
        return
    # Legacy call-shape — ad-hoc callers that only knew thread_count + digest.
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
        truncated_reasons=[],
        serialized_bytes_actual=0,
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
    except Exception:
        # Recovery emission failure is also swallowed — the whole point is
        # that nothing from this path can terminate the thread. Follow-up
        # narrows this from `BaseException` to `Exception` so SystemExit,
        # KeyboardInterrupt, GeneratorExit, and asyncio.CancelledError still
        # propagate as intended.
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
        except Exception:
            # A sleep failure is itself unrecoverable (clock corruption etc.)
            # Return cleanly; the daemon thread exits. Follow-up narrows
            # from BaseException to Exception — SystemExit / KeyboardInterrupt
            # / GeneratorExit / asyncio.CancelledError now propagate.
            return
        if state.stop_event.is_set():
            return

        # ----- iteration body — any Exception is swallowed; BaseException
        # subclasses (SystemExit, KeyboardInterrupt, GeneratorExit,
        # asyncio.CancelledError) propagate so the parent context can act.
        try:
            if pending_cooldown:
                # Post-capture cooldown: observe a longer wait before
                # re-detecting the SAME stall. The outer loop's
                # `sleep_fn(check_interval_s)` ran once; add the stall-
                # threshold delta here. Still unconditional sleep.
                try:
                    sleep_fn(stall_threshold_s)
                except Exception:
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
        except Exception:
            # Any `Exception` subclass inside the iteration body (capture
            # raise, emission raise, enumerate raise, serialization raise)
            # must NOT terminate the thread. Record a recovery event and
            # continue to the next iteration. The recovery emission is
            # itself best-effort. Follow-up narrows this from BaseException
            # to Exception so SystemExit / KeyboardInterrupt / GeneratorExit
            # / asyncio.CancelledError propagate instead of being swallowed.
            recovery_count += 1
            try:
                _emit_watchdog_recovery(
                    recovery_limiter,
                    recovery_count,
                    time_fn() if callable(time_fn) else time.time(),
                )
            except Exception:
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
