# editing-sermon Planning Document

> **Summary**: Spreadsheet round-trip workflow for reviewing and editing machine-translated sermon segments outside the app — export reviewable `.xlsx`, edit in Google Sheets / Excel / Numbers, re-import the corrected file, broadcast with reviewed translations.
>
> **Project**: Real-Time Translation Platform
> **Version**: 0.1
> **Author**: namju
> **Date**: 2026-06-16
> **Status**: Draft

---

## Executive Summary

| Perspective | Content |
|-------------|---------|
| **Problem** | Long-form sermon review inside the app is painful — pastors and translators need to edit dozens or hundreds of translated segments, but the existing host UI is built for live broadcast, not bulk text editing. As a result, machine translations ship to listeners with avoidable errors, or pastors abandon review entirely. |
| **Solution** | A bidirectional **Export Review File / Import Reviewed Translation** workflow. The app generates a structured `.xlsx` keyed by stable Segment IDs, the user edits comfortably in Google Sheets / Excel / Numbers, and re-uploads. Strict validation on import preserves segment integrity; reviewed translations are then used during broadcast. |
| **Function/UX Effect** | Hosts and admins can review a 60-minute sermon in 10 minutes in their preferred spreadsheet tool. Bulk find-and-replace, multi-row editing, and team review (sharing the file) become trivial. The app stays focused on translation and broadcast. |
| **Core Value** | The platform's promise of accurate real-time translation gains an "I can actually fix it before service" escape valve, without bloating the in-app editor or sacrificing data integrity. |

---

## Context Anchor

> Auto-generated from Executive Summary. Propagated to Design/Do documents for context continuity.

| Key | Value |
|-----|-------|
| **WHY** | In-app bulk editing of sermon translations is impractical; pastors need a familiar tool (Google Sheets) to review and correct machine output efficiently. |
| **WHO** | Org-level `owner`, `admin`, `host` roles preparing a sermon for an upcoming service. Listeners and `viewer` role are not affected by this feature. |
| **RISK** | Re-import data corruption — a user-edited spreadsheet with shifted rows, deleted Segment IDs, or wrong sermon attached could silently overwrite correct translations or fail import. Validation must catch this before any write. |
| **SUCCESS** | (1) Host can complete export → edit → import round-trip with zero data loss, (2) at least one validation error per malformed file blocks import, (3) reviewed translations are used during broadcast in preference to machine output. |
| **SCOPE** | MVP: sermon source ingestion (Google Docs link + .txt/.docx upload + paste), segmentation + translation, `.xlsx` export, validated `.xlsx` import, broadcast integration. Post-MVP: translation memory learning, glossary suggestions, Google Sheets API live edits. |

---

## 1. Overview

### 1.1 Purpose

Provide an external review channel for sermon translations so pastors and translators can leverage familiar spreadsheet tooling (find/replace, multi-cell selection, collaborative editing) instead of fighting a per-segment in-app editor.

### 1.2 Background

The platform's existing `SermonPrep.tsx` flow translates a pasted Korean sermon segment-by-segment, but editing many segments inside the web UI is slow. Pastors who actively review sermons currently either (a) tolerate machine errors, (b) write corrections as freeform notes, or (c) skip review entirely. A round-trip spreadsheet workflow lets reviewers stay in their preferred tool and brings corrections back into the app via a stable Segment ID key.

The sermon-accuracy plan (`docs/01-plan/features/sermon-accuracy.plan.md`) already addresses translation quality via prompt engineering and few-shot examples; this feature is the **human-in-the-loop** complement: a fast, ergonomic way for a human to override the machine output before broadcast.

### 1.3 Related Documents

- Sermon accuracy work: `docs/01-plan/features/sermon-accuracy.plan.md`
- Existing sermon prep UI: `frontend/components/SermonPrep.tsx`
- Existing translation orchestration: `backend/app/utils/translate.py`
- Existing chunker: `backend/app/chunker/`
- Firestore data layer: `backend/app/services/multichurch_store.py`

---

## 2. Scope

### 2.1 In Scope

