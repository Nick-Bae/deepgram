#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency for env convenience
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False

from google.cloud import firestore

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.billing.config import BILLING_CONFIG
from app.billing.models import default_billing_state, plan_spec


DEFAULT_DATABASE_ID = "worship-translation"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _yyyymm(dt: datetime) -> str:
    return f"{dt.year:04d}{dt.month:02d}"


def _load_backend_env() -> Path:
    backend_dir = BACKEND_DIR
    load_dotenv(backend_dir / ".env")

    creds = _clean(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
    if not creds:
        fallback_creds = backend_dir / "google-translation.json"
        if fallback_creds.exists():
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(fallback_creds)
    return backend_dir


def _project_id_from_env() -> str | None:
    project_id = (
        _clean(os.getenv("GOOGLE_CLOUD_PROJECT"))
        or _clean(os.getenv("GCP_PROJECT"))
        or _clean(os.getenv("FIRESTORE_PROJECT"))
        or _clean(os.getenv("FIREBASE_PROJECT_ID"))
    )
    return project_id or None


def _resolve_credentials_path(raw_path: str) -> str | None:
    explicit = _clean(raw_path)
    if explicit:
        resolved = Path(explicit).expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Credentials file not found: {resolved}")
        return str(resolved)

    env_credentials = _clean(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
    if env_credentials:
        resolved = Path(env_credentials).expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"GOOGLE_APPLICATION_CREDENTIALS does not exist: {resolved}")
        return str(resolved)
    return None


def _split_org_ids(raw_values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for token in str(raw or "").split(","):
            org_id = _clean(token)
            if not org_id or org_id in seen:
                continue
            seen.add(org_id)
            out.append(org_id)
    return out


def _confirm(args: argparse.Namespace, org_ids: list[str]) -> bool:
    if args.dry_run or args.yes:
        return True
    if not sys.stdin.isatty():
        print("Refusing non-interactive billing reset without --yes.", file=sys.stderr)
        return False

    scope = "all organizations" if args.all_orgs else ", ".join(org_ids)
    extras: list[str] = []
    if args.delete_usage:
        extras.append("delete usage subcollections")
    if args.delete_billing_events:
        extras.append("delete top-level billingEvents")
    suffix = f" and {' + '.join(extras)}" if extras else ""
    print(f"About to reset billing for {scope}{suffix}.")
    confirm = input("Type YES to continue: ").strip()
    return confirm == "YES"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reset embedded organization billing state in Firestore to a clean trial/default state.",
    )
    parser.add_argument("--org-id", action="append", default=[], help="Target organizations/{orgId} doc id (repeatable or comma-separated).")
    parser.add_argument("--all-orgs", action="store_true", help="Reset every document in the organizations collection.")
    parser.add_argument("--plan", default="trial", help="Plan key to reset orgs to. Default: trial")
    parser.add_argument("--project-id", default="", help="Firestore project id override (optional).")
    parser.add_argument("--database", default=DEFAULT_DATABASE_ID, help=f"Firestore database id. Default: {DEFAULT_DATABASE_ID}")
    parser.add_argument("--credentials", default="", help="Service account JSON path override (optional).")
    parser.add_argument("--delete-billing-events", action="store_true", help="Also delete the top-level billingEvents collection.")
    parser.add_argument("--delete-usage", action="store_true", help="Also recursively delete organizations/{orgId}/usage subcollections.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes only.")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    return parser


def _target_org_ids(db: firestore.Client, args: argparse.Namespace) -> list[str]:
    explicit_ids = _split_org_ids(list(args.org_id or []))
    if args.all_orgs:
        org_ids = [snap.id for snap in db.collection("organizations").stream()]
        if explicit_ids:
            seen = set(org_ids)
            for org_id in explicit_ids:
                if org_id not in seen:
                    org_ids.append(org_id)
        return org_ids
    return explicit_ids


def _build_reset_payload(*, now: datetime, plan_key: str) -> dict[str, Any]:
    resolved_plan = plan_spec(plan_key)
    billing = default_billing_state(
        now=now,
        plan_key=resolved_plan.key,
        trial_days=int(BILLING_CONFIG.trial_days),
        trial_minutes=int(BILLING_CONFIG.trial_minutes),
    )
    normalized_status = str(billing.get("status") or "").strip().lower()
    return {
        "plan": resolved_plan.key,
        "status": "trial" if normalized_status == "trialing" else (normalized_status or "active"),
        "billing": billing,
        "currentMonthMinutes": 0,
        "currentMonthKey": _yyyymm(now),
        "hardCapReached": False,
        "softCapReached": False,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }


def main() -> int:
    _load_backend_env()
    parser = _build_parser()
    args = parser.parse_args()

    if not args.all_orgs and not args.org_id:
        parser.error("Provide --org-id or --all-orgs.")

    plan_token = _clean(args.plan).lower() or "trial"
    resolved_plan = plan_spec(plan_token)

    project_id = _clean(args.project_id) or _project_id_from_env()
    if not project_id:
        parser.error("Project ID not found. Set GOOGLE_CLOUD_PROJECT/GCP_PROJECT or pass --project-id.")

    try:
        credentials_path = _resolve_credentials_path(args.credentials)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if credentials_path:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path

    db = firestore.Client(project=project_id, database=_clean(args.database) or DEFAULT_DATABASE_ID)
    org_ids = _target_org_ids(db, args)
    if not org_ids and not args.delete_billing_events:
        print("No target organizations found.", file=sys.stderr)
        return 1

    if not _confirm(args, org_ids):
        print("Aborted.")
        return 2

    run_started_at = _utcnow().isoformat()
    now = _utcnow()
    payload = _build_reset_payload(now=now, plan_key=resolved_plan.key)

    any_error = False
    for org_id in org_ids:
        event: dict[str, Any] = {
            "timestamp": _utcnow().isoformat(),
            "runStartedAt": run_started_at,
            "projectId": project_id,
            "database": _clean(args.database) or DEFAULT_DATABASE_ID,
            "orgId": org_id,
            "dryRun": bool(args.dry_run),
            "resetPlanKey": resolved_plan.key,
            "deleteUsage": bool(args.delete_usage),
            "success": True,
        }
        try:
            org_ref = db.collection("organizations").document(org_id)
            org_snap = org_ref.get()
            if not org_snap.exists:
                raise ValueError("org_not_found")

            org = org_snap.to_dict() or {}
            event["beforePlan"] = _clean(org.get("plan")) or None
            event["beforeStatus"] = _clean(org.get("status")) or None
            billing = org.get("billing") if isinstance(org.get("billing"), dict) else {}
            event["beforeBillingStatus"] = _clean(billing.get("status")) or None
            event["beforeStripeCustomerId"] = _clean(billing.get("stripeCustomerId")) or None
            event["beforeStripeSubscriptionId"] = _clean(billing.get("stripeSubscriptionId")) or None

            if not args.dry_run:
                org_ref.set(payload, merge=True)
                if args.delete_usage:
                    deleted_usage_docs = db.recursive_delete(org_ref.collection("usage"))
                    event["deletedUsageDocs"] = int(deleted_usage_docs)

            event["afterPlan"] = payload["plan"]
            event["afterStatus"] = payload["status"]
            event["afterBillingStatus"] = payload["billing"]["status"]
            print(json.dumps(event, ensure_ascii=True, sort_keys=True))
        except Exception as exc:
            any_error = True
            event["success"] = False
            event["error"] = str(exc)
            print(json.dumps(event, ensure_ascii=True, sort_keys=True), file=sys.stderr)

    if args.delete_billing_events:
        event = {
            "timestamp": _utcnow().isoformat(),
            "runStartedAt": run_started_at,
            "projectId": project_id,
            "database": _clean(args.database) or DEFAULT_DATABASE_ID,
            "collection": "billingEvents",
            "dryRun": bool(args.dry_run),
            "success": True,
        }
        try:
            if not args.dry_run:
                deleted_docs = db.recursive_delete(db.collection("billingEvents"))
                event["deletedDocs"] = int(deleted_docs)
            print(json.dumps(event, ensure_ascii=True, sort_keys=True))
        except Exception as exc:
            any_error = True
            event["success"] = False
            event["error"] = str(exc)
            print(json.dumps(event, ensure_ascii=True, sort_keys=True), file=sys.stderr)

    return 1 if any_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
