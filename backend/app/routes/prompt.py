from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.firebase_auth import AuthenticatedUser, get_current_user_required
from app.services.multichurch_store import multichurch_store

router = APIRouter(tags=["prompt"])


class PromptPayload(BaseModel):
    prompt: str = Field(default="", max_length=16000)
    service_prompt: str = Field(default="", max_length=16000)


@router.get("/org/{org_id}/prompt")
def get_org_prompt(
    org_id: str,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    try:
        return multichurch_store.get_org_prompt(
            org_id=org_id,
            requested_by_uid=user.uid,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        if detail == "invalid_uid":
            raise HTTPException(status_code=400, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "prompt_fetch_failed") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc) or "forbidden") from exc


@router.post("/org/{org_id}/prompt")
def set_org_prompt(
    org_id: str,
    payload: PromptPayload,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    try:
        return multichurch_store.set_org_prompt(
            org_id=org_id,
            requested_by_uid=user.uid,
            prompt=payload.prompt or "",
            service_prompt=payload.service_prompt or "",
        )
    except ValueError as exc:
        detail = str(exc)
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        if detail == "invalid_uid":
            raise HTTPException(status_code=400, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "prompt_save_failed") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc) or "forbidden") from exc
