import asyncio
import os
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.firebase_auth import AuthenticatedUser, get_current_user_required
from app.services.multichurch_store import multichurch_store
from app.services.script_store import script_store
from app.utils.translate import translate_text

router = APIRouter(tags=["script"])
SCRIPT_EDITOR_ROLES = {"owner", "admin", "host"}
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。？！…])\s+|\n+")
SERMON_TRANSLATION_MODEL = (os.getenv("OPENAI_SERMON_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini"


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return max(min_value, min(max_value, parsed))


SERMON_TRANSLATION_CONCURRENCY = _env_int("SERMON_TRANSLATION_CONCURRENCY", 2, min_value=1, max_value=6)


class Pair(BaseModel):
    source: str = Field(..., min_length=1, description="Korean source text")
    target: str = Field(..., min_length=1, description="English target text")


class UploadPayload(BaseModel):
    payload: dict
    cfg: dict | None = None


class SermonDraftRequest(BaseModel):
    sermon_id: str = Field(..., min_length=1, max_length=120)
    korean: str = Field(..., min_length=1)
    auto_split: bool = True
    threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    lang_src: str = Field(default="ko", min_length=2, max_length=20)
    lang_tgt: str = Field(default="en", min_length=2, max_length=20)


class SermonSegment(BaseModel):
    id: int = Field(..., ge=1)
    ko: str = Field(..., min_length=1)
    en: str = Field(..., min_length=1)


class SermonFinalizeRequest(BaseModel):
    sermon_id: str = Field(..., min_length=1, max_length=120)
    threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    lang_src: str = Field(default="ko", min_length=2, max_length=20)
    lang_tgt: str = Field(default="en", min_length=2, max_length=20)
    segments: list[SermonSegment] = Field(default_factory=list)


def _require_script_editor(org_id: str, user: AuthenticatedUser) -> None:
    clean_org_id = (org_id or "").strip()
    if not clean_org_id:
        raise HTTPException(status_code=404, detail="org_not_found")
    memberships = multichurch_store.list_memberships(user.uid)
    match = next((row for row in memberships if str(row.get("orgId") or "").strip() == clean_org_id), None)
    if not match:
        raise HTTPException(status_code=403, detail="org_access_denied")
    role = str(match.get("role") or "").strip().lower()
    if role not in SCRIPT_EDITOR_ROLES:
        raise HTTPException(status_code=403, detail="forbidden")


def _resolve_default_editor_org_id(user: AuthenticatedUser) -> str:
    """
    Resolve a default org for non-org-scoped script endpoints.
    Priority:
    1) User's current org (if script-edit capable)
    2) First membership where role allows script editing
    """
    current_org_id = str(multichurch_store.get_current_org_id(user.uid) or "").strip()
    if current_org_id:
        try:
            _require_script_editor(current_org_id, user)
            return current_org_id
        except HTTPException as exc:
            if exc.status_code not in {403, 404}:
                raise

    memberships = multichurch_store.list_memberships(user.uid)
    for row in memberships:
        org_id = str(row.get("orgId") or "").strip()
        role = str(row.get("role") or "").strip().lower()
        if org_id and role in SCRIPT_EDITOR_ROLES:
            return org_id

    if memberships:
        raise HTTPException(status_code=403, detail="forbidden")
    raise HTTPException(status_code=404, detail="org_not_found")


def _split_korean_text(raw: str, auto_split: bool) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    if not auto_split:
        return [line.strip() for line in text.splitlines() if line.strip()]
    return [part.strip() for part in SENTENCE_SPLIT_RE.split(text) if part.strip()]


def _resolve_org_prompt_overrides(org_id: str) -> tuple[str | None, str | None]:
    try:
        org_prompt = multichurch_store.get_org_prompt_for_translation(org_id) or {}
    except Exception:
        return "", ""
    return str(org_prompt.get("prompt") or ""), str(org_prompt.get("service_prompt") or "")


async def _translate_segments(
    *,
    segments: list[str],
    source_lang: str,
    target_lang: str,
    org_id: str,
) -> list[dict[str, Any]]:
    custom_prompt, service_prompt = _resolve_org_prompt_overrides(org_id)
    semaphore = asyncio.Semaphore(SERMON_TRANSLATION_CONCURRENCY)

    async def _translate_one(idx: int, source_text: str) -> dict[str, Any]:
        async with semaphore:
            target_text = await translate_text(
                source_text,
                source_lang,
                target_lang,
                custom_prompt=custom_prompt,
                service_prompt=service_prompt,
                compact_prompt=True,
                model_override=SERMON_TRANSLATION_MODEL,
            )
            return {
                "id": idx + 1,
                "ko": source_text,
                "en": (target_text or "").strip(),
            }

    translated_rows = await asyncio.gather(*[_translate_one(idx, source_text) for idx, source_text in enumerate(segments)])
    translated_rows.sort(key=lambda row: int(row.get("id") or 0))
    return translated_rows


@router.get("/org/{org_id}/script")
def script_status(
    org_id: str,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    _require_script_editor(org_id, user)
    count, threshold, version = script_store.stats(org_id=org_id)
    return {"count": count, "threshold": threshold, "version": version}


@router.get("/script")
def script_status_default_org(
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    org_id = _resolve_default_editor_org_id(user)
    return script_status(org_id=org_id, user=user)


@router.post("/org/{org_id}/script/upload")
def upload_script(
    org_id: str,
    body: UploadPayload,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    _require_script_editor(org_id, user)
    pairs_raw = (body.payload or {}).get("pairs")
    if not isinstance(pairs_raw, list) or not pairs_raw:
        raise HTTPException(status_code=400, detail="payload.pairs must be a non-empty list")

    # Validate + normalize pairs
    pairs: list[dict[str, str]] = []
    for item in pairs_raw:
        src = (item.get("source") or "").strip()
        tgt = (item.get("target") or "").strip()
        if not src or not tgt:
            continue
        pairs.append({"source": src, "target": tgt})

    if not pairs:
        raise HTTPException(status_code=400, detail="No valid source/target pairs provided")

    threshold = None
    if body.cfg and "threshold" in body.cfg:
        try:
            threshold = float(body.cfg["threshold"])
        except Exception as exc:  # pragma: no cover - defensive
            raise HTTPException(status_code=400, detail=f"Invalid threshold: {exc}")

    loaded, used_threshold, version = script_store.load(pairs, threshold, org_id=org_id)
    return {"loaded": loaded, "threshold": used_threshold, "version": version}


@router.post("/script/upload")
def upload_script_default_org(
    body: UploadPayload,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    org_id = _resolve_default_editor_org_id(user)
    return upload_script(org_id=org_id, body=body, user=user)


@router.delete("/org/{org_id}/script")
def clear_script(
    org_id: str,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    _require_script_editor(org_id, user)
    removed, version = script_store.clear(org_id=org_id)
    return {"cleared": True, "removed": removed, "version": version}


@router.delete("/script")
def clear_script_default_org(
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    org_id = _resolve_default_editor_org_id(user)
    return clear_script(org_id=org_id, user=user)


@router.post("/org/{org_id}/sermon/draft")
async def draft_sermon(
    org_id: str,
    body: SermonDraftRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    _require_script_editor(org_id, user)
    sermon_id = body.sermon_id.strip()
    source_lang = body.lang_src.strip().lower() or "ko"
    target_lang = body.lang_tgt.strip().lower() or "en"
    parts = _split_korean_text(body.korean, body.auto_split)
    if not parts:
        raise HTTPException(status_code=400, detail="No valid Korean segments found")

    translated = await _translate_segments(
        segments=parts,
        source_lang=source_lang,
        target_lang=target_lang,
        org_id=org_id,
    )
    return {
        "sermon_id": sermon_id,
        "threshold": body.threshold,
        "lang_src": source_lang,
        "lang_tgt": target_lang,
        "segments": translated,
    }


@router.post("/sermon/draft")
async def draft_sermon_default_org(
    body: SermonDraftRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    org_id = _resolve_default_editor_org_id(user)
    return await draft_sermon(org_id=org_id, body=body, user=user)


@router.post("/org/{org_id}/sermon/finalize")
def finalize_sermon(
    org_id: str,
    body: SermonFinalizeRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    _require_script_editor(org_id, user)
    sermon_id = body.sermon_id.strip()
    source_lang = body.lang_src.strip().lower() or "ko"
    target_lang = body.lang_tgt.strip().lower() or "en"

    normalized_segments: list[dict[str, Any]] = []
    pairs: list[dict[str, str]] = []
    for idx, seg in enumerate(body.segments):
        ko = seg.ko.strip()
        en = seg.en.strip()
        if not ko or not en:
            raise HTTPException(status_code=400, detail=f"segments[{idx}] must include both ko and en")
        normalized_segments.append({"id": idx + 1, "ko": ko, "en": en})
        pairs.append({"source": ko, "target": en})

    if not pairs:
        raise HTTPException(status_code=400, detail="segments must be a non-empty list")

    loaded, used_threshold, version = script_store.load(pairs, body.threshold, org_id=org_id)
    payload = {
        "sermon_id": sermon_id,
        "threshold": used_threshold,
        "lang_src": source_lang,
        "lang_tgt": target_lang,
        "segments": normalized_segments,
    }
    script_store.save_sermon(payload, org_id=org_id)
    return {
        "saved": True,
        "loaded": loaded,
        "version": version,
        **payload,
    }


@router.post("/sermon/finalize")
def finalize_sermon_default_org(
    body: SermonFinalizeRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    org_id = _resolve_default_editor_org_id(user)
    return finalize_sermon(org_id=org_id, body=body, user=user)
