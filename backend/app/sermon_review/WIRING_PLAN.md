# Progressive Manuscript Matcher — Wiring Plan

**Feature branch:** `feature/progressive-manuscript-matching`
**Design:** `docs/02-design/features/progressive-manuscript-matching.design.md`
**Modules already committed:**
- `backend/app/pipeline_trace.py` (10 tests)
- `backend/app/sermon_review/progressive_matcher.py` (18 tests)

This checklist walks a fresh session through wiring the state machine + trace into `main.py` behind an org-level feature flag. The state machine is 100% pure with all edge cases covered by tests; the wiring itself is mechanical.

---

## Pre-flight

- [ ] Rebase feature branch on latest `dev` (or `main` if the PR merged) so wiring lands on current code
- [ ] Re-run `pytest tests/test_pipeline_trace.py tests/test_progressive_matcher.py` to confirm 28 baseline tests still pass

---

## Step 1 — Org-level feature flag

Add a boolean flag to org config, default `False`.

- [ ] `backend/app/services/multichurch_store.py` — add `progressiveManuscriptMatching: bool` field to org write shapes (default `False`); expose on the org-read return
- [ ] `backend/app/routes/multichurch.py` — allow super-admins to toggle it via existing org-update endpoint
- [ ] Firestore rules: no change (writes are server-side only)
- [ ] Add a test in `test_services.py` that a newly-created org has the flag `False`

---

## Step 2 — Per-room state manager

Currently the Deepgram receive loop threads state through local variables (`pending_src`, `held_src`, `latest_partial`, etc.). Progressive matcher needs its own per-room state that survives across loop iterations.

- [ ] New file `backend/app/sermon_review/room_state.py`:
  ```python
  @dataclass
  class ProgressiveMatcherRoomState:
      enabled: bool = False           # snapshotted from org config at session start
      cursor: int = 0                 # index into segments
      state: State = field(default_factory=lambda: INITIAL_STATE)
      trace: Optional[PipelineTrace] = None
      segments: list[dict] = field(default_factory=list)  # cached at session start
      preview_shown_at_ms: Optional[int] = None  # written when ShowPreview action fires
  ```
