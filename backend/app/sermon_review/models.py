# Design Ref: §3 Data Model + §6.2 Validation Rule Catalog.

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SegmentStatus = Literal["Draft", "Reviewed", "Skip"]
SourceType = Literal["google_docs", "file_txt", "file_docx", "paste"]
ValidationLevel = Literal["ok", "warn", "error"]

ValidationCode = Literal[
    "OK",
    "WRONG_SERMON_ID",
    "MISSING_REQUIRED_COLUMN",
    "UNKNOWN_SEGMENT_ID",
    "DUPLICATE_SEGMENT_ID",
    "MISSING_SEGMENT",
    "INVALID_STATUS",
    "ORIGINAL_TEXT_MUTATED",
    "APP_TRANSLATION_MUTATED",
    "EMPTY_REVIEW",
    "EXCESSIVE_LENGTH",
]


class Segment(BaseModel):
    segmentId: str
    order: int
    original: str
    appTranslation: str
    reviewedTranslation: str
    notes: str = ""
    status: SegmentStatus = "Draft"


class Sermon(BaseModel):
    sermonId: str
    orgId: str
    title: str
    sourceType: SourceType
    sourceRef: str | None = None
    segments: list[Segment]
    createdBy: str
    createdAt: datetime
    updatedAt: datetime


class ValidationRow(BaseModel):
    row: int  # 1-based row number in the workbook; 0 reserved for workbook-level findings
    segmentId: str | None  # None for workbook-level findings (missing column, etc.)
    level: ValidationLevel
    code: ValidationCode
    message: str = ""


class ValidationSummary(BaseModel):
    total: int = 0  # data rows in the workbook (excluding header)
    imported: int = 0  # rows that would be imported on success
    warned: int = 0
    errored: int = 0


class ValidationReport(BaseModel):
    summary: ValidationSummary = Field(default_factory=ValidationSummary)
    rows: list[ValidationRow] = Field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return self.summary.errored > 0


class SermonNotFoundError(LookupError):
    """Raised by store when a sermon does not exist."""


class SermonConflictError(RuntimeError):
    """Raised by store when an updatedAt precondition fails (concurrent import)."""


class ServiceAlreadyLinkedError(RuntimeError):
    """Raised when linking would replace an existing link without replace=True."""
