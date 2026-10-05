"""Hard rule: emitted events must not carry user-content or sensitive labels.

Iterates one representative emission from every observability event name and
asserts the JSON contains none of the forbidden keys/substrings. Also
inspects the module source files for any accidental logging of forbidden
field names.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


FORBIDDEN_KEYS = {
    "org_id",
    "room_id",
    "uid",
    "user_id",
    "email",
    "token",
    "idtoken",
    "password",
    "secret",
    "authorization",
    "cookie",
    "api_key",
    "stt_text",
    "transcript",
    "audio",
}


def _events(captured: str) -> list[dict]:
    out: list[dict] = []
    for line in captured.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        out.append(obj)
    return out


def test_representative_emissions_have_no_sensitive_keys(capsys):
    try:
        from app.observability._emit import emit
    except ImportError as exc:
        pytest.fail(f"observability._emit missing — implementation gated. {exc}")

    # Run one invocation of each sampler and the watchdog's emit paths.
    from app.observability.event_loop_lag import start_event_loop_lag_sampler
    from app.observability.task_count import start_task_count_sampler
    from app.observability.process_cpu import start_process_cpu_sampler
    from app.observability.executor_queue import start_executor_queue_sampler

    async def run_briefly() -> None:
        tasks = [
            start_event_loop_lag_sampler(emit_interval_s=0.1, sample_interval_s=0.01),
            start_task_count_sampler(emit_interval_s=0.1),
            start_process_cpu_sampler(emit_interval_s=0.1),
            start_executor_queue_sampler(emit_interval_s=0.1),
        ]
        try:
            await asyncio.sleep(0.4)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run_briefly())

    # Trigger the watchdog's suppressed-emit explicitly (safe, no stall).
    from app.observability.heartbeat_watchdog import _emit_suppressed, _emit_stack_capture, _emit_stack_frames
    _emit_suppressed("min_interval")
    _emit_stack_capture(stall_seconds=5.5, thread_count=4, digest="abcdef0123456789")
    _emit_stack_frames(
        "abcdef0123456789",
        [{"frame_index": 0, "thread_name": "MainThread", "file": "backend/app/main.py", "function": "foo", "line": 42}],
    )

    captured = capsys.readouterr().out
    events = _events(captured)
    assert events, f"expected emissions; got: {captured[:400]!r}"

    for ev in events:
        lower_keys = {str(k).lower() for k in ev.keys()}
        bad = lower_keys & FORBIDDEN_KEYS
        assert not bad, f"event {ev.get('event')!r} leaked forbidden key(s) {bad}: {ev}"
        # And no forbidden *values* either — defensive text scan.
        serialized = json.dumps(ev).lower()
        for term in ("bearer ", "idtoken=", "token=", "password=", "x-google-access-token"):
            assert term not in serialized, (
                f"event {ev.get('event')!r} contains forbidden substring {term!r}: {ev}"
            )


def test_module_sources_do_not_grep_sensitive_names():
    """A paranoia scan — observability module code must not reference these
    forbidden field names (unless commented out as a 'MUST NOT' example).
    """
    pkg_root = pathlib.Path(__file__).resolve().parents[1] / "app" / "observability"
    offenders: list[str] = []
    sensitive_fns = re.compile(
        r"(?<![a-z_])(stt_text|transcript_text|audio_bytes|password|cookie)(?![a-z_])",
        re.IGNORECASE,
    )
    for py in pkg_root.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for m in sensitive_fns.finditer(text):
            # Allow mentions inside a comment line that explicitly describes
            # the exclusion rule (e.g., docstrings).
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            if line_end < 0:
                line_end = len(text)
            line = text[line_start:line_end]
            if line.lstrip().startswith(("#", '"', "'")) or "NEVER" in line or "no " in line.lower():
                continue
            offenders.append(f"{py.name}: {line.strip()}")
    assert not offenders, f"observability source references forbidden names: {offenders}"
