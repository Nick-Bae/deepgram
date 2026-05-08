---
template: design
version: 1.3
feature: help-center
date: 2026-05-08
author: namju
project: Worship Translation
projectVersion: 0.1.0
---

# help-center Design Document

> **Summary**: Public English `/help` page (single Next.js Pages Router file with a local `PanicCard` helper) plus inline "Help" link insertions in the host dashboard header and listener page corner. No backend, no shared component extraction beyond what's actually reused inside the page.
>
> **Project**: Worship Translation
> **Version**: 0.1.0
> **Author**: namju
> **Date**: 2026-05-08
> **Status**: Draft
> **Planning Doc**: [help-center.plan.md](../../01-plan/features/help-center.plan.md)

---

## Context Anchor

> Copied from Plan document. Ensures strategic context survives Design→Do handoff.

| Key | Value |
|-----|-------|
| **WHY** | No in-app self-service surface for Sunday-morning failures; `/contact` is async-only and not discoverable from the app. |
| **WHO** | Hosts mid-service (primary), new admins setting up (secondary), listeners with playback issues (tertiary). |
| **RISK** | Wrong/stale content actively misleads users mid-service. Mitigated by small v1 (panic only) and explicit "if this doesn't help, contact support" CTAs. |
| **SUCCESS** | `/help` reachable in ≤1 click from host dashboard and listener page; panic section answers 4 most-common Sunday issues with authored copy; lint+build pass; no mobile-layout regression. |
| **SCOPE** | 1 new page + 2 minor UI edits. No backend, no i18n, no CMS. |

---

## 1. Overview

### 1.1 Design Goals

- Deliver the help-center feature in **one focused PDCA cycle** with the smallest reasonable file count.
- Match the existing visual language of the app (cream `#ede5d8` page background, Manrope font, glass-card surfaces) without introducing a new design system.
- Make the panic-section content easy to maintain over time (data-driven cards, not hand-written 4×).
- Keep entry-point edits to the host header and listener page **non-invasive** — they must not destabilize layouts that already work.

### 1.2 Design Principles

- **YAGNI**: No `HelpSection` component, no shared `HelpLink`, no `data/help-content.ts` file. Per `CLAUDE.md`: "Three similar lines is better than a premature abstraction."
- **One leaf, one file**: `pages/help.tsx` owns its content, its layout, and one small co-located helper.
- **Inline styles match existing convention**: `contact.tsx` and `host/c/[churchSlug].tsx` both use inline style objects. We follow.
- **Honest scaffolding**: Sections 2–4 ship visibly incomplete with a "Coming soon" notice rather than placeholder lorem ipsum.
- **No JS work to render**: The page is essentially static markup; no client state, no fetches, no auth checks.

---

## 2. Architecture Options

### 2.0 Architecture Comparison

| Criteria | Option A: Minimal | Option B: Clean | Option C: Pragmatic |
|----------|:-:|:-:|:-:|
| **Approach** | Pure inline JSX, 4× repetition | 5 files, full extraction | 1 file with co-located `PanicCard` helper |
| **New Files** | 1 | 5 | 1 |
| **Modified Files** | 2 | 2 | 2 |
| **Complexity** | Low | High | Low–Medium |
| **Maintainability** | Medium | High | High |
| **Effort** | Low | High | Low |
| **Risk** | Low (some duplication) | Medium (premature abstraction) | Low |
| **Recommendation** | Hotfix-style ship | Long-term, growth to 20+ articles | **Default choice** |

**Selected**: **Option C — Pragmatic** — **Rationale**: Removes the only real duplication (4 panic cards) via one small co-located helper, while keeping file count low and matching `CLAUDE.md`'s "no premature abstraction" rule. The two link sites in host header and listener page need different visual treatments anyway, so a shared `HelpLink` would just take a `variant` prop without sharing real code.

### 2.1 Component Diagram

