# CLAUDE.md — Real-Time Translation Platform

## Project Overview

Multi-tenant SaaS platform for real-time Korean→English church service translation.
- **Backend**: FastAPI (Python 3.12) + Uvicorn, deployed to Google Cloud Run
- **Frontend**: Next.js 15 + React 19 + TypeScript + Tailwind CSS 4
- **Auth**: Firebase Auth + custom claims (super_admin)
- **Database**: Google Firestore (`worship-translation`, us-central1)
- **STT**: Deepgram nova-3 (WebSocket, Korean audio)
- **Translation**: OpenAI GPT-4o
- **TTS**: Google Cloud Text-to-Speech
- **Billing**: Stripe
- **Email**: Resend

---

## Repository Structure

```
real-time-translation/
├── backend/               # FastAPI app
│   ├── app/
│   │   ├── main.py        # App entry point, WebSocket handlers, CORS, rate limits
│   │   ├── env.py         # ENV config class (models, translation params)
│   │   ├── routes/        # HTTP API endpoints
│   │   ├── services/      # Firestore data layer (multichurch_store.py is the core)
│   │   ├── auth/          # Firebase token verification + RBAC guards
│   │   ├── translator/    # OpenAI translation logic
│   │   ├── chunker/       # Korean text chunking for streaming
│   │   ├── utils/         # Core translation, Korean text utils
│   │   ├── billing/       # Stripe client, plan models
│   │   ├── scripture/     # Bible verse detection
│   │   └── socket_manager.py  # WebSocket connection manager (rooms/broadcast)
│   ├── firestore/         # Firestore security rules + composite indexes
│   ├── scripts/           # Bootstrap + super admin management
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   ├── pages/             # Next.js pages (host console, listener view, admin, auth)
│   ├── components/        # React components (TranslationBox, SermonPrep are large)
│   ├── lib/               # Auth context, Firebase client, WebSocket hooks
│   └── utils/             # URL resolution, stream context
└── .github/workflows/     # GitHub Actions (translation log maintenance)
```

---

## Development Commands

### Full-stack dev (from `frontend/`)
```bash
npm run dev-all      # Auto-detects backend IP, starts backend + frontend concurrently
```

### Backend only (from `frontend/` or project root)
```bash
npm run backend      # cd ../backend && venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Frontend only
```bash
cd frontend
npm run dev          # localhost:3000
npm run dev:lan      # 0.0.0.0:3000 (LAN access)
npm run build        # Production build
npm run lint         # ESLint — must pass before deployment
```

### Backend venv setup (first time)
```bash
cd backend
python -m venv venv
venv/bin/pip install -r requirements.txt
```

### Production Docker
```bash
cd backend
docker build -t translation-backend .
docker run -p 8000:8000 --env-file .env translation-backend
```

---

## Environment Variables

### Backend (`backend/.env`)
```
OPENAI_API_KEY=
OPENAI_TRANSLATION_MODEL=gpt-4o          # Default translation model
OPENAI_SERMON_TRANSLATION_MODEL=         # Falls back to OPENAI_TRANSLATION_MODEL
DEEPGRAM_API_KEY=
DEEPGRAM_MODEL=nova-3
DEEPGRAM_LANGUAGE=ko
FIREBASE_ADMIN_CREDENTIALS=/path/to/serviceaccount.json
GOOGLE_CLOUD_PROJECT=
CORS_ALLOW_ORIGINS=https://yourdomain.com
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
RESEND_API_KEY=
RESEND_FROM_EMAIL=

# Runtime tuning (optional)
PARTIAL_CADENCE_MS=150
SILENCE_COMMIT_MS=450
ROOM_IDLE_TIMEOUT_SEC=900
ROOM_MAX_DURATION_SEC=10800

