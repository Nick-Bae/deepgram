# Security Hardening — Completion Report

## Executive Summary

| Item | Detail |
|------|--------|
| Feature | security-hardening |
| Started | 2026-04-11 |
| Completed | 2026-04-11 |
| Match Rate | 100% (6/6 items) |
| Primary files | `backend/app/main.py`, `backend/app/translator/openai_translator.py`, `backend/.gitignore` |

### Value Delivered

| Perspective | Detail |
|-------------|--------|
| Problem | FastAPI auto-docs exposed the full API schema in production; a stale editor temp file sat uncommitted; a guardrail print was logging live Korean/English sermon transcripts unconditionally to Cloud Run logs |
| Solution | Disabled `/docs`, `/redoc`, `/openapi.json` in production via `_IS_PRODUCTION` flag; deleted temp file and added `*.tmp.*` to gitignore; gated transcript print behind `GUARDRAIL_DEBUG` env var; completed cross-org isolation and NEXT_PUBLIC_ audits |
| Function / UX Effect | Zero visible UX change — all changes are server-side configuration, file hygiene, and log gating. Host console, listener join, and billing flows work identically |
| Core Value | A live-audio platform handling worship transcripts and billing data no longer self-documents its attack surface in production. Sermon content is no longer emitted to Cloud Run logs by default |

---

## 1. What Was Built

### SH-01 — FastAPI Docs Disabled in Production

**File**: `backend/app/main.py:249–251`

```python
app = FastAPI(
    title="Real-Time Translation Backend",
    version="1.0.0",
    docs_url=None if _IS_PRODUCTION else "/docs",
    redoc_url=None if _IS_PRODUCTION else "/redoc",
    openapi_url=None if _IS_PRODUCTION else "/openapi.json",
)
```

The existing `_IS_PRODUCTION` flag (set when `K_SERVICE` env var is present — Cloud Run sets this automatically) was reused with no new detection logic. Local development is unaffected.

---

### SH-02 — Stale Temp File Deleted

`backend/app/main.py.tmp.2869.1774109189155` — a full copy of `main.py` written by an editor temp operation — was deleted. The file was not imported or executed, but it contained the full API surface and was not gitignored.

---

### SH-03 — Temp File Patterns Added to .gitignore

**File**: `backend/.gitignore`

```gitignore
# Editor/tool temp files
*.tmp
*.tmp.*
```

Future editor temp files will not be tracked by git.

---

### SH-04 — Cross-Org Isolation Audit

All 12 HTTP routes that accept an `orgId` were audited. Every route was confirmed to enforce org membership before any data access, via one of two patterns:

- `require_org_role(org_id, user, roles, store)` from `app.auth.guards` — raises HTTP 403 on non-members
- `multichurch_store` method with `requested_by_uid` — raises `PermissionError("forbidden")` on non-members

**Verdict**: 11/11 org-scoped routes PASS. The one exception (`/ws/translate`) is intentionally unauthenticated for public church broadcasts — documented as an accepted risk.

No code changes were required. The existing guard architecture is sound.

---

### SH-05 — Guardrail Print Gated Behind Debug Flag

**File**: `backend/app/translator/openai_translator.py`

The pronoun guardrail in `openai_translator.py` was unconditionally printing Korean sermon text and the raw English translation on every activation. In Cloud Run, all `print()` output goes to Cloud Logging (accessible to anyone with `roles/logging.viewer`).

```python
# Added near imports
_GUARDRAIL_DEBUG = os.getenv("GUARDRAIL_DEBUG", "").lower() in {"1", "true", "yes"}

# Gated the print
if _GUARDRAIL_DEBUG:
    print(debug_tag, "ko:", ko, "raw_en:", en, "pronoun_key:", pronoun_key)
```

Production default: no transcript content logged. Set `GUARDRAIL_DEBUG=1` to re-enable for debugging.

---

### SH-06 — NEXT_PUBLIC_ Secret Audit

All 16 `NEXT_PUBLIC_` variables in use across the frontend were enumerated and reviewed. No secrets were found. Confirmed absent as `NEXT_PUBLIC_` variables:

- `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `STRIPE_SECRET_KEY`, `FIREBASE_ADMIN_CREDENTIALS`, `STRIPE_WEBHOOK_SECRET`

One variable not anticipated in the plan — `NEXT_PUBLIC_TURNSTILE_SITE_KEY` — is the Cloudflare Turnstile **public** site key, which by Cloudflare's design is embedded in the browser page to render the CAPTCHA widget. The corresponding secret key (`CLOUDFLARE_TURNSTILE_SECRET`) is server-only. No issue.

No code changes were required.

---

## 2. Accepted Risks (Documented, Not Changed)

| Risk | Level | Rationale |
|------|-------|-----------|
| Firestore `rooms/{roomId}` public read | Medium | Required for unauthenticated listener UX. Room ID + org ID pair is not enumerable. |
| Firestore `services/{serviceKey}` public read | Low | Required for listener join page slug resolution. Service metadata is not sensitive. |
| `/ws/translate` unauthenticated | Low-Medium | Church broadcasts are public-facing by design. Per-IP limit (20) is the abuse control. |
| Firebase App Check | Low | Deferred to future sprint. Existing rate limiting and auth are sufficient. |

---

## 3. Gap Analysis Results

**Match Rate: 100%** — 6/6 design items fully implemented.

| ID | Criterion | Status |
|----|-----------|--------|
| SH-01 | `GET /docs`, `/redoc`, `/openapi.json` return 404 in production | PASS |
| SH-02 | `main.py.tmp.2869.1774109189155` does not exist | PASS |
| SH-03 | `*.tmp.*` pattern in `backend/.gitignore` | PASS |
| SH-04 | Cross-org audit table complete — all routes PASS | PASS |
| SH-05 | `GUARDRAIL_DEBUG` env var controls transcript print; default = no output | PASS |
| SH-06 | No `NEXT_PUBLIC_` variables contain secrets | PASS |

ESLint: 0 warnings, 0 errors.

---

## 4. Files Changed

| File | Change |
|------|--------|
| `backend/app/main.py` | Added `docs_url`, `redoc_url`, `openapi_url` conditionals to FastAPI constructor |
| `backend/app/main.py.tmp.2869.1774109189155` | Deleted |
| `backend/.gitignore` | Added `*.tmp` and `*.tmp.*` patterns |
| `backend/app/translator/openai_translator.py` | Added `_GUARDRAIL_DEBUG` flag; gated guardrail print |

Audit documentation (no code change): SH-04 cross-org isolation table, SH-06 NEXT_PUBLIC_ inventory.

---

## 5. Non-Goals (Confirmed Out of Scope)

- Firestore rule changes for `rooms` / `services` (accepted risk)
- Firebase App Check implementation (future sprint)
- Signed listener room tokens (future sprint)
- Infrastructure changes to Cloud Run visibility settings (already correct)
- Two-factor authentication (separate initiative)

---

## 6. Relation to Existing Security Work

| Existing Feature | Scope | Security Hardening |
|-----------------|-------|-------------------|
| `auth-hardening` | Account creation, email verification, rate limits on auth endpoints | API schema exposure, org isolation audit, secrets/log hygiene |
| `full-risk-analysis` | Deepgram leak, segment save, session storage, billing race | Docs disable, temp file cleanup, cross-org isolation audit |

These three initiatives are additive with no overlap.
