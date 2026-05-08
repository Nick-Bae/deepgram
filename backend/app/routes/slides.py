"""
Slide CRUD endpoints for presentation-display-mode (Design Ref: §4).

Owns: PNG/JPEG upload, ordering, current-slide-index broadcast, and the public
read-only state endpoint used by reconnecting display clients.

Storage layout (Firebase Storage):
    orgs/{orgId}/services/{serviceKey}/slides/{slideId}.{png|jpg}

Firestore layout:
    organizations/{orgId}/services/{serviceKey}/slides/{slideId}
    organizations/{orgId}/services/{serviceKey}  (currentSlideIndex, slidesVisibility, slideCount)

Route conventions match the existing repo pattern (see routes/multichurch.py,
routes/script.py): write paths are org-scoped under /org/{org_id}/...; public
read paths use the church slug under /c/{slug}/s/{service_key}/...
"""
from __future__ import annotations

from datetime import timedelta
import io
import logging
import os
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Path, Request, UploadFile
from pydantic import BaseModel, Field

from app.auth.firebase_auth import AuthenticatedUser, get_current_user_required
from app.auth.guards import require_org_role
from app.security_log import security_event, client_ip as _client_ip
from app.services.multichurch_store import multichurch_store
from app.socket_manager import manager as ws_manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["slides"])

# Design Ref: §10.3 — environment-tunable caps; defaults match Plan §3.2 NFRs.
MAX_SLIDE_IMAGE_BYTES = max(
    1, int((os.getenv("MAX_SLIDE_IMAGE_BYTES") or str(10 * 1024 * 1024)).strip() or "0") or 10 * 1024 * 1024
)
MAX_SLIDES_PER_SERVICE = max(
    1, int((os.getenv("MAX_SLIDES_PER_SERVICE") or "50").strip() or "0") or 50
)
SLIDE_SIGNED_URL_TTL_SEC = max(
    300, int((os.getenv("SLIDE_SIGNED_URL_TTL_SEC") or "86400").strip() or "0") or 86400
)

ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg"}
HOST_ROLES = ("owner", "admin", "host")
MEMBER_ROLES = ("owner", "admin", "host", "viewer")

# Magic-byte signatures — defense in depth against client-supplied Content-Type lies.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"


class SlideUpdate(BaseModel):
    caption: Optional[str] = Field(default=None, max_length=240)


class SlideOrderUpdate(BaseModel):
    orderedSlideIds: List[str] = Field(..., min_length=1, max_length=MAX_SLIDES_PER_SERVICE)


class SlideIndexUpdate(BaseModel):
    index: int = Field(..., ge=0)
    roomId: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _detect_image_type(blob: bytes) -> Optional[str]:
    """Inspect magic bytes; return canonical MIME or None if unrecognised."""
    if blob.startswith(_PNG_SIGNATURE):
        return "image/png"
    if blob.startswith(_JPEG_SIGNATURE):
        return "image/jpeg"
    return None


def _file_extension_for(content_type: str) -> str:
    return "png" if content_type == "image/png" else "jpg"


def _bucket():
    """Return the Firebase Storage bucket; raises 500 if not configured."""
    try:
        from firebase_admin import storage as _fb_storage  # type: ignore
    except Exception as exc:  # pragma: no cover - missing dependency
        raise HTTPException(status_code=500, detail="storage_unavailable") from exc
    explicit = (os.getenv("FIREBASE_STORAGE_BUCKET") or "").strip()
    if explicit:
        bucket_name = explicit
    else:
        # Fallback: derive from project id. New Firebase projects use {project}.firebasestorage.app;
        # legacy projects use {project}.appspot.com. Try the modern format first.
        project = (
            (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
            or (os.getenv("GCP_PROJECT") or "").strip()
            or (os.getenv("FIREBASE_PROJECT_ID") or "").strip()
            or (os.getenv("FIRESTORE_PROJECT") or "").strip()
        )
        if not project:
            raise HTTPException(status_code=500, detail="storage_bucket_not_configured")
        bucket_name = f"{project}.firebasestorage.app"
    try:
        return _fb_storage.bucket(bucket_name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="storage_bucket_unavailable") from exc


def _strip_exif_and_get_size(blob: bytes, content_type: str) -> tuple[bytes, int, int]:
    """Return (stripped_bytes, width, height). Falls back to original on Pillow errors."""
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return blob, 0, 0
    try:
        with Image.open(io.BytesIO(blob)) as img:
            width, height = img.size
            output = io.BytesIO()
            save_format = "PNG" if content_type == "image/png" else "JPEG"
            save_kwargs: Dict[str, Any] = {}
            if save_format == "JPEG":
                save_kwargs["quality"] = 90
                save_kwargs["optimize"] = True
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
            else:
                save_kwargs["optimize"] = True
            img.save(output, format=save_format, **save_kwargs)  # exif stripped by default
            return output.getvalue(), int(width), int(height)
    except Exception:
        return blob, 0, 0


def _signed_url_for(blob, *, public: bool) -> str:
    """Return a public CDN URL or a signed URL with TTL for private slides."""
    if public:
        try:
            blob.make_public()
            return blob.public_url
        except Exception:
            pass
    try:
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=SLIDE_SIGNED_URL_TTL_SEC),
            method="GET",
        )
    except Exception:
        return getattr(blob, "media_link", None) or ""


