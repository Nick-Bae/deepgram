# Translation Latency Improvement Planning Document

> **Summary**: Reduce end-to-end translation latency from ~2.5s to <1s by addressing five identified bottlenecks in the STT → LLM → broadcast pipeline, implemented one at a time in order of risk.
>
> **Project**: Real-Time Translation Platform
> **Author**: namjubae
> **Date**: 2026-04-10
> **Status**: Draft

---

## Executive Summary

| Perspective | Content |
|-------------|---------|
| **Problem** | End-to-end latency from last spoken word to listener display is ~2.1–3.0 s, dominated by a 1500 ms Deepgram silence gate, two hardcoded hold timers (850 ms combined), and a full-response-wait OpenAI call (300–800 ms). This makes the translation feel noticeably behind the speaker. |
| **Solution** | Four sequential improvements: (A) tune Deepgram endpointing env vars, (B) externalize hardcoded commit-delay constants, (C) switch partial-preview calls to gpt-4o-mini, (D) implement OpenAI streaming for final translations. Each is independently deployable and reversible. |
| **Function/UX Effect** | Listeners see translated text within ~800 ms of the speaker finishing a clause instead of ~2.5 s. Partial previews appear faster during speech. No user-visible UI changes required for Plans A–C; Plan D adds a streaming word-by-word display. |
| **Core Value** | Real-time feel is the core promise of the product. A <1 s latency gap is imperceptible in a live worship setting; the current 2–3 s gap is noticeable and disruptive for listeners trying to follow the sermon. |

---

## 1. Overview

### 1.1 Purpose

Improve the perceived and measured translation latency across the entire pipeline:

```
[Speaker speaks]
  → Deepgram STT (nova-3, Korean)
  → KoChunker / commit-delay logic
  → OpenAI GPT-4o translation
  → WebSocket broadcast to listeners
  → TranslationBox.tsx display
```

The current worst-case path adds **1500 + 600 + 250 = 2350 ms of idle waiting** before the OpenAI call even starts.

### 1.2 Background

Detailed profiling (static analysis of the pipeline) identified five bottlenecks:

| # | Bottleneck | Location | Current Value | Latency Added |
|---|---|---|---|---|
| 1 | Deepgram endpointing | `deepgram_session.py` | `DG_ENDPOINTING_MS=1500` | 1500 ms |
| 2 | CJK pending hold | `main.py` (hardcoded) | `CJK_PENDING_HOLD_MS=600` | up to 600 ms |
| 3 | Commit wait timer | `main.py` (hardcoded) | `COMMIT_WAIT_MS=250` | 250 ms |
| 4 | OpenAI non-streaming | `translate.py` | full response await | 300–800 ms |
| 5 | GPT-4o for partial previews | `main.py` | same model as finals | cost + latency |

**Pre-script matches** (sermon script lookup) already bypass bottleneck #4 and achieve ~100 ms latency — the existing fast path works well. The improvements here focus on the LLM path.

### 1.3 Related Documents

- Analysis: [full latency analysis conversation, 2026-04-10]
- Code: `backend/app/main.py`, `backend/app/utils/translate.py`, `backend/app/deepgram_session.py`

---

## 2. Scope

### 2.1 In Scope

- [ ] **Plan A** — Tune `DG_ENDPOINTING_MS` and `DG_UTTER_END_MS` via env var (no code change)
- [ ] **Plan B** — Externalize `COMMIT_WAIT_MS` and `CJK_PENDING_HOLD_MS` as env vars in `main.py`
- [ ] **Plan C** — Use `gpt-4o-mini` for partial preview translations (compact_prompt path)
- [ ] **Plan D** — Implement OpenAI streaming (`stream=True`) for final translation broadcasts

### 2.2 Out of Scope

- Deepgram model change (nova-3 → nova-2 or others) — separate accuracy tradeoff
- Frontend display animation / word-by-word rendering design — separate UX feature
- KoChunker algorithm changes (wait-k parameters, sentence boundary logic)
- TTS latency optimization
- Multi-language (non-Korean) path optimization

