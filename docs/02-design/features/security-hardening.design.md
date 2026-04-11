# Design: Security Hardening

> Reference: [Plan](../../01-plan/features/security-hardening.plan.md)

## 1. Change Inventory

| ID | Priority | Change | File | Type |
|---|---|---|---|---|
| SH-01 | P0 | Disable FastAPI docs in production | `backend/app/main.py:246` | Code change |
| SH-02 | P1 | Delete stale temp file | `backend/app/main.py.tmp.2869.1774109189155` | File deletion |
| SH-03 | P1 | Add `*.tmp.*` to gitignore | `backend/.gitignore` | Config |
| SH-04 | P1 | Cross-org isolation audit — document results | (audit only, no code change) | Documentation |
| SH-05 | P2 | Gate transcript print behind debug flag | `backend/app/translator/openai_translator.py:242` | Code change |
| SH-06 | P2 | NEXT_PUBLIC_ secret audit — document results | (audit only, no code change) | Documentation |

---

## 2. SH-01 — Disable FastAPI Docs in Production

### Context

`FastAPI(...)` is initialized at `backend/app/main.py:246` with no `docs_url`, `redoc_url`, or `openapi_url` overrides. FastAPI defaults to:
- `/docs` — Swagger UI (interactive API explorer)
- `/redoc` — ReDoc documentation
- `/openapi.json` — full machine-readable schema

`_IS_PRODUCTION` is already defined at `main.py:176` using `K_SERVICE` env var (Cloud Run sets this automatically). No new detection logic is needed.

### Change

**File**: `backend/app/main.py`  
**Line**: 246  
**Type**: In-place constructor update

```python
# BEFORE
app = FastAPI(title="Real-Time Translation Backend", version="1.0.0")

# AFTER
app = FastAPI(
    title="Real-Time Translation Backend",
    version="1.0.0",
    docs_url=None if _IS_PRODUCTION else "/docs",
    redoc_url=None if _IS_PRODUCTION else "/redoc",
    openapi_url=None if _IS_PRODUCTION else "/openapi.json",
)
```

### Behavior after change

| Environment | `/docs` | `/redoc` | `/openapi.json` |
|---|---|---|---|
| Local dev (`K_SERVICE` not set) | 200 — accessible | 200 — accessible | 200 — accessible |
| Cloud Run production | 404 — Not Found | 404 — Not Found | 404 — Not Found |

### Verification

```bash
# Production (after deploy)
curl -o /dev/null -s -w "%{http_code}" https://your-backend.run.app/docs
# Expected: 404

# Local dev
curl -o /dev/null -s -w "%{http_code}" http://localhost:8000/docs
# Expected: 200
```

---

## 3. SH-02 — Delete Stale Temp File

### Context

`backend/app/main.py.tmp.2869.1774109189155` exists on disk. This is a leftover from an editor write operation. It contains a full copy of `main.py` content at the time it was written and is not in `.gitignore`.

### Change

```bash
rm backend/app/main.py.tmp.2869.1774109189155
```

No code change required. Just deletion.

---

## 4. SH-03 — Add Temp File Pattern to gitignore

### Context

`backend/.gitignore` currently contains:
```
.env
cert.pem
key.pem
# Python cache
__pycache__/
*.py[cod]
*$py.class
```

### Change

**File**: `backend/.gitignore`  
**Type**: Append

```gitignore
# Editor/tool temp files
*.tmp
*.tmp.*
```

---

## 5. SH-04 — Cross-Org Isolation Audit

### Audit Method

Every HTTP route that accepts an `orgId` (from URL path, query string, or request body) was reviewed to confirm it either:
1. Calls `require_org_role(org_id, user, roles, store)` from `app.auth.guards` — raises HTTP 403 if caller is not a member with required role, OR
2. Delegates to a `multichurch_store` method that accepts `requested_by_uid` / `created_by_uid` and internally calls `_member_role()` → raises `PermissionError("forbidden")` if the caller is not a member

Both patterns are equivalent: both verify Firebase-authenticated identity + Firestore org membership before any data access.

### Audit Results

