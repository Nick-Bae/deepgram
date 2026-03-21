# Plan: sermon-accuracy

## Executive Summary

| Perspective | Detail |
|---|---|
| **Problem** | Three accuracy gaps exist: (1) when live translation falls back to GPT-4o, it has no knowledge of the current sermon's vocabulary or style; (2) translation corrections exist in the backend but are completely disconnected from the live host console; (3) in-memory sermon state is wiped on server restart, silently degrading mid-service accuracy. |
| **Solution** | A layered set of backend improvements: sermon-aware dynamic few-shot injection, expanded context window, keyword glossary extraction, adaptive thresholding, better draft quality, per-org corrections, STT vocabulary correction, and Firestore-backed script persistence. |
| **Function / UX Effect** | Off-script sentences match the sermon's exact vocabulary. Mid-service translation errors can be corrected once and immediately improve all future similar sentences. A server restart no longer silently kills pre-loaded sermon accuracy. |
| **Core Value** | The platform's core promise — "upload your sermon and get accurate real-time translation" — becomes true for the full service, not just the scripted 70%. |

---

## 1. Current System Inventory

Understanding what already exists before adding anything.

### 1.1 Pre-Service Accuracy Inputs
| Input | How It Works | Gap |
|---|---|---|
| **Sermon prep draft** | Admin pastes Korean → GPT-4o translates each sentence → admin edits | Draft uses `compact_prompt=True` (simplified prompt) — accuracy below potential |
| **Sermon finalize** | Reviewed pairs → loaded into `script_store` (in-memory) | In-memory only: server restart during service silently wipes it |
| **Org prompt settings** | `custom_prompt` + `service_prompt` (2000 chars each) | Free-text with no structure; both labeled "[advisory only]" → weak signal |
| **Script upload** | Raw pairs JSON → re-loadable at any time | No UI affordance to reload a saved sermon on the host console |

### 1.2 Live Translation Inputs
| Input | How It Works | Gap |
|---|---|---|
| **Fuzzy match ("pre" mode)** | SequenceMatcher ≥ 0.84 → use pre-translated text | Fixed threshold for all text lengths; short fragments under-matched |
| **GPT-4o ("live" mode)** | Falls back when no match; uses system prompt | Zero knowledge of current sermon vocabulary, topic, or style |
| **Static theological glossary** | 9 hardcoded terms (은혜→grace, 성령→Holy Spirit, etc.) | Sermon-specific terms not covered |
| **Bible names list** | 224 Korean→English mappings | Accurate but static; no sermon context |
| **Recent context pairs** | Last 2 translated pairs sent to GPT-4o | Too small (2 pairs); context lost on reconnect |
| **Scripture lookup** | Detects Bible references → ESV text injected | Works well; no gap |

### 1.3 Post-Service Correction System (Hidden)
| Feature | Status | Gap |
|---|---|---|
| `POST /examples/correct` | Exists, works | **No UI** — host console has no button to trigger it |
| `translation_examples.jsonl` | Logs all translations + corrections | **Global** (not per-org) — one church's corrections bleed into all others |
| Few-shot examples | Top corrected examples injected into future prompts | Only activated for corrected rows; most rows never get corrected |
| `GET/POST /examples/update` | Admin can retroactively correct past translations | No discovery path from host console |

---

## 2. Improvement Ideas (All 9)

### Idea 1: Script-Sourced Dynamic Few-Shot Examples (HIGH)
**What:** When GPT-4o is called (no direct script match), find the 2-3 most contextually similar sermon pairs and inject them as style examples into the system prompt.
**Why it works:** GPT-4o immediately learns the pastor's exact vocabulary choices ("the resurrection" not "rising from the dead") from the pairs he already curated.
**Cost:** Zero extra API calls. One extra pass over the in-memory pairs list — same O(N) as the existing `match()` scan.

