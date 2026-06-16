# editing-sermon Design Document

> **Summary**: Bidirectional `.xlsx` round-trip for reviewing sermon translations outside the app, with stable Segment ID matching, atomic import validation, and broadcast integration.
>
> **Project**: Real-Time Translation Platform
> **Version**: 0.1
> **Author**: namju
> **Date**: 2026-06-16
> **Status**: Draft
> **Planning Doc**: [editing-sermon.plan.md](../../01-plan/features/editing-sermon.plan.md)

---

## Context Anchor

> Copied from Plan. Ensures strategic context survives Design → Do handoff.

| Key | Value |
|-----|-------|
| **WHY** | In-app bulk editing of sermon translations is impractical; pastors need a familiar tool (Google Sheets) to review and correct machine output efficiently. |
| **WHO** | Org-level `owner`, `admin`, `host` roles preparing a sermon for an upcoming service. Listeners and `viewer` role are not affected by this feature. |
| **RISK** | Re-import data corruption — shifted rows, deleted Segment IDs, or wrong sermon attached could silently overwrite correct translations. Validation must catch this before any write. |
| **SUCCESS** | (1) Host can complete export → edit → import round-trip with zero data loss, (2) every malformed file blocks import with row-level errors, (3) reviewed translations used during broadcast in preference to machine output. |
| **SCOPE** | MVP: 3 ingestion paths (Google Docs URL + `.txt`/`.docx` upload + paste), `.xlsx` export, validated `.xlsx` import, broadcast integration. Post-MVP: translation memory, glossary suggestions, Google Sheets API live sync. |

---

## 1. Overview

### 1.1 Design Goals

- **Atomicity**: import is all-or-nothing at the sermon-document level. No partial writes possible.
- **Cost-bounded**: 1 Firestore read per export, 1 write per import. No per-segment reads or writes anywhere in the system.
- **Additive**: no edits to `SermonPrep.tsx`, `services/{serviceKey}` schema, or in-memory `script_store`. Existing live broadcast path regresses to zero when no sermon is linked.
- **Stable matching**: server-generated `Segment ID` is the only key used to match rows on re-import. Format, order, and ID generation rules are locked.
- **Pluggable ingestion without ABC**: four ingestion functions share an output shape (`Segment[]`) but are not subclasses of a common base — keeps code small until a 5th source justifies the abstraction.
- **One translation-pipeline hook**: a single function `get_reviewed_text()` is consulted before the existing machine-output fallback. No refactor of `translate.py`.

### 1.2 Design Principles

- Selected architecture: **Option C — Pragmatic Balance**. See §2.0.
- Cheap path = correct path. The Firestore layout, the openpyxl in-memory generation, and the single-call broadcast hook are the cheapest implementations that satisfy requirements; we do not pre-optimize for scenarios the Plan explicitly defers to post-MVP.
- Strict server-side authority. The frontend renders state and forwards files; all validation, sermon ID embedding, Segment ID generation, and RBAC checks happen in the backend.
- Backwards compatibility through additive schema. New Firestore subcollection, new optional field on `services`, no schema migration required for existing data.

---

## 2. Architecture Options

### 2.0 Architecture Comparison

| Criteria | A: Minimal | B: Clean | C: Pragmatic |
|----------|:-:|:-:|:-:|
| **New Files** | ~2 | ~15 | ~10 |
| **Modified Files** | 1 large (SermonPrep.tsx + service schema) | 5+ | 1 small (`translate.py`, +1 call site) |
| **Complexity** | Low | High | Medium |
| **Maintainability** | Medium | High | High |
| **Effort** | Low (~2 sessions) | High (~6 sessions) | Medium (~3–4 sessions) |
| **Risk** | Medium (production SermonPrep grows) | High (refactor + schema migration on a production SaaS) | Low (additive only) |
| **Honors Plan locked decisions** | No (storage tied to service) | Yes | Yes |
| **Recommendation** | Quick wins only | Long-term greenfield | **Selected default** |

**Selected**: **Option C — Pragmatic Balance**
**Rationale**: Honors Plan's locked sermon-per-org + single-doc + 1-read/1-write decisions. Additive only, so existing live broadcast and host console paths have zero regression surface. Defers a `SermonPrep.tsx` cleanup to a separate intentional decision rather than coupling it to this feature.

### 2.1 Component Diagram

```
                       ┌──────────────────────────────────┐
                       │   Frontend (Next.js Pages Router)│
                       │                                  │
  Host/Admin/Owner ──▶ │  /admin/sermons/new      ─┐      │
                       │  /admin/sermons          ──┼──┐  │
                       │  /admin/sermons/[id]      ─┘   │ │
                       │                                │ │
                       │  SermonIngestForm  ┐           │ │
                       │  SermonReviewControls  ────────┼─┼──▶ Bearer auth
                       └────────────────────────────────┼─┼─┐
                                                        │ │ │
                                                        ▼ ▼ ▼
                       ┌──────────────────────────────────────────┐
                       │  Backend (FastAPI)                       │
                       │                                          │
                       │  routes/sermon_review.py                 │
                       │      POST /sermons/ingest                │
                       │      GET  /sermons/{id}                  │
                       │      GET  /sermons/{id}/review-file.xlsx │
                       │      POST /sermons/{id}/review-file      │
                       │      POST /sermons/{id}/link             │
                       │                                          │
                       │  sermon_review/                          │
                       │    ingest.py        (4 funcs)            │
                       │    xlsx_export.py   (openpyxl write)     │
                       │    xlsx_import.py   (openpyxl read)      │
                       │    validation.py    (row-level rules)    │
                       │    models.py        (Pydantic)           │
                       │                                          │
                       │  services/multichurch_store.py           │
                       │      +create_sermon, get_sermon,         │
                       │       update_sermon_reviews,             │
                       │       list_org_sermons, link_sermon      │
                       │                                          │
                       │  utils/translate.py                      │
                       │      +get_reviewed_text() [ONE call site]│
                       └─────────────┬────────────────────────────┘
                                     │
                                     ▼
                       ┌──────────────────────────────────┐
                       │  Firestore (worship-translation) │
                       │                                  │
                       │  organizations/{orgId}/          │
                       │    sermons/{sermonId}            │
                       │      └─ segments: [...]          │
                       │    services/{serviceKey}         │
                       │      └─ +linkedSermonId          │
                       └──────────────────────────────────┘

  External:
    Google Docs API (documents.readonly) ── via ingest_from_google_docs()
```

