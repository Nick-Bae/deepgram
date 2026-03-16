from __future__ import annotations

from collections import deque
import logging
import os
import re
import time
from threading import Lock
from typing import Deque, Dict, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.firebase_auth import (
    AuthenticatedUser,
    generate_password_reset_link_value,
    get_current_user_required,
)
from app.services.multichurch_store import DEFAULT_INVITE_EXPIRY_HOURS, multichurch_store

router = APIRouter()
logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return max(min_value, min(max_value, parsed))


_INVITE_RATE_WINDOW_SECONDS = _env_int("INVITE_RATE_WINDOW_SECONDS", 60, min_value=5, max_value=3600)
_INVITE_RATE_CREATE_MAX = _env_int("INVITE_RATE_CREATE_MAX_PER_WINDOW", 20, min_value=1, max_value=5000)
_INVITE_RATE_PREVIEW_MAX = _env_int("INVITE_RATE_PREVIEW_MAX_PER_WINDOW", 120, min_value=1, max_value=5000)
_INVITE_RATE_REDEEM_MAX = _env_int("INVITE_RATE_REDEEM_MAX_PER_WINDOW", 40, min_value=1, max_value=5000)
_invite_rate_hits: Dict[Tuple[str, str], Deque[float]] = {}
_invite_rate_lock = Lock()
_PASSWORD_RESET_RATE_WINDOW_SECONDS = _env_int("PASSWORD_RESET_RATE_WINDOW_SECONDS", 3600, min_value=60, max_value=86400)
_PASSWORD_RESET_RATE_MAX_PER_IP = _env_int("PASSWORD_RESET_RATE_MAX_PER_IP", 5, min_value=1, max_value=500)
_PASSWORD_RESET_RATE_MAX_PER_EMAIL = _env_int("PASSWORD_RESET_RATE_MAX_PER_EMAIL", 3, min_value=1, max_value=100)
_password_reset_ip_hits: Dict[str, Deque[float]] = {}
_password_reset_email_hits: Dict[str, Deque[float]] = {}
_password_reset_rate_lock = Lock()
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", re.I)


def _enforce_invite_rate_limit(uid: str, *, action: str, max_hits: int) -> None:
    clean_uid = (uid or "").strip()
    if not clean_uid:
        return
    now = time.monotonic()
    cutoff = now - _INVITE_RATE_WINDOW_SECONDS
    key = (clean_uid, action)
    with _invite_rate_lock:
        bucket = _invite_rate_hits.get(key)
        if bucket is None:
            bucket = deque()
            _invite_rate_hits[key] = bucket
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= max_hits:
            raise HTTPException(status_code=429, detail="invite_rate_limited")
        bucket.append(now)
        if len(_invite_rate_hits) > 4096:
            stale = [k for k, values in _invite_rate_hits.items() if not values or values[-1] <= cutoff]
            for stale_key in stale:
                _invite_rate_hits.pop(stale_key, None)


class BootstrapOwnerRequest(BaseModel):
    churchName: str = Field(..., min_length=2, max_length=120)
    churchSlug: str = Field(..., min_length=2, max_length=80)
    timezone: str = Field(default="America/Chicago", min_length=2, max_length=80)
    source: str = Field(default="ko", min_length=2, max_length=20)
    target: str = Field(default="en", min_length=2, max_length=20)


class SetCurrentOrgRequest(BaseModel):
    orgId: str = Field(..., min_length=2, max_length=120)


class CreateInviteRequest(BaseModel):
    role: str = Field(default="host", min_length=4, max_length=20)
    expiresHours: int = Field(default=DEFAULT_INVITE_EXPIRY_HOURS, ge=1, le=24 * 30)


class SetOrgBillingLimitsRequest(BaseModel):
    enabled: bool = Field(...)


class PasswordResetRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)


def _sanitize_membership_payload(rows: list[dict]) -> list[dict]:
    sanitized: list[dict] = []
    for row in rows:
        item = dict(row or {})
        item.pop("hostToken", None)
        item.pop("host_token", None)
        sanitized.append(item)
    return sanitized


def _sanitize_auth_payload(payload: dict) -> dict:
    item = dict(payload or {})
    item.pop("hostToken", None)
    item.pop("host_token", None)
    return item