- [ ] **Sermon ingestion (3 paths)**: Google Docs link/paste-URL, `.txt` / `.docx` file upload, copy-paste raw text into an in-app textarea
- [ ] **Segmentation + initial translation**: existing chunker + GPT-4o pipeline produces `segments[]` with stable Segment IDs
- [ ] **Persistent sermon entity** in Firestore at `organizations/{orgId}/sermons/{sermonId}` (single doc, cost-optimized — see §7)
- [ ] **Export Review File**: backend generates `.xlsx` with protected metadata columns and editable review columns; pre-fills `Reviewed Translation` with `App Translation` so user starts from non-blank
- [ ] **Import Reviewed Translation**: backend accepts `.xlsx` upload, runs structural + content validation, writes reviewed segments back to Firestore
- [ ] **Validation report**: import returns per-row pass/warn/error summary with row numbers and Segment IDs
- [ ] **Broadcast integration**: when a reviewed sermon is linked to a service, the live translation pipeline prefers `reviewedTranslation` over machine output for matched segments
- [ ] **Skip semantics**: rows marked `Status = Skip` fall back to App Translation (machine output) during broadcast
- [ ] **RBAC**: only `owner`, `admin`, `host` roles can export/import; `viewer` denied
- [ ] **Audit logging**: every export and import emits a `security_event()` with actor UID, sermon ID, row counts

### 2.2 Out of Scope

- Google Sheets API live edits (real-time bidirectional sync) — post-MVP
- Translation memory / glossary learning loop ("you changed `saints → brothers and sisters` 3 times, remember this?") — post-MVP
- Real-time collaborative editing inside the app
- PDF / image upload as sermon source
- Sermon versioning history (a sermon has one current reviewed state; no diff history in v1)
- Mobile-optimized review UI inside the app (reviewers are expected to use Sheets / Excel)
- Per-segment audio playback aligned to spreadsheet rows
- Auto-application of reviewed translations to *past* services (only forward)
- xlsx cell-level protection enforcement (we surface "do not edit" hints visually; Excel-level locking is best-effort)

---

## 3. Requirements

### 3.1 Functional Requirements

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| FR-01 | User can create a new sermon by pasting a Google Docs link; backend fetches and ingests the document text | High | Pending |
| FR-02 | User can create a new sermon by uploading a `.txt` or `.docx` file (max 1 MB) | High | Pending |
| FR-03 | User can create a new sermon by pasting raw text into a textarea | High | Pending |
| FR-04 | Backend segments the source text and produces an initial GPT-4o translation per segment | High | Pending |
| FR-05 | Each segment receives a stable, server-generated Segment ID that persists across exports/imports | High | Pending |
| FR-06 | Authenticated `owner`/`admin`/`host` can download an `.xlsx` Review File for any sermon in their org | High | Pending |
| FR-07 | Review File pre-fills `Reviewed Translation` with `App Translation` so user starts from non-blank | High | Pending |
| FR-08 | Review File includes protected (non-editable hint) columns: Sermon ID, Segment ID, Segment Order, Original Text, App Translation | High | Pending |
| FR-09 | Review File includes editable columns: Reviewed Translation, Notes, Status (Draft / Reviewed / Skip) | High | Pending |
| FR-10 | Authenticated user can re-upload an edited `.xlsx`; backend validates and writes reviewed segments | High | Pending |
| FR-11 | Import validates: correct Sermon ID, no missing Segment IDs, no duplicate Segment IDs, all required columns present, no unknown rows added | High | Pending |
| FR-12 | Import returns a structured report: total rows, imported count, warned count, error count, per-row details | High | Pending |
| FR-13 | Import is atomic — a file with any error-level finding writes nothing; warnings (e.g., empty Reviewed Translation) import successfully with fallback to App Translation | High | Pending |
| FR-14 | Live translation pipeline prefers `reviewedTranslation` over machine output when a sermon is linked to the active service | High | Pending |
| FR-15 | Segments with `Status = Skip` use App Translation (machine fallback) during broadcast | Medium | Pending |
| FR-16 | Host can link a sermon to a service (one sermon per service at a time) | High | Pending |
| FR-17 | Host can unlink or replace a sermon on a service | Medium | Pending |
| FR-18 | Export and import actions emit `security_event()` audit log entries | High | Pending |
| FR-19 | `viewer` role receives 403 on export and import endpoints | High | Pending |
| FR-20 | Frontend surfaces a sermon list page showing per-sermon status (Draft / Some Reviewed / Fully Reviewed) | Medium | Pending |

### 3.2 Non-Functional Requirements