```
┌────────────────────────────────────────────────────────────────────┐
│ Browser (no auth needed for /help)                                 │
└────────────────────────────────────────────────────────────────────┘
       │
       │  GET /help
       ▼
┌────────────────────────────────────────────────────────────────────┐
│ Next.js Pages Router                                               │
│                                                                    │
│  pages/help.tsx              (NEW)                                 │
│    ├─ <head>: <title>, <meta>                                      │
│    ├─ Page header (logo + title + date marker)                     │
│    ├─ Table of contents (4 anchor links)                           │
│    ├─ Section 1: Sunday-morning panic                              │
│    │   └─ PanicCard ×4 (local helper, declared in same file)       │
│    │       props: { title, symptom, steps[], escalation }          │
│    ├─ Section 2: First-time setup    (intro + "Coming soon")       │
│    ├─ Section 3: Display / OBS / PPT (intro + "Coming soon")       │
│    ├─ Section 4: Billing & plans     (intro + "Coming soon")       │
│    └─ Footer CTA: "Still stuck? Contact support" → /contact        │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘

       Entry points (additive edits):
       ┌─────────────────────────────────────────────────────────────┐
       │ pages/host/c/[churchSlug].tsx                               │
       │   └─ inline <Link href="/help"> chip near LOGOUT            │
       ├─────────────────────────────────────────────────────────────┤
       │ pages/c/[churchSlug]/s/[serviceKey].tsx                     │
       │   └─ low-opacity fixed-position <Link href="/help">         │
       │      in bottom-right corner (does not occlude feed)         │
       └─────────────────────────────────────────────────────────────┘
```

### 2.2 Data Flow

```
None. /help is a static page with no API calls, no state, no fetches.
Render path = Next.js SSG → HTML → browser.
```

### 2.3 Dependencies

| Component | Depends On | Purpose |
|-----------|-----------|---------|
| `pages/help.tsx` | `next/link`, `next/head` | Routing + meta tags |
| `pages/help.tsx` | (nothing else) | No project-internal imports beyond Next.js primitives |
| Host header edit | `next/link` (already imported) | Help link |
| Listener page edit | `next/link` (already imported) | Help link |

**No new npm packages.** Manrope is already loaded via `_document.tsx`.

---

## 3. Data Model

Not applicable. `/help` has no persisted data.

The local `PanicCard` helper takes a typed prop shape that lives only in `help.tsx`:

```typescript
type PanicCardProps = {
  title: string;            // e.g., "Mic isn't picking up audio"
  symptom: string;          // one-sentence description of what the user sees
  steps: string[];          // ordered resolution steps
  escalation?: string;      // optional one-liner: "If still broken: …"
};
```

The 4 panic cases are declared as constant data inside `help.tsx`:

```typescript
const PANIC_CASES: PanicCardProps[] = [
  { title: "Mic isn't picking up audio", symptom: "...", steps: [...], escalation: "..." },
  { title: "Listener can't connect", symptom: "...", steps: [...] },
  { title: "Audio is silent on listener side", symptom: "...", steps: [...] },
  { title: "Translation feed is frozen", symptom: "...", steps: [...] },
];
```

This is local data; not extracted to a separate file (per Option C).

---

## 4. API Specification

Not applicable. No new API endpoints. No backend change.

The page links to:
- `/contact` (already exists at `frontend/pages/contact.tsx`) — unchanged.
- Section anchor IDs: `#panic`, `#setup`, `#display`, `#billing`.

---

## 5. UI/UX Design

### 5.1 Screen Layout

