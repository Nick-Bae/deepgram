# Design: deepgram-vocab-complete

## Executive Summary

| Perspective | Detail |
|---|---|
| **Problem** | Only `keyterm` is used; `replace` is not. Glossary terms aren't prioritized in Tier 2 keyterms. |
| **Solution** | Phase A: 3-line change to put glossary terms first in Tier 2. Phase B: full `replace` pipeline — default list + per-org custom corrections + frontend UI. |
| **Function / UX Effect** | Systematic STT errors (honorific spaces, variant spellings) silently corrected before translation. |
| **Core Value** | Two-layer accuracy: recognize better (keyterm) + fix what slips through (replace). |

---

## 1. System Architecture

```
Session Start (/ws/stt/deepgram)
  │
  ├── Keyterm list (100 slots)
  │     Tier 1: org custom keyterms          sttKeyterms in Firestore
  │     Tier 2a: glossary Korean terms  ←── get_keyword_glossary() [NEW priority]
  │     Tier 2b: vocab set remainder         get_vocab_set()
  │     Tier 3: default list                 deepgram_keywords.py
  │
  ├── Replace list (~200 slots)              [ALL NEW]
  │     Tier 1: org custom corrections       sttReplacements in Firestore
  │     Tier 2: default corrections          DEFAULT_DEEPGRAM_REPLACEMENTS
  │
  └── connect_to_deepgram(keywords=..., replacements=...)
            │
            ▼
      ?keyterm=할렐루야&keyterm=사사기&...&replace=할렐루아:할렐루야&replace=예수 님:예수님&...
            │
            ▼
      Deepgram nova-3
        [Recognition] ← keyterms bias model
        [Replace]     ← replace post-processes output
            │
            ▼
      Corrected Korean transcript → translation pipeline
```

---

## 2. Phase A — Glossary-Priority Tier 2

**File**: `backend/app/main.py` — `ws_stt_deepgram()`, session start block (~line 1405)

```python
# BEFORE
_sermon_vocab = list(script_store.get_vocab_set(room_id=room_id)) if room_id else []

# AFTER
_glossary_ko = [ko for ko, _ in script_store.get_keyword_glossary(room_id=room_id)] if room_id else []
_vocab_rest  = list(script_store.get_vocab_set(room_id=room_id)) if room_id else []
_sermon_vocab = _glossary_ko + [t for t in _vocab_rest if t not in set(_glossary_ko)]
```

**Why**: `get_keyword_glossary()` returns ≤15 terms that appear in ≥2 sermon lines — the highest-quality subset of the sermon vocab. These must fill keyterm slots before the broader unordered `get_vocab_set()` to ensure they aren't crowded out when the 100-term budget is tight.

No new files, no Firestore changes, no frontend changes.

---

## 3. Phase B1 — Default Replacement List

**File**: `backend/app/config/deepgram_keywords.py`

Add after `DEFAULT_DEEPGRAM_KEYWORDS`:

```python
# Known nova-3 Korean STT patterns that replace can fix post-recognition.
# Each tuple: (find, replacement). 'find' is lowercase → matches any case.
DEFAULT_DEEPGRAM_REPLACEMENTS: List[Tuple[str, str]] = [
    ("할렐루아",  "할렐루야"),   # variant spelling (common nova-3 output)
    ("아-멘",     "아멘"),        # hyphenated pause artifact
    ("예수 님",   "예수님"),      # space inserted in honorific compound
    ("하나 님",   "하나님"),      # space inserted in honorific compound
    ("성령 님",   "성령님"),      # space inserted in honorific compound
    ("목사 님",   "목사님"),      # space inserted in honorific compound
    ("전도 사",   "전도사"),      # space in 전도사
    ("장로 님",   "장로님"),      # space in honorific
]
```

Add import at top of file:
```python
from typing import List, Tuple
```

---

## 4. Phase B2 — `replace` in Deepgram Session Layer

**File**: `backend/app/deepgram_session.py`

### 4.1 Import

Add to existing imports:
```python
from app.config.deepgram_keywords import DEFAULT_DEEPGRAM_KEYWORDS, DEFAULT_DEEPGRAM_REPLACEMENTS
```

### 4.2 `_build_replace_list()` helper

Add after `_build_keyterm_list()`:

```python
def _build_replace_list(
    org_custom: Optional[List[Tuple[str, str]]] = None,
    limit: int = 200,
) -> List[Tuple[str, str]]:
    """
    Merge replace tiers (priority: org_custom > defaults).
    Dedup on the 'find' key (lowercased). Returns list of (find, replacement) tuples.

    Tier 1 — org_custom:  per-church corrections (admin-defined)
    Tier 2 — defaults:    DEFAULT_DEEPGRAM_REPLACEMENTS (known nova-3 patterns)
    """
    seen: set = set()
    result: List[Tuple[str, str]] = []

    def _add(pairs: List[Tuple[str, str]]) -> None:
        for find, replacement in pairs:
            find = (find or "").strip()
            replacement = (replacement or "").strip()
            if not find:
                continue
            key = find.lower()
            if key not in seen and len(result) < limit:
                seen.add(key)
                result.append((find, replacement))

    _add(org_custom or [])
    _add(DEFAULT_DEEPGRAM_REPLACEMENTS)

    if DG_DEBUG:
        print(f"[DG] _build_replace_list: {len(result)} pairs "
              f"(org={len(org_custom or [])}, defaults={len(DEFAULT_DEEPGRAM_REPLACEMENTS)})")
    return result
```

### 4.3 Update `_qs()` signature and body

```python
# BEFORE signature
def _qs(
    model: str,
    language: str,
    sample_rate: int,
    keywords: Optional[List[str]],
    endpointing_ms: Optional[int],
    utter_end_ms: Optional[int],
) -> str:

# AFTER signature
def _qs(
    model: str,
    language: str,
    sample_rate: int,
    keywords: Optional[List[str]],
    endpointing_ms: Optional[int],
    utter_end_ms: Optional[int],
    replacements: Optional[List[Tuple[str, str]]] = None,   # NEW
) -> str:
```

Add after the keyterm params block (before `return urlencode`):
```python
    # Find-and-replace: post-processing corrections (nova-3 + nova-2, not Flux)
    if replacements:
        for find, replacement in replacements:
            params.append(("replace", f"{find}:{replacement}"))
```

### 4.4 Update `connect_to_deepgram()` signature and call

```python
# BEFORE
async def connect_to_deepgram(
    model: Optional[str] = None,
    language: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    sample_rate: int = 48000,
):

# AFTER
async def connect_to_deepgram(
    model: Optional[str] = None,
    language: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    sample_rate: int = 48000,
    replacements: Optional[List[Tuple[str, str]]] = None,   # NEW
):
```

Pass `replacements` into `_qs()`:
```python
# BEFORE
url = f"{DG_ENDPOINT}?{_qs(m, lg, sample_rate, kw, DG_ENDPOINTING_MS, DG_UTTER_END_MS)}"

# AFTER
url = f"{DG_ENDPOINT}?{_qs(m, lg, sample_rate, kw, DG_ENDPOINTING_MS, DG_UTTER_END_MS, replacements)}"
```

---

## 5. Phase B3 — Per-Org Corrections in Firestore + API

### 5.1 Constants in `multichurch_store.py`

Add alongside `STT_KEYTERMS_MAX` (~line 117):
```python
STT_REPLACEMENTS_MAX: int = 50
```

### 5.2 `InMemoryMultiChurchStore` — 3 new methods

Add after `get_org_stt_keyterms_for_session()` (~line 1805):