| Category | Criteria | Measurement Method |
|----------|----------|-------------------|
| Performance — export | `.xlsx` generation for 500-segment sermon completes in < 2 s server-side | Backend timing logs |
| Performance — import | `.xlsx` validation + write for 500-segment sermon completes in < 3 s server-side | Backend timing logs |
| Performance — Firestore cost | Loading a sermon for export = 1 Firestore read; importing = 1 Firestore write per sermon (not per segment) | Firestore usage console; design assertion |
| Security — auth | All export/import endpoints require Bearer token; RBAC enforced server-side, not client-side | Manual + automated 401/403 tests |
| Security — file | Uploaded `.xlsx` capped at 5 MB; file type sniffed (not just extension); openpyxl read in defusedxml-safe mode | Backend rejects oversized / wrong-MIME files |
| Security — Google Docs OAuth | Drive read scope requested only at moment of need; refresh tokens stored in Firestore encrypted-at-rest or not stored if scope is one-shot | Token audit; OAuth flow review |
| Data integrity | Import is transactional at the sermon-document level — partial writes impossible | Code review of Firestore write |
| Reliability | Backend rejects malformed `.xlsx` with row-level error report rather than 500 | Unit tests for corrupt inputs |
| Accessibility | Export/Import buttons reachable via keyboard; status report screen-reader friendly | Manual axe-core run |
| Internationalization | Spreadsheet column headers are English in MVP; ko/en bilingual headers post-MVP | Header literal review |

---

## 4. Success Criteria

### 4.1 Definition of Done

- [ ] Host can export a Review File from a freshly translated sermon
- [ ] Host can re-upload the edited file and see a validation report
- [ ] Broadcast uses reviewed translations for matched segments
- [ ] All 3 sermon ingestion paths (Google Docs / file upload / paste) work end-to-end
- [ ] RBAC enforced on backend (`viewer` cannot export/import)
- [ ] Unit tests cover happy-path import, missing-column import, duplicate-Segment-ID import, wrong-sermon-ID import, empty-Reviewed-Translation import
- [ ] Integration test covers full round-trip: ingest → translate → export → edit-in-test → import → broadcast → reviewed text appears in listener WebSocket
- [ ] Frontend lint clean (`npm run lint`)
- [ ] No new Firestore composite indexes required, OR new indexes deployed via `firestore.indexes.json`
- [ ] Documentation added to `CLAUDE.md` describing the sermon lifecycle and review workflow
- [ ] `security_log.py` audit events for export and import verified in dev

### 4.2 Quality Criteria

- [ ] Backend test coverage ≥ 80% on new modules (`backend/app/sermon_review/` or equivalent)
- [ ] Frontend lint clean, no new TypeScript `any` introduced
- [ ] Build succeeds (`npm run build`)
- [ ] No Firestore reads/writes per segment (cost regression prevention)
- [ ] `.xlsx` round-trip is byte-stable for unmodified files (export → import without edits leaves data identical)

---

## 5. Risks and Mitigation

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| User deletes rows or shifts cells in spreadsheet, corrupting Segment ID mapping | High — wrong translation broadcast | Medium | Strict Segment ID validation on import; reject file if any row's Segment ID doesn't match a stored segment; UI hints in exported sheet warn against editing gray columns |
| User uploads wrong sermon's file to the wrong sermon | High — silent overwrite of unrelated sermon | Medium | Sermon ID embedded in spreadsheet metadata + cross-check against the target sermon; mismatch → reject |
| Google Docs OAuth scope creep — users grant broader Drive access than needed | Medium — privacy concern | Medium | Use `drive.file` scope (per-file access) if picker-based; if URL-based, request only `documents.readonly` and only at moment of fetch |
| Google Docs API rate limits / 429s during peak preparation hours (Sunday morning) | Medium — feature unavailable when needed most | Low | Exponential backoff; surface clear error to user; allow fallback to copy-paste |
| `.docx` parsing edge cases (tables, embedded media) produce garbage text | Medium — bad initial translation | High | Use `python-docx` text-only extraction; document known limitations; copy-paste is always available as fallback |
| Firestore document grows past 1 MB for very long sermons | High — write fails silently | Low | Hard-cap sermons at 1000 segments in v1; surface error during ingestion; reserve subcollection migration path for v2 |
| Translation costs spike if users re-translate on every re-import | High — OpenAI bill | Low | Re-import never re-translates; it only overlays `reviewedTranslation`. Re-translation is an explicit separate action |
| Race condition: two admins import different files simultaneously for the same sermon | Medium — last-write-wins data loss | Low | Use Firestore transaction with `updatedAt` precondition; surface conflict error to second uploader |
| openpyxl + untrusted `.xlsx` security (zip bomb, XXE) | Medium — backend resource exhaustion | Low | Use `defusedxml`-backed openpyxl; cap file size at 5 MB; cap row count at 5000 |

---

## 6. Impact Analysis

> **Purpose**: List every existing consumer of the resources being changed.

### 6.1 Changed Resources

