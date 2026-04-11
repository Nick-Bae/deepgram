# Translation Latency Improvement Design Document

> **Summary**: Code-level specifications for four sequential latency improvements (A→D) covering env tuning, constant externalization, partial-model switching, and OpenAI streaming.
>
> **Project**: Real-Time Translation Platform
> **Author**: namjubae
> **Date**: 2026-04-10
> **Status**: Draft
> **Planning Doc**: [translation-latency.plan.md](../01-plan/features/translation-latency.plan.md)

---

## 1. Overview

### 1.1 Design Goals

- Each plan is a self-contained, independently reversible change
- No behavioral change when new env vars are absent (backward compatible)
- Streaming (Plan D) must not break the existing rate-limit budget accounting
- All code changes follow existing project patterns (`_env_int`, `ENV` class, async/await)

### 1.2 Design Principles

- **One change at a time** — deploy and validate Plan A before touching Plan B
- **Env-first config** — every tunable number becomes an env var, never re-hardcoded
- **Fail-open** — streaming errors fall back to the existing non-streaming path, not silence
- **Pre-script path untouched** — the `script_match` fast path (≤100 ms) must remain unchanged

---

## 2. Architecture

### 2.1 Current Pipeline (Baseline)

```
[Host mic]
   │ PCM audio (WebSocket)
   ▼
backend: /ws/stt/deepgram
   │ forward bytes
   ▼
Deepgram WebSocket (nova-3, Korean)
   │ partial / is_final transcripts
   ▼
from_deepgram_to_server()
   ├── [partial] → emit_preview() → send_translation(partial=True)
   │                                 └─ compact_prompt, max_tokens=120
   │                                 └─ GPT-4o → broadcast
   │
   └── [final]  → commit_now()
                   ├── CJK hold (CJK_PENDING_HOLD_MS)
                   ├── commit timer (COMMIT_WAIT_MS)
                   └─ send_translation(partial=False)
                       ├── script_match → instant (pre path)
                       └── GPT-4o → full response await → broadcast
```

### 2.2 Target Pipeline (After A+B+C+D)

```
[Host mic]
   │
   ▼
Deepgram  ← DG_ENDPOINTING_MS=500 (was 1500)
   │ final fires ~1000 ms earlier
   ▼
from_deepgram_to_server()
   ├── CJK_PENDING_HOLD_MS=300 (was 600)  ← Plan B
   ├── COMMIT_WAIT_MS=100 (was 250)       ← Plan B
   │
   ├── [partial] → GPT-4o-mini (was 4o)  ← Plan C
   │
   └── [final]
       ├── script_match → instant (unchanged)
       └── GPT-4o streaming → token chunks → broadcast ← Plan D
                              ↓
                    listeners receive words as they generate
```

### 2.3 Key Files

| File | Plans Affected |
|------|---------------|
| `backend/.env` | A |
| `backend/app/deepgram_session.py` | A (default value change) |
| `backend/app/env.py` | B, C |
| `backend/app/main.py` | B, C, D |
| `backend/app/utils/translate.py` | D |
| `frontend/components/TranslationBox.tsx` | D |

---

## 3. Plan A — Deepgram Endpointing Tuning

### 3.1 What Changes

`backend/app/deepgram_session.py` already reads `DG_ENDPOINTING_MS` from env (line ~34). No code change needed — only the default value and `.env` need updating.

**Current defaults:**
```python
DG_ENDPOINTING_MS = _int_env("DG_ENDPOINTING_MS", 1500, min_value=200, max_value=6000)
DG_UTTER_END_MS   = _int_env("DG_UTTER_END_MS",   1000, min_value=300, max_value=6000)
```

**New defaults (code change in `deepgram_session.py`):**
```python
DG_ENDPOINTING_MS = _int_env("DG_ENDPOINTING_MS", 500,  min_value=200, max_value=6000)
DG_UTTER_END_MS   = _int_env("DG_UTTER_END_MS",   600,  min_value=300, max_value=6000)
```

### 3.2 `.env` Addition

```bash
# backend/.env  (and document in CLAUDE.md)
DG_ENDPOINTING_MS=500    # ms of silence before Deepgram fires speech_final (default was 1500)
DG_UTTER_END_MS=600      # ms after last word before utterance end (default was 1000)
```

### 3.3 Tuning Guide

| Value | Behavior |
|-------|----------|
| 300 ms | Very aggressive — may split mid-clause on Korean breath pauses |
| 500 ms | Recommended start — captures most sentence endings |
| 700 ms | Conservative — fewer false commits, more latency |
| 1500 ms | Current default (very slow) |

