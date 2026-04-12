# Translation Latency Optimization Planning Document

> **Summary**: Reduce end-to-end latency from Korean speech to English display/TTS by eliminating blocking I/O in the hot translation path and reducing OpenAI prompt token overhead.
>
> **Project**: Real-Time Translation Platform
> **Author**: namjubae
> **Date**: 2026-04-11
> **Status**: Draft

---

## Executive Summary

| Perspective | Content |
|-------------|---------|
| **Problem** | Several synchronous operations in the translation hot path block the async event loop and inflate OpenAI prompt sizes, adding measurable latency (200–1500 ms) to every utterance |
| **Solution** | Move blocking I/O off the event loop (executor), cache fewshot file reads with mtime, reduce system prompt token count by relocating the Bible names list, and strip `[[Tn]]` from streaming display |
| **Function/UX Effect** | Listeners see translated text and hear TTS faster; no `[[Tn]]` artifacts in display during streaming; the event loop stays unblocked so all rooms benefit simultaneously |
| **Core Value** | Lower perceived latency makes the translation experience feel real-time rather than delayed, which is the core value proposition for church attendees |

---

## 1. Overview

### 1.1 Purpose

After the event-loop blocking fixes in commit analysis (`live_rooms` + `touch_audio`), several
additional latency sources remain in the hot path for every translated utterance.
This plan documents and prioritizes them for implementation.

### 1.2 Background

**Current translation pipeline (per utterance):**

```
Deepgram is_final=True
  → from_deepgram_to_server()
    → preprocess_source_text()      ← fast
    → script_store.match_with_examples()  ← CPU-bound fuzzy match, on event loop
    → detect_scripture_verse()      ← regex scan, on event loop
    → _translate_streaming_guarded()
        → _build_system_prompt()
            → _build_fewshot_block()
                → _load_fewshot_examples()  ← SYNCHRONOUS FILE READ every call
            → Bible names block (224+ entries, ~1500 tokens)
        → OpenAI GPT-4o streaming   ← TTFT depends heavily on prompt size
    → broadcast_room(stream_token)  ← raw [[Tn]] visible in display
  → final broadcast                 ← [[Tn]] replaced
```

**Measured / estimated overhead:**

| Source | Overhead | Frequency |
|--------|----------|-----------|
| `_load_fewshot_examples()` file read | 5–50 ms (grows over time) | Every GPT call |
| Bible names list in system prompt | ~1500 extra prompt tokens → ~50–150 ms TTFT | Every GPT call |
| `script_store.match_with_examples()` CPU | 1–20 ms for large sermons | Every utterance |
| `detect_scripture_verse()` regex | 1–10 ms | Every Korean utterance |
| `[[Tn]]` visible in display | Flash of placeholder text | Every GPT-translated utterance |

### 1.3 Related Documents

- Previous latency investigation: `82f8da9f` commit analysis (blocking `live_rooms` + `touch_audio`)
- Architecture: `CLAUDE.md`

---

## 2. Scope

### 2.1 In Scope

- [ ] **Plan A** — Cache `_load_fewshot_examples()` with mtime (stop re-reading file every call)
- [ ] **Plan B** — Remove Bible names list from system prompt; redirect to Deepgram keyterms
- [ ] **Plan C** — Show only complete final sentence in display (suppress streaming fragments)
- [ ] **Plan D** — Move `script_store.match_with_examples()` to thread executor
- [ ] **Plan E** — Move `detect_scripture_verse()` to thread executor

### 2.2 Out of Scope

- Changing Deepgram endpointing parameters (already tuned to 500ms/600ms)
- Changing KoChunker timing constants (already tuned via ENV)
- Replacing OpenAI model (gpt-4o already selected for quality)
- Infrastructure changes (Cloud Run scaling, regions)
- Frontend WebSocket reconnect / buffering logic

---

## 3. Requirements

### 3.1 Functional Requirements

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| FR-01 | `_load_fewshot_examples()` must use mtime-based cache so it only re-reads when the file changes | High | Pending |
| FR-02 | Bible names must not appear in the system prompt; they should be provided to Deepgram as keyterms instead | High | Pending |
| FR-03 | Frontend display must strip `[[T\d+]]` placeholder tokens from streamed translation text before rendering | High | Pending |
| FR-04 | `script_store.match_with_examples()` must be called via `run_in_executor` so it never blocks the event loop | Medium | Pending |
| FR-05 | `detect_scripture_verse()` must be called via `run_in_executor` | Medium | Pending |
| FR-06 | All existing translation quality (subject continuity, pronoun guardrails) must be preserved | High | Pending |
| FR-07 | Bible name accuracy must not regress (Deepgram keyterms must carry the same correction coverage) | High | Pending |