def _attach_urls(slides: List[Dict[str, Any]], visibility: str) -> List[Dict[str, Any]]:
    if not slides:
        return slides
    public = visibility == "public"
    try:
        bucket = _bucket()
    except HTTPException:
        # If storage is not configured, return slides without URLs rather than failing the entire request.
        return [{**slide, "url": slide.get("publicUrl") or ""} for slide in slides]
    out: List[Dict[str, Any]] = []
    for slide in slides:
        path = slide.get("storagePath") or ""
        url = slide.get("publicUrl") if public else None
        if not url and path:
            try:
                blob = bucket.blob(path)
                url = _signed_url_for(blob, public=public)
            except Exception:
                url = ""
        out.append({**slide, "url": url})
    return out


def _http_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": code, "message": message},
    )


def _resolve_org_from_slug(slug: str, service_key: str) -> Dict[str, Any]:
    """Public-read resolver: slug + service_key → org doc."""
    resolved = multichurch_store.resolve_service(slug=slug, service_key=service_key)
    if not resolved:
        raise _http_error(404, "SERVICE_NOT_FOUND", "Service not found.")
    return resolved


# ─────────────────────────────────────────────────────────────────────────────
# Public read endpoint — used by display reconnects (no auth required, matches
# existing /c/{slug}/s/{service_key}/resolve pattern in routes/multichurch.py)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/c/{slug}/s/{service_key}/slides/state")
async def get_slide_state_public(
    request: Request,
    slug: str = Path(..., min_length=1, max_length=128),
    service_key: str = Path(..., min_length=1, max_length=128),
):
    resolved = _resolve_org_from_slug(slug, service_key)
    org_id = resolved.get("orgId") or ""
    if not org_id:
        raise _http_error(404, "SERVICE_NOT_FOUND", "Service not found.")
    state = multichurch_store.get_slide_state(org_id=org_id, service_key=service_key)
    state["slides"] = _attach_urls(state.get("slides") or [], state.get("slidesVisibility") or "private")
    return {"data": state}


# ─────────────────────────────────────────────────────────────────────────────
# Org-scoped CRUD endpoints — match existing /org/{org_id}/services/{service_key}/...
# pattern from routes/script.py and routes/multichurch.py.
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/org/{org_id}/services/{service_key}/slides")
async def list_slides(
    request: Request,
    org_id: str = Path(..., min_length=1, max_length=128),
    service_key: str = Path(..., min_length=1, max_length=128),
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=org_id, user=user, roles=MEMBER_ROLES, store=multichurch_store)
    state = multichurch_store.get_slide_state(org_id=org_id, service_key=service_key)
    slides = _attach_urls(state.get("slides") or [], state.get("slidesVisibility") or "private")
    return {"data": {"slides": slides, "currentSlideIndex": state.get("currentSlideIndex")}}


