"""Progressive manuscript matcher — pure state machine.

Design: docs/02-design/features/progressive-manuscript-matching.design.md

Consumes Deepgram interim transcripts and sentence-commit events; emits
display actions (show, replace, clear preview) and cursor/live-translation
control actions. Zero side effects — caller is responsible for actually
broadcasting, mutating cursor, requesting GPT translation, etc.

Segments are dicts matching the shape stored by editing-sermon
(`{"segmentId": ..., "original": ..., "reviewedTranslation": ...,
"appTranslation": ..., "status": ...}`).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal, Optional, Union

from .lookup import _similarity


# --- config -----------------------------------------------------------------

@dataclass(frozen=True)
class MatcherConfig:
    min_prefix_chars: int = 8
    min_prefix_eojeol: int = 3
    partial_confirm_count: int = 2
    strong_confirm_count: int = 3
    # 2026-08-14: relaxed from 0.84 → 0.78. Real-sermon PIPELINE_TRACE
    # analysis on the Genesis-17 test showed the ≥4s misses cluster in the
    # 0.69-0.87 top-1-score band — capped by minor STT mis-transcriptions
    # ("선대 여러분" vs "성도 여러분") and small paraphrase drift, not by
    # PMM's matching logic itself. score_margin=0.10 stays as the anti-jump
    # guard, so lowering the floor doesn't loosen the top-1-vs-top-2 gap.
    # Watch deviationDetectedAt counts on the next real Sunday — if wrong
    # segments start locking, revert to 0.82 or 0.84.
    score_min: float = 0.78
    score_margin: float = 0.10
    old_score_collapse: float = 0.15
    deviation_streak: int = 4
    min_dwell_ms: int = 1200
    search_window_back: int = 1
    search_window_forward: int = 5
    # Cursor-bias fast-preview — relax gate thresholds ONLY when the top-1
    # scored segment is segments[cursor] (the next-in-sequence one). Non-
    # adjacent (jump) matches still need the full strict prefix + partial-
    # confirm gates. Trades a little safety for earlier reveal when the
    # pastor stays on-script; score_min/score_margin are unchanged so we
    # still require a strong top-1 vs top-2 gap.
    cursor_bias_partial_confirm_count: int = 1
    cursor_bias_min_prefix_chars: int = 4
    cursor_bias_min_prefix_eojeol: int = 2


DEFAULT_CONFIG = MatcherConfig()


# --- state ------------------------------------------------------------------

StateKind = Literal["IDLE", "CANDIDATE", "PREVIEW", "DEVIATED"]


@dataclass(frozen=True)
class State:
    kind: StateKind = "IDLE"
    seg_id: Optional[str] = None
    confirmations: int = 0
    lock_score: float = 0.0
    preview_shown_at_ms: Optional[int] = None
    # Corrective replacement tracking (only meaningful in PREVIEW)
    corrective_candidate_seg_id: Optional[str] = None
    corrective_confirmations: int = 0
    # Deviation detection (only meaningful in PREVIEW)
    no_match_streak: int = 0


INITIAL_STATE = State()


# --- events -----------------------------------------------------------------

@dataclass(frozen=True)
class InterimEvent:
    prefix: str  # Deepgram accumulated text since last commit
    now_ms: int


@dataclass(frozen=True)
class CommitEvent:
    full_text: str
    now_ms: int


Event = Union[InterimEvent, CommitEvent]


# --- actions ----------------------------------------------------------------

@dataclass(frozen=True)
class ShowPreview:
    seg_id: str
    reviewed_text: str


@dataclass(frozen=True)
class ReplacePreview:
    from_seg_id: str
    to_seg_id: str
    reviewed_text: str


@dataclass(frozen=True)
class ClearPreview:
    pass


@dataclass(frozen=True)
class ConfirmAndAdvanceCursor:
    seg_id: str


@dataclass(frozen=True)
class RequestLiveTranslation:
    full_text: str


Action = Union[
    ShowPreview,
    ReplacePreview,
    ClearPreview,
    ConfirmAndAdvanceCursor,
    RequestLiveTranslation,
]


# --- helpers ----------------------------------------------------------------

def _eojeol_count(text: str) -> int:
    return len([w for w in (text or "").split() if w])


def _segment_reviewed_text(segment: dict[str, Any]) -> str:
    reviewed = (segment.get("reviewedTranslation") or "").strip()
    if reviewed:
        return reviewed
    return (segment.get("appTranslation") or "").strip()


def _prefix_by_eojeols(text: str, n: int) -> str:
    if n <= 0:
        return ""
    parts = (text or "").split()
    return " ".join(parts[:n])


def _score_prefix(prefix: str, segment_original: str) -> float:
    """Prefix-vs-prefix score: compare `prefix` against the leading eojeols
    of `segment_original` of the same eojeol count.

    Direct fix for the class of issue where short-prefix-vs-long-sentence
    similarity is dragged to zero by length imbalance.
    """
    n = _eojeol_count(prefix)
    if n == 0:
        return 0.0
    seg_prefix = _prefix_by_eojeols(segment_original, n)
    return _similarity(prefix, seg_prefix)


def _search_window(
    segments: list[dict[str, Any]],
    cursor: int,
    config: MatcherConfig,
) -> list[dict[str, Any]]:
    """Return the candidate segments the matcher may consider, honoring
    Skip-marked segment exclusion (FR-15)."""
    start = max(0, cursor - config.search_window_back)
    end = min(len(segments), cursor + config.search_window_forward + 1)
    return [
        seg for seg in segments[start:end]
        if str(seg.get("status") or "Draft") != "Skip"
    ]


def _score_all(
    prefix: str,
    window: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], float]]:
    scored = [(seg, _score_prefix(prefix, seg.get("original") or "")) for seg in window]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def _prefix_qualifies(prefix: str, config: MatcherConfig) -> bool:
    return (
        len(prefix) >= config.min_prefix_chars
        or _eojeol_count(prefix) >= config.min_prefix_eojeol
    )


def _prefix_qualifies_cursor_bias(prefix: str, config: MatcherConfig) -> bool:
    """Looser prefix requirement used only when the top-scored segment is
    segments[cursor]. Bounded by cursor position → safe to fire earlier."""
    return (
        len(prefix) >= config.cursor_bias_min_prefix_chars
        or _eojeol_count(prefix) >= config.cursor_bias_min_prefix_eojeol
    )


def _cursor_segment_id(
    cursor: int,
    segments: list[dict[str, Any]],
) -> Optional[str]:
    """seg_id at cursor position, skipping Skip-marked segments (FR-15).
    Returns None if cursor is past the end or every remaining segment is
    Skip."""
    for seg in segments[cursor:]:
        if str(seg.get("status") or "Draft") == "Skip":
            continue
        seg_id = str(seg.get("segmentId") or "")
        return seg_id or None
    return None


def _passes_gates(scored: list[tuple[dict[str, Any], float]], config: MatcherConfig) -> bool:
    if not scored:
        return False
    top_score = scored[0][1]
    second_score = scored[1][1] if len(scored) > 1 else 0.0
    if top_score < config.score_min:
        return False
    if (top_score - second_score) < config.score_margin:
        return False
    return True


def _find_segment(window: list[dict[str, Any]], seg_id: str) -> Optional[dict[str, Any]]:
    for seg in window:
        if str(seg.get("segmentId") or "") == seg_id:
            return seg
    return None


# --- main API ---------------------------------------------------------------

def advance(
    state: State,
    event: Event,
    *,
    cursor: int,
    segments: list[dict[str, Any]],
    config: MatcherConfig = DEFAULT_CONFIG,
) -> tuple[State, list[Action]]:
    """Apply one event to the current state. Returns (new_state, actions)."""
    if isinstance(event, InterimEvent):
        return _on_interim(state, event, cursor, segments, config)
    if isinstance(event, CommitEvent):
        return _on_commit(state, event, cursor, segments, config)
    raise TypeError(f"unknown event type: {type(event)!r}")


# --- interim handling -------------------------------------------------------

def _on_interim(
    state: State,
    event: InterimEvent,
    cursor: int,
    segments: list[dict[str, Any]],
    config: MatcherConfig,
) -> tuple[State, list[Action]]:
    # Cursor-bias thresholds are the loosest — if the prefix doesn't meet
    # even those, no path can fire yet.
    if not _prefix_qualifies_cursor_bias(event.prefix, config):
        return state, []

    window = _search_window(segments, cursor, config)
    scored = _score_all(event.prefix, window)

    # Cursor-adjacent = top-1 segment is segments[cursor]. When true, use
    # cursor-bias gates (fewer confirmations, looser prefix). When false,
    # non-adjacent (jump) matches must clear the strict prefix gate.
    adjacent_seg_id = _cursor_segment_id(cursor, segments)
    top_seg_id = str(scored[0][0].get("segmentId") or "") if scored else ""
    is_adjacent = bool(adjacent_seg_id) and top_seg_id == adjacent_seg_id

    if not is_adjacent and not _prefix_qualifies(event.prefix, config):
        return state, []

    if state.kind == "IDLE":
        return _from_idle(state, scored, config, is_adjacent=is_adjacent)
    if state.kind == "CANDIDATE":
        return _from_candidate(state, scored, config, is_adjacent=is_adjacent)
    if state.kind == "PREVIEW":
        return _from_preview(state, event, scored, window, config)
    if state.kind == "DEVIATED":
        # DEVIATED does not re-engage until commit — utterance is over
        # from the matcher's perspective until commit fires.
        return state, []
    return state, []


def _effective_partial_confirm_count(config: MatcherConfig, is_adjacent: bool) -> int:
    return (
        config.cursor_bias_partial_confirm_count
        if is_adjacent
        else config.partial_confirm_count
    )


def _from_idle(
    state: State,
    scored: list[tuple[dict[str, Any], float]],
    config: MatcherConfig,
    *,
    is_adjacent: bool,
) -> tuple[State, list[Action]]:
    if not _passes_gates(scored, config):
        return state, []
    top_seg, top_score = scored[0]
    top_id = str(top_seg.get("segmentId") or "")

    # Cursor-bias fast path: skip CANDIDATE and PREVIEW in one interim
    # when the top segment is the next-in-sequence one AND config says
    # cursor-bias needs only one confirmation.
    if is_adjacent and _effective_partial_confirm_count(config, True) <= 1:
        reviewed_text = _segment_reviewed_text(top_seg)
        return (
            State(
                kind="PREVIEW",
                seg_id=top_id,
                confirmations=1,
                lock_score=top_score,
                preview_shown_at_ms=None,
            ),
            [ShowPreview(seg_id=top_id, reviewed_text=reviewed_text)],
        )

    return (
        State(
            kind="CANDIDATE",
            seg_id=top_id,
            confirmations=1,
            lock_score=top_score,
        ),
        [],
    )


def _from_candidate(
    state: State,
    scored: list[tuple[dict[str, Any], float]],
    config: MatcherConfig,
    *,
    is_adjacent: bool,
) -> tuple[State, list[Action]]:
    if not _passes_gates(scored, config):
        return INITIAL_STATE, []
    top_seg, top_score = scored[0]
    top_id = str(top_seg.get("segmentId") or "")
    effective_count = _effective_partial_confirm_count(config, is_adjacent)

    if top_id != state.seg_id:
        # Different top segment — reset. Fast-path applies to the new
        # candidate too when it's the cursor-adjacent one.
        if is_adjacent and effective_count <= 1:
            reviewed_text = _segment_reviewed_text(top_seg)
            return (
                State(
                    kind="PREVIEW",
                    seg_id=top_id,
                    confirmations=1,
                    lock_score=top_score,
                    preview_shown_at_ms=None,
                ),
                [ShowPreview(seg_id=top_id, reviewed_text=reviewed_text)],
            )
        return (
            State(
                kind="CANDIDATE",
                seg_id=top_id,
                confirmations=1,
                lock_score=top_score,
            ),
            [],
        )
    confirmations = state.confirmations + 1
    lock_score = max(state.lock_score, top_score)
    if confirmations < effective_count:
        return (
            replace(state, confirmations=confirmations, lock_score=lock_score),
            [],
        )
    # Reached PARTIAL_CONFIRM_COUNT → PREVIEW
    reviewed_text = _segment_reviewed_text(top_seg)
    return (
        State(
            kind="PREVIEW",
            seg_id=top_id,
            confirmations=confirmations,
            lock_score=lock_score,
            preview_shown_at_ms=None,  # set below via preview event marker
        ),
        [ShowPreview(seg_id=top_id, reviewed_text=reviewed_text)],
    )


def _from_preview(
    state: State,
    event: InterimEvent,
    scored: list[tuple[dict[str, Any], float]],
    window: list[dict[str, Any]],
    config: MatcherConfig,
) -> tuple[State, list[Action]]:
    # Backfill preview_shown_at_ms if this is the first interim after the
    # PREVIEW transition (caller wires this via the show-preview action).
    shown_at = state.preview_shown_at_ms if state.preview_shown_at_ms is not None else event.now_ms
    state_with_shown = replace(state, preview_shown_at_ms=shown_at)

    # 1) Deviation check: no segment in window passes SCORE_MIN
    top_score = scored[0][1] if scored else 0.0
    if top_score < config.score_min:
        streak = state_with_shown.no_match_streak + 1
        elapsed = event.now_ms - shown_at
        if streak >= config.deviation_streak and elapsed >= config.min_dwell_ms:
            return (
                State(kind="DEVIATED"),
                [ClearPreview()],
            )
        return (
            replace(state_with_shown, no_match_streak=streak, corrective_confirmations=0, corrective_candidate_seg_id=None),
            [],
        )

    # Reset the deviation counter — we had a passing top this round.
    state_after_deviation_reset = replace(state_with_shown, no_match_streak=0)

    top_seg, top_score = scored[0]
    top_id = str(top_seg.get("segmentId") or "")

    if top_id == state_after_deviation_reset.seg_id:
        # Same segment still tops — no-op. Reset corrective candidate.
        return (
            replace(
                state_after_deviation_reset,
                corrective_candidate_seg_id=None,
                corrective_confirmations=0,
            ),
            [],
        )

    # A different segment tops. Consider corrective replacement.
    return _consider_corrective_replacement(
        state_after_deviation_reset, event, scored, window, config
    )


def _consider_corrective_replacement(
    state: State,
    event: InterimEvent,
    scored: list[tuple[dict[str, Any], float]],
    window: list[dict[str, Any]],
    config: MatcherConfig,
) -> tuple[State, list[Action]]:
    top_seg, top_score = scored[0]
    top_id = str(top_seg.get("segmentId") or "")
    second_score = scored[1][1] if len(scored) > 1 else 0.0

    # New top must pass gates
    if top_score < config.score_min or (top_score - second_score) < config.score_margin:
        return (
            replace(state, corrective_candidate_seg_id=None, corrective_confirmations=0),
            [],
        )

    # Accumulate/reset corrective confirmations. Per design: reset on candidate change.
    if state.corrective_candidate_seg_id == top_id:
        corr_confirmations = state.corrective_confirmations + 1
    else:
        corr_confirmations = 1

    if corr_confirmations < config.strong_confirm_count:
        return (
            replace(
                state,
                corrective_candidate_seg_id=top_id,
                corrective_confirmations=corr_confirmations,
            ),
            [],
        )

    # Check dwell floor
    assert state.preview_shown_at_ms is not None  # set by _from_preview
    elapsed = event.now_ms - state.preview_shown_at_ms
    if elapsed < config.min_dwell_ms:
        # Hold accumulation — dwell isn't up yet. Do not replace.
        return (
            replace(
                state,
                corrective_candidate_seg_id=top_id,
                corrective_confirmations=corr_confirmations,
            ),
            [],
        )

    # Check old-score collapse: previously-locked segment's current score
    # must have collapsed (or fallen below SCORE_MIN)
    old_seg = _find_segment(window, state.seg_id or "")
    old_current_score = _score_prefix(event.prefix, (old_seg or {}).get("original") or "") if old_seg else 0.0
    old_score_collapsed = (
        old_current_score < config.score_min
        or (state.lock_score - old_current_score) >= config.old_score_collapse
    )
    if not old_score_collapsed:
        return (
            replace(
                state,
                corrective_candidate_seg_id=top_id,
                corrective_confirmations=corr_confirmations,
            ),
            [],
        )

    # All criteria met — replace the preview
    reviewed_text = _segment_reviewed_text(top_seg)
    return (
        State(
            kind="PREVIEW",
            seg_id=top_id,
            confirmations=corr_confirmations,
            lock_score=top_score,
            preview_shown_at_ms=event.now_ms,  # dwell restarts
        ),
        [ReplacePreview(from_seg_id=state.seg_id or "", to_seg_id=top_id, reviewed_text=reviewed_text)],
    )


# --- commit handling --------------------------------------------------------

def _on_commit(
    state: State,
    event: CommitEvent,
    cursor: int,
    segments: list[dict[str, Any]],
    config: MatcherConfig,
) -> tuple[State, list[Action]]:
    if state.kind == "PREVIEW":
        window = _search_window(segments, cursor, config)
        previewed = _find_segment(window, state.seg_id or "")
        if previewed is None:
            # Preview segment slipped out of the window — treat as unconfirmed
            return INITIAL_STATE, [RequestLiveTranslation(full_text=event.full_text)]
        whole_score = _similarity(event.full_text, previewed.get("original") or "")
        if whole_score >= config.score_min:
            return (
                INITIAL_STATE,
                [ConfirmAndAdvanceCursor(seg_id=state.seg_id or "")],
            )
        # Preview shown but not confirmed → live GPT fires immediately
        return (
            INITIAL_STATE,
            [RequestLiveTranslation(full_text=event.full_text)],
        )
    # IDLE / CANDIDATE / DEVIATED → live GPT fires immediately, no cursor advance
    return INITIAL_STATE, [RequestLiveTranslation(full_text=event.full_text)]
