"""
Structured security event logging.

All security-relevant events are emitted as JSON to the "security" logger.
On Cloud Run, stdout is ingested by Cloud Logging and the `jsonPayload` fields
become queryable — use severity=WARNING/ERROR to trigger alerts.

Usage:
    from app.security_log import security_event
    security_event("ws_auth_rejected", ip=client_ip, org_id=org_id, detail="no_token")
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

_logger = logging.getLogger("security")

# Honour the same TRUSTED_PROXY_COUNT env var used by the rate-limit middleware.
_TRUSTED_PROXY_COUNT: int = max(
    0, min(10, int((os.getenv("TRUSTED_PROXY_COUNT") or "0").strip() or "0"))
)
_IP_RE = re.compile(r"^[\d.]+$|^[0-9a-fA-F:]+$")


def client_ip(request: Any) -> str:
    """Extract the real client IP from a Starlette Request, respecting trusted proxies."""
    if _TRUSTED_PROXY_COUNT > 0:
        forwarded = (getattr(request.headers, "get", lambda k, d="": d)("x-forwarded-for") or "").strip()
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",")]
            idx = len(parts) - _TRUSTED_PROXY_COUNT
            if idx >= 0:
                ip = parts[idx]
                if ip and _IP_RE.match(ip):
                    return ip
    conn = getattr(request, "client", None)
    host = getattr(conn, "host", None)
    return str(host or "unknown")


def security_event(
    event: str,
    *,
    severity: str = "WARNING",
    uid: str | None = None,
    ip: str | None = None,
    org_id: str | None = None,
    method: str | None = None,
    path: str | None = None,
    status: int | None = None,
    detail: str | None = None,
    **extra: Any,
) -> None:
    """
    Emit a structured security event.

    All non-None kwargs are included as fields in the JSON payload so that
    Cloud Logging / log aggregators can filter and alert on them.
    """
    record: dict[str, Any] = {
        "event": event,
        "severity": severity,
        "ts": int(time.time()),
    }
    if uid is not None:
        record["uid"] = uid
    if ip is not None:
        record["ip"] = ip
    if org_id is not None:
        record["org_id"] = org_id
    if method is not None:
        record["method"] = method
    if path is not None:
        record["path"] = path
    if status is not None:
        record["status"] = status
    if detail is not None:
        record["detail"] = detail
    for k, v in extra.items():
        if v is not None:
            record[k] = v

    level = logging.ERROR if severity == "ERROR" else logging.WARNING
    _logger.log(level, json.dumps(record))
