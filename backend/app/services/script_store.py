"""In-memory org-scoped store for pre-script pairs used by the hybrid admin console."""

from dataclasses import dataclass, field
from difflib import SequenceMatcher
import re
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple


def _norm(text: str) -> str:
    """Lowercase + collapse whitespace for fuzzy matching."""
    return " ".join((text or "").split()).lower()


_ALNUM_HANGUL_RE = re.compile(r"[^\w\u3131-\u318e\uac00-\ud7a3]+")


def _norm_compact(text: str) -> str:
    """
    Lowercase + remove punctuation/symbol spacing noise.
    Keeps Hangul, latin letters, and digits to improve STT/script matching.
    """
    base = _norm(text)
    return _ALNUM_HANGUL_RE.sub("", base)


def _partial_ratio(a: str, b: str) -> float:
    """
    Best-window similarity between two strings.
    Equivalent to fuzzy "partial ratio" behavior and robust when STT includes
    repeated/extra text around the scripted sentence.
    """
    if not a or not b:
        return 0.0
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if not short:
        return 0.0
    if short in long:
        return 1.0

    matcher = SequenceMatcher(None, short, long)
    best = 0.0
    for block in matcher.get_matching_blocks():
        # Align a same-length window in the longer string near each matching block.
        start = max(0, block.b - block.a)
        window = long[start : start + len(short)]
        if not window:
            continue
        score = SequenceMatcher(None, short, window).ratio()
        if score > best:
            best = score
    return best


def _similarity(a: str, b: str) -> float:
    """
    Robust fuzzy score for STT/script pairs.
    - raw ratio: tolerant to minor edits
    - compact ratio: resilient to punctuation/spacing
    - containment bonus: resilient when one side has extra surrounding text
    """
    norm_a = _norm(a)
    norm_b = _norm(b)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0

    compact_a = _norm_compact(norm_a)
    compact_b = _norm_compact(norm_b)
    if compact_a and compact_a == compact_b:
        return 1.0

    raw_ratio = SequenceMatcher(None, norm_a, norm_b).ratio()
    compact_ratio = SequenceMatcher(None, compact_a, compact_b).ratio() if compact_a and compact_b else 0.0
    partial_raw = _partial_ratio(norm_a, norm_b)
    partial_compact = _partial_ratio(compact_a, compact_b) if compact_a and compact_b else 0.0

    containment = 0.0
    if compact_a and compact_b:
        shorter_len = min(len(compact_a), len(compact_b))
        longer_len = max(len(compact_a), len(compact_b))
        if shorter_len >= 12 and (compact_a in compact_b or compact_b in compact_a):
            # If one side contains the other, prioritize this strongly.
            # STT often adds leading/trailing words around the scripted sentence.
            containment = max(0.86, min(0.99, (shorter_len / longer_len) + 0.24))

    return max(raw_ratio, compact_ratio, containment, partial_raw * 0.98, partial_compact * 0.98)


@dataclass
class ScriptPair:
    source: str
    target: str
    index: int


@dataclass
class ScriptBuffer:
    pairs: List[ScriptPair] = field(default_factory=list)
    threshold: float = 0.84
    version: int = 0
    sermons: Dict[str, dict[str, Any]] = field(default_factory=dict)


