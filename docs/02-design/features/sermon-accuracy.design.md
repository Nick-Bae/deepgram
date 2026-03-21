# Design: sermon-accuracy

## Executive Summary

| Perspective | Detail |
|---|---|
| **Problem** | Live GPT-4o translation has no knowledge of the current sermon; context window is too narrow; correction system has no UI; sermon state is wiped on restart. |
| **Solution** | 9 targeted improvements across 3 phases: better draft quality, wider context window, dynamic few-shot injection from script, keyword glossary, adaptive threshold, STT vocab correction, per-org corrections, Firestore persistence, and correction UI. |
| **Function / UX Effect** | Off-script sentences match the sermon's vocabulary. Mid-service errors can be corrected inline. A server restart no longer silently kills accuracy. Corrections compound across services. |
| **Core Value** | "Upload your sermon → get accurate translation" becomes true for the full service, not just the scripted 70%. |

---

## 1. System Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                     Phase 1 (Pure backend)               │
│  script.py     → compact_prompt=False (I5)              │
│  translate.py  → context window 2→4 / 3→5 (I3)         │
│  ScriptStore   → match_with_examples() (I1)             │
│  ScriptStore   → get_keyword_glossary() (I2)            │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│                     Phase 2 (Logic changes)              │
│  ScriptStore   → adaptive threshold in match() (I4)     │
│  translate.py  → org_id in corrections (I9)             │
│  translate.py  → _stt_vocab_correct() (I7)              │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│                     Phase 3 (Persistence + Frontend)     │
│  multichurch_store.py → save/load sermon pairs (I6)     │
│  TranslationBox.tsx   → inline correction UI (I8)       │
└─────────────────────────────────────────────────────────┘
```

---

## 2. Phase 1 Design

### 2.1 I5 — Full Prompt for Sermon Draft

**File**: `backend/app/routes/script.py`

**Change**: Remove `compact_prompt=True` from `_translate_segments()`.

```python
# BEFORE (line ~117)
result = await translate_text(
    ...
    compact_prompt=True,
)

# AFTER
result = await translate_text(
    ...
    # compact_prompt defaults to False — use full theological prompt
)
```

**Rationale**: `compact_prompt=True` strips the full theological prompt (Bible names glossary, style instructions, subject-continuity guardrails). Draft is async background — quality matters more than latency here.

**Cache impact**: None. Draft uses its own org settings; system prompt cache is keyed per `(source, target, hash(service_text), hash(custom_text), compact_prompt)`.

---

### 2.2 I3 — Increase Context Window

**File**: `backend/app/utils/translate.py`

**Change 1** — `TranslationContext.remember()` default:
```python
# BEFORE (line ~90)
def remember(self, source_text: str, translated_text: str, *, max_items: int = 3) -> None:

# AFTER
def remember(self, source_text: str, translated_text: str, *, max_items: int = 5) -> None:
```

**Change 2** — `_build_recent_context_block()` default:
```python
# BEFORE (line ~681)
def _build_recent_context_block(
    ctx: Optional[TranslationContext],
    source: str,
    target: str,
    max_items: int = 2,
) -> str:

