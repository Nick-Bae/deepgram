"""Per-utterance pipeline latency trace.

Emits one structured JSON log line per utterance at COMMIT and attaches a
subset of fields to the outbound broadcast so the client can echo back
receive/render timestamps.

Timestamps are epoch milliseconds (int) for easy latency arithmetic
downstream. Field taxonomy matches the progressive manuscript matching
design doc.

Zero-side-effect except `emit()`, which prints a `[PIPELINE_TRACE]`
prefixed JSON line to stdout (picked up by Cloud Run logging).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass
class PipelineTrace:
    utterance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    org_id: Optional[str] = None
    room_id: Optional[str] = None

    # Ingest / STT
    audio_first_partial_at: Optional[int] = None
    prefix_first_qualified_at: Optional[int] = None

    # Progressive matcher state machine
    candidate_entered_at: Optional[int] = None
    candidate_seg_id: Optional[str] = None
    candidate_score: Optional[float] = None

    preview_entered_at: Optional[int] = None
    preview_seg_id: Optional[str] = None
    preview_lock_score: Optional[float] = None
    preview_prefix_len: Optional[int] = None

    corrective_replacement_at: Optional[int] = None
    corrective_from_seg_id: Optional[str] = None
    corrective_to_seg_id: Optional[str] = None

    deviation_detected_at: Optional[int] = None

    # Commit + downstream translation
    committed_at: Optional[int] = None
    committed_whole_sentence_score: Optional[float] = None
    committed_source: Optional[str] = None  # "reviewed" | "live" | "none"

    live_translation_requested_at: Optional[int] = None
    live_translation_arrived_at: Optional[int] = None

    broadcast_sent_at: Optional[int] = None

    # Echoed from client via /api/pipeline_trace
    client_received_at: Optional[int] = None
    client_rendered_at: Optional[int] = None

    # --- marker helpers -----------------------------------------------------

    def mark_audio_first_partial(self) -> None:
        if self.audio_first_partial_at is None:
            self.audio_first_partial_at = _now_ms()

    def mark_prefix_qualified(self) -> None:
        if self.prefix_first_qualified_at is None:
            self.prefix_first_qualified_at = _now_ms()

    def mark_candidate(self, seg_id: str, score: float) -> None:
        # Only record the first time — later CANDIDATE resets on different
        # segments don't overwrite the "first qualifying candidate" timing.
        if self.candidate_entered_at is None:
            self.candidate_entered_at = _now_ms()
            self.candidate_seg_id = seg_id
            self.candidate_score = score

    def mark_preview(self, seg_id: str, lock_score: float, prefix_len: int) -> None:
        self.preview_entered_at = _now_ms()
        self.preview_seg_id = seg_id
        self.preview_lock_score = lock_score
        self.preview_prefix_len = prefix_len

    def mark_corrective_replacement(self, from_seg_id: str, to_seg_id: str) -> None:
        self.corrective_replacement_at = _now_ms()
        self.corrective_from_seg_id = from_seg_id
        self.corrective_to_seg_id = to_seg_id

    def mark_deviation(self) -> None:
        self.deviation_detected_at = _now_ms()

    def mark_committed(self, whole_sentence_score: Optional[float], source: str) -> None:
        self.committed_at = _now_ms()
        self.committed_whole_sentence_score = whole_sentence_score
        self.committed_source = source

    def mark_live_requested(self) -> None:
        self.live_translation_requested_at = _now_ms()

    def mark_live_arrived(self) -> None:
        self.live_translation_arrived_at = _now_ms()

    def mark_broadcast_sent(self) -> None:
        self.broadcast_sent_at = _now_ms()

    def apply_client_echo(self, *, received_at: Optional[int], rendered_at: Optional[int]) -> None:
        if received_at is not None:
            self.client_received_at = received_at
        if rendered_at is not None:
            self.client_rendered_at = rendered_at

    # --- serialization ------------------------------------------------------

    def to_broadcast_payload(self) -> dict:
        """Subset of trace fields attached to the outbound broadcast so the
        client can echo receive/render timings correlated back to this
        utterance."""
        return {
            "utteranceId": self.utterance_id,
            "broadcastSentAt": self.broadcast_sent_at,
        }

    def to_log_dict(self) -> dict:
        d = asdict(self)
        # camelCase for consistency with the rest of the broadcast wire format
        return {_to_camel(k): v for k, v in d.items()}

    def emit(self) -> None:
        payload = self.to_log_dict()
        print(f"[PIPELINE_TRACE] {json.dumps(payload, separators=(',', ':'))}")


def _to_camel(snake: str) -> str:
    parts = snake.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])