If false mid-sentence commits occur, increase `DG_ENDPOINTING_MS` by 100 ms at a time.

### 3.4 Expected Latency Saving

**~700–1000 ms** off the pipeline before GPT even starts.

---

## 4. Plan B — Externalize Hardcoded Commit Delays

### 4.1 What Changes

Inside `from_deepgram_to_server()` in `main.py`, two constants are hardcoded at the top of the coroutine:

```python
# main.py, inside async def from_deepgram_to_server(): (~line 1496)
COMMIT_WAIT_MS = 250        # ← Plan B: make this an env var
CJK_PENDING_HOLD_MS = 600   # ← Plan B: make this an env var
```

These need to move to module-level env-var reads so they can be tuned without code changes.

### 4.2 `backend/app/env.py` Addition

```python
# Add to class ENV:
COMMIT_WAIT_MS: int = int(os.getenv("COMMIT_WAIT_MS", "100"))
CJK_PENDING_HOLD_MS: int = int(os.getenv("CJK_PENDING_HOLD_MS", "300"))
```

### 4.3 `backend/app/main.py` Change

**Before** (inside `from_deepgram_to_server()`):
```python
COMMIT_WAIT_MS = 250
CJK_PENDING_HOLD_MS = 600
```

**After** (replace with references to ENV):
```python
COMMIT_WAIT_MS = ENV.COMMIT_WAIT_MS
CJK_PENDING_HOLD_MS = ENV.CJK_PENDING_HOLD_MS
```

That's the entire code change. The rest of the function already uses these local names.

### 4.4 `.env` Addition

```bash
COMMIT_WAIT_MS=100       # ms to wait after Deepgram final before committing (was hardcoded 250)
CJK_PENDING_HOLD_MS=300  # ms hold for Korean trailing words after speech_final (was hardcoded 600)
```

### 4.5 Expected Latency Saving

**~350 ms** (150 ms from COMMIT_WAIT + 300 ms from CJK_PENDING_HOLD reduction).

---

## 5. Plan C — gpt-4o-mini for Partial Previews

### 5.1 What Changes

Partial preview translations (`compact_prompt=True, max_tokens=120`) currently use the same
model as final commits (GPT-4o). Adding `OPENAI_PARTIAL_MODEL` env var routes previews to
a faster, cheaper model.

### 5.2 `backend/app/env.py` Addition

```python
# Add to class ENV:
PARTIAL_TRANSLATION_MODEL: str = _env_str(
    "OPENAI_PARTIAL_MODEL", "PARTIAL_MODEL",
    default="gpt-4o-mini"
)
```

### 5.3 `backend/app/main.py` Change

In `send_translation()`, the partial branch (`else:` at ~line 1682):

**Before:**
```python
translated, limit_meta = await _translate_text_guarded(
    clean_src,
    src_lang_full,
    tgt_lang_full,
    org_id=org_id,
    host_uid=host_uid_claim,
    ctx=translation_ctx,
    update_ctx=update_ctx,
    custom_prompt=custom_prompt,
    service_prompt="",
    compact_prompt=True,
    max_tokens=120,
)
```

**After** (add `model_override`):
```python
translated, limit_meta = await _translate_text_guarded(
    clean_src,
    src_lang_full,
    tgt_lang_full,
    org_id=org_id,
    host_uid=host_uid_claim,
    ctx=translation_ctx,
    update_ctx=update_ctx,
    custom_prompt=custom_prompt,
    service_prompt="",
    compact_prompt=True,
    max_tokens=120,
    model_override=ENV.PARTIAL_TRANSLATION_MODEL,  # ← add this line
)
```

`_translate_text_guarded` already accepts `model_override` and passes it to `translate_text()`.
`translate_text()` already passes it to `ENV.resolve_translation_model()`. No other changes needed.

### 5.4 `.env` Addition

```bash
OPENAI_PARTIAL_MODEL=gpt-4o-mini   # model for partial/preview translations (default: gpt-4o-mini)
```

### 5.5 Expected Benefit

- ~100–200 ms faster TTFT for preview translations
- ~60–80% cost reduction for preview calls (gpt-4o-mini vs gpt-4o pricing)
- Preview quality is unaffected for UX — partials are ephemeral, replaced by the final commit

---

## 6. Plan D — OpenAI Streaming for Final Translations

