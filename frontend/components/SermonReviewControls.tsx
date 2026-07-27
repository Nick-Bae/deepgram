// Design Ref: §5.4 Page UI Checklist — Detail page "Export Review File" /
// "Import Reviewed" controls + per-row validation report panel.

"use client";

import { ChangeEvent, useCallback, useId, useState } from "react";

import { useAuth } from "../lib/authContext";
import {
  exportReviewFile,
  importReviewFile,
  triggerBlobDownload,
} from "../lib/api/sermons";
import {
  ReviewMode,
  SermonApiError,
  ValidationCode,
  ValidationReport,
  ValidationRow,
  isImportValidationError,
} from "../lib/types/sermon";

type Props = {
  orgId: string;
  sermonId: string;
  sermonTitle: string;
  reviewMode?: ReviewMode;
  onImportSuccess?: (report: ValidationReport) => void;
};

type ImportOutcome =
  | { kind: "success"; report: ValidationReport }
  | { kind: "failure"; status: number; code: string; report: ValidationReport | null; message: string };

const VALIDATION_HELP: Record<ValidationCode, string> = {
  OK: "This row is ready.",
  WRONG_SERMON_ID: "Upload the review file exported from this sermon, or export a fresh file and copy your translations into it.",
  MISSING_REQUIRED_COLUMN: "Keep the required header columns exactly as exported.",
  UNKNOWN_SEGMENT_ID: "Fill the Segment ID cell, or leave it blank only in a three-column translation template so the app can generate one.",
  DUPLICATE_SEGMENT_ID: "Make each Segment ID unique.",
  MISSING_SEGMENT: "A row from the exported full review file is missing. Re-export and copy your edits back in.",
  INVALID_STATUS: "Use Draft, Reviewed, or Skip.",
  ORIGINAL_TEXT_MUTATED: "For full review files, do not edit Original Text. For edited segmentation, use the three-column template.",
  APP_TRANSLATION_MUTATED: "Leave App Translation unchanged, or move your edit into Reviewed Translation.",
  EMPTY_ORIGINAL: "Fill Original Text for this row or delete the row.",
  EMPTY_REVIEW: "Fill Reviewed Translation for this row.",
  EXCESSIVE_LENGTH: "Shorten this translation if it was pasted into the wrong row.",
};

export default function SermonReviewControls({
  orgId,
  sermonId,
  sermonTitle,
  reviewMode = "app_assisted",
  onImportSuccess,
}: Props) {
  const { getIdToken } = useAuth();
  const inputId = useId();

  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<ImportOutcome | null>(null);
  const [reportOpen, setReportOpen] = useState(false);

  const handleExport = useCallback(async () => {
    setExportError(null);
    setExporting(true);
    try {
      const idToken = await getIdToken();
      if (!idToken) {
        setExportError("You are signed out. Please sign in again.");
        return;
      }
      const result = await exportReviewFile(orgId, sermonId, { idToken });
      triggerBlobDownload(
        result.blob,
        reviewFilenameFromTitle(sermonTitle, result.filename)
      );
    } catch (err: unknown) {
      if (err instanceof SermonApiError) {
        setExportError(err.message);
      } else if (err instanceof Error) {
        setExportError(err.message);
      } else {
        setExportError("Unknown error.");
      }
    } finally {
      setExporting(false);
    }
  }, [getIdToken, orgId, sermonId, sermonTitle]);

  const handleImport = useCallback(
    async (event: ChangeEvent<HTMLInputElement>) => {
      const file = event.target.files?.[0];
      // Reset the input so the user can re-pick the same file after a failure.
      event.target.value = "";
      if (!file) return;

      setOutcome(null);
      setReportOpen(true);
      setImporting(true);
      try {
        const idToken = await getIdToken();
        if (!idToken) {
          setOutcome({
            kind: "failure",
            status: 401,
            code: "UNAUTHORIZED",
            report: null,
            message: "You are signed out. Please sign in again.",
          });
          return;
        }
        const report = await importReviewFile(orgId, sermonId, file, { idToken });
        setOutcome({ kind: "success", report });
        onImportSuccess?.(report);
      } catch (err: unknown) {
        if (isImportValidationError(err)) {
          setOutcome({
            kind: "failure",
            status: err.status,
            code: err.code,
            report: err.details,
            message: err.message,
          });
        } else if (err instanceof SermonApiError) {
          setOutcome({
            kind: "failure",
            status: err.status,
            code: err.code,
            report: null,
            message: err.message,
          });
        } else if (err instanceof Error) {
          setOutcome({
            kind: "failure",
            status: 0,
            code: "INTERNAL_ERROR",
            report: null,
            message: err.message,
          });
        } else {
          setOutcome({
            kind: "failure",
            status: 0,
            code: "INTERNAL_ERROR",
            report: null,
            message: "Unknown error.",
          });
        }
      } finally {
        setImporting(false);
      }
    },
    [getIdToken, orgId, sermonId, onImportSuccess]
  );

  return (
    <section className="space-y-4 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold text-slate-900">
            {reviewMode === "pre_translated"
              ? "Translation Template"
              : "Review File"}
          </h3>
          <p className="text-xs text-slate-500">
            {reviewMode === "pre_translated"
              ? "Export the three-column template, add translations, and re-upload."
              : "Export to Excel / Google Sheets, edit, and re-upload."}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={handleExport}
            disabled={exporting}
            className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {exporting
              ? "Preparing…"
              : reviewMode === "pre_translated"
                ? "Export Template"
                : "Export Review File"}
          </button>

          <label
            htmlFor={inputId}
            className={
              "inline-flex cursor-pointer items-center rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 " +
              (importing ? "cursor-not-allowed opacity-60" : "")
            }
          >
            {importing ? "Importing…" : "Import Reviewed"}
            <input
              id={inputId}
              type="file"
              accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              className="sr-only"
              onChange={handleImport}
              disabled={importing}
            />
          </label>
        </div>
      </header>

      {exportError && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
          Export failed: {exportError}
        </div>
      )}

      {outcome && <OutcomeBanner outcome={outcome} onToggle={() => setReportOpen((o) => !o)} open={reportOpen} />}

      {outcome?.kind === "failure" && outcome.report && (
        <ValidationSummaryPanel report={outcome.report} />
      )}

      {outcome && reportOpen && (
        <ReportTable report={resolveReport(outcome)} />
      )}
    </section>
  );
}

