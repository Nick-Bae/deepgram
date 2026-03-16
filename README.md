# real-time-translation

Production security runbook: [PRODUCTION_SECURITY.md](./PRODUCTION_SECURITY.md)

Debug UI traffic:

```js
localStorage.setItem("rt_debug", "1");
location.reload();
```

## Multi-church notes

- Host start/end APIs and host WebSocket publishing are protected by `hostToken`.
- Set a per-org token in Firestore at `organizations/{orgId}.hostToken` (and/or `HOST_API_TOKEN` as global token accepted across orgs).
- Host dashboard page: `/host/c/{churchSlug}` now has a `Host Token` input.
- Listener URL remains stable: `/c/{churchSlug}/s/{serviceKey}`.
- In-memory dev backend seeds `demo` and `arkchurch` slugs by default.

### Backend CORS allowlist

Backend now requires explicit CORS origins (no wildcard `*`):

```bash
# backend/.env
CORS_ALLOW_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
```

- Use full origin values only (`http(s)://host[:port]`).
- Add your deployed frontend origin(s) in production.
- Default dev fallback (if unset): `localhost/127.0.0.1` on ports `3000` and `5173`.

## Host auth (signup/login)

- Signup page: `/signup`
- Login page: `/login`
- Church onboarding page: `/onboarding/create-church`
- Host page now requires login and will redirect to `/login` if unauthenticated.
- Listeners remain public (`/c/{churchSlug}/s/{serviceKey}`), no listener login required.

### Temporary Billing Bypass + Master User

If billing is not configured yet and you need to keep testing:

```bash
# backend env
DISABLE_BILLING_LIMITS=1

# one or more Firebase Auth UIDs (comma-separated)
MASTER_USER_UIDS=your_firebase_uid
# or single value:
# MASTER_USER_UID=your_firebase_uid

# Optional: lock billing controls to specific account(s)
BILLING_ADMIN_EMAILS=namjubae@gmail.com
# BILLING_ADMIN_UIDS=your_firebase_uid
```

- `DISABLE_BILLING_LIMITS=1` skips monthly cap enforcement (`hardCapReached`).
- `MASTER_USER_UIDS` gives global owner-level access across organizations.
- `BILLING_ADMIN_EMAILS`/`BILLING_ADMIN_UIDS` (if set) override default behavior and restrict billing settings access to those identities only.
- Preferred production approach: assign Firebase custom claim(s) such as `super_admin=true` to your admin account instead of long-lived env UID lists.
- Restart backend after changing env vars.

Per-org toggle (recommended for temporary exceptions):

- Super user can open `/host/c/{churchSlug}?section=settings` and use the **Billing Limits** card.
- Backend API for automation: `GET/POST /api/auth/org/{orgId}/billing-limits` with `{ "enabled": true|false }`.

Frontend visibility lock (optional, UX-only):

```bash
# frontend env
NEXT_PUBLIC_BILLING_ADMIN_EMAILS=namjubae@gmail.com
```

### Sermon Prep usage logging + budget cap

Sermon Prep now tracks OpenAI token usage per org and per sermon (`sermon_id`) and can enforce a monthly USD cap.

```bash
# Optional monthly default cap per org (0 = disabled)
SERMON_PREP_DEFAULT_BUDGET_USD=0

# Optional kill switch (1 = disable Sermon Prep budget enforcement globally)
# DISABLE_SERMON_PREP_BUDGETS=1

# OpenAI cost rates used for estimation (defaults shown)
SERMON_PREP_INPUT_COST_PER_MILLION=0.15
SERMON_PREP_OUTPUT_COST_PER_MILLION=0.60
```

- Super user can manage per-org Sermon Prep cap from `/host/c/{churchSlug}?section=settings` in the **Sermon Prep Budget** card.
- API `GET /api/org/{orgId}/sermon/usage`
- API `POST /api/org/{orgId}/sermon/budget` with `{ "budget_usd": number }`
- When cap is reached, Sermon Draft returns HTTP `402` with detail `sermon_prep_budget_reached`.

### Manage God Mode claims (production)

Use the guarded admin script (custom claim + audit JSON output):

```bash
cd backend

# grant
python scripts/set_super_admin.py \
  --grant \
  --uid your_firebase_uid \
  --actor you@example.com \
  --reason "on-call billing admin" \
  --ticket SEC-123 \
  --audit-log ./logs/god-mode-audit.jsonl \
  --yes

# check status
python scripts/set_super_admin.py --status --uid your_firebase_uid

# revoke
python scripts/set_super_admin.py --revoke --uid your_firebase_uid --actor you@example.com --ticket SEC-123 --yes
```

- Script defaults to claim key `super_admin`.
- Set `GOOGLE_APPLICATION_CREDENTIALS` (or pass `--credentials`) for Admin SDK access.
- After grant/revoke, target user should sign out/in to refresh ID token claims.

### Frontend env for Firebase Auth

Add to `frontend/.env.local`:

```bash
NEXT_PUBLIC_FIREBASE_API_KEY=...
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=...
NEXT_PUBLIC_FIREBASE_PROJECT_ID=...
NEXT_PUBLIC_FIREBASE_APP_ID=...
NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=...    # optional
NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=...          # optional
```

### Backend env for branded password reset emails

Add to the backend deployment environment:

```bash
RESEND_API_KEY=...
PASSWORD_RESET_FROM_EMAIL="Worship <support@worshiptranslation.com>"
RESEND_FALLBACK_FROM_EMAIL="Worship <onboarding@resend.dev>"
PASSWORD_RESET_CONTINUE_URL=https://www.worshiptranslation.com/login
PASSWORD_RESET_BRAND_NAME="Worship Translation"
FIREBASE_ADMIN_CREDENTIALS=/abs/path/to/firebase-adminsdk.json
```

If `PASSWORD_RESET_FROM_EMAIL` is not set, the backend falls back to `CONTACT_FROM_EMAIL`.
`FIREBASE_ADMIN_CREDENTIALS` should point to a Firebase Admin SDK service account with Firebase Auth permissions. If it is not set, the backend falls back to `GOOGLE_APPLICATION_CREDENTIALS`.
If your custom sender domain is not verified in Resend yet, `RESEND_FALLBACK_FROM_EMAIL` can temporarily use `onboarding@resend.dev`.

### Backend env for OpenAI translation models

Use one canonical env for live translation:

```bash
OPENAI_TRANSLATION_MODEL=gpt-4o
```

Optional sermon-only override:

```bash
OPENAI_SERMON_TRANSLATION_MODEL=gpt-4o-mini
```

Compatibility note:
- `OPENAI_TRANSLATION_MODEL` is preferred for the live translation path.
- `OPENAI_SERMON_TRANSLATION_MODEL` is preferred for sermon prep/script translation.
- Older env names `TRANSLATION_MODEL`, `OPENAI_MODEL`, and `OPENAI_SERMON_MODEL` still work as fallbacks.

## Firestore bootstrap

Seed one org + default services:

```bash
cd backend
python scripts/bootstrap_multichurch.py \
  --org-id arkchurch \
  --slug arkchurch \
  --name "Ark Church" \
  --service sun-11am:"Sunday 11 AM" \
  --service sun-2pm:"Sunday 2 PM"
```

Outputs include the generated `hostToken`.
Re-running bootstrap now reuses the existing org `hostToken` unless `--host-token` is provided explicitly.

## Firestore deployment artifacts

- Rules: `backend/firestore/firestore.rules`
- Indexes: `backend/firestore/firestore.indexes.json`
