# Plan: Security Hardening

## Executive Summary

| Perspective | Description |
|---|---|
| **Problem** | Three production-accessible endpoints expose the full API schema publicly, a stale temp file sits in the repo, Firestore public-read rules on rooms and services are undocumented as accepted risk, and the listener WebSocket relies entirely on a per-IP connection limit with no org-scoped abuse controls. |
| **Solution** | Disable FastAPI auto-docs in production, delete the stale temp file, audit cross-org isolation on all HTTP routes, and document + optionally scope the Firestore public-read policy with a signed listener token path. |
| **Function / UX Effect** | No visible UX change — these are entirely server-side and configuration changes. Hosts, listeners, and admins are unaffected. The attack surface available to a malicious researcher drops significantly. |
| **Core Value** | A church translation platform handling live audio, transcripts, and billing data must not publish its own API blueprint. Closing these gaps is the difference between a platform that "works" and one that is defensible. |

---

## 1. What's Already Well-Hardened

Existing controls that are solid — do not regress these:

| Area | Status | Location |
|---|---|---|
| Firebase ID token verification | All authenticated endpoints | `auth/firebase_auth.py:get_current_user_required` |
| CORS explicit allowlist, no wildcard | Production enforced | `main.py:CORSMiddleware` |
| `_can_host()` for STT WebSocket | Host auth required before accepting | `main.py:ws_stt_deepgram:1431` |
| Per-IP viewer connection limit | 20 concurrent per IP on `/ws/translate` | `main.py:_WS_VIEWER_MAX_CONNS_PER_IP` |
| Translation rate limiting | Global + org + uid + anon tiers | `main.py:_reserve_translation_budget` |
| Password reset rate limiting | 5/IP/hour + 3/email/hour | `auth.py:_enforce_password_reset_rate_limit` |
| Invite rate limiting | Multi-bucket | `auth.py:_enforce_invite_rate_limit` |
| `hostToken` never in API responses | Sanitized before return | `auth.py:_sanitize_membership_payload` |
| Security event logging | Sensitive actions logged | `security_log.py:security_event` |
| `super_admin` claim gating | Admin routes require Firebase custom claim | `firebase_auth.py` |
| Firestore client write blocked | All writes are `if false` from client | `firestore.rules` |
| Org member data cross-org blocked | Members readable only by `isOrgMember(orgId)` | `firestore.rules:61` |

---

## 2. Gaps Identified

### Gap 1 — FastAPI Docs Endpoints Exposed in Production (Critical)

**Current behavior**: `FastAPI(title="Real-Time Translation Backend", version="1.0.0")` uses no `docs_url`, `redoc_url`, or `openapi_url` override. FastAPI defaults expose three endpoints:
- `/docs` — Swagger UI with full interactive API explorer
- `/redoc` — ReDoc documentation
- `/openapi.json` — Machine-readable full schema

In production on Cloud Run, these are publicly accessible to anyone with the base URL. They reveal every endpoint path, all query parameters, request/response shapes, and auth requirements — essentially a penetration test guide for the platform.

**Risk**: A researcher can enumerate all routes, identify unauthenticated endpoints, discover admin paths, and understand token/param shapes without any credentials.

**Proposed fix**: Disable all three in production by passing `docs_url=None, redoc_url=None, openapi_url=None` when `_IS_PRODUCTION` is `True`. Keep them active in development.

```python
# main.py — FastAPI constructor
app = FastAPI(
    title="Real-Time Translation Backend",
    version="1.0.0",
    docs_url=None if _IS_PRODUCTION else "/docs",
    redoc_url=None if _IS_PRODUCTION else "/redoc",
    openapi_url=None if _IS_PRODUCTION else "/openapi.json",
)
```

**Files**: `backend/app/main.py:246`

---

### Gap 2 — Stale Temp File in Repo (High)

**Current state**: `backend/app/main.py.tmp.2869.1774109189155` exists on disk. This is a leftover from an editor or tool write. It:
- Contains a full copy of `main.py` logic — duplicating the entire API surface
- Is not in `.gitignore` — could be committed accidentally
- May contain secrets or context from an in-progress edit state

**Fix**: Delete the file. Add `*.tmp.*` and `*.tmp` to `.gitignore` for the backend.

**Files**: `backend/app/main.py.tmp.2869.1774109189155`, `backend/.gitignore` (or root `.gitignore`)

---

### Gap 3 — Cross-Org HTTP Route Isolation Audit (High)

**Current behavior**: The STT WebSocket (`/ws/stt/deepgram`) correctly verifies org membership via `_can_host(org_id, host_uid, host_token)` before accepting. But HTTP routes in `routes/billing.py`, `routes/auth.py`, and others accept an `orgId` from the request and must verify the caller is actually a member of that org.

**Risk pattern**: If any route accepts `orgId` from a query param or request body and performs a Firestore read/write without verifying `uid ∈ org.members`, a caller can supply any `orgId` and access another organization's data (billing state, usage data, service config).

**Proposed fix**: Audit every route that accepts `orgId` as a parameter — either from the URL path, query string, or request body. Verify each either:
1. Uses `Depends(get_current_user_required)` + explicitly checks org membership, OR
2. Uses `Depends(require_org_role(...))` from `auth/guards.py`

Document the audit result as a table in the design document.

**Files**: `backend/app/routes/billing.py`, `backend/app/routes/auth.py`, `backend/app/auth/guards.py`

---

### Gap 4 — Firestore Public Read: Rooms and Services (Medium — Accepted Risk with Documentation)

**Current rules**:
```
match /services/{serviceKey} { allow read: if true; }
match /rooms/{roomId}         { allow read: if true; }
```

**What this exposes**: Anyone who knows an `orgId` + `roomId` can read the full live room document, which includes `liveTranscript`, room status, `lastAudioAt`, and session metadata. Similarly, service documents (service name, key, target language) for any org are publicly readable.

**Why it's intentional**: The listener join page resolves a `churchSlug` to an `orgId` without authentication. The `TranslationBox` consumer needs to read room state in real time. Requiring auth on these would break unauthenticated listener access.

**Actual risk level**: Low-medium. To read a room, you need both `orgId` (not guessable — a Firestore document ID) and `roomId` (a UUID). Getting these requires either the listener join URL or the `/ws/translate` status response. This is essentially "security through obscurity" for the room content, but it is workable given the public-facing nature of church broadcasts.

**Proposed action**: Document this as a deliberate accepted risk in the design doc. Do NOT change the rules right now (it would break listener UX). Flag for a future enhancement: optionally scope room reads to a short-lived signed listener token that the backend issues on `/join`.

---

### Gap 5 — NEXT_PUBLIC_ Secret Audit (Medium)

**What to verify**: Every `NEXT_PUBLIC_` variable in `frontend/.env.local.example` and any `NEXT_PUBLIC_` usage in the frontend code must be intentionally public. The following are safe:
- `NEXT_PUBLIC_API_BASE_URL` — public URL, expected
- `NEXT_PUBLIC_WS_URL` — public URL, expected
- `NEXT_PUBLIC_FIREBASE_API_KEY` — Firebase web config, not a secret (protected by Auth rules + App Check)
- `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN`, `PROJECT_ID`, `APP_ID`, `MESSAGING_SENDER_ID`, `STORAGE_BUCKET` — standard web config
- `NEXT_PUBLIC_BILLING_ADMIN_EMAILS` — low-risk admin hint

**What must NOT be in NEXT_PUBLIC_**: OpenAI API key, Stripe secret key, Firebase Admin service account JSON, any webhook secret, any backend-only env var.

**Proposed action**: Run a grep for `NEXT_PUBLIC_` across the entire repo including `.env.local` and Vercel/Cloud Run deployment configs. Confirm none of the above are present. Document the result.

**Files**: `frontend/.env.local`, Vercel environment settings, `frontend/` source

---

### Gap 6 — Log Privacy Audit (Medium)

**What to verify**: Backend logs (`print(...)` statements) must not include:
- Firebase ID tokens or host tokens
- Raw email addresses (hash them or omit)
- Full Korean/English transcript text in error paths
- OpenAI API key or Deepgram API key in exception messages

**Risk**: Cloud Run logs are accessible to anyone with Cloud Run viewer permissions. If tokens or emails appear in logs, a compromised GCP account exposes auth material.

**Proposed action**: Grep backend for `print(` calls that include variables likely to contain PII or secrets. Audit the most sensitive handlers (`ws_stt_deepgram`, `ws_translate`, `handle_commit`). Document findings.

**Files**: `backend/app/main.py`, `backend/app/routes/auth.py`, `backend/app/services/multichurch_store.py`

---

### Gap 7 — Firebase App Check (Low — Future Enhancement)

**Current state**: App Check is not configured. Any HTTP client (not just the web app) can call the backend by obtaining a valid Firebase ID token.

**Risk**: Scrapers, bots, or malicious users can register accounts and call authenticated APIs programmatically without restrictions beyond existing rate limits.

**Proposed action**: Evaluate App Check for the web frontend. Do NOT implement now — App Check requires backend verification and has implications for CI/testing. Flag as a future enhancement after the higher-priority gaps are closed.

---

## 3. Non-Goals