### Idea 2: Dynamic Keyword Glossary from Script (HIGH)
**What:** From all loaded script pairs, extract short recurring Korean terms (≤6 chars, appearing in ≥2 pairs) and their English equivalents. Inject as a mini-glossary into the system prompt.
**Why it works:** Sermon-specific proper nouns, place names, and repeated theological phrases are enforced consistently, just like the static glossary.
**Example:** If the script has "새벽기도" in 8 sentences, extract "새벽기도 → dawn prayer" and enforce it across all live translations.
**Cost:** Computed once on script load, cached per version.

### Idea 3: Increase Recent Context Window (MEDIUM)
**What:** Increase `_build_recent_context_block` from 2 → 4 pairs; `TranslationContext.remember()` from 3 → 5 pairs.
**Why it works:** Subject continuity errors (wrong pronouns, repeated subject drops) happen because GPT-4o sees too little prior context. 4 pairs covers a full narrative paragraph.
**Cost:** ~60 extra tokens per request.

### Idea 4: Adaptive Fuzzy Match Threshold (MEDIUM)
**What:** Instead of a fixed 0.84 threshold, apply a length-aware threshold: short fragments (< 15 chars) use a lower threshold (0.72); standard sentences use the org threshold (0.84).
**Why it works:** Short Korean fragments like "아멘" or "할렐루야" rarely hit 0.84 against their script equivalents due to STT word count differences, but they have obvious correct translations.
**Cost:** One additional comparison in the `match()` function.

### Idea 5: Better Sermon Draft Translation Quality (HIGH)
**What:** Change sermon draft translation from `compact_prompt=True` to `compact_prompt=False` (the full theological prompt). Add a per-segment concurrency limit to avoid rate limits.
**Why it works:** The draft quality directly sets the ceiling for human correction — better drafts = less editing = better final pairs = better live accuracy.
**Cost:** Slightly more tokens per draft request. Draft is asynchronous; latency impact is acceptable.

### Idea 6: Firestore-Backed Script Persistence (HIGH)
**What:** When `POST /sermon/finalize` succeeds, also write the pairs to Firestore (`organizations/{orgId}/sermons/{sermonId}`). On WebSocket connection start, if `script_store` is empty for an org, auto-load the most recently finalized sermon from Firestore.
**Why it works:** Currently a backend container restart (routine in Cloud Run) silently wipes the sermon mid-service. The pastor and audience notice this as suddenly worse translations with no warning.
**Cost:** One Firestore write on finalize; one Firestore read on connection start (only if store is empty).

### Idea 7: Sermon-Aware STT Vocabulary Correction (MEDIUM)
**What:** Build a compact word-level vocabulary set from script pairs. Before fuzzy matching, check each token in the STT output against the vocab set using edit distance ≤ 1. If a near-miss is found, correct it.
**Why it works:** Deepgram nova-3 frequently mishears Korean phonemes under real church acoustics (reverb, microphone distance). "은혜" becomes "은해", "성령" becomes "성녕". STT vocabulary correction catches these before they break fuzzy matching.
**Example:** STT: "하나님의 은해가 넘칩니다" → corrected: "하나님의 은혜가 넘칩니다" → now matches script pair → "pre" mode used instead of GPT-4o.
**Cost:** Computed once on script load (vocab set). Edit distance check is O(vocab_size × word_count) — negligible for typical sermon sizes.

### Idea 8: Live Correction UI in Host Console (HIGH)
**What:** Add a "Correct this translation" button on each displayed translation in the host console. Opens a quick inline editor: shows Korean + current English, admin edits English, submits. Saves to `POST /examples/correct` with org_id.
**Why it works:** Corrections are the most valuable few-shot signal (human-verified, sermon-specific). Currently this system exists but has zero UI access path. One correction mid-service can fix the same phrase for the rest of the service.
**Cost:** Frontend: add edit UI to TranslationBox. Backend: add `org_id` field to correction record.

### Idea 9: Per-Org Correction Isolation (MEDIUM)
**What:** Add `org_id` to correction records in `translation_examples.jsonl`. In `_load_fewshot_examples()`, filter to current org first; if fewer than N results, fall back to global.
**Why it works:** A Korean-American church's corrections for Reformed theological vocabulary should not affect a Korean immigrant church using different English style choices.
**Cost:** Small schema change to JSONL format. Backwards-compatible (records without `org_id` treated as global).