### 2.2 Data Flow

#### Ingest → Translate → Export

```
User picks source (Docs URL / .txt / .docx / paste)
   │
   ▼
POST /sermons/ingest  { sourceType, payload, title }
   │
   ▼
ingest.py → returns raw_text (string)
   │
   ▼
chunker/ (existing) → segments[] with order index
   │
   ▼
translator/ (existing OpenAI GPT-4o) → appTranslation per segment
   │
   ▼
build Sermon: assign Segment IDs (S001, S002, ...)
   │
   ▼
Firestore: write organizations/{orgId}/sermons/{sermonId}  ← 1 write
   │
   ▼
Return sermonId; redirect to /admin/sermons/[sermonId]
```

#### Export

```
GET /sermons/{id}/review-file.xlsx
   │
   ▼
multichurch_store.get_sermon(orgId, sermonId)  ← 1 read
   │
   ▼
xlsx_export.build_xlsx(sermon) → in-memory BytesIO (openpyxl)
   │
   ▼
StreamingResponse with Content-Disposition: attachment; filename=...
```

#### Import

```
POST /sermons/{id}/review-file (multipart/form-data, .xlsx)
   │
   ▼
xlsx_import.read_workbook(bytes) → rows[]
   │
   ▼
validation.validate(rows, sermon) → ValidationReport
   │
   ├─ has errors? ─▶ return 400 with structured report; NO WRITE
   │
   ▼
Firestore transaction:
  - get sermon (with updatedAt precondition)
  - overlay reviewedTranslation/notes/status onto segments[]
  - bump updatedAt
  - write sermon  ← 1 write
   │
   ▼
emit security_event("sermon_review_imported", actor, sermonId, counts)
   │
   ▼
Return 200 with ValidationReport including imported/warned counts
```

#### Live Broadcast

```
Listener WebSocket open → existing /ws/translate handler
   │
   ▼
Host audio → Deepgram STT → Korean transcript
   │
   ▼
existing translate_text(korean, service_context)
   │
   ▼
  NEW: get_reviewed_text(service.linkedSermonId, korean) ─┐
                                                          │
   ┌──── reviewed text found ──── return it ─────────────┘
   │
   └──── no match ──── existing machine-output path (unchanged)
```

### 2.3 Dependencies

| Component | Depends On | Purpose |
|-----------|-----------|---------|
| `routes/sermon_review.py` | `firebase_auth.get_current_user_required`, `multichurch_store`, `sermon_review/*` | HTTP boundary |
| `sermon_review/ingest.py` | `chunker/`, `translator/`, optional `google-api-python-client`, `python-docx` | Source ingestion |
| `sermon_review/xlsx_export.py` | `openpyxl` | Generate `.xlsx` |
| `sermon_review/xlsx_import.py` | `openpyxl` + `defusedxml` | Read `.xlsx` |
| `sermon_review/validation.py` | none (pure functions on `models.py` types) | Row-level rules |
| `utils/translate.py` | `multichurch_store.get_sermon` (cached) | Reviewed-text lookup hook |
| Frontend `SermonIngestForm` | `lib/authContext`, `utils/urls`, fetch | Ingestion UI |
| Frontend `SermonReviewControls` | same | Export/Import UI |

---

## 3. Data Model

### 3.1 Entity Definition

```python
# backend/app/sermon_review/models.py
from __future__ import annotations
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field

SegmentStatus = Literal["Draft", "Reviewed", "Skip"]
SourceType = Literal["google_docs", "file_txt", "file_docx", "paste"]

class Segment(BaseModel):
    segmentId: str             # "S001" .. "S{NNN}" — zero-padded width=3 minimum
    order: int                 # 1-based, dense, monotonic
    original: str              # Korean source text
    appTranslation: str        # GPT-4o initial translation
    reviewedTranslation: str   # default == appTranslation
    notes: str = ""
    status: SegmentStatus = "Draft"

class Sermon(BaseModel):
    sermonId: str              # auto-generated by Firestore
    orgId: str
    title: str
    sourceType: SourceType
    sourceRef: str | None      # e.g. Google Docs URL or original filename
    segments: list[Segment]
    createdBy: str             # uid
    createdAt: datetime
    updatedAt: datetime        # used as precondition for concurrent imports
```

### 3.2 Entity Relationships

```
Organization 1 ── N Sermon
    │
    └── 1 ── N Service ── 0..1 Sermon (via linkedSermonId)

A Sermon may be linked to 0 or 1 Service at a time.
Unlinking is supported; linking a different sermon replaces.
```

### 3.3 Firestore Schema

#### `organizations/{orgId}/sermons/{sermonId}`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | yes | User-supplied title |
| `sourceType` | string | yes | `google_docs` / `file_txt` / `file_docx` / `paste` |
| `sourceRef` | string \| null | no | URL or original filename (no full content stored — privacy + size) |
| `segments` | array | yes | See Segment shape below — bounded by `SERMON_MAX_SEGMENTS` (default 1000) |
| `createdBy` | string | yes | Firebase Auth `uid` |
| `createdAt` | timestamp | yes | Server timestamp |
| `updatedAt` | timestamp | yes | Server timestamp; precondition for concurrent imports |

Segment array element:

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `segmentId` | string | yes | `S{n:03d}` minimum; widens to 4 digits at ≥1000 |
| `order` | int | yes | 1-based dense index, also encoded into Segment ID |
| `original` | string | yes | Trimmed; non-empty |
| `appTranslation` | string | yes | Trimmed; may be empty if upstream translation failed |
| `reviewedTranslation` | string | yes | Defaults to `appTranslation`; never null |
| `notes` | string | yes | Default `""` |
| `status` | string | yes | One of `Draft` / `Reviewed` / `Skip`; default `Draft` |