```python
def get_org_stt_replacements(self, *, org_id: str, requested_by_uid: str) -> Dict[str, Any]:
    clean_org_id = _clean_token(org_id)
    clean_uid = _clean_token(requested_by_uid)
    if not clean_org_id:
        raise ValueError("org_not_found")
    if not clean_uid:
        raise ValueError("invalid_uid")
    with self._lock:
        org = self._orgs.get(clean_org_id)
        if not org:
            raise ValueError("org_not_found")
        role = self._member_role(clean_org_id, clean_uid)
        if role not in STT_KEYTERMS_ROLES:
            raise PermissionError("forbidden")
        return {
            "orgId": clean_org_id,
            "replacements": list(org.get("sttReplacements") or []),
        }

def set_org_stt_replacements(
    self,
    *,
    org_id: str,
    requested_by_uid: str,
    replacements: List[Dict[str, str]],
) -> Dict[str, Any]:
    clean_org_id = _clean_token(org_id)
    clean_uid = _clean_token(requested_by_uid)
    if not clean_org_id:
        raise ValueError("org_not_found")
    if not clean_uid:
        raise ValueError("invalid_uid")
    with self._lock:
        org = self._orgs.get(clean_org_id)
        if not org:
            raise ValueError("org_not_found")
        role = self._member_role(clean_org_id, clean_uid)
        if role not in STT_KEYTERMS_ROLES:
            raise PermissionError("forbidden")
        cleaned = _clean_replacements(replacements, STT_REPLACEMENTS_MAX)
        org["sttReplacements"] = cleaned
        org["updatedAt"] = _utcnow()
        return {"orgId": clean_org_id, "replacements": cleaned}

def get_org_stt_replacements_for_session(self, org_id: Optional[str]) -> List[Tuple[str, str]]:
    """Called at session start — no auth check, returns list of (find, replace) tuples."""
    clean_org_id = _clean_token(org_id)
    if not clean_org_id:
        return []
    with self._lock:
        org = self._orgs.get(clean_org_id)
        if not org:
            return []
        return [(r["find"], r["replace"]) for r in (org.get("sttReplacements") or [])
                if r.get("find") and r.get("replace") is not None]
```

### 5.3 `FirestoreMultiChurchStore` — 3 new methods

Add after `get_org_stt_keyterms_for_session()` in the Firestore class (~line 4565):

```python
def get_org_stt_replacements(self, *, org_id: str, requested_by_uid: str) -> Dict[str, Any]:
    clean_org_id = _clean_token(org_id)
    clean_uid = _clean_token(requested_by_uid)
    if not clean_org_id:
        raise ValueError("org_not_found")
    if not clean_uid:
        raise ValueError("invalid_uid")
    org_snap = self._org_ref(clean_org_id).get()
    if not org_snap.exists:
        raise ValueError("org_not_found")
    role = self._member_role(clean_org_id, clean_uid)
    if role not in STT_KEYTERMS_ROLES:
        raise PermissionError("forbidden")
    org = org_snap.to_dict() or {}
    return {
        "orgId": clean_org_id,
        "replacements": list(org.get("sttReplacements") or []),
    }

def set_org_stt_replacements(
    self,
    *,
    org_id: str,
    requested_by_uid: str,
    replacements: List[Dict[str, str]],
) -> Dict[str, Any]:
    clean_org_id = _clean_token(org_id)
    clean_uid = _clean_token(requested_by_uid)
    if not clean_org_id:
        raise ValueError("org_not_found")
    if not clean_uid:
        raise ValueError("invalid_uid")
    org_ref = self._org_ref(clean_org_id)
    org_snap = org_ref.get()
    if not org_snap.exists:
        raise ValueError("org_not_found")
    role = self._member_role(clean_org_id, clean_uid)
    if role not in STT_KEYTERMS_ROLES:
        raise PermissionError("forbidden")
    cleaned = _clean_replacements(replacements, STT_REPLACEMENTS_MAX)
    org_ref.set(
        {"sttReplacements": cleaned, "updatedAt": gcf_firestore.SERVER_TIMESTAMP},
        merge=True,
    )
    return {"orgId": clean_org_id, "replacements": cleaned}

def get_org_stt_replacements_for_session(self, org_id: Optional[str]) -> List[Tuple[str, str]]:
    """Called at session start — no auth check, returns list of (find, replace) tuples."""
    clean_org_id = _clean_token(org_id)
    if not clean_org_id:
        return []
    org_snap = self._org_ref(clean_org_id).get()
    if not org_snap.exists:
        return []
    org = org_snap.to_dict() or {}
    return [(r["find"], r["replace"]) for r in (org.get("sttReplacements") or [])
            if r.get("find") and r.get("replace") is not None]
```

