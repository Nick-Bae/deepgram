# Design Ref: §2.2 Live Broadcast hook + §10.4 — pure function called before
# the existing machine-output fallback in the live translation pipeline.
# Module-3 ships this function; main.py splice into _translate_text_guarded
# is a deliberate follow-up to avoid risking the production WS path.

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

# Same default threshold as the sermon-accuracy fuzzy-match work; keep us in
# the same ballpark so behavior is predictable for ops.
DEFAULT_THRESHOLD: float = 0.84


_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    return _WS_RE.sub(" ", cleaned)


def _similarity(a: str, b: str) -> float:
    a_norm, b_norm = _norm(a), _norm(b)
    if not a_norm or not b_norm:
        return 0.0
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def get_reviewed_text(
    *,
    store: Any,
    org_id: str,
    service_key: str | None,
    korean_text: str,
    threshold: float = DEFAULT_THRESHOLD,
) -> str | None:
    """Return the reviewed English translation for `korean_text` if a linked
    sermon has a segment matching above `threshold`. None otherwise.

    Skip semantics (Design FR-15): segments with status=Skip are treated as
    'not a match' so the broadcast falls back to machine output instead of
    showing reviewed text the user marked to skip.
    """
    if not service_key or not korean_text:
        return None

    service = _get_service(store, org_id, service_key)
    sermon_id = (service or {}).get("linkedSermonId") if service else None
    if not sermon_id:
        return None

    sermon = store.get_review_sermon(org_id, sermon_id)
    if not sermon:
        return None

    best_score = 0.0
    best_text: str | None = None
    for segment in sermon.get("segments") or []:
        status = str(segment.get("status") or "Draft")
        if status == "Skip":
            continue
        score = _similarity(korean_text, segment.get("original") or "")
        if score > best_score:
            best_score = score
            reviewed = (segment.get("reviewedTranslation") or "").strip()
            app_trans = (segment.get("appTranslation") or "").strip()
            best_text = reviewed if reviewed else app_trans

    if best_score >= threshold and best_text:
        return best_text
    return None


def _get_service(store: Any, org_id: str, service_key: str) -> dict[str, Any] | None:
    """Tolerant service lookup — supports both InMemory (._services dict) and
    Firestore variants without coupling lookup to a single API shape."""
    if hasattr(store, "_services"):
        svc = store._services.get((org_id, service_key))  # type: ignore[attr-defined]
        return dict(svc) if svc else None
    getter = getattr(store, "get_service", None)
    if callable(getter):
        try:
            return getter(org_id, service_key)
        except Exception:
            return None
    return None