**Size constraint**: 1000 segments × ~600 bytes typical (Korean + English + metadata) = ~600 KB. Stays well under Firestore's 1 MB per-doc limit. Hard cap enforced at ingest time via `SERMON_MAX_SEGMENTS`.

#### `organizations/{orgId}/services/{serviceKey}` (extension)

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `linkedSermonId` | string \| null | no | New optional field. Existing services have no field → behave identically to today. |

#### `firestore.rules` additions

```javascript
match /organizations/{orgId}/sermons/{sermonId} {
  // Read: any authenticated org member (any role)
  allow read: if request.auth != null
              && exists(/databases/$(database)/documents/organizations/$(orgId)/members/$(request.auth.uid));
  // Write: server-only (backend uses Admin SDK; rules deny client writes)
  allow write: if false;
}
```

#### `firestore.indexes.json`

- List view: `sermons` ordered by `updatedAt DESC` — single-field, no composite index needed.
- No new composite indexes required for v1.

### 3.4 Segment ID Generation

```
For a sermon with N segments:
  width = max(3, len(str(N)))
  for i in range(1, N+1):
      segmentId = f"S{i:0{width}d}"

Examples:
  10 segments  → S001 .. S010
  150 segments → S001 .. S150
  1000 segments → S0001 .. S1000  (width promotes at boundary)
```

IDs are generated once at ingest time and **never change**. Re-translation does not re-issue IDs. Re-import never creates new IDs.

---

## 4. API Specification

All endpoints are mounted under `/api/org/{orgId}/sermons` and require `Authorization: Bearer <Firebase ID token>`. RBAC enforced server-side via `org_role_required(["owner","admin","host"])`.

### 4.1 Endpoint List

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/api/org/{orgId}/sermons/ingest` | Ingest source text from one of 4 paths → translate → persist Sermon | owner/admin/host |
| `GET` | `/api/org/{orgId}/sermons` | List org's sermons (id, title, segment count, status summary, updatedAt) | owner/admin/host |
| `GET` | `/api/org/{orgId}/sermons/{sermonId}` | Get sermon detail (segments inline) | owner/admin/host |
| `GET` | `/api/org/{orgId}/sermons/{sermonId}/review-file.xlsx` | Download `.xlsx` Review File | owner/admin/host |
| `POST` | `/api/org/{orgId}/sermons/{sermonId}/review-file` | Upload edited `.xlsx`; validate; persist reviews | owner/admin/host |
| `POST` | `/api/org/{orgId}/sermons/{sermonId}/link` | Link sermon to a service (`{ serviceKey }` body, or `null` to unlink) | owner/admin |

### 4.2 Detailed Specification

#### `POST /api/org/{orgId}/sermons/ingest`

**Request (one of)**:
```json
// google_docs
{ "sourceType": "google_docs", "url": "https://docs.google.com/document/d/...", "title": "Romans 8 — Easter Sermon" }

// paste
{ "sourceType": "paste", "text": "Korean text ...", "title": "..." }
```
For `file_txt` and `file_docx`, request is `multipart/form-data` with fields `sourceType`, `title`, and file part `file` (max 1 MB).

**Response (201)**:
```json
{
  "data": { "sermonId": "8KqZ...", "title": "...", "segmentCount": 127 }
}
```

**Errors**:
- `400 INVALID_SOURCE` — unsupported `sourceType`, missing required field, file too big, wrong MIME
- `400 INGEST_FAILED` — Google Docs fetch returned non-200, `.docx` parse error
- `401 UNAUTHORIZED` — missing or invalid token
- `403 FORBIDDEN` — role is `viewer` or user not in org
- `413 PAYLOAD_TOO_LARGE` — file or paste exceeds `SERMON_MAX_UPLOAD_BYTES`
- `429 GOOGLE_RATE_LIMITED` — Google Docs API returned 429; client should retry with backoff
- `507 SEGMENT_LIMIT_EXCEEDED` — would produce > `SERMON_MAX_SEGMENTS` segments

#### `GET /api/org/{orgId}/sermons/{sermonId}/review-file.xlsx`

**Response (200)**:
- Content-Type: `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
- Content-Disposition: `attachment; filename="{slug-title}-{sermonId-short}-review.xlsx"`
- Body: binary `.xlsx`

**Errors**:
- `401`, `403`, `404 SERMON_NOT_FOUND`

#### `POST /api/org/{orgId}/sermons/{sermonId}/review-file`

**Request**: `multipart/form-data` with field `file` (`.xlsx`, max 5 MB).

**Response (200)** — atomic success:
```json
{
  "data": {
    "summary": {
      "total": 127,
      "imported": 127,
      "warned": 3,
      "errored": 0
    },
    "rows": [
      { "row": 2, "segmentId": "S001", "level": "ok" },
      { "row": 5, "segmentId": "S004", "level": "warn", "code": "EMPTY_REVIEW", "message": "Reviewed Translation empty — fell back to App Translation" }
    ]
  }
}
```

**Response (400)** — atomic failure (no write):
```json
{
  "error": {
    "code": "IMPORT_VALIDATION_FAILED",
    "message": "Import rejected — 2 errors in uploaded file.",
    "details": {
      "summary": { "total": 127, "imported": 0, "warned": 0, "errored": 2 },
      "rows": [
        { "row": 7, "segmentId": "S006", "level": "error", "code": "UNKNOWN_SEGMENT_ID" },
        { "row": 12, "segmentId": "S011", "level": "error", "code": "DUPLICATE_SEGMENT_ID" }
      ]
    }
  }
}
```

**Other errors**: `401`, `403`, `404`, `409 SERMON_MODIFIED_CONCURRENTLY`, `413 PAYLOAD_TOO_LARGE`, `415 UNSUPPORTED_MEDIA_TYPE`.

