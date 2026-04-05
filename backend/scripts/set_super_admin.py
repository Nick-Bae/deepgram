#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency for env convenience
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False

try:
    import firebase_admin  # type: ignore
    from firebase_admin import auth as firebase_auth  # type: ignore
    from firebase_admin import credentials as firebase_credentials  # type: ignore
except Exception:  # pragma: no cover - optional dependency in some local envs
    firebase_admin = None
    firebase_auth = None
    firebase_credentials = None


CLAIM_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _load_backend_env() -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    load_dotenv(backend_dir / ".env")


def _project_id_from_env() -> str | None:
    project_id = (
        _clean(os.getenv("GOOGLE_CLOUD_PROJECT"))
        or _clean(os.getenv("GCP_PROJECT"))
        or _clean(os.getenv("FIREBASE_PROJECT_ID"))
    )
    return project_id or None


def _split_values(raw_values: list[str], *, lowercase: bool = False) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for token in re.split(r"[,\s]+", _clean(raw)):
            value = _clean(token)
            if lowercase:
                value = value.lower()
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
    return out


def _split_uids(raw_values: list[str]) -> list[str]:
    return _split_values(raw_values)


def _split_emails(raw_values: list[str]) -> list[str]:
    return _split_values(raw_values, lowercase=True)


def _load_value_file(path: str, *, value_name: str) -> list[str]:
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"{value_name} file not found: {file_path}")
    values: list[str] = []
    for raw in file_path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        values.append(line)
    return values


def _load_uid_file(path: str) -> list[str]:
    return _split_uids(_load_value_file(path, value_name="UID"))


def _load_email_file(path: str) -> list[str]:
    return _split_emails(_load_value_file(path, value_name="Email"))


def _resolve_actor(raw_actor: str) -> str:
    actor = _clean(raw_actor)
    if actor:
        return actor
    return _clean(os.getenv("USER") or os.getenv("LOGNAME") or "")


def _claim_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _init_firebase(*, project_id: str | None, credentials_path: str | None) -> None:
    if firebase_admin is None or firebase_auth is None or firebase_credentials is None:
        raise RuntimeError("firebase_admin package is not available in this environment")
    if firebase_admin._apps:  # type: ignore[attr-defined]
        return

    credential = None
    explicit_credentials = _clean(credentials_path)
    if explicit_credentials:
        resolved = Path(explicit_credentials).expanduser().resolve()
        if not resolved.exists():
            raise RuntimeError(f"Credentials file not found: {resolved}")
        credential = firebase_credentials.Certificate(str(resolved))
    else:
        env_credentials_name = ""
        env_credentials = _clean(os.getenv("FIREBASE_ADMIN_CREDENTIALS"))
        if env_credentials:
            env_credentials_name = "FIREBASE_ADMIN_CREDENTIALS"
        else:
            env_credentials = _clean(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
            if env_credentials:
                env_credentials_name = "GOOGLE_APPLICATION_CREDENTIALS"
        if env_credentials:
            resolved = Path(env_credentials).expanduser().resolve()
            if not resolved.exists():
                raise RuntimeError(f"{env_credentials_name or 'credentials'} does not exist: {resolved}")
            credential = firebase_credentials.Certificate(str(resolved))
        else:
            credential = firebase_credentials.ApplicationDefault()

    options: dict[str, Any] = {}
    if project_id:
        options["projectId"] = project_id
    firebase_admin.initialize_app(credential, options or None)


def _append_audit_log(path: str, event: dict[str, Any]) -> None:
    file_path = Path(path).expanduser().resolve()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")


def _confirm_mutation(action: str, uids: list[str], claim_key: str) -> bool:
    if not sys.stdin.isatty():
        print("Refusing non-interactive claim mutation without --yes.", file=sys.stderr)
        return False
    print(f"About to {action} claim '{claim_key}' for {len(uids)} UID(s): {', '.join(uids)}")
    confirm = input("Type YES to continue: ").strip()
    return confirm == "YES"


def _lookup_uid_by_email(email: str) -> str:
    clean_email = _clean(email).lower()
    if not clean_email:
        raise ValueError("email is required")
    if firebase_auth is None:
        raise RuntimeError("firebase_admin auth client is not available")
    user = firebase_auth.get_user_by_email(clean_email)  # type: ignore[union-attr]
    uid = _clean(getattr(user, "uid", ""))
    if not uid:
        raise RuntimeError(f"Email lookup returned empty UID for {clean_email}")
    return uid


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Grant/revoke Firebase custom claim for God Mode (super admin) with JSON audit output.",
    )
    action_group = parser.add_mutually_exclusive_group(required=True)
    action_group.add_argument("--grant", action="store_true", help="Grant claim (enable God Mode).")
    action_group.add_argument("--revoke", action="store_true", help="Revoke claim (disable God Mode).")
    action_group.add_argument("--status", action="store_true", help="Read-only: show current claim state.")

    parser.add_argument("--uid", action="append", default=[], help="Target UID (repeatable, also supports comma-separated values).")
    parser.add_argument("--uid-file", default="", help="Path to file with UIDs (newline or comma separated; # comments allowed).")
    parser.add_argument("--email", action="append", default=[], help="Target email (repeatable, also supports comma-separated values).")
    parser.add_argument("--email-file", default="", help="Path to file with emails (newline or comma separated; # comments allowed).")
    parser.add_argument("--claim-key", default="super_admin", help="Custom claim key to manage. Default: super_admin")
    parser.add_argument("--project-id", default="", help="Firebase project id override (optional).")
    parser.add_argument("--credentials", default="", help="Service account JSON file path override (optional).")
    parser.add_argument("--actor", default="", help="Operator identifier for audit logs (email or username).")
    parser.add_argument("--reason", default="", help="Short reason for change (for audit).")
    parser.add_argument("--ticket", default="", help="Ticket/change id for traceability (for audit).")
    parser.add_argument("--audit-log", default="", help="Optional JSONL path to append audit events.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; do not change claims.")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation for grant/revoke.")
    parser.add_argument(
        "--revoke-refresh-tokens",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Revoke refresh tokens after claim mutation (default: true).",
    )
    return parser


