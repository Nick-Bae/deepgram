# Contact Us Form — Completion Report

## Executive Summary

| Item | Detail |
|------|--------|
| Feature | contact-us-form |
| Started | 2026-03-20 |
| Completed | 2026-03-28 |
| Match Rate | 100% |
| Primary files | `frontend/pages/contact.tsx`, `frontend/pages/api/contact.ts` |

### Value Delivered

| Perspective | Detail |
|-------------|--------|
| Problem | No direct contact channel existed; support requests had no structured intake or spam protection |
| Solution | Full contact form page with topic routing, Turnstile spam gate for anonymous users, and Firebase auth bypass for signed-in users |
| Function / UX Effect | Warm glassmorphism UI, 7 topic categories, auto-prefill from auth + query params, obfuscated direct email fallback, user confirmation email |
| Core Value | Support team receives clean, categorized, spam-filtered submissions routed to the right inbox (billing vs general) |

---

## 1. What Was Built

### Frontend — `frontend/pages/contact.tsx`

- Glassmorphism card layout with warm bokeh background (consistent with site design system)
- **Form fields**: name, email, organization, topic (7 options), message (min 20 chars)
- **Auto-prefill**: from Firebase auth session (name, email, org from membership API)
- **Query param prefill**: `?topic=`, `?organization=`, `?message=` — used by in-app deep-links
- **Cloudflare Turnstile**: spam protection widget rendered only for anonymous users; signed-in users submit directly
- **Direct email reveal**: obfuscated support address revealed on demand; Turnstile gate applies here too
- **Copy to clipboard**: one-click copy with feedback state
- **Fallback banner**: shown when Resend is not configured, displays the direct email instead
- **Responsive layout**: two-column (`minmax(min(100%, 320px), 1fr)`) collapses to single column on mobile

### API — `frontend/pages/api/contact.ts`

- **Validation**: name (≥2 chars), email (regex), topic (enum), message (≥20 chars, ≤3 links)
- **Honeypot**: hidden `website` field — submissions with it set are silently discarded
- **Rate limiting** (in-memory, per serverless instance):
  - IP: 3/hour anonymous, 6/hour authenticated
  - Email: 5/day anonymous, 10/day authenticated
- **Turnstile verification**: calls Cloudflare siteverify API; skipped if `TURNSTILE_SECRET_KEY` not set
- **Firebase auth verification**: calls `/api/auth/me` with the `idToken` to confirm sender identity
- **Email dispatch via Resend**:
  - Routes billing topic to `BILLING_TO_EMAIL`, all others to `CONTACT_TO_EMAIL`
  - `reply_to` set to submitter's email
  - Falls back to `RESEND_FALLBACK_FROM_EMAIL` if primary domain is unverified
  - Sends HTML + plain text, tags with `source` and `topic`
- **Confirmation email**: fire-and-forget reply to submitter; failure does not affect response
- **Error surfacing**: `contact_not_configured` → 503 with user-friendly message and direct-email fallback UI

---

## 2. Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Turnstile only for anonymous | Reduces friction for signed-in church admins who are already authenticated |
| Per-instance rate store | Acceptable for low-traffic contact form; noted in code with upgrade path to KV store |
| Honeypot + Turnstile together | Defense in depth — honeypot catches bots before any async work |
| Confirmation email fire-and-forget | Confirmation is a courtesy; primary submit should never fail because confirmation does |
| Obfuscated direct email | Prevents email harvesting by crawlers while still giving users a fallback path |
| Billing inbox routing | Support team asked for billing messages to be separated from general support |

---

## 3. Environment Variables Required

| Variable | Purpose |
|----------|---------|
| `RESEND_API_KEY` | Resend API key for email dispatch |
| `CONTACT_FROM_EMAIL` | Verified sender address |
| `CONTACT_TO_EMAIL` | Support inbox |
| `BILLING_TO_EMAIL` | Billing inbox (optional, falls back to `CONTACT_TO_EMAIL`) |
| `RESEND_FALLBACK_FROM_EMAIL` | Fallback sender if primary domain unverified |
| `NEXT_PUBLIC_TURNSTILE_SITE_KEY` | Cloudflare Turnstile site key (anonymous spam protection) |
| `TURNSTILE_SECRET_KEY` | Cloudflare Turnstile secret (server-side verify) |

---

## 4. Check Phase Result

- **Match Rate**: 100%
- **Iterations**: 0
- All form fields, validation, spam protection, email routing, and confirmation flow verified against design intent.
