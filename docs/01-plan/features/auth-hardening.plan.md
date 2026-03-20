# Plan: Auth Hardening

## Executive Summary

| Perspective | Description |
|---|---|
| **Problem** | New accounts are fully functional without email verification, org creation has no rate limit, and password requirements are minimal — creating vectors for fake accounts, spam org registration, and weak credentials. |
| **Solution** | Add email verification gating on first host action, rate limiting on `bootstrap-owner-org`, and stronger password requirements with client + server enforcement. |
| **Function / UX Effect** | Unverified users see a one-time verification prompt before accessing the host console; rapid signups are throttled silently; weak passwords are rejected at signup with clear feedback. |
| **Core Value** | Trustworthy platform access — only real users with real email addresses can create and operate church workspaces. |

---

## 1. What's Already Well-Hardened

Before listing gaps, it's important to record what's solid so future changes don't regress it:

| Area | Status | Location |
|---|---|---|
| Firebase ID token verification | All authenticated endpoints | `auth/firebase_auth.py:get_current_user_required` |
| CORS explicit allowlist | No wildcard in production | `main.py:CORSMiddleware` |
| Password reset rate limiting | 5/IP/hour + 3/email/hour | `auth.py:_enforce_password_reset_rate_limit` |
| Invite rate limiting | 20 create / 40 redeem / 120 preview per window | `auth.py:_enforce_invite_rate_limit` |
| `?next=` redirect validation | Must start with `/`, not `//` | `login.tsx:74-78`, `signup.tsx:45-49` |
| `hostToken` stripped from API responses | Never leaks to client | `auth.py:_sanitize_membership_payload` |
| XFF proxy header handling | Configurable `TRUSTED_PROXY_COUNT` | `auth.py:_client_ip` |
| Security event logging | Sensitive actions logged | `security_log.py:security_event` |
| Hard redirect on logout | `window.location.replace("/")` + clears all sessionStorage | `authContext.tsx:logout` |
| `super_admin` claim gating | Admin routes require Firebase custom claim | `firebase_auth.py:_SUPER_CLAIM_KEYS` |

---

## 2. Gaps Identified

### Gap 1 — No Email Verification (High)

