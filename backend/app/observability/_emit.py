"""Single-line JSON stdout emitter shared across observability samplers.

Shape matches `backend/app/services/room_reconciler.py:_emit` — a single JSON
object per line with the fixed envelope `{event, schema_version, severity,
component, instance_id, message, ...}` plus caller-supplied numeric fields.

**Hard rule:** no user-content, no transcript text, no token/UID/secret, no
exception text, no high-cardinality labels. The caller must only pass numeric
values, bounded enums, or short deterministic hashes.

**`instance_id` is a STRUCTURED LOG FIELD, NOT a metric label.** Cloud Run
instance ids are unbounded (one per instance, changes per revision/scale).
The log-based-metric layer (`ops/monitoring/*`) must NOT reference
`instance_id` as a metric dimension — doing so creates cardinality explosion
in Cloud Monitoring. The only metric-label-safe fields in these emissions
are `component`, `event`, `schema_version`, and `severity`. `instance_id`
is here solely so a human (or a non-metric log query) can correlate events
that happened on the same container process.

Reserved-field precedence: caller kwargs cannot override the envelope fields
`event`, `schema_version`, `component`, `instance_id` — these are forced from
the function arguments. Any collision in kwargs is silently dropped; see
`_emit` for the explicit precedence order.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any


_SCHEMA_VERSION = "1"

_ALLOWED_SEVERITIES = frozenset({"DEBUG", "INFO", "NOTICE", "WARNING", "ERROR"})


def instance_id() -> str:
    """Resolve the process instance id without importing app.env (avoid a
    circular import at startup). Falls back to a stable local marker when the
    env var is unset — never raises."""
    raw = os.getenv("INSTANCE_ID") or ""
    cleaned = raw.strip()
    return cleaned or "<local>"


def emit(
    event: str,
    *,
    severity: str = "INFO",
    component: str,
    message: str = "",
    **fields: Any,
) -> None:
    """Emit one JSON log line.

    Caller fields are merged under the envelope: the four reserved envelope
    keys cannot be overridden by kwargs. Everything else goes straight through.
    Best-effort — any serialisation failure prints a single-line ERROR event
    and swallows the exception so a broken emission can never crash a
    sampler loop.
    """
    sev = severity if severity in _ALLOWED_SEVERITIES else "INFO"
    envelope: dict[str, Any] = {
        "event": event,
        "schema_version": _SCHEMA_VERSION,
        "severity": sev,
        "component": component,
        "instance_id": instance_id(),
    }
    # Strip reserved keys from caller fields (precedence: envelope wins).
    safe_fields = {k: v for k, v in fields.items() if k not in envelope}
    envelope.update(safe_fields)
    if message:
        envelope.setdefault("message", message)
    try:
        line = json.dumps(envelope, ensure_ascii=False, sort_keys=True, default=str)
    except Exception as exc:  # pragma: no cover — serialisation failure path
        fallback = {
            "event": "observability_emit_error",
            "schema_version": _SCHEMA_VERSION,
            "severity": "ERROR",
            "component": "observability",
            "instance_id": instance_id(),
            "err_type": type(exc).__name__,
        }
        try:
            sys.stdout.write(json.dumps(fallback, sort_keys=True) + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        return
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass
