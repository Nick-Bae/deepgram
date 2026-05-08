# presentation-display-mode Planning Document

> **Summary**: In-app presentation mode that displays uploaded slide images on the top half of a single screen with the live translation subtitle on the bottom — eliminating the need to run PowerPoint and the translation display in separate windows during worship services.
>
> **Project**: Real-Time Translation Platform
> **Version**: 0.1
> **Author**: namju
> **Date**: 2026-05-05
> **Status**: Draft

---

## Executive Summary

| Perspective | Content |
|-------------|---------|
| **Problem** | Churches must run PowerPoint full-screen on one window and the translation display on another, making projector setup awkward and forcing host operators to manage two surfaces during a live service. PowerPoint's full-screen mode cannot share the screen with another app cleanly. |
| **Solution** | An in-app Presentation Display Mode: hosts upload slide images (PNG/JPG) per service, the display page renders slide-on-top + translation-subtitle-on-bottom in a single full-screen layout, and slide navigation is synced over the existing WebSocket room. |
| **Function/UX Effect** | One projector URL, one keyboard shortcut, one host-controlled flow. The host advances slides from the existing host console; the projector display reflects changes instantly with the live translation subtitle below. No external presentation software required. |
| **Core Value** | Churches get an integrated worship-display surface — sermon slides + live Korean→English translation in one view — strengthening the platform's identity from "translation tool" to "worship presentation platform." |

---

## Context Anchor

> Auto-generated from Executive Summary. Propagated to Design/Do documents for context continuity.

| Key | Value |
|-----|-------|
| **WHY** | Churches can't cleanly combine PowerPoint full-screen with the translation display; projector setup is fragile and host-juggling during service is error-prone. |
| **WHO** | Host operators running a live service from `/host/c/[churchSlug]`; congregation watching the projector via `/display`; remote listeners on the same display URL. |
| **RISK** | Slide-state desync between host control and display (WebSocket disconnect mid-service); projector must continue showing the last known slide if the host loses connection. |
| **SUCCESS** | (1) Host uploads slides + advances them with no PowerPoint open; (2) display page shows slide + translation subtitle in one screen; (3) slide change latency < 500ms; (4) slide cost < $0.05/church/month for in-room use. |
| **SCOPE** | Phase 1: image upload + storage; Phase 2: split-layout display + subtitle integration; Phase 3: host control surface (next/prev/jump/keyboard) + WS broadcast sync. |

---

## 1. Overview

### 1.1 Purpose

Eliminate the need for PowerPoint during worship services on this platform. Provide a built-in presentation surface that combines uploaded slides with the live translation subtitle in a single projector-ready display.

### 1.2 Background

Today, churches running a service with this platform must:
1. Open PowerPoint (or Keynote) in a separate window/screen
2. Open `/display` for the translation subtitle
3. Manually arrange the two on the projector

PowerPoint's "Browsed by an individual window" mode allows resizing but is fragile — closing it accidentally, or the slideshow grabbing focus, can disrupt service. Some churches solve this with OBS, but OBS adds operational complexity that's not realistic for volunteer operators.

The platform already has the harder pieces:
- `/display` rendering live translations from `/ws/translate`
- A WebSocket room model in `socket_manager.py` that broadcasts to listeners
- A host console at `/host/c/[churchSlug]` with auth and service context
- Firestore service-level data per `organizations/{orgId}/services/{serviceKey}`

Adding a slide layer reuses all of these primitives.

### 1.3 Related Documents

- Existing display page: `frontend/pages/display.tsx`
- Host console: `frontend/pages/host/c/[churchSlug].tsx`
- WebSocket connection manager: `backend/app/socket_manager.py`
- Firestore data layer: `backend/app/services/multichurch_store.py`
- Firestore rules: `backend/firestore/firestore.rules`
- Subtitle hook: `frontend/utils/useSubtitleSocket.ts`

---

## 2. Scope

### 2.1 In Scope

- [ ] Per-service slide image storage in Firebase Storage at `orgs/{orgId}/services/{serviceKey}/slides/{n}.{png|jpg}`
- [ ] Firestore metadata document per service: ordered slide list + current slide index
- [ ] Slide upload UI on the host console (new "Slides" tab/section on `/host/c/[churchSlug]`)
- [ ] Reorder, replace, delete uploaded slides
- [ ] Slide-aware display mode on `/display`: slide-top (~70vh) + subtitle-bottom (~30vh)
- [ ] Aspect-ratio-preserving slide rendering (`object-fit: contain`)
- [ ] Host slide navigation: prev/next buttons, ←/→ keyboard shortcuts, jump-to-slide picker, current slide indicator
- [ ] WebSocket room broadcast of slide change events to all display clients
- [ ] Backwards-compatible toggle: subtitle-only mode still works when no slides uploaded
- [ ] Display state durability: last slide index persisted to Firestore so a reconnecting display catches up