---

## 3. Requirements

### 3.1 Functional Requirements

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| FR-01 | Plan A: `DG_ENDPOINTING_MS` default reduced; existing env override still works | High | Pending |
| FR-02 | Plan B: `COMMIT_WAIT_MS` and `CJK_PENDING_HOLD_MS` readable from env vars with safe fallback defaults | High | Pending |
| FR-03 | Plan C: Partial preview (`compact_prompt=True`) path uses `gpt-4o-mini`; final commit path unaffected | Medium | Pending |
| FR-04 | Plan D: Final translation response streamed token-by-token to listener WebSockets | High | Pending |
| FR-05 | All plans must not break existing pre-script match path (latency must remain ≤100 ms for matched segments) | High | Pending |
| FR-06 | All plans must be independently deployable and reversible (env flag or safe default) | High | Pending |

### 3.2 Non-Functional Requirements

| Category | Criteria | Measurement Method |
|----------|----------|-------------------|
| Latency (target) | Final translation visible to listeners within 800 ms of utterance end | Manual timing with stopwatch on live session after each plan |
| Latency (partial) | Partial preview appears within 400 ms of chunker emit | Console timestamps in `send_translation()` |
| Translation quality | No regression in translation accuracy for final commits | Side-by-side comparison during test sessions |
| Stability | No new WebSocket disconnects or error rate increase | Cloud Run logs after each plan deployment |
| Cost | Plan C reduces OpenAI token cost for previews by ~60% (mini vs 4o) | OpenAI usage dashboard |

---

## 4. Success Criteria

### 4.1 Definition of Done (per plan)

**Plan A** (env var only)
- [ ] `DG_ENDPOINTING_MS=500` tested in dev session
- [ ] No false mid-sentence commits observed in 10-minute test session
- [ ] Value documented in `backend/.env.example`

**Plan B** (externalize constants)
- [ ] `COMMIT_WAIT_MS` and `CJK_PENDING_HOLD_MS` read from env with defaults matching current behavior
- [ ] Both vars documented in `CLAUDE.md` env vars section
- [ ] Behavior unchanged when vars not set (backward compatible)

**Plan C** (gpt-4o-mini for partials)
- [ ] Partial preview path calls `gpt-4o-mini` (or configured model)
- [ ] Final commit path still uses `gpt-4o` (or configured model)
- [ ] Model selection is env-configurable (`OPENAI_PARTIAL_MODEL`)
- [ ] Cost reduction measurable in OpenAI dashboard

**Plan D** (OpenAI streaming)
- [ ] `translate_text()` supports `stream=True` mode
- [ ] Token chunks are forwarded to listeners via WebSocket as they arrive
- [ ] Frontend `TranslationBox.tsx` handles incremental token updates without flicker
- [ ] Final committed text is identical to non-streaming output
- [ ] Rate limiting and token budget accounting still work correctly

### 4.2 Quality Criteria

- [ ] `npm run lint` passes (frontend)
- [ ] `backend/venv/bin/python -m pytest` passes (if test suite exists)
- [ ] No regressions in pre-script match path
- [ ] Cloud Run deployment succeeds after each plan

---

## 5. Risks and Mitigation

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Lower `DG_ENDPOINTING_MS` causes false commits mid-sentence (Korean speakers pause within clauses) | Medium | Medium | Start with 500 ms and monitor; increase to 700 ms if false commits occur; KoChunker provides a second layer of boundary detection |
| Streaming (Plan D) complicates rate-limit token accounting (`_reserve_translation_budget`) | Medium | Medium | Reserve estimated tokens at stream start; settle with actual tokens at stream end (same pattern as current) |
| Streaming (Plan D) adds frontend complexity — partial text flicker or duplicate display | Medium | Medium | Implement Plan D after A+B+C are validated; use sequence numbers to deduplicate |
| `gpt-4o-mini` quality for partial previews is noticeably worse | Low | Low | Partials are ephemeral (replaced by final); quality threshold for previews is lower |
| Reducing `CJK_PENDING_HOLD_MS` below 300 ms causes premature commits for long Korean compound verbs | Medium | Low | Default to 300 ms; expose as env var so it can be tuned per deployment |
| Plans interact — tuning multiple params simultaneously makes root cause harder to identify | Medium | Low | Implement and test one plan at a time; deploy separately |

