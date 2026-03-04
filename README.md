# real-time-translation

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

## Host auth (signup/login)

- Signup page: `/signup`
- Login page: `/login`
- Church onboarding page: `/onboarding/create-church`
- Host page now requires login and will redirect to `/login` if unauthenticated.
- Listeners remain public (`/c/{churchSlug}/s/{serviceKey}`), no listener login required.

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
