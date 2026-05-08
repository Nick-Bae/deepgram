---
template: plan
version: 1.3
feature: help-center
date: 2026-05-08
author: namju
project: Worship Translation
projectVersion: 0.1.0
---

# help-center Planning Document

> **Summary**: A public `/help` page giving pastors and church volunteers self-service answers (with a Sunday-morning panic guide as the priority section), plus a single "Help" link surfaced from the host dashboard header and the listener page footer. Replaces the prior idea of using Slack as the primary support channel.
>
> **Project**: Worship Translation
> **Version**: 0.1.0
> **Author**: namju
> **Date**: 2026-05-08
> **Status**: Draft

---

## Executive Summary

| Perspective | Content |
|-------------|---------|
| **Problem** | Pastors and volunteers have no self-service answers when something breaks (mic, listener URL, OBS) — especially Sunday morning when there's no time to email support. The only existing support surface is `/contact` (an async form), which is not discoverable from inside the app. |
| **Solution** | Add a public, English, static Next.js page at `/help` with four sections (Sunday panic guide as priority, plus first-time setup, display/OBS/PPT, billing — last three scaffolded). Surface a single "Help" link from the host dashboard header (next to LOGOUT) and the listener page footer. The existing `/contact` form stays as the escalation path, reachable via a CTA inside `/help`. |
| **Function/UX Effect** | Hosts in trouble during a service can read a panic guide in one click. New church admins find setup steps without emailing. Reduced inbound "how do I…" questions on the existing contact form. No new vendor, no new account requirement (no Slack/tawk/Crisp). |
| **Core Value** | Lower the cost of getting unstuck on Sunday morning. Make support feel built-in and trustworthy without committing to live-chat staffing the project can't sustain. |

---

## Context Anchor

> Auto-generated from Executive Summary. Propagated to Design/Do documents for context continuity.

| Key | Value |
|-----|-------|
| **WHY** | Sunday-morning failures (mic, listener URL, OBS, PPT subtitles) currently have no in-app self-service surface. `/contact` is async-only and not discoverable from the app. |
| **WHO** | Primary: church hosts/operators mid-service. Secondary: new church admins setting up for the first time. Tertiary: listeners with playback issues. |
| **RISK** | Stale or wrong content actively misleads users during a service (worse than no help at all). Mitigated by keeping v1 content small (panic section only) and scoping out community/live-chat surfaces. |
| **SUCCESS** | (1) `/help` reachable in ≤1 click from host dashboard and listener page; (2) Panic-guide section answers the 4 most-common Sunday issues with copy authored, not placeholder; (3) ESLint and Vercel build pass; (4) No regression to existing dashboard/listener layout on mobile widths. |
| **SCOPE** | Single PDCA cycle: 1 new page (`pages/help.tsx`), 1 host header edit (`pages/host/c/[churchSlug].tsx`), 1 listener footer edit (`pages/c/[churchSlug]/s/[serviceKey].tsx`). No backend, no i18n, no CMS. |

---

## 1. Overview

### 1.1 Purpose

Give users a self-service help surface inside the app so common problems (especially Sunday-morning panic situations) have a documented answer reachable in one click — without forcing them to open an email client, create a Slack account, or wait for an async reply.

### 1.2 Background

The product currently has only one support surface: `frontend/pages/contact.tsx`, a 711-line contact form with name/email/organization/topic/message + Turnstile + honeypot. The form works, but:

- It is **not linked from the host dashboard or listener page**, so users in trouble during a service can't easily find it.
- It is **async**: replies arrive by email, which is useless when a service is starting in 5 minutes.
- It mixes "I have a billing question" with "my mic isn't working right now," with no triage signal beyond a topic dropdown.

A prior evaluation (May 2026) considered Slack / tawk.to / Crisp / Help Scout as primary support channels. All were rejected for v1: Slack adds account friction for older pastors, live chat sets a staffing expectation a one-operator project can't meet, and additional vendors add privacy/CSP/cost surface. The chosen path is **a self-service Help Center first, the existing contact form as escalation, third-party tools deferred**.

### 1.3 Related Documents

- Existing contact form: `frontend/pages/contact.tsx`
- Host dashboard (header edit point): `frontend/pages/host/c/[churchSlug].tsx`
- Listener page (footer edit point): `frontend/pages/c/[churchSlug]/s/[serviceKey].tsx`
- Project conventions: `CLAUDE.md` (root)

---

## 2. Scope

### 2.1 In Scope

