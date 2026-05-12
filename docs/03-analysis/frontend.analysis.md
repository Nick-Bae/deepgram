# Frontend Glassmorphism Theme — Gap Analysis

**Date:** 2026-03-28
**Feature:** frontend (warm glassmorphism theme redesign)
**Phase:** Check
**Match Rate:** 52%

---

## File-by-File Assessment

| File | Role | Score |
|------|------|-------|
| `StudioAccessLayout.tsx` | Auth/Access Layout | 95% |
| `host/c/[churchSlug].tsx` | Host Console | 88% |
| `index.tsx` | Landing Page | 72% |
| `TranslationBox.tsx` | Translator UI | 25% |
| `c/[churchSlug]/s/[serviceKey].tsx` | Listener Display | 15% |
| `_document.tsx` | Document Wrapper | N/A (no UI) |

**Weighted Match Rate: 52%**

---

## What's Done Well

- Bokeh background (6-blob radial gradient system) is consistent where implemented (StudioAccessLayout, Host Console)
- Warm color palette — cream (#fffaee/#ede5d8), gold (#b89a5e/#c5a263), navy (#0f1f3d) — applied in auth/admin areas
- Glass border system (rgba white borders 0.55–0.70 opacity) consistent across glass panels
- Backdrop blur stack (blur(40px) primary, blur(14px) secondary, WebKit fallback) applied correctly
- Mobile responsive — all styled files include media queries and fluid sizing

---

## Gaps

### TranslationBox.tsx (25%)
- No bokeh background
- Only 2–3 glass effect instances (shellStyle, panelStyle)
- Color palette inconsistent — mostly functional colors, not warm/gold
- No inset shadows for depth

### Listener Page — [serviceKey].tsx (15%)
- Intentionally dark theme (#060b18) for TV/projection display mode
- No bokeh background
- Minimal glass styling
- Note: dark theme may be intentional; assess whether warm glass should be applied here

### index.tsx (72%)
- No bokeh background in hero/sections
- Glass effects limited to scrolled navbar and demo cards
- Landing page sections feel visually disconnected from the auth/host experience

### Missing System-Wide
- No shared glass utilities — each file redefines the same rgba/blur values
- No CSS custom properties or shared constants for color tokens

---

## Recommendations

1. Add bokeh background to `TranslationBox.tsx` (listener view)
2. Add bokeh background blobs to `index.tsx` hero section
3. Decide intentionally on `[serviceKey].tsx` — keep dark or migrate to warm glass
4. Extract shared glass styles to a utility layer (CSS vars or shared constants)
