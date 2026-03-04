#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import random
import string
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib import error, request


def load_env_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def http_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
) -> Tuple[int, Dict[str, Any] | None, str]:
    body: Optional[bytes] = None
    req_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    req = request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            data = None
            if text:
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    data = None
            return int(resp.status), data, text
    except error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        data = None
        if text:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
        return int(exc.code), data, text


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def firebase_auth(api_key: str, email: str, password: str) -> Dict[str, Any]:
    sign_up_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={api_key}"
    sign_in_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}"
    payload = {"email": email, "password": password, "returnSecureToken": True}

    status, data, _ = http_json("POST", sign_up_url, payload=payload)
    if status == 200 and data:
        return data

    err_msg = ""
    if isinstance(data, dict):
        err_msg = str((data.get("error") or {}).get("message") or "")

    if status in {400, 403} and "EMAIL_EXISTS" in err_msg:
        status2, data2, raw2 = http_json("POST", sign_in_url, payload=payload)
        assert_true(status2 == 200 and isinstance(data2, dict), f"Firebase signIn failed: status={status2}, body={raw2}")
        return data2

    raise RuntimeError(f"Firebase signUp failed: status={status}, error={err_msg}")


def firebase_delete_user(api_key: str, id_token: str) -> None:
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:delete?key={api_key}"
    status, data, raw = http_json("POST", url, payload={"idToken": id_token})
    if status not in {200, 400, 403}:
        raise RuntimeError(f"Firebase delete failed: status={status}, body={raw}, parsed={data}")