```
┌───────────────────────────────────────────────────────────────┐
│  Worship Translation · Help Center                            │
│  Last updated: 2026-05-08                                     │
├───────────────────────────────────────────────────────────────┤
│  Jump to:  [Sunday panic]  [Setup]  [Display]  [Billing]      │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│  ## Sunday-morning panic guide                                │
│                                                               │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ Mic isn't picking up audio                           │    │
│  │ Symptom: ...                                         │    │
│  │ 1. ...   2. ...   3. ...                             │    │
│  │ Still stuck? → Contact support                       │    │
│  └──────────────────────────────────────────────────────┘    │
│  ┌──────────────────────────────────────────────────────┐    │
│  │ Listener can't connect                               │    │
│  │ ...                                                  │    │
│  └──────────────────────────────────────────────────────┘    │
│  ┌── Audio is silent on listener side ──────────────────┐    │
│  └──────────────────────────────────────────────────────┘    │
│  ┌── Translation feed is frozen ────────────────────────┐    │
│  └──────────────────────────────────────────────────────┘    │
│                                                               │
│  ## First-time setup                                          │
│  Short intro paragraph.                                       │
│  ┃ Coming soon — full guide in progress                       │
│                                                               │
│  ## Display, OBS, projector, PPT subtitles                    │
│  Short intro.                                                 │
│  ┃ Coming soon                                                │
│                                                               │
│  ## Billing & plans                                           │
│  Short intro.                                                 │
│  ┃ Coming soon                                                │
│                                                               │
├───────────────────────────────────────────────────────────────┤
│  Still stuck?  [ Contact support ]                            │
└───────────────────────────────────────────────────────────────┘
```

**Width**: `max-width: 800px`, centered, gutter 24 px on mobile.
**Background**: `#ede5d8` (matches host dashboard).
**Card surface**: white-ish with subtle shadow (matches existing glass cards).
**Typography**: Manrope (already loaded). H2 = 24–28 px, body = 15–16 px, line-height 1.6.

### 5.2 User Flow

```
Host dashboard ──Click "Help" chip──▶ /help#panic ──Click section anchor──▶ scroll
Listener page ──Click corner Help link──▶ /help#panic
/help bottom CTA ──Click "Contact support"──▶ /contact (existing form)
```

### 5.3 Component List

| Component | Location | Responsibility |
|-----------|----------|----------------|
| `HelpPage` (default export) | `frontend/pages/help.tsx` | Top-level page, renders all sections |
| `PanicCard` (local helper) | inside `pages/help.tsx` | One panic case: title + symptom + numbered steps + optional escalation |
| Host header Help link | inline edit in `pages/host/c/[churchSlug].tsx` | Chip-style `<Link>` near LOGOUT |
| Listener corner Help link | inline edit in `pages/c/[churchSlug]/s/[serviceKey].tsx` | Low-opacity fixed-position `<Link>` in bottom-right corner |

### 5.4 Page UI Checklist

#### Help Page (`/help`)

- [ ] **Page title**: `<title>Help — Worship Translation</title>`
- [ ] **Page header**: H1 reading "Help Center", subtitle "Last updated: 2026-05-08"
- [ ] **Table of contents**: 4 anchor links — "Sunday panic", "First-time setup", "Display & OBS", "Billing", each linking to the corresponding section ID
- [ ] **Section 1 heading**: H2 "Sunday-morning panic guide" with `id="panic"`
- [ ] **Panic Card 1**: title="Mic isn't picking up audio", with symptom paragraph, ordered list of ≥3 resolution steps, escalation line
- [ ] **Panic Card 2**: title="Listener can't connect", same structure
- [ ] **Panic Card 3**: title="Audio is silent on the listener side", same structure
- [ ] **Panic Card 4**: title="Translation feed is frozen", same structure
- [ ] **Inline contact CTA**: After panic section, a "Still stuck? Contact support" button/link to `/contact`
- [ ] **Section 2 heading**: H2 "First-time setup" with `id="setup"`
- [ ] **Section 2 content**: One short intro paragraph + visible "Coming soon" notice
- [ ] **Section 3 heading**: H2 "Display, OBS, projector, PPT" with `id="display"`
- [ ] **Section 3 content**: One short intro paragraph + "Coming soon" notice
- [ ] **Section 4 heading**: H2 "Billing & plans" with `id="billing"`
- [ ] **Section 4 content**: One short intro paragraph + "Coming soon" notice
- [ ] **Footer CTA**: "Still stuck?" + button/link "Contact support" → `/contact`
- [ ] **Responsive**: Single column on viewports ≤640 px; max-width 800 px on desktop; no horizontal scroll at 360 px width