@router.post("/org/{org_id}/services/{service_key}/slides")
async def upload_slides(
    request: Request,
    org_id: str = Path(..., min_length=1, max_length=128),
    service_key: str = Path(..., min_length=1, max_length=128),
    files: List[UploadFile] = File(...),
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=org_id, user=user, roles=HOST_ROLES, store=multichurch_store)

    if not files:
        raise _http_error(400, "NO_FILES", "Upload at least one image.")

    state = multichurch_store.get_slide_state(org_id=org_id, service_key=service_key)
    existing_count = int(state.get("slideCount") or 0)
    visibility = state.get("slidesVisibility") or "private"
    if existing_count + len(files) > MAX_SLIDES_PER_SERVICE:
        raise _http_error(
            400,
            "SLIDE_LIMIT_EXCEEDED",
            f"Service slide limit reached ({MAX_SLIDES_PER_SERVICE}).",
        )

    bucket = _bucket()
    saved: List[Dict[str, Any]] = []
    next_order = existing_count
    for upload in files:
        blob_bytes = await upload.read()
        if len(blob_bytes) > MAX_SLIDE_IMAGE_BYTES:
            raise _http_error(
                400,
                "FILE_TOO_LARGE",
                f"Image must be under {MAX_SLIDE_IMAGE_BYTES // (1024 * 1024)}MB.",
            )
        if len(blob_bytes) == 0:
            raise _http_error(400, "EMPTY_FILE", "Uploaded file is empty.")

        detected = _detect_image_type(blob_bytes)
        client_type = (upload.content_type or "").strip().lower()
        if detected is None or detected not in ALLOWED_CONTENT_TYPES:
            raise _http_error(400, "INVALID_FILE_TYPE", "Only PNG or JPEG images are accepted.")
        if client_type and client_type not in ALLOWED_CONTENT_TYPES:
            raise _http_error(400, "INVALID_FILE_TYPE", "Declared Content-Type not allowed.")

        stripped, width, height = _strip_exif_and_get_size(blob_bytes, detected)
        slide_id = uuid4().hex
        ext = _file_extension_for(detected)
        storage_path = f"orgs/{org_id}/services/{service_key}/slides/{slide_id}.{ext}"
        try:
            obj = bucket.blob(storage_path)
            obj.upload_from_string(stripped, content_type=detected, timeout=30)
            obj.cache_control = "public, max-age=3600" if visibility == "public" else "private, max-age=300"
            obj.patch()
        except HTTPException:
            raise
        except Exception:
            logger.exception("slide_upload_storage_error org=%s service=%s", org_id, service_key)
            raise _http_error(500, "STORAGE_UPLOAD_FAILED", "Image upload failed.")

        record = multichurch_store.add_slide(
            org_id=org_id,
            service_key=service_key,
            slide={
                "slideId": slide_id,
                "order": next_order,
                "storagePath": storage_path,
                "contentType": detected,
                "byteSize": len(stripped),
                "width": width,
                "height": height,
                "createdBy": user.uid,
            },
        )
        next_order += 1
        saved.append(record)

        security_event(
            "slide_uploaded",
            ip=_client_ip(request),
            actor_uid=user.uid,
            org_id=org_id,
            service_key=service_key,
            slide_id=slide_id,
            byte_size=len(stripped),
        )

    saved = _attach_urls(saved, visibility)
    return {"data": {"slides": saved}}


@router.patch("/org/{org_id}/services/{service_key}/slides/order")
async def reorder_slides(
    request: Request,
    org_id: str = Path(..., min_length=1, max_length=128),
    service_key: str = Path(..., min_length=1, max_length=128),
    body: SlideOrderUpdate = ...,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=org_id, user=user, roles=HOST_ROLES, store=multichurch_store)

    slides = multichurch_store.reorder_slides(
        org_id=org_id, service_key=service_key, ordered_slide_ids=body.orderedSlideIds
    )
    state = multichurch_store.get_slide_state(org_id=org_id, service_key=service_key)
    enriched = _attach_urls(slides, state.get("slidesVisibility") or "private")

    security_event(
        "slides_reordered",
        ip=_client_ip(request),
        actor_uid=user.uid,
        org_id=org_id,
        service_key=service_key,
        slide_count=len(enriched),
    )
    return {"data": {"slides": enriched}}