# AFTER
def _build_recent_context_block(
    ctx: Optional[TranslationContext],
    source: str,
    target: str,
    max_items: int = 4,
) -> str:
```

**Token budget**: ~60 additional tokens per request (2 extra pairs × ~30 tokens each). Negligible vs GPT-4o's 128k context.

---

### 2.3 I1 — Script-Sourced Dynamic Few-Shot Examples

**File**: `backend/app/services/script_store.py`

**New method** on `ScriptStore`:

```python
def match_with_examples(
    self,
    text: str,
    *,
    org_id: Optional[str] = None,
    example_min_score: float = 0.20,
    example_max: int = 3,
) -> Tuple[Optional[ScriptPair], float, int, float, List[ScriptPair]]:
    """
    Single O(N) scan that returns:
      (best_match, score, version, threshold, examples)

    examples: pairs with example_min_score <= score < threshold, sorted by
    score desc, capped at example_max. If none qualify, returns the first 2
    pairs from the store as style anchors.
    Returns examples=[] if store is empty.
    """
    query = _norm(text)
    key = self._org_key(org_id)

    with self._lock:
        buffer = self._buffers.get(key) or ScriptBuffer()
        pairs_snapshot = list(buffer.pairs)
        threshold = buffer.threshold
        version = buffer.version

    if not query:
        return None, 0.0, version, threshold, []

    scored: List[Tuple[float, ScriptPair]] = []
    best: Optional[ScriptPair] = None
    best_score = 0.0

    for pair in pairs_snapshot:
        score = _similarity(pair.source, query)
        if score > best_score:
            best_score = score
            best = pair
        if example_min_score <= score:
            scored.append((score, pair))

    # Sort by score desc; exclude the winner itself
    scored.sort(key=lambda x: -x[0])
    examples: List[ScriptPair] = []
    for sc, pair in scored:
        if best and pair.index == best.index and best_score >= threshold:
            continue  # exclude the matched winner
        if sc >= threshold:
            continue  # also skip pairs that are matches (shouldn't happen here)
        examples.append(pair)
        if len(examples) >= example_max:
            break

    # Style anchors fallback
    if not examples and pairs_snapshot:
        examples = pairs_snapshot[:2]

    matched = best if best and best_score >= threshold else None
    return matched, best_score, version, threshold, examples
```

**File**: `backend/app/utils/translate.py`

**New helper** (add after `_build_fewshot_block`):

```python
def _build_script_examples_block(
    examples: List["ScriptPair"],  # TYPE_CHECKING import from script_store
    source: str,
    target: str,
) -> str:
    """Build a few-shot block from script pairs for style/vocab alignment."""
    if not examples:
        return ""
    lines = [
        f"Style reference from this sermon (use for vocabulary and style only):",
    ]
    for pair in examples:
        lines.append(f"  [{source.upper()}] {pair.source}")
        lines.append(f"  [{target.upper()}] {pair.target}")
    return "\n".join(lines)
```

**Modified** `translate_text()` signature:

```python
async def translate_text(
    source_text: str,
    *,
    source: str = "ko",
    target: str = "en",
    service_text: str = "",
    custom_text: str = "",
    ctx: Optional[TranslationContext] = None,
    compact_prompt: bool = False,
    script_examples: Optional[List["ScriptPair"]] = None,   # NEW
    script_glossary: Optional[List[Tuple[str, str]]] = None, # NEW
) -> str:
```

**Modified** `_build_system_prompt()` to accept and inject them *after* the cached base (cache NOT invalidated):

```python
def _build_system_prompt(
    source: str,
    target: str,
    service_text: str = "",
    custom_text: str = "",
    ctx: Optional[TranslationContext] = None,
    compact_prompt: bool = False,
    current_source_text: str = "",
    script_examples: Optional[List["ScriptPair"]] = None,   # NEW
    script_glossary: Optional[List[Tuple[str, str]]] = None, # NEW
) -> str:
    cache_key = (source, target, hash(service_text), hash(custom_text), compact_prompt)
    # ... existing base cache logic unchanged ...

    parts = [base]

    # Inject glossary (NEW — only when compact_prompt=False)
    if not compact_prompt and script_glossary:
        glossary_str = ", ".join(f"{k}→{v}" for k, v in script_glossary)
        parts.append(f"\nKey terms in this sermon: {glossary_str}")

    # Inject style examples (NEW — only when compact_prompt=False)
    if not compact_prompt and script_examples:
        parts.append("\n" + _build_script_examples_block(script_examples, source, target))

    # Existing fewshot + context blocks
    parts.append(_build_fewshot_block(source, target, current_source_text=current_source_text))
    parts.append(_build_recent_context_block(ctx, source, target))

    return "\n".join(p for p in parts if p)
```

**Both WebSocket handlers in `main.py`** (Deepgram STT handler + producer handler):

```python
# Replace script_store.match() with match_with_examples()
best_match, match_score, sv, threshold, script_examples = script_store.match_with_examples(
    clean_src, org_id=org_id
)
script_glossary = script_store.get_keyword_glossary(org_id=org_id)

# Pass to translate_text when falling back to GPT-4o
if best_match:
    translated = best_match.target
    mode = "pre"
