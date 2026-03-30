# sermon-service-isolation Design Document

> **Summary**: Isolate pre-uploaded sermon scripts per service slot and date so that multiple Sunday services can each carry their own frozen translation context without overwriting each other.
>
> **Project**: Real-Time Translation Platform
> **Version**: 1.0
> **Author**: namju
> **Date**: 2026-03-27
> **Status**: Draft
> **Planning Doc**: N/A (design derived from architecture discussion)

---

## Executive Summary

| Dimension | Detail |
|-----------|--------|
| **Problem** | `script_store` is keyed by `org_id` only. Finalizing a sermon for the 11 AM service while 9 AM is running overwrites the active translation context, and `_try_reload_sermon` loads "latest for org" with no date or service awareness. |
| **Solution** | Key the runtime script store by `room_id` (frozen on room start). Persist sermons under `services/{serviceKey}/sermons/{serviceDate}` in Firestore with a `draft → published` lifecycle. Load the exact published snapshot when a room opens. |
| **Functional UX Effect** | Admin prepares sermons days in advance per service slot. Multiple Sunday services run in parallel with zero cross-contamination. Mid-service edits do not affect an already-running room. |
| **Core Value** | Reliable, isolated translation context per service instance — correct sermon text is always loaded for the right service, even when multiple services share the same organization. |

---

## 1. Overview

### 1.1 Design Goals

- Eliminate cross-service sermon overwrite: each service slot + date gets its own independent script store entry at runtime
- Allow sermon prep several days before worship — sermon is frozen at publish time, not finalize time
- Make room startup deterministic: room always loads the published sermon for its `serviceKey + serviceDate`
- Preserve backward compatibility: orgs with a single service continue to work via fallback to org-latest

### 1.2 Design Principles

- **Room owns its translation context**: use `room_id` as the runtime script store key — complete isolation, automatic scope
- **Firestore is source of truth**: in-memory store is a cache loaded from Firestore; any backend instance can reconstruct it
- **Immutable once published**: a published sermon snapshot is never overwritten — only a new date entry can replace it
- **Opt-in service binding**: `service_key` + `service_date` are required for new sermon prep; org-global fallback exists only for legacy orgs

---

## 2. Architecture

### 2.1 Component Diagram

```
┌──────────────────────────────────────────────────────────────┐
│  Admin UI (SermonPrep.tsx)                                    │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────┐ │
│  │ Org Selector│  │ Svc Selector │  │ Date Picker         │ │
│  └──────┬──────┘  └──────┬───────┘  └──────────┬──────────┘ │
│         └────────────────┴─────────────────────┘            │
│                  ▼ POST /sermon/draft                        │
│                  ▼ PUT  /sermon/{date}                       │
│                  ▼ POST /sermon/{date}/publish               │
└──────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│  Backend (routes/script.py)                                  │
│                                                              │
│  draft    → generate AI translation, return segments        │
│  save     → persist to Firestore (status: draft)            │
│  publish  → freeze snapshot (status: published)             │
│           → script_store.load(pairs, room_id=None)          │
│             [eager pre-warm for service_key::date key]      │
└──────────────────────────────────────────────────────────────┘
              │ Firestore
              ▼
┌─────────────────────────────────────────────────────────────┐
│  organizations/{orgId}/                                      │
│    services/{serviceKey}/                                    │
│      publishedSermonDate: "2026-03-29"   ← pointer          │
│      sermons/{serviceDate}/              ← subcollection     │
│        status: "draft" | "published"                        │
│        pairs: [{source, target}]                            │
│        threshold: 0.84                                      │
│        publishedAt: timestamp | null                        │
└──────────────────────────────────────────────────────────────┘
              │
              ▼ on room start
┌─────────────────────────────────────────────────────────────┐
│  main.py — start_service()                                   │
│                                                              │
│  1. serviceDate = today (YYYY-MM-DD)                        │
│  2. doc = get_published_sermon(orgId, serviceKey, date)     │
│  3. script_store.load(doc.pairs, room_id=roomId)  ← FREEZE │
│  4. room.sermonDate = date  (immutable)                     │
└──────────────────────────────────────────────────────────────┘
              │
              ▼ during live translation (ws/stt_deepgram, ws/translate)
┌─────────────────────────────────────────────────────────────┐
│  script_store.match_with_examples(text, room_id=roomId)     │
│  → hits buffer["room::roomId"]                              │
│  → completely independent per concurrent room               │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 Script Store Key Evolution

```
Current:   buffer[org_id]
New:       buffer["room::{room_id}"]       ← runtime (WebSocket)
           buffer["{org}::{svc}::{date}"]  ← publish pre-warm (optional)
           buffer[org_id]                  ← legacy fallback (single-service orgs)
