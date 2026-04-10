# Plan: korean-subject-fix

## Executive Summary

| Perspective | Description |
|---|---|
| **Problem** | Korean pro-drop sentences (no explicit subject) are inconsistently translated with "we" instead of the correct subject (I/he/she/they), breaking sermon accuracy for listeners |
| **Solution** | Audit and fix the three-layer subject inference pipeline (prompt context, `_needs_recent_context` gating, and post-hoc guardrails) so prior sentences reliably establish subject for GPT |
| **Functional UX Effect** | Listeners hear grammatically correct, contextually accurate English — "Yesterday, I was very tired" instead of "Yesterday, we were very tired" when the pastor is describing personal experience |
| **Core Value** | Reliable subject inference restores trust in the live translation and removes the main grammar failure mode in personal-narrative sermon passages |

---

## Problem Statement

Korean is a pro-drop language — the subject is frequently omitted when it is clear from context. The app attempts to infer the correct subject via three layers:

1. **Prompt context block** (`_build_context_block`): tells GPT the established subject/pronoun
2. **Recent context gating** (`_needs_recent_context` → `_should_include_prompt_context`): decides whether to inject the last N sentence pairs into the prompt
3. **Post-hoc guardrails** (`_enforce_subject_guardrails`): regex-based pronoun substitution after GPT responds

The fix sending prior sentences to GPT was implemented but is inconsistent. From logs tested 2026-04-05:
- "어제는 너무 피곤했습니다" → "Yesterday, **we** were very tired." (wrong: should be "I")
- "그래서 집에 가자마자 바로 잤습니다" → "So, as soon as **we** got home, **we** went straight to sleep." (wrong)

The problem regresses — sometimes works, sometimes doesn't.

---

## Root Cause Hypotheses

1. **`_needs_recent_context` gating is too restrictive**: The function returns `False` (skips context) when text contains first-person or we-markers, but plain past-tense sentences like "어제는 너무 피곤했습니다" have neither marker → falls into the `len(compact) <= 24` branch (30 chars — misses the 24-char threshold). Context gets skipped.

2. **Subject context block uses wrong default**: When `ctx.subject` is not yet established at session start, it falls back to `ENV.CONTEXT_SUBJECT` (default "we"). This poisons early sentences before any subject signal is seen.

3. **`_infer_subject_from_context_history` only helps if prior English established a clear subject**: For the first sentence or after a topic shift, there is no prior English and inference returns the default "we".

4. **Post-hoc guardrail conflicts**: `_enforce_subject_guardrails` tries to regex-replace pronouns, but if GPT already wove "we" throughout the sentence naturally ("as soon as we got home, we went straight to sleep"), the regex may partially replace or miss occurrences.

---

## Scope

### In scope
- Audit `_needs_recent_context` threshold logic (24-char cutoff and Korean marker detection)
- Fix session-start default subject (the "we" cold-start problem)
- Improve `_build_context_block` to be clearer to GPT when subject is uncertain vs. established
- Add explicit GPT instruction: "When no subject is available from context, prefer 'I' for personal narratives unless Korean contains 우리/저희"
- Verify `recent_pairs` is actually being populated and passed through the call chain for streaming partial translations

### Out of scope
- Changing the Korean→English translation model
- Frontend changes
- Non-Korean source languages

---

## Key Files

| File | Role |
|---|---|
| `backend/app/utils/translate.py` | All subject inference logic, prompt building, guardrails |
| `backend/app/env.py` | `CONTEXT_SUBJECT`, `CONTEXT_PRONOUN` defaults |

---

## Success Criteria

1. Personal-narrative passages ("어제는", "제가", first-person implied) translate with "I" not "we" consistently
2. Congregational passages ("우리가", "저희는") still correctly use "we"
3. Third-person passages (pastor describing someone else) still use correct third-person subject
4. No regression in existing test cases in `backend/app/data/translation_examples.jsonl`

---

## Implementation Approach

1. **Diagnose** — add debug logging to `_needs_recent_context` and `_should_include_prompt_context` to confirm whether context is being passed for subject-drop sentences
2. **Fix the gating** — review the `<= 24` char threshold and the early-return conditions; sentences without subject markers and without first-person markers should still get recent context
3. **Fix cold-start default** — change default subject in `_build_context_block` to signal "unknown" rather than "we" when no subject is established
4. **Strengthen the GPT prompt instruction** — make the "when subject is omitted" rule more explicit with examples
5. **Verify the call chain** — confirm `recent_pairs` is populated before `_build_recent_context_block` is called for streaming (partial) translations

---

## Risk

| Risk | Mitigation |
|---|---|
| Changing default breaks congregational "we" passages | Test with "우리", "저희" examples in translation_examples.jsonl before deploying |
| Overly aggressive "I" default causes wrong subject when third-person was established | Keep `_infer_subject_from_context_history` running first; only default to "I" when no context at all |
| Regex guardrail produces double-replacement | Unit test `_enforce_subject_guardrails` with complex multi-clause English outputs |
