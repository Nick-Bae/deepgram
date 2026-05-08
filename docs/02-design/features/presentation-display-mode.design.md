# presentation-display-mode Design Document

> **Summary**: In-app split-layout presentation mode — slide image (top ~70vh) + live translation subtitle (bottom ~30vh) on one URL, controlled from the existing host console, synced via WebSocket room broadcasts.
>
> **Project**: Real-Time Translation Platform
> **Version**: 0.1
> **Author**: namju
> **Date**: 2026-05-05
> **Status**: Draft
> **Planning Doc**: [presentation-display-mode.plan.md](../../01-plan/features/presentation-display-mode.plan.md)

---

## Context Anchor

> Copied from Plan document. Ensures strategic context survives Design→Do handoff.

| Key | Value |
|-----|-------|
| **WHY** | Churches can't cleanly combine PowerPoint full-screen with the translation display; projector setup is fragile and host-juggling during service is error-prone. |
| **WHO** | Host operators running a live service from `/host/c/[churchSlug]`; congregation watching the projector via `/display`; remote listeners on the same display URL. |
| **RISK** | Slide-state desync between host control and display (WebSocket disconnect mid-service); projector must continue showing the last known slide if the host loses connection. |
| **SUCCESS** | (1) Host uploads slides + advances them with no PowerPoint open; (2) display shows slide + subtitle in one screen; (3) slide change latency < 500ms; (4) slide cost < $0.05/church/month for in-room use. |
| **SCOPE** | Module 1: storage + upload. Module 2: split-layout display. Module 3: host control + WS sync. |

---

## 1. Overview

### 1.1 Design Goals

- Reuse the existing WebSocket room model (`socket_manager.broadcast_room`) for slide sync — no new transport.
- Reuse the existing data layer (`multichurch_store.py`) for slide metadata — slides naturally belong as a subcollection of `services/{serviceKey}`.
- Add a single new backend route module (`routes/slides.py`) for slide CRUD over HTTP.
- Add 4 focused frontend components (one per host concern + one per display concern) and 1 hook.
- Preserve every existing display.tsx behavior (subtitle / fullScreen modes, `f` toggle, `useSubtitleSocket` shape).

### 1.2 Design Principles

- **Additive, not invasive**: extend `display.tsx` and `[churchSlug].tsx` rather than fork them. The Slides tab on the host page is hidden when no slides exist.
- **One transport, one source of truth**: WebSocket broadcasts the live slide index; Firestore holds the durable index for late-joiners and reconnects.
- **Preserve aspect ratios always**: `object-fit: contain` and a fixed slide-area ratio. Letterboxing is acceptable; squashing is not.
- **Reuse existing role guards**: any host control endpoint reuses `require_org_role(["owner","admin","host"])` — no new auth code paths.
- **Cap cost at the edge**: client-side downscale on upload (max 1920px width) + server-side hard caps (10MB/image, 50 slides/service).

---

## 2. Architecture Options (v1.7.0)

### 2.0 Architecture Comparison

| Criteria | Option A: Minimal | Option B: Clean | Option C: Pragmatic |
|----------|:-:|:-:|:-:|
| **Approach** | Inline new logic into existing files | Full `presentation/` feature module with strict layers | New focused components + hooks; backend slide routes split out; reuse existing data-layer file |
| **New Files** | 0–1 | ~13 | ~6 |
| **Modified Files** | 6–7 | 5 | 5 |
| **Complexity** | Low upfront, higher coupling later | High upfront, lowest long-term | Medium |
| **Maintainability** | Medium — heavy files get heavier | High — fully isolated | High — clear seams without over-abstraction |
| **Effort** | 1–2 days | 4–5 days | 2–3 days |
| **Risk to existing code** | Higher | Lower | Lower |
| **Recommendation** | Quick prototype | Over-engineered for current scope | **Default choice** |

**Selected**: **Option C — Pragmatic Balance**.
**Rationale**: Matches the existing repo conventions (`components/` + `utils/` hooks + `routes/{name}.py`). Avoids introducing a `features/` folder pattern that would be the only one in the repo. Reuses `multichurch_store.py` (the established service-level Firestore entry point) for slide metadata, mirroring how rooms and members are stored.

### 2.1 Component Diagram

