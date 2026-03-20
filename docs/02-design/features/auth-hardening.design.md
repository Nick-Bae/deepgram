# Design: Auth Hardening

> Refs: `docs/01-plan/features/auth-hardening.plan.md`
> Phase: Design
> Updated: 2026-03-20

---

## 1. Overview

Four targeted hardening changes across two frontend files and one backend file:

| Gap | Severity | File | Change |
|---|---|---|---|
| 1. Email verification gate | High | `frontend/pages/host/c/[churchSlug].tsx` | Show prompt instead of console when `emailVerified === false` |
| 2. Org creation rate limit | High | `backend/app/routes/auth.py` | Max 3 per UID per hour on `POST /auth/bootstrap-owner` |
| 3. Password strength | Medium | `frontend/pages/signup.tsx` | `minLength` 6→8, strength indicator |
| 4. Failed login logging | Low | `frontend/pages/login.tsx` + `backend/app/routes/auth.py` | Fire-and-forget call to new `POST /auth/login-failed` endpoint |

---

## 2. Architecture

### 2.1 Data Flow

```
[Gap 1] Host Console mount
  └── user.emailVerified === false?
        yes → render <EmailVerificationPrompt> (no API call)
        no  → render normal console

[Gap 2] POST /api/auth/bootstrap-owner (already auth-required)
  └── _enforce_bootstrap_org_rate_limit(uid)  ← NEW, before store call
        hit → HTTP 429 "bootstrap_org_rate_limited"
        ok  → continue to multichurch_store.bootstrap_owner_org(...)

[Gap 3] /signup password field
  └── onChange → passwordStrength(value) → renders inline badge

[Gap 4] /login submit failure
  └── auth/too-many-requests | auth/wrong-password | auth/invalid-credential
        → void reportLoginFailed(email)  ← NEW, fire-and-forget
             → hash email (SHA-256, client-side)
             → POST /api/auth/login-failed { email_hash }
                  → _enforce_login_failed_rate_limit(ip)
                  → security_event("login_failed", email_hash, ip)
                  → HTTP 200
```

### 2.2 No New Firestore Writes

All changes are in-memory (rate limit buckets) or security log (stdout/Cloud Logging). Zero Firestore writes for any of the four gaps.

---

## 3. Backend Design

### 3.1 Gap 2 — Rate Limit on `POST /auth/bootstrap-owner`

**File**: `backend/app/routes/auth.py`

#### New module-level constants and state

```python
_BOOTSTRAP_ORG_RATE_WINDOW_SECONDS = _env_int(
    "BOOTSTRAP_ORG_RATE_WINDOW_SECONDS", 3600, min_value=60, max_value=86400
)
_BOOTSTRAP_ORG_RATE_MAX = _env_int(
    "BOOTSTRAP_ORG_RATE_MAX_PER_WINDOW", 3, min_value=1, max_value=100
)
_bootstrap_org_rate_hits: Dict[str, Deque[float]] = {}
_bootstrap_org_rate_lock = Lock()
```

#### New function `_enforce_bootstrap_org_rate_limit`

Follows the exact same sliding-window deque pattern as `_enforce_invite_rate_limit`:

```python
def _enforce_bootstrap_org_rate_limit(uid: str) -> None:
    clean_uid = (uid or "").strip()
    if not clean_uid:
        return
    now = time.monotonic()
    cutoff = now - _BOOTSTRAP_ORG_RATE_WINDOW_SECONDS
    with _bootstrap_org_rate_lock:
        bucket = _bootstrap_org_rate_hits.get(clean_uid)
        if bucket is None:
            bucket = deque()
            _bootstrap_org_rate_hits[clean_uid] = bucket
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= _BOOTSTRAP_ORG_RATE_MAX:
            security_event(
                "rate_limit_hit",
                detail="bootstrap_org_rate_limited",
                uid=clean_uid,
            )
            raise HTTPException(status_code=429, detail="bootstrap_org_rate_limited")
        bucket.append(now)
        if len(_bootstrap_org_rate_hits) > 4096:
            stale = [k for k, v in _bootstrap_org_rate_hits.items() if not v or v[-1] <= cutoff]
            for sk in stale:
                _bootstrap_org_rate_hits.pop(sk, None)
```