def main() -> int:
    _load_backend_env()
    parser = _build_parser()
    args = parser.parse_args()

    action = "status"
    if args.grant:
        action = "grant"
    elif args.revoke:
        action = "revoke"

    claim_key = _clean(args.claim_key)
    if not CLAIM_KEY_PATTERN.fullmatch(claim_key):
        parser.error("Invalid --claim-key. Use letters/numbers/underscore, starting with a letter.")

    uid_values: list[str] = list(args.uid or [])
    uid_file = _clean(args.uid_file)
    if uid_file:
        uid_values.extend(_load_uid_file(uid_file))
    email_values: list[str] = list(args.email or [])
    email_file = _clean(args.email_file)
    if email_file:
        email_values.extend(_load_email_file(email_file))

    requested_uids = _split_uids(uid_values)
    requested_emails = _split_emails(email_values)
    if not requested_uids and not requested_emails:
        parser.error("Provide at least one target via --uid/--uid-file or --email/--email-file.")

    actor = _resolve_actor(args.actor)
    if action in {"grant", "revoke"} and not actor:
        parser.error("--actor is required for claim mutations.")

    project_id = _clean(args.project_id) or _project_id_from_env()
    try:
        _init_firebase(project_id=project_id, credentials_path=_clean(args.credentials) or None)
    except Exception as exc:
        print(f"Firebase init failed: {exc}", file=sys.stderr)
        return 1

    run_started_at = datetime.now(timezone.utc).isoformat()
    script_name = Path(__file__).name
    any_error = False
    uids = list(requested_uids)
    seen_uids = set(requested_uids)

    for email in requested_emails:
        now = datetime.now(timezone.utc).isoformat()
        try:
            resolved_uid = _lookup_uid_by_email(email)
            if resolved_uid not in seen_uids:
                seen_uids.add(resolved_uid)
                uids.append(resolved_uid)
        except Exception as exc:
            any_error = True
            event = {
                "timestamp": now,
                "runStartedAt": run_started_at,
                "script": script_name,
                "action": action,
                "success": False,
                "dryRun": bool(args.dry_run),
                "projectId": project_id,
                "actor": actor or None,
                "targetEmail": email,
                "claimKey": claim_key,
                "error": str(exc),
                "reason": _clean(args.reason) or None,
                "ticket": _clean(args.ticket) or None,
            }
            print(json.dumps(event, ensure_ascii=True, sort_keys=True), file=sys.stderr)
            if _clean(args.audit_log):
                _append_audit_log(_clean(args.audit_log), event)

    if not uids:
        return 1

    if action in {"grant", "revoke"} and not args.dry_run and not args.yes:
        if not _confirm_mutation(action, uids, claim_key):
            print("Aborted.")
            return 2

    for uid in uids:
        now = datetime.now(timezone.utc).isoformat()
        try:
            user = firebase_auth.get_user(uid)  # type: ignore[union-attr]
            before_claims = dict(user.custom_claims or {})
            after_claims = dict(before_claims)
            if action == "grant":
                after_claims[claim_key] = True
            elif action == "revoke":
                after_claims.pop(claim_key, None)

            changed = action in {"grant", "revoke"} and (before_claims != after_claims)
            if changed and not args.dry_run:
                firebase_auth.set_custom_user_claims(uid, after_claims or None)  # type: ignore[union-attr]
                if args.revoke_refresh_tokens:
                    firebase_auth.revoke_refresh_tokens(uid)  # type: ignore[union-attr]

            event = {
                "timestamp": now,
                "runStartedAt": run_started_at,
                "script": script_name,
                "action": action,
                "success": True,
                "dryRun": bool(args.dry_run),
                "changed": bool(changed),
                "projectId": project_id,
                "actor": actor or None,
                "targetUid": uid,
                "targetEmail": _clean(user.email) or None,
                "claimKey": claim_key,
                "beforeEnabled": _claim_bool(before_claims.get(claim_key)),
                "afterEnabled": _claim_bool(after_claims.get(claim_key)),
                "beforeClaims": before_claims,
                "afterClaims": after_claims,
                "reason": _clean(args.reason) or None,
                "ticket": _clean(args.ticket) or None,
                "revokeRefreshTokens": bool(args.revoke_refresh_tokens if action in {"grant", "revoke"} else False),
            }
            print(json.dumps(event, ensure_ascii=True, sort_keys=True))
            if _clean(args.audit_log):
                _append_audit_log(_clean(args.audit_log), event)
        except Exception as exc:
            any_error = True
            event = {
                "timestamp": now,
                "runStartedAt": run_started_at,
                "script": script_name,
                "action": action,
                "success": False,
                "dryRun": bool(args.dry_run),
                "projectId": project_id,
                "actor": actor or None,
                "targetUid": uid,
                "claimKey": claim_key,
                "error": str(exc),
                "reason": _clean(args.reason) or None,
                "ticket": _clean(args.ticket) or None,
            }
            print(json.dumps(event, ensure_ascii=True, sort_keys=True), file=sys.stderr)
            if _clean(args.audit_log):
                _append_audit_log(_clean(args.audit_log), event)

    return 1 if any_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
