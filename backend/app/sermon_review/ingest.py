# Design Ref: §2.2 Ingest flow + §6.1 INGEST_FAILED error code.
# Four pure ingestion functions share an output shape (str) without an ABC —
# small functions are cheaper than a class hierarchy until a 5th source appears.
# build_sermon() orchestrates split → translate → assemble.

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Awaitable, Callable

from .models import Segment, Sermon, SourceType


class IngestError(ValueError):
    """Raised when source ingestion or sermon assembly fails."""


# Match the existing host-console sermon prep splitter
# (backend/app/routes/script.py::SENTENCE_SPLIT_RE). Do not infer a sentence
# boundary from Korean ending syllables such as "다": they also occur in
# wrapped phrases like "오래 하다 보면" and sentences like "다 맞는 말입니다."
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。？！…])\s+|\n{2,}")

_GOOGLE_DOC_ID_RE = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")


def _normalize_text(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Treat single newlines as soft wraps (paragraphs from pasted docs) but
    # keep double newlines as hard breaks.
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return text


def split_korean_text(raw: str, *, auto_split: bool = True) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not auto_split:
        # Treat each non-empty line as a segment; do NOT collapse soft wraps.
        return [line.strip() for line in text.splitlines() if line.strip()]
    text = _normalize_text(text)
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]


def generate_segment_id(index: int, total: int) -> str:
    """Generate S001/S002 zero-padded Segment IDs per Design §3.4."""
    if index < 1:
        raise ValueError("Segment index must be 1-based and positive.")
    if total < 1:
        raise ValueError("Total must be >= 1.")
    width = max(3, len(str(total)))
    return f"S{index:0{width}d}"


# --- Source-specific ingestion (each: → str) --------------------------------


def ingest_from_paste(text: str) -> str:
    normalized = _normalize_text(text)
    if not normalized:
        raise IngestError("Pasted text is empty.")
    return normalized


def ingest_from_txt(data: bytes) -> str:
    if not data:
        raise IngestError("Empty .txt file.")
    last_err: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp949"):
        try:
            text = data.decode(encoding)
            normalized = _normalize_text(text)
            if not normalized:
                raise IngestError("Decoded .txt file is empty.")
            return normalized
        except UnicodeDecodeError as exc:
            last_err = exc
            continue
    raise IngestError(
        f"Could not decode .txt file as Korean text: {last_err}"
    ) from last_err


def ingest_from_docx(data: bytes) -> str:
    if not data:
        raise IngestError("Empty .docx file.")
    try:
        from docx import Document  # python-docx — text extraction only
    except ImportError as exc:
        raise IngestError(
            "python-docx is not installed; cannot read .docx files."
        ) from exc
    try:
        doc = Document(BytesIO(data))
    except Exception as exc:
        raise IngestError(f"Could not parse .docx: {exc}") from exc

    parts: list[str] = []
    for paragraph in doc.paragraphs:
        line = (paragraph.text or "").strip()
        if line:
            parts.append(line)
    text = "\n".join(parts)
    normalized = _normalize_text(text)
    if not normalized:
        raise IngestError("No readable text found in .docx.")
    return normalized


def ingest_from_google_docs(url: str, service: Any) -> str:
    """Fetch a Google Doc as plain text.

    `service` is a `googleapiclient.discovery.Resource` for the Docs API,
    pre-authenticated by the caller. Module-2 stays out of OAuth — that lives
    in the route layer (module-3) where credentials are sourced.
    """
    doc_id = _extract_doc_id(url)
    if not doc_id:
        raise IngestError(
            f"Could not extract document ID from URL: {url!r}. "
            "Expected a /document/d/<id>/... URL."
        )
    try:
        document = service.documents().get(documentId=doc_id).execute()
    except Exception as exc:
        raise IngestError(f"Google Docs fetch failed: {exc}") from exc

    text = _extract_google_docs_text(document)
    normalized = _normalize_text(text)
    if not normalized:
        raise IngestError("Google Doc has no readable text content.")
    return normalized


def _extract_doc_id(url: str) -> str | None:
    m = _GOOGLE_DOC_ID_RE.search(url or "")
    return m.group(1) if m else None


def _extract_google_docs_text(document: dict[str, Any]) -> str:
    body = document.get("body") or {}
    parts: list[str] = []
    for element in body.get("content") or []:
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for piece in paragraph.get("elements") or []:
            text_run = piece.get("textRun") or {}
            parts.append(str(text_run.get("content") or ""))
    return "".join(parts)


# --- Orchestrator -----------------------------------------------------------


Translator = Callable[[str], Awaitable[str]]


async def build_sermon(
    *,
    sermonId: str,
    orgId: str,
    title: str,
    sourceType: SourceType,
    sourceRef: str | None,
    text: str,
    creatorUid: str,
    translator: Translator,
    now: datetime | None = None,
    max_segments: int = 1000,
    concurrency: int = 5,
) -> Sermon:
    """Split text → translate concurrently → assemble Sermon with stable IDs.

    `translator` is injected so unit tests can stub OpenAI. Module-3 wires the
    real `translate_text` with org/service prompts.

    Plan SC: 'sermon-per-org, optionally linked to one service' — this builds
    the canonical sermon entity that storage helpers persist.
    """
    raw_segments = split_korean_text(text)
    if not raw_segments:
        raise IngestError("No segments produced from source text.")
    if len(raw_segments) > max_segments:
        raise IngestError(
            f"Source produced {len(raw_segments)} segments, exceeds maximum "
            f"of {max_segments}."
        )

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _translate_one(index: int, source: str) -> str:
        async with semaphore:
            try:
                result = await translator(source)
            except Exception:
                # Per Design §6 'INGEST_FAILED' — surface failure at API layer,
                # but at the build_sermon layer we degrade to empty string so a
                # partial translation outage doesn't lose the whole sermon.
                # The user can re-export and edit.
                return ""
            return (result or "").strip()

    translations = await asyncio.gather(
        *[_translate_one(i, src) for i, src in enumerate(raw_segments)]
    )

    total = len(raw_segments)
    created_at = now or datetime.now(timezone.utc)
    segments = [
        Segment(
            segmentId=generate_segment_id(i + 1, total),
            order=i + 1,
            original=src,
            appTranslation=trans,
            reviewedTranslation=trans,  # pre-filled per Design §5.4 / FR-07
            notes="",
            status="Draft",
        )
        for i, (src, trans) in enumerate(zip(raw_segments, translations))
    ]

    return Sermon(
        sermonId=sermonId,
        orgId=orgId,
        title=title,
        sourceType=sourceType,
        sourceRef=sourceRef,
        segments=segments,
        createdBy=creatorUid,
        createdAt=created_at,
        updatedAt=created_at,
    )
