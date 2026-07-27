# Design Ref: §6.2 — 11-code rule catalog. Pure function: no I/O, no Firestore.
# Plan SC: Unit tests cover happy-path, missing-column, duplicate-ID,
# wrong-sermon-ID, empty-review imports.

from __future__ import annotations

from typing import Iterable, Literal, get_args

from .models import (
    Segment,
    SegmentStatus,
    Sermon,
    ValidationReport,
    ValidationRow,
    ValidationSummary,
)

COLUMNS: tuple[str, ...] = (
    "Sermon ID",
    "Segment ID",
    "Segment Order",
    "Original Text",
    "App Translation",
    "Reviewed Translation",
    "Notes",
    "Status",
)

PRE_TRANSLATED_COLUMNS: tuple[str, ...] = (
    "Segment ID",
    "Original Text",
    "Reviewed Translation",
)

_VALID_STATUSES: frozenset[str] = frozenset(get_args(SegmentStatus))
_EXCESSIVE_LENGTH_THRESHOLD = 2000


def is_pre_translated_workbook(headers: Iterable[str] | None) -> bool:
    if headers is None:
        return False
    normalized = [str(h).strip() for h in headers if str(h).strip()]
    return normalized == list(PRE_TRANSLATED_COLUMNS)


def validate_workbook(
    rows: list[dict[str, str]],
    sermon: Sermon,
    *,
    headers: Iterable[str] | None = None,
    template: Literal["full", "pre_translated"] = "full",
) -> ValidationReport:
    """Validate parsed workbook rows against a stored sermon.

    Atomic semantics — `summary.imported` reflects what *would* be written iff
    `summary.errored == 0`. Callers must refuse to write on any error.
    """
    report = ValidationReport()
    report.summary.total = len(rows)

    required_columns = (
        PRE_TRANSLATED_COLUMNS if template == "pre_translated" else COLUMNS
    )
    missing_cols = _missing_columns(rows, headers, required_columns)
    if missing_cols:
        report.rows.append(
            ValidationRow(
                row=0,
                segmentId=None,
                level="error",
                code="MISSING_REQUIRED_COLUMN",
                message=f"Missing required column(s): {', '.join(missing_cols)}",
            )
        )
        report.summary.errored = 1
        return report

    segments_by_id: dict[str, Segment] = {s.segmentId: s for s in sermon.segments}
    seen_ids: dict[str, int] = {}  # segmentId -> first row number where seen
    rows_with_error: set[int] = set()
    rows_with_warn: set[int] = set()

    def _emit(
        row_no: int,
        segment_id: str | None,
        level: str,
        code: str,
        message: str = "",
    ) -> None:
        report.rows.append(
            ValidationRow(
                row=row_no,
                segmentId=segment_id,
                level=level,  # type: ignore[arg-type]
                code=code,  # type: ignore[arg-type]
                message=message,
            )
        )
        if row_no > 0 and level == "error":
            rows_with_error.add(row_no)
        elif row_no > 0 and level == "warn":
            rows_with_warn.add(row_no)

    for idx, row in enumerate(rows, start=2):  # data starts at workbook row 2
        segment_id = (row.get("Segment ID") or "").strip()
        sermon_id_cell = (row.get("Sermon ID") or "").strip()
        original_cell = row.get("Original Text") or ""
        app_cell = row.get("App Translation") or ""
        reviewed_cell = row.get("Reviewed Translation") or ""
        status_cell = (row.get("Status") or "").strip()

        if sermon_id_cell and sermon_id_cell != sermon.sermonId:
            _emit(
                idx,
                segment_id or None,
                "error",
                "WRONG_SERMON_ID",
                f"Row's Sermon ID '{sermon_id_cell}' does not match target "
                f"sermon '{sermon.sermonId}'.",
            )
            continue

        segment = segments_by_id.get(segment_id)
        if segment is None:
            _emit(
                idx,
                segment_id or None,
                "error",
                "UNKNOWN_SEGMENT_ID",
                f"Segment ID '{segment_id}' is not part of this sermon.",
            )
            continue

        if segment_id in seen_ids:
            _emit(
                idx,
                segment_id,
                "error",
                "DUPLICATE_SEGMENT_ID",
                f"Segment ID '{segment_id}' appears in multiple rows "
                f"(row {seen_ids[segment_id]} and row {idx}).",
            )
            continue
        seen_ids[segment_id] = idx

        if original_cell != segment.original:
            _emit(
                idx,
                segment_id,
                "error",
                "ORIGINAL_TEXT_MUTATED",
                "Original Text in this row does not match the stored "
                "original. Did you edit a protected column or shift rows?",
            )
            continue

        if status_cell and status_cell not in _VALID_STATUSES:
            _emit(
                idx,
                segment_id,
                "error",
                "INVALID_STATUS",
                f"Status '{status_cell}' is not one of "
                f"{sorted(_VALID_STATUSES)}.",
            )
            continue

        # Warnings: row would still import; flagged for reviewer.
        warned = False

        if template == "full" and app_cell != segment.appTranslation:
            _emit(
                idx,
                segment_id,
                "warn",
                "APP_TRANSLATION_MUTATED",
                "App Translation differs from stored value — likely "
                "an accidental edit. Not blocking.",
            )
            warned = True

        if not reviewed_cell.strip():
            level = "error" if template == "pre_translated" else "warn"
            _emit(
                idx,
                segment_id,
                level,
                "EMPTY_REVIEW",
                (
                    "Reviewed Translation is required for pre-translated sermons."
                    if template == "pre_translated"
                    else "Reviewed Translation is empty — falling back to "
                    "App Translation on save."
                ),
            )
            warned = True
        elif len(reviewed_cell) > _EXCESSIVE_LENGTH_THRESHOLD:
            _emit(
                idx,
                segment_id,
                "warn",
                "EXCESSIVE_LENGTH",
                f"Reviewed Translation exceeds "
                f"{_EXCESSIVE_LENGTH_THRESHOLD} chars.",
            )
            warned = True

        if not warned:
            _emit(idx, segment_id, "ok", "OK")

    for segment in sermon.segments:
        if segment.segmentId not in seen_ids:
            _emit(
                0,
                segment.segmentId,
                "error",
                "MISSING_SEGMENT",
                f"Sermon segment '{segment.segmentId}' has no row in the "
                "uploaded file. Did you delete a row?",
            )

    workbook_errors = sum(
        1 for r in report.rows if r.row == 0 and r.level == "error"
    )
    report.summary.errored = len(rows_with_error) + workbook_errors
    report.summary.warned = len(rows_with_warn)
    if report.summary.errored == 0:
        report.summary.imported = report.summary.total

    return report


