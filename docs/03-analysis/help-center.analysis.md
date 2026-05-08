---
template: analysis
version: 1.3
feature: help-center
date: 2026-05-08
author: namju
project: Worship Translation
projectVersion: 0.1.0
---

# help-center Analysis Report

> **Analysis Type**: Gap Analysis (Design vs Implementation) — static, no runtime
>
> **Project**: Worship Translation
> **Version**: 0.1.0
> **Analyst**: namju
> **Date**: 2026-05-08
> **Design Doc**: [help-center.design.md](../02-design/features/help-center.design.md)

---

## Context Anchor

| Key | Value |
|-----|-------|
| **WHY** | No in-app self-service surface for Sunday-morning failures; `/contact` is async-only and not discoverable. |
| **WHO** | Hosts mid-service (primary), new admins (secondary), listeners with playback issues (tertiary). |
| **RISK** | Wrong/stale content actively misleads users mid-service. Mitigated by small v1 + explicit "if this doesn't help, contact support" CTAs. |
| **SUCCESS** | `/help` reachable in ≤1 click from host dashboard and listener page; panic section answers 4 most-common Sunday issues with authored copy; lint+build pass; no mobile-layout regression. |
| **SCOPE** | 1 new page + 2 minor UI edits. No backend, no i18n, no CMS. |

---

## Strategic Alignment Check

### PRD Alignment

No PRD was generated for this feature (small scope; PM phase skipped per `/pdca plan` flow). Strategic alignment derives from Plan §1.1 + Context Anchor.

| Element | Expected | Implementation Status |
|---------|----------|:---------------------:|
| Core Problem (WHY) | In-app self-service for Sunday-morning failures | ✅ Addressed — `/help` route exists and is reachable from host header + listener corner |
| Target User (WHO) | Hosts (mid-service), new admins, listeners | ✅ Addressed — page is public (no auth), entry points cover both host and listener surfaces |
| Value Proposition | Lower the cost of getting unstuck on Sunday morning | ✅ Delivered structurally — panic guide is the first section, content is real (not placeholder) for all 4 cases. Caveat: copy accuracy unverified by domain owner. |

### Success Criteria Status

| # | Criterion (from Plan §4.1 + §4.2) | Status | Evidence |
|---|-----------------------------------|:------:|----------|
| SC-1 | `/help` reachable in ≤1 click from host dashboard | ✅ | `pages/host/c/[churchSlug].tsx:2434` — `<Link href="/help">` |
| SC-2 | `/help` reachable in ≤1 click from listener page | ✅ | `pages/c/[churchSlug]/s/[serviceKey].tsx:540` — fixed-position corner `<Link href="/help">` |
| SC-3 | Panic section has authored copy for 4 cases | ✅ | `pages/help.tsx:32-81` — `PANIC_CASES` array, 4 entries each with title, symptom, ≥3 steps, escalation |
| SC-4 | Lint passes | ✅ | `npx eslint pages/help.tsx pages/host/c/[churchSlug].tsx pages/c/[churchSlug]/s/[serviceKey].tsx` — exit 0, no output |
| SC-5 | Build / typecheck succeeds | ⚠️ | `npx tsc --noEmit` clean for this feature's 3 files (`grep` confirms 0 errors). Full-project `tsc` shows 1 unrelated pre-existing error in `components/PresentationDisplay.tsx` (untracked, in-progress separate feature). `npm run build` not executed because of that pre-existing error. |
| SC-6 | No mobile-layout regression at 360 px | ⚠️ | **Cannot be verified statically.** Requires manual smoke test (Design §8.3 row 8). |
| SC-7 | All functional requirements implemented | ✅ | See FR table below — 10/10 implemented |
| SC-8 | No new dependencies added | ✅ | `git diff frontend/package.json` — no changes |
| SC-9 | No new env vars required | ✅ | `pages/help.tsx` reads zero `process.env.*`; entry-point edits unchanged in env-var usage |
| SC-10 | No backend route changes | ✅ | `git diff backend/` — no help-center-related changes |
| SC-11 | Page works logged out and logged in | ✅ | `pages/help.tsx` does not import `lib/authContext`; renders identically in both states |
| SC-12 | Edits to `[churchSlug].tsx` and `[serviceKey].tsx` are minimal and localized | ✅ | Listener page: +28 LoC (one import + one Link block at end of `<main>`). Host page: Help link addition is +9 LoC (the 57-line stat in `git diff` includes unrelated worship-wordmark CSS-text replacement from earlier in the same uncommitted session) |