| Route | orgId Source | Auth Check | Pattern | Verdict |
|---|---|---|---|---|
| `GET /billing/org/{org_id}/status` | URL path | `require_org_role(org_id, user, {"owner","admin","host"})` | Guard | PASS |
| `POST /billing/checkout-session` | Request body `body.orgId` | `require_org_role(body.orgId, user, {"owner","admin"})` | Guard | PASS |
| `POST /billing/portal-session` | Request body `body.orgId` | `require_org_role(body.orgId, user, {"owner","admin"})` | Guard | PASS |
| `POST /billing/change-plan` | Request body `body.orgId` | `require_org_role(body.orgId, user, {"owner","admin"})` | Guard | PASS |
| `GET /auth/org/{org_id}/billing-limits` | URL path | `store.get_org_billing_limits(org_id, requested_by_uid=user.uid)` → `PermissionError` | Store | PASS |
| `POST /auth/org/{org_id}/billing-limits` | URL path | `store.set_org_billing_limits(org_id, requested_by_uid=user.uid)` → `PermissionError` | Store | PASS |
| `POST /auth/org/{org_id}/invites` | URL path | `store.create_invite(org_id, created_by_uid=user.uid)` → role check | Store | PASS |
| `GET /auth/org/{org_id}/invites` | URL path | `store.list_invites(org_id, requested_by_uid=user.uid)` → `PermissionError` | Store | PASS |
| `POST /auth/current-org` | Request body `payload.orgId` | `store.set_current_org(user.uid, orgId)` → member check → `PermissionError("org_access_denied")` | Store | PASS |
| `DELETE /auth/org/{org_id}/members/{uid}` | URL path | `store.remove_member(org_id, requested_by_uid=user.uid)` → role check | Store | PASS |
| `GET /ws/stt/deepgram` | Query param `orgId` | `_can_host(org_id, host_uid, host_token)` before `await websocket.accept()` | Pre-accept | PASS |
| `GET /ws/translate` | Query param `orgId` | Requires valid `orgId`+`roomId` pair; listener is intentionally unauthenticated | Intentional | ACCEPTED |

### Verdict

**No cross-org isolation gaps found.** Every route that accepts an `orgId` enforces membership before any data operation. The patterns are consistent across billing and auth routes.

The one intentional exception is `/ws/translate` (listener), which is unauthenticated by design — church broadcasts are public-facing. This is an accepted risk documented in the plan.

### No code change required for SH-04.

---

## 6. SH-05 — Gate Transcript Print Behind Debug Flag

### Context

`backend/app/translator/openai_translator.py:242`:

```python
debug_tag = "[guardrail]"
print(debug_tag, "ko:", ko, "raw_en:", en, "pronoun_key:", pronoun_key)
```

This `print` statement fires unconditionally whenever the pronoun guardrail activates — which happens on most Korean sentences that contain first-person subjects. It logs:
- `ko`: the raw Korean sermon transcript text
- `raw_en`: the initial English translation

Both are live worship content. In Cloud Run, all `print()` output goes to Cloud Logging (Stackdriver), which is accessible to anyone with `roles/logging.viewer` on the project. This is transcript privacy leakage.

### Change

**File**: `backend/app/translator/openai_translator.py`

Add a module-level debug flag, then gate the print:

```python
# Add near top of file (with other constants/imports)
_GUARDRAIL_DEBUG = os.getenv("GUARDRAIL_DEBUG", "").lower() in {"1", "true", "yes"}
```

```python
# Line ~242: change unconditional print to gated print
debug_tag = "[guardrail]"
if _GUARDRAIL_DEBUG:
    print(debug_tag, "ko:", ko, "raw_en:", en, "pronoun_key:", pronoun_key)
```

### Behavior after change

| Condition | Log output |
|---|---|
| Production (`GUARDRAIL_DEBUG` not set) | No transcript text logged |
| `GUARDRAIL_DEBUG=1` set | Full Korean+English text logged (opt-in for debugging) |

### Note on other print statements

The remaining `print()` calls in the codebase (DG keyword counts, session IDs, billing events) do not log PII or transcript text. They log counts, IDs, and error messages — all safe for Cloud Run logs. No other changes are needed.

---

## 7. SH-06 — NEXT_PUBLIC_ Secret Audit

### Audit Method

Grep `NEXT_PUBLIC_` across all frontend files and confirm each variable is intentionally public.

```bash
grep -r "NEXT_PUBLIC_" frontend/ --include="*.ts" --include="*.tsx" --include="*.env*" | grep -v node_modules
```

### Variables in Use