class ScriptStore:
    """
    Very small in-memory buffer to hold uploaded bilingual pairs.
    Designed for interactive use; does not persist to disk.
    """

    _GLOBAL_KEY = "__global__"

    def __init__(self):
        self._buffers: Dict[str, ScriptBuffer] = {}
        self._lock = Lock()

    def _org_key(self, org_id: Optional[str]) -> str:
        clean = (org_id or "").strip()
        return clean or self._GLOBAL_KEY

    def load(
        self,
        pairs: List[dict],
        threshold: float | None = None,
        *,
        org_id: Optional[str] = None,
    ) -> Tuple[int, float, int]:
        """
        Replace the buffer with the provided pairs.
        Returns (count_loaded, threshold_used, new_version).
        """
        cleaned: List[ScriptPair] = []
        for idx, pair in enumerate(pairs):
            src = (pair.get("source") or "").strip()
            tgt = (pair.get("target") or "").strip()
            if not src or not tgt:
                continue
            cleaned.append(ScriptPair(source=src, target=tgt, index=idx))

        with self._lock:
            key = self._org_key(org_id)
            buffer = self._buffers.get(key) or ScriptBuffer()
            buffer.pairs = cleaned
            if threshold is not None:
                buffer.threshold = max(0.0, min(1.0, float(threshold)))
            buffer.version += 1
            self._buffers[key] = buffer
            return len(buffer.pairs), buffer.threshold, buffer.version

    def clear(self, *, org_id: Optional[str] = None) -> Tuple[int, int]:
        """Clear all pairs; returns (removed_count, new_version)."""
        with self._lock:
            key = self._org_key(org_id)
            buffer = self._buffers.get(key) or ScriptBuffer()
            removed = len(buffer.pairs)
            buffer.pairs = []
            buffer.sermons = {}
            buffer.version += 1
            self._buffers[key] = buffer
            return removed, buffer.version

    def stats(self, *, org_id: Optional[str] = None) -> Tuple[int, float, int]:
        """Return (count, threshold, version) without exposing internal list."""
        with self._lock:
            key = self._org_key(org_id)
            buffer = self._buffers.get(key) or ScriptBuffer()
            return len(buffer.pairs), buffer.threshold, buffer.version

    def save_sermon(
        self,
        sermon: dict[str, Any],
        *,
        org_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Store a finalized sermon payload keyed by sermon_id.
        This does not change the live pair-matching version counter.
        """
        sermon_id = str((sermon or {}).get("sermon_id") or "").strip()
        if not sermon_id:
            raise ValueError("invalid_sermon_id")

        normalized = {
            "sermon_id": sermon_id,
            "threshold": float((sermon or {}).get("threshold", 0.84)),
            "lang_src": str((sermon or {}).get("lang_src") or "ko").strip() or "ko",
            "lang_tgt": str((sermon or {}).get("lang_tgt") or "en").strip() or "en",
            "segments": list((sermon or {}).get("segments") or []),
        }

        with self._lock:
            key = self._org_key(org_id)
            buffer = self._buffers.get(key) or ScriptBuffer()
            buffer.sermons[sermon_id] = normalized
            self._buffers[key] = buffer
            return dict(normalized)

    def get_sermon(self, sermon_id: str, *, org_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Return a finalized sermon payload by sermon_id, if it exists."""
        clean_sermon_id = (sermon_id or "").strip()
        if not clean_sermon_id:
            return None
        with self._lock:
            key = self._org_key(org_id)
            buffer = self._buffers.get(key) or ScriptBuffer()
            hit = buffer.sermons.get(clean_sermon_id)
            return dict(hit) if hit else None

    def match(self, text: str, *, org_id: Optional[str] = None) -> Tuple[Optional[ScriptPair], float, int, float]:
        """
        Find the best matching pair for the given text using SequenceMatcher.
        Returns (pair | None, score, version, threshold_used).
        """
        query = _norm(text)
        key = self._org_key(org_id)

        with self._lock:
            buffer = self._buffers.get(key) or ScriptBuffer()
            pairs_snapshot = list(buffer.pairs)
            threshold = buffer.threshold
            version = buffer.version

        if not query:
            return None, 0.0, version, threshold

        best: Optional[ScriptPair] = None
        best_score = 0.0
        for pair in pairs_snapshot:
            score = _similarity(pair.source, query)
            if score > best_score:
                best_score = score
                best = pair

        if best and best_score >= threshold:
            return best, best_score, version, threshold
        return None, best_score, version, threshold


# Singleton instance used across the app
script_store = ScriptStore()
