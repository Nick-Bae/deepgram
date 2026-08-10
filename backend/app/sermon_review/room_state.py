"""Per-room progressive matcher state.

Owns the progressive matcher's per-session state so the Deepgram receive
loop can advance() cleanly across events. Also holds the cached segments
list (fetched once at session start) and the current utterance's
PipelineTrace.

Nothing here is thread-safe; each ws_stt_deepgram session owns its own
instance and reads/writes it single-threadedly from the same coroutine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.pipeline_trace import PipelineTrace
from app.sermon_review.progressive_matcher import INITIAL_STATE, State


@dataclass
class ProgressiveMatcherRoomState:
    enabled: bool = False
    org_id: Optional[str] = None
    room_id: Optional[str] = None
    service_key: Optional[str] = None
    sermon_id: Optional[str] = None
    segments: list[dict[str, Any]] = field(default_factory=list)
    cursor: int = 0
    state: State = INITIAL_STATE
    preview_shown_at_ms: Optional[int] = None
    trace: Optional[PipelineTrace] = None

    def reset_for_next_utterance(self) -> None:
        """Called after COMMIT is fully processed. Fresh state for the
        next utterance; cursor and segments carry over."""
        self.state = INITIAL_STATE
        self.preview_shown_at_ms = None
        self.trace = None

    def ensure_trace(self) -> PipelineTrace:
        if self.trace is None:
            self.trace = PipelineTrace(org_id=self.org_id, room_id=self.room_id)
        return self.trace

    def advance_cursor_to(self, seg_id: str) -> None:
        """Move cursor to seg_id + 1 (i.e., past the confirmed segment).
        Falls back to cursor + 1 if the seg_id can't be located."""
        for idx, seg in enumerate(self.segments):
            if str(seg.get("segmentId") or "") == seg_id:
                self.cursor = idx + 1
                return
        self.cursor += 1


def build_room_state(
    *,
    store: Any,
    org_id: Optional[str],
    room_id: Optional[str],
    service_key: Optional[str],
) -> ProgressiveMatcherRoomState:
    """Fetch org flag + linked sermon segments and construct the per-room
    state. If the flag is off or no linked sermon exists, returns a
    disabled state (enabled=False, empty segments)."""
    if not org_id:
        return ProgressiveMatcherRoomState(enabled=False)

    try:
        enabled = bool(store.get_org_progressive_manuscript_matching_enabled(org_id))
    except Exception:
        enabled = False
    if not enabled:
        return ProgressiveMatcherRoomState(enabled=False, org_id=org_id, room_id=room_id)

    if not service_key:
        return ProgressiveMatcherRoomState(enabled=False, org_id=org_id, room_id=room_id)

    # Reach into the store to find the linked sermon and cache its segments
    # once — the receive loop is hot and we don't want per-partial Firestore
    # reads.
    try:
        service = _resolve_service(store, org_id, service_key)
        sermon_id = (service or {}).get("linkedSermonId")
        if not sermon_id:
            return ProgressiveMatcherRoomState(
                enabled=False, org_id=org_id, room_id=room_id, service_key=service_key
            )
        sermon = store.get_review_sermon(org_id, sermon_id)
        segments = list((sermon or {}).get("segments") or [])
    except Exception:
        return ProgressiveMatcherRoomState(
            enabled=False, org_id=org_id, room_id=room_id, service_key=service_key
        )

    return ProgressiveMatcherRoomState(
        enabled=True,
        org_id=org_id,
        room_id=room_id,
        service_key=service_key,
        sermon_id=sermon_id,
        segments=segments,
    )


def _resolve_service(store: Any, org_id: str, service_key: str) -> Optional[dict[str, Any]]:
    """Return the service dict without triggering an auth check."""
    getter = getattr(store, "get_service", None)
    if getter is None:
        return None
    try:
        return getter(org_id, service_key)
    except Exception:
        return None