---

## 6. Architecture Considerations

### 6.1 Project Level

**Dynamic** — existing FastAPI + Next.js fullstack app. No new services or infra needed.

### 6.2 Key Architectural Decisions

| Decision | Options | Selected | Rationale |
|----------|---------|----------|-----------|
| Streaming transport | Server-Sent Events / WebSocket token chunks / batch | WebSocket token chunks | WebSocket already established; adding streaming tokens is minimal change |
| Partial model config | Hardcode gpt-4o-mini / env var | Env var `OPENAI_PARTIAL_MODEL` | Consistent with existing pattern (`OPENAI_TRANSLATION_MODEL`); allows future tuning |
| Commit delay config | Hardcode / env var / bkit config | Env var | Zero-downtime tuning in Cloud Run without code push |
| Streaming state on frontend | Replace-in-place / append tokens / full replace on final | Replace-in-place with token appending, replaced by final | Minimizes flicker; existing rolling update logic in `TranslationBox` can be extended |

### 6.3 Implementation Order

```
Plan A (env only)
  → no code change, deploy immediately

Plan B (~5 lines, backend only)
  main.py: COMMIT_WAIT_MS = _env_int("COMMIT_WAIT_MS", 100, ...)
           CJK_PENDING_HOLD_MS = _env_int("CJK_PENDING_HOLD_MS", 300, ...)

Plan C (~2 lines, backend only)
  translate.py or main.py: resolve partial model from OPENAI_PARTIAL_MODEL env
  partial preview call uses resolved model instead of default

Plan D (~100 lines, backend + frontend)
  translate.py: add streaming path to translate_text()
  main.py:  send_translation() streams tokens via ws.send_json()
  TranslationBox.tsx: handle streaming token messages
```

---

## 7. Convention Prerequisites

### 7.1 Existing Project Conventions (verified)

- [x] `CLAUDE.md` has coding conventions section — Python async, env vars via `os.getenv()` with defaults
- [x] Env var pattern: `_env_int(name, default, *, min_value, max_value)` helper in `main.py` — reuse for Plans A/B
- [x] `ENV` class in `backend/app/env.py` for centralized config — Plans B/C should add vars here
- [x] ESLint configured in frontend (`npm run lint`)

### 7.2 Environment Variables Needed

| Variable | Purpose | Default | Scope | Plan |
|----------|---------|---------|-------|------|
| `DG_ENDPOINTING_MS` | Deepgram silence gate before speech_final | `500` (currently `1500`) | Backend | A |
| `DG_UTTER_END_MS` | Deepgram utterance end timeout | `600` (currently `1000`) | Backend | A |
| `COMMIT_WAIT_MS` | Wait after Deepgram final before committing | `100` (currently hardcoded `250`) | Backend | B |
| `CJK_PENDING_HOLD_MS` | Hold after speech_final for CJK trailing words | `300` (currently hardcoded `600`) | Backend | B |
| `OPENAI_PARTIAL_MODEL` | Model for partial/preview translations | `gpt-4o-mini` | Backend | C |

---

## 8. Next Steps

1. [ ] **Plan A** — Update `backend/.env` and `CLAUDE.md`; test in one live session
2. [ ] `/pdca design translation-latency` — Write design doc covering Plans B, C, D in detail
3. [ ] Implement Plan B after Plan A is validated
4. [ ] Implement Plan C after Plan B is validated
5. [ ] Implement Plan D (streaming) last — requires frontend changes

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-04-10 | Initial draft — 5 bottlenecks, 4 plans, A→D order | namjubae |
