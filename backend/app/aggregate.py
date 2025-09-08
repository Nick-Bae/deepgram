# backend/app/aggregate.py
import time

_TERMINAL = set(".?!。？！…")

class SentenceAggregator:
    """
    Collects DG finals and emits one fuller sentence when:
      - sentence-ending punctuation is seen, or
      - a short timeout passes, or
      - Deepgram signals speech_final.
    """
    def __init__(self, max_wait_ms: int = 2200, require_punct: bool = False):
        self.max_wait_ms = max_wait_ms
        self.require_punct = require_punct
        self._open = False
        self._started = 0.0
        self._latest = ""

    @staticmethod
    def _has_terminal_punct(s: str) -> bool:
        s = (s or "").strip()
        return bool(s) and s[-1] in _TERMINAL

    def ingest(self, text: str, is_final: bool, speech_final: bool):
        t = (text or "").strip()
        if not t:
            return None

        now = time.time()
        if not self._open:
            self._open = True
            self._started = now

        # keep only the latest stabilized wording
        self._latest = t

        should_flush = False
        if speech_final:
            should_flush = True
        elif is_final:
            if self._has_terminal_punct(t):
                should_flush = True
            elif not self.require_punct and ((now - self._started) * 1000) >= self.max_wait_ms:
                should_flush = True

        if should_flush and self._latest:
            out = self._latest
            self._latest = ""
            self._open = False
            return out

        return None

    def force_flush(self):
        if self._open and self._latest:
            out = self._latest
            self._latest = ""
            self._open = False
            return out
        return None