```

### 2.3 Dependencies

| Component | Depends On | Purpose |
|-----------|-----------|---------|
| `routes/script.py` | `multichurch_store` | Persist/read sermon subcollection |
| `routes/script.py` | `script_store` | Pre-warm memory on publish |
| `main.py` start_service | `multichurch_store` | Load published sermon at room open |
| `main.py` start_service | `script_store` | Freeze pairs into room-keyed buffer |
| `main.py` WS handlers | `script_store` | Match using room_id key |
| `SermonPrep.tsx` | `/org/{id}/services` | List service slots for selector |

---

## 3. Data Model

### 3.1 Firestore Schema

#### `organizations/{orgId}/services/{serviceKey}` (existing, extended)

```
title: "Sunday 9 AM"
timezone: "America/Chicago"
defaultLanguagePair: {source: "ko", target: "en"}
publishedSermonDate: "2026-03-29"     ← NEW: pointer to active published sermon
activeRoomId: string | null
lastRoomId: string | null
updatedAt: timestamp
```

#### `organizations/{orgId}/services/{serviceKey}/sermons/{serviceDate}` (NEW subcollection)

```
serviceKey: "sun-9am"                 ← denormalized for query
serviceDate: "2026-03-29"             ← matches document ID
status: "draft" | "published"
pairs: [
  {source: "한국어 문장", target: "English sentence"},
  ...
]
threshold: 0.84
langSrc: "ko"
langTgt: "en"
publishedAt: Timestamp | null         ← null until published
createdBy: uid
createdAt: Timestamp
updatedAt: Timestamp
segmentCount: number                  ← denormalized for listing
```

**Why date as document ID:**
- Natural dedup: one sermon per slot per date (no duplicates possible)
- Predictable path: `services/sun-9am/sermons/2026-03-29`
- Simple cleanup: delete docs older than N weeks

#### `organizations/{orgId}/rooms/{roomId}` (existing, extended)

```
serviceKey: "sun-9am"                 ← existing
sermonDate: "2026-03-29"              ← NEW: frozen at room creation
sermonLoadedAt: Timestamp             ← NEW: audit trail (null if no sermon found)
# all other fields unchanged
```

### 3.2 Entity Relationships

```
Organization 1 ──── N  Service (slot definition)
                          │
                          └── 1 ──── N  SermonDoc (date-keyed snapshot)
                                           │
                          ┌────────────────┘
Service 1 ──── N  Room (instance)
                    │
                    └── 0..1 SermonDoc (loaded by serviceDate at room start)
```

### 3.3 Script Store Runtime State

```python
@dataclass
class ScriptBuffer:
    pairs: List[ScriptPair]
    threshold: float = 0.84
    version: int = 0
    sermons: Dict[str, dict]  # sermon_id → payload (existing)