### 3.2 Non-Functional Requirements

| Category | Criteria | Measurement Method |
|----------|----------|-------------------|
| Latency | Time-to-first-token improvement ≥ 50 ms after Bible names removal | Compare prompt token count before/after |
| Latency | `_load_fewshot_examples()` takes < 1 ms on warm cache | Timing log |
| Correctness | Bible proper nouns (e.g., 엘리야→Elijah) correctly output by GPT | Manual spot test during service |
| Stability | Event loop not blocked by script matching on 500+ pair sermons | No new translation freezes reported |
| UX | No `[[Tn]]` visible in listener display during streaming | Visual inspection |

---

## 4. Detailed Analysis

### 4.1 Plan A — `_load_fewshot_examples()` cache (High Priority)

**Location**: `backend/app/utils/translate.py` — `_load_fewshot_examples()` called from `_build_fewshot_block()` which is called from `_build_system_prompt()` on every GPT call.

**Current behavior**: Reads `translation_examples.jsonl` line-by-line on every call. No cache. File grows unboundedly as corrections accumulate.

**Fix**: Add mtime-based cache (same pattern as `_CUSTOM_PROMPT_CACHE` and `_SERVICE_PROMPT_CACHE`):

```python
_FEWSHOT_CACHE: dict[str, object] = {"mtime": None, "ko_en": []}

def _load_fewshot_examples(...) -> List[dict]:
    try:
        stat = _TRANSLATION_LOG_PATH.stat()
    except FileNotFoundError:
        return []
    mtime = stat.st_mtime
    if _FEWSHOT_CACHE.get("mtime") == mtime:
        # Filter cached rows for relevance — no file I/O
        return _filter_fewshot(_FEWSHOT_CACHE["ko_en"], ...)
    # Re-read only when file changed
    rows = _read_fewshot_file()
    _FEWSHOT_CACHE.update({"mtime": mtime, "ko_en": rows})
    return _filter_fewshot(rows, ...)
```

**Expected gain**: 5–50 ms per translation call eliminated; file I/O only on new corrections.

### 4.2 Plan B — Remove Bible names from system prompt (High Priority)

**Location**: `backend/app/utils/translate.py` — `_build_system_prompt_base()` appends 224+ Bible name mappings to every system prompt.

**Current overhead**: 224 lines × ~6 tokens/line ≈ 1344 extra prompt tokens per call.
At GPT-4o's typical 10 ms/100-token processing rate, this adds ~130 ms to time-to-first-token.

**Fix options**:
1. Remove `bible_names_block` from system prompt entirely — Deepgram `nova-3` with keyterms already corrects Korean Bible book names at the STT layer.
2. Keep only the 10–15 most commonly mis-translated Bible names (those NOT correctable by Deepgram) as a short fallback list.

**Risk**: If Deepgram's keyterm correction misses a rare Bible name and GPT doesn't have the mapping, the name may be transliterated rather than correctly translated.

