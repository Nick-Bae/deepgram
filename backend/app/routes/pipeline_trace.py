"""Client-echo endpoint for pipeline_trace timings.

The listener client posts {utteranceId, receivedAt, renderedAt} after
rendering a caption. We log it as a single [PIPELINE_TRACE_CLIENT]
line for out-of-band correlation with the server-side [PIPELINE_TRACE]
line (both keyed on utteranceId).

No auth: this is public observability and only accepts trace metadata,
never message content. Fire-and-forget from the client side.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()


@router.post("/pipeline_trace")
async def receive_client_trace(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "reason": "invalid_json"}

    if not isinstance(payload, dict):
        return {"ok": False, "reason": "invalid_payload"}

    trimmed = {
        "utteranceId": str(payload.get("utteranceId") or ""),
        "receivedAt": _coerce_ms(payload.get("receivedAt")),
        "renderedAt": _coerce_ms(payload.get("renderedAt")),
    }
    if not trimmed["utteranceId"]:
        return {"ok": False, "reason": "missing_utterance_id"}

    print(f"[PIPELINE_TRACE_CLIENT] {json.dumps(trimmed, separators=(',', ':'))}")
    return {"ok": True}


def _coerce_ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