### 2.2 Out of Scope

- Automatic PPT/PPTX → image conversion (deferred — host exports to PNG/JPG manually)
- PDF upload (deferred to a v1.1 if requested)
- Slide annotations, drawing, laser pointer
- Speaker notes view
- Multi-presenter handoff (only one host controls slides at a time)
- Per-slide translation overrides or slide-specific glossaries
- Slide library reuse across services (each upload is per-service for MVP)
- Animation/transition effects between slides

---

## 3. Requirements

### 3.1 Functional Requirements

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| FR-01 | Host can upload one or more PNG/JPG images for a given service from the host console | High | Pending |
| FR-02 | Uploaded images are stored in Firebase Storage with deterministic per-service paths | High | Pending |
| FR-03 | Each service has a Firestore document listing slide order and `currentSlideIndex` | High | Pending |
| FR-04 | Host can reorder slides via drag handles (or up/down arrows) | High | Pending |
| FR-05 | Host can delete or replace an individual slide | High | Pending |
| FR-06 | Display page renders the current slide (top ~70vh) + live translation subtitle (bottom ~30vh) when slides are present | High | Pending |
| FR-07 | Display falls back gracefully to the existing subtitle-only modes when no slides are uploaded | High | Pending |
| FR-08 | Host advances slides via Next/Prev buttons and ←/→ keyboard shortcuts | High | Pending |
| FR-09 | Host can jump to any slide via a thumbnail strip or slide-number picker | Medium | Pending |
| FR-10 | Slide changes broadcast over the existing WebSocket room reach all connected displays in <500ms | High | Pending |
| FR-11 | A display joining mid-service receives the current slide index from Firestore on connect | High | Pending |
| FR-12 | Slide image URLs delivered to the display use signed URLs or public CDN URLs with cache headers (≥ 1 hour) | High | Pending |
| FR-13 | Only org members with `host`, `admin`, or `owner` roles can upload/control slides (Firestore rules + backend guard) | High | Pending |
| FR-14 | Display URL remains public/listener-readable (consistent with existing display behavior) | Medium | Pending |
| FR-15 | Slide ratio is preserved on the display (`object-fit: contain`); letterboxed black bars are acceptable | High | Pending |
| FR-16 | Host UI shows current slide number ("3 / 24") and a small live preview of the next slide | Medium | Pending |

### 3.2 Non-Functional Requirements

| Category | Criteria | Measurement Method |
|----------|----------|-------------------|
| Performance | Slide change roundtrip (host click → display swap) < 500ms on broadband | Manual stopwatch + WS log timestamps |
| Performance | Slide image initial paint < 1s for ≤2MB images on broadband | Browser DevTools Network tab |
| Performance | Display memory stable across a 3-hour service (no leak from slide swaps) | Chrome DevTools Memory snapshot before/after |
| Security | Only authenticated host/admin/owner can upload or change `currentSlideIndex` | Firestore rules unit test + backend route guard |
| Security | Uploaded files restricted to image/png and image/jpeg, max 10MB each | Backend Multer/FastAPI validation + Storage rules |
| Cost | Per-church monthly storage cost < $0.10 for typical usage (30 slides × 4 services/mo) | GCS billing report |
| Cost | Per-service image egress < 50MB for in-room projector-only viewing | Storage egress logs |
| Accessibility | Slide images carry `alt` text equal to the filename or host-provided caption | Manual axe DevTools audit |
| Reliability | If WebSocket drops, display continues showing the last slide and reconnects automatically | Manual WS-disconnect test |

---

## 4. Success Criteria

### 4.1 Definition of Done

- [ ] All High-priority FRs implemented and manually verified against a real PNG slide deck
- [ ] Host can run a complete simulated service (upload → advance → translate) without opening PowerPoint
- [ ] Display reconnect after network drop resumes at the correct slide
- [ ] Firestore rules deny slide writes from non-host accounts (rules unit test passes)
- [ ] `npm run lint` passes in `frontend/`
- [ ] `npm run build` succeeds in `frontend/`
- [ ] Backend has no new failing endpoints under existing test runs
- [ ] Documentation: README/CLAUDE.md updated with the new Storage path and slide-control endpoints

### 4.2 Quality Criteria

- [ ] Slide change latency p50 < 300ms, p95 < 500ms (measured locally)
- [ ] No unhandled error boundary in the display when slides are missing or 403/404
- [ ] No regression in the existing subtitle-only `/display` mode (key `f` toggle still works)
- [ ] No regression in the existing host console flows (mic, sermon prep, translation box)

---

