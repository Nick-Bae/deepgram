# Design: deepgram-keyterms

## Executive Summary

| Perspective | Detail |
|---|---|
| **Problem** | Static 117-term list trimmed to 60, boost values silently ignored for nova-3, no per-church terms, and sermon vocab never reaches Deepgram. |
| **Solution** | 4 targeted changes: config cleanup, raise limit to 100, per-org custom keyterms in Firestore, and sermon script vocab injected at session start. |
| **Function / UX Effect** | Korean STT recognizes pastor names, church-specific terms, and today's sermon vocabulary. |
| **Core Value** | STT accuracy becomes church-specific and sermon-aware before translation even begins. |

---

## 1. System Architecture Overview

```
Session Start (/ws/stt/deepgram)
  │
  ├── Tier 1: org custom keyterms ─────── Firestore: organizations/{orgId}.sttKeyterms
  │           (max 50 slots)              GET /api/org/{orgId}/stt-keyterms
  │                                       PUT /api/org/{orgId}/stt-keyterms
  │
  ├── Tier 2: sermon script vocab ─────── script_store.get_vocab_set(room_id)
  │           (fills remaining up to 100) already extracts Korean tokens
  │
  └── Tier 3: default church terms ────── config/deepgram_keywords.py (cleaned)
              (fills remaining budget)    96 terms: 30 worship + 66 Bible books
                                           ↓
                                    connect_to_deepgram(keywords=merged_list)
                                           ↓
                                    ?keyterm=예수님&keyterm=하나님&... (nova-3)
```

---

## 2. Phase 1 — Config Cleanup

### 2.1 Restructure `DEFAULT_DEEPGRAM_KEYWORDS`

**File**: `backend/app/config/deepgram_keywords.py`

Remove all boost values (`:2.5`, `:2.0`, etc.) since the current model is `nova-3` and boost is
silently ignored. Split into two named groups for clarity.

```python
# BEFORE (mixed list with boost values)
DEFAULT_DEEPGRAM_KEYWORDS = [
    "예수님:2.5",
    "하나님:2.5",
    ...
]

# AFTER (two groups, no boost, plain strings)
_WORSHIP_TERMS = [
    "예수님", "하나님", "성령님", "할렐루야", "아멘",
    "주님", "예배", "찬양", "기도", "믿음",
    "말씀", "복음", "구원", "은혜", "십자가",
    "부활", "성경", "교회", "목사님", "사랑",
    "죄", "용서", "영광", "천국", "지옥",
    "성도", "예수", "그리스도", "주", "회개",
]  # 30 terms

_BIBLE_BOOKS = [
    "창세기", "출애굽기", "레위기", "민수기", "신명기",
    "여호수아", "사사기", "룻기", "사무엘상", "사무엘하",
    "열왕기상", "열왕기하", "역대상", "역대하", "에스라",
    "느헤미야", "에스더", "욥기", "시편", "잠언",
    "전도서", "아가", "이사야", "예레미야", "예레미야애가",
    "에스겔", "다니엘", "호세아", "요엘", "아모스",
    "오바댜", "요나", "미가", "나훔", "하박국",
    "스바냐", "학개", "스가랴", "말라기",
    "마태복음", "마가복음", "누가복음", "요한복음", "사도행전",
    "로마서", "고린도전서", "고린도후서", "갈라디아서", "에베소서",
    "빌립보서", "골로새서", "데살로니가전서", "데살로니가후서",
    "디모데전서", "디모데후서", "디도서", "빌레몬서", "히브리서",
    "야고보서", "베드로전서", "베드로후서", "요한일서", "요한이서",
    "요한삼서", "유다서", "요한계시록",
]  # 66 terms

DEFAULT_DEEPGRAM_KEYWORDS = _WORSHIP_TERMS + _BIBLE_BOOKS  # 96 total
```

### 2.2 Raise `DG_KEYWORDS_LIMIT` Default