else:
    translated = await translate_text(
        clean_src,
        ...
        script_examples=script_examples,
        script_glossary=script_glossary,
    )
    mode = "live"
```

---

### 2.4 I2 — Dynamic Keyword Glossary from Script

**File**: `backend/app/services/script_store.py`

**Cache structure** (add to `ScriptStore.__init__`):
```python
self._glossary_cache: Dict[Tuple[str, int], List[Tuple[str, str]]] = {}
# key: (org_key, store_version)
```

**New method** on `ScriptStore`:

```python
def get_keyword_glossary(
    self,
    *,
    org_id: Optional[str] = None,
    max_terms: int = 15,
    min_char_len: int = 2,
    max_char_len: int = 6,
    min_pair_count: int = 2,
) -> List[Tuple[str, str]]:
    """
    Extract short recurring Korean terms (min_char_len–max_char_len chars)
    appearing in >= min_pair_count source texts, with their first English
    equivalent word. Cached per (org_key, store_version).

    Returns list of (korean_term, english_term) tuples, at most max_terms.
    Returns [] if store has fewer than min_pair_count pairs.
    """
    key = self._org_key(org_id)
    with self._lock:
        buffer = self._buffers.get(key) or ScriptBuffer()
        pairs_snapshot = list(buffer.pairs)
        version = buffer.version

    cache_key = (key, version)
    if cache_key in self._glossary_cache:
        return self._glossary_cache[cache_key]

    if len(pairs_snapshot) < min_pair_count:
        self._glossary_cache[cache_key] = []
        return []

    # Count Korean token occurrences across source texts
    # Token: Hangul-only runs that meet length criteria
    _HANGUL_TOKEN_RE = re.compile(r'[\uac00-\ud7a3]+')
    token_targets: Dict[str, List[str]] = {}  # token → list of target texts it appeared in

    for pair in pairs_snapshot:
        src_tokens = _HANGUL_TOKEN_RE.findall(pair.source)
        for tok in set(src_tokens):  # unique per pair
            if min_char_len <= len(tok) <= max_char_len:
                token_targets.setdefault(tok, []).append(pair.target)

    # Keep tokens appearing in >= min_pair_count pairs
    glossary: List[Tuple[str, str]] = []
    for tok, targets in token_targets.items():
        if len(targets) < min_pair_count:
            continue
        # Use first English word from the most common target as the translation
        first_target = targets[0]
        en_words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", first_target)
        if not en_words:
            continue
        glossary.append((tok, en_words[0]))

    glossary = glossary[:max_terms]
    self._glossary_cache[cache_key] = glossary
    return glossary
```

**Cache invalidation**: The cache key includes `store_version`, which increments on every `load()` and `clear()`. No explicit invalidation needed.

---

## 3. Phase 2 Design

### 3.1 I4 — Adaptive Fuzzy Match Threshold

**File**: `backend/app/services/script_store.py`

**Change** in `ScriptStore.match()` (and identically in `match_with_examples()`):

```python
# After computing best_score, before threshold comparison:
compact_text = _norm_compact(query)
effective_threshold = threshold
if len(compact_text) < 15:
    effective_threshold = min(threshold, 0.72)

if best and best_score >= effective_threshold:
    return best, best_score, version, threshold  # report org threshold, not adjusted
return None, best_score, version, threshold
```

**Scope**: Applied in both `match()` and `match_with_examples()` consistently.

**Note**: `meta_payload` already reports `threshold` from return value — the org threshold (not adjusted) is always returned, maintaining observability consistency.

---

### 3.2 I9 — Per-Org Correction Isolation

**File**: `backend/app/routes/examples.py`

**Schema change** — add `org_id` to `CorrectionPayload`:
```python
class CorrectionPayload(BaseModel):
    source_lang: str = Field(default="ko")
    target_lang: str = Field(default="en")
    stt_text: str = Field(..., min_length=1)
    auto_translation: str = Field(..., min_length=1)
    final_translation: str = Field(..., min_length=1)
    org_id: Optional[str] = Field(default=None)  # NEW — None = global