---

## 3. Implementation Plan

### 3.1 Prioritization

| ID | Idea | Impact | Effort | Risk | Priority |
|---|---|---|---|---|---|
| I1 | Script-sourced dynamic few-shot examples | High | Low | Low | **P1** |
| I2 | Dynamic keyword glossary from script | High | Low | Low | **P1** |
| I5 | Better sermon draft quality (full prompt) | High | Trivial | None | **P1** |
| I3 | Increase context window 2→4 | Medium | Trivial | None | **P1** |
| I4 | Adaptive fuzzy match threshold | Medium | Low | Low | **P2** |
| I6 | Firestore-backed script persistence | High | Medium | Medium | **P2** |
| I7 | Sermon-aware STT vocabulary correction | Medium | Medium | Low | **P2** |
| I8 | Live correction UI (host console) | High | Medium | Low | **P2** |
| I9 | Per-org correction isolation | Medium | Low | Low | **P2** |

### 3.2 Implementation Order

#### Phase 1 — Pure backend, zero latency cost, no schema changes
1. **I5** — Fix sermon draft to use full prompt (`compact_prompt=False`)
2. **I3** — Increase context window
3. **I1** — `ScriptStore.match_with_examples()` + prompt injection
4. **I2** — `ScriptStore.get_keyword_glossary()` + prompt injection

#### Phase 2 — Backend logic changes
5. **I4** — Adaptive fuzzy threshold in `match()`
6. **I9** — Add `org_id` to correction records + per-org filtering
7. **I7** — STT vocabulary correction using script pairs

#### Phase 3 — Persistence + Frontend
8. **I6** — Firestore script persistence + auto-reload on connect
9. **I8** — Live correction UI in host console

---

## 4. Detailed Acceptance Criteria

### Phase 1

**I5 — Full prompt for sermon draft**
- AC: `POST /sermon/draft` calls `translate_text` with `compact_prompt=False`
- AC: Theological glossary and Bible names are included in draft translations
- AC: Concurrency limit unchanged (`SERMON_TRANSLATION_CONCURRENCY`)

**I3 — Increase context window**
- AC: `_build_recent_context_block` returns up to 4 pairs (was 2)
- AC: `TranslationContext.remember()` retains up to 5 pairs (was 3)
- AC: No change to broadcast payload or WebSocket protocol

**I1 — Script-sourced few-shot examples**
- AC: `ScriptStore.match_with_examples(text, org_id=...)` does one scan and returns both `(best_match, score, version, threshold)` AND `examples: list[ScriptPair]`
- AC: `examples` contains pairs with 0.20 ≤ score < threshold, capped at 3
- AC: If no pairs score ≥ 0.20, returns first 2 script pairs as style anchors
- AC: If script store empty, returns `examples=[]`
- AC: `translate_text(script_examples=[...])` injects them after the cached system prompt base (cache not invalidated)
- AC: `compact_prompt=True` → examples NOT injected (partial translations)
- AC: Both WebSocket handlers use `match_with_examples()`

**I2 — Dynamic keyword glossary**
- AC: `ScriptStore.get_keyword_glossary(org_id=...)` extracts Korean tokens ≤6 chars appearing in ≥2 pairs with their English equivalents
- AC: Returns at most 15 term pairs (to control token budget)
- AC: Cached per (org_id, store_version) — not recomputed on every request
- AC: Injected into system prompt as "Key terms in this sermon: X→Y, A→B"
- AC: Empty when script store has fewer than 2 pairs loaded

### Phase 2

**I4 — Adaptive threshold**
- AC: Text with < 15 compact chars uses `min(threshold, 0.72)` as effective threshold
- AC: Text with ≥ 15 compact chars uses the org threshold unchanged
- AC: The `meta_payload` still reports the org threshold (not the adjusted one) for consistency

