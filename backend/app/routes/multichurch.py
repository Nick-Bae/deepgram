from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.auth.firebase_auth import AuthenticatedUser, get_current_user_optional, get_current_user_required
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


def _host_claims(
    *,
    host_uid_payload: str | None,
    host_uid_header: str | None,
    host_token_payload: str | None,
    host_token_payload_snake: str | None,
    host_token_payload_legacy: str | None,
    host_token_header: str | None,
    host_api_token_header: str | None,
) -> tuple[str | None, str | None]:
    host_uid = host_uid_payload or host_uid_header
    host_token = host_token_payload or host_token_payload_snake or host_token_payload_legacy or host_token_header or host_api_token_header
    return host_uid, host_token


def _start_service_for_org(
    *,
    org_id: str,
    service_key: str,
    payload: StartServiceRequest,
    x_host_token: str | None,
    x_host_api_token: str | None,
    x_host_uid: str | None,
    current_user_uid: str | None,
):
    host_uid, host_token = _host_claims(
        host_uid_payload=current_user_uid or payload.hostUid,
        host_uid_header=x_host_uid,
        host_token_payload=payload.hostToken,
        host_token_payload_snake=payload.host_token,
        host_token_payload_legacy=payload.token,
        host_token_header=x_host_token,
        host_api_token_header=x_host_api_token,
    )
    allowed = multichurch_store.authorize_host(org_id, host_uid=host_uid, host_token=host_token)
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
        if detail == "hard_cap_reached":
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
        raise HTTPException(status_code=403, detail=str(exc) or "forbidden") from exc


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


@router.post("/org/{org_id}/service/{service_key}/start")
def start_service(
    org_id: str,
    service_key: str,
    payload: StartServiceRequest,
    x_host_token: str | None = Header(default=None),
    x_host_api_token: str | None = Header(default=None),
    x_host_uid: str | None = Header(default=None),
    current_user: AuthenticatedUser | None = Depends(get_current_user_optional),
):
    return _start_service_for_org(
        org_id=org_id,
        service_key=service_key,
        payload=payload,
        x_host_token=x_host_token,
        x_host_api_token=x_host_api_token,
        x_host_uid=x_host_uid,
        current_user_uid=current_user.uid if current_user else None,
    )


@router.post("/c/{slug}/service/{service_key}/start")
def start_service_by_slug(
    slug: str,
    service_key: str,
    payload: StartServiceRequest,
    x_host_token: str | None = Header(default=None),
    x_host_api_token: str | None = Header(default=None),
    x_host_uid: str | None = Header(default=None),
    current_user: AuthenticatedUser | None = Depends(get_current_user_optional),
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
        x_host_token=x_host_token,
        x_host_api_token=x_host_api_token,
        x_host_uid=x_host_uid,
        current_user_uid=current_user.uid if current_user else None,
    )


@router.post("/org/{org_id}/room/{room_id}/end")
def end_room(
    org_id: str,
    room_id: str,
    payload: EndRoomRequest,
    x_host_token: str | None = Header(default=None),
    x_host_api_token: str | None = Header(default=None),
    x_host_uid: str | None = Header(default=None),
    current_user: AuthenticatedUser | None = Depends(get_current_user_optional),
):
    allowed = multichurch_store.authorize_host(
        org_id,
        host_uid=((current_user.uid if current_user else None) or payload.hostUid or x_host_uid),
        host_token=(payload.hostToken or payload.host_token or payload.token or x_host_token or x_host_api_token),
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