**Current behavior**: After `createUserWithEmailAndPassword`, the account is immediately active with full org-creation rights. An attacker can register with any email address (including someone else's) without the real owner knowing.

**Risk**:
- Someone registers `pastor@realchurch.com` without owning it
- No way to distinguish real users from bot-registered accounts
- Allows typo-squatting of org slugs

**Proposed fix**: Gate the first host action (entering the host console `/host/c/{slug}/broadcast`) behind an email verification check. Show a re-sendable verification prompt instead of the console if `user.emailVerified === false`. Do not block signup itself or the invite-join flow (invite verifies intent).

---

### Gap 2 — No Rate Limit on `POST /api/auth/bootstrap-owner-org` (High)

**Current behavior**: The org creation endpoint has no rate limiting. A script could call it hundreds of times with different slugs, exhausting the slug namespace and creating Firestore noise.

**Risk**: Namespace squatting, Firestore write cost spike, junk data in admin dashboard.

**Proposed fix**: Add in-memory rate limiting matching the existing pattern (`_invite_rate_limit`): max 3 org-creation attempts per UID per hour. Return HTTP 429 on breach.

---

### Gap 3 — Weak Password Requirements (Medium)

**Current behavior**: Only `minLength={6}` enforced client-side by HTML attribute. No complexity check. Backend (Firebase) has its own 6-char minimum but no complexity policy.

**Risk**: Users create passwords like `111111` or `aaaaaa`, vulnerable to credential stuffing.

**Proposed fix**: Client-side only (Firebase controls the backend rule):
- Minimum 8 characters (up from 6)
- At least 1 non-letter character (number or symbol)
- Show a strength indicator: weak / ok / strong
- Update `minLength={6}` → `minLength={8}` on both password fields in `signup.tsx`

Note: Firebase Admin SDK does not support custom password policies via the client SDK — enforcement is client-side. A backend check on the signup token would require reimplementing password handling, which is out of scope.

---

### Gap 4 — No Failed Login Logging (Low)

**Current behavior**: Firebase handles lockout after too many failed attempts (`auth/too-many-requests`). The backend never sees failed logins — they happen entirely client-side via the Firebase client SDK. No server-side record of failed login patterns.

**Risk**: Cannot detect credential-stuffing campaigns that stay below Firebase's threshold across multiple IPs.

**Proposed fix**: Add a `POST /api/auth/login-failed` endpoint that the frontend calls on `auth/too-many-requests` or repeated `auth/wrong-password`. Logs to `security_event` with IP and email hash (not plaintext). This is observability only — no blocking logic.

---

## 3. Non-Goals

- **Social login** (Google, GitHub) — not in current architecture; separate initiative
- **Multi-factor authentication** — future enhancement; Firebase supports it but not in scope
- **Server-side password validation** — Firebase owns password auth; re-implementing it adds complexity without safety benefit
- **IP-based login blocking** — Firebase already throttles; adding redundant backend blocking creates false-positive risk
- **Slug enumeration protection** — slug availability check is intentionally public (listeners need it); not a meaningful attack surface

---

## 4. User Stories

| As a... | I want to... | So that... |
|---|---|---|
| Platform operator | Know all active host accounts have verified email addresses | I trust the org list in the admin dashboard |
| Platform operator | Prevent rapid slug squatting | The namespace stays clean |
| New user | Know my password is secure before I submit | I'm not locked out later from a weak credential |
| Security auditor | See failed login patterns in logs | I can detect attacks before they succeed |

---

## 5. Scope

### In Scope

**Frontend (`frontend/pages/signup.tsx`)**
- `minLength` 6 → 8 on both password fields
- Password strength indicator (weak/ok/strong inline)
- `auth/too-many-requests` → call `/api/auth/login-failed` in `login.tsx`

**Frontend (`frontend/lib/authContext.tsx`)**
- On login: if `auth/wrong-password` or `auth/invalid-credential`, call `POST /api/auth/login-failed`

**Frontend (`frontend/pages/host/c/[churchSlug].tsx` — host console)**
- On mount: if `user.emailVerified === false`, render verification prompt instead of console
- Verification prompt: show email address, "Resend verification email" button, "Check again" button
- On "Resend": call `user.sendEmailVerification()`
- On "Check again": call `user.reload()` then re-check `emailVerified`

**Backend (`backend/app/routes/auth.py`)**
- Rate limit on `POST /api/auth/bootstrap-owner-org`: 3 per UID per hour, HTTP 429 on breach
- `POST /api/auth/login-failed`: accept `{ email_hash: str }`, log `security_event`, no auth required, rate-limited by IP (max 30/min)

### Out of Scope
- Email verification for invite-accept flow (invite is already intent-verified)
- Blocking unverified users from the listener view (listeners don't need accounts)
- Blocking unverified users from the API entirely (too disruptive; host console gate is sufficient)

---

## 6. Affected Files

| File | Change |
|---|---|
| `frontend/pages/signup.tsx` | Password minLength 8 + strength indicator |
| `frontend/lib/authContext.tsx` | Call `/api/auth/login-failed` on repeated auth failure |
| `frontend/pages/host/c/[churchSlug].tsx` | Email verification gate on host console mount |
| `backend/app/routes/auth.py` | Rate limit `bootstrap-owner-org` + add `login-failed` endpoint |

---

## 7. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Email verification blocks legitimate users who missed the email | Medium | "Resend" button always visible; verification only gates host console, not account creation |
| Org creation rate limit blocks legitimate multi-org setup | Low | 3/hour is generous for real use; super_admin bypass possible via env flag |
| Strength indicator is client-side only — weak passwords still accepted if user bypasses | Low | Acceptable given Firebase's own 6-char floor; further enforcement requires server-side Firebase Admin SDK password policy (Firebase supports this via Identity Platform) |
| `login-failed` endpoint spammed by bots (DDoS vector) | Low | Rate limited at 30 req/min per IP; no state written to Firestore; only in-memory logging |

---

## 8. Acceptance Criteria

- [ ] Password field on `/signup` requires min 8 characters
- [ ] Password strength indicator appears below password field (weak/ok/strong)
- [ ] Entering the host console (`/host/c/{slug}/broadcast`) when `emailVerified === false` shows verification prompt, not the console
- [ ] "Resend verification email" button works; "Check again" button reloads auth state and re-checks
- [ ] Invite-join flow (`?next=/join?...`) is not affected by verification gate
- [ ] `POST /api/auth/bootstrap-owner-org` returns HTTP 429 after 3 attempts per UID within 1 hour
- [ ] `POST /api/auth/login-failed` logs `security_event` and returns 200; rate-limited at 30/min/IP
- [ ] All existing auth tests (login, signup, invite) still pass
- [ ] `npm run lint` passes with no errors

---

## 9. Next Steps

```
/pdca design auth-hardening   ← Detailed component design
/pdca do auth-hardening        ← Implementation
/pdca analyze auth-hardening   ← Gap analysis
```