export function reviewFilenameFromTitle(
  sermonTitle: string,
  fallback = "sermon-review.xlsx"
): string {
  const safeTitle = (sermonTitle || "")
    .trim()
    .replace(/[<>:"/\\|?*\u0000-\u001F]/g, "-")
    .replace(/[.\s]+$/g, "")
    .slice(0, 120);
  return safeTitle ? `${safeTitle}.xlsx` : fallback;
}

function resolveReport(outcome: ImportOutcome): ValidationReport | null {
  return outcome.kind === "success" ? outcome.report : outcome.report;
}

function OutcomeBanner({
  outcome,
  onToggle,
  open,
}: {
  outcome: ImportOutcome;
  onToggle: () => void;
  open: boolean;
}) {
  if (outcome.kind === "success") {
    const { imported, warned } = outcome.report.summary;
    return (
      <div className="flex items-center justify-between rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
        <span>
          ✓ {imported} segment{imported === 1 ? "" : "s"} imported
          {warned > 0 ? ` (${warned} warning${warned === 1 ? "" : "s"})` : ""}.
        </span>
        {outcome.report.rows.length > 0 && (
          <button
            type="button"
            onClick={onToggle}
            className="text-xs font-medium underline"
          >
            {open ? "Hide details" : "Show details"}
          </button>
        )}
      </div>
    );
  }
  const errored = outcome.report?.summary.errored ?? 0;
  const topIssue = outcome.report ? summarizeTopIssue(outcome.report) : "";
  return (
    <div className="space-y-1 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800">
      <div className="flex items-center justify-between">
        <span>
          ✕ Import rejected ({outcome.code}
          {errored > 0 ? ` — ${errored} error${errored === 1 ? "" : "s"}` : ""}
          ).
        </span>
        {outcome.report && outcome.report.rows.length > 0 && (
          <button
            type="button"
            onClick={onToggle}
            className="text-xs font-medium underline"
          >
            {open ? "Hide details" : "Show details"}
          </button>
        )}
      </div>
      {topIssue ? <p className="text-xs font-medium text-red-800">{topIssue}</p> : null}
      <p className="text-xs text-red-700">{outcome.message}</p>
    </div>
  );
}

function ValidationSummaryPanel({ report }: { report: ValidationReport }) {
  const issues = summarizeValidationCodes(report).filter(([code]) => code !== "OK");
  if (!issues.length) return null;
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
      <p className="font-medium">Fix these spreadsheet issues, then upload again.</p>
      <ul className="mt-2 space-y-1">
        {issues.map(([code, count]) => (
          <li key={code}>
            <span className="font-mono text-xs">{code}</span>
            <span> ({count}) - {VALIDATION_HELP[code]}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function summarizeTopIssue(report: ValidationReport): string {
  const issue = summarizeValidationCodes(report).find(([code]) => code !== "OK");
  if (!issue) return "";
  const [code, count] = issue;
  return `${count} row${count === 1 ? "" : "s"} need attention: ${VALIDATION_HELP[code]}`;
}

function summarizeValidationCodes(report: ValidationReport): Array<[ValidationCode, number]> {
  const counts = new Map<ValidationCode, number>();
  for (const row of report.rows) {
    counts.set(row.code, (counts.get(row.code) ?? 0) + 1);
  }
  return Array.from(counts.entries()).sort((a, b) => {
    if (a[0] === "OK") return 1;
    if (b[0] === "OK") return -1;
    return b[1] - a[1];
  });
}

function ReportTable({ report }: { report: ValidationReport | null }) {
  if (!report || report.rows.length === 0) return null;
  const rows =
    report.summary.errored > 0
      ? report.rows.filter((row) => row.level !== "ok")
      : report.rows;
  if (!rows.length) return null;
  return (
    <div className="overflow-hidden rounded-lg border border-slate-200">
      <table className="min-w-full divide-y divide-slate-200 text-sm">
        <thead className="bg-slate-50 text-xs font-semibold uppercase tracking-wide text-slate-600">
          <tr>
            <th className="px-3 py-2 text-left">Row</th>
            <th className="px-3 py-2 text-left">Segment</th>
            <th className="px-3 py-2 text-left">Level</th>
            <th className="px-3 py-2 text-left">Code</th>
            <th className="px-3 py-2 text-left">Message</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 bg-white">
          {rows.map((row, i) => (
            <ReportRow key={`${row.row}-${row.code}-${i}`} row={row} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ReportRow({ row }: { row: ValidationRow }) {
  const color =
    row.level === "error"
      ? "text-red-700"
      : row.level === "warn"
        ? "text-amber-700"
        : "text-emerald-700";
  return (
    <tr>
      <td className="px-3 py-2 text-slate-700">{row.row || "—"}</td>
      <td className="px-3 py-2 text-slate-700">{row.segmentId ?? "—"}</td>
      <td className={"px-3 py-2 font-medium " + color}>{row.level}</td>
      <td className="px-3 py-2 text-slate-700">{row.code}</td>
      <td className="px-3 py-2 text-slate-600">{row.message}</td>
    </tr>
  );
}