- [ ] New static page `frontend/pages/help.tsx` — public (no auth required).
- [ ] Page sections (in this priority order):
  - [ ] **Section 1 — Sunday-morning panic guide** (full real content): mic not picking up audio, listener can't connect, audio cuts out / silence on listener side, translation feed frozen.
  - [ ] **Section 2 — First-time setup** (short scaffold + "Coming soon"): create church → service → start broadcasting → share listener URL.
  - [ ] **Section 3 — Display / OBS / projector / PPT subtitles** (short scaffold + "Coming soon").
  - [ ] **Section 4 — Billing & plans** (short scaffold + "Coming soon").
- [ ] In-page table of contents at top with anchor links to each section.
- [ ] "Still stuck? Contact support" CTA at the end of the panic section and again at the bottom of the page, linking to `/contact`.
- [ ] Single "Help" link in the host dashboard header (placed next to LOGOUT in `[churchSlug].tsx`).
- [ ] Single "Help" link in the listener page footer (`[serviceKey].tsx`).
- [ ] Page metadata: `<title>Help — Worship Translation</title>`, basic description meta tag.
- [ ] Visual styling consistent with existing pages: cream `#ede5d8` background, Manrope, glass cards (matches host dashboard tone).
- [ ] Responsive layout (single-column on mobile, max-width container on desktop).
- [ ] ESLint clean.

### 2.2 Out of Scope

- Live chat widget (tawk.to, Crisp, Intercom, etc.).
- Slack workspace, Discord, or any community space.
- KakaoTalk Open Chat integration.
- Korean translation / i18n toggle (English only for v1).
- Search inside `/help`.
- CMS-backed articles (content lives in the `.tsx` file as plain JSX for v1).
- Screenshot upload on the existing `/contact` form (separate future change).
- Floating "Need Help?" button on every page (just header/footer links for v1).
- Help link on `frontend/pages/display.tsx` (the projector/OBS surface is intentionally chrome-less).
- Help link on auth pages (`/login`, `/signup`, `/auth/action`) — they already have minimal chrome.
- Embedded videos / animated GIFs (text + static screenshots only if any).
- Analytics on which articles are read.
- "Was this helpful?" feedback widgets.

---

## 3. Requirements

### 3.1 Functional Requirements

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| FR-01 | `/help` is accessible without authentication (public page). | High | Pending |
| FR-02 | `/help` renders four sections with stable anchor IDs (`#panic`, `#setup`, `#display`, `#billing`). | High | Pending |
| FR-03 | The Sunday-morning panic section contains real, written troubleshooting copy for at least these 4 cases: mic not picking up audio, listener can't connect, audio silence on listener side, translation feed frozen. | High | Pending |
| FR-04 | Sections 2–4 render with a short intro paragraph plus a visible "Coming soon — full guide in progress" notice, so they are honest about being incomplete. | Medium | Pending |
| FR-05 | A "Still stuck? Contact support" link/button appears (a) at the end of the panic section and (b) at the bottom of the page. Both link to `/contact`. | High | Pending |
| FR-06 | The host dashboard header (`pages/host/c/[churchSlug].tsx`) contains a single "Help" link, visually grouped with LOGOUT, that opens `/help` in the same tab. | High | Pending |
| FR-07 | The listener page (`pages/c/[churchSlug]/s/[serviceKey].tsx`) contains a footer with a "Help" link to `/help`. The footer must not interfere with the existing translation feed UI. | High | Pending |
| FR-08 | A skip-to-content / table-of-contents anchor list appears at the top of `/help` so users can jump to the relevant section in one click. | Medium | Pending |
| FR-09 | Page works without JavaScript beyond what Next.js requires (no client-side state, no fetches) — it must render correctly on a stale or slow connection during a service. | Medium | Pending |
| FR-10 | No regression: the host dashboard header layout still fits on a 360-wide mobile viewport after the Help link is added; the listener page's existing layout still works after the footer is added. | High | Pending |

### 3.2 Non-Functional Requirements

| Category | Criteria | Measurement Method |
|----------|----------|-------------------|
| Performance | Initial render ≤ 100 ms on a typical laptop; no client fetches; total page weight < 50 KB beyond shared bundles. | DevTools Network tab; Lighthouse. |
| Accessibility | All section headings are real `<h2>`/`<h3>` (not styled divs); anchor links work with keyboard; color contrast ≥ 4.5:1 for body text on the page background. | Manual keyboard pass; axe DevTools spot check. |
| Responsiveness | Page is readable on viewports from 360 px to 1440 px wide. No horizontal scroll. | Manual browser resize. |
| Discoverability | A user on the host dashboard or listener page reaches `/help` in exactly one click. | Manual test. |
| Maintenance | Content lives in plain JSX inside `pages/help.tsx`. Editing one section requires editing one file, no build pipeline beyond `npm run dev`. | Code review. |
| Lint/Build | `npm run lint` passes with zero errors; `npm run build` succeeds. | `npm run lint && npm run build`. |

---

## 4. Success Criteria

### 4.1 Definition of Done