#### Modified endpoint

```python
@router.post("/auth/bootstrap-owner")
def auth_bootstrap_owner(
    payload: BootstrapOwnerRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    _enforce_bootstrap_org_rate_limit(user.uid)   # ← ADD THIS LINE
    try:
        result = multichurch_store.bootstrap_owner_org(...)
```

---

### 3.2 Gap 4 — New `POST /auth/login-failed` Endpoint

**File**: `backend/app/routes/auth.py`

#### New Pydantic model

```python
class LoginFailedRequest(BaseModel):
    email_hash: str = Field(..., min_length=1, max_length=128)
```

#### New rate limit state (IP-keyed, 30/min)

```python
_LOGIN_FAILED_RATE_WINDOW_SECONDS = 60
_LOGIN_FAILED_RATE_MAX_PER_IP = 30
_login_failed_ip_hits: Dict[str, Deque[float]] = {}
_login_failed_rate_lock = Lock()


def _enforce_login_failed_rate_limit(ip: str) -> None:
    clean_ip = (ip or "").strip() or "unknown"
    now = time.monotonic()
    cutoff = now - _LOGIN_FAILED_RATE_WINDOW_SECONDS
    with _login_failed_rate_lock:
        bucket = _login_failed_ip_hits.get(clean_ip)
        if bucket is None:
            bucket = deque()
            _login_failed_ip_hits[clean_ip] = bucket
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= _LOGIN_FAILED_RATE_MAX_PER_IP:
            raise HTTPException(status_code=429, detail="login_failed_rate_limited")
        bucket.append(now)
        if len(_login_failed_ip_hits) > 4096:
            stale = [k for k, v in _login_failed_ip_hits.items() if not v or v[-1] <= cutoff]
            for sk in stale:
                _login_failed_ip_hits.pop(sk, None)
```

#### New endpoint

```python
@router.post("/auth/login-failed")
def auth_login_failed(payload: LoginFailedRequest, request: Request):
    """Observability-only endpoint — no auth required, no Firestore writes."""
    ip = _client_ip(request)
    _enforce_login_failed_rate_limit(ip)
    security_event("login_failed", email_hash=payload.email_hash, ip=ip)
    return {"ok": True}
```

