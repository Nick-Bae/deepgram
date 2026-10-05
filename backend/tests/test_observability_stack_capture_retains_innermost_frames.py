"""Guard defect-4 remediation: frame truncation retains the INNERMOST
(deepest) frames, which are the ones currently executing.

The base at commit 7cf47d47 keeps `extracted[:MAX]`, the OUTERMOST frames.
That drops the deepest frames, which is exactly the diagnostic signal we
need for CPU-pin investigation. After remediation, truncation uses
`extracted[-MAX:]`.
"""
from __future__ import annotations

import json
import pathlib
import sys
import types
from io import StringIO
from unittest.mock import patch

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


_SENTINEL_DEEPEST_FN = "DEEPEST_SENTINEL_FN"
_SENTINEL_DEEPEST_FILE = "deepest_sentinel.py"
_SENTINEL_OUTERMOST_FN = "OUTERMOST_SENTINEL_FN"
_SENTINEL_OUTERMOST_FILE = "outermost_sentinel.py"


def _parse_stack_frames(stdout: str) -> list[dict]:
    rows: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("event") == "stack_frames":
            rows.append(d)
    return rows


def _run_capture(fake_frames: list[tuple]) -> set[str]:
    """Drive the real capture + emit pipeline with a single mocked thread
    whose stack is `fake_frames` (outermost → innermost). Returns the set
    of function names in the emitted `stack_frames` events."""
    from app.observability import heartbeat_watchdog as hw

    class _StubFrame: pass
    stub_frame = _StubFrame()

    buf = StringIO()
    with patch("sys.stdout", buf), \
         patch("app.observability.heartbeat_watchdog.sys._current_frames",
               return_value={1: stub_frame}), \
         patch("app.observability.heartbeat_watchdog.threading.enumerate",
               return_value=[_FakeThread(1, "MainThread")]), \
         patch("app.observability.heartbeat_watchdog.traceback.extract_stack",
               return_value=fake_frames):
        result = hw._capture_stack_frames()
        hw._emit_stack_capture(stall_seconds=5.1, result=result)
        hw._emit_stack_frames(result.digest, result.frames)

    frames = _parse_stack_frames(buf.getvalue())
    return {f.get("function") for f in frames}


def test_deepest_frame_retained_when_stack_exceeds_cap():
    """Build a 300-deep synthetic stack with a unique sentinel in the
    deepest frame (index 299, innermost). The emitted frames MUST include
    the deepest sentinel. The outermost sentinel at index 0 may or may
    not be included, but it's acceptable for it to be dropped — the
    innermost is what matters."""
    fake_frames = []
    for i in range(300):
        is_outer = i == 0
        is_inner = i == 299
        file = (
            _SENTINEL_OUTERMOST_FILE if is_outer
            else _SENTINEL_DEEPEST_FILE if is_inner
            else f"filler_{i}.py"
        )
        func = (
            _SENTINEL_OUTERMOST_FN if is_outer
            else _SENTINEL_DEEPEST_FN if is_inner
            else f"filler_fn_{i}"
        )
        fake_frames.append((file, 10 + i, func, None))

    functions = _run_capture(fake_frames)
    assert _SENTINEL_DEEPEST_FN in functions, (
        f"deepest frame dropped by truncation; emitted functions "
        f"sample: {sorted(functions)[:5]}... (total={len(functions)})"
    )


def test_outermost_frames_dropped_when_stack_exceeds_cap():
    """Mirror of the above: when the stack is deeper than the per-thread
    cap, the OUTERMOST frames should be the ones dropped. This test
    hardens the ordering choice."""
    from app.observability.constants import STACK_CAPTURE_MAX_FRAMES_PER_THREAD

    depth = STACK_CAPTURE_MAX_FRAMES_PER_THREAD + 50  # 50 above the cap
    fake_frames = []
    for i in range(depth):
        file = (
            _SENTINEL_OUTERMOST_FILE if i == 0
            else _SENTINEL_DEEPEST_FILE if i == depth - 1
            else f"filler_{i}.py"
        )
        func = (
            _SENTINEL_OUTERMOST_FN if i == 0
            else _SENTINEL_DEEPEST_FN if i == depth - 1
            else f"filler_fn_{i}"
        )
        fake_frames.append((file, 10 + i, func, None))

    functions = _run_capture(fake_frames)
    assert _SENTINEL_OUTERMOST_FN not in functions, (
        "outermost frame should have been truncated out"
    )
    assert _SENTINEL_DEEPEST_FN in functions, (
        "deepest frame should always be retained"
    )


class _FakeThread:
    def __init__(self, ident: int, name: str) -> None:
        self.ident = ident
        self.name = name