- [ ] `frontend/pages/help.tsx` exists and renders all four sections.
- [ ] Sunday-morning panic section has authored copy (FR-03) — not placeholder.
- [ ] Sections 2–4 have intro + "Coming soon" notice (FR-04).
- [ ] "Help" link added to host dashboard header (FR-06).
- [ ] "Help" link added to listener page footer (FR-07).
- [ ] Both contact CTAs in `/help` link correctly to `/contact` (FR-05).
- [ ] Anchor links from the table of contents jump to the right sections (FR-02, FR-08).
- [ ] `npm run lint` clean; `npm run build` succeeds.
- [ ] Manual smoke test on desktop + mobile widths (FR-10).

### 4.2 Quality Criteria

- [ ] No new dependencies added to `frontend/package.json`.
- [ ] No new env vars required.
- [ ] No backend route changes.
- [ ] Page works while logged out and while logged in (no auth gate, no auth-dependent UI).
- [ ] Edits to `[churchSlug].tsx` and `[serviceKey].tsx` are minimal and localized — no refactor of unrelated code.

---

## 5. Risks and Mitigation

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Wrong or stale troubleshooting copy actively misleads a host mid-service. | High | Medium | Keep v1 panic content small (4 cases), grounded in actual product behavior; add a date/version marker at the top of `/help`; explicit "If this doesn't help, contact support" CTA after every panic case. |
| The "Help" link crowds the host header and pushes the tab bar to wrap on small screens. | Medium | Medium | Use the same compact chip style as LOGOUT; verify on 360 px viewport before merging (FR-10); fall back to icon-only if needed. |
| Listener page footer interferes with the translation feed (e.g., covers text on small screens or is mistaken for content). | Medium | Low | Place footer below the feed in document order with clear visual separation; do not use `position: fixed`; test on mobile. |
| Users expect Korean content (large Korean church user base) and bounce when they see English-only. | Medium | Medium | Document English-only as a v1 trade-off; plan Korean translation as a follow-up cycle; keep copy short and use simple words. |
| Adding a public `/help` page raises an SEO/scraping surface that wasn't there before. | Low | Low | Static content only, no PII, no internal-only info; standard `<meta>` tags; if needed later, gate via robots.txt — not a v1 concern. |
| ESLint failure blocks Vercel deployment (per CLAUDE.md). | High | Low | Run `npm run lint` locally before commit; follow existing inline-style pattern from `contact.tsx` (no new lint rules introduced). |
| Future content edits become messy because content is hard-coded JSX. | Low | Medium | Acceptable for v1 (one operator, low edit volume). If content grows beyond ~10 sections, reconsider with a separate cycle (CMS or MDX). |

---

## 6. Impact Analysis

> Every existing consumer of the resources being changed.

### 6.1 Changed Resources

| Resource | Type | Change Description |
|----------|------|--------------------|
| `frontend/pages/help.tsx` | New page | New file, public route, no auth, no API calls. |
| `frontend/pages/host/c/[churchSlug].tsx` | UI edit | Add a single "Help" link in the header region near LOGOUT (around the user-menu / logout area, ~line 2440). |
| `frontend/pages/c/[churchSlug]/s/[serviceKey].tsx` | UI edit | Add a footer block below the existing layout containing a "Help" link. |
| `frontend/pages/_document.tsx` | None expected | No font additions needed (Manrope already loaded). |

### 6.2 Current Consumers

| Resource | Operation | Code Path | Impact |
|----------|-----------|-----------|--------|
| `pages/host/c/[churchSlug].tsx` header | READ (rendered) | All host dashboard users on every tab (Live Broadcast, Church Settings, Billing, Team) | Needs verification — header layout must still fit on mobile after the new link is added. |
| `pages/c/[churchSlug]/s/[serviceKey].tsx` | READ (rendered) | All listeners visiting any active service URL | Needs verification — new footer must not collide with the existing translation feed component on small screens. |
| `pages/contact.tsx` | READ (linked to) | Currently linked from one location (the existing support hint visible in the host page). | None — this plan adds *more* inbound links to `/contact`, no API or form change. |
| Next.js routing | New route `/help` | None previously. | None — additive. |

### 6.3 Verification

- [ ] Header layout verified on 360 px / 768 px / 1024 px / 1440 px widths after the Help link is added.
- [ ] Listener page verified to still render the translation feed correctly on mobile after the footer is added.
- [ ] `/contact` still works after additional inbound links (no schema or form change, but smoke-test it).
- [ ] `/help` renders correctly while logged out, logged in (host), and logged in (super_admin).
- [ ] No new ESLint rule violations after edits to `[churchSlug].tsx` and `[serviceKey].tsx`.

---

## 7. Architecture Considerations

### 7.1 Project Level Selection