**Mitigation**: Audit which names Deepgram already handles correctly via `DEFAULT_DEEPGRAM_KEYWORDS` and `DEFAULT_DEEPGRAM_REPLACEMENTS`. Only remove names that Deepgram already handles. Retain ~20 critical names (proper nouns not in Deepgram's base vocabulary).

**Expected gain**: ~100–150 ms TTFT improvement per translation, plus cost reduction.

### 4.3 Plan C — Strip `[[Tn]]` from streaming display (High Priority)

**Location**: `frontend/utils/useTranslationSocket.ts` — `translation_stream_token` handler.

**Current behavior**: Raw masked tokens (`[[T4]]`) are broadcast as streaming tokens and rendered directly in `TranslationBox.tsx`. The final `translation` message replaces them, but there's a visible flash.

**Fix**: Strip `[[T\d+]]` from streaming token text before updating display state:

```typescript
// In translation_stream_token handler:
const cleanToken = token.replace(/\[\[T\d+\]\]/g, '');
```

Note: TTS is already fixed (`enqueueFinalTTS`). This fix addresses the display.

**Expected gain**: No visible `[[Tn]]` artifacts in listener display during streaming.

### 4.4 Plan D — `script_store.match_with_examples()` executor (Medium Priority)

**Location**: `backend/app/main.py` — `send_translation()` and `handle_commit()`.

**Current behavior**: Fuzzy string matching runs synchronously on the async event loop. For sermons with 200–500 pairs and a complex Korean input, this can take 5–20 ms, blocking all other coroutines.

**Fix**:
```python
script_match, match_score, script_version, script_threshold, examples = \
    await asyncio.get_running_loop().run_in_executor(
        None, script_store.match_with_examples, clean_src, room_id
    )
```

**Risk**: `script_store` must be thread-safe. Verify `ScriptStore` uses thread-safe reads (dict/list reads in CPython are GIL-protected; no lock needed for reads).

### 4.5 Plan E — `detect_scripture_verse()` executor (Medium Priority)

**Location**: `backend/app/main.py` — `send_translation()`.

**Current behavior**: Scripture verse detection runs regex patterns against a verse database synchronously.

**Fix**:
```python
scripture_hit = await asyncio.get_running_loop().run_in_executor(
    None, detect_scripture_verse, clean_src
)
```

---

## 5. Success Criteria

### 5.1 Definition of Done

- [ ] FR-01: `_load_fewshot_examples` cache implemented and verified (warm cache < 1 ms)
- [ ] FR-02: Bible names removed from `_build_system_prompt_base()`; keyterms audited
- [ ] FR-03: Frontend display strips `[[Tn]]` from stream tokens
- [ ] FR-04: `match_with_examples` wrapped in executor
- [ ] FR-05: `detect_scripture_verse` wrapped in executor
- [ ] FR-06: Translation quality spot-checked (5 test utterances including Bible name references)
- [ ] FR-07: Bible name test: 엘리야→Elijah, 에스더→Esther, 느헤미야→Nehemiah translate correctly

### 5.2 Quality Criteria

- [ ] No new ESLint errors (`npm run lint`)
- [ ] Backend starts without errors
- [ ] Manual live test: translation appears within 1 s of speech final for a typical Korean sentence
- [ ] No `[[Tn]]` visible in display during streaming

---

## 6. Risks and Mitigation

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Bible name accuracy regression after removing names from prompt | Medium | Low | Audit Deepgram keyterm coverage; keep 15–20 critical fallback names |
| Fewshot cache serving stale examples after file update | Low | Low | Cache key is file mtime — any write updates mtime |
| `script_store` thread-safety issue with executor | Medium | Low | CPython GIL protects list/dict reads; verify no mutable state during reads |
| `detect_scripture_verse` not thread-safe | Low | Low | Inspect implementation before moving to executor |

---

## 7. Architecture Considerations

### 7.1 Project Level

**Enterprise** — existing FastAPI + Firestore + OpenAI architecture. Changes are localized to:
- `backend/app/utils/translate.py` (Plans A, B)
- `backend/app/main.py` (Plans D, E)
- `frontend/utils/useTranslationSocket.ts` (Plan C)

### 7.2 Key Architectural Decisions

| Decision | Selected | Rationale |
|----------|----------|-----------|
| Cache strategy for fewshot | mtime-based dict cache (same pattern as custom/service prompt) | Consistent with existing codebase patterns |
| Bible names approach | Remove from system prompt; audit Deepgram keyterm coverage | Deepgram STT layer is the right place for transcription correction |
| Blocking work isolation | `run_in_executor(None, fn)` (thread pool) | Consistent with existing `live_rooms` and `touch_audio` fixes |
| Display streaming fix | Strip regex on token in `useTranslationSocket.ts` | Minimal, targeted fix; no state machine change needed |

---

## 8. Implementation Order

Ordered by impact × risk ratio (highest first):

1. **Plan C** — Frontend `[[Tn]]` display strip (5 min, zero risk, immediate UX fix)
2. **Plan A** — Fewshot cache (15 min, low risk, eliminates file I/O per call)
3. **Plan B** — Bible names system prompt removal (30 min, medium risk, requires audit)
4. **Plan D** — Script match executor (10 min, low risk)
5. **Plan E** — Scripture detection executor (10 min, verify thread safety first)

---

## 9. Next Steps

1. [ ] Create design document (`translation-latency-optimization.design.md`)
2. [ ] Audit `DEFAULT_DEEPGRAM_KEYWORDS` / `DEFAULT_DEEPGRAM_REPLACEMENTS` for Bible name coverage (Plan B prerequisite)
3. [ ] Start implementation in order above
4. [ ] Manual spot-test after each plan

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-04-11 | Initial draft from codebase analysis | namjubae |
