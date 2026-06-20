# Design Ref: §4 API Specification (6 endpoints) + §6.1 Error Codes +
# §7 Security (RBAC + audit logging via security_event).
# Module-3 boundary: this file owns the HTTP layer. Domain/validation/xlsx
# live in app.sermon_review.* (modules 1-2). Live broadcast splice (touching
# main.py:_translate_text_guarded) is deferred per module-3 scope.

from __future__ import annotations

import secrets
from typing import Any, Optional
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel

from app.auth.firebase_auth import AuthenticatedUser, get_current_user_required
from app.auth.guards import require_org_role
from app.env import ENV
from app.security_log import security_event
from app.services.multichurch_store import multichurch_store
from app.sermon_review import (
    ImportReadError,
    IngestError,
    Sermon,
    SermonConflictError,
    SermonNotFoundError,
    ServiceAlreadyLinkedError,
    build_sermon,
    build_xlsx,
    ingest_from_docx,
    ingest_from_google_docs,
    ingest_from_paste,
    ingest_from_txt,
    read_workbook,
    validate_workbook,
)
from app.utils.translate import translate_text

router = APIRouter(tags=["sermon-review"])


# Per Design §7 — viewer blocked everywhere. Link/unlink narrows further to
# owner+admin. Other endpoints accept host as well.
READ_ROLES = frozenset({"owner", "admin", "host"})
WRITE_ROLES = frozenset({"owner", "admin", "host"})
LINK_ROLES = frozenset({"owner", "admin"})


# The frontend uses Firebase Google sign-in with the `documents.readonly`
# OAuth scope, then forwards the Google access token in `X-Google-Access-Token`.
# We build a per-request Docs API client from that token. If the header is
# missing (email/password users, expired token), we return None so the route
# responds 501 → frontend shows the paste/upload fallback message.
def _google_docs_service_dependency(request: Request) -> Any:
    token = request.headers.get("x-google-access-token")
    if not token:
        return None
    return _build_docs_service(token.strip())


def _build_docs_service(access_token: str) -> Any:
    if not access_token:
        return None
    try:
        from google.oauth2.credentials import Credentials  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except Exception:
        return None
    try:
        creds = Credentials(token=access_token)
        return build("docs", "v1", credentials=creds, cache_discovery=False)
    except Exception:
        return None


def _map_google_docs_error(exc: IngestError) -> Optional[HTTPException]:
    """Convert a Google API HttpError (wrapped in IngestError) into a more
    actionable HTTP response. None means caller should re-raise as-is."""
    cause = exc.__cause__
    status = getattr(getattr(cause, "resp", None), "status", None)
    if status == 429:
        return _err(
            "GOOGLE_RATE_LIMITED",
            "Google Docs rate limit hit. Please try again in a minute.",
            status=429,
        )
    if status == 401:
        return _err(
            "GOOGLE_OAUTH_NOT_CONFIGURED",
            "Your Google sign-in expired. Please sign out and back in.",
            status=401,
        )
    if status in (403, 404):
        return _err(
            "INGEST_FAILED",
            "Couldn't read that Google Doc. Make sure you have access and the URL is correct.",
        )
    return None


# Translator factory — module-3 wraps the existing translate_text with org
# context so build_sermon (module-2) stays pure.
def _make_translator(
    *,
    org_id: str,
    source: str = "ko",
    target: str = "en",
):
    custom_prompt = ""
    service_prompt = ""
    try:
        org_prompt = multichurch_store.get_org_prompt_for_translation(org_id) or {}
        custom_prompt = str(org_prompt.get("prompt") or "")
        service_prompt = str(org_prompt.get("service_prompt") or "")
    except Exception:
        pass

    async def _translate(text: str) -> str:
        result = await translate_text(
            text,
            source,
            target,
            custom_prompt=custom_prompt,
            service_prompt=service_prompt,
            org_id=org_id,
            model_override=ENV.SERMON_TRANSLATION_MODEL,
        )
        return (result or "").strip()

    return _translate