```

**Pass-through**: `add_correction()` passes `org_id` to `log_corrected_translation()`.

**File**: `backend/app/utils/translate.py`

**Modified** `log_corrected_translation()`:
```python
def log_corrected_translation(
    source_lang: str,
    target_lang: str,
    stt_text: str,
    auto_translation: str,
    final_translation: str,
    org_id: Optional[str] = None,  # NEW
) -> None:
```

**Modified** `_log_translation_example()` — add `org_id` to the JSONL record:
```python
record = {
    "timestamp": ...,
    "source_lang": ...,
    "target_lang": ...,
    "stt_text": ...,
    "auto_translation": ...,
    "final_translation": ...,
    "corrected": corrected,
}
if org_id:
    record["org_id"] = org_id   # NEW
```

**Modified** `_load_fewshot_examples()` — add org filtering:
```python
def _load_fewshot_examples(
    source_lang: str,
    target_lang: str,
    org_id: Optional[str] = None,   # NEW
    max_examples: int = 6,
) -> List[dict]:
    # Load org-specific first
    org_records = [r for r in all_corrected if r.get("org_id") == org_id] if org_id else []
    # Fill remainder from global (no org_id field or org_id != current)
    global_records = [r for r in all_corrected if not r.get("org_id")]

    combined = org_records[-max_examples:] if len(org_records) >= max_examples \
        else org_records + global_records[-(max_examples - len(org_records)):]
    return combined[-max_examples:]
```

**Backwards compatibility**: Records without `org_id` are treated as global — no migration needed.

---

### 3.3 I7 — Sermon-Aware STT Vocabulary Correction

**File**: `backend/app/services/script_store.py`

**New method** on `ScriptStore`:
```python
def get_vocab_set(self, *, org_id: Optional[str] = None) -> Set[str]:
    """
    Return all unique Hangul tokens from script source texts.
    Used for STT error correction (edit distance 1).
    """
    key = self._org_key(org_id)
    with self._lock:
        buffer = self._buffers.get(key) or ScriptBuffer()
        pairs_snapshot = list(buffer.pairs)

    _HANGUL_TOKEN_RE = re.compile(r'[\uac00-\ud7a3]+')
    vocab: Set[str] = set()
    for pair in pairs_snapshot:
        for tok in _HANGUL_TOKEN_RE.findall(pair.source):
            if len(tok) >= 3:  # avoid particles
                vocab.add(tok)
    return vocab
```

**File**: `backend/app/utils/translate.py`

**New helper** (add near top of file):
```python
def _stt_vocab_correct(text: str, vocab_set: Set[str]) -> str:
    """
    Replace STT tokens that are 1 edit distance from a vocab word.
    Only corrects tokens with len >= 3 (avoids Korean particles).
    Uses simple character-level edit distance (insertions/deletions/substitutions = 1).
    """
    if not vocab_set or not text:
        return text

    _HANGUL_TOKEN_RE = re.compile(r'([\uac00-\ud7a3]+)')

    def _edit1(a: str, b: str) -> bool:
        """True if edit distance between a and b is exactly 1."""
        if abs(len(a) - len(b)) > 1:
            return False
        if len(a) == len(b):
            diffs = sum(1 for x, y in zip(a, b) if x != y)
            return diffs == 1
        short, long = (a, b) if len(a) < len(b) else (b, a)
        i = j = 0
        found = False
        while i < len(short) and j < len(long):
            if short[i] == long[j]:
                i += 1; j += 1
            elif found:
                return False
            else:
                found = True; j += 1
        return True

    def replace_token(m: re.Match) -> str:
        tok = m.group(1)
        if len(tok) < 3 or tok in vocab_set:
            return tok
        for v in vocab_set:
            if len(v) >= 3 and _edit1(tok, v):
                return v
        return tok

    return _HANGUL_TOKEN_RE.sub(replace_token, text)
```

**Integration point** in both WS handlers in `main.py`:
```python
# After receiving STT text, before match_with_examples():
vocab_set = script_store.get_vocab_set(org_id=org_id)
if vocab_set:
    clean_src = _stt_vocab_correct(clean_src, vocab_set)