**Success Rate**: 10/12 fully met (✅), 2/12 partial (⚠️) — both partials are verification gaps, not implementation gaps.

### Decision Record Verification

| Source | Decision | Followed? | Deviation |
|--------|----------|:---------:|-----------|
| [Plan] | Architecture Option C (Pragmatic): 1 file + co-located helper | ✅ | None |
| [Plan] | Public page (no auth) | ✅ | `pages/help.tsx` has no auth check |
| [Plan] | English only for v1 | ✅ | All text English; no i18n setup added |
| [Plan] | Real content for panic; "Coming soon" for others | ✅ | `PANIC_CASES` has authored copy; sections 2–4 use `ComingSoon` helper |
| [Plan] | Single "Help" link (not Help+Contact) in host header | ✅ | One `<Link href="/help">` chip near LOGOUT |
| [Design] | Inline JSX content (no `data/help-content.ts`) | ✅ | `PANIC_CASES` constant lives inside `pages/help.tsx` |
| [Design] | Inline style objects (no Tailwind) | ✅ | All styles are inline objects; matches `contact.tsx` pattern |
| [Design] | Listener corner link, not full footer | ✅ | `position: fixed; bottom: 12px; right: 14px;` — preserves immersive feed |
| [Design] | Listener link low opacity, brightens on hover | ✅ | `pages/c/[churchSlug]/s/[serviceKey].tsx:551-565` — opacity 0.55 → 1 via `onMouseEnter`/`onMouseLeave` |

**No deviations from any decision record.**

---

## 1. Analysis Overview

### 1.1 Analysis Purpose

Verify that the help-center implementation matches the Plan and Design documents before declaring the Do phase complete, identify any structural gaps, and surface manual-verification work for the user.

### 1.2 Analysis Scope

- **Plan Document**: `docs/01-plan/features/help-center.plan.md`
- **Design Document**: `docs/02-design/features/help-center.design.md`
- **Implementation Files**:
  - `frontend/pages/help.tsx` (new, 295 LoC)
  - `frontend/pages/host/c/[churchSlug].tsx` (modified — Help chip added)
  - `frontend/pages/c/[churchSlug]/s/[serviceKey].tsx` (modified — corner Help link added)
- **Analysis Date**: 2026-05-08
- **Verification mode**: Static (no Playwright in repo, no API to curl, no DB to seed)

---

## 2. Gap Analysis (Design vs Implementation)

### 2.1 API Endpoints

Not applicable. Feature has no backend changes.

### 2.2 Data Model

Not applicable. No persistent data; only the local `PanicCardProps` type and `PANIC_CASES` constant inside `pages/help.tsx`.

### 2.3 Component Structure

| Design Component | Implementation File | Status |
|------------------|---------------------|:------:|
| `HelpPage` (default export) | `frontend/pages/help.tsx:184` | ✅ Match |
| `PanicCard` (local helper) | `frontend/pages/help.tsx:83` | ✅ Match |
| `ComingSoon` (local helper, not in Design but justified) | `frontend/pages/help.tsx:121` | ✅ Match (additive, matches Design intent for sections 2–4) |
| Host header Help chip | `frontend/pages/host/c/[churchSlug].tsx:2433-2440` | ✅ Match |
| Listener corner Help link | `frontend/pages/c/[churchSlug]/s/[serviceKey].tsx:538-565` | ✅ Match |

> Note: `ComingSoon` was not enumerated in Design §5.3 component list but is a trivial local helper (~14 LoC) used 3× to satisfy FR-04. This is consistent with Design §1.2 ("local co-located helpers are allowed under Option C"). Not flagged as a deviation.

### 2.4 Functional Depth Analysis

| File | Depth Score | Placeholder Indicators | Missing Design Elements |
|------|:-----------:|------------------------|------------------------|
| `pages/help.tsx` | 95 | None — no `lorem ipsum`, no `TBD` strings rendered to user. Internal `// TODO: verify` comment block at line 30 is a code comment, not user-visible content. | None |
| `pages/host/c/[churchSlug].tsx` (Help chip only) | 100 | None | None |
| `pages/c/[churchSlug]/s/[serviceKey].tsx` (corner link only) | 100 | None | None |

