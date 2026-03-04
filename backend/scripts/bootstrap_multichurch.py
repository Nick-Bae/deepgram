#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import secrets
from dataclasses import dataclass
from typing import List
import os
from pathlib import Path

from google.cloud import firestore
from dotenv import load_dotenv


@dataclass
class ServiceSeed:
    key: str
    title: str


def parse_service_arg(raw: str) -> ServiceSeed:
    token = (raw or "").strip()
    if not token:
        raise ValueError("empty service seed")
    if ":" in token:
        key, title = token.split(":", 1)
    else:
        key, title = token, token
    key = key.strip()
    title = title.strip() or key
    if not key:
        raise ValueError(f"invalid service seed: {raw}")
    return ServiceSeed(key=key, title=title)


def load_runtime_env() -> str | None:
    backend_dir = Path(__file__).resolve().parents[1]
    load_dotenv(backend_dir / ".env")

    creds = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if not creds:
        fallback_creds = backend_dir / "google-translation.json"
        if fallback_creds.exists():
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(fallback_creds)

    project = (
        (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
        or (os.getenv("GCP_PROJECT") or "").strip()
        or (os.getenv("FIRESTORE_PROJECT") or "").strip()
    )
    return project or None


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed one organization and recurring services in Firestore.")
    parser.add_argument("--org-id", required=True, help="organizations/{orgId} document id")
    parser.add_argument("--slug", required=True, help="Public slug used by /c/{slug}/...")
    parser.add_argument("--name", required=True, help="Organization display name")
    parser.add_argument("--timezone", default="America/Chicago", help="Default service timezone")
    parser.add_argument("--plan", default="starter")
    parser.add_argument("--status", default="active")
    parser.add_argument("--max-minutes", type=int, default=500)
    parser.add_argument("--max-concurrent-rooms", type=int, default=1)
    parser.add_argument("--source-lang", default="ko")
    parser.add_argument("--target-lang", default="en")
    parser.add_argument(
        "--service",
        action="append",
        default=[],
        help="Recurring service seed in key:title format (repeatable). Example: --service sun-11am:'Sunday 11 AM'",
    )
    parser.add_argument("--owner-uid", default="", help="Optional owner/admin uid for organizations/{orgId}/members/{uid}")
    parser.add_argument("--host-token", default="", help="Optional host token (auto-generated if omitted)")
    args = parser.parse_args()

    services: List[ServiceSeed] = [parse_service_arg(s) for s in args.service]
    if not services:
        services = [
            ServiceSeed("sun-11am", "Sunday 11 AM"),
            ServiceSeed("sun-2pm", "Sunday 2 PM"),
            ServiceSeed("wed-7pm", "Wednesday 7 PM"),
        ]

    project_id = load_runtime_env()
    db = firestore.Client(project=project_id, database="worship-translation")
    org_ref = db.collection("organizations").document(args.org_id.strip())
    existing_snap = org_ref.get()
    existing = existing_snap.to_dict() if existing_snap.exists else {}

    explicit_token = (args.host_token or "").strip()
    existing_token = str(existing.get("hostToken") or "").strip()
    token = explicit_token or existing_token or secrets.token_urlsafe(24)

    now = datetime.now(timezone.utc)
    current_month_key = f"{now.year:04d}{now.month:02d}"

    org_ref.set(
        {
            "slug": args.slug.strip().lower(),
            "name": args.name.strip(),
            "plan": args.plan.strip(),
            "status": args.status.strip().lower(),
            "maxMinutesPerMonth": max(0, int(args.max_minutes)),
            "currentMonthMinutes": 0,
            "currentMonthKey": current_month_key,
            "maxConcurrentRooms": max(1, int(args.max_concurrent_rooms)),
            "hardCapReached": False,
            "hostToken": token,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    for row in services:
        org_ref.collection("services").document(row.key).set(
            {
                "title": row.title,
                "timezone": args.timezone.strip(),
                "rrule": None,
                "defaultLanguagePair": {
                    "source": args.source_lang.strip().lower(),
                    "target": args.target_lang.strip().lower(),
                },
                "activeRoomId": None,
                "lastRoomId": None,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )

    owner_uid = (args.owner_uid or "").strip()
    if owner_uid:
        org_ref.collection("members").document(owner_uid).set(
            {"role": "owner", "createdAt": firestore.SERVER_TIMESTAMP},
            merge=True,
        )

    print("Seed complete")
    if explicit_token:
        print("hostTokenSource=explicit")
    elif existing_token:
        print("hostTokenSource=existing")
    else:
        print("hostTokenSource=generated")
    print(f"orgId={args.org_id.strip()}")
    print(f"slug={args.slug.strip().lower()}")
    print(f"hostToken={token}")
    print("services=" + ",".join([s.key for s in services]))


if __name__ == "__main__":
    main()