# Key patterns in _buffers dict:
# "room::{roomId}"              → frozen per live room
# "{orgId}::{svcKey}::{date}"   → pre-warmed on publish
# "{orgId}"                     → legacy / single-service fallback
```

---

## 4. API Specification

### 4.1 Endpoint List

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| POST | `/org/{orgId}/services/{serviceKey}/sermon/draft` | Generate AI draft from Korean text | host/admin/owner |
| GET | `/org/{orgId}/services/{serviceKey}/sermon/{serviceDate}` | Get draft or published sermon | host/admin/owner |
| PUT | `/org/{orgId}/services/{serviceKey}/sermon/{serviceDate}` | Save edited segments (status: draft) | host/admin/owner |
| POST | `/org/{orgId}/services/{serviceKey}/sermon/{serviceDate}/publish` | Freeze sermon snapshot | host/admin/owner |
| DELETE | `/org/{orgId}/services/{serviceKey}/sermon/{serviceDate}/publish` | Unpublish (only if no room started) | owner/admin |
| GET | `/org/{orgId}/services/{serviceKey}/sermons` | List sermons for a service slot | host/admin/owner |

**Legacy endpoints kept (with deprecation header):**
- `POST /org/{orgId}/sermon/draft` → still works, no service_key binding
- `POST /org/{orgId}/sermon/finalize` → still works, loads into org-level key

### 4.2 Detailed Specification

#### `POST /org/{orgId}/services/{serviceKey}/sermon/draft`

**Request:**
```json
{
  "serviceDate": "2026-03-29",
  "koreanText": "한국어 설교 전문...",
  "threshold": 0.84,
  "langSrc": "ko",
  "langTgt": "en",
  "autoSplit": true
}
```

**Response (200 OK):**
```json
{
  "serviceKey": "sun-9am",
  "serviceDate": "2026-03-29",
  "status": "draft",
  "segments": [
    {"id": 1, "ko": "오늘 말씀은...", "en": "Today's message is..."},
    {"id": 2, "ko": "예수님께서 말씀하시기를", "en": "Jesus said"}
  ],
  "usage": {"promptTokens": 1200, "completionTokens": 800, "costUsd": 0.0012}
}
```

#### `PUT /org/{orgId}/services/{serviceKey}/sermon/{serviceDate}`

**Request:**
```json
{
  "segments": [
    {"id": 1, "ko": "오늘 말씀은...", "en": "Today's message is..."},
    {"id": 2, "ko": "예수님께서 말씀하시기를", "en": "As Jesus said"}
  ],
  "threshold": 0.84
}
```

**Response (200 OK):**
```json
{
  "status": "draft",
  "segmentCount": 2,
  "updatedAt": "2026-03-27T10:00:00Z"
}
```

#### `POST /org/{orgId}/services/{serviceKey}/sermon/{serviceDate}/publish`

**Request:** `{}` (empty body)

**Response (200 OK):**
```json
{
  "status": "published",
  "serviceKey": "sun-9am",
  "serviceDate": "2026-03-29",
  "segmentCount": 45,
  "publishedAt": "2026-03-27T10:30:00Z",
  "preWarmed": true
}
```

**Errors:**
- `400` — no draft exists for this serviceKey + serviceDate
- `400` — draft has empty segments (not all rows filled)
- `409` — already published (must unpublish first)

**Side effects:**
1. Sets `status = "published"`, `publishedAt = now()` in Firestore
2. Sets `service.publishedSermonDate = serviceDate`
3. Pre-warms `script_store` with key `{orgId}::{serviceKey}::{serviceDate}` (so room start is instant)

#### `DELETE /org/{orgId}/services/{serviceKey}/sermon/{serviceDate}/publish`

**Errors:**
- `409` — a room using this sermon is currently live (cannot unpublish mid-service)

---

## 5. UI/UX Design

### 5.1 SermonPrep Flow (updated)

```
Step 0 (NEW): Select Service
┌─────────────────────────────────────────┐
│  Church: [Grace Church ▼]               │
│  Service: [Sunday 9 AM ▼]               │
│  Date: [2026-03-29  📅]                 │
│                           [Next →]      │
└─────────────────────────────────────────┘

Step 1: Paste Korean Text  (unchanged)
Step 2: Generate Draft     (unchanged, but calls new endpoint)
Step 3: Edit Segments      (unchanged)

Step 4 (CHANGED): Save & Publish
┌─────────────────────────────────────────┐
│  [Save Draft]   [✓ Publish for Service] │
│                                          │
│  ⚠  Publishing freezes this sermon for  │
│     Sunday 9 AM on 2026-03-29.          │
│     It cannot be changed once the       │
│     service starts.                     │
└─────────────────────────────────────────┘
```

**"Save Draft"** — persists edits, status stays `draft`, no memory load
**"Publish for Service"** — freezes snapshot, pre-warms memory, sets service pointer

### 5.2 Service Selector Component

```tsx
// New component added before Step 1 in SermonPrep.tsx
interface ServiceSelectorProps {
  orgId: string
  onSelect: (serviceKey: string, serviceDate: string) => void
}
// Fetches GET /org/{orgId}/services → populates dropdown
// Date picker defaults to next occurrence of the service's day-of-week
```

### 5.3 Component Changes

| Component | Change |
|-----------|--------|
| `SermonPrep.tsx` | Add `serviceKey` + `serviceDate` state; add ServiceSelector step; split "Finalize" into "Save Draft" + "Publish" |
| `pages/admin/sermon-prep.tsx` | Pass `serviceKey`, `serviceDate` state to `SermonPrep` |
| `lib/api/sermon.ts` (new or existing) | Add `draftServiceSermon`, `saveServiceSermon`, `publishServiceSermon` API calls |

---

## 6. Backend Implementation Details

### 6.1 `script_store.py` Changes

```python
def _org_key(
    self,
    org_id: Optional[str] = None,
    *,
    room_id: Optional[str] = None,
    service_key: Optional[str] = None,
    service_date: Optional[str] = None,
) -> str:
    # Priority 1: room_id (runtime isolation — most specific)
    if room_id:
        return f"room::{room_id.strip()}"
    # Priority 2: service + date (pre-warm on publish)
    org = (org_id or "").strip() or self._GLOBAL_KEY
    svc = (service_key or "").strip()
    date = (service_date or "").strip()
    if svc and date:
        return f"{org}::{svc}::{date}"
    if svc:
        return f"{org}::{svc}"
    # Priority 3: org-only (legacy fallback)
    return org