> **Prerequisite**: Plans A, B, and C should be deployed and validated first.
> Plan D has the most code surface area and requires frontend changes.

### 6.1 New WebSocket Message Types

**Streaming start** (broadcast when streaming begins):
```json
{
  "type": "translation_stream_start",
  "seq": 42,
  "orgId": "org-abc",
  "roomId": "room-xyz",
  "src": { "text": "하나님은 사랑이십니다", "lang": "ko" },
  "tgt": { "lang": "en" }
}
```

**Streaming token** (broadcast for each token chunk):
```json
{
  "type": "translation_stream_token",
  "seq": 42,
  "token": "God ",
  "orgId": "org-abc",
  "roomId": "room-xyz"
}
```

**Streaming end** (replaces the existing `translation` message when complete):
```json
{
  "type": "translation_stream_end",
  "seq": 42,
  "text": "God is love.",
  "orgId": "org-abc",
  "roomId": "room-xyz",
  "src": { "text": "하나님은 사랑이십니다", "lang": "ko" },
  "tgt": { "lang": "en" },
  "meta": { "mode": "live", "is_final": true, ... }
}
```

The existing `translation` message type continues to be broadcast at the end (backward compat).

### 6.2 `backend/app/utils/translate.py` — New Streaming Function

Add a new `translate_text_streaming()` async generator that wraps the OpenAI streaming API:

```python
from typing import AsyncIterator

async def translate_text_streaming(
    text: str,
    source: str,
    target: str,
    ctx: Optional[TranslationContext] = None,
    *,
    update_ctx: bool = True,
    custom_prompt: Optional[str] = None,
    service_prompt: Optional[str] = None,
    script_examples: Optional[list] = None,
    script_glossary: Optional[list] = None,
    org_id: Optional[str] = None,
    model_override: Optional[str] = None,
    usage_out: Optional[Dict[str, Any]] = None,
) -> AsyncIterator[str]:
    """
    Async generator yielding translation token chunks as they arrive.
    Final assembled text is equivalent to translate_text() output.
    Caller must consume the entire iterator to ensure ctx.remember() is called.

    Usage:
        assembled = ""
        async for chunk in translate_text_streaming(text, "ko", "en", ctx=ctx):
            assembled += chunk
            await ws.send_json({"type": "translation_stream_token", "token": chunk})
    """
    text = (text or "").strip()
    text = _preprocess_source_text(text, source)
    masked_text, hard_map = _mask_hard_glossary(text, source)
    if source.lower().startswith("ko"):
        masked_text = apply_ko_spacing(masked_text)
    if not masked_text:
        return

    # Build prompt (same logic as translate_text)
    explicit_first_person = _contains_first_person_markers(text)
    implicit_first_person = _contains_implicit_first_person_kinship(text)
    if ctx:
        subj_hint, pronoun_hint = _infer_subject_from_context_history(ctx)
        ctx.subject = subj_hint
        ctx.pronoun = pronoun_hint
    if explicit_first_person:
        ctx_for_prompt = None
    else:
        ctx_for_prompt = _context_for_prompt(ctx, text, implicit_first_person=implicit_first_person)
    had_established_context = _has_established_context(ctx_for_prompt)
    ctx_for_system = ctx_for_prompt if had_established_context else None

    system = _build_system_prompt(
        source, target, ctx_for_system,
        current_source_text=text,
        custom_prompt=custom_prompt,
        service_prompt=service_prompt,
        compact_prompt=False,
        script_examples=script_examples,
        script_glossary=script_glossary,
        org_id=org_id,
    )
    user_content = _build_user_content(masked_text, ctx_for_prompt, text, had_established_context, update_ctx)

    client = _get_client()
    model_name = ENV.resolve_translation_model(model_override)

    assembled = ""
    prompt_tokens = 0
    completion_tokens = 0

    try:
        stream = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            **_openai_chat_options(model_name),
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
            if delta:
                assembled += delta
                yield delta
            # capture usage from the final chunk
            if chunk.usage:
                prompt_tokens = chunk.usage.prompt_tokens or 0
                completion_tokens = chunk.usage.completion_tokens or 0
    except Exception as exc:
        # Fail-open: yield the original text so the caller can broadcast something
        if assembled:
            pass  # already streamed partial — caller handles completion
        else:
            yield text
        if usage_out is not None:
            usage_out.update({"failOpen": True, "errorMessage": str(exc)})
        return

    # Post-processing (same as translate_text)
    assembled = assembled.strip().strip('"\u201c\u201d')
    assembled = _unmask_hard_glossary(assembled, hard_map)
    if ctx and source.lower().startswith("ko"):
        assembled = _enforce_subject_guardrails(assembled, text, ctx)
    if source.lower().startswith("ko"):
        assembled = _enforce_we_guardrails(assembled, text, ctx)
    if target.lower().startswith("en"):
        assembled = _normalize_english_pronoun_case(assembled)
    if ctx and update_ctx:
        explicit_ko_subject = (
            _contains_first_person_markers(text)
            or _contains_we_markers(text)
            or bool(_detect_third_person_pronoun(text))
        )
        if had_established_context or explicit_ko_subject:
            ctx.subject, ctx.pronoun = _infer_subject_from_english(
                assembled,
                ctx.subject or ENV.CONTEXT_SUBJECT,
                ctx.pronoun or ENV.CONTEXT_PRONOUN,
            )
        ctx.remember(text, assembled)

    if usage_out is not None:
        total = prompt_tokens + completion_tokens
        usage_out.update({
            "promptTokens": prompt_tokens,
            "completionTokens": completion_tokens,
            "totalTokens": total,
            "model": model_name,
        })
```