# DANGER — production must NOT set these
DISABLE_BILLING_LIMITS=      # Must be unset or 0 in prod
MASTER_USER_UIDS=            # Must be empty in prod
DISABLE_WS_TRANSLATION_LIMITS=  # Must be unset or 0 in prod
```

### Frontend (`frontend/.env.local`)
```
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
NEXT_PUBLIC_WS_URL=ws://127.0.0.1:8000/ws/translate
NEXT_PUBLIC_FIREBASE_API_KEY=
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=
NEXT_PUBLIC_FIREBASE_PROJECT_ID=
NEXT_PUBLIC_FIREBASE_APP_ID=
NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=
NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=
NEXT_PUBLIC_BILLING_ADMIN_EMAILS=
NEXT_PUBLIC_GOOGLE_API_KEY=         # Google Cloud API key with Picker API enabled (for sermon Google Doc picker)
```

---

## Architecture Notes

### Authentication Flow
1. Client authenticates via Firebase Auth → gets ID token
2. All backend requests include `Authorization: Bearer <id_token>`
3. Backend verifies token via `app.auth.firebase_auth.get_current_user_required`
4. Returns `AuthenticatedUser(uid, email, displayName, isSuper)`
5. `isSuper` is derived from Firebase custom claim `super_admin=true`
6. Org-level roles (`owner/admin/host/viewer`) stored in Firestore: `organizations/{orgId}/members/{uid}`

### Real-Time Translation Pipeline
```
Host audio → Deepgram STT WebSocket (nova-3, Korean)
           → Korean transcript
           → KoChunker (streaming chunks)
           → sermon_review.lookup.get_reviewed_text() ── hit ──┐
           │      (if service has linkedSermonId; fuzzy ≥0.84) │
           │                                                   │
           → OpenAI GPT-4o (translation)                       │
           → ConnectionManager.broadcast_room() ◄──────────────┘
           → Listener WebSocket connections
           → TranslationBox.tsx (live display)