```

All public methods (`load`, `clear`, `stats`, `match`, `match_with_examples`,
`get_keyword_glossary`, `get_vocab_set`) get `room_id=None` kwarg, passed to `_org_key`.

### 6.2 `multichurch_store.py` — New Methods

```python
def save_sermon_draft(
    self, org_id: str, service_key: str, service_date: str,
    pairs: list[dict], threshold: float,
    lang_src: str = "ko", lang_tgt: str = "en",
    created_by: str = "",
) -> None:
    # Writes to: organizations/{orgId}/services/{serviceKey}/sermons/{serviceDate}
    # status = "draft"

def get_sermon_draft(
    self, org_id: str, service_key: str, service_date: str
) -> dict | None:
    # Reads from the subcollection doc

def publish_sermon(
    self, org_id: str, service_key: str, service_date: str
) -> dict:
    # Sets status="published", publishedAt=now()
    # Sets service.publishedSermonDate = service_date (on parent doc)
    # Returns the full doc

def get_published_sermon(
    self, org_id: str, service_key: str, service_date: str
) -> dict | None:
    # Reads subcollection doc, checks status == "published"

def list_sermon_drafts(
    self, org_id: str, service_key: str, limit: int = 10
) -> list[dict]:
    # Lists sermons for a service slot, ordered by serviceDate desc
```

### 6.3 `main.py` — `_try_reload_sermon` (updated)

```python
def _try_reload_sermon(
    org_id: str,
    *,
    room_id: Optional[str] = None,
    service_key: Optional[str] = None,
    service_date: Optional[str] = None,
) -> bool:
    """
    Load the correct sermon into script_store for this room.
    Returns True if pairs were loaded.
    """
    try:
        # Check if already loaded for this room
        if room_id:
            count, _, _ = script_store.stats(room_id=room_id)
            if count > 0:
                return True

        # 1. Try service-specific published sermon
        doc = None
        if service_key and service_date:
            doc = multichurch_store.get_published_sermon(
                org_id, service_key, service_date
            )

        # 2. Fall back to org-latest (legacy orgs)
        if not doc:
            doc = multichurch_store.get_latest_sermon_pairs(org_id)

        if not doc or not doc.get("pairs"):
            return False

        script_store.load(
            doc["pairs"],
            doc.get("threshold"),
            room_id=room_id,  # freeze to room
        )
        return True
    except Exception:
        return False
```

### 6.4 `main.py` — Room Start (updated)

```python
# Inside start_service() / create_room():
service_date = datetime.now(service_tz).strftime("%Y-%m-%d")
sermon_loaded = _try_reload_sermon(
    org_id,
    room_id=room_id,
    service_key=service_key,
    service_date=service_date,
)
# Persist sermonDate on room doc
multichurch_store.update_room(room_id, {
    "sermonDate": service_date,
    "sermonLoadedAt": datetime.utcnow() if sermon_loaded else None,
})
```

### 6.5 `main.py` — WebSocket Translation (updated)

```python
# Both /ws/stt_deepgram and /ws/translate:
# Replace all script_store calls:
# BEFORE: script_store.match_with_examples(text, org_id=org_id)
# AFTER:  script_store.match_with_examples(text, room_id=room_id)

