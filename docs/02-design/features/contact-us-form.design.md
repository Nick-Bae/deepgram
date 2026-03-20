# Design: Contact Us Form

> Refs: `docs/01-plan/features/contact-us-form.plan.md`
> Phase: Design
> Updated: 2026-03-20

---

## 1. Overview

Three targeted additions to the existing, fully-functional contact form:

| Gap | File | Change |
|---|---|---|
| 1. Submitter confirmation email | `frontend/pages/api/contact.ts` | New `sendConfirmationEmail` function + call after primary delivery |
| 2. Rate store isolation comment | `frontend/pages/api/contact.ts` | `// NOTE:` comment on `getRateStore` |
| 3. `contact_not_configured` fallback | `frontend/pages/contact.tsx` | New state + fallback panel with mailto link |

No new dependencies. No new routes. No schema changes.

---

## 2. Architecture

### 2.1 Data Flow (additions only)

```
[Gap 1] Successful POST /api/contact
  └── sendContactEmail(...)         ← existing
  └── sendConfirmationEmail(...)    ← NEW (fire-and-forget)
        └── POST https://api.resend.com/emails
              from: CONTACT_FROM_EMAIL (or fallback)
              to:   payload.email
              subject: "We received your message — Worship Support"
              text: plain-text body only (no HTML)
        └── .catch(() => {})        ← error silently swallowed

[Gap 3] POST /api/contact → 503 "Support inbox is not configured yet."
  └── contact.tsx onSubmit catch
        └── detect exact error string
        └── setUnconfiguredFallback(true)
        └── render <UnconfiguredFallbackPanel>
              → shows mailto link using buildSupportEmail()
```

### 2.2 No New State on API Route

`sendConfirmationEmail` uses the same `CONTACT_FROM_EMAIL` / `RESEND_API_KEY` env vars already read by `sendContactEmail`. No new config required.

---

## 3. API Route Design (`api/contact.ts`)

### 3.1 Gap 1 — `sendConfirmationEmail` function

Add after `sendContactEmail` function definition (around line 260):

```typescript
async function sendConfirmationEmail(params: {
  email: string;
  name: string;
  topic: ContactTopic;
}): Promise<void> {
  const resendApiKey = (process.env.RESEND_API_KEY || "").trim();
  const fromEmail = (process.env.CONTACT_FROM_EMAIL || "").trim();
  const fallbackFromEmail = (process.env.RESEND_FALLBACK_FROM_EMAIL || "Worship <onboarding@resend.dev>").trim();

  if (!resendApiKey || !fromEmail) return; // not configured — skip silently

  const topicLabel = TOPIC_LABELS[params.topic] || TOPIC_LABELS.other;
  const subject = "We received your message — Worship Support";
  const text = [
    `Hi ${params.name},`,
    "",
    `We received your message about "${topicLabel}". Our support team will follow up within 1 business day.`,
    "",
    "You don't need to reply to this email. If you'd like to add more details, send a new message at worshiptranslation.com/contact.",
    "",
    "— Worship Support Team",
  ].join("\n");

  const send = async (activeFrom: string) =>
    fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${resendApiKey}`,
      },
      body: JSON.stringify({
        from: activeFrom,
        to: [params.email],
        subject,
        text,
        tags: [{ name: "source", value: "worship-contact-confirmation" }],
      }),
    });

  let res = await send(fromEmail);
  if (!res.ok && isResendDomainNotVerified(res.status, await res.text()) && fallbackFromEmail !== fromEmail) {
    res = await send(fallbackFromEmail);
  }
  // Result intentionally ignored — confirmation failure must not block the primary response.
}
```

**Key design decisions:**
- Plain text only — no HTML body. Avoids spam filter triggers for a transactional acknowledgment.
- No `reply_to` — the support team's reply should come from their own inbox, not as a reply to this auto-ack.
- Same domain-not-verified fallback pattern as `sendContactEmail` for consistency.
- Early return if not configured — the confirmation is best-effort; if `CONTACT_FROM_EMAIL` is missing the primary submission still succeeded.

### 3.2 Call site in `handler`

Replace the success block:

```typescript
// Before:
await sendContactEmail({ ... });
return res.status(200).json({ ok: true });