```

### Sermon Review (editing-sermon)
Pastors can review and correct machine translations offline in Google Sheets, then re-import.
- **Ingestion**: paste / `.txt` / `.docx` / Google Docs URL → translate via GPT-4o → segments stored in `organizations/{orgId}/sermons/{sermonId}` (single-doc, `segments[]` array).
- **Google Docs OAuth**: Firebase Google sign-in requests `documents.readonly` + `drive.readonly` scopes. Frontend stashes the access token in sessionStorage and forwards it per-ingest as `X-Google-Access-Token`. Backend builds a per-request `googleapiclient` Docs client from that token.
- **Email/password users + Google Docs**: `authContext.connectGoogleForDocs()` uses Firebase `linkWithPopup` (or `reauthenticateWithPopup` if Google is already linked) to attach a Google credential to the existing email/password account and capture an access token. `SermonIngestForm` shows "Connect Google to pick a Doc" → opens the popup → picker proceeds. Users keep their email/password login; Google becomes a second linked provider.
- **Google Drive Picker**: `frontend/lib/googlePicker.ts` dynamically loads `apis.google.com/js/api.js` and opens a `DOCUMENTS`-filtered picker. Users browse and select a Doc instead of pasting a URL. Requires `NEXT_PUBLIC_GOOGLE_API_KEY` (Picker API enabled in Google Cloud).
- **Review file**: `.xlsx` export (openpyxl) → edit in Sheets → re-upload. Import is atomic — any row error returns 400 with `IMPORT_VALIDATION_FAILED` and per-row details; nothing is written.
- **Validation**: shifted rows, deleted Segment IDs, or wrong sermon attached are rejected. `updatedAt` precondition prevents concurrent overwrites.
- **Broadcast hook**: when a service has `linkedSermonId`, `main.py:_translate_text_guarded` (broadcast branch only — not previews) consults `get_reviewed_text()` first. Hit → reviewed text (`mode=reviewed`); miss → fall back to GPT-4o. Segments with `status=Skip` are ignored (FR-15) so they fall through to machine translation.
- **Routes**: mounted at `/api/org/{orgId}/sermons/*` — see `backend/app/routes/sermon_review.py`.
- **Frontend**: admin pages at `frontend/pages/admin/sermons/` (list, new, detail).

### Key WebSocket Endpoints
- `/ws/stt_deepgram` — Host audio input (requires auth)
- `/ws/translate` — Listener translation output (public, rate-limited)

### Firestore Data Model
- `organizations/{orgId}` — Org config, billing state, plan, prompt overrides
- `organizations/{orgId}/services/{serviceKey}` — Service definitions
- `organizations/{orgId}/rooms/{roomId}` — Live session state + transcripts
- `organizations/{orgId}/members/{uid}` — Roles: `owner | admin | host | viewer`
- `organizations/{orgId}/invites/{inviteId}` — Invite codes (with expiry)
- `organizations/{orgId}/usage/{periodKey}` — Monthly token/minute usage

**Important**: Firestore is server-write-only from the backend. Clients have read-only access per `firestore.rules`. All writes go through `multichurch_store.py`.

### Billing Plans
| Plan | Minutes/month | Services | Price |
|---|---|---|---|
| Trial | 20 | 2 | Free |
| Starter | — | 5 | $20 |
| Growth | — | 12 | $40 |
| Premium | — | unlimited | $60 |

Hard caps enforced at service start. `hardCapReached` flag in Firestore blocks new rooms.

---

## Code Conventions

### Backend (Python)
- Python 3.12, async where possible (`async def` for routes and WebSocket handlers)
- `from __future__ import annotations` at top of files
- Env vars always read via `os.getenv()` with explicit defaults — never assume presence
- Sensitive config centralized in `backend/app/env.py` (the `ENV` class)
- All route files define a `router = APIRouter()` and are mounted in `main.py`
- Use `Depends(get_current_user_required)` for authenticated endpoints
- Log security-sensitive actions via `security_event()` from `app.security_log`
- CORS origins come from `CORS_ALLOW_ORIGINS` env var — no wildcards in production

### Frontend (TypeScript/Next.js)
- Pages Router (not App Router) — all pages in `frontend/pages/`
- Firebase Auth state managed via `lib/authContext.tsx`
- API base URL from `utils/urls.ts` (resolves `NEXT_PUBLIC_API_BASE_URL`)
- WebSocket connections managed in hooks under `lib/`
- Tailwind CSS 4 with PostCSS — no inline style objects for layout
- `npm run lint` must be clean before committing (ESLint blocks Vercel builds)

---

## Important Files

| File | Purpose |
|---|---|
| `backend/app/main.py` | FastAPI app, all WebSocket logic, rate limiting config |
| `backend/app/env.py` | All configurable env vars with defaults |
| `backend/app/services/multichurch_store.py` | Core Firestore data layer (187KB) — all org/room ops |
| `backend/app/utils/translate.py` | Core translation orchestration (49KB) |
| `backend/app/routes/billing.py` | Stripe lifecycle, webhook handler (32KB) |
| `backend/app/routes/auth.py` | Signup, invite, password reset (25KB) |
| `frontend/components/TranslationBox.tsx` | Main live translation UI (56KB) |
| `frontend/components/SermonPrep.tsx` | Sermon script editor (18KB) |
| `backend/firestore/firestore.rules` | Firestore security rules — review before schema changes |

---

## Security Rules

- Never set `DISABLE_BILLING_LIMITS`, `MASTER_USER_UIDS`, or `DISABLE_WS_TRANSLATION_LIMITS` in production
- Never commit `.env`, `.env.local`, or Firebase service account JSON files
- CORS `allow_origins` must be an explicit list — never `["*"]` in production
- All God Mode (super admin) actions are logged via `security_log.py` with actor/reason
- `super_admin` custom claim is managed only via `backend/scripts/set_super_admin.py`
- Rate limiting is active on `/ws/translate`: 20 concurrent unauthenticated connections max

---

## Deployment

### Backend (Google Cloud Run)
- Production detected via `K_SERVICE` environment variable
- Build: `docker build -t translation-backend ./backend`
- Port: `$PORT` (defaults to 8000)
- Secrets via Cloud Run secret manager (not env file)

### Frontend (Vercel / Cloud Run)
- `npm run build` in `frontend/`
- Must pass `npm run lint` — ESLint errors block Vercel deployments
- Use `HTTPS` and `WSS` URLs in production (never HTTP/WS)

### Firestore
- Deploy rules: `firebase deploy --only firestore:rules`
- Deploy indexes: `firebase deploy --only firestore:indexes`
- Database: `worship-translation` in `us-central1`