def validate_pre_translated_replacement_workbook(
    rows: list[dict[str, str]],
    *,
    headers: Iterable[str] | None = None,
) -> ValidationReport:
    """Validate a three-column pre-translated workbook as the new source of
    truth for sermon segments.

    Unlike validate_workbook(..., template="pre_translated"), this does not
    compare Segment ID / Original Text against the stored sermon. It is used
    when users intentionally re-segment the Korean sermon in the template.
    """
    report = ValidationReport()
    report.summary.total = len(rows)

    missing_cols = _missing_columns(rows, headers, PRE_TRANSLATED_COLUMNS)
    if missing_cols:
        report.rows.append(
            ValidationRow(
                row=0,
                segmentId=None,
                level="error",
                code="MISSING_REQUIRED_COLUMN",
                message=f"Missing required column(s): {', '.join(missing_cols)}",
            )
        )
        report.summary.errored = 1
        return report

    seen_ids: dict[str, int] = {}
    rows_with_error: set[int] = set()
    rows_with_warn: set[int] = set()

    def _emit(
        row_no: int,
        segment_id: str | None,
        level: str,
        code: str,
        message: str = "",
    ) -> None:
        report.rows.append(
            ValidationRow(
                row=row_no,
                segmentId=segment_id,
                level=level,  # type: ignore[arg-type]
                code=code,  # type: ignore[arg-type]
                message=message,
            )
        )
        if level == "error":
            rows_with_error.add(row_no)
        elif level == "warn":
            rows_with_warn.add(row_no)

    for idx, row in enumerate(rows, start=2):
        segment_id = (row.get("Segment ID") or "").strip()
        original = (row.get("Original Text") or "").strip()
        reviewed = (row.get("Reviewed Translation") or "").strip()
        has_issue = False

        if segment_id and segment_id in seen_ids:
            _emit(
                idx,
                segment_id,
                "error",
                "DUPLICATE_SEGMENT_ID",
                f"Segment ID '{segment_id}' appears in multiple rows "
                f"(row {seen_ids[segment_id]} and row {idx}).",
            )
            has_issue = True
        elif segment_id:
            seen_ids[segment_id] = idx

        if not original:
            _emit(
                idx,
                segment_id or None,
                "error",
                "EMPTY_ORIGINAL",
                "Original Text is required.",
            )
            has_issue = True

        if not reviewed:
            _emit(
                idx,
                segment_id or None,
                "error",
                "EMPTY_REVIEW",
                "Reviewed Translation is required.",
            )
            has_issue = True
        elif len(reviewed) > _EXCESSIVE_LENGTH_THRESHOLD:
            _emit(
                idx,
                segment_id or None,
                "warn",
                "EXCESSIVE_LENGTH",
                f"Reviewed Translation exceeds "
                f"{_EXCESSIVE_LENGTH_THRESHOLD} chars.",
            )
            has_issue = True

        if not has_issue:
            _emit(idx, segment_id, "ok", "OK")

    report.summary.errored = len(rows_with_error)
    report.summary.warned = len(rows_with_warn)
    if report.summary.errored == 0:
        report.summary.imported = report.summary.total
    return report


def _missing_columns(
    rows: list[dict[str, str]],
    headers: Iterable[str] | None,
    required_columns: Iterable[str],
) -> list[str]:
    if headers is not None:
        present = {str(h).strip() for h in headers if h}
    elif rows:
        present = set(rows[0].keys())
    else:
        present = set()
    return [c for c in required_columns if c not in present]
