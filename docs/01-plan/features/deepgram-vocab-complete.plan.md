# Plan: deepgram-vocab-complete

## Executive Summary

| Perspective | Detail |
|---|---|
| **Problem** | Deepgram has two vocabulary features — `keyterm` (boost recognition) and `replace` (fix output) — but we only use `keyterm`. Systematic misrecognitions like "예수 님" (space in honorific) survive to translation unchanged. Also, the curated `get_keyword_glossary()` terms (recurring verified sermon vocabulary) are not prioritized in Tier 2 — all sermon vocab is treated equally. |
| **Solution** | Two additions: (1) promote `get_keyword_glossary()` Korean terms to the front of Tier 2 keyterms so curated terms fill slots first; (2) add Deepgram `replace` support with a default Korean correction list and per-org custom corrections stored in Firestore. |
| **Function / UX Effect** | Hardcoded STT glitches (space in Korean honorifics, common variant spellings) are silently corrected before text reaches translation. Church admins can define their own corrections for pastor-specific recurring errors. |
| **Core Value** | Two-layer accuracy — recognize better (keyterm) + fix what slips through (replace) — eliminates the category of systematic, repeatable STT errors without retraining the model. |

---

## 1. Research Findings

### 1.1 Deepgram Has No "Glossary" API

After researching Deepgram's official documentation, there is **no standalone glossary feature**. Vocabulary customization for nova-3 consists of exactly two features:

| Feature | Param | Stage | Nova-3 | Limit |
|---|---|---|---|---|
| **Keyterm Prompting** | `?keyterm=term` | During recognition | ✅ | 100 terms (monolingual) |
| **Find and Replace** | `?replace=wrong:right` | After recognition | ✅ | ~200 pairs suggested |
| Keywords (legacy) | `?keywords=term:boost` | During recognition | ❌ nova-2 only | 100 terms |

### 1.2 How the Two Stages Work Together

```
Audio stream
    │
    ▼
┌─────────────────────────────────────┐
│  Deepgram nova-3 Recognition        │
│  keyterm=할렐루야                    │  ← boosts probability of hearing this
│  keyterm=사사기                      │
└─────────────────────────────────────┘
    │
    ▼
Raw transcript: "할렐루야 주를 찬양해"
    │
    ▼
┌─────────────────────────────────────┐
│  Find and Replace (post-processing) │
│  replace=할렐루아:할렐루야            │  ← corrects if keyterm didn't catch it
│  replace=예수 님:예수님              │
└─────────────────────────────────────┘
    │
    ▼
Final transcript → our translation pipeline
```

They operate at **different stages** and can be combined in the same WebSocket connection — `keyterm` and `replace` params in the same query string.

### 1.3 The Existing `script_glossary` in Our System

`script_store.get_keyword_glossary()` returns `List[Tuple[str, str]]` — curated Korean→English pairs:
```python
[("할렐루야", "Hallelujah"), ("성령님", "Spirit"), ("십자가", "cross"), ...]
```

These terms are high-quality Deepgram signals: they appear in ≥2 source texts of today's sermon, are 2–6 chars (proper names, theological terms), and have confirmed translations. Currently they go **only to OpenAI** as translation hints.

### 1.4 Two Gaps in the Current `deepgram-keyterms` Implementation

**Gap A — Glossary terms not prioritized in Tier 2**

Tier 2 currently uses `script_store.get_vocab_set()` — all Korean tokens ≥3 chars, returned as an unordered set. Glossary terms (the highest-quality subset) are mixed in with hundreds of ordinary words and may not fit in the 100-term budget.

**Gap B — No `replace` layer**

Keyterms boost probability but cannot guarantee recognition. When Deepgram has a systematic pattern (e.g., always adds a space in Korean honorifics: "예수 님" instead of "예수님"), only `replace` can fix it post-recognition.

---

## 2. Solution Design

### 2.1 Gap A Fix — Glossary Terms First in Tier 2

**Current Tier 2** (unordered set, random fill order):
```
get_vocab_set() → set of all Korean tokens ≥3 chars
```

**New Tier 2** (ordered by quality):
```
Tier 2a: get_keyword_glossary() Korean side  → up to 15 curated recurring terms
Tier 2b: get_vocab_set() remainder           → remaining sermon vocab (de-duped)
```

Change in `main.py` session start (3 lines):
```python
# BEFORE
_sermon_vocab = list(script_store.get_vocab_set(room_id=room_id)) if room_id else []

# AFTER
_glossary_ko = [ko for ko, _ in script_store.get_keyword_glossary(room_id=room_id)] if room_id else []
_vocab_rest = list(script_store.get_vocab_set(room_id=room_id)) if room_id else []
_sermon_vocab = _glossary_ko + [t for t in _vocab_rest if t not in set(_glossary_ko)]
```

No new files, no new Firestore fields, no frontend changes. Pure quality improvement.

### 2.2 Gap B Fix — Deepgram `replace` Support

#### Default Korean correction list

A hardcoded list of known nova-3 Korean STT patterns to ship out of the box:

