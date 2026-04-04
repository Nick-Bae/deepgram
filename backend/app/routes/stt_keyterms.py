from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from app import validators
from app.auth.guards import require_org_role
from app.auth.firebase_auth import AuthenticatedUser, get_current_user_required
from app.services.multichurch_store import multichurch_store

router = APIRouter(tags=["stt"])

STT_KEYTERMS_ROLES = {"owner", "admin"}


class SttKeytermsPayload(BaseModel):
    keyterms: List[str] = Field(default_factory=list, max_length=50)


@router.get("/org/{org_id}/stt-keyterms")
def get_org_stt_keyterms(
    org_id: str = Path(pattern=validators.ORG_ID),
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(
        org_id=org_id,
        user=user,
        roles=STT_KEYTERMS_ROLES,
        store=multichurch_store,
        missing_membership_detail="forbidden",
    )
    try:
        return multichurch_store.get_org_stt_keyterms(
            org_id=org_id, requested_by_uid=user.uid
        )
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=404 if detail == "org_not_found" else 400, detail=detail
        ) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc) or "forbidden") from exc


@router.put("/org/{org_id}/stt-keyterms")
def set_org_stt_keyterms(
    *,
    org_id: str = Path(pattern=validators.ORG_ID),
    payload: SttKeytermsPayload,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(
        org_id=org_id,
        user=user,
        roles=STT_KEYTERMS_ROLES,
        store=multichurch_store,
        missing_membership_detail="forbidden",
    )
    try:
        return multichurch_store.set_org_stt_keyterms(
            org_id=org_id,
            requested_by_uid=user.uid,
            keyterms=payload.keyterms,
        )
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=404 if detail == "org_not_found" else 400, detail=detail
        ) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc) or "forbidden") from exc


class SttReplacementItem(BaseModel):
    find: str = Field(min_length=1, max_length=50)
    replace: str = Field(default="", max_length=50)


class SttReplacementsPayload(BaseModel):
    replacements: List[SttReplacementItem] = Field(default_factory=list, max_length=50)


@router.get("/org/{org_id}/stt-replacements")
def get_org_stt_replacements(
    org_id: str = Path(pattern=validators.ORG_ID),
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(
        org_id=org_id,
        user=user,
        roles=STT_KEYTERMS_ROLES,
        store=multichurch_store,
        missing_membership_detail="forbidden",
    )
    try:
        return multichurch_store.get_org_stt_replacements(
            org_id=org_id, requested_by_uid=user.uid
        )
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=404 if detail == "org_not_found" else 400, detail=detail
        ) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc) or "forbidden") from exc


@router.put("/org/{org_id}/stt-replacements")
def set_org_stt_replacements(
    *,
    org_id: str = Path(pattern=validators.ORG_ID),
    payload: SttReplacementsPayload,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(
        org_id=org_id,
        user=user,
        roles=STT_KEYTERMS_ROLES,
        store=multichurch_store,
        missing_membership_detail="forbidden",
    )
    try:
        return multichurch_store.set_org_stt_replacements(
            org_id=org_id,
            requested_by_uid=user.uid,
            replacements=[r.model_dump() for r in payload.replacements],
        )
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=404 if detail == "org_not_found" else 400, detail=detail
        ) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc) or "forbidden") from exc