> **Note on `_build_user_content`**: Extract the user-content building block from `translate_text()` into a private helper `_build_user_content(masked_text, ctx_for_prompt, text, had_established_context, update_ctx) -> str` to avoid duplication between the two functions.

### 6.3 `backend/app/main.py` — Streaming Path in `send_translation()`

In `send_translation()`, replace the non-partial GPT-4o call with a streaming path:

```python
# Inside send_translation(), in the `if not partial:` branch,
# replace the existing _translate_text_guarded call with:

reservations, blocked = _reserve_translation_budget(
    org_id=org_id, host_uid=host_uid_claim, source_text=clean_src
)
if blocked:
    meta_payload.update(_rate_limit_meta(blocked))
    translated = clean_src
else:
    # Announce stream start to listeners
    stream_start_msg = {
        "type": "translation_stream_start",
        "seq": seq,
        "orgId": org_id,
        "roomId": room_id,
        "src": {"text": clean_src, "lang": src_lang_full},
        "tgt": {"lang": tgt_lang_full},
    }
    try:
        if org_id and room_id:
            await manager.broadcast_room(org_id, room_id, stream_start_msg)
    except Exception:
        pass

    tx_usage: dict[str, Any] = {}
    assembled = ""
    try:
        custom_prompt, service_prompt = _cached_prompt_overrides(org_id)
        async for token in translate_text_streaming(
            clean_src,
            src_lang_full,
            tgt_lang_full,
            ctx=translation_ctx,
            update_ctx=True,
            custom_prompt=custom_prompt,
            service_prompt=service_prompt,
            script_examples=_script_examples_dg,
            script_glossary=_cached_script_glossary_dg,
            org_id=org_id,
            usage_out=tx_usage,
        ):
            assembled += token
            token_msg = {
                "type": "translation_stream_token",
                "seq": seq,
                "token": token,
                "orgId": org_id,
                "roomId": room_id,
            }
            try:
                if org_id and room_id:
                    await manager.broadcast_room(org_id, room_id, token_msg)
            except Exception:
                pass
    except Exception as exc:
        assembled = assembled or clean_src
        meta_payload.update(_fail_open_meta(exc))
    finally:
        _settle_translation_budget(
            reservations, actual_tokens=int(tx_usage.get("totalTokens") or 0)
        )

    translated = assembled or clean_src
```

The rest of `send_translation()` (building `live_msg_new`, `live_msg_legacy`, broadcasting the final message) continues unchanged — the `stream_end` is effectively the existing `translation` broadcast.

### 6.4 `frontend/components/TranslationBox.tsx` — Handle Streaming Messages

The frontend needs to handle three new message types. Add to the WebSocket message handler:

```typescript
// In the useTranslationSocket message handler, add cases:

case 'translation_stream_start': {
  // Clear any existing stream for this seq and prepare accumulator
  const { seq, src, tgt } = msg
  setStreamingState({ seq, accumulated: '', srcLang: src?.lang, tgtLang: tgt?.lang })
  break
}

case 'translation_stream_token': {
  const { seq: tokenSeq, token } = msg
  setStreamingState(prev => {
    if (!prev || prev.seq !== tokenSeq) return prev
    const accumulated = prev.accumulated + token
    // Update the live display with the accumulated text so far
    setCurrentTranslation(accumulated)
    return { ...prev, accumulated }
  })
  break
}

case 'translation_stream_end':
  // Handled by the existing 'translation' message — clear streaming state
  setStreamingState(null)
  break
```