#### Host Dashboard Header (modified)

- [ ] **Help chip**: A `<Link href="/help">` styled as a compact chip (matching the LOGOUT button style), placed inside the user-menu cluster near LOGOUT
- [ ] **Existing layout preserved**: Tab bar, user info, LOGOUT all still render correctly on 360 px / 768 px / 1440 px

#### Listener Page (modified)

- [ ] **Corner Help link**: A small `<Link href="/help">` positioned `fixed; bottom: 12px; right: 12px;` with low opacity (~0.4) and a subtle hover state (opacity 1), `z-index` low enough to never overlap the modal/error states but high enough to be clickable
- [ ] **Existing feed unchanged**: Translation feed, recent-lines list, idle placeholder all render correctly with the corner link present

---

## 6. Error Handling

### 6.1 Error Cases

| Code | Cause | Handling |
|------|-------|----------|
| 404 | User navigates to `/help#unknown-anchor` | Browser handles gracefully — page still renders, anchor scrolls to top. No code needed. |
| Build error | Typo in TSX or unused import | Caught by `npm run build` and ESLint before merge. |
| Broken link to `/contact` | Accidental rename of `/contact` route | Verified by manual smoke test (FR-05); no automated check unless we add Playwright (deferred). |

### 6.2 Error Response Format

Not applicable. No API.

---

## 7. Security Considerations

- [x] **Input validation**: N/A — page renders only static content authored by us. No user input on `/help`.
- [x] **Authentication/Authorization**: Intentionally none — page is public (Plan §7.2 decision).
- [x] **Sensitive data**: None displayed. Help text references only feature names, not credentials, internal infra, or PII.
- [x] **HTTPS enforcement**: Inherited from existing project deploy (Vercel/Cloud Run already enforce HTTPS).
- [x] **Rate limiting**: N/A — static page, no server endpoint.
- [x] **External links**: The page contains zero external links. The only outbound link is internal (`/contact`).
- [x] **CSP**: Static markup with no inline scripts or external font additions (Manrope is already on the allowlist via `_document.tsx`).
- [x] **XSS**: All content is hard-coded JSX strings; no `dangerouslySetInnerHTML` anywhere.
- [x] **CSRF**: N/A — no form submissions on this page.

---

## 8. Test Plan

### 8.1 Test Scope

| Type | Target | Tool | Phase |
|------|--------|------|-------|
| L1: Build & Lint | Page compiles, ESLint passes, TypeScript types check | `npm run lint` + `npm run build` | Do |
| L2: Manual UI smoke | Visual + keyboard-nav check on 3 widths | Browser DevTools (manual) | Do |
| L3: Manual E2E flow | End-to-end click path | Browser (manual) | Do |

> Playwright is **not** installed in this project (verified during Plan phase). L2 / L3 are manual smoke tests for v1. Adding Playwright is deferred to a separate cycle.

### 8.2 L1: Build & Lint Test Scenarios

| # | Test | Command | Expected |
|---|------|---------|----------|
| 1 | ESLint clean on new page | `cd frontend && npx eslint pages/help.tsx` | exit 0, no errors |
| 2 | ESLint clean on edited host file | `npx eslint pages/host/c/[churchSlug].tsx` | exit 0, no errors |
| 3 | ESLint clean on edited listener file | `npx eslint pages/c/[churchSlug]/s/[serviceKey].tsx` | exit 0, no errors |
| 4 | TypeScript compiles | `npx tsc --noEmit` | exit 0 |
| 5 | Next.js build succeeds | `npm run build` | exit 0, no warnings about new files |

### 8.3 L2: Manual UI Smoke Test Scenarios

