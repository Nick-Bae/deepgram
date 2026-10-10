"""Lightweight per-utterance latency probe for engine A/B comparison.

Instrumented at 4 points across all three engines:

    T0  first Korean audio input arrives   (mark_t0)
    T1  first English text broadcast       (mark_t1)
    T2  final/stable English text broadcast (mark_t2)
    T3  first native audio broadcast        (mark_t3)

Deepgram path only (v2+ — F2 follow-up): correlation marks that let an
offline analyzer join host-side probe emits to listener-side receipts.
v4 clarifies the semantics of each mark:

    firstPostResetAudio  first audio chunk IN the WS AFTER the previous
                         reset (= mark_t0). This is a DIAGNOSTIC MARK, not
                         a valid D6 origin. Between utterances the operator
                         may be silent (padding/silence frames); the mark
                         anchors on the first POST-reset byte, not on an
                         acoustic utterance start. The emit payload field
                         is `firstPostResetAudioMs` (the ABS epoch ms) and
                         carries the legacy alias `audioChunkStartMs` for
                         back-compat with v2/v3 fixtures.
    speech_final         Deepgram speech_final=True arrival at the backend
                         (mark_speech_final). PROXY ANCHOR for D1 — see
                         "speech_final is a PROXY" below.
    broadcast_seq        seq assigned by publish_room terminal (record_broadcast)
    segment_id           chunker/reviewer segment id (if any)

**D1 and D6 are NOT YET SATISFIED by this instrumentation.** The procedure
requires:
  - D1 anchor = utterance end (acoustic)
  - D6 anchor = host audio chunk corresponding to the utterance
Neither is directly captured by the marks above. `speech_final` is a
proxy for the D1 anchor; `firstPostResetAudio` is NOT a valid D6 origin.
Satisfying the F2 criteria requires BOTH:
  (a) a true acoustic-origin mark (e.g., first Deepgram interim
      transcript event's audio_offset_ms), AND
  (b) a measured host↔listener clock-skew bound.
This change ships the primitives; it does not manufacture D1/D6
acceptance from them. See `measurement-uncertainty-policy.md`.

`emit_and_reset()` prints one `[LATENCY_PROBE]` JSON line to stdout with
deltas from T0 for T1/T2/T3 and for speech_final, PLUS the explicit
`firstPostResetAudioMs` (alias: `audioChunkStartMs`) and `speechFinalMs`
ABSOLUTE wall-clock epoch milliseconds, PLUS `segmentId` and
`broadcastSeq`, then resets so the next utterance starts fresh.

speech_final is a PROXY for acoustic utterance end
---------------------------------------------------

`speech_final` is the wall-clock receipt time at the backend of
Deepgram's `speech_final=True` event. It LAGS the acoustic utterance
end by Deepgram's endpointing time (typically 100-800 ms, bounded by
the configured endpointing threshold). The correct relationship is:

    acoustic_end  →  (endpointing lag)  →  speech_final_receipt
                                                            ↓
                                           →  listener_recv

So:

    acoustic_end_to_listener_latency
       = (listener_recv − acoustic_end)
       = (listener_recv − speech_final_receipt) + (speech_final_receipt − acoustic_end)
       = measured_from_speech_final + endpointing_lag

Both terms on the right are ≥ 0. Measuring from `speech_final`
UNDER-estimates the true acoustic-end-to-listener latency by exactly
the endpointing lag. The procedure wording "within 10 s of utterance
end" against this proxy is therefore an OPTIMISTIC bound; a run that
passes with the proxy may still exceed 10 s to acoustic end.

Clock + skew note (do NOT silently classify near the threshold)
---------------------------------------------------------------

T0..T3, audio_chunk_start, and speech_final are server wall-clock epoch
ms captured on the Cloud Run instance. Listener-side JSONL timestamps
come from the harness's local wall-clock. Clock skew between the two
can be up to several seconds for a laptop running the harness. The
D1/D6 offline join classifies results within ±skew_ms of the threshold
as INCONCLUSIVE rather than silently producing PASS/FAIL. See
`app.analysis.latency_join.uncertainty_band` for the exact rule and
`DEFAULT_CLOCK_SKEW_MS` for the current default.

Backward compatibility
----------------------

- `mark_utterance_end()` is a backward-compatible alias for
  `mark_speech_final()`. The emit payload exposes BOTH `speechFinalMs`
  (new, canonical) and `utteranceEndMs` (alias, same value) for
  consumers that haven't migrated yet.
- The historical slot name `utterance_end_ms` is also kept as a
  read-only property for ergonomics.

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
    __slots__ = (
        "engine",
        "org_id",
        "room_id",
        "t0_ms",
        "t1_ms",
        "t2_ms",
        "t3_ms",
        "audio_chunk_start_ms",
        "speech_final_ms",
        "segment_id",
        "broadcast_seq",
    )

    def __init__(self, engine: str, org_id: Optional[str], room_id: Optional[str]) -> None:
        self.engine = engine
        self.org_id = org_id or ""
        self.room_id = room_id or ""
        self.t0_ms: Optional[int] = None
        self.t1_ms: Optional[int] = None
        self.t2_ms: Optional[int] = None
        self.t3_ms: Optional[int] = None
        self.audio_chunk_start_ms: Optional[int] = None
        self.speech_final_ms: Optional[int] = None
        self.segment_id: Optional[str] = None
        self.broadcast_seq: Optional[int] = None

    @property
    def utterance_end_ms(self) -> Optional[int]:
        return self.speech_final_ms

    def mark_t0(self) -> None:
        if not ENABLED:
            return
        if self.t0_ms is None:
            now = _now_ms()
            self.t0_ms = now
            self.audio_chunk_start_ms = now

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

    def mark_speech_final(self, segment_id: Optional[str] = None) -> None:
        if not ENABLED:
            return
        if self.t0_ms is None:
            return
        if self.speech_final_ms is None:
            self.speech_final_ms = _now_ms()
        if segment_id is not None and self.segment_id is None:
            self.segment_id = str(segment_id)

    def mark_utterance_end(self, segment_id: Optional[str] = None) -> None:
        self.mark_speech_final(segment_id)

    def record_broadcast(self, seq: Optional[int], segment_id: Optional[str] = None) -> None:
        if not ENABLED:
            return
        if seq is not None and self.broadcast_seq is None:
            try:
                self.broadcast_seq = int(seq)
            except (TypeError, ValueError):
                self.broadcast_seq = None
        if segment_id is not None and self.segment_id is None:
            self.segment_id = str(segment_id)

    def emit_and_reset(self) -> None:
        if not ENABLED:
            return
        if self.t0_ms is None:
            return
        payload = {
            "engine": self.engine,
            "orgId": self.org_id,
            "roomId": self.room_id,
            "t0Ms": self.t0_ms,
            # v4: renamed semantically — the field is the first POST-RESET
            # audio chunk timestamp, which is NOT a valid D6 acoustic
            # origin. Legacy key `audioChunkStartMs` retained for back-compat
            # with v2/v3 fixtures and the offline analyzer's loader.
            "firstPostResetAudioMs": self.audio_chunk_start_ms,
            "audioChunkStartMs": self.audio_chunk_start_ms,  # legacy alias
            "speechFinalMs": self.speech_final_ms,
            "utteranceEndAbsMs": self.speech_final_ms,
            "dT1Ms": (self.t1_ms - self.t0_ms) if self.t1_ms is not None else None,
            "dT2Ms": (self.t2_ms - self.t0_ms) if self.t2_ms is not None else None,
            "dT3Ms": (self.t3_ms - self.t0_ms) if self.t3_ms is not None else None,
            "speechFinalDeltaMs": (self.speech_final_ms - self.t0_ms) if self.speech_final_ms is not None else None,
            "utteranceEndMs": (self.speech_final_ms - self.t0_ms) if self.speech_final_ms is not None else None,
            "segmentId": self.segment_id,
            "broadcastSeq": self.broadcast_seq,
        }
        print(f"[LATENCY_PROBE] {json.dumps(payload, separators=(',', ':'))}")
        self.t0_ms = None
        self.t1_ms = None
        self.t2_ms = None
        self.t3_ms = None
        self.audio_chunk_start_ms = None
        self.speech_final_ms = None
        self.segment_id = None
        self.broadcast_seq = None