> Scoring: 0=empty, 20=skeleton, 40=mock, 60=partial, 80=mostly complete, 100=fully implemented. The `pages/help.tsx` score is 95 (not 100) because sections 2–4 intentionally show "Coming soon" — this is per-design-spec, not a gap. The 5-point reduction reflects the genuine content gap that the Plan/Design explicitly schedules for a follow-up cycle.

**Shallow File Count**: 0 / 3 files (0%)

### 2.5 Page UI Checklist Verification

> Cross-references Design §5.4 against `pages/help.tsx` and the two edited files.

#### Help Page (`/help`) — Design §5.4

| # | Element | Implemented | Evidence |
|---|---------|:-----------:|----------|
| 1 | Page title `<title>Help — Worship Translation</title>` | ✅ | `pages/help.tsx:189` |
| 2 | H1 "Help Center" + "Last updated: 2026-05-08" subtitle | ✅ | `pages/help.tsx:207-211` (`LAST_UPDATED` constant on L5) |
| 3 | Table of contents with 4 anchor links | ✅ | `pages/help.tsx:215-220` — `<nav aria-label="Table of contents">` with 4 `<a>` chips |
| 4 | Section 1 H2 "Sunday-morning panic guide" with `id="panic"` | ✅ | `pages/help.tsx:222-223` |
| 5 | Panic Card 1: "Mic isn't picking up audio" | ✅ | `PANIC_CASES[0]`, rendered via `<PanicCard>` |
| 6 | Panic Card 2: "Listener can't connect" | ✅ | `PANIC_CASES[1]` |
| 7 | Panic Card 3: "Audio is silent on listener side" | ✅ | `PANIC_CASES[2]` (title: "Translation appears but listeners hear silence") |
| 8 | Panic Card 4: "Translation feed frozen" | ✅ | `PANIC_CASES[3]` |
| 9 | Inline contact CTA after panic section | ✅ | `pages/help.tsx:233-235` |
| 10 | Section 2 H2 "First-time setup" with `id="setup"` | ✅ | `pages/help.tsx:239-240` |
| 11 | Section 2 intro + "Coming soon" notice | ✅ | `pages/help.tsx:241-245` |
| 12 | Section 3 H2 "Display / OBS / PPT" with `id="display"` | ✅ | `pages/help.tsx:249-250` |
| 13 | Section 3 intro + "Coming soon" notice | ✅ | `pages/help.tsx:251-255` |
| 14 | Section 4 H2 "Billing & plans" with `id="billing"` | ✅ | `pages/help.tsx:259-260` |
| 15 | Section 4 intro + "Coming soon" notice | ✅ | `pages/help.tsx:261-264` |
| 16 | Footer CTA with "Still stuck?" + Contact button | ✅ | `pages/help.tsx:268-291` |
| 17 | Responsive single-column on viewports ≤640 px | ⚠️ | Implemented (`max-width: 800px`, `display: grid`, no fixed widths > 800) but **not manually verified** |

**Help Page sub-rate**: 16/17 verified (94%) — 1 item requires manual smoke

#### Host Dashboard Header — Design §5.4

| # | Element | Implemented | Evidence |
|---|---------|:-----------:|----------|
| 18 | Help chip `<Link href="/help">` near LOGOUT | ✅ | `pages/host/c/[churchSlug].tsx:2433-2440` |
| 19 | Existing layout preserved (360 / 768 / 1440 px) | ⚠️ | Code change is additive; **mobile width not manually verified** |

**Host header sub-rate**: 1/2 verified (50%) — the regression check requires manual test

#### Listener Page — Design §5.4

| # | Element | Implemented | Evidence |
|---|---------|:-----------:|----------|
| 20 | Corner Help link `position: fixed; bottom-right` low opacity | ✅ | `pages/c/[churchSlug]/s/[serviceKey].tsx:538-565` |
| 21 | Existing feed unchanged | ⚠️ | Edit is additive (one import, one element appended inside `<main>` after the existing feed); **not visually verified on mobile** |

**Listener sub-rate**: 1/2 verified (50%) — the regression check requires manual test

**Total Page UI Checklist Rate**: 18/21 fully verified statically = **86%** (the 3 ⚠️ items are "implemented but unverified-on-mobile", not "not implemented").

