# Analysis: Auth Hardening

> Feature: auth-hardening
> Phase: Check
> Date: 2026-03-20
> Match Rate: **100% (46/46)**
> Status: **PASS**

---

## Summary

| Metric | Value |
|---|---|
| Match Rate | 100% |
| Total items checked | 46 |
| Matched | 46 |
| Gaps | 1 found + fixed (missing `API_URL` import in `login.tsx`) |
| Lint | Clean (0 errors) |

One gap was detected by the gap-detector agent: `API_URL` was referenced in `reportLoginFailed()` but not imported in `login.tsx`. Fixed immediately by adding `import { API_URL } from "../utils/urls";`. Lint re-run confirmed clean. Final match rate: 100%.

---

## Item-by-Item Results

### Gap 2 — Backend: bootstrap-owner rate limit

| # | Requirement | Status |
|---|---|:---:|
| 1 | `_BOOTSTRAP_ORG_RATE_WINDOW_SECONDS` constant via `_env_int` | MATCH |
| 2 | `_BOOTSTRAP_ORG_RATE_MAX` constant via `_env_int` | MATCH |
| 3 | `_bootstrap_org_rate_hits: Dict[str, Deque[float]]` | MATCH |
| 4 | `_bootstrap_org_rate_lock = Lock()` | MATCH |
| 5 | `_enforce_bootstrap_org_rate_limit(uid)` function | MATCH |
| 6 | Call to `_enforce_bootstrap_org_rate_limit(user.uid)` in `auth_bootstrap_owner` | MATCH |

### Gap 4 — Backend: login-failed endpoint

| # | Requirement | Status |
|---|---|:---:|
| 7 | `_LOGIN_FAILED_RATE_WINDOW_SECONDS = 60` | MATCH |
| 8 | `_LOGIN_FAILED_RATE_MAX_PER_IP = 30` | MATCH |
| 9 | `_login_failed_ip_hits: Dict[str, Deque[float]]` | MATCH |
| 10 | `_login_failed_rate_lock = Lock()` | MATCH |
| 11 | `_enforce_login_failed_rate_limit(ip)` function | MATCH |
| 12 | `LoginFailedRequest` model with `email_hash` field | MATCH |
| 13 | `@router.post("/auth/login-failed")` endpoint exists | MATCH |
| 14 | Endpoint calls `_enforce_login_failed_rate_limit(ip)` | MATCH |
| 15 | Endpoint calls `security_event("login_failed", ...)` | MATCH |
| 16 | Endpoint returns `{"ok": True}` | MATCH |

### Gap 3 — Frontend: signup.tsx password strength

| # | Requirement | Status |
|---|---|:---:|
| 17 | `passwordStrength()` helper function | MATCH |
| 18 | `minLength={8}` on first password field | MATCH |
| 19 | `minLength={8}` on confirm password field | MATCH |
| 20 | Strength indicator JSX inline after first password field | MATCH |
| 21 | `mapFirebaseError` updated to "at least 8 characters" | MATCH |

### Gap 4 — Frontend: login.tsx report login failure

| # | Requirement | Status |
|---|---|:---:|
| 22 | `hashEmail()` helper function | MATCH |
| 23 | `reportLoginFailed()` helper function (fire-and-forget) | MATCH |
| 24 | `API_URL` imported from `../utils/urls` | MATCH (fixed) |
| 25 | `onSubmit` catch checks `auth/too-many-requests` | MATCH |
| 26 | `onSubmit` catch checks `auth/wrong-password` | MATCH |
| 27 | `onSubmit` catch checks `auth/invalid-credential` | MATCH |
| 28 | Calls `void reportLoginFailed(email.trim())` | MATCH |

### Gap 1 — Frontend: host console email verification gate

| # | Requirement | Status |
|---|---|:---:|
| 29 | `getFirebaseClient` import added | MATCH |
| 30 | `verificationRequired` state variable | MATCH |
| 31 | `verificationSending` state variable | MATCH |
| 32 | `verificationError` state variable | MATCH |
| 33 | `useEffect` sets `verificationRequired = !user.emailVerified` | MATCH |
| 34 | `handleResendVerification` callback (calls `sendEmailVerification`) | MATCH |
| 35 | `handleCheckVerification` callback (calls `user.reload()` + checks `emailVerified`) | MATCH |
| 36 | Verification prompt render block | MATCH |

### Testing Checklist

| # | Requirement | Status |
|---|---|:---:|
| 37 | `bootstrap-owner` returns 429 on 4th attempt per UID per hour | MATCH |
| 38 | `login-failed` with `{ email_hash }` returns 200 + logs `security_event` | MATCH |
| 39 | `login-failed` returns 429 after 31 calls/min from same IP | MATCH |
| 40 | Password field requires min 8 chars | MATCH |
| 41 | Strength indicator weak/ok/strong logic correct | MATCH |
| 42 | Host console shows verification prompt when `emailVerified === false` | MATCH |
| 43 | Resend button calls `sendEmailVerification()` + loading state | MATCH |
| 44 | Check Again calls `user.reload()` + dismisses if verified | MATCH |
| 45 | Verified user sees normal console immediately | MATCH |
| 46 | `npm run lint` passes — 0 errors | MATCH |

---

## Gap Found & Fixed

| Gap | File | Issue | Fix Applied |
|---|---|---|---|
| Missing `API_URL` import | `frontend/pages/login.tsx` | `reportLoginFailed()` referenced `API_URL` (from design spec) but it was not imported — would cause a `ReferenceError` silently swallowed by the fire-and-forget catch | Added `import { API_URL } from "../utils/urls";` |

---

## Action Required

None. All 46 items match. Proceed to `/pdca report auth-hardening`.