### 5.4 `_clean_replacements()` module-level helper

Add as a module-level function in `multichurch_store.py` (near other helpers):

```python
def _clean_replacements(
    raw: List[Dict[str, str]], max_count: int
) -> List[Dict[str, str]]:
    """Validate and deduplicate replacement pairs. Dedup on 'find' key."""
    seen: set = set()
    result = []
    for item in raw:
        find = str(item.get("find") or "").strip()
        replacement = str(item.get("replace") or "").strip()
        if not find:
            continue
        key = find.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append({"find": find, "replace": replacement})
        if len(result) >= max_count:
            break
    return result
```

### 5.5 New endpoints in `routes/stt_keyterms.py`

Add after the existing PUT `/stt-keyterms` endpoint:

```python
class SttReplacementItem(BaseModel):
    find: str = Field(min_length=1, max_length=50)
    replace: str = Field(default="", max_length=50)

class SttReplacementsPayload(BaseModel):
    replacements: List[SttReplacementItem] = Field(default_factory=list, max_length=50)


@router.get("/org/{org_id}/stt-replacements")
def get_org_stt_replacements(
    org_id: str = Path(pattern=validators.ORG_ID),
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(
        org_id=org_id, user=user, roles=STT_KEYTERMS_ROLES, store=multichurch_store,
        missing_membership_detail="forbidden",
    )
    try:
        return multichurch_store.get_org_stt_replacements(
            org_id=org_id, requested_by_uid=user.uid
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=404 if str(exc) == "org_not_found" else 400, detail=str(exc)
        ) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc) or "forbidden") from exc


@router.put("/org/{org_id}/stt-replacements")
def set_org_stt_replacements(
    *,
    org_id: str = Path(pattern=validators.ORG_ID),
    payload: SttReplacementsPayload,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(
        org_id=org_id, user=user, roles=STT_KEYTERMS_ROLES, store=multichurch_store,
        missing_membership_detail="forbidden",
    )
    try:
        return multichurch_store.set_org_stt_replacements(
            org_id=org_id,
            requested_by_uid=user.uid,
            replacements=[r.model_dump() for r in payload.replacements],
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=404 if str(exc) == "org_not_found" else 400, detail=str(exc)
        ) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc) or "forbidden") from exc
```

### 5.6 Session start update in `main.py`

Update the import line:
```python
# BEFORE
from app.deepgram_session import connect_to_deepgram, _build_keyterm_list, DG_DEBUG

# AFTER
from app.deepgram_session import connect_to_deepgram, _build_keyterm_list, _build_replace_list, DG_DEBUG
```

Update the Korean keyterm/replace block:
```python
if src_lang.startswith("ko"):
    _org_keyterms = multichurch_store.get_org_stt_keyterms_for_session(org_id)
    # Tier 2a: glossary terms first (curated), then broader vocab
    _glossary_ko  = [ko for ko, _ in script_store.get_keyword_glossary(room_id=room_id)] if room_id else []
    _vocab_rest   = list(script_store.get_vocab_set(room_id=room_id)) if room_id else []
    _sermon_vocab = _glossary_ko + [t for t in _vocab_rest if t not in set(_glossary_ko)]
    dg_keywords = _build_keyterm_list(
        org_custom=_org_keyterms,
        sermon_vocab=_sermon_vocab,
    )
    # Replace: org corrections first, then defaults
    _org_replacements = multichurch_store.get_org_stt_replacements_for_session(org_id)
    dg_replacements = _build_replace_list(org_custom=_org_replacements)
else:
    dg_keywords = []
    dg_replacements = []

dg = await connect_to_deepgram(language=dg_language, keywords=dg_keywords, replacements=dg_replacements)
```