| Resource | Type | Change Description |
|----------|------|--------------------|
| `organizations/{orgId}` Firestore schema | DB schema | Add new subcollection `sermons/{sermonId}`; existing fields untouched |
| `organizations/{orgId}/services/{serviceKey}` | DB schema | Add optional field `linkedSermonId` |
| `firestore.rules` | Config | Add read rules for `sermons` subcollection (org members read-only) |
| `firestore.indexes.json` | Config | Likely no new composite indexes needed (sermon list = single field order by `updatedAt`) |
| `backend/app/main.py` | API | Mount new router for sermon CRUD + export + import |
| `backend/app/routes/` | API | New file `sermon_review.py` (or extend `sermons.py` if exists) |
| `backend/app/utils/translate.py` | Logic | Live translation: when a service has `linkedSermonId`, lookup reviewed text by segment match before falling back to machine output |
| `backend/app/services/multichurch_store.py` | Data layer | Add `create_sermon()`, `get_sermon()`, `update_sermon_reviews()`, `list_org_sermons()` |
| `frontend/components/SermonPrep.tsx` | UI | Add Export / Import buttons; possibly refactor to load from new sermon entity |
| New frontend pages | UI | Sermon list page; sermon detail page with export/import controls |
| `requirements.txt` | Deps | Add `openpyxl`, `python-docx`, and Google API client libs (`google-api-python-client`, `google-auth-oauthlib`) |

### 6.2 Current Consumers

| Resource | Operation | Code Path | Impact |
|----------|-----------|-----------|--------|
| `services/{serviceKey}` | READ | `multichurch_store.get_service()` and all callers | None — new optional field, defaults to null |
| `services/{serviceKey}` | UPDATE | Existing admin routes that edit service config | None — new field added at edit time only |
| `utils/translate.translate_text()` and friends | invoked from | `/ws/stt_deepgram` handler in `main.py` | Needs verification — must not regress when no sermon linked |
| Existing in-memory `script_store` (referenced by sermon-accuracy plan) | READ | translation pipeline | Needs verification — interaction with new persistent reviewed store must be defined in Design phase |
| `SermonPrep.tsx` | UI | `pages/admin/...` (sermon prep page) | Refactor required if we centralize sermon entity; backward-compatible deprecation path acceptable |

### 6.3 Verification

- [ ] All consumers of `services/{serviceKey}` schema verified to ignore new `linkedSermonId` field if absent
- [ ] No auth/permission changes break existing operations (existing routes unchanged)
- [ ] Live translation pipeline regression-tested with no sermon linked (must behave identically to today)
- [ ] Interaction between new persistent sermon entity and existing in-memory `script_store` documented in Design

---

## 7. Architecture Considerations

### 7.1 Project Level Selection

| Level | Characteristics | Recommended For | Selected |
|-------|-----------------|-----------------|:--------:|
| **Starter** | Simple structure | Static sites | ☐ |
| **Dynamic** | Feature-based modules, BaaS-style auth + DB | Web apps with backend, SaaS | ☑ |
| **Enterprise** | Strict layer separation, DI, microservices | High-traffic, complex | ☐ |

Project is already Dynamic (FastAPI + Next.js + Firestore + Firebase Auth + Stripe). This feature fits cleanly into existing module conventions.

### 7.2 Key Architectural Decisions

| Decision | Options | Selected | Rationale |
|----------|---------|----------|-----------|
| Sermon source ingestion | Google Docs only / file only / paste only / all three | **All three** | User explicitly requested all three for MVP; gives pastors flexibility |
| Google Docs integration approach | Picker API + Drive scope / paste URL + Docs API / paste URL + published-to-web | **Paste URL + Docs API (`documents.readonly`)** as default; Picker as optional enhancement | Lower scope; doesn't require Google Picker JS; works for any link the user can paste. Final decision deferred to Design phase. |
| Sermon storage shape | Single doc + `segments[]` array / subcollection per segment / hybrid | **Single doc + `segments[]` array** | Cost-optimized: 1 Firestore read per export, 1 write per import. Stays well under 1 MB doc limit for typical sermons. Subcollection migration reserved for >1000 segments. |
| Spreadsheet format | `.xlsx` / `.csv` / `.ods` | **`.xlsx`** | Best Google Sheets / Excel / Numbers compatibility; supports cell formatting and column visual cues |
| xlsx library (backend) | openpyxl / xlsxwriter / pylightxl | **openpyxl** | Reads and writes; mature; supports cell protection hints |
| `.docx` parser | python-docx / mammoth / pandoc | **python-docx** | Pure Python; sufficient for text-only extraction; no system dependencies |
| Translation re-use | Re-translate on import / overlay-only | **Overlay-only** | Re-import never calls OpenAI; cost control + idempotency |
| Linkage model | Sermon-per-service / sermon-per-org with link / standalone | **Sermon-per-org, optionally linked to one service at a time** | Allows sermon reuse across services (e.g., multiple campuses); avoids duplicating sermon data per service |
| Conflict resolution on concurrent import | Last-write-wins / `updatedAt` precondition transaction | **Transaction with `updatedAt` precondition** | Catches concurrent edits; surfaces 409 to second writer |
| API style | REST / WebSocket / GraphQL | **REST** (matches existing routes) | Existing project convention |

