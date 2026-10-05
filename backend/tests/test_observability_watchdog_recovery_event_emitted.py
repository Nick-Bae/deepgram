"""watchdog_recovery event — low-cardinality payload only.

After a capture or emission exception, the watchdog emits a rate-limited
`watchdog_recovery` event. The event MUST NOT include exception text, args,
locals, stack text, or any identifier beyond the fixed envelope +
`recovery_count_since_start`.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _events(captured: str, name: str) -> list[dict]:
    out: list[dict] = []
    for line in captured.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("event") == name:
            out.append(obj)
    return out


_FORBIDDEN_RECOVERY_FIELDS = {
    "exception",
    "exception_type",
    "exc",
    "exc_type",
    "message",
    "msg",
    "args",
    "arguments",
    "stack",
    "stack_text",
    "traceback",
    "tb",
    "error_text",
    "err",
    "err_text",
    "err_message",
    "locals",
    "org_id",
    "room_id",
    "uid",
    "user_id",
    "email",
    "token",
    "authorization",
    "cookie",
    "secret",
    "url",
}


def test_recovery_event_shape_is_minimal(capsys, monkeypatch):
    try:
        from app.observability import heartbeat_watchdog as hbw
        from app.observability.constants import (
            STACK_CAPTURE_HOUR_CAP,
            STACK_CAPTURE_MIN_INTERVAL_S,
            STACK_CAPTURE_WINDOW_S,
        )
    except ImportError as exc:
        pytest.fail(f"heartbeat_watchdog missing — implementation gated. {exc}")

    state = hbw.HeartbeatState()
    state.counter = 1
    state.last_tick_ts = 100.0

    rate_limiter = hbw._RateLimiter(
        min_interval_s=STACK_CAPTURE_MIN_INTERVAL_S,
        hour_cap=STACK_CAPTURE_HOUR_CAP,
        window_s=STACK_CAPTURE_WINDOW_S,
    )

    def always_raise() -> tuple[str, list[dict]]:
        # Include a secret-looking string so the test catches leakage into
        # the recovery payload.
        raise RuntimeError("SENSITIVE:token=abc123 uid=XXX email=x@y")

    monkeypatch.setattr(hbw, "_capture_stack_frames", always_raise)

    now_container = [100.0]
    sleep_calls = {"n": 0}

    def fake_time() -> float:
        return now_container[0]

    def fake_sleep(dt: float) -> None:
        now_container[0] += dt
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 10:
            state.stop_event.set()

    hbw._watchdog_run(
        state,
        check_interval_s=1.0,
        stall_threshold_s=2.0,
        rate_limiter=rate_limiter,
        time_fn=fake_time,
        sleep_fn=fake_sleep,
    )

    captured = capsys.readouterr().out
    recoveries = _events(captured, "watchdog_recovery")
    assert len(recoveries) >= 1, (
        f"expected at least one watchdog_recovery event; stdout tail: {captured[-800:]!r}"
    )

    # Verify the sensitive string from the raised exception does not appear
    # anywhere in the recovery emissions.
    for ev in recoveries:
        serialized = json.dumps(ev, sort_keys=True)
        assert "SENSITIVE" not in serialized, f"recovery leaked exception content: {serialized}"
        assert "token=abc123" not in serialized
        assert "email=x@y" not in serialized

        # Only the fixed envelope + recovery_count_since_start permitted.
        for forbidden in _FORBIDDEN_RECOVERY_FIELDS:
            assert forbidden not in ev, (
                f"recovery payload leaked forbidden field '{forbidden}': {ev}"
            )

        # Required fields.
        assert ev["event"] == "watchdog_recovery"
        assert ev["component"] == "heartbeat_watchdog"
        assert ev["schema_version"] == "1"
        assert ev["severity"] == "WARNING"
        assert "recovery_count_since_start" in ev
        assert isinstance(ev["recovery_count_since_start"], int)
        assert ev["recovery_count_since_start"] >= 1