def _new_sermon_id() -> str:
    return f"srm_{secrets.token_urlsafe(12)}"


def _err(code: str, message: str, *, status: int = 400, details: Any = None) -> HTTPException:
    payload: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        payload["details"] = details
    return HTTPException(status_code=status, detail={"error": payload})


def _ok(data: Any) -> dict[str, Any]:
    return {"data": data}


def _request_meta(request: Request) -> dict[str, Optional[str]]:
    client = request.client
    return {
        "ip": client.host if client else None,
        "method": request.method,
        "path": str(request.url.path),
    }


class _LinkBody(BaseModel):
    serviceKey: Optional[str] = None
    replace: bool = False


# -- POST /org/{org_id}/sermons/ingest ----------------------------------------


@router.post("/org/{org_id}/sermons/ingest")
async def ingest_sermon(
    org_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user_required),
    google_docs_service: Any = Depends(_google_docs_service_dependency),
    sourceType: str = Form(...),
    title: str = Form(...),
    text: Optional[str] = Form(None),
    url: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
):
    require_org_role(
        org_id=org_id, user=user, roles=WRITE_ROLES, store=multichurch_store
    )

    if not title.strip():
        raise _err("INVALID_SOURCE", "title is required.")

    source_ref: Optional[str] = None
    raw_text: str

    try:
        if sourceType == "paste":
            if not text:
                raise _err("INVALID_SOURCE", "text is required for sourceType=paste.")
            raw_text = ingest_from_paste(text)
        elif sourceType == "file_txt":
            data = await _read_upload(file, ENV.SERMON_SOURCE_MAX_BYTES)
            raw_text = ingest_from_txt(data)
            source_ref = (file.filename if file else None) or None
        elif sourceType == "file_docx":
            data = await _read_upload(file, ENV.SERMON_SOURCE_MAX_BYTES)
            raw_text = ingest_from_docx(data)
            source_ref = (file.filename if file else None) or None
        elif sourceType == "google_docs":
            if not url:
                raise _err("INVALID_SOURCE", "url is required for sourceType=google_docs.")
            if google_docs_service is None:
                raise _err(
                    "GOOGLE_OAUTH_NOT_CONFIGURED",
                    "Sign in with Google to import from Google Docs, or paste/upload instead.",
                    status=501,
                )
            try:
                raw_text = ingest_from_google_docs(url, google_docs_service)
            except IngestError as exc:
                mapped = _map_google_docs_error(exc)
                if mapped is not None:
                    raise mapped
                raise
            source_ref = url
        else:
            raise _err(
                "INVALID_SOURCE",
                f"Unknown sourceType: {sourceType!r}. Allowed: paste, "
                "file_txt, file_docx, google_docs.",
            )
    except IngestError as exc:
        raise _err("INGEST_FAILED", str(exc))

    sermon_id = _new_sermon_id()
    translator = _make_translator(org_id=org_id)

    try:
        sermon = await build_sermon(
            sermonId=sermon_id,
            orgId=org_id,
            title=title.strip(),
            sourceType=sourceType,  # type: ignore[arg-type]
            sourceRef=source_ref,
            text=raw_text,
            creatorUid=user.uid,
            translator=translator,
            max_segments=ENV.SERMON_MAX_SEGMENTS,
        )
    except IngestError as exc:
        # Most likely SEGMENT_LIMIT_EXCEEDED.
        if "exceeds maximum" in str(exc):
            raise _err("SEGMENT_LIMIT_EXCEEDED", str(exc), status=507)
        raise _err("INGEST_FAILED", str(exc))

    multichurch_store.create_review_sermon(sermon.model_dump())

    security_event(
        "sermon_review_ingested",
        severity="INFO",
        uid=user.uid,
        org_id=org_id,
        **_request_meta(request),
        sermon_id=sermon_id,
        source_type=sourceType,
        segment_count=len(sermon.segments),
    )

    return _ok(
        {
            "sermonId": sermon_id,
            "title": sermon.title,
            "segmentCount": len(sermon.segments),
        }
    )


