"""Lightweight per-utterance latency probe for engine A/B comparison.

Instrumented at 4 points across all three engines:

    T0  first Korean audio input arrives   (mark_t0)
    T1  first English text broadcast       (mark_t1)
    T2  final/stable English text broadcast (mark_t2)
    T3  first native audio broadcast        (mark_t3)

`emit()` prints one `[LATENCY_PROBE]` JSON line to stdout with deltas from
T0 for T1/T2/T3, then resets so the next utterance starts fresh.

Emit is called at the natural utterance boundary for each engine:
- Deepgram + GPT   → after the final commit + Google TTS broadcast
- OpenAI Realtime  → after session.output_audio.done or output_transcript.done
- Gemini Live      → after turnComplete

Set `LATENCY_PROBE_ENABLED=0` to silence in prod once measurement is done.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


ENABLED = os.getenv("LATENCY_PROBE_ENABLED", "1").strip() not in {"0", "false", "no", "off"}


class LatencyProbe:
    __slots__ = ("engine", "org_id", "room_id", "t0_ms", "t1_ms", "t2_ms", "t3_ms")

    def __init__(self, engine: str, org_id: Optional[str], room_id: Optional[str]) -> None:
        self.engine = engine
        self.org_id = org_id or ""
        self.room_id = room_id or ""
        self.t0_ms: Optional[int] = None
        self.t1_ms: Optional[int] = None
        self.t2_ms: Optional[int] = None
        self.t3_ms: Optional[int] = None

    # T0 fires only for the *first* audio chunk of a new utterance. Subsequent
    # partials from the same utterance don't reset it — that would defeat
    # the point of measuring end-to-end latency.
    def mark_t0(self) -> None:
        if not ENABLED:
            return
        if self.t0_ms is None:
            self.t0_ms = _now_ms()

    def mark_t1(self) -> None:
        if not ENABLED:
            return
        if self.t1_ms is None and self.t0_ms is not None:
            self.t1_ms = _now_ms()

    def mark_t2(self) -> None:
        if not ENABLED:
            return
        if self.t2_ms is None and self.t0_ms is not None:
            self.t2_ms = _now_ms()

    def mark_t3(self) -> None:
        if not ENABLED:
            return
        if self.t3_ms is None and self.t0_ms is not None:
            self.t3_ms = _now_ms()

    def emit_and_reset(self) -> None:
        """Emit one probe line, then reset — call at utterance boundary."""
        if not ENABLED:
            return
        if self.t0_ms is None:
            # Nothing was marked; nothing to emit.
            return
        payload = {
            "engine": self.engine,
            "orgId": self.org_id,
            "roomId": self.room_id,
            "t0Ms": self.t0_ms,
            "dT1Ms": (self.t1_ms - self.t0_ms) if self.t1_ms is not None else None,
            "dT2Ms": (self.t2_ms - self.t0_ms) if self.t2_ms is not None else None,
            "dT3Ms": (self.t3_ms - self.t0_ms) if self.t3_ms is not None else None,
        }
        print(f"[LATENCY_PROBE] {json.dumps(payload, separators=(',', ':'))}")
        self.t0_ms = None
        self.t1_ms = None
        self.t2_ms = None
        self.t3_ms = None
