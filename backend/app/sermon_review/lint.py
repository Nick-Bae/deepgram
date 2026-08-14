"""Sermon manuscript linter.

Detects patterns that trip up PMM's commit-time cursor-sync — chiefly
segments that repeat scripture or refrain phrases at multiple positions
in the manuscript. When the pastor reads the first occurrence, the
fuzzy matcher returns all occurrences; without the "nearest forward
match" cursor-sync fix, the cursor would leap to the last one.

Pure-function API so it works both on Firestore-loaded sermons (via
the route handler) and on .xlsx workbooks (via the CLI script).
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_STRIP_RE = re.compile(r'[\s"“”‘’「」『』.,·!?…()（）]+')

MIN_SUBSTRING_CHARS = 8
"""Skip matches shorter than this — noise-suppression threshold. Filters
common single-word repetitions ('하나님', '아멘', '예수님', '감사') while
still catching short scripture citations like '나는 전능한 하나님이라'
(10 normalized chars) and '내가 너의 하나님이 되겠다' (11)."""

MIN_GAP = 10
"""Only flag pairs at least this many segments apart. Adjacent duplicates
(intentional near-repetitions in a call-and-response section) shouldn't
appear as landmines — the cursor won't leap far enough to lose PMM."""


def _normalize(text: str) -> str:
    """Strip punctuation/whitespace so quoted vs unquoted variants of the
    same phrase register as identical."""
    return _STRIP_RE.sub("", text or "")


def _segment_index(segment_id: str) -> int:
    """S013 → 13. Returns -1 for unparseable IDs (shouldn't happen in
    practice — sermon-review IDs are always S+digits)."""
    if not segment_id or not segment_id.startswith("S"):
        return -1
    try:
        return int(segment_id[1:])
    except ValueError:
        return -1


def lint_sermon_segments(segments: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Analyze a sermon's segments for PMM-risky patterns.

    Args:
        segments: iterable of dicts with at least 'segmentId' and 'original'
            (the Korean source text). Extra fields are ignored.

    Returns:
        {
            'totalSegments': int,
            'collisions': [
                {
                    'shorterSegmentId': 'S051',
                    'longerSegmentId':  'S015',
                    'gap': 36,
                    'matchedText': '나는 전능한 하나님이라',
                    'severity': 'high' | 'medium',
                }, ...
            ],
        }
    """
    items: list[tuple[str, str, str]] = []
    for seg in segments:
        sid = str(seg.get("segmentId") or "").strip()
        ko = str(seg.get("original") or "").strip()
        if not sid or not ko:
            continue
        items.append((sid, ko, _normalize(ko)))

    collisions: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()

    for i, (sid_a, _ko_a, norm_a) in enumerate(items):
        if len(norm_a) < MIN_SUBSTRING_CHARS:
            continue
        for sid_b, _ko_b, norm_b in items[i + 1:]:
            if len(norm_b) < MIN_SUBSTRING_CHARS:
                continue
            # Determine which is the shorter (potential substring) side.
            if len(norm_a) <= len(norm_b):
                shorter_sid, shorter_norm = sid_a, norm_a
                longer_sid, longer_norm = sid_b, norm_b
            else:
                shorter_sid, shorter_norm = sid_b, norm_b
                longer_sid, longer_norm = sid_a, norm_a
            if shorter_norm not in longer_norm:
                continue
            gap = abs(_segment_index(sid_a) - _segment_index(sid_b))
            if gap < MIN_GAP:
                continue
            key = (shorter_sid, longer_sid)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            # Recover the matched substring in its unnormalized form by
            # snipping the original Korean of the shorter segment — the
            # normalized form drops punctuation and would render oddly
            # in a UI.
            matched_display = _ko_a if sid_a == shorter_sid else _ko_b
            collisions.append(
                {
                    "shorterSegmentId": shorter_sid,
                    "longerSegmentId": longer_sid,
                    "gap": gap,
                    "matchedText": matched_display,
                    "severity": "high" if gap >= 30 else "medium",
                }
            )

    # Sort collisions by descending gap — biggest jumps (worst cursor-sync
    # risks) surface first.
    collisions.sort(key=lambda c: (-c["gap"], c["shorterSegmentId"]))

    return {
        "totalSegments": len(items),
        "collisions": collisions,
    }