```

**Performance note**: `get_vocab_set()` returns a snapshot on each call. For high-frequency use, callers should cache the result for the WS session (re-fetch only on script version change). See main.py integration notes in §5.

---

## 4. Phase 3 Design

### 4.1 I6 — Firestore-Backed Script Persistence

#### 4.1.1 Firestore Schema

Collection path: `organizations/{orgId}/sermons/{sermonId}`

Document fields:
```
{
  "sermon_id": string,
  "created_at": Timestamp,
  "threshold": float,
  "lang_src": string,
  "lang_tgt": string,
  "pairs": [
    {"source": string, "target": string},
    ...
  ]
}
```

**Index**: None required (fetching most recent = `orderBy("created_at", "desc").limit(1)`).

#### 4.1.2 Firestore Security Rule

File: `backend/firestore/firestore.rules`

```
match /organizations/{orgId}/sermons/{sermonId} {
  // Backend writes only; no client read or write
  allow read, write: if false;
}
```

#### 4.1.3 Backend — multichurch_store.py

Add two methods to `FirestoreMultiChurchStore` (no-op stubs on `InMemoryMultiChurchStore`):

```python
def save_sermon_pairs(
    self,
    org_id: str,
    sermon_id: str,
    pairs: List[dict],
    *,
    threshold: float = 0.84,
    lang_src: str = "ko",
    lang_tgt: str = "en",
) -> None:
    """
    Write sermon pairs to Firestore. Fire-and-forget safe — caller should
    not await or check return value.
    """
    doc_ref = (
        self._db
        .collection("organizations").document(org_id)
        .collection("sermons").document(sermon_id)
    )
    doc_ref.set({
        "sermon_id": sermon_id,
        "created_at": firestore.SERVER_TIMESTAMP,
        "threshold": threshold,
        "lang_src": lang_src,
        "lang_tgt": lang_tgt,
        "pairs": pairs,
    })

def get_latest_sermon_pairs(self, org_id: str) -> Optional[dict]:
    """
    Return the most recently finalized sermon document for an org, or None.
    Format: {"sermon_id": str, "pairs": [...], "threshold": float, ...}
    """
    col_ref = (
        self._db
        .collection("organizations").document(org_id)
        .collection("sermons")
    )
    docs = col_ref.order_by("created_at", direction="DESCENDING").limit(1).get()
    for doc in docs:
        return doc.to_dict()
    return None
```

#### 4.1.4 Write on Finalize — script.py

```python
@router.post("/org/{org_id}/sermon/finalize")
def finalize_sermon(org_id: str, body: FinalizeBody, user=...):
    ...
    loaded, used_threshold, version = script_store.load(pairs, body.threshold, org_id=org_id)
    script_store.save_sermon(payload, org_id=org_id)

    # NEW: Fire-and-forget Firestore write
    try:
        store.save_sermon_pairs(
            org_id=org_id,
            sermon_id=body.sermon_id,
            pairs=[{"source": p.source, "target": p.target} for p in ...],
            threshold=body.threshold or 0.84,
            lang_src=body.lang_src or "ko",
            lang_tgt=body.lang_tgt or "en",
        )
    except Exception:
        pass  # never fail finalize due to Firestore write error
```

#### 4.1.5 Auto-Reload on WS Connect — main.py

```python
# In ws_stt_deepgram handler, after org_id is resolved:
count, _, _ = script_store.stats(org_id=org_id)
if count == 0:
    # Fire-and-forget: do not delay WS handshake
    asyncio.get_event_loop().run_in_executor(None, lambda: _try_reload_sermon(org_id))

def _try_reload_sermon(org_id: str) -> None:
    """Load most recent Firestore sermon into script_store if store is empty."""
    try:
        doc = store.get_latest_sermon_pairs(org_id)
        if not doc or not doc.get("pairs"):
            return
        current_count, _, _ = script_store.stats(org_id=org_id)
        if current_count > 0:
            return  # already loaded by another connection
        script_store.load(
            doc["pairs"],
            doc.get("threshold"),
            org_id=org_id,
        )
    except Exception:
        pass  # never block WS on Firestore error