// After:
await sendContactEmail({ ... });
sendConfirmationEmail({
  email: payload.email,
  name: payload.name,
  topic: payload.topic,
}).catch(() => {});
return res.status(200).json({ ok: true });
```

`sendConfirmationEmail(...).catch(() => {})` — fire-and-forget pattern. The response is not awaited so the HTTP response is not delayed.

### 3.3 Gap 2 — Rate Store Comment

Add a block comment immediately before `function getRateStore()`:

```typescript
// NOTE: This rate store lives in `globalThis` and is per-serverless-instance.
// In Vercel production, multiple concurrent cold-start instances each maintain
// independent state, so the rate limits are enforced per-instance, not globally.
// This is acceptable for a low-traffic contact form — the limits still prevent
// casual abuse from a single session. For strict cross-instance enforcement,
// replace the Map-based store with a shared KV store (e.g. Vercel KV / Upstash).
function getRateStore(): RateStore {
```

---

## 4. Frontend Design (`contact.tsx`)

### 4.1 Gap 3 — Unconfigured Fallback State

#### New state variable

Add near the other state declarations (after `copiedDirectEmail`):

```typescript
const [unconfiguredFallback, setUnconfiguredFallback] = useState(false);
```

#### Modified `onSubmit` catch block

```typescript
} catch (err) {
  const msg = err instanceof Error ? err.message : "Failed to submit contact request.";
  if (msg === "Support inbox is not configured yet.") {
    setUnconfiguredFallback(true);
  } else {
    setErrorMsg(msg);
  }
}
```

#### Fallback panel JSX

Replace the existing `{errorMsg ? ...}` block with a conditional that also handles `unconfiguredFallback`:

```tsx
{unconfiguredFallback ? (
  <div
    style={{
      margin: 0,
      borderRadius: 14,
      padding: "16px 18px",
      background: "rgba(249,245,230,0.9)",
      border: "1px solid rgba(210,190,130,0.5)",
      color: "#5a4a20",
      display: "grid",
      gap: 10,
    }}
  >
    <p style={{ margin: 0, fontSize: 14, fontWeight: 700 }}>
      Contact form is temporarily unavailable.
    </p>
    <p style={{ margin: 0, fontSize: 14, lineHeight: 1.7 }}>
      Please email us directly:
    </p>
    <a
      href={`mailto:${buildSupportEmail()}`}
      style={{ color: "#3f6093", fontWeight: 700, wordBreak: "break-all" }}
    >
      {buildSupportEmail()}
    </a>
  </div>
) : errorMsg ? (
  <p style={{ margin: 0, borderRadius: 14, padding: "12px 14px", background: "rgba(188,95,111,0.12)", color: "#a33d51", fontSize: 14 }}>
    {errorMsg}
  </p>
) : null}
```

**Design notes:**
- `buildSupportEmail()` is already defined at module level in `contact.tsx` — no new import needed.
- The fallback panel uses a warm amber style to distinguish it from the red error state.
- `unconfiguredFallback` is never reset to false — once shown, it stays until page reload (the misconfiguration requires an operator fix, not a retry).

---

## 5. Affected Files

| File | Change | Lines affected |
|---|---|---|
| `frontend/pages/api/contact.ts` | `sendConfirmationEmail` function + call site + `getRateStore` comment | ~3 insertions |
| `frontend/pages/contact.tsx` | `unconfiguredFallback` state + modified catch + fallback panel JSX | ~30 lines |

---

## 6. Implementation Order

1. `api/contact.ts` — Add `// NOTE:` comment on `getRateStore`
2. `api/contact.ts` — Add `sendConfirmationEmail` function after `sendContactEmail`
3. `api/contact.ts` — Add `sendConfirmationEmail(...).catch(() => {})` call in `handler`
4. `contact.tsx` — Add `unconfiguredFallback` state
5. `contact.tsx` — Update `onSubmit` catch to set `unconfiguredFallback`
6. `contact.tsx` — Replace error block with conditional fallback/error render
7. `npm run lint` — verify clean

---

## 7. Testing Checklist

- [ ] Submitting the form (with valid Resend config) sends a confirmation email to the submitter's address
- [ ] Confirmation email subject is `"We received your message — Worship Support"`
- [ ] Confirmation email body is plain text (no HTML)
- [ ] If `sendConfirmationEmail` throws, the API still returns HTTP 200 (primary submission unaffected)
- [ ] `// NOTE:` comment exists on `getRateStore` explaining per-instance isolation
- [ ] When API returns `"Support inbox is not configured yet."`, the amber fallback panel appears
- [ ] Fallback panel contains a clickable `mailto:` link with the correct support email
- [ ] Normal API errors (non-503) still show the red `errorMsg` paragraph
- [ ] `npm run lint` passes with no errors
