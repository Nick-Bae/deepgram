// Design Ref: §3.1 Data Model + §6.1 Error Codes.
// Mirrors backend/app/sermon_review/models.py (Pydantic). Keep in sync when
// the backend models change.

export type SegmentStatus = "Draft" | "Reviewed" | "Skip";

export type SourceType = "google_docs" | "file_txt" | "file_docx" | "paste";
export type ReviewMode = "app_assisted" | "pre_translated";

export type ValidationLevel = "ok" | "warn" | "error";

export type ValidationCode =
  | "OK"
  | "WRONG_SERMON_ID"
  | "MISSING_REQUIRED_COLUMN"
  | "UNKNOWN_SEGMENT_ID"
  | "DUPLICATE_SEGMENT_ID"
  | "MISSING_SEGMENT"
  | "INVALID_STATUS"
  | "ORIGINAL_TEXT_MUTATED"
  | "APP_TRANSLATION_MUTATED"
  | "EMPTY_ORIGINAL"
  | "EMPTY_REVIEW"
  | "EXCESSIVE_LENGTH";

export type ApiErrorCode =
  | "INVALID_SOURCE"
  | "INGEST_FAILED"
  | "IMPORT_VALIDATION_FAILED"
  | "UNAUTHORIZED"
  | "FORBIDDEN"
  | "SERMON_NOT_FOUND"
  | "SERMON_MODIFIED_CONCURRENTLY"
  | "SERVICE_ALREADY_LINKED"
  | "PAYLOAD_TOO_LARGE"
  | "UNSUPPORTED_MEDIA_TYPE"
  | "GOOGLE_OAUTH_NOT_CONFIGURED"
  | "GOOGLE_RATE_LIMITED"
  | "SEGMENT_LIMIT_EXCEEDED"
  | "INTERNAL_ERROR";

export type Segment = {
  segmentId: string;
  order: number;
  original: string;
  appTranslation: string;
  reviewedTranslation: string;
  notes: string;
  status: SegmentStatus;
};

export type Sermon = {
  sermonId: string;
  orgId: string;
  title: string;
  sourceType: SourceType;
  reviewMode: ReviewMode;
  sourceRef: string | null;
  segments: Segment[];
  createdBy: string;
  createdAt: string; // ISO-8601
  updatedAt: string; // ISO-8601 — used as precondition for concurrent imports
};

export type SermonSummary = {
  sermonId: string;
  title: string;
  sourceType: SourceType;
  reviewMode: ReviewMode;
  segmentCount: number;
  reviewedCount: number;
  updatedAt: string; // ISO-8601
};

export type IngestResult = {
  sermonId: string;
  title: string;
  reviewMode: ReviewMode;
  segmentCount: number;
};

export type ValidationRow = {
  row: number;
  segmentId: string | null;
  level: ValidationLevel;
  code: ValidationCode;
  message: string;
};

export type ValidationSummary = {
  total: number;
  imported: number;
  warned: number;
  errored: number;
};

export type ValidationReport = {
  summary: ValidationSummary;
  rows: ValidationRow[];
};

export type LinkResult = {
  orgId: string;
  serviceKey: string;
  linkedSermonId: string | null;
};

export type ApiErrorPayload = {
  code: ApiErrorCode | string;
  message: string;
  details?: unknown;
};

export class SermonApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details?: unknown;

  constructor(payload: ApiErrorPayload, status: number) {
    super(payload.message || payload.code || "Sermon API error");
    this.code = payload.code || "INTERNAL_ERROR";
    this.status = status;
    this.details = payload.details;
    this.name = "SermonApiError";
  }
}

export function isImportValidationError(
  err: unknown
): err is SermonApiError & { details: ValidationReport } {
  return (
    err instanceof SermonApiError &&
    err.code === "IMPORT_VALIDATION_FAILED" &&
    typeof err.details === "object" &&
    err.details !== null &&
    "rows" in err.details
  );
}