```

---

### 4.2 I8 — Live Correction UI in Host Console

#### 4.2.1 Location

The correction UI lives in **`TranslationBox.tsx`** (self-contained, renders in host console). `orgId` is read from `sessionStorage` via the existing `contextFromSession()` helper in `utils/streamContext.ts`.

#### 4.2.2 State

Add to `TranslationBox` component state:

```typescript
type CommittedLine = {
  id: number;
  srcText: string;    // Korean STT text
  translated: string; // English translation as broadcast
};

const [committedLines, setCommittedLines] = useState<CommittedLine[]>([]);
const [correcting, setCorrecting] = useState<number | null>(null); // id of line being corrected
const [correctionDraft, setCorrectionDraft] = useState("");
const [correctionSaved, setCorrectionSaved] = useState<Set<number>>(new Set());
```

#### 4.2.3 Capturing Committed Translations

When a final translation is committed (existing logic that triggers TTS), also append to `committedLines`:

```typescript
// In the final translation handler (where TTS is triggered):
setCommittedLines(prev => [
  ...prev.slice(-20),  // keep last 20 lines max
  { id: Date.now(), srcText: committedSrc, translated: finalEnglish }
]);
```

#### 4.2.4 UI — Translation History Panel

Below the main live translation display, add a collapsible "Translation Log" panel showing `committedLines`:

```tsx
{committedLines.length > 0 && (
  <div className="mt-3 border-t border-slate-200 pt-2">
    <p className="text-xs text-slate-400 mb-1">Recent translations</p>
    <div className="space-y-1 max-h-48 overflow-y-auto">
      {[...committedLines].reverse().map(line => (
        <div key={line.id} className="flex items-start gap-2 group">
          <div className="flex-1 text-sm">
            <span className="text-slate-500 text-xs">{line.srcText}</span>
            {correcting === line.id ? (
              <div className="mt-1 flex gap-1">
                <input
                  className="flex-1 text-sm border border-slate-300 rounded px-2 py-0.5"
                  value={correctionDraft}
                  onChange={e => setCorrectionDraft(e.target.value)}
                  autoFocus
                />
                <button
                  onClick={() => submitCorrection(line)}
                  className="text-xs px-2 py-0.5 bg-blue-600 text-white rounded"
                >Save</button>
                <button
                  onClick={() => setCorrecting(null)}
                  className="text-xs px-2 py-0.5 text-slate-500"
                >Cancel</button>
              </div>
            ) : (
              <p className={`text-sm ${correctionSaved.has(line.id) ? "text-green-700" : "text-slate-800"}`}>
                {line.translated}
                {correctionSaved.has(line.id) && (
                  <span className="ml-1 text-xs text-green-600">✓ saved</span>
                )}
              </p>
            )}
          </div>
          {correcting !== line.id && !correctionSaved.has(line.id) && (
            <button
              className="opacity-0 group-hover:opacity-100 text-xs text-slate-400 hover:text-slate-700 mt-0.5"
              title="Correct this translation"
              onClick={() => {
                setCorrecting(line.id);
                setCorrectionDraft(line.translated);
              }}
            >✎</button>
          )}
        </div>
      ))}
    </div>
  </div>
)}
```

#### 4.2.5 Submit Handler

```typescript
const submitCorrection = useCallback(async (line: CommittedLine) => {
  const { orgId } = contextFromSession();
  const idToken = getAuthTokenFromSession();
  if (!correctionDraft.trim() || correctionDraft === line.translated) {
    setCorrecting(null);
    return;
  }
  try {
    await fetch(`${API_URL}/examples/correct`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(idToken ? { Authorization: `Bearer ${idToken}` } : {}),
      },
      body: JSON.stringify({
        stt_text: line.srcText,
        auto_translation: line.translated,
        final_translation: correctionDraft.trim(),
        org_id: orgId || undefined,
      }),
    });
    setCorrectionSaved(prev => new Set([...prev, line.id]));
  } catch {
    // silent fail — non-critical
  } finally {
    setCorrecting(null);
  }
}, [correctionDraft]);
```

---

## 5. main.py Integration Notes

Both WebSocket handlers (`ws_stt_deepgram` and the producer WS) need identical updates for I1, I2, I7. To avoid repetition, introduce a small helper:

```python
# Add near top of main.py, after script_store import:
def _get_script_context(org_id: Optional[str]):
    """Returns (match_fn, glossary, vocab_set) for a given org's script store."""
    # vocab_set is fetched once per call — callers should cache per WS session
    vocab_set = script_store.get_vocab_set(org_id=org_id)
    glossary = script_store.get_keyword_glossary(org_id=org_id)
    return vocab_set, glossary
