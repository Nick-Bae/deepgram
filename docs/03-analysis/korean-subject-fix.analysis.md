# Gap Analysis: korean-subject-fix

**Match Rate: 100%**

Date: 2026-04-05

---

## Design vs Implementation Comparison

### Fix 1 — `_has_established_context()` helper

| Design | Implementation | Status |
|---|---|---|
| Add helper after `_infer_subject_from_context_history` (~line 696) | Added at line 699 | MATCH |
| Returns `False` when ctx is None | `if not ctx: return False` | MATCH |
| Returns `True` when `ctx.recent_pairs` or `ctx.last_english` is set | `return bool(ctx.recent_pairs or ctx.last_english)` | MATCH |

### Fix 2 — Capture `had_established_context` before build

| Design | Implementation | Status |
|---|---|---|
| Capture before `_build_system_prompt` call | Line 1364: `had_established_context = _has_established_context(ctx_for_prompt)` | MATCH |

### Fix 2 (cont.) — Cold-start user_content branch

| Design | Implementation | Status |
|---|---|---|
| When `had_established_context=True` + context block: inject subject hint as before | Lines 1398–1406: original subject injection preserved | MATCH |
| When `had_established_context=False` + context block: neutral cold-start message | Lines 1407–1415: `"No prior context established. Translate naturally. Do NOT use 'we' unless..."` | MATCH |
| When `had_established_context=True` + no context block: inject subject hint | Lines 1417–1423: original path preserved | MATCH |
| When `had_established_context=False` + no context block: pass raw Korean | Lines 1424–1426: `user_content = masked_text` | MATCH |

### Fix 3 — Guard ctx subject/pronoun update

| Design | Implementation | Status |
|---|---|---|
| Detect `explicit_ko_subject` from Korean text | Lines 1488–1492: checks `_contains_first_person_markers`, `_contains_we_markers`, `_detect_third_person_pronoun` | MATCH |
| Only update `ctx.subject/pronoun` when `had_established_context OR explicit_ko_subject` | Line 1493: `if had_established_context or explicit_ko_subject:` | MATCH |
| `ctx.remember()` still called unconditionally | Line 1499: `ctx.remember(text, out)` outside the if-block | MATCH |

---

## Behavioral Verification (logic trace)

### Scenario A: First sentence "어제는 너무 피곤했습니다" (cold-start)

- `ctx.recent_pairs = []`, `ctx.last_english = None`
- `had_established_context = False`
- `_needs_recent_context("어제는너무피곤했습니다")` → len=11 ≤ 24 → `True`
- `_should_include_prompt_context` → `True`
- Branch taken: cold-start + context block → `"No prior context established. Translate naturally. Do NOT use 'we' unless 우리/저희."`
- GPT has no "we" pressure → infers subject from Korean → likely "I was very tired"
- After: `had_established_context=False`, `explicit_ko_subject=False` → ctx.subject/pronoun NOT updated from output
- `ctx.remember(text, out)` → `ctx.recent_pairs` now has 1 entry

### Scenario B: Second sentence "그래서 집에 가자마자 바로 잤습니다" (after first)

- `ctx.recent_pairs` has 1 entry → `had_established_context = True`
- Normal subject injection path resumes
- Subject/pronoun updates from English output now propagated correctly

### Scenario C: "우리가 함께 예배드립시다" (explicit 우리)

- `_contains_we_markers` → `True`
- `ctx_for_prompt = None` is NOT set (no first-person exclusion for 우리)
- Normal path → subject injected if context exists
- After: `explicit_ko_subject = True` → ctx updated from English output → "we" correctly propagated

---

## No Regressions Identified

- Existing subject injection logic for established-context sentences: unchanged
- `_enforce_subject_guardrails` and `_enforce_we_guardrails`: unchanged
- `_build_context_block` in system prompt: unchanged (still provides subject continuity instruction)
- `update_ctx=False` path (sermon prep): unaffected
- `explicit_first_person` path (`ctx_for_prompt = None`): unaffected

---

## Summary

All 3 design fixes are fully implemented with exact behavioral match. The cold-start poisoning loop is broken: the first subject-drop sentence no longer receives an injected "we", and the bad default cannot propagate into subsequent sentences.