---

## 6. Phase B4 — Frontend

### 6.1 API client additions to `frontend/lib/sttKeyterms.ts`

```typescript
export type SttReplacementItem = { find: string; replace: string };
export type SttReplacementsResponse = { orgId: string; replacements: SttReplacementItem[] };

export async function getSttReplacements(
  orgId: string, idToken: string
): Promise<SttReplacementsResponse> {
  const res = await fetch(
    `${API_URL}/api/org/${encodeURIComponent(orgId)}/stt-replacements`,
    { headers: { Authorization: `Bearer ${idToken}` } }
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function setSttReplacements(
  orgId: string, replacements: SttReplacementItem[], idToken: string
): Promise<SttReplacementsResponse> {
  const res = await fetch(
    `${API_URL}/api/org/${encodeURIComponent(orgId)}/stt-replacements`,
    {
      method: "PUT",
      headers: { Authorization: `Bearer ${idToken}`, "Content-Type": "application/json" },
      body: JSON.stringify({ replacements }),
    }
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
```

### 6.2 STT Corrections section in `SttKeytermsEditor.tsx`

Add a new collapsible section below the existing keywords UI:

**UI structure:**
- Section header: "STT Corrections" with pair count (`{custom.length} custom · {DEFAULT_COUNT} built-in`)
- Built-in corrections: collapsed `<details>` listing `DEFAULT_DEEPGRAM_REPLACEMENTS` (read-only)
- Custom corrections table:
  - Each row: `[find input] → [replace input] [×]`
  - Add row button (disabled when ≥50 custom pairs)
  - Save button → `PUT /api/org/{orgId}/stt-replacements`
  - Saved confirmation: "Saved. Takes effect on next session start."
- Note below table: "Find terms are case-insensitive. Leave replace empty to delete the term from transcripts."

**State additions to component:**
```typescript
const [replacements, setReplacements] = useState<SttReplacementItem[]>([]);
const [replacementsSaved, setReplacementsSaved] = useState(false);
const [replacementsSaving, setReplacementsSaving] = useState(false);
```

---

## 7. Data Constraints Summary

| Constraint | Value | Enforced by |
|---|---|---|
| Max org custom keyterms | 50 | `set_org_stt_keyterms()` slice + Pydantic `max_length=50` |
| Max org custom replacements | 50 | `_clean_replacements()` + Pydantic `max_length=50` |
| Max replace `find` length | 50 chars | Pydantic `max_length=50` on `SttReplacementItem.find` |
| Total replace pairs to Deepgram | ≤200 | `_build_replace_list(limit=200)` |
| `replace` only for Korean | — | `if src_lang.startswith("ko")` check in `main.py` |
| `replace` dedup key | `find.lower()` | `_build_replace_list()` and `_clean_replacements()` |

---

## 8. Implementation Order

1. **Phase A** — `main.py` 3-line glossary Tier 2 fix (zero risk, no deps)
2. **Phase B1** — `deepgram_keywords.py` add `DEFAULT_DEEPGRAM_REPLACEMENTS`
3. **Phase B2** — `deepgram_session.py` — `_build_replace_list()`, `_qs()`, `connect_to_deepgram()`
4. **Phase B3a** — `multichurch_store.py` — `_clean_replacements()` + 3 methods × 2 classes + `STT_REPLACEMENTS_MAX`
5. **Phase B3b** — `routes/stt_keyterms.py` — 2 new endpoints
6. **Phase B3c** — `main.py` — import + session start wiring
7. **Phase B4** — `frontend/lib/sttKeyterms.ts` + `SttKeytermsEditor.tsx`