**File**: `backend/app/deepgram_session.py`

```python
# BEFORE
DG_KEYWORDS_LIMIT = _int_env("DEEPGRAM_KEYWORDS_LIMIT", 60, min_value=0, max_value=200)

# AFTER
DG_KEYWORDS_LIMIT = _int_env("DEEPGRAM_KEYWORDS_LIMIT", 100, min_value=0, max_value=200)
```

---

## 3. Phase 2 — Per-Org Custom Keyterms

### 3.1 `InMemoryMultiChurchStore` — New Methods

**File**: `backend/app/services/multichurch_store.py` (InMemoryMultiChurchStore class, ~line 1700+)

Add after `set_org_prompt`:

```python
STT_KEYTERMS_ROLES = {"owner", "admin"}
STT_KEYTERMS_MAX = 50  # org custom slots budget

def get_org_stt_keyterms(self, *, org_id: str, requested_by_uid: str) -> Dict[str, Any]:
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
            "keyterms": list(org.get("sttKeyterms") or []),
        }

def set_org_stt_keyterms(
    self,
    *,
    org_id: str,
    requested_by_uid: str,
    keyterms: List[str],
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
        cleaned = [t.strip() for t in keyterms if t.strip()][:STT_KEYTERMS_MAX]
        org["sttKeyterms"] = cleaned
        org["updatedAt"] = _utcnow()
        return {"orgId": clean_org_id, "keyterms": cleaned}

def get_org_stt_keyterms_for_session(self, org_id: Optional[str]) -> List[str]:
    """Called at session start — no auth check, returns list directly."""
    clean_org_id = _clean_token(org_id)
    if not clean_org_id:
        return []
    with self._lock:
        org = self._orgs.get(clean_org_id)
        if not org:
            return []
        return list(org.get("sttKeyterms") or [])
```

### 3.2 `FirestoreMultiChurchStore` — New Methods

**File**: `backend/app/services/multichurch_store.py` (FirestoreMultiChurchStore class, ~line 4400+)

Add after `set_org_prompt` (line ~4447):

```python
def get_org_stt_keyterms(self, *, org_id: str, requested_by_uid: str) -> Dict[str, Any]:
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
        "keyterms": list(org.get("sttKeyterms") or []),
    }

def set_org_stt_keyterms(
    self,
    *,
    org_id: str,
    requested_by_uid: str,
    keyterms: List[str],
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
    cleaned = [t.strip() for t in keyterms if t.strip()][:STT_KEYTERMS_MAX]
    org_ref.set(
        {"sttKeyterms": cleaned, "updatedAt": gcf_firestore.SERVER_TIMESTAMP},
        merge=True,
    )
    return {"orgId": clean_org_id, "keyterms": cleaned}

def get_org_stt_keyterms_for_session(self, org_id: Optional[str]) -> List[str]:
    """Called at session start — no auth check, returns list directly."""
    clean_org_id = _clean_token(org_id)
    if not clean_org_id:
        return []
    org_snap = self._org_ref(clean_org_id).get()
    if not org_snap.exists:
        return []
    org = org_snap.to_dict() or {}
    return list(org.get("sttKeyterms") or [])
```

### 3.3 New Route — `backend/app/routes/stt_keyterms.py`

Create a new file following the same pattern as `prompt.py`:

```python
from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from typing import List

from app import validators
from app.auth.guards import require_org_role
from app.auth.firebase_auth import AuthenticatedUser, get_current_user_required
from app.services.multichurch_store import multichurch_store

router = APIRouter(tags=["stt"])
STT_KEYTERMS_ROLES = {"owner", "admin"}


class SttKeytermsPayload(BaseModel):
    keyterms: List[str] = Field(default_factory=list, max_length=50)


@router.get("/org/{org_id}/stt-keyterms")
def get_org_stt_keyterms(
    org_id: str = Path(pattern=validators.ORG_ID),
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(
        org_id=org_id, user=user, roles=STT_KEYTERMS_ROLES, store=multichurch_store,
        missing_membership_detail="forbidden",
    )
    try:
        return multichurch_store.get_org_stt_keyterms(
            org_id=org_id, requested_by_uid=user.uid
        )
    except ValueError as exc:
        raise HTTPException(status_code=404 if str(exc) == "org_not_found" else 400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc) or "forbidden") from exc


@router.put("/org/{org_id}/stt-keyterms")
def set_org_stt_keyterms(
    *,
    org_id: str = Path(pattern=validators.ORG_ID),
    payload: SttKeytermsPayload,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(
        org_id=org_id, user=user, roles=STT_KEYTERMS_ROLES, store=multichurch_store,
        missing_membership_detail="forbidden",
    )
    try:
        return multichurch_store.set_org_stt_keyterms(
            org_id=org_id, requested_by_uid=user.uid, keyterms=payload.keyterms
        )
    except ValueError as exc:
        raise HTTPException(status_code=404 if str(exc) == "org_not_found" else 400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc) or "forbidden") from exc
```

### 3.4 Mount the Router

**File**: `backend/app/main.py` (router registration section)

```python
# Find where other routers are included, e.g.:
# app.include_router(prompt.router, prefix="/api")
# Add:
from app.routes import stt_keyterms
app.include_router(stt_keyterms.router, prefix="/api")
```

---

## 4. Phase 3 — Sermon Script Vocab → Deepgram

### 4.1 `_build_keyterm_list()` Helper

**File**: `backend/app/deepgram_session.py`

Add a new helper that accepts all three tiers and merges them:

```python
def _build_keyterm_list(
    org_custom: Optional[List[str]] = None,
    sermon_vocab: Optional[List[str]] = None,
    limit: int = 100,
) -> List[str]:
    """
    Merge keyterm tiers (priority: org_custom > sermon_vocab > defaults).
    Returns deduplicated list up to `limit` items.
    """
    seen: set[str] = set()
    result: List[str] = []

    def _add(terms: List[str]) -> None:
        for t in terms:
            t = t.strip()
            if not t:
                continue
            key = t.lower()
            if key not in seen and len(result) < limit:
                seen.add(key)
                result.append(t)

    _add(org_custom or [])
    _add(sermon_vocab or [])
    _add(_current_keywords())  # Tier 3: defaults

    return result
```

### 4.2 Session Start — Merge All Tiers

**File**: `backend/app/main.py` — `ws_stt_deepgram()` handler

Current code (~line 1360–1403):
```python
dg_keywords = None if src_lang.startswith("ko") else []
# ...
dg = await connect_to_deepgram(language=dg_language, keywords=dg_keywords)
```

Replace with:
```python
# Build merged keyterm list from all 3 tiers (Korean only)
if src_lang.startswith("ko"):
    _org_keyterms = multichurch_store.get_org_stt_keyterms_for_session(org_id)
    _sermon_vocab = list(script_store.get_vocab_set(room_id=room_id)) if room_id else []
    from app.deepgram_session import _build_keyterm_list
    dg_keywords = _build_keyterm_list(
        org_custom=_org_keyterms,
        sermon_vocab=_sermon_vocab,
    )
    if DG_DEBUG:
        print(f"[DG] keyterms: {len(dg_keywords)} total "
              f"(org={len(_org_keyterms)}, sermon={len(_sermon_vocab)})")
else:
    dg_keywords = []
# ...
dg = await connect_to_deepgram(language=dg_language, keywords=dg_keywords)
```

**Import needed** at top of main.py:
```python
from app.deepgram_session import DG_DEBUG  # add if not already imported
```

---

## 5. Phase 4 — Frontend Admin UI

### 5.1 API Client

**File**: `frontend/lib/api/sttKeyterms.ts` (new file)

