# Progressive Manuscript Matching — Design

**Status:** Approved spec, implementation in progress
**Branch:** `feature/progressive-manuscript-matching`
**Related:** Builds on `editing-sermon` (reviewed-manuscript ingest + commit-time lookup)

## Problem

The current sermon-review pipeline only substitutes reviewed English at *sentence commit* time — i.e., after Deepgram emits `is_final` and Korean sentence-end logic fires. In practice that means the audience sees the prepared English several seconds *after* the pastor finishes speaking each sentence, defeating the primary reason for preparing translations in advance.

## Goal

Display prepared English **while the pastor is still speaking the Korean sentence**, once the interim transcript has stabilized enough to identify the manuscript segment with high confidence. Do so without introducing wrong-segment jumps, flicker, or added GPT cost.

## Non-goals

- Changing sentence-commit / cursor-advancement semantics. Cursor advancement still requires whole-sentence confirmation at commit time.
- Replacing live GPT translation for off-manuscript speech.
- Reworking the Firestore write path or the listener transport.
- Solving multi-instance broadcast (deferred until Cloud Run instance count > 1).

## Load-bearing principles

1. **Separate display from commit.** Showing English on listener screens is a display concern; advancing the manuscript cursor is a state concern. Progressive display never mutates cursor state.
2. **Prefix-vs-prefix scoring, not whole-vs-whole.** During interim matching we compare "the audio so far" against "a similar-length prefix of each candidate segment." Whole-sentence scoring is reserved for commit-time confirmation.
3. **Live GPT fallback fires immediately on any commit without a confirmed reviewed match.** No timer, no delay.
4. **Measure before tuning.** Ship `pipeline_trace` first; treat every numeric threshold below as a starting value that Sunday data will refine.

## State machine (per utterance)

An *utterance* is the audio between one sentence commit and the next. The state machine restarts fresh on every commit.

### Constants (starting values)

| Name | Value | Purpose |
|---|---|---|
| `MIN_PREFIX_CHARS` | 8 | Below this, don't attempt matching |
| `MIN_PREFIX_EOJEOL` | 3 | Alt. threshold; whichever fires first |
| `PARTIAL_CONFIRM_COUNT` | 2 | Consecutive same-segment partials to enter PREVIEW |
| `STRONG_CONFIRM_COUNT` | 3 | Consecutive new-segment partials required to replace an on-screen preview |
| `SCORE_MIN` | 0.84 | Minimum prefix-vs-prefix score for any candidate |
| `SCORE_MARGIN` | 0.10 | `top - second_top` gap required |
| `OLD_SCORE_COLLAPSE` | 0.15 | Drop from lock-time score of currently-previewed segment required to allow corrective replacement |
| `DEVIATION_STREAK` | 4 | Consecutive partials with no in-window match to declare DEVIATED |
| `MIN_DWELL_MS` | 1200 | Reviewed caption cannot be replaced or cleared before this elapses |
| `SEARCH_WINDOW_BACK` | 1 | Manuscript segments back from cursor to include in candidate set |
| `SEARCH_WINDOW_FORWARD` | 5 | Manuscript segments forward from cursor to include in candidate set |

### States