async def _read_upload(
    file: Optional[UploadFile], max_bytes: int
) -> bytes:
    if file is None:
        raise _err("INVALID_SOURCE", "A file upload is required for this sourceType.")
    data = await file.read()
    if not data:
        raise _err("INVALID_SOURCE", "Uploaded file is empty.")
    if len(data) > max_bytes:
        raise _err(
            "PAYLOAD_TOO_LARGE",
            f"File exceeds maximum size of {max_bytes} bytes.",
            status=413,
        )
    return data


# -- GET /org/{org_id}/sermons -------------------------------------------------


@router.get("/org/{org_id}/sermons")
def list_sermons(
    org_id: str,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(
        org_id=org_id, user=user, roles=READ_ROLES, store=multichurch_store
    )
    sermons = multichurch_store.list_review_sermons(org_id)
    summaries = [
        {
            "sermonId": s.get("sermonId"),
            "title": s.get("title"),
            "sourceType": s.get("sourceType"),
            "segmentCount": len(s.get("segments") or []),
            "reviewedCount": sum(
                1
                for seg in (s.get("segments") or [])
                if str(seg.get("status") or "Draft") == "Reviewed"
            ),
            "updatedAt": s.get("updatedAt"),
        }
        for s in sermons
    ]
    return _ok(summaries)


# -- GET /org/{org_id}/sermons/{sermon_id} ------------------------------------


@router.get("/org/{org_id}/sermons/{sermon_id}")
def get_sermon(
    org_id: str,
    sermon_id: str,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(
        org_id=org_id, user=user, roles=READ_ROLES, store=multichurch_store
    )
    sermon = multichurch_store.get_review_sermon(org_id, sermon_id)
    if sermon is None:
        raise _err("SERMON_NOT_FOUND", "Sermon not found.", status=404)
    return _ok(sermon)


# -- GET /org/{org_id}/sermons/{sermon_id}/review-file.xlsx -------------------


@router.get(
    "/org/{org_id}/sermons/{sermon_id}/review-file.xlsx",
    response_class=Response,
)
def export_review_file(
    org_id: str,
    sermon_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(
        org_id=org_id, user=user, roles=READ_ROLES, store=multichurch_store
    )
    raw = multichurch_store.get_review_sermon(org_id, sermon_id)
    if raw is None:
        raise _err("SERMON_NOT_FOUND", "Sermon not found.", status=404)

    sermon = Sermon.model_validate(raw)
    data = build_xlsx(sermon)

    safe_title = _slugify(sermon.title or "sermon")
    short_id = sermon_id.split("_", 1)[-1][:8]
    filename = f"{safe_title}-{short_id}-review.xlsx"
    utf8_filename = f"{_unicode_filename_stem(sermon.title)}-{short_id}-review.xlsx"

    security_event(
        "sermon_review_exported",
        severity="INFO",
        uid=user.uid,
        org_id=org_id,
        **_request_meta(request),
        sermon_id=sermon_id,
        byte_size=len(data),
        segment_count=len(sermon.segments),
    )

    return Response(
        content=data,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"; '
                f"filename*=UTF-8''{quote(utf8_filename)}"
            )
        },
    )


def _slugify(text: str) -> str:
    cleaned = "".join(
        c if c.isascii() and (c.isalnum() or c in {"-", "_"}) else "-"
        for c in text.strip()
    )
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return (cleaned or "sermon").lower()[:50]


def _unicode_filename_stem(text: str) -> str:
    cleaned = "".join(
        c if c.isalnum() or c in {"-", "_"} else "-" for c in (text or "").strip()
    )
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return (cleaned or "sermon")[:50]


# -- POST /org/{org_id}/sermons/{sermon_id}/review-file -----------------------


@router.post("/org/{org_id}/sermons/{sermon_id}/review-file")
async def import_review_file(
    org_id: str,
    sermon_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user_required),
    file: Optional[UploadFile] = File(None),
):
    require_org_role(
        org_id=org_id, user=user, roles=WRITE_ROLES, store=multichurch_store
    )
    if file is None:
        raise _err("UNSUPPORTED_MEDIA_TYPE", "An .xlsx file upload is required.", status=415)

    data = await file.read()
    if len(data) > ENV.SERMON_MAX_UPLOAD_BYTES:
        raise _err(
            "PAYLOAD_TOO_LARGE",
            f"File exceeds maximum size of {ENV.SERMON_MAX_UPLOAD_BYTES} bytes.",
            status=413,
        )

    raw = multichurch_store.get_review_sermon(org_id, sermon_id)
    if raw is None:
        raise _err("SERMON_NOT_FOUND", "Sermon not found.", status=404)
    sermon = Sermon.model_validate(raw)

    try:
        rows, headers = read_workbook(data)
    except ImportReadError as exc:
        raise _err("UNSUPPORTED_MEDIA_TYPE", str(exc), status=415)

    report = validate_workbook(rows, sermon, headers=headers)

    if report.has_errors:
        security_event(
            "sermon_review_import_rejected",
            severity="WARNING",
            uid=user.uid,
            org_id=org_id,
            **_request_meta(request),
            sermon_id=sermon_id,
            errored=report.summary.errored,
            warned=report.summary.warned,
        )
        raise _err(
            "IMPORT_VALIDATION_FAILED",
            f"Import rejected — {report.summary.errored} error(s) in file.",
            details=report.model_dump(),
        )

    updates = [
        {
            "segmentId": row.get("Segment ID"),
            "reviewedTranslation": row.get("Reviewed Translation"),
            "notes": row.get("Notes") or "",
            "status": row.get("Status") or "Draft",
        }
        for row in rows
        if row.get("Segment ID")
    ]

    try:
        multichurch_store.update_review_sermon_segments(
            org_id,
            sermon_id,
            segment_updates=updates,
            expected_updated_at=sermon.updatedAt,
        )
    except SermonConflictError as exc:
        raise _err("SERMON_MODIFIED_CONCURRENTLY", str(exc), status=409)
    except SermonNotFoundError as exc:
        raise _err("SERMON_NOT_FOUND", str(exc), status=404)

    security_event(
        "sermon_review_imported",
        severity="INFO",
        uid=user.uid,
        org_id=org_id,
        **_request_meta(request),
        sermon_id=sermon_id,
        imported=report.summary.imported,
        warned=report.summary.warned,
    )

    return _ok(report.model_dump())


