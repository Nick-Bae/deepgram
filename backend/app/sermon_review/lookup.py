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
_COMPACT_RE = re.compile(r"[^0-9A-Za-z가-힣]+")
_MIN_CONTAINED_CHARS = 8
_MIN_SEQUENCE_COVERAGE = 0.55


def _norm(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    return _WS_RE.sub(" ", cleaned)


def _compact(text: str) -> str:
    return _COMPACT_RE.sub("", (text or "")).lower()


def _similarity(a: str, b: str) -> float:
    a_norm, b_norm = _norm(a), _norm(b)
    if not a_norm or not b_norm:
        return 0.0
    compact_a, compact_b = _compact(a_norm), _compact(b_norm)
    if not compact_a or not compact_b:
        return 0.0
    if compact_a == compact_b:
        return 1.0
    shorter, longer = (
        (compact_a, compact_b)
        if len(compact_a) <= len(compact_b)
        else (compact_b, compact_a)
    )
    if len(shorter) >= _MIN_CONTAINED_CHARS and shorter in longer:
        coverage = len(shorter) / max(1, len(longer))
        return max(0.85, min(0.93, 0.74 + (coverage * 0.20)))
    return max(
        SequenceMatcher(None, a_norm, b_norm).ratio(),
        SequenceMatcher(None, compact_a, compact_b).ratio(),
    )


def _segment_text(segment: dict[str, Any]) -> str:
    reviewed = (segment.get("reviewedTranslation") or "").strip()
    app_trans = (segment.get("appTranslation") or "").strip()
    return reviewed if reviewed else app_trans


def _reviewed_segment_matches(
    korean_text: str,
    segments: list[dict[str, Any]],
    *,
    exclude_segment_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    live_compact = _compact(korean_text)
    if not live_compact:
        return []

    excluded = exclude_segment_ids or set()
    matches: list[tuple[int, int, int, dict[str, Any]]] = []
    for segment in segments:
        segment_id = str(segment.get("segmentId") or "").strip()
        if segment_id and segment_id in excluded:
            continue
        status = str(segment.get("status") or "Draft")
        if status == "Skip":
            continue
        original = str(segment.get("original") or "").strip()
        original_compact = _compact(original)
        if len(original_compact) < _MIN_CONTAINED_CHARS:
            continue
        pos = live_compact.find(original_compact)
        if pos < 0:
            continue
        text = _segment_text(segment)
        if not text:
            continue
        order = int(segment.get("order") or len(matches) + 1)
        matches.append(
            (
                pos,
                order,
                len(original_compact),
                {
                    "segmentId": segment_id,
                    "order": order,
                    "original": original,
                    "reviewedText": text,
                },
            )
        )

    matches.sort(key=lambda item: (item[0], item[1]))
    return [item[3] for item in matches]


def _multi_segment_text(
    korean_text: str,
    segments: list[dict[str, Any]],
) -> str | None:
    live_compact = _compact(korean_text)
    if not live_compact:
        return None

    matches = _reviewed_segment_matches(korean_text, segments)
    if len(matches) < 2:
        return None

    coverage = sum(len(_compact(item["original"])) for item in matches) / max(
        1, len(live_compact)
    )
    if coverage < _MIN_SEQUENCE_COVERAGE:
        return None

    return " ".join(item["reviewedText"] for item in matches)


def get_reviewed_matches(
    *,
    store: Any,
    org_id: str,
    service_key: str | None,
    korean_text: str,
    exclude_segment_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return reviewed segment matches contained in `korean_text`.

    This is used by live partial handling: if Deepgram has already spoken a
    whole uploaded segment, the broadcast can emit that segment's reviewed
    translation immediately without waiting for sentence-final punctuation.
    """
    if not service_key or not korean_text:
        return []

    service = _get_service(store, org_id, service_key)
    sermon_id = (service or {}).get("linkedSermonId") if service else None
    if not sermon_id:
        return []

    sermon = store.get_review_sermon(org_id, sermon_id)
    if not sermon:
        return []

    segments = list(sermon.get("segments") or [])
    return _reviewed_segment_matches(
        korean_text,
        segments,
        exclude_segment_ids=exclude_segment_ids,
    )


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

    segments = list(sermon.get("segments") or [])
    multi_text = _multi_segment_text(korean_text, segments)
    if multi_text:
        return multi_text

    best_score = 0.0
    best_text: str | None = None
    for segment in segments:
        status = str(segment.get("status") or "Draft")
        if status == "Skip":
            continue
        score = _similarity(korean_text, segment.get("original") or "")
        if score > best_score:
            best_score = score
            best_text = _segment_text(segment)

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