def _client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    real_ip = (request.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return str(host or "unknown")


def _enforce_password_reset_rate_limit(ip: str, email: str) -> None:
    now = time.monotonic()
    cutoff = now - _PASSWORD_RESET_RATE_WINDOW_SECONDS
    clean_ip = (ip or "").strip() or "unknown"
    clean_email = (email or "").strip().lower()
    with _password_reset_rate_lock:
        ip_bucket = _password_reset_ip_hits.get(clean_ip)
        if ip_bucket is None:
            ip_bucket = deque()
            _password_reset_ip_hits[clean_ip] = ip_bucket
        while ip_bucket and ip_bucket[0] <= cutoff:
            ip_bucket.popleft()
        if len(ip_bucket) >= _PASSWORD_RESET_RATE_MAX_PER_IP:
            raise HTTPException(status_code=429, detail="password_reset_rate_limited")
        ip_bucket.append(now)

        email_bucket = _password_reset_email_hits.get(clean_email)
        if email_bucket is None:
            email_bucket = deque()
            _password_reset_email_hits[clean_email] = email_bucket
        while email_bucket and email_bucket[0] <= cutoff:
            email_bucket.popleft()
        if len(email_bucket) >= _PASSWORD_RESET_RATE_MAX_PER_EMAIL:
            raise HTTPException(status_code=429, detail="password_reset_rate_limited")
        email_bucket.append(now)


def _password_reset_continue_url() -> str | None:
    value = (
        (os.getenv("PASSWORD_RESET_CONTINUE_URL") or "").strip()
        or (os.getenv("AUTH_PASSWORD_RESET_CONTINUE_URL") or "").strip()
    )
    return value or None


def _password_reset_from_email() -> str:
    value = (
        (os.getenv("PASSWORD_RESET_FROM_EMAIL") or "").strip()
        or (os.getenv("AUTH_FROM_EMAIL") or "").strip()
        or (os.getenv("CONTACT_FROM_EMAIL") or "").strip()
    )
    if not value:
        raise HTTPException(status_code=503, detail="password_reset_email_not_configured")
    return value


def _password_reset_fallback_from_email() -> str:
    value = (os.getenv("RESEND_FALLBACK_FROM_EMAIL") or "").strip()
    return value or "Worship <onboarding@resend.dev>"


def _password_reset_brand_name() -> str:
    value = (
        (os.getenv("PASSWORD_RESET_BRAND_NAME") or "").strip()
        or (os.getenv("APP_BRAND_NAME") or "").strip()
    )
    return value or "Worship Translation"


def _is_resend_domain_not_verified(status_code: int, body: str) -> bool:
    if int(status_code) != 403:
        return False
    payload = str(body or "").lower()
    return "domain is not verified" in payload or "verify your domain" in payload


def _send_password_reset_email_via_resend(*, email: str, reset_link: str) -> None:
    resend_api_key = (os.getenv("RESEND_API_KEY") or "").strip()
    if not resend_api_key:
        raise HTTPException(status_code=503, detail="password_reset_email_not_configured")
    from_email = _password_reset_from_email()
    fallback_from_email = _password_reset_fallback_from_email()
    brand_name = _password_reset_brand_name()
    subject = f"Reset your {brand_name} password"
    html = f"""
    <div style="font-family: Arial, sans-serif; line-height: 1.6; color: #0f172a;">
      <h2 style="margin-bottom: 12px;">Reset your password</h2>
      <p>A password reset was requested for your {brand_name} account.</p>
      <p style="margin: 22px 0;">
        <a href="{reset_link}" style="display: inline-block; padding: 12px 18px; border-radius: 999px; background: #4f73aa; color: #ffffff; text-decoration: none; font-weight: 700;">
          Reset Password
        </a>
      </p>
      <p>If the button does not work, request another password reset from the login page.</p>
      <p>If you did not request this, you can ignore this email.</p>
      <p>Thanks,<br />{brand_name}</p>
    </div>
    """.strip()
    text = "\n".join(
        [
            f"Reset your {brand_name} password",
            "",
            f"A password reset was requested for your {brand_name} account.",
            "",
            "Use the reset button in the email to continue.",
            "If you did not request this, you can ignore this email.",
            "",
            f"Thanks,",
            brand_name,
        ]
    )

    def _post_email(active_from_email: str) -> httpx.Response:
        return client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": active_from_email,
                "to": [email],
                "subject": subject,
                "html": html,
                "text": text,
                "tags": [
                    {"name": "source", "value": "worship-password-reset"},
                ],
            },
        )

    try:
        with httpx.Client(timeout=10) as client:
            response = _post_email(from_email)
            if (
                _is_resend_domain_not_verified(response.status_code, response.text)
                and fallback_from_email
                and fallback_from_email != from_email
            ):
                logger.warning(
                    "Primary password reset sender rejected by Resend; retrying with fallback sender %s for %s",
                    fallback_from_email,
                    email,
                )
                response = _post_email(fallback_from_email)
    except Exception as exc:
        logger.exception("Password reset email delivery failed for %s", email)
        raise HTTPException(status_code=503, detail="password_reset_email_delivery_failed") from exc

    if response.status_code >= 400:
        logger.error(
            "Password reset email delivery failed for %s: status=%s body=%s",
            email,
            response.status_code,
            response.text[:1000],
        )
        raise HTTPException(status_code=503, detail="password_reset_email_delivery_failed")