#### `POST /api/org/{orgId}/sermons/{sermonId}/link`

**Request**: `{ "serviceKey": "sunday-am" }` or `{ "serviceKey": null }` to unlink.

**Response (200)**: `{ "data": { "sermonId": "...", "linkedServiceKey": "sunday-am" } }`

**Errors**: `401`, `403`, `404`, `409 SERVICE_ALREADY_LINKED` (the service already has a different sermon — client must explicitly confirm replace via `?replace=true`).

### 4.3 Error Response Format (project-wide)

```json
{
  "error": {
    "code": "MACHINE_READABLE_CODE",
    "message": "Human-readable message",
    "details": { /* optional structured detail */ }
  }
}
```

---

## 5. UI/UX Design

### 5.1 Screen Layout

#### `/admin/sermons` — Sermon List

```
┌──────────────────────────────────────────────────────────────┐
│  Sermons                              [+ New Sermon]         │
├──────────────────────────────────────────────────────────────┤
│  Title              Segments  Status        Updated  Link    │
├──────────────────────────────────────────────────────────────┤
│  Easter Sermon      127       89% reviewed  2h ago   sunday  │
│  Christmas Eve      94        Draft          1d ago   —       │
│  Acts 2 Series      210       Fully reviewed 3d ago   wed     │
└──────────────────────────────────────────────────────────────┘
```

#### `/admin/sermons/new` — Ingest

```
┌──────────────────────────────────────────────────────────────┐
│  New Sermon                                                  │
├──────────────────────────────────────────────────────────────┤
│  Title: [_______________________________________________]    │
│                                                              │
│  Source:  ( Google Docs ) ( Upload File ) ( Paste Text )     │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ [active tab content — URL field / file picker / textarea]│ │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│                                       [Cancel]  [Translate]  │
└──────────────────────────────────────────────────────────────┘
```

#### `/admin/sermons/[sermonId]` — Detail

```
┌──────────────────────────────────────────────────────────────┐
│  Easter Sermon         [Export Review File] [Import Reviewed]│
│  127 segments • 89% reviewed                                 │
│  Linked to service: sunday-am  [Change…]  [Unlink]           │
├──────────────────────────────────────────────────────────────┤
│  Last import: 3 errors  ─  see details                       │
├──────────────────────────────────────────────────────────────┤
│  S001  오늘 우리는 …            Today we will look at …       │
│        Reviewed: Today, we will look together at the grace … │
│        Status: Reviewed                                      │
│  S002  은혜는 …                  Grace is not …               │
│        Reviewed: Grace is not just a comforting emotion.     │
│        Status: Reviewed                                      │
│  …                                                           │
└──────────────────────────────────────────────────────────────┘
```

### 5.2 User Flow

```
1. Host opens /admin/sermons         →  click [+ New Sermon]
2. /admin/sermons/new                 →  choose source, fill, click Translate
3. Backend ingests + translates       →  redirect to /admin/sermons/[id]
4. Host clicks [Export Review File]   →  .xlsx downloads
5. Host opens in Google Sheets        →  edits Reviewed Translation/Notes/Status
6. Host returns, clicks [Import Reviewed] → picks file, confirms upload
7. Backend validates                   →  shows summary (imported/warned/errored)
8. If errors: host fixes file, retries
9. If success: host clicks [Change…] to link to a service
10. During broadcast: reviewed text used in preference to machine output
```

### 5.3 Component List

| Component | Location | Responsibility |
|-----------|----------|----------------|
| `SermonsListPage` (page) | `frontend/pages/admin/sermons/index.tsx` | List sermons; row-click navigates to detail |
| `SermonDetailPage` (page) | `frontend/pages/admin/sermons/[sermonId].tsx` | Show segments; export/import controls; linking |
| `SermonNewPage` (page) | `frontend/pages/admin/sermons/new.tsx` | 3-tab ingestion form |
| `SermonIngestForm` (component) | `frontend/components/SermonIngestForm.tsx` | 3-tab UI: Google Docs / Upload / Paste |
| `SermonReviewControls` (component) | `frontend/components/SermonReviewControls.tsx` | Export button + Import dropzone + last-import status |
| `ImportReport` (component) | colocated inside `SermonReviewControls.tsx` for v1 | Render structured per-row report |

### 5.4 Page UI Checklist

#### `/admin/sermons` (List)

- [ ] Button: `+ New Sermon` (top-right, primary)
- [ ] Empty state: "No sermons yet — create your first" with CTA button
- [ ] Table column: Title (clickable → detail page)
- [ ] Table column: Segments (integer count)
- [ ] Table column: Status (badge — `Draft` / `N% reviewed` / `Fully reviewed`)
- [ ] Table column: Updated (relative time, e.g. "2h ago")
- [ ] Table column: Linked service (service key or `—`)
- [ ] Sort: default by `updatedAt DESC`
- [ ] Loading skeleton while fetching
- [ ] Auth guard: redirects `viewer` role away from this route

#### `/admin/sermons/new` (Ingest)

- [ ] Field: Title (required, max 200 chars)
- [ ] Tab: Google Docs (URL field, helper text "Anyone-with-link or you own it")
- [ ] Tab: Upload File (`.txt` and `.docx` accepted, max 1 MB warning)
- [ ] Tab: Paste Text (textarea, char counter)
- [ ] Button: Cancel (returns to list)
- [ ] Button: Translate (disabled until title + valid source selected)
- [ ] Loading state while ingesting (spinner + "Translating … this can take 30s for long sermons")
- [ ] Error state: shows backend `error.message` inline; does not lose user input
- [ ] Auth guard: `viewer` blocked

#### `/admin/sermons/[sermonId]` (Detail)

