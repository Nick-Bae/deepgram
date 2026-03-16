from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.firebase_auth import AuthenticatedUser, get_current_user_required
from app.services.multichurch_store import multichurch_store

router = APIRouter()


class StartServiceRequest(BaseModel):
    source: str = Field(default="ko", min_length=2, max_length=20)
    target: str = Field(default="en", min_length=2, max_length=20)
    hostUid: str | None = Field(default=None)
    hostToken: str | None = Field(default=None)
    host_token: str | None = Field(default=None)
    token: str | None = Field(default=None)


class EndRoomRequest(BaseModel):
    reason: str = Field(default="host_end", min_length=2, max_length=64)
    transcript: str | None = Field(default=None)
    hostUid: str | None = Field(default=None)
    hostToken: str | None = Field(default=None)
    host_token: str | None = Field(default=None)
    token: str | None = Field(default=None)


class CreateServiceRequest(BaseModel):
    serviceKey: str = Field(..., min_length=2, max_length=80)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    timezone: str | None = Field(default=None, min_length=2, max_length=80)
    source: str = Field(default="ko", min_length=2, max_length=20)
    target: str = Field(default="en", min_length=2, max_length=20)


class UpdateOrgProfileRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)


def _start_service_for_org(
    *,
    org_id: str,
    service_key: str,
    payload: StartServiceRequest,
    current_user: AuthenticatedUser,
):
    host_uid = current_user.uid
    allowed = multichurch_store.authorize_host(org_id, host_uid=host_uid, host_token=None)
    if not allowed:
        raise HTTPException(status_code=403, detail="host_auth_failed")
    try:
        result = multichurch_store.start_service(
            org_id=org_id,
            service_key=service_key,
            host_uid=host_uid,
            source=(payload.source or "ko").strip().lower(),
            target=(payload.target or "en").strip().lower(),
        )
    except PermissionError as exc:
        detail = str(exc)
        if detail in {
            "hard_cap_reached",
            "subscription_required",
            "trial_expired",
            "grace_expired",
            "plan_limit_reached",
        }:
            raise HTTPException(status_code=402, detail=detail) from exc
        if detail == "concurrency_limit_reached":
            raise HTTPException(status_code=429, detail=detail) from exc
        raise HTTPException(status_code=403, detail=detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return result


@router.get("/c/{slug}/s/{service_key}/resolve")
def resolve_service(slug: str, service_key: str):
    data = multichurch_store.resolve_service(slug=slug, service_key=service_key)
    if not data:
        raise HTTPException(status_code=404, detail="service_not_found")
    return data


@router.get("/c/{slug}/services")
def list_services(slug: str):
    data = multichurch_store.list_services(slug=slug)
    if not data:
        raise HTTPException(status_code=404, detail="org_not_found")
    return data


@router.post("/org/{org_id}/services")
def create_service(
    org_id: str,
    payload: CreateServiceRequest,
    current_user: AuthenticatedUser = Depends(get_current_user_required),
):
    try:
        return multichurch_store.create_service(
            org_id=org_id,
            service_key=payload.serviceKey,
            requested_by_uid=current_user.uid,
            title=payload.title,
            timezone=payload.timezone,
            source=payload.source,
            target=payload.target,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail in {"invalid_uid", "invalid_service_key"}:
            raise HTTPException(status_code=400, detail=detail) from exc
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        if detail == "service_exists":
            raise HTTPException(status_code=409, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "service_create_failed") from exc
    except PermissionError as exc:
        detail = str(exc) or "forbidden"
        if detail in {
            "subscription_required",
            "trial_expired",
            "grace_expired",
            "plan_limit_reached",
        }:
            raise HTTPException(status_code=402, detail=detail) from exc
        raise HTTPException(status_code=403, detail=detail) from exc


@router.delete("/org/{org_id}/services/{service_key}")
def delete_service(
    org_id: str,
    service_key: str,
    current_user: AuthenticatedUser = Depends(get_current_user_required),
):
    try:
        return multichurch_store.delete_service(
            org_id=org_id,
            service_key=service_key,
            requested_by_uid=current_user.uid,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail in {"invalid_uid", "invalid_service_key"}:
            raise HTTPException(status_code=400, detail=detail) from exc
        if detail in {"org_not_found", "service_not_found"}:
            raise HTTPException(status_code=404, detail=detail) from exc
        if detail == "service_active":
            raise HTTPException(status_code=409, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "service_delete_failed") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc) or "forbidden") from exc


@router.post("/org/{org_id}/profile")
def update_org_profile(
    org_id: str,
    payload: UpdateOrgProfileRequest,
    current_user: AuthenticatedUser = Depends(get_current_user_required),
):
    try:
        return multichurch_store.update_org_profile(
            org_id=org_id,
            requested_by_uid=current_user.uid,
            name=payload.name,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail in {"invalid_uid", "invalid_name"}:
            raise HTTPException(status_code=400, detail=detail) from exc
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "org_profile_update_failed") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc) or "forbidden") from exc


@router.post("/org/{org_id}/service/{service_key}/start")
def start_service(
    org_id: str,
    service_key: str,
    payload: StartServiceRequest,
    current_user: AuthenticatedUser = Depends(get_current_user_required),
):
    return _start_service_for_org(
        org_id=org_id,
        service_key=service_key,
        payload=payload,
        current_user=current_user,
    )


@router.post("/c/{slug}/service/{service_key}/start")
def start_service_by_slug(
    slug: str,
    service_key: str,
    payload: StartServiceRequest,
    current_user: AuthenticatedUser = Depends(get_current_user_required),
):
    data = multichurch_store.list_services(slug=slug)
    if not data:
        raise HTTPException(status_code=404, detail="org_not_found")
    org_id = str(data.get("orgId") or "").strip()
    if not org_id:
        raise HTTPException(status_code=404, detail="org_not_found")
    return _start_service_for_org(
        org_id=org_id,
        service_key=service_key,
        payload=payload,
        current_user=current_user,
    )


@router.post("/org/{org_id}/room/{room_id}/end")
def end_room(
    org_id: str,
    room_id: str,
    payload: EndRoomRequest,
    current_user: AuthenticatedUser = Depends(get_current_user_required),
):
    allowed = multichurch_store.authorize_host(
        org_id,
        host_uid=current_user.uid,
        host_token=None,
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="host_auth_failed")
    try:
        result = multichurch_store.end_room(
            org_id=org_id,
            room_id=room_id,
            reason=(payload.reason or "host_end").strip().lower(),
            transcript=(payload.transcript or "").strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return result
