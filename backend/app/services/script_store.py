"""In-memory store for pre-script pairs used by the hybrid admin console."""

from dataclasses import dataclass
from difflib import SequenceMatcher
from threading import Lock
from typing import List, Optional, Tuple


def _norm(text: str) -> str:
    """Lowercase + collapse whitespace for fuzzy matching."""
    return " ".join((text or "").split()).lower()


@dataclass
class ScriptPair:
    source: str
    target: str
    index: int


class ScriptStore:
    """
    Very small in-memory buffer to hold uploaded bilingual pairs.
    Designed for interactive use; does not persist to disk.
    """

    def __init__(self):
        self._pairs: List[ScriptPair] = []
        self.threshold: float = 0.84
        self.version: int = 0
        self._lock = Lock()

    def load(self, pairs: List[dict], threshold: float | None = None) -> Tuple[int, float, int]:
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
            self._pairs = cleaned
            if threshold is not None:
                self.threshold = max(0.0, min(1.0, float(threshold)))
            self.version += 1
            return len(self._pairs), self.threshold, self.version

    def clear(self) -> Tuple[int, int]:
        """Clear all pairs; returns (removed_count, new_version)."""
        with self._lock:
            removed = len(self._pairs)
            self._pairs = []
            self.version += 1
            return removed, self.version

    def stats(self) -> Tuple[int, float, int]:
        """Return (count, threshold, version) without exposing internal list."""
        with self._lock:
            return len(self._pairs), self.threshold, self.version

    def match(self, text: str) -> Tuple[Optional[ScriptPair], float, int, float]:
        """
        Find the best matching pair for the given text using SequenceMatcher.
        Returns (pair | None, score, version, threshold_used).
        """
        query = _norm(text)
        if not query:
            return None, 0.0, self.version, self.threshold

        with self._lock:
            pairs_snapshot = list(self._pairs)
            threshold = self.threshold
            version = self.version

        best: Optional[ScriptPair] = None
        best_score = 0.0
        for pair in pairs_snapshot:
            score = SequenceMatcher(None, _norm(pair.source), query).ratio()
            if score > best_score:
                best_score = score
                best = pair

        if best and best_score >= threshold:
            return best, best_score, version, threshold
        return None, best_score, version, threshold


# Singleton instance used across the app
script_store = ScriptStore()