| # | Page | Action | Expected Result |
|---|------|--------|-----------------|
| 1 | `/help` (logged out) | Load page | All 4 sections visible; ToC anchors present; "Coming soon" notices visible on sections 2–4 |
| 2 | `/help` (logged in as host) | Load page | Same as #1 — auth state must not change rendering |
| 3 | `/help` desktop 1440 px | Load page | Max-width 800 px container, centered |
| 4 | `/help` mobile 360 px | Load page | Single column, no horizontal scroll, panic cards stack vertically |
| 5 | `/help` keyboard nav | Tab through links | All ToC anchor links and Contact CTA reachable via Tab; visible focus ring |
| 6 | `/help` anchor jump | Click "First-time setup" in ToC | Scrolls to `#setup` heading |
| 7 | Host dashboard | Visual check | Help chip visible in header, layout not broken on mobile |
| 8 | Host dashboard mobile 360 px | Resize | Tabs and Help chip do not visually collide; tabs may wrap but page remains usable |
| 9 | Listener page | Visual check on desktop | Corner Help link visible bottom-right at low opacity; hovering brightens it |
| 10 | Listener page mobile | Visual check | Corner link does not overlap the translation feed text |

### 8.4 L3: Manual E2E Scenario Test Scenarios

| # | Scenario | Steps | Success Criteria |
|---|----------|-------|------------------|
| 1 | Host clicks Help during setup | Open host dashboard → click Help chip → land on `/help#panic` (or top) → click "Contact support" → land on `/contact` | All transitions work; no 404; back button returns correctly |
| 2 | Listener clicks Help mid-service | Open listener page → click corner Help link → `/help` opens (same tab is fine) → can scroll to panic guide | No hijack of audio; corner link clickable on touch devices |
| 3 | Logged-out user reaches Help | Open `/help` directly without logging in | Page renders fully, no auth redirect, no console errors |
| 4 | ToC anchor flow | Open `/help` → click each ToC link in sequence → click "Contact support" at bottom | Each anchor scrolls to the right section; final CTA navigates to `/contact` |

### 8.5 Seed Data Requirements

None. `/help` is a static page with no DB dependency.

---

## 9. Clean Architecture

### 9.1 Layer Structure (this feature)

| Layer | Responsibility | Location |
|-------|---------------|----------|
| **Presentation** | Page render + co-located `PanicCard` helper | `frontend/pages/help.tsx` |
| **Application** | N/A | — |
| **Domain** | `PanicCardProps` type (declared inline in the page) | `frontend/pages/help.tsx` |
| **Infrastructure** | N/A | — |

### 9.2 Dependency Rules

```
pages/help.tsx ──→ next/link, next/head
                ──→ (nothing else internal)

The page does NOT import:
  - lib/authContext (no auth check needed)
  - lib/backendAuth (no API calls)
  - components/* (intentionally — Option C, no shared component)
```

### 9.3 File Import Rules

| From | Can Import | Cannot Import |
|------|-----------|---------------|
| `pages/help.tsx` | `next/link`, `next/head`, React | Auth context, backend client, anything API-related |
| Host header edit | `next/link` (already imported in file) | Anything new |
| Listener page edit | `next/link` (already imported in file) | Anything new |

### 9.4 This Feature's Layer Assignment

| Component | Layer | Location |
|-----------|-------|----------|
| `HelpPage` (default export) | Presentation | `frontend/pages/help.tsx` |
| `PanicCard` (local helper) | Presentation | inside `frontend/pages/help.tsx` |
| `PanicCardProps` type | Domain (trivial) | inside `frontend/pages/help.tsx` |

---

## 10. Coding Convention Reference

### 10.1 Naming Conventions (this feature)

| Target | Rule | Applied |
|--------|------|---------|
| Page default export | PascalCase | `HelpPage` |
| Local helper component | PascalCase | `PanicCard` |
| Type | PascalCase | `PanicCardProps` |
| Local data constants | UPPER_SNAKE_CASE | `PANIC_CASES` |
| File (page) | kebab-or-lowercase per Pages Router | `help.tsx` |

### 10.2 Import Order

Following existing pattern in `pages/contact.tsx`:

```typescript
// 1. External libraries
import Head from "next/head";
import Link from "next/link";

// 2. (none — no internal imports for this page)

// 3. (none)

// 4. Type imports
//    (none — types are declared inline)

// 5. (no styles file)
```