- [ ] Store one instance per active WS session (either in `manager` state or in the `ws_stt_deepgram` handler's locals)
- [ ] Fetch flag + segments at session start (`main.py` around line 1790, near `ws_stt_deepgram` handler entry)

---

## Step 3 — Wire the INTERIM path

**File:** `backend/app/main.py`
**Location:** the interim branch at line ~2691 (`if transcript and not is_final:`)

- [ ] Immediately after `latest_partial = transcript`, if `room_state.enabled`:
  - Lazily create `room_state.trace = PipelineTrace(org_id=org_id, room_id=room_id)` if `room_state.trace is None`
  - Call `room_state.trace.mark_audio_first_partial()`
  - Build event: `event = InterimEvent(prefix=transcript, now_ms=int(time.time()*1000))`
  - Call `new_state, actions = advance(room_state.state, event, cursor=room_state.cursor, segments=room_state.segments)`
  - Update `room_state.state = new_state`
- [ ] Handle each action (`ShowPreview` / `ReplacePreview` / `ClearPreview`) — see Step 5 for broadcast shapes

**Notes:**
- Keep the existing `emit_reviewed_segment_matches(transcript)` call at line 2699 running even when progressive matcher is enabled — it handles a different case (whole-segment containment) that complements prefix matching. Belt-and-suspenders during rollout.
- Do NOT run the progressive matcher on `partial` messages that lack `transcript` — matcher's minimum-prefix gates already filter these but adding a guard here saves work.
- **Second Deepgram receive loop** exists elsewhere in `main.py` (grep `dg_msg.get`); apply the same wiring there.

---

## Step 4 — Wire the COMMIT path

**Location:** where `commit_now(pending_src)` is called (line 2744 in the first loop; grep for others)

- [ ] Before the existing `commit_now`, if `room_state.enabled`:
  - `event = CommitEvent(full_text=pending_src, now_ms=int(time.time()*1000))`
  - `new_state, actions = advance(room_state.state, event, cursor=room_state.cursor, segments=room_state.segments)`
  - `room_state.state = new_state`
- [ ] Handle each action:
  - `ConfirmAndAdvanceCursor(seg_id)` → `room_state.cursor += 1` (or find seg_id's index + 1); mark trace `committed_source="reviewed"`; **skip the existing commit-time `get_reviewed_text` lookup** (already handled by progressive matcher); broadcast only the "commit confirmation" message (no new reviewed text — it's already on screen from the preview)
  - `RequestLiveTranslation(full_text)` → fall through to existing translation path (`_translate_text_guarded` with normal live-translation logic); mark trace `committed_source="live"`, `mark_live_requested()` before dispatch, `mark_live_arrived()` after
- [ ] Emit trace at end of commit: `room_state.trace.mark_broadcast_sent(); room_state.trace.emit(); room_state.trace = None` (reset for next utterance)

---

## Step 5 — Broadcast shapes for progressive-matcher messages

Wire progressive matcher actions to `manager.broadcast_room` payloads. Keep the shape close to existing `translation` messages but add `mode: "reviewed", kind: "preview"` markers so the client's priority-lane handler can route them (Step 7).

- [ ] `ShowPreview(seg_id, reviewed_text)`:
  ```python
  {
      "type": "translation",
      "text": reviewed_text,
      "meta": {
          "mode": "reviewed",
          "kind": "preview",
          "seg_id": seg_id,
          "utterance_id": room_state.trace.utterance_id,
          "seq": <assign a preview seq>,
      },
      "pipelineTrace": room_state.trace.to_broadcast_payload(),
  }
  ```
- [ ] `ReplacePreview(from_seg_id, to_seg_id, reviewed_text)`:
  ```python
  {
      "type": "translation",
      "text": reviewed_text,
      "meta": {
          "mode": "reviewed",
          "kind": "preview_replace",
          "from_seg_id": from_seg_id,
          "seg_id": to_seg_id,
          "utterance_id": ...,
      },
      "pipelineTrace": ...,
  }
  ```
- [ ] `ClearPreview()`:
  ```python
  {
      "type": "translation",
      "text": "",
      "meta": {
          "mode": "reviewed",
          "kind": "preview_clear",
          "utterance_id": ...,
      },
  }
  ```

After emitting each broadcast, call `room_state.trace.mark_broadcast_sent()` — trace is emitted at commit but broadcast timestamps for preview actions are informational only (dropped from trace at emit).

**On `ShowPreview` specifically**, also set `room_state.preview_shown_at_ms = event.now_ms` immediately after the broadcast, so subsequent INTERIM events can compute dwell (the state machine assumes the caller wires this).

---

## Step 6 — `/api/pipeline_trace` endpoint (client echo)

- [ ] New route in `backend/app/routes/pipeline_trace.py`:
  ```python
  @router.post("/pipeline_trace")
  async def receive_client_trace(payload: dict):
      # payload: {utteranceId, receivedAt, renderedAt}
      # Append to a per-utterance dict or forward straight to structured log
      print(f"[PIPELINE_TRACE_CLIENT] {json.dumps(payload, separators=(',', ':'))}")
      return {"ok": True}
  ```
- [ ] Register in `main.py`: `app.include_router(pipeline_trace_routes.router, prefix="/api")`
- [ ] Rate limit: use existing global limit (no per-user auth needed; it's observability)

Note: this is fire-and-forget — client does not care about the response, we do not retry, we do not error if the endpoint is down. Analysis pipeline joins server-side trace + client-side trace by `utteranceId` at query time.

---

## Step 7 — Frontend priority lane (deferred — but sketch here)

Do this last, in a separate PR, when ready to test in a browser.

- [ ] `frontend/utils/useSubtitleSocket.ts`:
  - On inbound message with `meta.mode === "reviewed" && meta.kind === "preview"`: render immediately; skip pacing queue; enforce a 1200ms `Date.now()` timestamp before allowing replacement
  - On `meta.kind === "preview_replace"`: check that `previewShownAt + 1200 <= Date.now()` before actually replacing; if not, ignore (state machine already enforces this server-side, this is belt-and-suspenders)
  - On `meta.kind === "preview_clear"`: check dwell floor; then blank the caption or dim it
  - On any inbound message: `POST /api/pipeline_trace` with `{ utteranceId: meta.utterance_id, receivedAt: Date.now(), renderedAt: <after render commits> }`
- [ ] `frontend/components/TranslationBox.tsx`: no changes if `useSubtitleSocket` handles the state; otherwise mirror the priority-lane logic in the render layer

---

## Step 8 — Rollout dry-run

- [ ] Add pytest integration test that spins up the full flow with a fake WebSocket + a fake Deepgram stream (fixtures like `test_stt_idle_timeout.py`'s `_DummyDeepgram`) and asserts: interim → CANDIDATE → PREVIEW → COMMITTED → cursor advanced → single ShowPreview broadcast
- [ ] Flip flag on ONE test org via a script in `backend/scripts/`
- [ ] Verify on a real Sunday service
- [ ] Analyze `pipeline_trace` output to tune constants
- [ ] After two clean Sundays, remove the flag and enable by default

---

## Line-number references (as of commit `6c9397dc`)

| What | main.py line |
|---|---|
| `ws_stt_deepgram` handler start | 1790 |
| First Deepgram receive loop | ~2620 |
| INTERIM branch (`if transcript and not is_final:`) | 2691 |
| Existing `emit_reviewed_segment_matches(transcript)` | 2699 |
| `if not is_final: continue` (final gate) | 2716 |
| `commit_now(pending_src)` | 2744 |
| Second Deepgram receive loop | grep further down |
| Commit-time `get_reviewed_text` call | 2273 (inside `_translate_text_guarded`) |
| Existing `translation` broadcast msg | 2143, 2439, 3038, 3369 |
| Router registrations | 344-353 |

---

## Test coverage checklist before flipping flag

- [ ] All 28 baseline tests pass (`pipeline_trace` + `progressive_matcher`)
- [ ] New integration test (Step 8) passes
- [ ] Existing WebSocket-recovery tests still pass (`test_socket_manager*`)
- [ ] Manual: run local server with flag on, feed synthetic Deepgram partials via a script, verify broadcast shapes match Step 5
- [ ] Manual: verify pipeline_trace log lines land in stdout / Cloud Run logs