## 5. Risks and Mitigation

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Slide image upload bloats Firestore Storage cost if host accidentally uploads huge images | Medium | Medium | Enforce 10MB/image and 50 slides/service caps; surface a warning above 5MB; consider client-side downscale to max 1920px width on upload |
| WebSocket disconnect during service leaves display stuck on a stale slide | High | Medium | Display polls Firestore `currentSlideIndex` every 10s as a fallback; reconnect logic re-syncs on connect |
| Host advances slides faster than translation; subtitle area shrinks visually under heavy text | Medium | Medium | Cap subtitle area max-height; long subtitle text scrolls within its band, slide area never shrinks below 60vh |
| Slide upload occupies the only available WebSocket connection slot for a host | Low | Low | Uploads use HTTP, not WS; unrelated to WS connection budget |
| Firebase Storage public URLs leak slide content beyond the church | Medium | Low | Use signed URLs with 24-hour expiry for non-public slides; allow per-service `visibility: public|private` flag (default private) |
| Aspect ratio of slides differs from 16:9 and breaks layout | Medium | High | `object-fit: contain` lets letterboxing handle this; design tested with 4:3 and 16:9 sample decks |
| Adding a slide tab to the host console clutters an already heavy page | Medium | Medium | Hide the tab if no slides are uploaded; show a small "+ Slides" affordance in the toolbar instead |
| New Firestore rules accidentally over-restrict existing read paths | High | Low | Rules changes scoped to a new `slides` subcollection only; existing rules untouched; deploy via `firebase deploy --only firestore:rules` after staging test |

---

## 6. Impact Analysis

> **Purpose**: List every existing consumer of the resources being changed.

### 6.1 Changed Resources

| Resource | Type | Change Description |
|----------|------|--------------------|
| `frontend/pages/display.tsx` | Page component | Add slide-aware split layout mode (preserves existing subtitle-only modes) |
| `frontend/pages/host/c/[churchSlug].tsx` | Page component | Add a Slides tab/section with upload + control UI |
| `frontend/utils/useSubtitleSocket.ts` | WS hook | Extend to surface slide-change events alongside translation events |
| `backend/app/socket_manager.py` | WS room manager | Add `broadcast_slide_change(roomId, slideIndex)` method |
| `backend/app/main.py` | FastAPI app | Add HTTP routes for slide upload (`POST /services/{key}/slides`), list (`GET`), reorder (`PATCH`), delete (`DELETE`); broadcast slide-change handler |
| `backend/app/services/multichurch_store.py` | Firestore data layer | Add `slides` subcollection CRUD methods + `currentSlideIndex` setter |
| `backend/firestore/firestore.rules` | Firestore rules | Add rules for `services/{serviceKey}/slides/*` (host/admin/owner write; public read for slide metadata) |
| Firebase Storage bucket | New path | New `orgs/{orgId}/services/{serviceKey}/slides/` prefix |

### 6.2 Current Consumers

| Resource | Operation | Code Path | Impact |
|----------|-----------|-----------|--------|
| `display.tsx` | READ (rendering) | Listener browsers via `/display` | Needs verification — must not break existing subtitle/fullScreen toggle |
| `useSubtitleSocket` | READ | `display.tsx` and any TranslationBox consumer | Needs verification — extend the return shape additively, do not change existing fields |
| `[churchSlug].tsx` host page | READ/WRITE | Host operators | Needs verification — added tab must not break mic/sermon-prep flows |
| `socket_manager.py` | broadcast_room | Translation broadcast in `main.py` WebSocket handlers | Needs verification — new method is additive, existing broadcast unaffected |
| `multichurch_store.py` services CRUD | READ | Many routes (billing, host, admin) | None — new subcollection methods are additive |
| `firestore.rules` | All client reads/writes | All Firebase clients | Needs verification — confirm new rules don't override existing service rules |

### 6.3 Verification

- [ ] All consumers listed above verified to work with the proposed changes
- [ ] No auth/permission changes break existing operations (host/admin/owner role guards reused as-is)
- [ ] No field additions/removals break existing queries or mutations (slides live in a new subcollection)

---

## 7. Architecture Considerations

### 7.1 Project Level Selection

| Level | Characteristics | Recommended For | Selected |
|-------|-----------------|-----------------|:--------:|
| **Starter** | Simple structure | Static sites | ☐ |
| **Dynamic** | Feature-based modules, BaaS integration | Web apps with backend, SaaS | ☑ |
| **Enterprise** | Strict layer separation, DI, microservices | High-traffic systems | ☐ |

**Rationale**: This is a Dynamic-level fullstack feature on an existing Dynamic-level codebase (Next.js + FastAPI + Firebase). No new architecture tier needed.

### 7.2 Key Architectural Decisions

