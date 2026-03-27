"""In-memory org-scoped store for pre-script pairs used by the hybrid admin console."""

from dataclasses import dataclass, field
from difflib import SequenceMatcher
import re
from threading import Lock
from typing import Any, Dict, List, Optional, Set, Tuple


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
        self._glossary_cache: Dict[Tuple[str, int], List[Tuple[str, str]]] = {}

    def _org_key(
        self,
        org_id: Optional[str] = None,
        *,
        room_id: Optional[str] = None,
        service_key: Optional[str] = None,
        service_date: Optional[str] = None,
    ) -> str:
        # Priority 1: room_id — runtime isolation, most specific
        if room_id:
            return f"room::{room_id.strip()}"
        # Priority 2: org + service + date — pre-warm on publish
        org = (org_id or "").strip() or self._GLOBAL_KEY
        svc = (service_key or "").strip()
        date = (service_date or "").strip()
        if svc and date:
            return f"{org}::{svc}::{date}"
        if svc:
            return f"{org}::{svc}"
        # Priority 3: org-only — legacy fallback
        return org

    def load(
        self,
        pairs: List[dict],
        threshold: float | None = None,
        *,
        org_id: Optional[str] = None,
        room_id: Optional[str] = None,
        service_key: Optional[str] = None,
        service_date: Optional[str] = None,
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
            key = self._org_key(org_id, room_id=room_id, service_key=service_key, service_date=service_date)
            buffer = self._buffers.get(key) or ScriptBuffer()
            buffer.pairs = cleaned
            if threshold is not None:
                buffer.threshold = max(0.0, min(1.0, float(threshold)))
            buffer.version += 1
            self._buffers[key] = buffer
            return len(buffer.pairs), buffer.threshold, buffer.version

    def clear(
        self,
        *,
        org_id: Optional[str] = None,
        room_id: Optional[str] = None,
        service_key: Optional[str] = None,
        service_date: Optional[str] = None,
    ) -> Tuple[int, int]:
        """Clear all pairs; returns (removed_count, new_version)."""
        with self._lock:
            key = self._org_key(org_id, room_id=room_id, service_key=service_key, service_date=service_date)
            buffer = self._buffers.get(key) or ScriptBuffer()
            removed = len(buffer.pairs)
            buffer.pairs = []
            buffer.sermons = {}
            buffer.version += 1
            self._buffers[key] = buffer
            return removed, buffer.version

    def stats(
        self,
        *,
        org_id: Optional[str] = None,
        room_id: Optional[str] = None,
        service_key: Optional[str] = None,
        service_date: Optional[str] = None,
    ) -> Tuple[int, float, int]:
        """Return (count, threshold, version) without exposing internal list."""
        with self._lock:
            key = self._org_key(org_id, room_id=room_id, service_key=service_key, service_date=service_date)
            buffer = self._buffers.get(key) or ScriptBuffer()
            return len(buffer.pairs), buffer.threshold, buffer.version

    def save_sermon(
        self,
        sermon: dict[str, Any],
        *,
        org_id: Optional[str] = None,
        room_id: Optional[str] = None,
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
            key = self._org_key(org_id, room_id=room_id)
            buffer = self._buffers.get(key) or ScriptBuffer()
            buffer.sermons[sermon_id] = normalized
            self._buffers[key] = buffer
            return dict(normalized)

    def get_sermon(
        self,
        sermon_id: str,
        *,
        org_id: Optional[str] = None,
        room_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Return a finalized sermon payload by sermon_id, if it exists."""
        clean_sermon_id = (sermon_id or "").strip()
        if not clean_sermon_id:
            return None
        with self._lock:
            key = self._org_key(org_id, room_id=room_id)
            buffer = self._buffers.get(key) or ScriptBuffer()
            hit = buffer.sermons.get(clean_sermon_id)
            return dict(hit) if hit else None

    def match_with_examples(
        self,
        text: str,
        *,
        org_id: Optional[str] = None,
        room_id: Optional[str] = None,
        example_min_score: float = 0.20,
        example_max: int = 3,
    ) -> Tuple[Optional["ScriptPair"], float, int, float, List["ScriptPair"]]:
        """
        Single O(N) scan returning (best_match, score, version, threshold, examples).

        examples: pairs with example_min_score <= score < threshold, sorted desc by score,
        capped at example_max. Falls back to first 2 pairs as style anchors if none qualify.
        Returns examples=[] when store is empty.
        """
        query = _norm(text)
        key = self._org_key(org_id, room_id=room_id)

        with self._lock:
            buffer = self._buffers.get(key) or ScriptBuffer()
            pairs_snapshot = list(buffer.pairs)
            threshold = buffer.threshold
            version = buffer.version

        if not query:
            return None, 0.0, version, threshold, []

        scored: List[Tuple[float, ScriptPair]] = []
        best: Optional[ScriptPair] = None
        best_score = 0.0

        for pair in pairs_snapshot:
            score = _similarity(pair.source, query)
            if score > best_score:
                best_score = score
                best = pair
            if score >= example_min_score:
                scored.append((score, pair))

        # Adaptive threshold for short fragments
        compact_query = _norm_compact(query)
        effective_threshold = threshold
        if len(compact_query) < 15:
            effective_threshold = min(threshold, 0.72)

        matched = best if best and best_score >= effective_threshold else None

        # Build examples list: candidates below threshold (excluding the matched pair)
        scored.sort(key=lambda x: -x[0])
        examples: List[ScriptPair] = []
        for sc, pair in scored:
            if matched and pair.index == matched.index:
                continue
            if sc >= effective_threshold:
                continue
            examples.append(pair)
            if len(examples) >= example_max:
                break

        # Style anchor fallback
        if not examples and pairs_snapshot:
            examples = [p for p in pairs_snapshot[:2] if not matched or p.index != matched.index]

        return matched, best_score, version, threshold, examples

    def match(
        self,
        text: str,
        *,
        org_id: Optional[str] = None,
        room_id: Optional[str] = None,
    ) -> Tuple[Optional[ScriptPair], float, int, float]:
        """
        Find the best matching pair for the given text using SequenceMatcher.
        Returns (pair | None, score, version, threshold_used).
        """
        query = _norm(text)
        key = self._org_key(org_id, room_id=room_id)

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

        compact_query = _norm_compact(query)
        effective_threshold = min(threshold, 0.72) if len(compact_query) < 15 else threshold

        if best and best_score >= effective_threshold:
            return best, best_score, version, threshold
        return None, best_score, version, threshold


    def get_keyword_glossary(
        self,
        *,
        org_id: Optional[str] = None,
        room_id: Optional[str] = None,
        max_terms: int = 15,
        min_char_len: int = 2,
        max_char_len: int = 6,
        min_pair_count: int = 2,
    ) -> List[Tuple[str, str]]:
        """
        Extract short recurring Korean terms appearing in >= min_pair_count source texts
        with their first English equivalent word. Cached per (org_key, store_version).
        Returns [] when fewer than min_pair_count pairs are loaded.
        """
        key = self._org_key(org_id, room_id=room_id)
        with self._lock:
            buffer = self._buffers.get(key) or ScriptBuffer()
            pairs_snapshot = list(buffer.pairs)
            version = buffer.version

        cache_key = (key, version)
        if cache_key in self._glossary_cache:
            return self._glossary_cache[cache_key]

        if len(pairs_snapshot) < min_pair_count:
            self._glossary_cache[cache_key] = []
            return []

        _HANGUL_TOKEN_RE = re.compile(r"[\uac00-\ud7a3]+")
        token_targets: Dict[str, List[str]] = {}

        for pair in pairs_snapshot:
            src_tokens = _HANGUL_TOKEN_RE.findall(pair.source)
            for tok in set(src_tokens):
                if min_char_len <= len(tok) <= max_char_len:
                    token_targets.setdefault(tok, []).append(pair.target)

        glossary: List[Tuple[str, str]] = []
        for tok, targets in token_targets.items():
            if len(targets) < min_pair_count:
                continue
            en_words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", targets[0])
            if not en_words:
                continue
            glossary.append((tok, en_words[0]))

        glossary = glossary[:max_terms]
        self._glossary_cache[cache_key] = glossary
        return glossary

    def get_vocab_set(
        self,
        *,
        org_id: Optional[str] = None,
        room_id: Optional[str] = None,
    ) -> Set[str]:
        """
        Return all unique Hangul tokens (length >= 3) from script source texts.
        Used for STT error correction via edit distance 1.
        """
        key = self._org_key(org_id, room_id=room_id)
        with self._lock:
            buffer = self._buffers.get(key) or ScriptBuffer()
            pairs_snapshot = list(buffer.pairs)

        _HANGUL_TOKEN_RE = re.compile(r"[\uac00-\ud7a3]+")
        vocab: Set[str] = set()
        for pair in pairs_snapshot:
            for tok in _HANGUL_TOKEN_RE.findall(pair.source):
                if len(tok) >= 3:
                    vocab.add(tok)
        return vocab


# Singleton instance used across the app
script_store = ScriptStore()