# -- POST /org/{org_id}/sermons/{sermon_id}/link ------------------------------


@router.post("/org/{org_id}/sermons/{sermon_id}/link")
def link_sermon(
    org_id: str,
    sermon_id: str,
    body: _LinkBody,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(
        org_id=org_id, user=user, roles=LINK_ROLES, store=multichurch_store
    )

    if not body.serviceKey:
        raise _err("INVALID_SOURCE", "serviceKey is required.")
    if multichurch_store.get_review_sermon(org_id, sermon_id) is None:
        raise _err("SERMON_NOT_FOUND", "Sermon not found.", status=404)

    try:
        result = multichurch_store.link_review_sermon_to_service(
            org_id,
            body.serviceKey,
            sermon_id,
            replace=body.replace,
        )
    except ServiceAlreadyLinkedError as exc:
        raise _err("SERVICE_ALREADY_LINKED", str(exc), status=409)
    except SermonNotFoundError as exc:
        raise _err("SERMON_NOT_FOUND", str(exc), status=404)
    except LookupError as exc:
        raise _err("SERMON_NOT_FOUND", str(exc), status=404)

    security_event(
        "sermon_review_linked",
        severity="INFO",
        uid=user.uid,
        org_id=org_id,
        **_request_meta(request),
        sermon_id=sermon_id,
        service_key=body.serviceKey,
        replace=body.replace,
    )

    return _ok(result)