# Also update:
# script_store.stats(room_id=room_id)
# script_store.get_keyword_glossary(room_id=room_id)
# script_store.get_vocab_set(room_id=room_id)
```

Room-keyed calls will fall through to empty buffer (and thus to live AI translation)
if no sermon was loaded — safe, no exception.

### 6.6 Room End — Cleanup

```python
# Inside end_room():
script_store.clear(room_id=room_id)
```

Frees memory when service ends. Pre-warm entry (`{org}::{svc}::{date}`) stays in memory
until the next publish or server restart — low cost, avoids re-loading for re-starts.

---

## 7. Error Handling

| Code | Scenario | Handling |
|------|----------|----------|
| 400 | `serviceDate` format invalid (not YYYY-MM-DD) | Return validation error |
| 400 | Publish attempted with incomplete segments | Return list of empty rows |
| 404 | No draft found for serviceKey + serviceDate | Return 404, prompt to run /draft first |
| 409 | Publish when already published | Return current publishedAt, suggest unpublish first |
| 409 | Unpublish when live room is using this sermon | Return `roomId` of blocking room |
| 503 | Firestore unavailable during room start | Room starts anyway; sermonLoaded=false, falls back to live AI |

---

## 8. Test Plan

### 8.1 Test Scenarios

| # | Scenario | Expected |
|---|----------|----------|
| T1 | Draft + publish for `sun-9am / 2026-03-29` | `script_store["org::sun-9am::2026-03-29"]` pre-warmed |
| T2 | Start room for `sun-9am` on 2026-03-29 | `script_store["room::{roomId}"]` loaded from published sermon |
| T3 | Start room for `sun-11am` same day with different sermon | Independent buffer, no cross-contamination |
| T4 | Publish `sun-11am` sermon while `sun-9am` room is live | 9 AM buffer unaffected |
| T5 | Room end | `script_store.clear(room_id=roomId)` frees memory |
| T6 | Org has no published sermon for today | Room starts, falls back to org-latest, then live AI |
| T7 | Re-connect to same room (server restart) | `_try_reload_sermon` reloads from Firestore via room.sermonDate |
| T8 | Legacy org with no service_key | Falls back to org-level key, existing behavior preserved |

### 8.2 Verification Method (Zero-Script QA)

Monitor Docker logs for these patterns after each scenario:

```
# T2 success:
INFO sermon loaded for room room_abc123 (45 pairs, service=sun-9am, date=2026-03-29)

# T3 isolation:
INFO sermon loaded for room room_def456 (38 pairs, service=sun-11am, date=2026-03-29)

# T4 no cross-contamination:
DEBUG script_store match room_abc123: score=0.94 mode=pre   ← 9 AM still working

# T6 fallback:
WARN no published sermon for sun-9am/2026-03-29, falling back to org-latest
```

---

## 9. Implementation Order

### Phase 1 — Script store: add `room_id` key (isolated, no behavior change for existing code)
1. [ ] `backend/app/services/script_store.py` — update `_org_key` + all public method signatures
2. [ ] Manual test: `script_store.load(pairs, room_id="test-room")` → creates isolated buffer

### Phase 2 — Firestore: sermon subcollection
3. [ ] `backend/app/services/multichurch_store.py` — add 5 new sermon methods
4. [ ] `backend/firestore/firestore.rules` — add read/write rules for `services/{svcKey}/sermons/{date}`

### Phase 3 — New API routes
5. [ ] `backend/app/routes/script.py` — add new endpoints (draft, save, publish, unpublish, list)
6. [ ] Mount new router prefix or extend existing `/org/{orgId}/services/{serviceKey}/sermon/*`

### Phase 4 — Room start integration
7. [ ] `backend/app/main.py` — update `_try_reload_sermon` signature + body
8. [ ] `backend/app/main.py` — update room start to call `_try_reload_sermon(room_id=...)` + persist `sermonDate`
9. [ ] `backend/app/main.py` — update all `script_store.*` calls in WS handlers to use `room_id`
10. [ ] `backend/app/main.py` — update room end to call `script_store.clear(room_id=...)`

### Phase 5 — Frontend
11. [ ] `frontend/components/SermonPrep.tsx` — add ServiceSelector step, split Save/Publish
12. [ ] `frontend/pages/admin/sermon-prep.tsx` — add `serviceKey` + `serviceDate` state
13. [ ] API layer — add `draftServiceSermon`, `saveServiceSermon`, `publishServiceSermon` functions

---

## 10. Migration Notes

- Existing orgs with no `service_key` on sermon docs continue to use org-level `script_store` key
- `_try_reload_sermon` falls back to `get_latest_sermon_pairs(org_id)` when no service-specific sermon found
- Legacy endpoints (`POST /org/{orgId}/sermon/draft`, `POST /org/{orgId}/sermon/finalize`) remain active, load into org-level key
- No Firestore migration needed — existing sermon docs under `organizations/{orgId}/sermons/` are untouched
- Deprecation: legacy endpoints log `X-Deprecated: use /services/{serviceKey}/sermon/*` response header

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-03-27 | Initial design | namju |