### 2.6 API Contract Verification

Not applicable. No API in this feature.

### 2.7 Runtime Verification Results

#### L1: API Endpoint Tests

Not applicable — no API endpoints in this feature.

#### L2: UI Action Tests (Playwright)

**Skipped**: Playwright is not installed in the repository (verified during Plan phase). Per Design §8.1 footnote, L2 tests are **manual smoke** for v1 — not yet executed by the user.

#### L3: E2E Scenario Tests (Playwright)

**Skipped**: Same reason as L2.

**Runtime Match Rate**: N/A (not measurable without runtime tooling). Feature relies on **manual L2/L3 smoke tests** per Design §8.1.

### 2.8 Match Rate Summary

```
┌─────────────────────────────────────────────┐
│  Structural Match Rate:  100%                │
│  Functional Match Rate:   95%                │
│  Contract Match Rate:    N/A (no API)        │
│  Runtime Match Rate:     N/A (no Playwright) │
│  ─────────────────────────────────────────── │
│  Overall Match Rate:      96%                │
│  Formula (no API/runtime):                   │
│    = (Structural × 0.30) + (Functional × 0.70)│
│    = 100 × 0.30 + 95 × 0.70 = 96.5%          │
├─────────────────────────────────────────────┤
│  ✅ Match:           18 items (86%)          │
│  ⚠️ Unverified-mobile: 3 items (14%)         │
│  ❌ Not implemented:  0 items (0%)           │
└─────────────────────────────────────────────┘
```

**Threshold**: ≥90% (project default). **Status**: PASS (96%).

---

## 3. Code Quality Analysis

### 3.1 Complexity Analysis

| File | Function | Approx Complexity | Status | Recommendation |
|------|----------|:-----------------:|:------:|----------------|
| `pages/help.tsx` | `HelpPage` (default export) | 1 (no branches) | ✅ Good | — |
| `pages/help.tsx` | `PanicCard` | 2 (one ternary on `escalation`) | ✅ Good | — |
| `pages/help.tsx` | `ComingSoon` | 1 | ✅ Good | — |
| `pages/host/c/[churchSlug].tsx` (Help chip block) | inline | 1 | ✅ Good | — |
| `pages/c/[churchSlug]/s/[serviceKey].tsx` (corner link) | inline | 2 (mouseEnter/Leave handlers) | ✅ Good | — |

### 3.2 Code Smells

| Type | File | Location | Description | Severity |
|------|------|----------|-------------|:--------:|
| Inline event handlers | `pages/c/[churchSlug]/s/[serviceKey].tsx` | L562-563 | `onMouseEnter`/`onMouseLeave` mutate `style` directly. Acceptable for a single fixed-position link; would be better as CSS `:hover` if the file used CSS. Style consistent with the existing inline-style convention. | 🟢 Info |
| Magic numbers in `text-shadow` | `pages/help.tsx`, `pages/host/c/[churchSlug].tsx` | various | Consistent with rest of codebase (other pages also embed numeric tokens inline). | 🟢 Info |
| `// TODO: verify` block | `pages/help.tsx:28-31` | comment header on `PANIC_CASES` | Intentional — copy authored from codebase reading, must be reviewed by user. Tracked in Important Issues below. | 🟡 (tracked) |

No 🔴 critical smells.

### 3.3 Security Issues

| Severity | File | Location | Issue | Recommendation |
|----------|------|----------|-------|----------------|
| 🟢 Info | — | — | `pages/help.tsx` has zero user input, zero `dangerouslySetInnerHTML`, zero env reads, zero external links, zero fetches. | No action |
| 🟢 Info | `pages/c/[churchSlug]/s/[serviceKey].tsx` | corner link block | The fixed-position link uses `<Link>` (no `target="_blank"`, no `rel`-attack surface). | No action |

**No security findings of severity 🟡 or above.**

---

## 4. Performance Analysis

| Concern | Result |
|---------|--------|
| Initial render time | Static page, no client effects, no fetches → render bound only by HTML/CSS parse. Estimated <50 ms on a typical laptop. |
| Bundle weight | `pages/help.tsx` adds only static markup + 2 small components. Inline styles inflate the JSX slightly but no new packages or fonts. |
| Listener page corner link | Adds 1 new DOM node, 1 inline style object, 2 event handlers. No measurable impact on the existing translation-feed render path. |
| Host header Help chip | Adds 1 `<Link>` element with no event handlers. Negligible. |