- Re-architecting listener auth to require accounts (public church broadcasts are intentionally unauthenticated)
- Adding IP-based blocking or WAF rules (Cloud Run + existing rate limits are sufficient for now)
- Implementing signed listener room tokens now (complex, deferred to Gap 4 future path)
- Changing Firestore read rules for services or rooms (breaks listener UX)
- Two-factor authentication (separate initiative)

---

## 4. User Stories

| As a... | I want to... | So that... |
|---|---|---|
| Platform operator | Know the API schema is not publicly browsable in production | An attacker cannot use our own docs to plan an attack |
| Platform operator | Confirm no real secrets are in the frontend bundle | A user who inspects the browser network tab cannot extract backend credentials |
| Security auditor | See that every org-scoped HTTP route verifies org membership | I can certify that cross-org data access is not possible |
| Platform operator | Know that logs do not contain Firebase tokens or email addresses | A GCP access breach does not also become an auth breach |

---

## 5. Scope

### In Scope (Ordered by Priority)

**Backend (`backend/app/main.py`)**
- P0: Disable `/docs`, `/redoc`, `/openapi.json` in production via `_IS_PRODUCTION` flag
- P1: Delete `main.py.tmp.2869.1774109189155`

**Backend (`backend/app/routes/` + `backend/app/auth/guards.py`)**
- P1: Audit every HTTP route that accepts `orgId` — verify org membership check is present and inside the route, not assumed
- P1: Document audit result table (route → auth check method → verdict)

**Repo hygiene**
- P1: Add `*.tmp.*` pattern to `.gitignore`

**Secrets audit**
- P2: Grep `NEXT_PUBLIC_` usage across frontend and deployment configs — confirm no secrets
- P2: Document result (all clear or list of violations)

**Log audit**
- P2: Grep backend print/log statements for token, email, transcript variables
- P2: Redact or omit any PII/secret-adjacent values found

### Out of Scope

- Firestore rule changes for rooms/services (accepted risk, documented)
- Firebase App Check implementation (future enhancement)
- Signed listener tokens (future enhancement)
- Infrastructure changes (Cloud Run visibility settings are already correct)

---

## 6. Affected Files

| File | Change |
|---|---|
| `backend/app/main.py:246` | Add `docs_url`, `redoc_url`, `openapi_url` conditionals |
| `backend/app/main.py.tmp.2869.1774109189155` | Delete |
| `backend/.gitignore` (or root) | Add `*.tmp.*` pattern |
| `backend/app/routes/billing.py` | Audit only (no change expected if guards are correct) |
| `backend/app/routes/auth.py` | Audit only |
| `backend/app/auth/guards.py` | Audit only; possible tightening if gaps found |

---

## 7. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Disabling docs breaks developer workflow | Low | Docs still available in development (`_IS_PRODUCTION` is False locally) |
| Cross-org audit finds a real hole | Medium | Fix before shipping; add regression test for org membership check |
| NEXT_PUBLIC_ audit finds a leaked secret | Low | Rotate the key, audit git history, update deployment config |
| Log audit finds tokens in logs | Low-Medium | Redact in-place; Cloud Run log retention policy limits historical exposure |

---

## 8. Acceptance Criteria

- [ ] `GET /docs` returns 404 on the production Cloud Run URL
- [ ] `GET /redoc` returns 404 on the production Cloud Run URL
- [ ] `GET /openapi.json` returns 404 on the production Cloud Run URL
- [ ] `backend/app/main.py.tmp.2869.1774109189155` does not exist in the repo
- [ ] `*.tmp.*` is in `.gitignore`
- [ ] Cross-org audit table is completed: every `orgId`-accepting HTTP route has a verified membership check
- [ ] `grep -r "NEXT_PUBLIC_" frontend/` returns no secrets (OpenAI key, Stripe secret, service account)
- [ ] Backend log audit returns no raw Firebase ID tokens, no plaintext email addresses in hot-path print statements
- [ ] `npm run lint` passes
- [ ] All existing functionality (host console, listener join, billing) works unchanged after production docs disable

---

## 9. Relation to Existing Security Plans

This plan is additive — it does not duplicate existing work:

| Existing Plan | Scope | This Plan |
|---|---|---|
| `auth-hardening` | Account creation, email verification, rate limits on auth endpoints | API schema exposure, org isolation audit, secrets/log hygiene |
| `full-risk-analysis` | Deepgram leak, segment save, session storage, billing race | Docs disable, temp file cleanup, cross-org isolation audit |

---

## 10. Next Steps

```
/pdca design security-hardening   ← Detailed component design + audit tables
/pdca do security-hardening        ← Implementation (P0 → P1 → P2 order)
/pdca analyze security-hardening   ← Gap analysis
```