```
┌────────────────────────────────────────────────────────────────────┐
│                          Host Browser                              │
│  /host/c/[churchSlug]                                              │
│  ┌───────────────────────────────────────────────────────────┐     │
│  │ <SlidesPanel> tab                                         │     │
│  │  ├─ <SlideUploader>      ── HTTP ─┐                       │     │
│  │  ├─ <SlideThumbnailStrip>         │                       │     │
│  │  └─ NavControls (prev/next/jump)─┐│                       │     │
│  │     useSlideSync()  ◀─────WS─────┼┼───┐                   │     │
│  └───────────────────────────────────┼┼───┼───────────────────┘     │
└─────────────────────────────────────┼┼───┼─────────────────────────┘
                                       ││   │
                                       ▼│   │
┌────────────────────────────────────────────────────────────────────┐
│                       FastAPI Backend                              │
│   /api/services/{key}/slides         ◀ POST/GET/PATCH/DELETE       │
│   └─ routes/slides.py ─┐                                           │
│                        ▼                                           │
│   multichurch_store.py (extended: list/upsert/reorder slides,      │
│                          set_current_slide_index)                  │
│                                                                    │
│   socket_manager.py (extended: broadcast_slide_change)             │
│                                                                    │
│   /ws/translate ──── room broadcast ──┐                            │
└───────────────────────────────────────┼────────────────────────────┘
                                        │
                       ┌────────────────┼────────────────┐
                       ▼                ▼                ▼
                ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
                │   Display   │ │   Display   │ │   Display   │
                │ /display    │ │ /display    │ │ /display    │
                │ <Presenta-  │ │  ...        │ │  ...        │
                │  tionDisp-  │ │             │ │             │
                │  lay>       │ │             │ │             │
                └─────────────┘ └─────────────┘ └─────────────┘

Storage:
- Firebase Storage:  orgs/{orgId}/services/{serviceKey}/slides/{slideId}.{png|jpg}
- Firestore:         organizations/{orgId}/services/{serviceKey}/slides/{slideId}
                     organizations/{orgId}/services/{serviceKey}  (currentSlideIndex field)
```

### 2.2 Data Flow

**Upload flow:**
```
Host → <SlideUploader> → client-side resize (max 1920w)
     → POST /api/services/{key}/slides (multipart)
     → routes/slides.py validates (size, type, count cap)
     → upload bytes to Firebase Storage
     → multichurch_store.add_slide() writes Firestore doc {slideId, order, storagePath, contentType, width, height, createdBy}
     → 201 + { slide: {...} }
```

**Live navigation flow:**
```
Host clicks Next → useSlideSync.advance()
     → POST /api/services/{key}/slides/index { index: N }
     → multichurch_store.set_current_slide_index(orgId, serviceKey, N)
     → ConnectionManager.broadcast_slide_change(orgId, roomId, { type: "slide_change", index: N, slideId, url })
     → all connected display browsers receive via existing /ws/translate room
     → useSubtitleSocket detects type:"slide_change" → exposes currentSlide → <PresentationDisplay> swaps image
```

**Reconnect flow (display):**
```
Display socket drops → reconnects to /ws/translate
     → on connect, server immediately sends { type: "slide_state", index: N, slideId, url } from Firestore
     → display catches up to current slide without user action
```

### 2.3 Dependencies

| Component | Depends On | Purpose |
|-----------|-----------|---------|
| `<SlidesPanel>` | `useSlideSync`, `<SlideUploader>`, `<SlideThumbnailStrip>` | Host UI for upload + navigation |
| `<PresentationDisplay>` | `useSubtitleSocket` (extended) | Render slide + subtitle split layout |
| `useSlideSync` | Firebase Auth ID token, `/api/services/{key}/slides` REST | Host actions (advance, jump, list) |
| `useSubtitleSocket` (extended) | existing `/ws/translate` socket | Surface `slide_change` and `slide_state` events alongside translation events |
| `routes/slides.py` | `multichurch_store`, `socket_manager`, `auth.guards.require_org_role` | HTTP CRUD + broadcast trigger |
| `multichurch_store.py` (extended) | Firestore client, Firebase Storage admin | Slide CRUD + `currentSlideIndex` setter |
| `socket_manager.py` (extended) | existing `ConnectionManager` rooms | New `broadcast_slide_change(org_id, room_id, payload)` method |

---

## 3. Data Model

### 3.1 Slide Entity (Firestore)

Path: `organizations/{orgId}/services/{serviceKey}/slides/{slideId}`

