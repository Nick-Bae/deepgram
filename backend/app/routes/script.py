import asyncio
import os
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import validators
from app.auth.guards import require_org_role, resolve_default_org_id_for_roles
from app.auth.firebase_auth import AuthenticatedUser, get_current_user_required
from app.env import ENV
from app.services.multichurch_store import multichurch_store
from app.services.script_store import script_store
from app.utils.translate import translate_text

router = APIRouter(tags=["script"])
SCRIPT_EDITOR_ROLES = {"owner", "admin", "host"}
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。？！…])\s+|\n{2,}")
SERMON_TRANSLATION_MODEL = ENV.SERMON_TRANSLATION_MODEL


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


def _resolve_sermon_translation_concurrency(segment_count: int) -> int:
    if segment_count <= 0:
        return SERMON_TRANSLATION_CONCURRENCY
    extra_workers = max(0, (segment_count - 1) // 20)
    return min(6, SERMON_TRANSLATION_CONCURRENCY + extra_workers)


class Pair(BaseModel):
    source: str = Field(..., min_length=1, description="Korean source text")
    target: str = Field(..., min_length=1, description="English target text")


class UploadPayload(BaseModel):
    payload: dict
    cfg: dict | None = None


class SermonDraftRequest(BaseModel):
    sermon_id: str = Field(..., min_length=1, max_length=120, pattern=validators.SERMON_ID)
    korean: str = Field(..., min_length=1)
    auto_split: bool = True
    threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    lang_src: str = Field(default="ko", min_length=2, max_length=20, pattern=validators.LANG_CODE)
    lang_tgt: str = Field(default="en", min_length=2, max_length=20, pattern=validators.LANG_CODE)


class SermonSegment(BaseModel):
    id: int = Field(..., ge=1)
    ko: str = Field(..., min_length=1)
    en: str = Field(..., min_length=1)


class SermonFinalizeRequest(BaseModel):
    sermon_id: str = Field(..., min_length=1, max_length=120, pattern=validators.SERMON_ID)
    threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    lang_src: str = Field(default="ko", min_length=2, max_length=20, pattern=validators.LANG_CODE)
    lang_tgt: str = Field(default="en", min_length=2, max_length=20, pattern=validators.LANG_CODE)
    segments: list[SermonSegment] = Field(default_factory=list)


class SermonBudgetRequest(BaseModel):
    budget_usd: float = Field(..., ge=0.0, le=1_000_000.0)


# Service-scoped sermon request models (sermon-service-isolation)
_SERVICE_DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
_SERVICE_KEY_RE = r"^[a-zA-Z0-9_\-]{1,80}$"


class ServiceSermonDraftRequest(BaseModel):
    service_date: str = Field(..., pattern=_SERVICE_DATE_RE, description="YYYY-MM-DD")
    korean: str = Field(..., min_length=1)
    auto_split: bool = True
    threshold: float = Field(default=0.84, ge=0.0, le=1.0)
    lang_src: str = Field(default="ko", min_length=2, max_length=20, pattern=validators.LANG_CODE)
    lang_tgt: str = Field(default="en", min_length=2, max_length=20, pattern=validators.LANG_CODE)


class ServiceSermonSaveRequest(BaseModel):
    segments: list[SermonSegment] = Field(default_factory=list)
    threshold: float = Field(default=0.84, ge=0.0, le=1.0)
    lang_src: str = Field(default="ko", min_length=2, max_length=20, pattern=validators.LANG_CODE)
    lang_tgt: str = Field(default="en", min_length=2, max_length=20, pattern=validators.LANG_CODE)


def _split_korean_text(raw: str, auto_split: bool) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not auto_split:
        return [line.strip() for line in text.splitlines() if line.strip()]
    # Treat single newlines as soft wraps from pasted documents, not sentence breaks.
    normalized = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = re.sub(r"\n[ \t]+", "\n", normalized)
    return [part.strip() for part in SENTENCE_SPLIT_RE.split(normalized) if part.strip()]


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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    custom_prompt, service_prompt = _resolve_org_prompt_overrides(org_id)
    semaphore = asyncio.Semaphore(_resolve_sermon_translation_concurrency(len(segments)))

    async def _translate_one(idx: int, source_text: str) -> tuple[dict[str, Any], dict[str, Any]]:
        async with semaphore:
            usage_out: dict[str, Any] = {}
            target_text = await translate_text(
                source_text,
                source_lang,
                target_lang,
                custom_prompt=custom_prompt,
                service_prompt=service_prompt,
                model_override=SERMON_TRANSLATION_MODEL,
                usage_out=usage_out,
            )
            row = {
                "id": idx + 1,
                "ko": source_text,
                "en": (target_text or "").strip(),
            }
            return row, usage_out

    translated_rows: list[dict[str, Any]] = []
    usage_totals: dict[str, Any] = {
        "promptTokens": 0,
        "completionTokens": 0,
        "totalTokens": 0,
        "estimatedUsd": 0.0,
        "requestsCount": 0,
        "model": SERMON_TRANSLATION_MODEL,
    }
    translated_with_usage = await asyncio.gather(*[_translate_one(idx, source_text) for idx, source_text in enumerate(segments)])
    for row, usage in translated_with_usage:
        translated_rows.append(row)
        usage_totals["promptTokens"] += int(usage.get("promptTokens") or 0)
        usage_totals["completionTokens"] += int(usage.get("completionTokens") or 0)
        usage_totals["totalTokens"] += int(usage.get("totalTokens") or 0)
        usage_totals["estimatedUsd"] += float(usage.get("estimatedUsd") or 0.0)
        usage_totals["requestsCount"] += 1
        model_name = str(usage.get("model") or "").strip()
        if model_name:
            usage_totals["model"] = model_name

    translated_rows.sort(key=lambda row: int(row.get("id") or 0))
    usage_totals["estimatedUsd"] = round(max(0.0, float(usage_totals.get("estimatedUsd") or 0.0)), 6)
    return translated_rows, usage_totals


@router.get("/org/{org_id}/script")
def script_status(
    org_id: str,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=org_id, user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
    count, threshold, version = script_store.stats(org_id=org_id)
    return {"count": count, "threshold": threshold, "version": version}


@router.get("/script")
def script_status_default_org(
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    org_id = resolve_default_org_id_for_roles(user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
    return script_status(org_id=org_id, user=user)


@router.post("/org/{org_id}/script/upload")
def upload_script(
    org_id: str,
    body: UploadPayload,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=org_id, user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
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
    org_id = resolve_default_org_id_for_roles(user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
    return upload_script(org_id=org_id, body=body, user=user)


@router.delete("/org/{org_id}/script")
def clear_script(
    org_id: str,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=org_id, user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
    removed, version = script_store.clear(org_id=org_id)
    return {"cleared": True, "removed": removed, "version": version}


@router.delete("/script")
def clear_script_default_org(
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    org_id = resolve_default_org_id_for_roles(user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
    return clear_script(org_id=org_id, user=user)


@router.post("/org/{org_id}/sermon/draft")
async def draft_sermon(
    org_id: str,
    body: SermonDraftRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=org_id, user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
    try:
        budget_state = multichurch_store.ensure_sermon_prep_budget_not_reached(org_id=org_id)
    except ValueError as exc:
        detail = str(exc)
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "sermon_budget_check_failed") from exc
    except PermissionError as exc:
        detail = str(exc) or "forbidden"
        if detail == "sermon_prep_budget_reached":
            raise HTTPException(status_code=402, detail=detail) from exc
        raise HTTPException(status_code=403, detail=detail) from exc

    sermon_id = body.sermon_id.strip()
    source_lang = body.lang_src.strip().lower() or "ko"
    target_lang = body.lang_tgt.strip().lower() or "en"
    parts = _split_korean_text(body.korean, body.auto_split)
    if not parts:
        raise HTTPException(status_code=400, detail="No valid Korean segments found")

    translated, usage_totals = await _translate_segments(
        segments=parts,
        source_lang=source_lang,
        target_lang=target_lang,
        org_id=org_id,
    )
    try:
        budget_state = multichurch_store.record_sermon_prep_usage(
            org_id=org_id,
            sermon_id=sermon_id,
            prompt_tokens=int(usage_totals.get("promptTokens") or 0),
            completion_tokens=int(usage_totals.get("completionTokens") or 0),
            total_tokens=int(usage_totals.get("totalTokens") or 0),
            estimated_usd=float(usage_totals.get("estimatedUsd") or 0.0),
            requests_count=int(usage_totals.get("requestsCount") or len(parts)),
            model=str(usage_totals.get("model") or SERMON_TRANSLATION_MODEL),
        )
    except ValueError as exc:
        detail = str(exc)
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        if detail == "invalid_sermon_id":
            raise HTTPException(status_code=400, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "sermon_usage_record_failed") from exc

    return {
        "sermon_id": sermon_id,
        "threshold": body.threshold,
        "lang_src": source_lang,
        "lang_tgt": target_lang,
        "segments": translated,
        "usage": budget_state,
    }


@router.post("/sermon/draft")
async def draft_sermon_default_org(
    body: SermonDraftRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    org_id = resolve_default_org_id_for_roles(user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
    return await draft_sermon(org_id=org_id, body=body, user=user)


@router.post("/org/{org_id}/sermon/finalize")
def finalize_sermon(
    org_id: str,
    body: SermonFinalizeRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=org_id, user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
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

    loaded, used_threshold, version = script_store.load(
        pairs,
        body.threshold,
        org_id=org_id,
        target_lang=target_lang,
    )
    payload = {
        "sermon_id": sermon_id,
        "threshold": used_threshold,
        "lang_src": source_lang,
        "lang_tgt": target_lang,
        "segments": normalized_segments,
    }
    script_store.save_sermon(payload, org_id=org_id)

    # Fire-and-forget Firestore persistence — never fails the finalize response
    try:
        multichurch_store.save_sermon_pairs(
            org_id=org_id,
            sermon_id=sermon_id,
            pairs=pairs,
            threshold=used_threshold,
            lang_src=source_lang,
            lang_tgt=target_lang,
        )
    except Exception:
        pass

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
    org_id = resolve_default_org_id_for_roles(user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
    return finalize_sermon(org_id=org_id, body=body, user=user)


@router.get("/org/{org_id}/sermon/usage")
def get_sermon_usage(
    org_id: str,
    periodKey: str | None = None,
    sermonId: str | None = None,
    limit: int = 25,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=org_id, user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
    try:
        return multichurch_store.get_org_sermon_prep_usage(
            org_id=org_id,
            requested_by_uid=user.uid,
            period_key=periodKey,
            sermon_id=sermonId,
            limit=limit,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail == "invalid_uid":
            raise HTTPException(status_code=400, detail=detail) from exc
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "sermon_usage_fetch_failed") from exc
    except PermissionError as exc:
        detail = str(exc) or "forbidden"
        raise HTTPException(status_code=403, detail=detail) from exc


@router.get("/sermon/usage")
def get_sermon_usage_default_org(
    periodKey: str | None = None,
    sermonId: str | None = None,
    limit: int = 25,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    org_id = resolve_default_org_id_for_roles(user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
    return get_sermon_usage(
        org_id=org_id,
        periodKey=periodKey,
        sermonId=sermonId,
        limit=limit,
        user=user,
    )


@router.post("/org/{org_id}/sermon/budget")
def set_sermon_budget(
    org_id: str,
    body: SermonBudgetRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=org_id, user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
    try:
        return multichurch_store.set_org_sermon_prep_budget(
            org_id=org_id,
            requested_by_uid=user.uid,
            budget_usd=float(body.budget_usd),
            requested_by_email=user.email,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail in {"invalid_uid", "invalid_budget"}:
            raise HTTPException(status_code=400, detail=detail) from exc
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "sermon_budget_update_failed") from exc
    except PermissionError as exc:
        detail = str(exc) or "forbidden"
        raise HTTPException(status_code=403, detail=detail) from exc


@router.post("/sermon/budget")
def set_sermon_budget_default_org(
    body: SermonBudgetRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    org_id = resolve_default_org_id_for_roles(user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
    return set_sermon_budget(org_id=org_id, body=body, user=user)


# ---------------------------------------------------------------------------
# Service-scoped sermon endpoints (sermon-service-isolation)
# Path: /org/{orgId}/services/{serviceKey}/sermon/*
# ---------------------------------------------------------------------------

@router.post("/org/{org_id}/services/{service_key}/sermon/draft")
async def draft_service_sermon(
    org_id: str,
    service_key: str,
    body: ServiceSermonDraftRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    """Generate an AI translation draft for a specific service slot + date."""
    require_org_role(org_id=org_id, user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)

    service_date = body.service_date.strip()
    source_lang = body.lang_src.strip().lower() or "ko"
    target_lang = body.lang_tgt.strip().lower() or "en"

    raw_segments = _split_korean_text(body.korean, body.auto_split)
    if not raw_segments:
        raise HTTPException(status_code=400, detail="korean text produced no segments")

    translated_rows, usage = await _translate_segments(
        segments=raw_segments,
        source_lang=source_lang,
        target_lang=target_lang,
        org_id=org_id,
    )

    # Persist as draft immediately (so user can resume editing)
    pairs = [{"source": r["ko"], "target": r["en"]} for r in translated_rows]
    multichurch_store.save_sermon_draft(
        org_id, service_key, service_date,
        pairs=pairs,
        threshold=body.threshold,
        lang_src=source_lang,
        lang_tgt=target_lang,
        created_by=user.uid,
    )

    return {
        "serviceKey": service_key,
        "serviceDate": service_date,
        "status": "draft",
        "segments": translated_rows,
        "usage": usage,
    }


@router.get("/org/{org_id}/services/{service_key}/sermon/{service_date}")
def get_service_sermon(
    org_id: str,
    service_key: str,
    service_date: str,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    """Return a sermon draft or published doc for a service slot + date."""
    require_org_role(org_id=org_id, user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
    doc = multichurch_store.get_sermon_draft(org_id, service_key, service_date)
    if not doc:
        raise HTTPException(status_code=404, detail="sermon_not_found")
    return doc


@router.put("/org/{org_id}/services/{service_key}/sermon/{service_date}")
def save_service_sermon(
    org_id: str,
    service_key: str,
    service_date: str,
    body: ServiceSermonSaveRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    """Save edited segments to a draft sermon (does not publish)."""
    require_org_role(org_id=org_id, user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)

    if not body.segments:
        raise HTTPException(status_code=400, detail="segments must be a non-empty list")

    source_lang = body.lang_src.strip().lower() or "ko"
    target_lang = body.lang_tgt.strip().lower() or "en"
    pairs: list[dict] = []
    for idx, seg in enumerate(body.segments):
        ko = seg.ko.strip()
        en = seg.en.strip()
        if not ko or not en:
            raise HTTPException(status_code=400, detail=f"segments[{idx}] must include both ko and en")
        pairs.append({"source": ko, "target": en})

    multichurch_store.save_sermon_draft(
        org_id, service_key, service_date,
        pairs=pairs,
        threshold=body.threshold,
        lang_src=source_lang,
        lang_tgt=target_lang,
        created_by=user.uid,
    )
    return {
        "status": "draft",
        "serviceKey": service_key,
        "serviceDate": service_date,
        "segmentCount": len(pairs),
    }


@router.post("/org/{org_id}/services/{service_key}/sermon/{service_date}/publish")
def publish_service_sermon(
    org_id: str,
    service_key: str,
    service_date: str,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    """
    Freeze a draft sermon as published for a service slot + date.
    Pre-warms the in-memory script store with the service+date key.
    """
    require_org_role(org_id=org_id, user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)

    doc = multichurch_store.get_sermon_draft(org_id, service_key, service_date)
    if not doc:
        raise HTTPException(status_code=404, detail="sermon_not_found")
    pairs = doc.get("pairs") or []
    if not pairs:
        raise HTTPException(status_code=400, detail="sermon_has_no_segments")

    result = multichurch_store.publish_sermon(org_id, service_key, service_date)
    if not result:
        raise HTTPException(status_code=500, detail="publish_failed")

    # Pre-warm in-memory store for instant room start
    script_store.load(
        pairs,
        doc.get("threshold"),
        service_key=service_key,
        service_date=service_date,
        org_id=org_id,
        target_lang=str(doc.get("langTgt") or doc.get("lang_tgt") or "en"),
    )

    return {
        "status": "published",
        "serviceKey": service_key,
        "serviceDate": service_date,
        "segmentCount": len(pairs),
        "preWarmed": True,
    }


@router.delete("/org/{org_id}/services/{service_key}/sermon/{service_date}/publish")
def unpublish_service_sermon(
    org_id: str,
    service_key: str,
    service_date: str,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    """Revert a published sermon to draft status (only if no live room is using it)."""
    require_org_role(org_id=org_id, user=user, roles={"owner", "admin"}, store=multichurch_store)

    doc = multichurch_store.get_sermon_draft(org_id, service_key, service_date)
    if not doc:
        raise HTTPException(status_code=404, detail="sermon_not_found")
    if doc.get("status") != "published":
        raise HTTPException(status_code=409, detail="not_published")

    try:
        sermon_ref = (
            multichurch_store._db  # type: ignore[attr-defined]
            .collection("organizations").document(org_id)
            .collection("services").document(service_key)
            .collection("sermons").document(service_date)
        )
        from google.cloud import firestore as gcf_fs  # type: ignore[import]
        sermon_ref.update({
            "status": "draft",
            "publishedAt": None,
            "updatedAt": gcf_fs.SERVER_TIMESTAMP,
        })
    except Exception as exc:
        raise HTTPException(status_code=500, detail="unpublish_failed") from exc

    return {"status": "draft", "serviceKey": service_key, "serviceDate": service_date}


@router.get("/org/{org_id}/services/{service_key}/sermons")
def list_service_sermons(
    org_id: str,
    service_key: str,
    limit: int = 10,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    """List sermon docs for a service slot, most recent first."""
    require_org_role(org_id=org_id, user=user, roles=SCRIPT_EDITOR_ROLES, store=multichurch_store)
    docs = multichurch_store.list_sermon_drafts(org_id, service_key, limit=min(limit, 50))
    return {"serviceKey": service_key, "sermons": docs, "count": len(docs)}