@router.get("/auth/me")
def auth_me(user: AuthenticatedUser = Depends(get_current_user_required)):
    memberships = multichurch_store.list_memberships(user.uid)
    current_org_id = multichurch_store.get_current_org_id(user.uid)
    is_master = bool(multichurch_store.is_master_user(user.uid))
    return {
        "user": {
            "uid": user.uid,
            "email": user.email,
            "displayName": user.displayName,
            "isMaster": is_master,
        },
        "currentOrgId": current_org_id,
        "memberships": _sanitize_membership_payload(memberships),
    }


@router.post("/auth/password-reset")
def auth_password_reset(payload: PasswordResetRequest, request: Request):
    clean_email = (payload.email or "").strip().lower()
    if not _EMAIL_PATTERN.match(clean_email):
        raise HTTPException(status_code=400, detail="invalid_email")

    _enforce_password_reset_rate_limit(_client_ip(request), clean_email)
    reset_link = generate_password_reset_link_value(
        clean_email,
        continue_url=_password_reset_continue_url(),
    )
    if reset_link:
        _send_password_reset_email_via_resend(email=clean_email, reset_link=reset_link)
    return {"ok": True}


@router.post("/auth/bootstrap-owner")
def auth_bootstrap_owner(
    payload: BootstrapOwnerRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    try:
        result = multichurch_store.bootstrap_owner_org(
            owner_uid=user.uid,
            owner_email=user.email,
            owner_display_name=user.displayName,
            church_name=payload.churchName,
            church_slug=payload.churchSlug,
            timezone=payload.timezone,
            source=payload.source,
            target=payload.target,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail in {"invalid_uid", "invalid_name", "invalid_slug"}:
            raise HTTPException(status_code=400, detail=detail) from exc
        if detail == "slug_taken":
            raise HTTPException(status_code=409, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "bootstrap_failed") from exc

    memberships = multichurch_store.list_memberships(user.uid)
    org_payload = {
        "orgId": result.get("orgId"),
        "slug": result.get("slug"),
        "name": result.get("name"),
        "role": result.get("role") or "owner",
    }
    return {
        "created": bool(result.get("created", True)),
        "org": org_payload,
        "services": result.get("services") or [],
        "memberships": _sanitize_membership_payload(memberships),
    }


@router.get("/auth/slug-availability")
def auth_slug_availability(
    slug: str = Query(..., min_length=1, max_length=120),
):
    try:
        return multichurch_store.check_org_slug_availability(slug=slug, max_suggestions=3)
    except ValueError as exc:
        detail = str(exc)
        if detail == "invalid_slug":
            raise HTTPException(status_code=400, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "slug_check_failed") from exc


@router.post("/auth/current-org")
def auth_set_current_org(
    payload: SetCurrentOrgRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    try:
        org_id = multichurch_store.set_current_org(user.uid, payload.orgId)
    except ValueError as exc:
        detail = str(exc)
        if detail in {"invalid_uid"}:
            raise HTTPException(status_code=400, detail=detail) from exc
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "set_current_org_failed") from exc
    except PermissionError as exc:
        detail = str(exc) or "org_access_denied"
        raise HTTPException(status_code=403, detail=detail) from exc
    return {"ok": True, "currentOrgId": org_id}


@router.get("/auth/org/{org_id}/billing-limits")
def auth_get_org_billing_limits(
    org_id: str,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    try:
        return multichurch_store.get_org_billing_limits(
            org_id=org_id,
            requested_by_uid=user.uid,
            requested_by_email=user.email,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail == "invalid_uid":
            raise HTTPException(status_code=400, detail=detail) from exc
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "billing_limits_fetch_failed") from exc
    except PermissionError as exc:
        detail = str(exc) or "forbidden"
        raise HTTPException(status_code=403, detail=detail) from exc


@router.post("/auth/org/{org_id}/billing-limits")
def auth_set_org_billing_limits(
    org_id: str,
    payload: SetOrgBillingLimitsRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    try:
        return multichurch_store.set_org_billing_limits(
            org_id=org_id,
            requested_by_uid=user.uid,
            enabled=payload.enabled,
            requested_by_email=user.email,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail == "invalid_uid":
            raise HTTPException(status_code=400, detail=detail) from exc
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "billing_limits_update_failed") from exc
    except PermissionError as exc:
        detail = str(exc) or "forbidden"
        raise HTTPException(status_code=403, detail=detail) from exc


@router.post("/auth/org/{org_id}/invites")
def auth_create_invite(
    org_id: str,
    payload: CreateInviteRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    _enforce_invite_rate_limit(user.uid, action="create", max_hits=_INVITE_RATE_CREATE_MAX)
    try:
        invite = multichurch_store.create_invite(
            org_id=org_id,
            created_by_uid=user.uid,
            role=payload.role,
            expires_in_hours=payload.expiresHours,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail in {"invalid_role", "invalid_uid"}:
            raise HTTPException(status_code=400, detail=detail) from exc
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        if detail == "invite_active_limit_reached":
            raise HTTPException(status_code=429, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "invite_create_failed") from exc
    except PermissionError as exc:
        detail = str(exc) or "forbidden"
        raise HTTPException(status_code=403, detail=detail) from exc
    return invite


@router.get("/auth/org/{org_id}/invites")
def auth_list_invites(
    org_id: str,
    status: str | None = Query(default="active"),
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    try:
        return {
            "orgId": org_id,
            "invites": multichurch_store.list_invites(
                org_id=org_id,
                requested_by_uid=user.uid,
                status=status,
            ),
        }
    except ValueError as exc:
        detail = str(exc)
        if detail in {"invalid_uid", "invalid_status"}:
            raise HTTPException(status_code=400, detail=detail) from exc
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "invite_list_failed") from exc
    except PermissionError as exc:
        detail = str(exc) or "forbidden"
        raise HTTPException(status_code=403, detail=detail) from exc


@router.get("/auth/invites/{code}/preview")
def auth_preview_invite(
    code: str,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    _enforce_invite_rate_limit(user.uid, action="preview", max_hits=_INVITE_RATE_PREVIEW_MAX)
    try:
        return multichurch_store.preview_invite(code=code, uid=user.uid)
    except ValueError as exc:
        detail = str(exc)
        if detail == "invite_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        if detail == "invite_expired":
            raise HTTPException(status_code=410, detail=detail) from exc
        if detail == "invite_invalid":
            raise HTTPException(status_code=409, detail=detail) from exc
        if detail in {"invalid_uid", "invalid_role"}:
            raise HTTPException(status_code=400, detail=detail) from exc
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "invite_preview_failed") from exc


@router.post("/auth/invites/{code}/redeem")
def auth_redeem_invite(
    code: str,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    _enforce_invite_rate_limit(user.uid, action="redeem", max_hits=_INVITE_RATE_REDEEM_MAX)
    try:
        redeemed = multichurch_store.redeem_invite(
            code=code,
            uid=user.uid,
            email=user.email,
            display_name=user.displayName,
        )
        return _sanitize_auth_payload(redeemed)
    except ValueError as exc:
        detail = str(exc)
        if detail == "invite_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        if detail == "invite_expired":
            raise HTTPException(status_code=410, detail=detail) from exc
        if detail == "invite_invalid":
            raise HTTPException(status_code=409, detail=detail) from exc
        if detail in {"invalid_uid", "invalid_role"}:
            raise HTTPException(status_code=400, detail=detail) from exc
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "invite_redeem_failed") from exc


@router.post("/auth/org/{org_id}/invites/{invite_id}/revoke")
def auth_revoke_invite(
    org_id: str,
    invite_id: str,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    try:
        return multichurch_store.revoke_invite(
            org_id=org_id,
            invite_id=invite_id,
            revoked_by_uid=user.uid,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail in {"invalid_uid", "invalid_status"}:
            raise HTTPException(status_code=400, detail=detail) from exc
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        if detail == "invite_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        if detail == "invite_expired":
            raise HTTPException(status_code=410, detail=detail) from exc
        if detail == "invite_invalid":
            raise HTTPException(status_code=409, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "invite_revoke_failed") from exc
    except PermissionError as exc:
        detail = str(exc) or "forbidden"
        raise HTTPException(status_code=403, detail=detail) from exc
