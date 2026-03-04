from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.firebase_auth import AuthenticatedUser, get_current_user_required
from app.services.multichurch_store import multichurch_store

router = APIRouter()


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
    expiresHours: int = Field(default=24 * 7, ge=1, le=24 * 30)


@router.get("/auth/me")
def auth_me(user: AuthenticatedUser = Depends(get_current_user_required)):
    memberships = multichurch_store.list_memberships(user.uid)
    current_org_id = multichurch_store.get_current_org_id(user.uid)
    return {
        "user": {
            "uid": user.uid,
            "email": user.email,
            "displayName": user.displayName,
        },
        "currentOrgId": current_org_id,
        "memberships": memberships,
    }


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
        "hostToken": result.get("hostToken"),
        "memberships": memberships,
    }


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


@router.post("/auth/org/{org_id}/invites")
def auth_create_invite(
    org_id: str,
    payload: CreateInviteRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
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
    try:
        return multichurch_store.redeem_invite(
            code=code,
            uid=user.uid,
            email=user.email,
            display_name=user.displayName,
        )
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