State shape:
```typescript
type StreamingState = {
  seq: number
  accumulated: string
  srcLang?: string
  tgtLang?: string
} | null
```

**Deduplication rule**: When a `translation` (final) message arrives with the same `seq` as an active stream, replace the streaming text with the final text and clear streaming state. This prevents double-display if the stream end and final message arrive close together.

---

## 7. Error Handling

| Scenario | Behavior |
|----------|----------|
| Plan A: Deepgram endpointing causes false commit | Short fragments translated; KoChunker + `is_short_korean_clause` guard already suppresses very short clips |
| Plan B: `COMMIT_WAIT_MS=0` (misconfigured) | `_env_int` with `min_value=50` prevents it from going below 50 ms |
| Plan C: `gpt-4o-mini` rate limited or unavailable | `_translate_text_guarded` fail-open path echoes source text; partial preview shows Korean temporarily |
| Plan D: OpenAI stream disconnects mid-response | `assembled` contains partial text; `_fail_open_meta` is set; final broadcast sends partial translation + fail_open flag |
| Plan D: WebSocket to listener disconnects mid-stream | `broadcast_room` already swallows send errors; next final message replaces any partial state |
| Plan D: `stream_options include_usage` not supported on model | Wrap usage extraction in `getattr(chunk, 'usage', None)` guard; budget settled with 0 actual tokens |

---

## 8. Security Considerations

- Streaming tokens are subject to the same rate-limit budget reservation as non-streaming — no bypass
- Token chunks do not expose new information not already present in the final message
- `translation_stream_token` messages carry `seq` for deduplication; no auth state change involved
- No new endpoints, no new auth paths introduced by any plan

---

## 9. Test Plan

### 9.1 Per-Plan Acceptance Tests

**Plan A**
- [ ] Start a dev session; speak a Korean sentence with a natural 400 ms breath pause — confirm Deepgram does NOT fire mid-sentence with `DG_ENDPOINTING_MS=500`
- [ ] Speak "..." — confirm Deepgram fires after ~500 ms silence
- [ ] Revert env var — confirm old behavior restored

**Plan B**
- [ ] Set `COMMIT_WAIT_MS=100 CJK_PENDING_HOLD_MS=300` — confirm `print("[COMMIT]")` logs appear ~350 ms sooner than baseline
- [ ] Set vars to empty — confirm behavior matches pre-change defaults

**Plan C**
- [ ] Check OpenAI dashboard: partial calls use `gpt-4o-mini`, final calls use `gpt-4o`
- [ ] Run 5-minute session; confirm partial translation quality acceptable (words appearing, not garbled)

**Plan D**
- [ ] Open listener tab; speak a sentence; confirm words appear one by one as GPT generates
- [ ] Simulate stream disconnect (kill backend mid-stream); confirm listener shows partial text + receives final message
- [ ] Confirm `translation` legacy message still arrives (backward compat for older listener clients)
- [ ] Confirm rate limit tokens are correctly settled after streaming

### 9.2 Regression Tests

- [ ] Pre-script match path: known sermon line → instant display, no streaming messages sent
- [ ] Scripture detection path: verse reference → instant display, no streaming messages sent
- [ ] `npm run lint` passes after Plan D frontend changes
- [ ] Cloud Run deployment: no startup errors after each plan

---

## 10. Implementation Order Summary

```
Plan A  ─── env var change only ────────────────────── ~5 min
  backend/.env: DG_ENDPOINTING_MS=500, DG_UTTER_END_MS=600
  deepgram_session.py: change default values

Plan B  ─── ~5 lines, backend only ─────────────────── ~15 min
  env.py: add COMMIT_WAIT_MS, CJK_PENDING_HOLD_MS
  main.py: replace 2 hardcoded constants with ENV references

Plan C  ─── ~2 lines, backend only ─────────────────── ~10 min
  env.py: add PARTIAL_TRANSLATION_MODEL
  main.py: add model_override= to partial _translate_text_guarded call

Plan D  ─── ~150 lines, backend + frontend ──────────── ~2–3 hours
  translate.py: add translate_text_streaming() + _build_user_content()
  main.py:      replace final GPT call with streaming loop
  TranslationBox.tsx: handle stream_start / stream_token / stream_end
```

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-04-10 | Initial design — Plans A through D with exact code specs | namjubae |