- [ ] Header: sermon title + segment count + review % badge
- [ ] Button: Export Review File (downloads `.xlsx`)
- [ ] Button: Import Reviewed (opens file picker; only `.xlsx`)
- [ ] Section: Linked Service (current link or "Not linked"; `Change…` / `Unlink` buttons; restricted to `owner`/`admin`)
- [ ] Section: Last Import Status (collapsed by default; expands to per-row report on click)
- [ ] List: segments (segmentId, original, appTranslation, reviewedTranslation, status badge)
- [ ] Pagination or virtualization if > 100 segments (default page size 50)
- [ ] After successful import: toast "127 segments imported, 3 warnings" + refresh segment list
- [ ] After failed import: red banner "Import rejected — 2 errors. Fix and retry." + expanded report
- [ ] Auth guard: `viewer` blocked from page entirely; `host` blocked from Link/Unlink section only

---

## 6. Error Handling

### 6.1 Error Code Definition

| Code | HTTP | Cause | Handling |
|------|------|-------|----------|
| `INVALID_SOURCE` | 400 | Wrong `sourceType` or missing fields | Show inline form error |
| `INGEST_FAILED` | 400 | Google Docs fetch failed / `.docx` unparseable | Show retry suggestion, fallback to paste |
| `IMPORT_VALIDATION_FAILED` | 400 | Spreadsheet violates rules | Show per-row report |
| `UNAUTHORIZED` | 401 | Missing/expired token | Redirect to `/login` |
| `FORBIDDEN` | 403 | Insufficient role | Show "Contact admin" message |
| `SERMON_NOT_FOUND` | 404 | Bad sermonId | Redirect to list with toast |
| `SERMON_MODIFIED_CONCURRENTLY` | 409 | Another writer beat us | "Sermon changed since you downloaded — please re-export and try again." |
| `SERVICE_ALREADY_LINKED` | 409 | Service has different sermon | Confirm dialog: "Replace existing link?" |
| `PAYLOAD_TOO_LARGE` | 413 | File too big | Inline error "File exceeds X MB" |
| `UNSUPPORTED_MEDIA_TYPE` | 415 | Wrong file type | "Only `.xlsx` accepted" |
| `GOOGLE_RATE_LIMITED` | 429 | Google API quota | Auto-retry once with backoff; surface error if persists |
| `SEGMENT_LIMIT_EXCEEDED` | 507 | > `SERMON_MAX_SEGMENTS` | "Sermon too long. Split into parts." |
| `INTERNAL_ERROR` | 500 | Uncaught backend error | Log + generic message |

### 6.2 Import Validation Rule Catalog

Each rule is `(level, code, message_template)`. `error` levels block the import atomically; `warn` levels import successfully but report.

| Code | Level | Trigger |
|------|-------|---------|
| `WRONG_SERMON_ID` | error | Workbook's embedded Sermon ID doesn't match URL `{sermonId}` |
| `MISSING_REQUIRED_COLUMN` | error | Any of: Sermon ID, Segment ID, Segment Order, Original Text, App Translation, Reviewed Translation, Notes, Status |
| `UNKNOWN_SEGMENT_ID` | error | Row's Segment ID not present in current sermon's segments[] |
| `DUPLICATE_SEGMENT_ID` | error | Same Segment ID appears in 2+ rows |
| `MISSING_SEGMENT` | error | A sermon segment has no corresponding row (user deleted) |
| `INVALID_STATUS` | error | Status not in `Draft` / `Reviewed` / `Skip` (case-sensitive) |
| `ORIGINAL_TEXT_MUTATED` | error | Row's `Original Text` differs from stored value (defends against shifted rows) |
| `APP_TRANSLATION_MUTATED` | warn | Row's `App Translation` differs from stored value — likely accidental edit; not blocking but logged |
| `EMPTY_REVIEW` | warn | Reviewed Translation is empty → falls back to App Translation on save |
| `EXCESSIVE_LENGTH` | warn | Reviewed Translation > 2000 chars (probably an error; not blocking) |
| `OK` | ok | Row passed all checks |

**Atomicity**: presence of any `error`-level finding → 400 with full report, no Firestore write. Warnings do not block.

### 6.3 Error Response Format

Project-standard shape, defined in §4.3.

---

## 7. Security Considerations

- [x] **Auth**: every endpoint uses `Depends(get_current_user_required)`; no public routes
- [x] **RBAC**: server-side role check via existing pattern; client UI gating is defense-in-depth only
- [x] **File-type sniffing**: validate magic bytes for `.xlsx` (PK\x03\x04 zip header + presence of `xl/workbook.xml`) — do not trust filename extension
- [x] **Zip-bomb protection**: openpyxl uses `defusedxml`; reject `.xlsx` whose decompressed content > 50 MB; reject > 5000 rows even if file < 5 MB
- [x] **Upload size cap**: enforced at FastAPI middleware (`SERMON_MAX_UPLOAD_BYTES`, default 5 MB for `.xlsx`; 1 MB for `.txt`/`.docx`)
- [x] **Google Docs OAuth scope**: request only `https://www.googleapis.com/auth/documents.readonly`; never request broader Drive scope in v1; tokens stored in Firestore with at-rest encryption via Cloud KMS (or not persisted if one-shot fetch is feasible)
- [x] **CORS**: existing `CORS_ALLOW_ORIGINS` env var governs; no new wildcards
- [x] **Audit logging**: every export, import, link/unlink emits `security_event()` with actor uid, sermon id, org id, byte sizes, row counts
- [x] **Rate limiting**: leverage existing slowapi config; add limits — `/sermons/ingest` 10/hr per user, `/sermons/{id}/review-file (POST)` 30/hr per sermon
- [x] **Firestore rules**: clients can read sermons (org-scoped); writes are server-only via Admin SDK
- [x] **No PII in `sourceRef`**: do not persist Google Docs OAuth tokens in the sermon doc; only the URL (which is not itself a secret)
- [x] **HTTPS/WSS only in production** (already enforced project-wide)

---

## 8. Test Plan

### 8.1 Test Scope