def auth_headers(id_token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {id_token}"}


def create_org(api_base: str, owner_token: str, *, church_name: str, slug: str) -> Dict[str, Any]:
    status, data, raw = http_json(
        "POST",
        f"{api_base}/api/auth/bootstrap-owner",
        headers=auth_headers(owner_token),
        payload={
            "churchName": church_name,
            "churchSlug": slug,
            "timezone": "America/Chicago",
            "source": "ko",
            "target": "en",
        },
    )
    assert_true(status == 200 and isinstance(data, dict), f"bootstrap-owner failed: status={status}, body={raw}")
    org = data.get("org") or {}
    assert_true(isinstance(org, dict) and org.get("orgId"), f"bootstrap-owner missing org: {data}")
    return data


def get_me(api_base: str, token: str) -> Dict[str, Any]:
    status, data, raw = http_json("GET", f"{api_base}/api/auth/me", headers=auth_headers(token))
    assert_true(status == 200 and isinstance(data, dict), f"auth/me failed: status={status}, body={raw}")
    return data


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    front_env = load_env_file(root / "frontend" / ".env.local")

    api_base = (
        os.getenv("SMOKE_API_BASE_URL")
        or front_env.get("NEXT_PUBLIC_API_BASE_URL")
        or ""
    ).strip().rstrip("/")
    firebase_api_key = (
        os.getenv("SMOKE_FIREBASE_API_KEY")
        or front_env.get("NEXT_PUBLIC_FIREBASE_API_KEY")
        or ""
    ).strip()

    assert_true(bool(api_base), "NEXT_PUBLIC_API_BASE_URL is missing in frontend/.env.local")
    assert_true(bool(firebase_api_key), "NEXT_PUBLIC_FIREBASE_API_KEY is missing in frontend/.env.local")

    run_suffix = f"{int(time.time())}{random.randint(100,999)}"
    password = "SmokeTest!234"

    owner1_email = f"smoke-owner1-{run_suffix}@example.com"
    owner2_email = f"smoke-owner2-{run_suffix}@example.com"
    member_email = f"smoke-member-{run_suffix}@example.com"

    owner1_id_token = ""
    owner2_id_token = ""
    member_id_token = ""

    print(f"[smoke] api_base={api_base}")

    try:
        owner1_auth = firebase_auth(firebase_api_key, owner1_email, password)
        owner2_auth = firebase_auth(firebase_api_key, owner2_email, password)
        member_auth = firebase_auth(firebase_api_key, member_email, password)

        owner1_id_token = str(owner1_auth.get("idToken") or "")
        owner2_id_token = str(owner2_auth.get("idToken") or "")
        member_id_token = str(member_auth.get("idToken") or "")

        assert_true(owner1_id_token and owner2_id_token and member_id_token, "Firebase idToken missing")

        org1_slug = f"smoke-{run_suffix}".lower()
        org2_slug = f"smoke-b-{run_suffix}".lower()

        org1_boot = create_org(api_base, owner1_id_token, church_name=f"Smoke Church {run_suffix}", slug=org1_slug)
        org2_boot = create_org(api_base, owner2_id_token, church_name=f"Smoke Church B {run_suffix}", slug=org2_slug)

        org1 = org1_boot["org"]
        org2 = org2_boot["org"]
        org1_id = str(org1["orgId"])
        org2_id = str(org2["orgId"])

        me_owner1 = get_me(api_base, owner1_id_token)
        assert_true(str(me_owner1.get("currentOrgId") or "") == org1_id, "Owner1 currentOrgId mismatch after bootstrap")

        # Org1 invite flow
        status, created1, raw = http_json(
            "POST",
            f"{api_base}/api/auth/org/{org1_id}/invites",
            headers=auth_headers(owner1_id_token),
            payload={"role": "viewer", "expiresHours": 24},
        )
        assert_true(status == 200 and isinstance(created1, dict), f"create invite org1 failed: status={status}, body={raw}")
        invite1_code = str(created1.get("code") or "")
        assert_true(bool(invite1_code), "org1 invite code missing")

        status, preview1, raw = http_json(
            "GET",
            f"{api_base}/api/auth/invites/{invite1_code}/preview",
            headers=auth_headers(member_id_token),
        )
        assert_true(status == 200 and isinstance(preview1, dict), f"preview org1 invite failed: status={status}, body={raw}")
        assert_true(str(preview1.get("orgId") or "") == org1_id, "preview org1 orgId mismatch")
        assert_true(bool(preview1.get("alreadyMember") is False), "preview org1 expected alreadyMember=false")

        status, redeemed1, raw = http_json(
            "POST",
            f"{api_base}/api/auth/invites/{invite1_code}/redeem",
            headers=auth_headers(member_id_token),
        )
        assert_true(status == 200 and isinstance(redeemed1, dict), f"redeem org1 invite failed: status={status}, body={raw}")
        assert_true(str(redeemed1.get("orgId") or "") == org1_id, "redeem org1 orgId mismatch")

        status, data_second, raw_second = http_json(
            "POST",
            f"{api_base}/api/auth/invites/{invite1_code}/redeem",
            headers=auth_headers(member_id_token),
        )
        detail_second = ""
        if isinstance(data_second, dict):
            detail_second = str(data_second.get("detail") or "")
        assert_true(status == 409 and detail_second == "invite_invalid", f"second redeem expected 409/invite_invalid, got status={status}, detail={detail_second}, body={raw_second}")

        # Org2 invite and org-switch
        status, created2, raw = http_json(
            "POST",
            f"{api_base}/api/auth/org/{org2_id}/invites",
            headers=auth_headers(owner2_id_token),
            payload={"role": "viewer", "expiresHours": 24},
        )
        assert_true(status == 200 and isinstance(created2, dict), f"create invite org2 failed: status={status}, body={raw}")
        invite2_code = str(created2.get("code") or "")
        assert_true(bool(invite2_code), "org2 invite code missing")

        status, _, raw = http_json(
            "POST",
            f"{api_base}/api/auth/invites/{invite2_code}/redeem",
            headers=auth_headers(member_id_token),
        )
        assert_true(status == 200, f"redeem org2 invite failed: status={status}, body={raw}")

        me_member = get_me(api_base, member_id_token)
        assert_true(str(me_member.get("currentOrgId") or "") == org2_id, "member currentOrgId should switch to org2 after redeem")

        status, switched, raw = http_json(
            "POST",
            f"{api_base}/api/auth/current-org",
            headers=auth_headers(member_id_token),
            payload={"orgId": org1_id},
        )
        assert_true(status == 200 and isinstance(switched, dict), f"set current-org failed: status={status}, body={raw}")
        assert_true(str(switched.get("currentOrgId") or "") == org1_id, "set current-org response mismatch")

        me_member2 = get_me(api_base, member_id_token)
        assert_true(str(me_member2.get("currentOrgId") or "") == org1_id, "member currentOrgId did not persist after switch")

        # Listener endpoints should be public (no auth header)
        status, listener_services, raw = http_json("GET", f"{api_base}/api/c/{org1_slug}/services")
        assert_true(status == 200 and isinstance(listener_services, dict), f"public listener services failed: status={status}, body={raw}")
        assert_true(str(listener_services.get("orgId") or "") == org1_id, "public listener orgId mismatch")

        print("[smoke] PASS")
        print(json.dumps(
            {
                "org1": {"orgId": org1_id, "slug": org1_slug},
                "org2": {"orgId": org2_id, "slug": org2_slug},
                "memberCurrentOrgId": me_member2.get("currentOrgId"),
                "listenerPublicServices": True,
            },
            ensure_ascii=False,
        ))
        return 0

    finally:
        # best-effort cleanup of auth users; Firestore docs intentionally left for audit/debug
        if owner1_id_token:
            try:
                firebase_delete_user(firebase_api_key, owner1_id_token)
            except Exception:
                pass
        if owner2_id_token:
            try:
                firebase_delete_user(firebase_api_key, owner2_id_token)
            except Exception:
                pass
        if member_id_token:
            try:
                firebase_delete_user(firebase_api_key, member_id_token)
            except Exception:
                pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[smoke] FAIL: {exc}")
        raise