No bottlenecks. No N+1 queries (no queries at all).

---

## 5. Test Coverage

This project does not enforce test-coverage thresholds for `pages/*.tsx`. Per Design §8.1, this feature is verified by:

- **L1 (Lint + typecheck)**: Automated, passing.
- **L2 (UI smoke)**: Manual — pending user execution per Design §8.3.
- **L3 (E2E flow)**: Manual — pending user execution per Design §8.4.

Adding Playwright is explicitly out-of-scope (Plan §2.2).

---

## 6. Clean Architecture Compliance

### 6.1 Layer Dependency Verification

| Layer | Expected Dependencies | Actual Dependencies | Status |
|-------|----------------------|---------------------|:------:|
| Presentation (`pages/help.tsx`) | `next/link`, `next/head`, React (implicit) | `next/head`, `next/link` only | ✅ |
| Presentation (host header edit) | `next/link` (already imported) | unchanged imports | ✅ |
| Presentation (listener edit) | `next/link` (one new import) | `next/link` added | ✅ |

### 6.2 Dependency Violations

None. `pages/help.tsx` does **not** import:
- `lib/authContext` (correctly — page is public)
- `lib/backendAuth` (correctly — no API calls)
- `components/*` (correctly — Option C avoids shared components)
- Any utility or service module

### 6.3 Layer Assignment Verification

| Component | Designed Layer | Actual Location | Status |
|-----------|---------------|-----------------|:------:|
| `HelpPage` | Presentation | `pages/help.tsx` | ✅ |
| `PanicCard` | Presentation (co-located) | `pages/help.tsx` (same file) | ✅ |
| `ComingSoon` | Presentation (co-located) | `pages/help.tsx` (same file) | ✅ |
| `PanicCardProps` type | Domain (trivial, co-located) | `pages/help.tsx` (same file) | ✅ |

### 6.4 Architecture Score

```
┌─────────────────────────────────────────────┐
│  Architecture Compliance: 100%               │
├─────────────────────────────────────────────┤
│  ✅ Correct layer placement: 4/4 components │
│  ⚠️ Dependency violations:    0             │
│  ❌ Wrong layer:              0             │
└─────────────────────────────────────────────┘
```

---

## 7. Convention Compliance

### 7.1 Naming Convention Check

| Category | Convention | Files Checked | Compliance | Violations |
|----------|-----------|:-------------:|:----------:|------------|
| Component default export | PascalCase | 1 (`HelpPage`) | 100% | — |
| Local components | PascalCase | 2 (`PanicCard`, `ComingSoon`) | 100% | — |
| Type | PascalCase | 1 (`PanicCardProps`) | 100% | — |
| Constants | UPPER_SNAKE_CASE | 2 (`LAST_UPDATED`, `PANIC_CASES`) | 100% | — |
| Local style objects | camelCase | 4 (`tocLinkStyle`, `sectionHeadingStyle`, `introStyle`, `contactCtaStyle`) | 100% | — |
| Page file | lowercase per Pages Router | `help.tsx` | 100% | — |

### 7.2 Folder Structure Check

| Expected Path | Exists | Notes |
|---------------|:------:|-------|
| `frontend/pages/help.tsx` | ✅ | Matches Design §11.1 |
| `frontend/pages/host/c/[churchSlug].tsx` | ✅ | Modified in place |
| `frontend/pages/c/[churchSlug]/s/[serviceKey].tsx` | ✅ | Modified in place |

### 7.3 Import Order Check

`pages/help.tsx`:
```typescript
import Head from "next/head";
import Link from "next/link";
```

- [x] External libraries first (Next.js)
- [x] No internal absolute imports needed
- [x] No relative imports
- [x] No type imports needed (types declared inline)
- [x] No styles file

✅ Matches `pages/contact.tsx` pattern.

### 7.4 Environment Variable Check

| Variable | Used? |
|----------|:-----:|
| Any `NEXT_PUBLIC_*` | ❌ (not needed) |
| Any server env | ❌ (no server-side code in this page) |

No env vars required. Matches Plan §3.2 / Design §10.3.

### 7.5 Convention Score

