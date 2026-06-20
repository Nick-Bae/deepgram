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
  SermonApiError,
  ValidationReport,
  ValidationRow,
  isImportValidationError,
} from "../lib/types/sermon";

type Props = {
  orgId: string;
  sermonId: string;
  sermonTitle: string;
  onImportSuccess?: (report: ValidationReport) => void;
};

type ImportOutcome =
  | { kind: "success"; report: ValidationReport }
  | { kind: "failure"; status: number; code: string; report: ValidationReport | null; message: string };

export default function SermonReviewControls({
  orgId,
  sermonId,
  sermonTitle,
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
            Review File
          </h3>
          <p className="text-xs text-slate-500">
            Export to Excel / Google Sheets, edit, and re-upload.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={handleExport}
            disabled={exporting}
            className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {exporting ? "Preparing…" : "Export Review File"}
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
      <p className="text-xs text-red-700">{outcome.message}</p>
    </div>
  );
}

function ReportTable({ report }: { report: ValidationReport | null }) {
  if (!report || report.rows.length === 0) return null;
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
          {report.rows.map((row, i) => (
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
