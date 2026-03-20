# Plan: Contact Us Form

## Executive Summary

| Perspective | Description |
|---|---|
| **Problem** | The contact form exists and submits correctly, but submitters receive no confirmation email, the in-memory rate limiter resets on each Vercel cold start (per-instance isolation), and the frontend shows a generic error when email sending is unconfigured rather than a helpful fallback message. |
| **Solution** | Add a submitter confirmation email via Resend, note the serverless rate-limit limitation, and improve the `contact_not_configured` error UX so users know to email support directly. |
| **Function / UX Effect** | After submitting, users receive an email acknowledging their request; the support team sees fewer duplicate follow-up submissions from confused users; and the platform degrades gracefully when email credentials are missing. |
| **Core Value** | A complete support loop: the contact form closes the feedback cycle by confirming receipt to the submitter, building trust that their request was actually received. |

---

## 1. What's Already Well-Implemented

The contact form is fully functional. Before listing gaps, here is what's solid:

| Area | Status | Location |
|---|---|---|
| Form fields: name, email, organization, topic, message | Complete | `contact.tsx` |
| Honeypot (`website` field) for bot detection | Complete | `contact.tsx`, `api/contact.ts:270` |
| Cloudflare Turnstile CAPTCHA for anonymous users | Complete | `contact.tsx:117-132`, `api/contact.ts:146-170` |
| Authenticated user bypass (less friction, higher rate limits) | Complete | `api/contact.ts:292-305` |
| In-memory rate limiting: 3/hr/IP (anon), 6/hr/IP (auth); 5/day/email (anon), 10/day/email (auth) | Complete | `api/contact.ts:114-123` |
| Email routing: billing topic → billing inbox, others → primary inbox | Complete | `api/contact.ts:186` |
| Resend domain-not-verified fallback to `RESEND_FALLBACK_FROM_EMAIL` | Complete | `api/contact.ts:250-258` |
| HTML + plaintext email with XSS escaping | Complete | `api/contact.ts:172-259` |
| Query param prefill: `?topic=`, `?organization=`, `?message=` | Complete | `contact.tsx:99-115` |
| Support email reveal with charcode obfuscation | Complete | `contact.tsx:25-31`, `contact.tsx:142-164` |
| Authenticated user prefill (name, email, organization from Firebase/API) | Complete | `contact.tsx:69-97` |
| Link count limit in message body (max 3) | Complete | `api/contact.ts:286-288` |

---

## 2. Gaps Identified

### Gap 1 — No Submitter Confirmation Email (Medium)

**Current behavior**: After a successful submission the page shows an on-screen notice: "Your message was sent. Support will follow up soon." No email is sent back to the submitter.

**Risk**: Users who close the tab before reading the notice, or who submit from a shared computer, have no record that their request was received. This causes duplicate submissions ("I wasn't sure it went through") and erodes trust.

**Proposed fix**: After `sendContactEmail` succeeds, send a second Resend call — a brief plain-text confirmation email to `payload.email`:

- Subject: `We received your message — Worship Support`
- Body: name, topic, short receipt summary, expected response time note
- From: same `CONTACT_FROM_EMAIL` / fallback pattern
- If this second Resend call fails, swallow the error silently (primary delivery already succeeded)

---

### Gap 2 — Serverless Rate Limit Isolation (Low / Tech Debt)

**Current behavior**: Rate limiting uses `globalThis.__contactRateStore` — an in-memory object on the Node.js serverless instance. In a Vercel production deployment, multiple concurrent serverless instances can exist simultaneously, each with their own `globalThis`. A determined user can bypass the limit by triggering requests on different instances (cold starts).

**Risk**: Low in practice (contact form submissions are infrequent), but notable for compliance/abuse scenarios.

**Proposed fix**: Document this limitation in a code comment rather than implement a distributed store — adding Redis/KV for a low-traffic contact form is over-engineering. The existing limit is still effective against casual abuse. Add a `// NOTE:` comment in `api/contact.ts` near the rate store explaining the per-instance scope.

---

### Gap 3 — Generic Error on `contact_not_configured` (Low)

**Current behavior**: If `CONTACT_FROM_EMAIL` or `RESEND_API_KEY` is missing, the API returns HTTP 503 with `"Support inbox is not configured yet."`. The frontend shows this string as a generic error paragraph with no further guidance.

**Risk**: Users who hit this during a misconfiguration period have no path forward — they see an error and don't know to email directly.

**Proposed fix**: In `contact.tsx`, detect the error string `"Support inbox is not configured yet."` and show a fallback panel with a mailto link using the obfuscated support address (`buildSupportEmail()`), instructing the user to email directly.

---

## 3. Non-Goals

- **Live chat** — out of scope; not in current architecture
- **CRM integration** (HubSpot, Salesforce) — not needed at current scale
- **File attachments** — Resend supports it but adds complexity without clear need
- **Distributed rate limiting with Redis** — over-engineering for current traffic volume
- **Auto-assignment routing** — single support inbox is sufficient

---

## 4. User Stories

| As a... | I want to... | So that... |
|---|---|---|
| Contact form submitter | Receive an email confirming my request was received | I know it went through and have a reference |
| Platform operator | See a code comment about rate limit scope | I understand the limitation before adding abuse monitoring |
| User during misconfiguration | See a direct email link when the form fails | I can still reach support |

---

## 5. Scope

### In Scope

**Next.js API Route (`frontend/pages/api/contact.ts`)**
- After successful `sendContactEmail`, call a new `sendConfirmationEmail(email, name, topic)` function
- Add a `// NOTE:` comment on the rate store explaining serverless isolation

**Frontend (`frontend/pages/contact.tsx`)**
- In `onSubmit` catch: detect `"Support inbox is not configured yet."` → show fallback panel with `buildSupportEmail()` mailto link

### Out of Scope
- Any backend (FastAPI) changes — the contact form runs entirely in Next.js API routes
- Design changes to the existing form layout
- New form fields

---

## 6. Affected Files

| File | Change |
|---|---|
| `frontend/pages/api/contact.ts` | `sendConfirmationEmail` function + call after successful delivery; rate store comment |
| `frontend/pages/contact.tsx` | `contact_not_configured` error detection + fallback panel |

---

## 7. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Confirmation email increases Resend usage/cost | Low | Single plain-text email; minimal tokens |
| Confirmation email triggers spam filters (reply-to is user's own email) | Low | Plain-text only, no links, from verified domain |
| Fallback panel reveals obfuscated email to bots | Low | Same obfuscation (`buildSupportEmail`) already used; reveal requires page render |

---

## 8. Acceptance Criteria

- [ ] Submitting the contact form sends a confirmation email to the submitter's address
- [ ] Confirmation email contains name, topic label, and a note to expect a reply within 1 business day
- [ ] If the confirmation email fails (Resend error), the primary submission still succeeds (error is swallowed)
- [ ] When the API returns `"Support inbox is not configured yet."`, the frontend shows a mailto link using the support address
- [ ] A `// NOTE:` comment exists on the rate store explaining per-instance isolation
- [ ] `npm run lint` passes with no errors

---

## 9. Next Steps

```
/pdca design contact-us-form   ← Detailed component design
/pdca do contact-us-form        ← Implementation
/pdca analyze contact-us-form   ← Gap analysis
```