### 10.3 Environment Variables

None. The page reads zero env vars.

### 10.4 This Feature's Conventions

| Item | Convention Applied |
|------|-------------------|
| Component naming | PascalCase (`HelpPage`, `PanicCard`) |
| File organization | Single file in `pages/`; matches Pages Router convention |
| State management | None — pure static render |
| Error handling | N/A — no fetches or user input |
| Styling | Inline style objects (matches `contact.tsx`, `host/c/[churchSlug].tsx`) |
| Color palette | Reuse `#ede5d8` (page bg), `#22344c` (heading ink), `#5f6f86` (muted text), `#855763` (accent — same as logo wordmark) |

---

## 11. Implementation Guide

### 11.1 File Structure

```
frontend/
├── pages/
│   ├── help.tsx                                ← NEW (~250 LoC, includes PanicCard helper)
│   ├── contact.tsx                             ← unchanged (linked to)
│   ├── host/
│   │   └── c/
│   │       └── [churchSlug].tsx                ← MINOR EDIT (add Help chip in header cluster)
│   └── c/
│       └── [churchSlug]/
│           └── s/
│               └── [serviceKey].tsx            ← MINOR EDIT (add corner Help link)
```

### 11.2 Implementation Order

1. [ ] **Module 1 — Page scaffold**: Create `pages/help.tsx` with `<Head>`, page header, ToC, and four empty section shells. Verify route renders at `/help`.
2. [ ] **Module 2 — Panic content**: Define `PanicCardProps`, `PanicCard` helper, and `PANIC_CASES` data with authored copy for all 4 cases.
3. [ ] **Module 3 — Sections 2–4 + Contact CTA**: Add intro paragraphs and "Coming soon" notices for sections 2–4. Add "Still stuck? Contact support" CTA after panic section and at page bottom.
4. [ ] **Module 4 — Host header link**: Add Help chip to `pages/host/c/[churchSlug].tsx` near LOGOUT (around the user-menu cluster). Verify mobile layout still fits on 360 px.
5. [ ] **Module 5 — Listener corner link**: Add fixed-position corner Help link to `pages/c/[churchSlug]/s/[serviceKey].tsx`. Verify it does not occlude the feed.
6. [ ] **Module 6 — Lint + manual smoke**: `npm run lint && npm run build`, then walk the L2/L3 manual scenarios.

### 11.3 Session Guide

> Auto-generated from Implementation Guide structure. Session split is recommended, not required.

#### Module Map

| Module | Scope Key | Description | Estimated Turns |
|--------|-----------|-------------|:---------------:|
| Page scaffold | `module-1` | Create `pages/help.tsx` shell + `<Head>` + ToC + empty sections | 5–8 |
| Panic content | `module-2` | `PanicCard` helper + `PANIC_CASES` data + 4 authored cases | 8–12 |
| Sections 2–4 + CTA | `module-3` | Intro paragraphs, "Coming soon" notices, contact CTAs | 4–6 |
| Host header link | `module-4` | Add Help chip in host dashboard header | 3–5 |
| Listener corner link | `module-5` | Add fixed-position corner Help link in listener page | 3–5 |
| Lint + smoke | `module-6` | `npm run lint && npm run build`; manual L2/L3 walk-through | 3–5 |

#### Recommended Session Plan

| Session | Phase | Scope | Turns |
|---------|-------|-------|:-----:|
| Session 1 | Plan + Design | full | done |
| Session 2 | Do | `--scope module-1,module-2,module-3` (build the page end-to-end) | 18–25 |
| Session 3 | Do | `--scope module-4,module-5,module-6` (entry points + verification) | 10–15 |
| Session 4 | Check + Report | full | 10–15 |

> Single-session implementation is also feasible — total estimated 30–45 turns for all 6 modules.

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-05-08 | Initial draft. Architecture Option C selected. 1 new page + 2 minor entry-point edits. | namju |