@router.patch("/org/{org_id}/services/{service_key}/slides/{slide_id}")
async def update_slide(
    request: Request,
    org_id: str = Path(..., min_length=1, max_length=128),
    service_key: str = Path(..., min_length=1, max_length=128),
    slide_id: str = Path(..., min_length=1, max_length=64),
    body: SlideUpdate = ...,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=org_id, user=user, roles=HOST_ROLES, store=multichurch_store)

    updates: Dict[str, Any] = {}
    if body.caption is not None:
        updates["caption"] = body.caption

    if not updates:
        raise _http_error(400, "NO_UPDATES", "No updatable fields provided.")

    updated = multichurch_store.update_slide(
        org_id=org_id, service_key=service_key, slide_id=slide_id, updates=updates
    )
    if not updated:
        raise _http_error(404, "SLIDE_NOT_FOUND", "Slide does not exist.")
    state = multichurch_store.get_slide_state(org_id=org_id, service_key=service_key)
    [enriched] = _attach_urls([updated], state.get("slidesVisibility") or "private")
    return {"data": {"slide": enriched}}


@router.delete("/org/{org_id}/services/{service_key}/slides/{slide_id}")
async def delete_slide(
    request: Request,
    org_id: str = Path(..., min_length=1, max_length=128),
    service_key: str = Path(..., min_length=1, max_length=128),
    slide_id: str = Path(..., min_length=1, max_length=64),
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=org_id, user=user, roles=HOST_ROLES, store=multichurch_store)

    existing = multichurch_store.get_slide(org_id=org_id, service_key=service_key, slide_id=slide_id)
    if not existing:
        raise _http_error(404, "SLIDE_NOT_FOUND", "Slide does not exist.")

    storage_path = existing.get("storagePath") or ""
    if storage_path:
        try:
            _bucket().blob(storage_path).delete(timeout=15)
        except HTTPException:
            pass
        except Exception:
            logger.warning(
                "slide_delete_storage_miss org=%s service=%s path=%s",
                org_id,
                service_key,
                storage_path,
            )

    deleted = multichurch_store.delete_slide(
        org_id=org_id, service_key=service_key, slide_id=slide_id
    )
    if not deleted:
        raise _http_error(500, "DELETE_FAILED", "Slide could not be deleted.")

    security_event(
        "slide_deleted",
        ip=_client_ip(request),
        actor_uid=user.uid,
        org_id=org_id,
        service_key=service_key,
        slide_id=slide_id,
    )
    return {"data": {"deleted": True, "slideId": slide_id}}


@router.post("/org/{org_id}/services/{service_key}/slides/index")
async def set_slide_index(
    request: Request,
    org_id: str = Path(..., min_length=1, max_length=128),
    service_key: str = Path(..., min_length=1, max_length=128),
    body: SlideIndexUpdate = ...,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=org_id, user=user, roles=HOST_ROLES, store=multichurch_store)

    state = multichurch_store.get_slide_state(org_id=org_id, service_key=service_key)
    slides = state.get("slides") or []
    if not slides:
        raise _http_error(400, "NO_SLIDES", "This service has no slides.")
    if body.index >= len(slides):
        raise _http_error(400, "INDEX_OUT_OF_RANGE", "Slide index is out of range.")

    new_index = multichurch_store.set_current_slide_index(
        org_id=org_id, service_key=service_key, index=body.index
    )
    target = slides[body.index]
    enriched = _attach_urls([target], state.get("slidesVisibility") or "private")[0]

    delivered = 0
    room_id = (body.roomId or "").strip() or multichurch_store.get_active_room(org_id, service_key) or ""
    if room_id:
        try:
            delivered = await ws_manager.broadcast_slide_change(
                org_id,
                room_id,
                {
                    "index": new_index,
                    "slideId": target.get("slideId"),
                    "url": enriched.get("url"),
                },
            )
        except Exception:
            logger.exception("slide_broadcast_error org=%s room=%s", org_id, room_id)

    security_event(
        "slide_index_set",
        ip=_client_ip(request),
        actor_uid=user.uid,
        org_id=org_id,
        service_key=service_key,
        slide_index=new_index,
        room_id=room_id or None,
        delivered=delivered,
    )

    return {
        "data": {
            "currentSlideIndex": new_index,
            "broadcastedTo": delivered,
            "slide": enriched,
        }
    }