```typescript
import { getApiBase } from "@/utils/urls";

export async function getSttKeyterms(orgId: string, idToken: string) {
  const res = await fetch(`${getApiBase()}/api/org/${orgId}/stt-keyterms`, {
    headers: { Authorization: `Bearer ${idToken}` },
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json() as Promise<{ orgId: string; keyterms: string[] }>;
}

export async function setSttKeyterms(
  orgId: string,
  keyterms: string[],
  idToken: string
) {
  const res = await fetch(`${getApiBase()}/api/org/${orgId}/stt-keyterms`, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${idToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ keyterms }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json() as Promise<{ orgId: string; keyterms: string[] }>;
}
```

### 5.2 STT Keywords Component

**File**: `frontend/components/SttKeytermsEditor.tsx` (new file)

UI structure:
- Section header: "STT Keywords" with slot usage counter (`{total} / 100 active`)
- Read-only tier display:
  - "Church Custom" — editable list, owner/admin only
  - "Sermon Script" — read-only note ("Loaded automatically from sermon prep")
  - "Default (Church + Bible)" — collapsed, 96 terms
- Add term: text input + Add button
- Remove term: × button on each tag
- Save button triggers `PUT /api/org/{orgId}/stt-keyterms`
- Warning banner if `custom.length > 50`

### 5.3 Integration Point

**File**: `frontend/pages/host/c/[churchSlug].tsx` (already modified per git status)

Add `<SttKeytermsEditor>` to the settings/admin section of the host console. Render only when `userRole` is `owner` or `admin`.

---

## 6. Firestore Rules

No client-side write access needed — all writes go through the backend API (Admin SDK bypasses rules). The existing `organizations/{orgId}` read rule already allows org members to read the document, so `sttKeyterms` field will be visible. No rule change required.

However, note the `sttKeyterms` field should be excluded from direct client writes — the current rule `allow create, update, delete: if false` already enforces this.

---

## 7. Data Constraints

| Constraint | Value | Enforcement |
|---|---|---|
| Max org custom keyterms | 50 | `set_org_stt_keyterms()` slices to 50 |
| Max total keyterms sent to Deepgram | 100 (nova-3) | `DG_KEYWORDS_LIMIT` env var |
| Min term length | 1 char (after strip) | `_build_keyterm_list()` filter |
| Sermon vocab filter | 2–8 char Hangul tokens | `script_store.get_vocab_set()` (existing) |
| API payload | `keyterms: string[]` | Pydantic `max_length=50` on the list |

---

## 8. Files Changed Summary

| File | Type | Change |
|---|---|---|
| `backend/app/config/deepgram_keywords.py` | Modify | Remove boosts, split into `_WORSHIP_TERMS` + `_BIBLE_BOOKS` |
| `backend/app/deepgram_session.py` | Modify | Raise default limit 60→100, add `_build_keyterm_list()` |
| `backend/app/services/multichurch_store.py` | Modify | Add 3 methods to both `InMemory` and `Firestore` classes |
| `backend/app/routes/stt_keyterms.py` | New | `GET/PUT /api/org/{orgId}/stt-keyterms` |
| `backend/app/main.py` | Modify | Mount router, merge 3 tiers before `connect_to_deepgram()` |
| `frontend/lib/api/sttKeyterms.ts` | New | API client functions |
| `frontend/components/SttKeytermsEditor.tsx` | New | STT keywords management UI component |
| `frontend/pages/host/c/[churchSlug].tsx` | Modify | Add `<SttKeytermsEditor>` to settings section |

---

## 9. Implementation Order

1. **`deepgram_keywords.py`** — Config cleanup (isolated, no dependencies)
2. **`deepgram_session.py`** — Raise limit + `_build_keyterm_list()` helper
3. **`multichurch_store.py`** — Add 3 new methods to both classes
4. **`stt_keyterms.py`** — New route (depends on store methods)
5. **`main.py`** — Mount router + merge tiers at session start
6. **Frontend** — API client → Component → Page integration
