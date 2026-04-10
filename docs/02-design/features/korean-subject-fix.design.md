# Design: korean-subject-fix

## Root Cause (confirmed by code reading)

The bug is **not** in the context-gating logic. It is a **cold-start default poisoning loop**:

### Step-by-step failure trace

1. Session starts. `ctx.recent_pairs = []`, `ctx.last_english = None`.
2. `_infer_subject_from_context_history(ctx)` → returns `ENV.CONTEXT_SUBJECT="the congregation"`, `ENV.CONTEXT_PRONOUN="we"` (no prior evidence → falls back to default).
3. `ctx.subject = "the congregation"`, `ctx.pronoun = "we"`.
4. GPT user message: **`IMPORTANT: The subject of this clause is "the congregation" (we).`**
5. GPT obediently uses "we": "Yesterday, we were very tired."
6. After translation, `_infer_subject_from_english("Yesterday, we were very tired", ...)` matches `^we` → returns `"we"` → `ctx.pronoun = "we"` again.
7. Second sentence inherits "we". Loop persists.

**Key code locations:**

| Location | Line | Issue |
|---|---|---|
| `translate_text` | 1339–1342 | `_infer_subject_from_context_history` returns "we" default with zero evidence |
| `translate_text` | 1384–1400 | Injects `"the congregation" (we)` into user prompt unconditionally |
| `translate_text` | 1459–1463 | Updates ctx from English output — locks in the injected "we" |
| `env.py` | 26–27 | `CONTEXT_SUBJECT="the congregation"`, `CONTEXT_PRONOUN="we"` |

---

## Design: Targeted Fixes

### Fix 1 — `_has_established_context()` helper  
**File**: `backend/app/utils/translate.py`

```python
def _has_established_context(ctx: Optional[TranslationContext]) -> bool:
    """Return True if ctx has at least one translated pair as evidence for subject."""
    if not ctx:
        return False
    return bool(ctx.recent_pairs or ctx.last_english)
```

### Fix 2 — Cold-start: skip subject injection when no evidence exists  
**File**: `backend/app/utils/translate.py`, around line 1384–1400

Current (broken):
```python
if _should_include_prompt_context(text, update_ctx=update_ctx):
    user_content = (
        recent_context_block +
        f"Previous English sentence: {prev}\n"
        f"IMPORTANT: The subject of this clause is \"{subject_hint}\" ({pronoun_hint}). ..."
        ...
    )
else:
    user_content = (
        f"IMPORTANT: The subject of this clause is \"{subject_hint}\" ({pronoun_hint}). ..."
        ...
    )
```

Proposed (fixed):
```python
if _should_include_prompt_context(text, update_ctx=update_ctx):
    if _has_established_context(ctx_for_prompt):
        # Evidence exists — inject established subject
        user_content = (
            recent_context_block +
            f"Previous English sentence: {prev}\n"
            f"IMPORTANT: The subject of this clause is \"{subject_hint}\" ({pronoun_hint}). "
            f"Do NOT introduce a new subject or use \"one\", \"people\", \"a person\", or other generic terms "
            f"unless the Korean explicitly names a new entity.\n\n"
            f"Current text:\n{masked_text}"
        )
    else:
        # Cold-start: no prior context — let GPT infer freely
        user_content = (
            recent_context_block +
            "No prior context. Translate naturally. "
            "Do NOT use \"we\" unless the Korean explicitly contains 우리 or 저희.\n\n"
            f"Current text:\n{masked_text}"
        )
else:
    if _has_established_context(ctx_for_prompt):
        user_content = (
            f"IMPORTANT: The subject of this clause is \"{subject_hint}\" ({pronoun_hint}). "
            f"Do NOT introduce a new subject or use \"one\", \"people\", \"a person\", or other generic terms "
            f"unless the Korean explicitly names a new entity.\n\n"
            f"Current text:\n{masked_text}"
        )
    else:
        # Cold-start, no context injection
        user_content = masked_text
```

### Fix 3 — Don't update ctx.pronoun from GPT output when we injected the subject  
**File**: `backend/app/utils/translate.py`, around line 1456–1464

The current code updates ctx from the English output unconditionally. If GPT said "we" because we told it to, we shouldn't lock that "we" into context as if it came from the Korean.

Add a guard: only update subject/pronoun from output if `_has_established_context(ctx)` was True before the call, OR if the Korean text itself contained an explicit subject marker.

```python
if ctx and update_ctx:
    # Only propagate subject from output if we had real evidence going in,
    # or if the Korean itself had explicit subject markers.
    explicit_ko_subject = (
        _contains_first_person_markers(text)
        or _contains_we_markers(text)
        or bool(_detect_third_person_pronoun(text))
    )
    if had_established_context or explicit_ko_subject:
        ctx.subject, ctx.pronoun = _infer_subject_from_english(
            out,
            ctx.subject or ENV.CONTEXT_SUBJECT,
            ctx.pronoun or ENV.CONTEXT_PRONOUN,
        )
    ctx.remember(text, out)
```

> **Note**: `had_established_context` must be captured before the translation call:
> ```python
> had_established_context = _has_established_context(ctx_for_prompt)
> ```

---

## Implementation Plan

### Step 1: Add `_has_established_context` helper
- Location: `translate.py`, after `_infer_subject_from_context_history` (around line 696)
- Simple boolean — no side effects

### Step 2: Capture `had_established_context` before translation call
- Location: `translate.py`, around line 1353 (before `_build_system_prompt`)
- `had_established_context = _has_established_context(ctx_for_prompt)`

### Step 3: Refactor the `user_content` construction block
- Location: `translate.py`, lines 1367–1400
- Four cases: (has context × includes recent block)

### Step 4: Guard the ctx update block
- Location: `translate.py`, lines 1456–1464
- Add `had_established_context or explicit_ko_subject` guard

---

## Files Changed

| File | Change |
|---|---|
| `backend/app/utils/translate.py` | Add helper, refactor user_content block, guard ctx update |

No other files need to change. `env.py` defaults stay as-is (they're used as fallback when context IS established).

---

## Test Cases

### Must pass after fix

| Input Korean | Expected | Should NOT be |
|---|---|---|
| 어제는 너무 피곤했습니다 (first sentence, no prior context) | "Yesterday, I was very tired." or "Yesterday, [he/she] was very tired." | "Yesterday, we were very tired." |
| 그래서 집에 가자마자 바로 잤습니다 (follows first sentence) | "So, as soon as [I/he/she] got home, [I/he/she] went straight to sleep." | "So, as soon as we got home, we went straight to sleep." |
| 우리가 함께 예배드립시다 (explicit 우리) | "Let us worship together." | subject change |
| 저희 교회는 (explicit 저희) | "Our church..." | "I/he/she..." |

### Must NOT regress

| Scenario | Expected behavior |
|---|---|
| Congregational "우리/저희" passages | Still translated with "we" |
| After pastor says "나는..." — subsequent subject-drop sentence | Stays "I" |
| Third-person established ("pastor" in prior sentence) | Stays third-person |

---

## Risk Assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| Cold-start GPT defaults to "I" incorrectly for true congregational passages | Low — 우리/저희 are almost always explicit in Korean | The cold-start instruction explicitly says "do not use we UNLESS 우리/저희" — so congregational passages with 우리 are protected |
| Removing ctx update breaks subject continuity for long third-person passages | Medium | The `had_established_context` guard preserves normal propagation after first sentence |
| Test cases in translation_examples.jsonl break | Low | These use explicit subjects; not affected by cold-start path |