```python
# backend/app/config/deepgram_keywords.py
DEFAULT_DEEPGRAM_REPLACEMENTS = [
    ("할렐루아", "할렐루야"),    # variant spelling commonly produced by nova-3
    ("아-멘", "아멘"),           # hyphenated pause artifact
    ("예수 님", "예수님"),       # space inserted in honorific compound
    ("하나 님", "하나님"),       # space inserted in honorific compound
    ("성령 님", "성령님"),       # space inserted in honorific compound
    ("목사 님", "목사님"),       # space inserted in honorific compound
]
```

#### Per-org custom corrections in Firestore

New field: `organizations/{orgId}.sttReplacements: [{find: str, replace: str}]`

Same role constraint as `sttKeyterms`: owner/admin only, max 50 entries.

API:
- `GET /api/org/{orgId}/stt-replacements`
- `PUT /api/org/{orgId}/stt-replacements`  (body: `{"replacements": [{"find": "...", "replace": "..."}]}`)

#### `_build_replace_list()` helper

```python
def _build_replace_list(
    org_custom: Optional[List[Tuple[str, str]]] = None,
    limit: int = 200,
) -> List[Tuple[str, str]]:
    """
    Merge replace tiers: org_custom first, then defaults.
    Dedup on the 'find' key. Returns list of (find, replace) tuples.
    """
```

#### `replace` in `_qs()`

```python
# In _qs(), after keyterm params:
if replacements:
    for find, replace in replacements:
        params.append(("replace", f"{find}:{replace}"))
```

#### `connect_to_deepgram()` signature update

```python
async def connect_to_deepgram(
    model=None, language=None, keywords=None,
    sample_rate=48000,
    replacements=None,   # NEW: List[Tuple[str, str]]
):
```

#### Session start in `main.py`

```python
if src_lang.startswith("ko"):
    # ... existing keyterm building ...
    _org_replacements = multichurch_store.get_org_stt_replacements_for_session(org_id)
    dg_replacements = _build_replace_list(org_custom=_org_replacements)
else:
    dg_replacements = []

dg = await connect_to_deepgram(..., replacements=dg_replacements)
```

### 2.3 Frontend — STT Corrections UI

Add a "STT Corrections" section below the existing STT Keywords section in the Settings tab.

UI: a small table of find → replace pairs (same pattern as keyterms), owner/admin only.

---

## 3. Implementation Scope

### Phase A — Glossary → Tier 2 priority (3 lines, main.py only)
- [ ] Replace `_sermon_vocab` construction in `ws_stt_deepgram` to prepend glossary terms

### Phase B1 — Default `replace` list (config only)
- [ ] Add `DEFAULT_DEEPGRAM_REPLACEMENTS` to `deepgram_keywords.py`

### Phase B2 — `replace` in Deepgram session layer
- [ ] Add `_build_replace_list()` to `deepgram_session.py`
- [ ] Add `replace` params to `_qs()` function
- [ ] Add `replacements` param to `connect_to_deepgram()`

### Phase B3 — Per-org corrections in Firestore + API
- [ ] Add `get_org_stt_replacements()`, `set_org_stt_replacements()`, `get_org_stt_replacements_for_session()` to both `InMemoryMultiChurchStore` and `FirestoreMultiChurchStore`
- [ ] Add `GET/PUT /api/org/{orgId}/stt-replacements` to `routes/stt_keyterms.py`
- [ ] Load org replacements in `main.py` session start

### Phase B4 — Frontend
- [ ] Add `getSttReplacements()` + `setSttReplacements()` to `frontend/lib/sttKeyterms.ts`
- [ ] Add STT Corrections table to `frontend/components/SttKeytermsEditor.tsx`

---

## 4. Files to Change

| File | Change |
|---|---|
| `backend/app/main.py` | Phase A: glossary-priority Tier 2; Phase B3: load replacements |
| `backend/app/config/deepgram_keywords.py` | Phase B1: `DEFAULT_DEEPGRAM_REPLACEMENTS` |
| `backend/app/deepgram_session.py` | Phase B2: `_build_replace_list()`, `_qs()`, `connect_to_deepgram()` |
| `backend/app/services/multichurch_store.py` | Phase B3: 3 new methods × 2 store classes |
| `backend/app/routes/stt_keyterms.py` | Phase B3: 2 new endpoints |
| `frontend/lib/sttKeyterms.ts` | Phase B4: 2 new API functions |
| `frontend/components/SttKeytermsEditor.tsx` | Phase B4: corrections table UI |

---

## 5. Out of Scope

- Auto-detecting correction candidates from transcription history
- Suppression / negative boost (not available in nova-3)
- `replace` for non-Korean languages (disabled when `src_lang` is not `ko`)
- Deepgram custom model training

---

## 6. Priority

| Phase | Effort | Ship order |
|---|---|---|
| **A — Glossary → Tier 2** | 3 lines | First — immediate quality gain, zero risk |
| **B1 — Default replace list** | 10 lines | Second — ships known fixes out of the box |
| **B2 — replace in session layer** | ~30 lines | Third — wires B1 into Deepgram |
| **B3 — Per-org corrections (backend)** | ~2 hours | Fourth — admin self-service |
| **B4 — Frontend UI** | ~1 hour | Last — completes the admin workflow |