| Type | Target | Tool | Phase |
|------|--------|------|-------|
| L1: API Tests | All 6 endpoints, status codes, response shapes, RBAC | `pytest` + `httpx.AsyncClient` (`backend/tests/`) | Do |
| L2: UI Action Tests | 3 new pages: form submissions, button clicks, error rendering | Playwright | Do |
| L3: E2E Scenario | Full round-trip: ingest → translate → export → edit-in-test → import → broadcast | Playwright + Firestore emulator + WebSocket assertion | Do |

### 8.2 L1: API Test Scenarios

| # | Endpoint | Method | Test Description | Expected Status | Expected Response |
|---|----------|--------|-----------------|:--------------:|-------------------|
| 1 | `/api/org/{orgId}/sermons/ingest` | POST | Paste-source happy path | 201 | `data.sermonId` exists, `data.segmentCount` > 0 |
| 2 | `/api/org/{orgId}/sermons/ingest` | POST | `.docx` upload happy path | 201 | `data.sermonId` exists |
| 3 | `/api/org/{orgId}/sermons/ingest` | POST | Google Docs URL happy path (mocked) | 201 | `data.sermonId` exists |
| 4 | `/api/org/{orgId}/sermons/ingest` | POST | Invalid `sourceType` | 400 | `error.code` == `INVALID_SOURCE` |
| 5 | `/api/org/{orgId}/sermons/ingest` | POST | File over 1 MB | 413 | `error.code` == `PAYLOAD_TOO_LARGE` |
| 6 | `/api/org/{orgId}/sermons/ingest` | POST | No auth header | 401 | `error.code` == `UNAUTHORIZED` |
| 7 | `/api/org/{orgId}/sermons/ingest` | POST | Viewer role | 403 | `error.code` == `FORBIDDEN` |
| 8 | `/api/org/{orgId}/sermons` | GET | List returns array sorted by updatedAt DESC | 200 | `data` is array, sorted |
| 9 | `/api/org/{orgId}/sermons/{id}` | GET | Detail returns segments inline | 200 | `data.segments.length` matches stored |
| 10 | `/api/org/{orgId}/sermons/{id}` | GET | Unknown id | 404 | `error.code` == `SERMON_NOT_FOUND` |
| 11 | `/api/org/{orgId}/sermons/{id}/review-file.xlsx` | GET | Download returns valid `.xlsx` | 200 | Content-Type `vnd.openxml...`, body opens via openpyxl, contains all segments |
| 12 | `/api/org/{orgId}/sermons/{id}/review-file` | POST | Unmodified round-trip imports clean | 200 | `summary.imported` == segment count, `errored` == 0 |
| 13 | `/api/org/{orgId}/sermons/{id}/review-file` | POST | Deleted row → MISSING_SEGMENT error | 400 | `error.code` == `IMPORT_VALIDATION_FAILED` with `MISSING_SEGMENT` in rows |
| 14 | `/api/org/{orgId}/sermons/{id}/review-file` | POST | Duplicated row → DUPLICATE_SEGMENT_ID | 400 | rows contain `DUPLICATE_SEGMENT_ID` |
| 15 | `/api/org/{orgId}/sermons/{id}/review-file` | POST | Mutated Original Text → ORIGINAL_TEXT_MUTATED | 400 | rows contain `ORIGINAL_TEXT_MUTATED` |
| 16 | `/api/org/{orgId}/sermons/{id}/review-file` | POST | Wrong sermon's file → WRONG_SERMON_ID | 400 | rows contain `WRONG_SERMON_ID`; sermon doc unchanged |
| 17 | `/api/org/{orgId}/sermons/{id}/review-file` | POST | Empty Reviewed Translation → EMPTY_REVIEW warning, imports | 200 | `summary.warned` ≥ 1, `errored` == 0, stored `reviewedTranslation` == `appTranslation` |
| 18 | `/api/org/{orgId}/sermons/{id}/review-file` | POST | Concurrent edit → 409 | 409 | `error.code` == `SERMON_MODIFIED_CONCURRENTLY` |
| 19 | `/api/org/{orgId}/sermons/{id}/review-file` | POST | Non-xlsx file (e.g. csv) | 415 | `error.code` == `UNSUPPORTED_MEDIA_TYPE` |
| 20 | `/api/org/{orgId}/sermons/{id}/link` | POST | Link to free service | 200 | service doc gains `linkedSermonId` |
| 21 | `/api/org/{orgId}/sermons/{id}/link` | POST | Link service already linked (no `replace=true`) | 409 | `error.code` == `SERVICE_ALREADY_LINKED` |
| 22 | `/api/org/{orgId}/sermons/{id}/link` | POST | Host role attempts link | 403 | linking is `owner`/`admin` only |

### 8.3 L2: UI Action Test Scenarios

| # | Page | Action | Expected Result | Data Verification |
|---|------|--------|----------------|-------------------|
| 1 | `/admin/sermons` | Load page (with seeded sermons) | All §5.4 checklist elements visible | Sermon list rows render from Firestore |
| 2 | `/admin/sermons` | Click `+ New Sermon` | Navigates to `/admin/sermons/new` | URL change observed |
| 3 | `/admin/sermons/new` | Tab switch Paste → Upload | UI swaps without losing title field | DOM state preserved |
| 4 | `/admin/sermons/new` | Submit paste with valid Korean text | Loading → redirect to detail page | Detail page shows segments |
| 5 | `/admin/sermons/new` | Submit `.docx` over 1 MB | Inline 413 error rendered | Form input preserved |
| 6 | `/admin/sermons/[id]` | Click `Export Review File` | `.xlsx` download initiated | Filename matches pattern |
| 7 | `/admin/sermons/[id]` | Upload unmodified `.xlsx` | Toast: "N imported, 0 warnings" | Last-import section shows summary |
| 8 | `/admin/sermons/[id]` | Upload corrupted `.xlsx` (deleted row) | Red banner + expanded per-row report | No segment in list updates |
| 9 | `/admin/sermons/[id]` | Host clicks Link section | Section is hidden / disabled (RBAC) | Link buttons absent |
| 10 | `/admin/sermons/[id]` | Owner clicks `Change…` then picks a service | Service link updates | Service doc gains `linkedSermonId` |
| 11 | All pages | Viewer attempts to load | Redirect or 403 page | No data leaked |