| Level | Characteristics | Recommended For | Selected |
|-------|-----------------|-----------------|:--------:|
| **Starter** | Simple structure | Static sites, portfolios, landing pages | ☐ |
| **Dynamic** | Feature-based modules, BaaS integration | Web apps with backend, SaaS MVPs, fullstack apps | ☑ |
| **Enterprise** | Strict layer separation, DI, microservices | High-traffic systems, complex architectures | ☐ |

This feature lives within the existing **Dynamic-level** Next.js Pages Router app. No level change.

### 7.2 Key Architectural Decisions

| Decision | Options | Selected | Rationale |
|----------|---------|----------|-----------|
| Routing | Pages Router (`pages/help.tsx`) / App Router (`app/help/page.tsx`) | **Pages Router** | Project standard per `CLAUDE.md`. App Router would be inconsistent with every other page. |
| Page type | Static (`getStaticProps` not even needed) / SSR | **Static** | No data fetching; pure JSX renders. |
| Content storage | Inline JSX / MDX / CMS | **Inline JSX** | One file, no new toolchain. Acceptable while content fits one page. |
| Styling | Inline style objects (matches existing pages) / Tailwind utility classes / CSS Modules | **Inline style objects** | Matches the dominant pattern in `host/c/[churchSlug].tsx` and `contact.tsx`. Tailwind 4 is available but mixing styles would inflate review surface. |
| Auth | Public / Auth-required | **Public** | Confirmed in Checkpoint 2. A pastor with a broken login or a listener with no account still benefits. |
| Internationalization | English only / EN+KO toggle / Next.js i18n routing | **English only** | Confirmed in Checkpoint 2. Korean follows in a separate cycle. |
| Header link strategy | "Help" only / "Help" + "Contact" | **"Help" only** | Confirmed in Checkpoint 2. Contact CTA lives inside `/help` to keep the host header compact. |
| Footer placement (listener) | Inline footer block / `position: fixed` overlay | **Inline footer below feed** | Avoids covering content on mobile. |

### 7.3 Clean Architecture Approach

```
frontend/
├── pages/
│   ├── help.tsx                                 ← NEW (this plan)
│   ├── contact.tsx                              ← unchanged (linked to from /help)
│   ├── host/c/[churchSlug].tsx                  ← MINOR EDIT (add header Help link)
│   └── c/[churchSlug]/s/[serviceKey].tsx        ← MINOR EDIT (add footer Help link)
└── components/                                  ← (not used in v1; if /help grows,
                                                    extract a HelpSection component)
```

No new module, no new directory. The page is a leaf.

---

## 8. Convention Prerequisites

### 8.1 Existing Project Conventions

- [x] `CLAUDE.md` has coding conventions section (Pages Router, ESLint required, Manrope font, env-var rules)
- [ ] `docs/01-plan/conventions.md` (not present — relying on CLAUDE.md)
- [ ] `CONVENTIONS.md` at project root (not present)
- [x] ESLint configuration (`.eslintrc.*` — present in `frontend/`)
- [ ] Prettier configuration (project-wide — not enforced)
- [x] TypeScript configuration (`frontend/tsconfig.json`)

### 8.2 Conventions to Define/Verify

| Category | Current State | To Define | Priority |
|----------|---------------|-----------|:--------:|
| **Naming** | exists (camelCase components, kebab-case file paths in pages) | none new | — |
| **Folder structure** | exists (Pages Router) | none new | — |
| **Inline style pattern** | exists (per `contact.tsx`, `host/c/[churchSlug].tsx`) | follow same `{ background, color, padding, borderRadius }` style | High |
| **Anchor / heading pattern** | none formalized | use real `<h2 id="panic">`, `<h3>` for sub-items | Medium |
| **Environment variables** | exists | none new (no env vars for /help) | — |

### 8.3 Environment Variables Needed

None. `/help` is a static page with no API calls, no env reads.

### 8.4 Pipeline Integration

Not applicable for a single-page Help Center addition. Phase 1 (Schema) and Phase 2 (Convention) are already in place at the project level.

---

## 9. Next Steps

1. [ ] Run `/pdca design help-center` to produce the Design document with 3 architecture options (Minimal / Clean / Pragmatic) and pick one.
2. [ ] Author the Sunday-morning panic copy (FR-03) — list the 4 cases and the resolution steps for each.
3. [ ] Run `/pdca do help-center` to implement.
4. [ ] Manual smoke test on mobile and desktop widths.
5. [ ] Run `/pdca analyze help-center` for gap analysis.
6. [ ] `/pdca report help-center` on completion.

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-05-08 | Initial draft. Public `/help` page, EN-only, panic-section content authored, other 3 sections scaffolded. Single "Help" link in host header and listener footer. | namju |