**Design notes**:
- No `Depends(get_current_user_required)` — unauthenticated by design (can't require auth on a failed-login event)
- No Firestore write — only in-memory logging via `security_event`
- `email_hash` is not validated as a real SHA-256 — just a max-length string; the backend doesn't care about the format
- Rate limit does NOT log `security_event` on breach (prevents log flooding by bot scanning the endpoint itself)

---

## 4. Frontend Design

### 4.1 Gap 1 — Email Verification Gate (`[churchSlug].tsx`)

**File**: `frontend/pages/host/c/[churchSlug].tsx`

#### New state variables (add near other state declarations)

```typescript
const [verificationRequired, setVerificationRequired] = useState(false);
const [verificationSending, setVerificationSending] = useState(false);
const [verificationError, setVerificationError] = useState<string | null>(null);
```

#### New `useEffect` — sets `verificationRequired` on auth load

Insert after the existing redirect-to-login effect (current line ~443):

```typescript
useEffect(() => {
  if (authLoading || !user) return;
  setVerificationRequired(!user.emailVerified);
}, [authLoading, user]);
```

#### New handler functions

```typescript
const handleResendVerification = useCallback(async () => {
  if (!user) return;
  setVerificationSending(true);
  setVerificationError(null);
  try {
    await user.sendEmailVerification();
  } catch (err) {
    setVerificationError(err instanceof Error ? err.message : "Failed to send email.");
  } finally {
    setVerificationSending(false);
  }
}, [user]);

const handleCheckVerification = useCallback(async () => {
  if (!user) return;
  setVerificationSending(true);
  setVerificationError(null);
  try {
    await user.reload();
    const client = getFirebaseClient();
    const fresh = client?.auth.currentUser;
    if (fresh?.emailVerified) {
      setVerificationRequired(false);
    } else {
      setVerificationError("Email not yet verified. Check your inbox and click the link.");
    }
  } catch (err) {
    setVerificationError(err instanceof Error ? err.message : "Failed to check.");
  } finally {
    setVerificationSending(false);
  }
}, [user]);
```

Note: `getFirebaseClient` import is already available via `../../../lib/firebaseClient` (used elsewhere in the file).

#### Render gate — at the top of the return JSX

Insert before the main console render (after auth-loading check):

```tsx
if (!authLoading && user && verificationRequired) {
  return (
    <div style={{ /* centered card style */ }}>
      <h2>Verify Your Email</h2>
      <p>
        A verification link was sent to <strong>{user.email}</strong>.
        Please click the link in that email before accessing the host console.
      </p>
      {verificationError && <p style={{ color: "#a33d51" }}>{verificationError}</p>}
      <button onClick={handleResendVerification} disabled={verificationSending}>
        {verificationSending ? "Sending..." : "Resend Verification Email"}
      </button>
      <button onClick={handleCheckVerification} disabled={verificationSending}>
        {verificationSending ? "Checking..." : "Check Again"}
      </button>
    </div>
  );
}
```

**Styling**: Use inline styles matching the existing page palette (dark navy background, white card, same font stack). No new CSS classes. No Tailwind (inline styles are used throughout this file).

**Invite flow**: Not affected. Users completing an invite join (`/join?...`) do not land on `/host/c/{slug}/broadcast` until after invite acceptance — by which time the verification gate would trigger only if email is unverified. This is correct behavior: invite users still need email verification to operate the console.

---

### 4.2 Gap 3 — Password Strength Indicator (`signup.tsx`)

**File**: `frontend/pages/signup.tsx`

#### New helper function (module-level, above component)

```typescript
function passwordStrength(pwd: string): "weak" | "ok" | "strong" | null {
  if (!pwd) return null;
  if (pwd.length < 8) return "weak";
  if (!/[^a-zA-Z]/.test(pwd)) return "weak";   // letters only = weak
  if (pwd.length >= 10) return "strong";
  return "ok";
}
```

Rule: weak = < 8 chars OR no non-letter; ok = 8-9 chars with ≥1 non-letter; strong = ≥10 chars with ≥1 non-letter.

#### `minLength` change

Both password `<input>` fields: `minLength={6}` → `minLength={8}`.

#### Strength indicator JSX

Add below the password `<input>` (before the closing `</label>`), on the **first** password field only (not confirm):

```tsx
{(() => {
  const s = passwordStrength(password);
  if (!s) return null;
  const color = s === "weak" ? "#a33d51" : s === "ok" ? "#b07d29" : "#2f6d4f";
  const label = s === "weak" ? "Weak" : s === "ok" ? "OK" : "Strong";
  return (
    <span style={{ ...studioHelperTextStyle, color }}>
      {label} password
    </span>
  );
})()}
```

#### `mapFirebaseError` update

Update the `auth/weak-password` message to reflect new minimum:

```typescript
if (code === "auth/weak-password") return "Password must be at least 8 characters.";
```

---

### 4.3 Gap 4 — Report Login Failure (`login.tsx`)

**File**: `frontend/pages/login.tsx`

#### New module-level helpers

```typescript
async function hashEmail(email: string): Promise<string> {
  const data = new TextEncoder().encode(email.toLowerCase().trim());
  const buffer = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(buffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function reportLoginFailed(email: string): Promise<void> {
  try {
    const emailHash = await hashEmail(email);
    await fetch(`${API_URL}/api/auth/login-failed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email_hash: emailHash }),
    });
  } catch {
    // fire-and-forget; never surfaces to the user
  }
}
```

`crypto.subtle` is available in all modern browsers and in Next.js (Node 15+ / browser). No import needed.

#### Modified `onSubmit`

```typescript
const onSubmit = async (e: FormEvent) => {
  e.preventDefault();
  setBusy(true);
  setErrorMsg(null);
  try {
    await login(email.trim(), password);
    await redirectWithFreshSession();
  } catch (err) {
    const code = typeof err === "object" && err && "code" in err
      ? String((err as { code?: string }).code || "")
      : "";
    if (
      code === "auth/too-many-requests" ||
      code === "auth/wrong-password" ||
      code === "auth/invalid-credential"
    ) {
      void reportLoginFailed(email.trim());   // ← ADD: fire-and-forget
    }
    if (isInvalidSessionError(err)) {
      try { await logout(); } catch {}
    }
    setErrorMsg(mapFirebaseError(err));
  } finally {
    setBusy(false);
  }
};
```

---

## 5. TypeScript / ESLint Notes

- `crypto.subtle` is typed in the TypeScript `lib.dom.d.ts` — no type assertion needed
- `void reportLoginFailed(...)` suppresses the "floating promise" ESLint rule without await-ing
- `getFirebaseClient()` is already imported in `[churchSlug].tsx` — no new import needed
- `useCallback` is already imported in `[churchSlug].tsx` — no new import needed
- `sendEmailVerification` is available on Firebase `User` — no new Firebase import needed

---

## 6. Affected Files

| File | Change |
|---|---|
| `backend/app/routes/auth.py` | Add `_enforce_bootstrap_org_rate_limit` + call in `auth_bootstrap_owner`; add `_enforce_login_failed_rate_limit` + `LoginFailedRequest` + `auth_login_failed` endpoint |
| `frontend/pages/signup.tsx` | `passwordStrength` helper; `minLength` 6→8 on both password fields; strength indicator JSX; `mapFirebaseError` message update |
| `frontend/pages/login.tsx` | `hashEmail` + `reportLoginFailed` helpers; call `reportLoginFailed` in `onSubmit` catch |
| `frontend/pages/host/c/[churchSlug].tsx` | `verificationRequired/Sending/Error` state; `handleResendVerification` + `handleCheckVerification` callbacks; verification gate `useEffect`; gate render block |

---

## 7. Implementation Order

1. `backend/app/routes/auth.py` — rate limit constants + `_enforce_bootstrap_org_rate_limit` + call in bootstrap endpoint
2. `backend/app/routes/auth.py` — `_enforce_login_failed_rate_limit` + `LoginFailedRequest` + `auth_login_failed` endpoint
3. `frontend/pages/signup.tsx` — `passwordStrength` helper + `minLength` 8 + indicator JSX + error message
4. `frontend/pages/login.tsx` — `hashEmail` + `reportLoginFailed` + call in `onSubmit`
5. `frontend/pages/host/c/[churchSlug].tsx` — verification state + effects + handlers + render gate
6. `npm run lint` — verify clean

---

## 8. Testing Checklist

- [ ] `POST /api/auth/bootstrap-owner` returns HTTP 429 on 4th attempt within the same hour (same UID)
- [ ] `POST /api/auth/login-failed` with `{ email_hash: "abc123" }` returns 200 and logs `security_event`
- [ ] `POST /api/auth/login-failed` called 31 times/min from same IP returns 429
- [ ] Password field on `/signup` requires min 8 chars (HTML validation + Firebase rejects < 6)
- [ ] Strength indicator shows "Weak" for `abc1234` (7 chars), `abcdefgh` (all letters), "OK" for `abc12345`, "Strong" for `abc1234567`
- [ ] Host console (`/host/c/{slug}/broadcast`) when `emailVerified === false` renders verification prompt, not broadcast UI
- [ ] "Resend verification email" button calls `sendEmailVerification()` and shows loading state
- [ ] "Check again" button calls `user.reload()` and dismisses prompt if now verified
- [ ] Verified user (`emailVerified === true`) sees normal console immediately
- [ ] `npm run lint` passes with no errors