### 8.4 L3: E2E Scenario Test Scenarios

| # | Scenario | Steps | Success Criteria |
|---|----------|-------|-----------------|
| 1 | **Full round-trip — paste source** | Login as host → `/admin/sermons/new` → paste Korean → Translate → detail → Export → simulate edit (Python openpyxl modifies 3 rows + marks Status=Reviewed) → Import → success report → link to service → open listener page → assert listener sees reviewed text for the 3 modified segments | Reviewed text observed at listener WebSocket within 2s of host audio match |
| 2 | **Full round-trip — `.docx` source** | Same as #1 but ingest via `.docx` upload | Same |
| 3 | **Wrong-sermon defense** | Create sermon A, create sermon B → export A → upload A's file to B's import endpoint | 400 `WRONG_SERMON_ID`; sermon B unchanged on Firestore |
| 4 | **Concurrent import** | Two users export same sermon at T=0 → user 1 imports at T=10 → user 2 imports at T=15 | User 2 receives 409; sermon reflects user 1's edits only |
| 5 | **Skip semantics in broadcast** | Mark segment S005 as `Status=Skip` and `reviewedTranslation="ignored"` → live broadcast → assert listener sees machine-translated text for S005 (not "ignored") | Verified via listener WS message inspection |
| 6 | **RBAC matrix** | Login as each role × hit each endpoint | viewer: 403 on all; host: 200 on ingest/export/import, 403 on link; admin/owner: 200 on all |

### 8.5 Seed Data Requirements

| Entity | Minimum Count | Key Fields Required |
|--------|:------------:|---------------------|
| Organization | 1 | `orgId` |
| Members | 4 | one of each: owner, admin, host, viewer |
| Services | 2 | `serviceKey`, at least one with no `linkedSermonId` |
| Sermon (test fixture) | 1 | 5 segments with known Korean + English text for round-trip integrity checks |
| Google Docs API | mocked | fixture `.docx`-equivalent text content |

Backend test fixtures live in `backend/tests/fixtures/sermon_review/` and include:
- `valid_unmodified.xlsx` — exported then re-uploaded
- `valid_with_edits.xlsx` — 3 edited rows
- `bad_missing_row.xlsx` — row deleted
- `bad_duplicate.xlsx` — duplicated row
- `bad_wrong_sermon.xlsx` — wrong Sermon ID embedded
- `bad_mutated_original.xlsx` — Original Text changed
- `bad_csv.csv` — wrong format

---

## 9. Clean Architecture

The project is FastAPI + Next.js (Pages Router), not the strict Presentation/Application/Domain/Infrastructure four-layer model the template assumes. The mapping below is approximate; what matters is the *direction of dependency*.

### 9.1 Layer Mapping (Backend)

| Layer | This Feature's Files |
|-------|----------------------|
| **Presentation (HTTP)** | `backend/app/routes/sermon_review.py` |
| **Application (use cases)** | `backend/app/sermon_review/{ingest,xlsx_export,xlsx_import,validation}.py` |
| **Domain (entities/rules)** | `backend/app/sermon_review/models.py` (Pydantic) |
| **Infrastructure (external)** | `backend/app/services/multichurch_store.py` (Firestore), `google-api-python-client`, `python-docx`, `openpyxl` |

### 9.2 Layer Mapping (Frontend)

| Layer | This Feature's Files |
|-------|----------------------|
| **Presentation** | `frontend/pages/admin/sermons/*.tsx`, `frontend/components/SermonReviewControls.tsx`, `SermonIngestForm.tsx` |
| **Application (hooks/services)** | inline within components for v1 — no premature abstraction |
| **Domain (types)** | `frontend/lib/types/sermon.ts` (new TypeScript interfaces mirroring `models.py`) |
| **Infrastructure (API client)** | `frontend/lib/api/org/{orgId}/sermons.ts` (new — thin fetch wrappers using existing auth context) |

### 9.3 Dependency Rules

- `routes/sermon_review.py` may import from `sermon_review/` and `services/multichurch_store`. It MUST NOT contain business logic.
- `sermon_review/*.py` modules MUST NOT import from `routes/`. They MAY import each other; circular imports are forbidden.
- `models.py` is the only file that defines `Segment`, `Sermon`, and enums. All other modules import from it.
- Frontend `pages/` import from `components/` and `lib/`. `lib/api/org/{orgId}/sermons.ts` is the only place that calls fetch on sermon endpoints.

---

## 10. Coding Convention Reference

### 10.1 Naming Conventions

Per project `CLAUDE.md`:

| Target | Rule | Example |
|--------|------|---------|
| Python files | snake_case | `xlsx_export.py` |
| Python classes | PascalCase | `Sermon`, `Segment`, `ValidationReport` |
| Python functions | snake_case | `build_xlsx`, `validate_workbook` |
| TS components | PascalCase | `SermonReviewControls` |
| TS files (components) | PascalCase.tsx | `SermonReviewControls.tsx` |
| TS files (utilities) | camelCase.ts | `sermonApi.ts` if a utility module is needed |
| Next.js pages | kebab-case routes, camel inside | `pages/admin/sermons/index.tsx` |

### 10.2 Backend Style

Per project `CLAUDE.md`:
- `from __future__ import annotations` at top of every file
- `async def` for routes and any I/O-bound calls
- Env vars via `os.getenv("KEY", default)` — never bare access
- Centralize new env in `backend/app/env.py` (`ENV` class)
- Log security-sensitive actions via `security_event()` from `app.security_log`
- Routes mounted from `main.py` via `app.include_router(router)`

