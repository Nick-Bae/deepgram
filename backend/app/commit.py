# backend/app/commit.py
import time

class Committer:
    def __init__(self):
        self.seq = 0
        self._last = ""

    def maybe_commit(self, text: str, is_final: bool, speech_final: bool):
        t = (text or "").strip()
        if not t:
            return None
        if is_final or speech_final:
            if t == self._last:
                return None
            self.seq += 1
            self._last = t
            return {"type": "commit", "seq": self.seq, "ts": time.time(), "text": t}
        return None