### 7.3 Folder Structure Preview

```
backend/app/
  routes/
    sermon_review.py        # NEW — sermon CRUD + export + import endpoints
  services/
    multichurch_store.py    # EXTEND — add sermon CRUD helpers
  sermon_review/            # NEW MODULE
    __init__.py
    ingest.py               # Google Docs / .txt / .docx / paste handlers
    xlsx_export.py          # openpyxl-based Review File generator
    xlsx_import.py          # openpyxl-based reader + validator
    validation.py           # Row-level validation rules
    models.py               # Pydantic models for Sermon + Segment

frontend/
  pages/
    admin/
      sermons/
        index.tsx           # NEW — sermon list
        [sermonId].tsx      # NEW — sermon detail + export/import
        new.tsx             # NEW — ingestion entry (3 options)
  components/
    SermonReviewControls.tsx  # NEW — export/import buttons + status
    SermonIngestForm.tsx      # NEW — 3-tab ingestion UI
    SermonPrep.tsx            # EXISTING — possibly deprecated or refactored

backend/firestore/
  firestore.rules           # EXTEND — sermons subcollection rules
```

---

## 8. Convention Prerequisites

### 8.1 Existing Project Conventions

- [x] `CLAUDE.md` has coding conventions section (verified)
- [ ] No `docs/01-plan/conventions.md`
- [ ] No project-root `CONVENTIONS.md`
- [x] ESLint configuration exists (verified — `npm run lint` is part of build)
- [x] TypeScript configuration exists
- [x] Python style: `from __future__ import annotations`, async routes, env vars via `os.getenv()` (per `CLAUDE.md`)

### 8.2 Conventions to Define/Verify

| Category | Current State | To Define | Priority |
|----------|---------------|-----------|:--------:|
| Spreadsheet column header naming | n/a | English-only in MVP; canonical header names locked in `xlsx_export.py` and re-validated on import | High |
| Segment ID format | n/a | Server-generated `S001`-style zero-padded order key; stable per sermon; not exposed to user | High |
| Error response shape on import | exists (FastAPI default) | Structured `{ status, summary: { total, imported, warned, errored }, rows: [...] }` | High |
| Pydantic model placement | partial (some routes use, some don't) | New sermon module uses Pydantic models throughout | Medium |
| Frontend file naming for new pages | exists | Kebab-case page paths, PascalCase components | Low (already established) |

### 8.3 Environment Variables Needed

| Variable | Purpose | Scope | To Be Created |
|----------|---------|-------|:-------------:|
| `GOOGLE_DOCS_OAUTH_CLIENT_ID` | OAuth client for Google Docs read | Server | ☑ |
| `GOOGLE_DOCS_OAUTH_CLIENT_SECRET` | OAuth client secret | Server | ☑ |
| `GOOGLE_DOCS_OAUTH_REDIRECT_URI` | OAuth callback URL | Server | ☑ |
| `SERMON_MAX_SEGMENTS` | Hard cap on segments per sermon (default 1000) | Server | ☑ |
| `SERMON_MAX_UPLOAD_BYTES` | Max `.xlsx` / `.docx` upload size (default 5 MB) | Server | ☑ |

### 8.4 Pipeline Integration

Not using the 9-phase Development Pipeline for this feature; following standard PDCA flow.

---

## 9. Next Steps

1. [ ] User approves this Plan
2. [ ] Run `/pdca design editing-sermon` to produce the Design document
   - Design will resolve the Google Docs integration approach (paste-URL vs Picker)
   - Design will specify exact Firestore document schema and indexes
   - Design will specify validation rule catalog and error/warning categorization
   - Design will specify the live broadcast pipeline integration point
3. [ ] Run `/pdca do editing-sermon` after Design approval
4. [ ] Run `/pdca analyze editing-sermon` post-implementation
5. [ ] Run `/pdca qa editing-sermon` for L1-L3 (and possibly L5 security) verification before shipping

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-06-16 | Initial draft from Checkpoints 1+2 | namju |