### 10.3 Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `GOOGLE_DOCS_OAUTH_CLIENT_ID` | — (required) | OAuth client for Google Docs read |
| `GOOGLE_DOCS_OAUTH_CLIENT_SECRET` | — (required) | OAuth client secret |
| `GOOGLE_DOCS_OAUTH_REDIRECT_URI` | — (required) | OAuth callback URL |
| `SERMON_MAX_SEGMENTS` | `1000` | Hard cap; ingest exceeds → 507 |
| `SERMON_MAX_UPLOAD_BYTES` | `5242880` (5 MB) | `.xlsx` cap |
| `SERMON_SOURCE_MAX_BYTES` | `1048576` (1 MB) | `.txt`/`.docx` cap |
| `SERMON_XLSX_MAX_DECOMPRESSED_BYTES` | `52428800` (50 MB) | zip-bomb guard |

### 10.4 This Feature's Conventions

| Item | Convention |
|------|-----------|
| Spreadsheet column headers | English-only; canonical names locked in `xlsx_export.py` as `COLUMNS = ["Sermon ID", "Segment ID", "Segment Order", "Original Text", "App Translation", "Reviewed Translation", "Notes", "Status"]`; same constant re-used in import validation |
| Segment ID | Server-generated; never user-editable; `S{n:0{w}d}` where `w = max(3, len(str(N)))` |
| Pydantic models | All sermon-related models live in `sermon_review/models.py`; no model leakage across module boundaries |
| Error responses | `{ error: { code, message, details? } }` matches project pattern |
| Frontend fetch | Use `useAuth()` from `lib/authContext` to attach Bearer token; never fetch without it |

---

## 11. Implementation Guide

### 11.1 File Structure

```
backend/
  app/
    routes/
      sermon_review.py                  [NEW]
    sermon_review/                      [NEW MODULE]
      __init__.py
      ingest.py                         (4 functions)
      xlsx_export.py
      xlsx_import.py
      validation.py
      models.py                         (Pydantic)
    services/
      multichurch_store.py              [+5 helpers, no breaking changes]
    utils/
      translate.py                      [+1 function: get_reviewed_text(); +1 call site]
    env.py                              [+6 env vars]
    main.py                             [+1 include_router line]
  firestore/
    firestore.rules                     [+1 match block]
  tests/
    fixtures/
      sermon_review/*.xlsx              [NEW — 7 fixture files]
    test_sermon_review_routes.py        [NEW]
    test_sermon_review_validation.py    [NEW]
    test_sermon_review_xlsx.py          [NEW]
    test_translate_reviewed_text.py     [NEW]
  requirements.txt                       [+openpyxl, python-docx, google-api-python-client, google-auth-oauthlib, defusedxml]

frontend/
  pages/
    admin/
      sermons/
        index.tsx                        [NEW — list]
        new.tsx                          [NEW — ingest]
        [sermonId].tsx                   [NEW — detail]
  components/
    SermonIngestForm.tsx                 [NEW]
    SermonReviewControls.tsx             [NEW]
  lib/
    api/
      sermons.ts                         [NEW — fetch wrappers]
    types/
      sermon.ts                          [NEW — TS mirror of Pydantic]
  tests/
    e2e/
      editing-sermon.spec.ts             [NEW — L2+L3]
```

### 11.2 Implementation Order

1. [ ] **Backend — domain & validation** (no I/O): `sermon_review/models.py`, `sermon_review/validation.py` + unit tests
2. [ ] **Backend — xlsx round-trip** (pure file I/O): `xlsx_export.py`, `xlsx_import.py` + tests using fixtures
3. [ ] **Backend — ingestion** (4 source paths): `ingest.py`; mock Google Docs API in tests
4. [ ] **Backend — Firestore data layer**: extend `multichurch_store.py` with sermon CRUD; add Firestore rules
5. [ ] **Backend — routes & RBAC**: `routes/sermon_review.py`; wire into `main.py`; integration tests with auth
6. [ ] **Backend — translation hook**: `get_reviewed_text()` in `translate.py`; verify zero-regression test for "no sermon linked" path
7. [ ] **Frontend — types & API client**: `lib/types/sermon.ts`, `lib/api/org/{orgId}/sermons.ts`
8. [ ] **Frontend — components**: `SermonIngestForm.tsx`, `SermonReviewControls.tsx`
9. [ ] **Frontend — pages**: `/admin/sermons/index.tsx`, `new.tsx`, `[sermonId].tsx`
10. [ ] **E2E**: Playwright spec covers full round-trip + broadcast assertion

### 11.3 Session Guide

> Module breakdown for incremental implementation. Use `/pdca do editing-sermon --scope module-N`.

#### Module Map

| Module | Scope Key | Description | Estimated Turns |
|--------|-----------|-------------|:---------------:|
| Backend domain & xlsx | `module-1` | `models.py`, `validation.py`, `xlsx_export.py`, `xlsx_import.py` + unit tests + fixtures | 30–35 |
| Backend ingestion & store | `module-2` | `ingest.py` (4 source paths), `multichurch_store.py` extensions, Firestore rules, env vars | 25–30 |
| Backend routes & translation hook | `module-3` | `routes/sermon_review.py`, RBAC, `main.py` wiring, `translate.py` hook + zero-regression tests | 25–30 |
| Frontend types, API, components | `module-4` | `lib/types/sermon.ts`, `lib/api/org/{orgId}/sermons.ts`, `SermonIngestForm.tsx`, `SermonReviewControls.tsx` | 25–30 |
| Frontend pages & E2E | `module-5` | 3 new pages + Playwright spec for full round-trip | 25–30 |

#### Recommended Session Plan

| Session | Phase | Scope | Turns |
|---------|-------|-------|:-----:|
| Session 1 (done) | Plan + Design | full | 30–35 |
| Session 2 | Do | `--scope module-1` | 30–35 |
| Session 3 | Do | `--scope module-2,module-3` | 50–60 |
| Session 4 | Do | `--scope module-4,module-5` | 50–60 |
| Session 5 | Check + Iterate + QA + Report | full | 40–50 |

Each module's checkpoint: tests for that module pass in CI before moving on. No "wire everything then test" — code + test = 1 set.

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-06-16 | Initial draft; Option C selected | namju |