```
┌─────────────────────────────────────────────┐
│  Convention Compliance: 100%                 │
├─────────────────────────────────────────────┤
│  Naming:           100%                      │
│  Folder Structure: 100%                      │
│  Import Order:     100%                      │
│  Env Variables:    100%                      │
└─────────────────────────────────────────────┘
```

---

## 8. Overall Score

```
┌─────────────────────────────────────────────┐
│  Overall Score: 96/100                       │
├─────────────────────────────────────────────┤
│  Design Match:        96 points  (96%)       │
│  Code Quality:        98 points  (no smells) │
│  Security:           100 points  (no input)  │
│  Testing:             —          (manual L2/L3 pending) │
│  Performance:         98 points  (static)    │
│  Architecture:       100 points              │
│  Convention:         100 points              │
└─────────────────────────────────────────────┘
```

> Testing line is "—" rather than a number because the v1 verification mode is manual smoke; not having Playwright is a known and accepted gap (Plan §2.2 out-of-scope).

---

## 9. Issues Found

### 9.1 Critical (Confidence ≥80%)

**None.**

### 9.2 Important (Confidence ≥80%)

| ID | Issue | Evidence | Recommended Fix |
|----|-------|----------|-----------------|
| **I-1** | Panic-section copy is **drafted by AI from codebase reading**, not authored by domain owner. The `// TODO: verify` block at `pages/help.tsx:28-31` calls this out. Specific risks: the "3-hour room max duration" claim in `PANIC_CASES[3]` is inferred from `ROOM_MAX_DURATION_SEC=10800` env default in `CLAUDE.md` — confirm this is the actual deployed value. The "Trial plan minute auto-end" claim in `PANIC_CASES[3]` should be verified against actual billing behavior. | `pages/help.tsx:28-31`, `pages/help.tsx:32-81` (PANIC_CASES) | User reads each of the 4 panic cases and corrects/confirms; remove `// TODO: verify` comment when done. |
| **I-2** | **Mobile-layout regression is unverified.** Plan FR-10, Design §5.4 row 17/19/21, and Design §8.3 rows 4/8/9/10 require visual verification on 360 px / 768 px / 1440 px. Static analysis cannot detect visual collisions (e.g., host header tabs wrapping into the new Help chip). | Design §8.3 manual smoke not yet executed | User opens host dashboard at 360 px width (DevTools or real device) and confirms: tabs/chip/logout all reachable, no horizontal scroll, listener corner link does not overlap mobile browser UI. |

### 9.3 Info-only (lower confidence or known-accepted)

| ID | Note |
|----|------|
| INFO-1 | `npm run build` (full) was not executed because of an unrelated pre-existing TypeScript error in `frontend/components/PresentationDisplay.tsx` (untracked, in-progress separate feature: `presentation-display-mode`). Per-file `tsc --noEmit` confirms 0 errors in the help-center files. |
| INFO-2 | Manual L2/L3 smoke walk-throughs (Design §8.3, §8.4) are pending user execution. They are not "missing tests" — Plan §2.2 explicitly defers Playwright. |
| INFO-3 | The `host-help-link` className is added to the new Help chip but no CSS rule targets it. It's there for future styling extensibility, consistent with existing `host-user-name` etc. classNames in the same file. Not a defect. |

---

## 10. Design Document Updates Needed

None. Implementation matches Design §1.2, §2.0 (Option C), §5.3 component list (the additive `ComingSoon` helper is consistent with §1.2's allowance for local helpers), §5.4 Page UI Checklist, §6 (no errors to handle), §7 (security review verbatim), §9 Clean Architecture, §10 conventions.

---

## 11. Next Steps

- [ ] **User action**: Walk through Important issue I-1 — review and correct the 4 panic cases at `frontend/pages/help.tsx:32-81`.
- [ ] **User action**: Walk through Important issue I-2 — manual mobile smoke at 360 px / 768 px (Design §8.3).
- [ ] After I-1 + I-2 are resolved (or accepted as-is), run `/pdca report help-center` to generate the completion report.
- [ ] Optional: `/simplify` for code cleanup (per bkit core rules at ≥90% match rate).
- [ ] Out-of-cycle: Author real content for Sections 2–4 in a follow-up `/pdca plan help-center-v2` once the panic guide is validated.

---

## Version History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 0.1 | 2026-05-08 | Initial analysis. Match rate 96%. 0 Critical, 2 Important (verification gaps), 3 Info. | namju |