```

**Session-level caching pattern** (to avoid O(N) vocab_set recomputation on every utterance):

```python
# At WS connect time:
_cached_script_version = -1
_cached_vocab_set: set[str] = set()
_cached_glossary: list[tuple[str, str]] = []

# Before each translation (in the utterance loop):
_, _, current_version, _ = script_store.stats(org_id=org_id)  # lightweight
if current_version != _cached_script_version:
    _cached_vocab_set = script_store.get_vocab_set(org_id=org_id)
    _cached_glossary = script_store.get_keyword_glossary(org_id=org_id)
    _cached_script_version = current_version
```

---

## 6. Files to Modify — Complete List

| File | Phase | Changes |
|---|---|---|
| `backend/app/routes/script.py` | P1 | Remove `compact_prompt=True` from `_translate_segments()` |
| `backend/app/utils/translate.py` | P1 | `remember()` max_items 3→5; `_build_recent_context_block()` max_items 2→4; add `_build_script_examples_block()`; add `script_examples` + `script_glossary` params to `_build_system_prompt()` and `translate_text()`; add `_stt_vocab_correct()` (P2); add `org_id` to `_log_translation_example()` and `_load_fewshot_examples()` (P2) |
| `backend/app/services/script_store.py` | P1,P2 | Add `match_with_examples()`, `get_keyword_glossary()` with `_glossary_cache`; adaptive threshold in `match()` and `match_with_examples()` (P2); `get_vocab_set()` (P2) |
| `backend/app/main.py` | P1,P2,P3 | Both WS handlers: use `match_with_examples()`, pass `script_examples` + `script_glossary`; STT vocab correction; session-level cache; `_try_reload_sermon()` on empty store (P3) |
| `backend/app/routes/examples.py` | P2 | Add `org_id` to `CorrectionPayload`; pass to `log_corrected_translation()` |
| `backend/app/services/multichurch_store.py` | P3 | Add `save_sermon_pairs()`, `get_latest_sermon_pairs()` to both store classes |
| `backend/firestore/firestore.rules` | P3 | Add `sermons/{sermonId}` read/write rule |
| `backend/app/routes/script.py` | P3 | Fire-and-forget Firestore write after finalize |
| `frontend/components/TranslationBox.tsx` | P3 | `CommittedLine` state, committed line capture, correction panel UI, `submitCorrection()` |

**No new Python dependencies.** No new npm packages.

---

## 7. Acceptance Criteria (Reference)

See `docs/01-plan/features/sermon-accuracy.plan.md` §4 for full acceptance criteria.

Key acceptance criteria summary per phase:

**Phase 1**
- `POST /sermon/draft` uses full theological prompt (no `compact_prompt`)
- `_build_recent_context_block` returns up to 4 pairs
- `TranslationContext.remember()` retains up to 5 pairs
- `match_with_examples()` returns examples list alongside match result
- Keyword glossary cached per `(org_key, version)`, max 15 terms
- Both WS handlers pass `script_examples` and `script_glossary` to `translate_text()`

**Phase 2**
- Short texts (< 15 compact chars) use `min(threshold, 0.72)` effective threshold
- `POST /examples/correct` stores `org_id` field
- `_load_fewshot_examples()` returns org-specific records first, then global
- `_stt_vocab_correct()` only corrects when vocab word length ≥ 3 and edit distance = 1

**Phase 3**
- Finalize writes to `organizations/{orgId}/sermons/{sermonId}` in Firestore
- Write failure never fails the finalize HTTP response
- Auto-reload on empty store is fire-and-forget (no WS handshake delay)
- Correction button hidden during live editing; shows ✓ on save
- `POST /examples/correct` called with `org_id` from `contextFromSession()`