- **IDLE** — no candidate. Default at utterance start.
- **CANDIDATE(segId, confirmations, lockScore)** — a segment tops the search but hasn't reached `PARTIAL_CONFIRM_COUNT`. Nothing on screen.
- **PREVIEW(segId, lockScore, previewShownAt)** — reviewed English is displayed.
- **DEVIATED** — preview cleared because speaker moved off-manuscript. Nothing on screen (or dimmed last caption — designer's call).
- **COMMITTED** — sentence-end fired. Terminal per utterance.

### Inputs

- **INTERIM(prefix)** — new Deepgram interim transcript for the current utterance.
- **COMMIT(fullText)** — sentence-end signal (`is_final` + `speech_final`, or `UtteranceEnd`).

### Transitions

#### IDLE → CANDIDATE (on INTERIM)

```
if len(prefix) < MIN_PREFIX_CHARS and eojeol(prefix) < MIN_PREFIX_EOJEOL: stay IDLE
scores = score_prefix_vs_prefix(prefix, window[cursor-1 .. cursor+5])
top, second = top2(scores)
if top.score < SCORE_MIN: stay IDLE
if (top.score - second.score) < SCORE_MARGIN: stay IDLE
→ CANDIDATE(top.segId, confirmations=1, lockScore=top.score)
```

#### CANDIDATE → CANDIDATE / PREVIEW / IDLE (on INTERIM)

```
top, second = top2(re-scored window)
if top.score < SCORE_MIN or (top.score - second.score) < SCORE_MARGIN:
    → IDLE
elif top.segId == state.segId:
    confirmations += 1
    lockScore = max(lockScore, top.score)
    if confirmations >= PARTIAL_CONFIRM_COUNT:
        → PREVIEW(segId, lockScore, previewShownAt=now())
        display reviewed English via priority lane
    else: stay CANDIDATE
else:
    → CANDIDATE(top.segId, confirmations=1, lockScore=top.score)   # reset
```

#### PREVIEW → PREVIEW (no-op) / PREVIEW (replaced) / DEVIATED (on INTERIM)

**Ordinary interim revision (dominant case):**

```
top = re-scored window's top segment
if top.segId == state.segId: do nothing to display
if top.segId != state.segId and (corrective criteria NOT met): do nothing to display
```

**Corrective replacement** — only if ALL true:

1. `now() - previewShownAt >= MIN_DWELL_MS`
2. Same new `segId` has been top for `STRONG_CONFIRM_COUNT` consecutive partials
3. new `top.score >= SCORE_MIN`
4. `(new top.score - second.score) >= SCORE_MARGIN`
5. Currently-previewed segId's latest score has dropped by `>= OLD_SCORE_COLLAPSE` from `lockScore`, **or** is now `< SCORE_MIN`

Then:

```
→ PREVIEW(new segId, lockScore=new top.score, previewShownAt=now())
   replace displayed English via priority lane (dwell timer restarts)
```

**Deviation:**

```
if for DEVIATION_STREAK consecutive partials no segment in window passes SCORE_MIN
   AND now() - previewShownAt >= MIN_DWELL_MS:
    → DEVIATED
    clear preview (or dim last caption)
```

#### PREVIEW → COMMITTED (on COMMIT)

```
wholeSentenceScore = score(fullText, manuscript[state.segId])
if wholeSentenceScore >= SCORE_MIN:
    confirmed = True
    advance cursor to state.segId + 1
    keep displayed English (dwell continues)
else:
    confirmed = False
    fire live GPT translation for fullText IMMEDIATELY
    do NOT advance cursor
    on GPT arrival: replace preview with live translation via priority lane
```

#### CANDIDATE / IDLE / DEVIATED → COMMITTED (on COMMIT)

```
no reviewed match was confirmed
fire live GPT translation for fullText IMMEDIATELY
do NOT advance cursor
on GPT arrival: display live translation via priority lane
```

## Invariants (must hold — enforce via tests)

1. Manuscript cursor advances **only** on the `PREVIEW → COMMITTED` transition where `wholeSentenceScore >= SCORE_MIN`. No other path advances it.
2. Nothing renders as a reviewed caption until at least `PARTIAL_CONFIRM_COUNT` (2) consecutive same-segment partials have occurred.
3. No on-screen preview is replaced or cleared before `MIN_DWELL_MS` has elapsed from its `previewShownAt`.
4. Corrective replacement requires **both** `STRONG_CONFIRM_COUNT` new-segment confirmations **and** collapse of the previous segment's score. Either alone is insufficient.
5. Every COMMIT that didn't confirm a reviewed match kicks off live GPT translation on the same tick — no timer path.
6. Reviewed captions render through a priority lane that bypasses live-translation pacing but enforces the `MIN_DWELL_MS` floor.
7. `Skip`-marked segments (per editing-sermon FR-15) are removed from the search window before scoring.

## Scoring

### Prefix-vs-prefix (interim path)

Compare `spoken_prefix` (Deepgram accumulated text since last commit) against a **similar-length prefix of the manuscript segment**. Prefix length rule:

- Take the last `N` eojeols of `spoken_prefix` where `N = eojeol_count(spoken_prefix)` (i.e., all of it)
- From each candidate segment, take the first `N` eojeols

Score using the same sequence-similarity metric already in `sermon_review/lookup.py::_similarity`. This is the mathematically correct comparison for the "is the audio so far the start of segment X?" question and is what fixes the class of issues surfaced by `test_short_fragment_does_not_expand_to_full_script_sentence` (short input scored against long segment always drags to zero).

### Whole-vs-whole (commit path)

Reuse existing `get_reviewed_text` logic unchanged. Progressive matching only affects the display path; commit still gates cursor advancement on whole-sentence confirmation.

## Modules

New:

- `backend/app/sermon_review/progressive_matcher.py` — pure state-machine module. Takes `(state, prefix, cursor, segments) → (new_state, display_action)`. Zero side effects.
- `backend/app/pipeline_trace.py` — per-utterance timing collector. Emits one structured log line at COMMIT and attaches trace to broadcast payload.

Changed:

- `backend/app/main.py` — `_translate_text_guarded` (broadcast branch) drives the state machine on interim, and `pipeline_trace` timestamps are inserted at all pipeline hops.
- `backend/app/socket_manager.py` — reviewed-mode broadcasts carry a `mode: "reviewed"` marker so the client can route them through the priority lane.
- `frontend/utils/useSubtitleSocket.ts` — new priority-lane handler for `mode: "reviewed"` messages: bypass pacing queue but enforce `MIN_DWELL_MS = 1200` before allowing replacement.

Unchanged:

- `backend/app/sermon_review/lookup.py` — commit-time whole-sentence lookup unchanged.
- Sermon ingest / review / xlsx modules.

## `pipeline_trace` fields (per utterance)

Emit one structured log line at COMMIT and echo relevant fields to the client on the outbound broadcast for end-to-end correlation.

```
utteranceId, orgId, roomId
audio_first_partial_at
prefix_first_qualified_at         (first partial passing MIN_PREFIX)
candidate_entered_at, candidate_segId, candidate_score
preview_entered_at, preview_segId, preview_lockScore, preview_prefix_len
corrective_replacement_at, corrective_from_segId, corrective_to_segId   (nullable)
deviation_detected_at             (nullable)
committed_at, committed_wholeSentenceScore
committed_source                  (reviewed | live | none)
live_translation_requested_at     (nullable)
live_translation_arrived_at       (nullable)
broadcast_sent_at
client_received_at, client_rendered_at   (echoed back from client trace)
```

Client attaches `client_received_at` and `client_rendered_at` and posts trace back via a lightweight `/api/pipeline_trace` endpoint (fire-and-forget POST). Traces persist to Firestore under `organizations/{orgId}/pipeline_trace/{utteranceId}` with a 14-day TTL rule.

## Rollout

1. **Ship `pipeline_trace` first** — no behavior change. Collect baseline from local/replay data and one Sunday.
2. **Ship state machine module + unit tests** — no wiring yet.
3. **Wire state machine into broadcast path behind an org-level feature flag** (`progressiveManuscriptMatching: true|false` on the org config). Default off.
4. **Ship frontend priority lane** with min-dwell enforcement.
5. **Enable flag for a single test org**, verify on a real Sunday.
6. **Enable by default** after two clean Sundays.

## Tests (must exist before flag flips on)

- `IDLE → CANDIDATE → PREVIEW` happy path (2 confirms of same segment)
- `CANDIDATE reset` — different top on 2nd partial
- `PREVIEW no-op` — ordinary interim revision of a locked preview does not replace
- `PREVIEW corrective replacement` — all 5 criteria met → replace
- `PREVIEW corrective NOT replaced` — 3 confirms but old score did not collapse
- `PREVIEW dwell floor` — new candidate confirmed at < 1200ms → no replacement, replay after dwell
- `Deviation clears preview` — 4 partials with no in-window match after dwell
- `COMMIT with preview + high whole-sentence score` — cursor advances, caption stays
- `COMMIT with preview + low whole-sentence score` — live GPT fires immediately, cursor does NOT advance
- `COMMIT from IDLE / CANDIDATE / DEVIATED` — live GPT fires immediately, cursor does NOT advance
- `Two segments with identical 10-char prefix` — `SCORE_MARGIN` blocks lock
- `Locked segment 42 then speaker skips to 45` — corrective replacement fires after strong confirm + collapse
- `Skip-marked segments removed from window` — FR-15 respected during interim matching too

## Expected latency

Given `MIN_PREFIX_CHARS=8`, `PARTIAL_CONFIRM_COUNT=2`, and Deepgram partials arriving every ~150–250ms:

- ~1.5–2s of speech to reach 8–10 meaningful characters
- + ~300–500ms for the second confirming partial
- **≈ 2–2.5s after the speaker starts a sentence**

Compare with current behavior (wait for sentence-end): ~5–10s. **Net improvement: ~3–7s per reviewed sentence.**

Actual numbers to be validated against `pipeline_trace` data from step 1 of the rollout.

## Open questions

- Should `DEVIATED` show a dimmed last caption or clear entirely? Recommend clear for the initial implementation — dim can be added if the flash is disorienting.
- Fire-and-forget POST for client trace: acceptable failure rate? Recommend no retry, no error surfacing — traces are best-effort observability.
- Should the corrective-replacement clock (`STRONG_CONFIRM_COUNT`) reset when the candidate top-segment changes, or accumulate? Recommend reset — anything else risks accidental triggers on noisy interim streams.
