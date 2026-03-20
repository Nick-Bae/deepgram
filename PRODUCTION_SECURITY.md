# Production Security Runbook

This document defines the production security baseline for this repository.

## 1) Permission Model

- Global platform privileges:
  - Use Firebase custom claim `super_admin=true`.
  - Intended for a very small set of trusted operators.
- Church/org privileges:
  - Use app membership roles in Firestore: `admin`, `host`.
  - Store at `organizations/{orgId}/members/{uid}.role`.

Do not use org membership roles as a replacement for global `super_admin`.

## 2) Production Environment Baseline

Set and enforce these rules in production:

- `DISABLE_BILLING_LIMITS` must be unset (or `0`).
- `MASTER_USER_UIDS` / `MASTER_USER_UID` must be empty.
- `CORS_ALLOW_ORIGINS` must be explicit origin allowlist only (no `*`).
- `GOOGLE_APPLICATION_CREDENTIALS` must point to a secure runtime secret path.

Never commit credential files to git.

## 3) God Mode (Super Admin) Operations

Use only the admin script:

- `backend/scripts/set_super_admin.py`

Examples:

```bash
cd backend

# Grant
python scripts/set_super_admin.py \
  --grant \
  --uid <firebase_uid> \
  --actor <operator_email> \
  --reason "billing admin duty" \
  --ticket SEC-123 \
  --audit-log ./logs/god-mode-audit.jsonl \
  --yes

# Status
python scripts/set_super_admin.py --status --uid <firebase_uid>

# Revoke
python scripts/set_super_admin.py \
  --revoke \
  --uid <firebase_uid> \
  --actor <operator_email> \
  --ticket SEC-123 \
  --audit-log ./logs/god-mode-audit.jsonl \
  --yes
```

Operational notes:

- Keep `--revoke-refresh-tokens` enabled (default).
- Claim changes are not instant for old ID tokens.
- User must sign out/in (or refresh token) after grant/revoke.

## 4) Backend Authorization Rules

For all sensitive endpoints:

- Require auth (`401` if missing/invalid).
- Enforce org role and/or `super_admin` on backend, never frontend-only.
- Use centralized guards for org role checks.
- Default deny on ambiguity.

For billing-sensitive actions:

- Require `super_admin`.
- Log actor UID, org ID, action, and timestamp.

## 5) Credential and Secret Handling

- Service account keys:
  - Keep out of repo.
  - Restrict file permissions.
  - Rotate on personnel change or exposure.
- Store secrets in a secret manager or runtime secure mount.
- Avoid passing secrets via chat, screenshots, or client-side code.

## 6) Monitoring and Audit

Minimum audit events:

- super admin grant/revoke/status checks
- billing limit changes
- invite creation/revoke
- role changes in org membership

Audit fields:

- timestamp
- actor
- target uid/org
- action
- reason/ticket
- success/failure

## 7) Incident Response (Key Leak or Unauthorized Access)

If a credential leak is suspected:

1. Revoke and rotate exposed key immediately.
2. Remove key from all hosts and CI/CD variables.
3. Audit last 30 days of super admin and billing actions.
4. Revoke suspicious custom claims and refresh tokens.
5. Document incident with timeline and remediation.

## 8) Release Checklist

Before each production deploy:

1. Verify production env flags (`DISABLE_BILLING_LIMITS`, `MASTER_USER_UIDS`, CORS).
2. Verify only approved UIDs have `super_admin`.
3. Verify secrets are loaded from secure source.
4. Run backend tests and auth-path smoke checks.
5. Confirm audit logging destination is writable and retained.