**I9 — Per-org corrections**
- AC: `POST /examples/correct` (and `log_corrected_translation`) accepts and stores `org_id`
- AC: `_load_fewshot_examples()` with `org_id` param returns org-specific records first, fills to max with global records
- AC: Backwards-compatible: records without `org_id` treated as global

**I7 — STT vocabulary correction**
- AC: `ScriptStore.get_vocab_set(org_id=...)` returns a set of unique Korean words from all script source texts
- AC: `_stt_vocab_correct(text, vocab_set)` replaces STT tokens that are 1 edit distance from a vocab word with the vocab word
- AC: Applied in `_preprocess_source_text()` when source is Korean and vocab_set is non-empty
- AC: Correction only applied when the vocab word length ≥ 3 (avoid correcting particles)
- AC: Used in both WebSocket handlers

### Phase 3

**I6 — Firestore script persistence**
- AC: `POST /sermon/finalize` writes pairs to `organizations/{orgId}/sermons/{sermonId}` in Firestore
- AC: On `ws/stt_deepgram` connect, if `script_store` is empty for the org, fetch the most recently created sermon document and load it
- AC: Firestore write fails silently (does not fail the finalize response)
- AC: Auto-reload is fire-and-forget (does not delay WebSocket handshake)
- AC: Firestore security rule: `organizations/{orgId}/sermons/{sermonId}` — write only from backend; no client read

**I8 — Live correction UI**
- AC: Each translation line in host console has a "Correct" action (pencil icon)
- AC: Clicking opens an inline editor with the Korean source and current English pre-filled
- AC: On submit, calls `POST /examples/correct` with `org_id`, `stt_text`, `auto_translation`, `final_translation`
- AC: Success dismisses the editor; no page reload
- AC: Correction is visible as a visual indicator on that translation line for the session

---

## 5. Files to Modify

| File | Phase | Changes |
|---|---|---|
| `backend/app/routes/script.py` | P1 | Remove `compact_prompt=True` from `_translate_segments()` |
| `backend/app/utils/translate.py` | P1 | Increase context window; add `_build_script_examples_block()`; add `script_examples` + `script_glossary` params to `_build_system_prompt()` and `translate_text()` |
| `backend/app/services/script_store.py` | P1,P2 | Add `match_with_examples()`, `get_keyword_glossary()`, `get_vocab_set()` |
| `backend/app/main.py` | P1,P2 | Both WS handlers: use `match_with_examples()`, pass examples + glossary; add STT vocab correction |
| `backend/app/utils/translate.py` | P2 | Add `org_id` to `_log_translation_example()`, `_load_fewshot_examples()` |
| `backend/app/routes/examples.py` | P2 | Accept and store `org_id` in `CorrectionPayload` |
| `backend/app/services/multichurch_store.py` | P3 | Add `save_sermon_pairs()`, `get_latest_sermon_pairs()` |
| `backend/firestore/firestore.rules` | P3 | Add `sermons/{sermonId}` rule |
| `backend/app/routes/script.py` | P3 | Write to Firestore on finalize |
| `backend/app/main.py` | P3 | Auto-reload sermon on WS connect |
| `frontend/components/TranslationBox.tsx` | P3 | Add correction UI per translation line |

**No new dependencies required.**

---

## 6. Risk Assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| Full prompt makes draft 2× slower | Low | Draft is async background; UX shows spinner. Concurrency limit prevents rate limits. |
| Keyword glossary extraction has false positives | Low | Min 2-pair frequency + max 6-char limit avoids sentence fragments. Model treats it as soft guidance. |
| STT vocab correction over-corrects | Low | Only applies when edit distance = 1 AND vocab word length ≥ 3. Rare word collisions unlikely. |
| Firestore write latency slows finalize | None | Write is fire-and-forget; response returned immediately. |
| Larger system prompt increases latency | Low | GPT-4o is token-fast; extra ~300 tokens adds < 30ms on typical response. |
| Script examples confuse model with unrelated context | Low | Score filter (≥ 0.20) + model instruction "use for style/vocab reference only" prevents misuse. |