```typescript
interface SlideDoc {
  slideId: string;          // Firestore doc id (uuid v4)
  order: number;            // 0-based ordinal within the deck
  storagePath: string;      // gs://bucket/orgs/{orgId}/services/{serviceKey}/slides/{slideId}.{ext}
  publicUrl?: string;       // CDN URL if visibility=public
  signedUrlExpiresAt?: number; // unix ms (when signed URL was last issued)
  contentType: "image/png" | "image/jpeg";
  byteSize: number;         // post-resize bytes
  width: number;            // pixels
  height: number;           // pixels
  caption?: string;         // optional alt text from host
  createdBy: string;        // Firebase uid
  createdAt: Timestamp;
  updatedAt: Timestamp;
}
```

Service document gets two new fields (additive — won't break existing reads):

```typescript
// organizations/{orgId}/services/{serviceKey}
interface ServiceDoc {
  // ...existing fields unchanged
  currentSlideIndex?: number;        // 0-based; undefined when no deck
  slidesVisibility?: "public" | "private"; // default "private" (signed URLs)
  slideCount?: number;               // denormalized for quick host-side checks
}
```

### 3.2 Entity Relationships

```
[Organization] 1 ── N [Service] 1 ── N [Slide]
                               1 ── 1 [currentSlideIndex (scalar)]
                               1 ── N [Room (existing, unchanged)]
```

### 3.3 Storage Layout (Firebase Storage)

```
gs://<bucket>/
└── orgs/
    └── {orgId}/
        └── services/
            └── {serviceKey}/
                └── slides/
                    ├── {slideId-1}.png
                    ├── {slideId-2}.jpg
                    └── ...
```

Caching headers on upload:
- `Cache-Control: public, max-age=3600` for `slidesVisibility=public`
- `Cache-Control: private, max-age=300` for `slidesVisibility=private` (signed URLs)

---

## 4. API Specification

### 4.1 Endpoint List

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | `/api/services/{serviceKey}/slides` | List all slides for a service (sorted by `order`) | Required (org member) |
| POST | `/api/services/{serviceKey}/slides` | Upload one or more slide images (multipart) | Required (host/admin/owner) |
| PATCH | `/api/services/{serviceKey}/slides/{slideId}` | Update single slide (caption, replace bytes) | Required (host/admin/owner) |
| DELETE | `/api/services/{serviceKey}/slides/{slideId}` | Delete a slide (and its Storage object) | Required (host/admin/owner) |
| PATCH | `/api/services/{serviceKey}/slides/order` | Bulk reorder; body `{ orderedSlideIds: string[] }` | Required (host/admin/owner) |
| POST | `/api/services/{serviceKey}/slides/index` | Set current slide index; triggers WS broadcast | Required (host/admin/owner) |
| GET | `/api/services/{serviceKey}/slides/state` | Get `{ currentIndex, slides[] }` for display reconnects | Optional (public, rate-limited) |

All paths derive `orgId` server-side from the authenticated session (or from the public `serviceKey` for the read-only `state` endpoint, matching existing public-listener behavior).

### 4.2 Detailed Specification

#### `POST /api/services/{serviceKey}/slides`

**Request**: `multipart/form-data` with one or more `files` parts (PNG or JPEG).

**Response (201 Created):**
```json
{
  "data": {
    "slides": [
      {
        "slideId": "9c2f...",
        "order": 0,
        "url": "https://firebasestorage.../...",
        "contentType": "image/png",
        "width": 1920,
        "height": 1080,
        "caption": null,
        "byteSize": 524288
      }
    ]
  }
}
```

**Errors:**
- `400 INVALID_FILE_TYPE` — non-image MIME
- `400 FILE_TOO_LARGE` — > `MAX_SLIDE_IMAGE_BYTES` (default 10MB)
- `400 SLIDE_LIMIT_EXCEEDED` — would exceed `MAX_SLIDES_PER_SERVICE` (default 50)
- `401 UNAUTHORIZED`
- `403 FORBIDDEN_ROLE` — caller is not host/admin/owner

#### `POST /api/services/{serviceKey}/slides/index`

**Request:**
```json
{ "index": 7, "roomId": "<active-room-id>" }
```

**Response (200 OK):**
```json
{ "data": { "currentSlideIndex": 7, "broadcastedTo": 4 } }
```

**Side effects:**
- Writes `currentSlideIndex=7` to `services/{serviceKey}` Firestore doc.
- Calls `ConnectionManager.broadcast_slide_change(orgId, roomId, payload)` where payload is:
  ```json
  { "type": "slide_change", "index": 7, "slideId": "9c2f...", "url": "https://..." }
  ```

**Errors:**
- `400 INDEX_OUT_OF_RANGE` — index < 0 or >= slide count
- `404 NO_ACTIVE_ROOM` — service has no active room (broadcast skipped, but Firestore index still updates)

#### `GET /api/services/{serviceKey}/slides/state`

Used by reconnecting displays. Returns the current index and slide URL list with cache headers (`Cache-Control: max-age=10` to throttle abuse).

**Response (200 OK):**
```json
{
  "data": {
    "currentSlideIndex": 7,
    "slides": [
      { "slideId": "...", "order": 0, "url": "...", "width": 1920, "height": 1080 },
      ...
    ]
  }
}
```

### 4.3 WebSocket Message Types (additive)

Extends the existing `/ws/translate` channel. `useSubtitleSocket` ignores unknown types today — these new types are surfaced to callers via additional return fields.

| Type | Direction | Shape | When sent |
|------|-----------|-------|-----------|
| `slide_change` | server → all display clients in room | `{ type, index, slideId, url }` | After `POST /slides/index` succeeds |
| `slide_state` | server → single client | `{ type, index, slideId, url }` | Immediately on display socket connect (if a deck exists for the room's service) |
| `slide_added` | server → host clients (optional, deferred) | `{ type, slideId }` | After upload — for live thumbnail refresh |

---

## 5. UI/UX Design

### 5.1 Screen Layout — Display (`/display`)

When `currentSlide?.url` is present and the new presentation mode is selected:

```
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│                                                              │
│              [ Slide image, object-fit: contain ]            │  ~70vh
│                                                              │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│            Live translation subtitle (max 2 lines)           │  ~30vh
│       (bg: linear-gradient #3b365f → #0f3a60, white text)    │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

When no slides uploaded → existing subtitle / fullScreen modes are used unchanged.

A new keyboard shortcut `p` toggles presentation mode (alongside existing `f` toggle); presentation mode is auto-selected when the WS first emits `slide_state` with a non-null index.

### 5.2 Screen Layout — Host Console Slides Tab

A new "Slides" tab/section appears in `/host/c/[churchSlug]` only when slides exist or when the operator clicks "+ Slides":

```
┌──────────────────────────────────────────────────────────────┐
│  [ Mic ]  [ Sermon Prep ]  [ Translation Box ]  [ Slides ]   │
├──────────────────────────────────────────────────────────────┤
│  ┌─────────────────────┐  Current slide:  3 / 24             │
│  │                     │  ┌──────────────────────────────┐   │
│  │   Slide preview     │  │ [◀ Prev]  [Next ▶]  [Jump…]  │   │
│  │   (current)         │  └──────────────────────────────┘   │
│  │                     │                                     │
│  └─────────────────────┘                                     │
│                                                              │
│  Thumbnails (horizontal scroll):                             │
│  [1][2][3*][4][5][6][7]...     [+ Upload]                    │
│                                                              │
│  Tip: ← / → keys advance slides while this tab is focused   │
└──────────────────────────────────────────────────────────────┘
```

### 5.3 User Flow

```
Host:  Open host console → Slides tab → Upload images → Reorder → Click Next/keyboard ←/→
Display:  Load /display → auto-detects slide_state → renders split layout → updates on slide_change
```

### 5.4 Component List

| Component | Location | Responsibility |
|-----------|----------|----------------|
| `<SlidesPanel>` | `frontend/components/SlidesPanel.tsx` | Top-level host slides tab; orchestrates upload, list, navigation |
| `<SlideUploader>` | `frontend/components/SlideUploader.tsx` | File picker + drag-drop, client-side resize, upload progress |
| `<SlideThumbnailStrip>` | `frontend/components/SlideThumbnailStrip.tsx` | Horizontal scroll of slides with current-index highlight, drag-to-reorder |
| `<PresentationDisplay>` | `frontend/components/PresentationDisplay.tsx` | Display-side split layout (slide top, subtitle bottom) |
| `useSlideSync` | `frontend/utils/useSlideSync.ts` | Hook: list slides, advance, jump, reorder; calls REST endpoints |
| `useSubtitleSocket` (extended) | `frontend/utils/useSubtitleSocket.ts` | Adds `currentSlide` and `slides` to its return type |

### 5.5 Page UI Checklist

#### Display page (`/display`) — slide mode

- [ ] Slide image visible in top region, object-fit contain, never cropped
- [ ] Subtitle area visible in bottom region, max 2 lines, white text on existing gradient
- [ ] Slide swap animation: 150ms cross-fade (no layout shift)
- [ ] Letterboxing: black bars on whichever axis doesn't fit (top+bottom for tall slides, left+right for wide slides)
- [ ] Keyboard `p` toggles presentation ↔ subtitle-only modes
- [ ] Keyboard `f` toggle still works exactly as before
- [ ] When slide URL fails to load (404/403): subtitle area still shows; slide area shows a small error placeholder
- [ ] When slide deck is empty: existing subtitle/fullScreen modes are presented (no presentation toggle shown)

#### Host console — Slides tab (`/host/c/[churchSlug]`)

- [ ] Tab labeled "Slides" with badge showing slide count
- [ ] Upload area: drag-drop OR click-to-select; accepts PNG/JPEG only
- [ ] Upload progress bar per file
- [ ] Client-side resize warning if image > 1920px width (auto-resize, show "resized to 1920px" toast)
- [ ] Hard error if file > 10MB or count > 50
- [ ] Thumbnail strip: shows all slides; current slide outlined in primary color
- [ ] Click thumbnail → jumps display to that slide
- [ ] Drag thumbnail → reorders deck (commits to backend on drop)
- [ ] Per-thumbnail context menu: Replace, Delete, Set caption
- [ ] Prev/Next buttons advance current slide
- [ ] ←/→ keyboard shortcuts advance current slide (only when tab focused)
- [ ] Indicator "3 / 24" updates immediately on local action; reverts if backend rejects
- [ ] Optimistic updates (UI moves first, rolls back on error)

#### Slide upload errors

- [ ] Wrong file type → "Only PNG or JPEG images are accepted."
- [ ] Too large → "Image must be under 10 MB."
- [ ] Too many → "This service already has 50 slides — delete some to add more."
- [ ] Network error → "Upload failed. Retry?"

---

## 6. Error Handling

### 6.1 Error Code Definition

| Code | Message | Cause | Handling |
|------|---------|-------|----------|
| 400 INVALID_FILE_TYPE | Only PNG/JPEG allowed | Wrong MIME | Client shows toast |
| 400 FILE_TOO_LARGE | Image must be under 10MB | byteSize > cap | Client shows toast |
| 400 SLIDE_LIMIT_EXCEEDED | Service slide limit reached | count > cap | Client shows toast |
| 400 INDEX_OUT_OF_RANGE | Slide index out of range | Malformed advance call | Client clamps locally and retries |
| 401 UNAUTHORIZED | Auth required | Missing/expired ID token | Existing auth refresh path |
| 403 FORBIDDEN_ROLE | Host/admin/owner required | Lower-role user | Hide UI affordances client-side |
| 404 SLIDE_NOT_FOUND | Slide does not exist | Stale cache | Refetch slide list |
| 404 NO_ACTIVE_ROOM | No active broadcast room | Index update without live room | Update Firestore only; no broadcast |
| 500 STORAGE_UPLOAD_FAILED | Image upload failed | Firebase Storage error | Client offers retry |

### 6.2 Display-side Error Handling

- Slide image `<img onError>` → render a centered low-key placeholder with the slide number. Subtitle continues working normally.
- Slide URL is a signed URL that expired mid-service → display reconnects to `/api/services/{key}/slides/state` and re-fetches a fresh URL.
- WebSocket disconnect → display continues showing last known slide; on reconnect, server emits `slide_state` to catch up.

---

## 7. Security Considerations

- [ ] **Auth on writes**: every write endpoint enforces `require_org_role(["owner","admin","host"])` (existing helper). No new auth code.
- [ ] **MIME enforcement**: `routes/slides.py` validates `content_type ∈ {"image/png","image/jpeg"}` and inspects file magic bytes; never trusts the client-provided MIME alone.
- [ ] **Size cap**: enforced both at upload (FastAPI) and at Firebase Storage rules (defense in depth).
- [ ] **Path scoping**: server constructs `orgs/{orgId}/services/{serviceKey}/slides/...` from authenticated session — client cannot specify destination path.
- [ ] **Visibility default = private**: signed URLs with 24h TTL by default; per-service `slidesVisibility="public"` flag exists but defaults off.
- [ ] **Firestore rules**: new `services/{serviceKey}/slides/*` rules — read = org member or public service, write = owner/admin/host. Existing rules untouched.
- [ ] **Firebase Storage rules**: `orgs/{orgId}/services/{serviceKey}/slides/*` — read per service visibility flag, write = owner/admin/host only.
- [ ] **Rate limiting**: `/slides/state` (public) inherits existing public rate limit (20 concurrent).
- [ ] **Input filename sanitization**: server generates `{slideId}.{png|jpg}` filenames; original filename is discarded.
- [ ] **No EXIF leakage**: server strips EXIF on upload using PIL/Pillow (`Image.save(..., exif=None)`).
- [ ] **CORS**: new HTTP routes inherit existing `CORS_ALLOW_ORIGINS` config from `main.py`.
- [ ] **Logging**: slide write actions logged via `security_event()` with actor uid, action, slideId.

---

## 8. Test Plan

### 8.1 Test Scope

| Type | Target | Tool | Phase |
|------|--------|------|-------|
| L1: API Tests | All `/slides` endpoints | curl + pytest httpx | Do |
| L2: UI Action Tests | SlidesPanel + PresentationDisplay | Playwright | Do |
| L3: E2E Scenario Tests | Upload → broadcast → display swap → reconnect | Playwright + WS mock | Do |

### 8.2 L1 — API Test Scenarios

| # | Endpoint | Method | Test Description | Expected Status | Expected Response |
|---|----------|--------|------------------|:---------------:|-------------------|
| 1 | `/services/{key}/slides` | GET | List slides as org member | 200 | `.data.slides[]` sorted by `order` |
| 2 | `/services/{key}/slides` | POST | Upload valid PNG as host | 201 | `.data.slides[0].slideId` exists |
| 3 | `/services/{key}/slides` | POST | Upload as viewer (lower role) | 403 | `.error.code = "FORBIDDEN_ROLE"` |
| 4 | `/services/{key}/slides` | POST | Upload `application/pdf` | 400 | `.error.code = "INVALID_FILE_TYPE"` |
| 5 | `/services/{key}/slides` | POST | Upload 11MB JPEG | 400 | `.error.code = "FILE_TOO_LARGE"` |
| 6 | `/services/{key}/slides` | POST | Upload when count=50 | 400 | `.error.code = "SLIDE_LIMIT_EXCEEDED"` |
| 7 | `/services/{key}/slides/{id}` | DELETE | Delete as host | 204 | Storage object also removed |
| 8 | `/services/{key}/slides/order` | PATCH | Reorder ids | 200 | `.data.slides[i].order = i` |
| 9 | `/services/{key}/slides/index` | POST | Set valid index | 200 | `.data.currentSlideIndex = N`; WS broadcast observed |
| 10 | `/services/{key}/slides/index` | POST | Index = -1 | 400 | `.error.code = "INDEX_OUT_OF_RANGE"` |
| 11 | `/services/{key}/slides/state` | GET | Public read | 200 | `.data.currentSlideIndex` present (no auth header) |
| 12 | unauthenticated POST | POST | Upload without token | 401 | `.error.code = "UNAUTHORIZED"` |

### 8.3 L2 — UI Action Test Scenarios

| # | Page | Action | Expected Result | Data Verification |
|---|------|--------|-----------------|-------------------|
| 1 | Host Slides tab | Drag-drop a PNG | Upload progress shown → thumbnail appears | New row in Firestore slides subcollection |
| 2 | Host Slides tab | Click thumbnail #3 | Display swaps to slide 3 within 500ms | WS message `slide_change index=2` observed |
| 3 | Host Slides tab | Press → key | currentSlideIndex increments; display updates | Same as #2 |
| 4 | Host Slides tab | Press ← at index 0 | No-op; no error | No WS message sent |
| 5 | Host Slides tab | Drag thumbnail to reorder | New order persists across reload | `PATCH /slides/order` observed |
| 6 | `/display` | Load with slides present | Split layout rendered (~70/30) | Slide URL fetched from state endpoint |
| 7 | `/display` | Press `p` | Toggles to subtitle-only; press again returns | No WS messages |
| 8 | `/display` | Slide URL 404 | Placeholder shown; subtitle unaffected | Console error logged once |

### 8.4 L3 — E2E Scenario Test Scenarios

| # | Scenario | Steps | Success Criteria |
|---|----------|-------|-----------------|
| 1 | Full service simulation | Login as host → upload 3 slides → start room → advance through deck → second tab shows live updates | All transitions < 500ms; no missed slides |
| 2 | Reconnect mid-service | Open display → host advances to slide 5 → kill display WS → reconnect | Display lands on slide 5 via `slide_state` |
| 3 | Permission boundary | Login as `viewer` → attempt POST `/slides` | 403; UI hides upload affordance |
| 4 | Cap enforcement | Upload 50 slides → attempt 51st | 400 with helpful message |
| 5 | Subtitle-only fallback | Service with no slides → load `/display` | Existing subtitle / fullScreen modes work; no regression |
| 6 | Reorder + advance | Upload 5 → swap slide 2 ↔ 4 → advance | Display matches new order |

### 8.5 Seed Data Requirements

| Entity | Minimum Count | Key Fields Required |
|--------|:-------------:|---------------------|
| Organization | 1 | `orgId`, plan |
| Service | 1 | `serviceKey` |
| Slides | 3 | `slideId`, `order`, `storagePath`, `contentType`, `byteSize` |
| Member (host role) | 1 | `uid`, `role="host"` |
| Member (viewer role) | 1 | for negative auth tests |

---

## 9. Clean Architecture (project-adapted)

This codebase is Dynamic-level Next.js + FastAPI without a `features/` or `domain/` layout. The "layer" mapping is:

### 9.1 Layer Structure

| Layer | Responsibility | Location (this codebase) |
|-------|---------------|--------------------------|
| **Presentation (frontend)** | UI components, hooks, pages | `frontend/components/`, `frontend/utils/`, `frontend/pages/` |
| **API (backend routes)** | Request validation, auth, response shaping | `backend/app/routes/slides.py` |
| **Domain/Service (backend)** | Firestore + Storage logic | `backend/app/services/multichurch_store.py` (extended) |
| **Infrastructure (backend)** | WebSocket transport, Firebase Admin SDK | `backend/app/socket_manager.py`, existing Firebase init |

### 9.2 File Import Rules

| From | Can Import | Cannot Import |
|------|-----------|---------------|
| `routes/slides.py` | `services/multichurch_store`, `socket_manager`, `auth.guards` | UI / pages |
| `services/multichurch_store` | Firestore client, Storage admin | `routes/`, `socket_manager` directly |
| Frontend hooks (`utils/`) | DOM, fetch, WS | Pages (avoid circular) |
| Frontend pages | components, utils | each other |

### 9.3 This Feature's Layer Assignment

| Component | Layer | Location |
|-----------|-------|----------|
| `<SlidesPanel>` | Presentation | `frontend/components/SlidesPanel.tsx` |
| `<SlideUploader>` | Presentation | `frontend/components/SlideUploader.tsx` |
| `<SlideThumbnailStrip>` | Presentation | `frontend/components/SlideThumbnailStrip.tsx` |
| `<PresentationDisplay>` | Presentation | `frontend/components/PresentationDisplay.tsx` |
| `useSlideSync` | Presentation hook | `frontend/utils/useSlideSync.ts` |
| `useSubtitleSocket` (extended) | Presentation hook | `frontend/utils/useSubtitleSocket.ts` |
| Slide CRUD methods | Domain/Service | `backend/app/services/multichurch_store.py` |
| `/api/services/.../slides*` routes | API | `backend/app/routes/slides.py` |
| `broadcast_slide_change` | Infrastructure | `backend/app/socket_manager.py` |

---

## 10. Coding Convention Reference

### 10.1 Naming Conventions (matches existing repo)

| Target | Rule | Example (this feature) |
|--------|------|------------------------|
| React components | PascalCase | `SlidesPanel`, `PresentationDisplay` |
| Hooks | camelCase, `use*` prefix | `useSlideSync` |
| Backend modules | snake_case | `slides.py` |
| FastAPI route handlers | snake_case | `async def list_slides(...)` |
| Firestore field names | camelCase | `currentSlideIndex`, `slidesVisibility` |
| WebSocket message types | snake_case strings | `"slide_change"`, `"slide_state"` |

### 10.2 Import Order

Frontend follows existing `useSubtitleSocket.ts` pattern:
```typescript
// 1. React + external
import { useEffect, useState } from "react";
// 2. Project utils
import { resolveStreamContext } from "./streamContext";
// 3. Types (inline)
type Slide = { slideId: string; ... };
```

Backend follows existing `routes/billing.py` pattern (stdlib → fastapi → app modules → services).

### 10.3 Environment Variables

| Variable | Purpose | Scope | Default |
|----------|---------|-------|---------|
| `MAX_SLIDE_IMAGE_BYTES` | Per-image upload cap | Server | 10485760 (10MB) |
| `MAX_SLIDES_PER_SERVICE` | Slide count cap | Server | 50 |
| `SLIDE_SIGNED_URL_TTL_SEC` | Signed URL TTL for private slides | Server | 86400 |
| `NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET` | Storage bucket reference | Client | (existing) |

### 10.4 This Feature's Conventions

| Item | Convention Applied |
|------|--------------------|
| Component naming | PascalCase, one component per file |
| Hook naming | `useSlideSync` (camelCase, `use*`) |
| State management | Local `useState` + custom hook (matches `useSubtitleSocket` pattern) |
| Error handling | Surface backend `error.code` to user as a toast/inline message; never silent |
| Optimistic UI | Slide-advance updates UI first, rolls back on backend rejection |

---

## 11. Implementation Guide

### 11.1 File Structure

```
frontend/
├─ components/
│  ├─ SlidesPanel.tsx           (NEW)
│  ├─ SlideUploader.tsx         (NEW)
│  ├─ SlideThumbnailStrip.tsx   (NEW)
│  └─ PresentationDisplay.tsx   (NEW)
├─ utils/
│  ├─ useSlideSync.ts           (NEW)
│  └─ useSubtitleSocket.ts      (extended)
└─ pages/
   ├─ display.tsx               (extended — slide mode toggle)
   └─ host/c/[churchSlug].tsx   (extended — Slides tab)

backend/
├─ app/
│  ├─ routes/
│  │  └─ slides.py              (NEW)
│  ├─ services/
│  │  └─ multichurch_store.py   (extended — slide CRUD)
│  └─ socket_manager.py         (extended — broadcast_slide_change)
└─ firestore/
   └─ firestore.rules           (extended)
```

### 11.2 Implementation Order

1. [ ] Backend: extend `multichurch_store.py` with `add_slide`, `list_slides`, `delete_slide`, `reorder_slides`, `set_current_slide_index` (no HTTP yet)
2. [ ] Backend: extend `socket_manager.py` with `broadcast_slide_change(org_id, room_id, payload)`; add `slide_state` emission on display socket connect
3. [ ] Backend: create `routes/slides.py` with all 7 endpoints; mount in `main.py:app.include_router(...)` with prefix `/api`
4. [ ] Backend: extend `firestore/firestore.rules` for `slides` subcollection; deploy to staging
5. [ ] Backend: extend Firebase Storage rules to enforce path + role + size
6. [ ] Frontend: create `useSlideSync` hook (REST CRUD + advance)
7. [ ] Frontend: extend `useSubtitleSocket` to surface `currentSlide`, `slides`, and `slide_*` events
8. [ ] Frontend: create `<SlideUploader>` (drag-drop + resize)
9. [ ] Frontend: create `<SlideThumbnailStrip>` (sortable list)
10. [ ] Frontend: create `<SlidesPanel>` (composes the above)
11. [ ] Frontend: integrate `<SlidesPanel>` as a tab inside `host/c/[churchSlug].tsx`
12. [ ] Frontend: create `<PresentationDisplay>` and integrate into `display.tsx` as a third mode
13. [ ] Frontend: add `p` key toggle in display.tsx (alongside existing `f`)
14. [ ] L1+L2 tests pass; L3 reconnect scenario manually verified
15. [ ] `npm run lint` clean; `npm run build` clean
16. [ ] Manual run-through of a full simulated service using a real PNG deck

### 11.3 Session Guide

> Auto-generated from Design structure. Use `/pdca do {feature} --scope module-N` per session.

#### Module Map

| Module | Scope Key | Description | Estimated Turns |
|--------|-----------|-------------|:---------------:|
| Backend storage + CRUD | `module-1-backend` | `multichurch_store` extensions, `routes/slides.py`, Firestore + Storage rules, `broadcast_slide_change` | 35–45 |
| Display split layout | `module-2-display` | `<PresentationDisplay>`, `useSubtitleSocket` extension, `display.tsx` integration, `p` toggle | 25–35 |
| Host control + upload | `module-3-host` | `<SlideUploader>`, `<SlideThumbnailStrip>`, `<SlidesPanel>`, `useSlideSync`, host page tab | 35–45 |
| Tests + cleanup | `module-4-tests` | L1 pytest, L2 Playwright, L3 reconnect scenario, lint pass | 20–30 |

#### Recommended Session Plan

| Session | Phase | Scope | Turns |
|---------|-------|-------|:-----:|
| Session 1 | Plan + Design | All | 30 (this session) |
| Session 2 | Do | `--scope module-1-backend` | 40 |
| Session 3 | Do | `--scope module-2-display` | 30 |
| Session 4 | Do | `--scope module-3-host` | 40 |
| Session 5 | Do | `--scope module-4-tests` | 25 |
| Session 6 | Check + Report | All | 30 |

> Sessions 2 and 3 can overlap — backend and display work share no files.

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-05-05 | Initial Design — Option C (Pragmatic) selected at Checkpoint 3 | namju |
