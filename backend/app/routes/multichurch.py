from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import validators
from app.auth.firebase_auth import AuthenticatedUser, get_current_user_required
from app.auth.guards import require_org_role
from app.socket_manager import manager
from app.services.multichurch_store import multichurch_store
from app.services.script_store import script_store

router = APIRouter()


class StartServiceRequest(BaseModel):
    source: str = Field(default="ko", min_length=2, max_length=20, pattern=validators.LANG_CODE)
    target: str = Field(default="en", min_length=2, max_length=20, pattern=validators.LANG_CODE)
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
    serviceKey: str = Field(..., min_length=2, max_length=80, pattern=validators.SERVICE_KEY)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    timezone: str | None = Field(default=None, min_length=2, max_length=80)
    source: str = Field(default="ko", min_length=2, max_length=20, pattern=validators.LANG_CODE)
    target: str = Field(default="en", min_length=2, max_length=20, pattern=validators.LANG_CODE)


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
def resolve_service(
    slug: str = Path(pattern=validators.CHURCH_SLUG),
    service_key: str = Path(pattern=validators.SERVICE_KEY),
):
    data = multichurch_store.resolve_service(slug=slug, service_key=service_key)
    if not data:
        raise HTTPException(status_code=404, detail="service_not_found")
    return data


@router.get("/c/{slug}/s/{service_key}/segments/latest")
def latest_service_segments(
    slug: str = Path(pattern=validators.CHURCH_SLUG),
    service_key: str = Path(pattern=validators.SERVICE_KEY),
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=10, ge=1, le=50),
):
    data = multichurch_store.resolve_service(slug=slug, service_key=service_key)
    if not data:
        raise HTTPException(status_code=404, detail="service_not_found")
    room_id = (data.get("activeRoomId") or "").strip()
    if not room_id or data.get("roomStatus") != "live":
        return {
            "orgId": data.get("orgId"),
            "slug": data.get("slug") or slug,
            "serviceKey": data.get("serviceKey") or service_key,
            "activeRoomId": None,
            "roomStatus": data.get("roomStatus") or "waiting",
            "segments": [],
        }
    segments = multichurch_store.latest_room_segments(
        str(data["orgId"]),
        room_id,
        after_seq=after_seq,
        limit=limit,
    )
    return {
        "orgId": data["orgId"],
        "slug": data.get("slug") or slug,
        "serviceKey": data.get("serviceKey") or service_key,
        "activeRoomId": room_id,
        "roomStatus": data.get("roomStatus") or "live",
        "segments": segments,
    }


@router.get("/c/{slug}/services")
def list_services(slug: str = Path(pattern=validators.CHURCH_SLUG)):
    data = multichurch_store.list_services(slug=slug)
    if not data:
        raise HTTPException(status_code=404, detail="org_not_found")
    return data


@router.get("/org/{org_id}/services")
def list_services_by_org(
    *,
    org_id: str = Path(pattern=validators.ORG_ID),
    current_user: AuthenticatedUser = Depends(get_current_user_required),
):
    """List services for an org by orgId (requires auth). Used by sermon-prep UI."""
    require_org_role(org_id=org_id, user=current_user, roles={"owner", "admin", "host", "viewer"}, store=multichurch_store)
    services = multichurch_store.list_services_by_org_id(org_id)
    return {"orgId": org_id, "services": services or []}


@router.post("/org/{org_id}/services")
def create_service(
    *,
    org_id: str = Path(pattern=validators.ORG_ID),
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
    *,
    org_id: str = Path(pattern=validators.ORG_ID),
    service_key: str = Path(pattern=validators.SERVICE_KEY),
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
    *,
    org_id: str = Path(pattern=validators.ORG_ID),
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
    *,
    org_id: str = Path(pattern=validators.ORG_ID),
    service_key: str = Path(pattern=validators.SERVICE_KEY),
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
    *,
    slug: str = Path(pattern=validators.CHURCH_SLUG),
    service_key: str = Path(pattern=validators.SERVICE_KEY),
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
    *,
    org_id: str = Path(pattern=validators.ORG_ID),
    room_id: str = Path(pattern=validators.ORG_ID),
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

    # Free in-memory sermon script for this room
    script_store.clear(room_id=room_id)
    manager.forget_room(org_id, room_id)

    return result


@router.get("/org/{org_id}/room/{room_id}/segments/export")
def export_room_segments(
    *,
    org_id: str = Path(pattern=validators.ORG_ID),
    room_id: str = Path(pattern=validators.ORG_ID),
    current_user: AuthenticatedUser = Depends(get_current_user_required),
):
    try:
        segments = multichurch_store.export_room_segments(
            org_id,
            room_id,
            requested_by_uid=current_user.uid,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="host_auth_failed")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="not_supported_in_dev_mode")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["seq", "timestamp", "mode", "korean_text", "english_text"])
    for seg in segments:
        writer.writerow([
            seg.get("seq", ""),
            seg.get("timestamp", ""),
            seg.get("mode", ""),
            seg.get("koreanText", ""),
            seg.get("englishText", ""),
        ])

    filename = f"{org_id}_{room_id}_translation.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