| Variable | Used In | Assessment |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | `utils/urls.ts` | Public backend URL — safe |
| `NEXT_PUBLIC_WS_URL` | `lib/useDeepgramProducer.ts` | Public WebSocket URL — safe |
| `NEXT_PUBLIC_FIREBASE_API_KEY` | Firebase client init | Firebase web config — not a secret (protected by Auth Rules + Security Rules) |
| `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN` | Firebase client init | Public config — safe |
| `NEXT_PUBLIC_FIREBASE_PROJECT_ID` | Firebase client init | Public config — safe |
| `NEXT_PUBLIC_FIREBASE_APP_ID` | Firebase client init | Public config — safe |
| `NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID` | Firebase client init | Public config — safe |
| `NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET` | Firebase client init | Public config — safe |
| `NEXT_PUBLIC_BILLING_ADMIN_EMAILS` | Admin UI hint | Low-risk — safe |
| `NEXT_PUBLIC_PCM_WORKLET_URL` | Audio worklet override | Optional URL — safe |
| `NEXT_PUBLIC_DEBUG` | Debug flag | Dev-only flag — safe |

### Confirmed Absent (must never appear in NEXT_PUBLIC_)

These were searched and are **not present** as `NEXT_PUBLIC_` variables:
- `OPENAI_API_KEY` — backend only, in `backend/.env`
- `DEEPGRAM_API_KEY` — backend only
- `STRIPE_SECRET_KEY` — backend only
- `FIREBASE_ADMIN_CREDENTIALS` — backend only (service account path)
- `STRIPE_WEBHOOK_SECRET` — backend only

### Verdict

**No secret leakage found in NEXT_PUBLIC_ variables.** All frontend-exposed variables are either public URLs, Firebase web configuration (which is designed to be public), or dev-only flags.

### No code change required for SH-06.

---

## 8. Implementation Order

```
Step 1 — SH-02: Delete temp file (1 minute)
  rm backend/app/main.py.tmp.2869.1774109189155

Step 2 — SH-03: Update .gitignore (1 minute)
  Add *.tmp and *.tmp.* to backend/.gitignore

Step 3 — SH-01: Disable docs in production (5 minutes)
  Edit main.py FastAPI constructor — add 3 keyword args

Step 4 — SH-05: Gate guardrail print (5 minutes)
  Add _GUARDRAIL_DEBUG flag + gate the print in openai_translator.py

Step 5 — SH-04 + SH-06: Documentation only
  Audit tables above serve as the documentation artifact
```

Total estimated implementation time: ~15 minutes.

---

## 9. Accepted Risks (Non-Changes)

| Item | Risk Level | Reason Not Changed |
|---|---|---|
| Firestore `rooms/{roomId}` public read | Medium | Intentional — listener UX requires unauthenticated room state reads. Room + org ID pair is not enumerable. Documented here as explicit accepted risk. |
| Firestore `services/{serviceKey}` public read | Low | Intentional — service resolution for listener join page. Service metadata (name, language pair) is not sensitive. |
| `/ws/translate` unauthenticated | Low-Medium | Intentional — church broadcasts are public-facing. Per-IP connection limit (20) is the abuse control. |
| Firebase App Check | Low | Enhancement for future sprint. Requires backend verification infrastructure. Current rate limiting and auth are sufficient. |

---

## 10. Regression Risk Assessment

| Change | Can Break Existing Functionality? | Mitigation |
|---|---|---|
| SH-01 (docs disable) | No — only affects `/docs`, `/redoc`, `/openapi.json` | Local dev unaffected; production returns 404 instead of 200 |
| SH-02 (temp file delete) | No — temp file is not imported or executed | N/A |
| SH-03 (gitignore) | No — gitignore does not affect runtime | N/A |
| SH-05 (guardrail print gate) | No — removes console output only, not logic | Guardrail correction logic unchanged; only the `print` is gated |
| SH-06 (audit) | No — documentation only | N/A |

**Zero functional regression risk.** All changes are either documentation, configuration, or removing/gating print statements.

---

## 11. Acceptance Criteria (from Plan)

- [ ] SH-01: `GET /docs` returns 404 in production
- [ ] SH-01: `GET /redoc` returns 404 in production
- [ ] SH-01: `GET /openapi.json` returns 404 in production
- [ ] SH-01: All three endpoints still return 200 in local dev
- [ ] SH-02: `backend/app/main.py.tmp.2869.1774109189155` does not exist
- [ ] SH-03: `*.tmp.*` pattern is in `backend/.gitignore`
- [ ] SH-04: Cross-org audit table complete — all routes verified PASS
- [ ] SH-05: `GUARDRAIL_DEBUG` env var controls the guardrail print; default is no output
- [ ] SH-06: No `NEXT_PUBLIC_` variables contain secrets
- [ ] All host console, listener join, and billing flows work unchanged
- [ ] `npm run lint` passes