| Decision | Options | Selected | Rationale |
|----------|---------|----------|-----------|
| Slide storage backend | Firestore docs (base64) / Firebase Storage / GCS direct | Firebase Storage | Cheapest for binary; existing Firebase project; simple SDK from frontend |
| Storage scope | per-room / per-service / per-org library | per-service | Reusable across recurring services without org-wide library complexity |
| Sync mechanism | WebSocket broadcast / Firestore listener / Both | WebSocket broadcast | Reuses proven `/ws/translate` path; lowest latency; no extra Firestore reads |
| Sync durability fallback | None / Firestore poll / WS-only with reconnect resync | Firestore on reconnect resync | Cheap, only reads on reconnect; no continuous polling |
| Host control surface | New tab on host console / separate URL / both | New tab on host console | One operator, one workflow; matches existing host-console convention |
| Slide upload format (MVP) | PNG/JPG / + PDF / + PPTX | PNG/JPG only | Zero backend conversion; ship fastest; PowerPoint can export to JPEG natively |
| Slide ordering | Numeric index / explicit order field / linked-list | Numeric `order` integer field | Simple to render; reorder = rewrite indices in batch |
| Public vs private slides | Always public / always private / per-service flag | Per-service flag (default private with signed URLs) | Some churches share publicly; some prefer private; flag is one extra Firestore field |
| Image processing on upload | None / client-side resize / server-side resize | Client-side resize to max 1920px width | Cheapest (no backend processing cost); fast; predictable |
| Frontend state for slides | useState / Context / Zustand | useState + WS hook | Matches existing pattern in display.tsx; no new dependency |

### 7.3 Clean Architecture Approach

```
Selected Level: Dynamic

Folder Structure (additions):
┌─────────────────────────────────────────────────────┐
│ frontend/                                           │
│   pages/host/c/[churchSlug].tsx (modified)          │
│   pages/display.tsx (modified)                      │
│   components/SlidesPanel.tsx (NEW)                  │
│   components/SlideUploader.tsx (NEW)                │
│   components/SlideThumbnailStrip.tsx (NEW)          │
│   components/PresentationDisplay.tsx (NEW)          │
│   utils/useSlideSync.ts (NEW)                       │
│   utils/useSubtitleSocket.ts (extended)             │
│ backend/                                            │
│   app/routes/slides.py (NEW)                        │
│   app/services/multichurch_store.py (extended)      │
│   app/socket_manager.py (extended)                  │
│   firestore/firestore.rules (extended)              │
└─────────────────────────────────────────────────────┘
```

---

## 8. Convention Prerequisites

### 8.1 Existing Project Conventions

- [x] `CLAUDE.md` has coding conventions section (Pages Router, Firebase Auth, Tailwind 4, lint clean)
- [ ] `docs/01-plan/conventions.md` exists — N/A
- [ ] `CONVENTIONS.md` exists — N/A
- [x] ESLint configuration — exists in `frontend/`
- [x] Prettier configuration — implicit via ESLint
- [x] TypeScript configuration — `frontend/tsconfig.json`

### 8.2 Conventions to Define/Verify

| Category | Current State | To Define | Priority |
|----------|---------------|-----------|:--------:|
| **Naming** | Existing (PascalCase components, camelCase utils) | Reuse | Low |
| **Folder structure** | `pages/`, `components/`, `utils/`, `lib/` | New components in `components/`, hooks in `utils/` | Low |
| **Import order** | Existing | Reuse | Low |
| **Environment variables** | Existing | Add `NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET` if missing | Medium |
| **Error handling** | Existing | Slide load errors → fallback to subtitle-only mode | Medium |

### 8.3 Environment Variables Needed

| Variable | Purpose | Scope | To Be Created |
|----------|---------|-------|:-------------:|
| `NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET` | Firebase Storage bucket reference | Client | Verify existing |
| `MAX_SLIDE_IMAGE_BYTES` | Server-side upload size cap (default 10MB) | Server | ☑ |
| `MAX_SLIDES_PER_SERVICE` | Server-side slide-count cap (default 50) | Server | ☑ |
| `SLIDE_SIGNED_URL_TTL_SEC` | Signed URL TTL for private slides (default 86400) | Server | ☑ |

### 8.4 Pipeline Integration

Not using the 9-phase Development Pipeline; this feature follows the existing PDCA cycle.

---

## 9. Next Steps

1. [ ] User review and approval of this Plan document
2. [ ] Run `/pdca design presentation-display-mode` to produce the Design document with 3 architecture options (Minimal / Clean / Pragmatic)
3. [ ] Capture a Design Anchor for the slide+subtitle layout (Pencil MCP if available, otherwise inline mock)
4. [ ] Implementation in modules: Module 1 (storage + upload), Module 2 (display layout), Module 3 (host control + WS sync)
5. [ ] Gap analysis (`/pdca analyze`) and QA pass before merge

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-05-05 | Initial draft following Checkpoints 1+2 confirmation | namju |
