"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/router";

import { useAuth } from "../lib/authContext";
import {
  draftOrgSermon,
  finalizeOrgSermon,
  draftServiceSermon,
  fetchServiceSermon,
  saveServiceSermon,
  publishServiceSermon,
  type SermonDraftSegment,
} from "../lib/backendAuth";

type Props = {
  orgId: string;
  serviceKey?: string;
  services?: Array<{ serviceKey: string; title: string; publishedSermonDate?: string }>;
  onServiceKeyChange?: (serviceKey: string) => void;
};

const PAGE_SIZE_OPTIONS = [25, 50, 100] as const;
const DEFAULT_PAGE_SIZE = 50;
const LANGUAGE_OPTIONS = [
  { value: "ko", label: "Korean" },
  { value: "en", label: "English" },
  { value: "es", label: "Spanish" },
  { value: "zh-cn", label: "Chinese (Simplified)" },
  { value: "zh-tw", label: "Chinese (Traditional)" },
  { value: "ja", label: "Japanese" },
  { value: "fr", label: "French" },
  { value: "de", label: "German" },
  { value: "pt", label: "Portuguese" },
  { value: "it", label: "Italian" },
  { value: "hi", label: "Hindi" },
] as const;

function buildDefaultSermonId(): string {
  const now = new Date();
  const yyyy = now.getFullYear();
  const mm = String(now.getMonth() + 1).padStart(2, "0");
  const dd = String(now.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}-am`;
}

function buildDefaultServiceDate(): string {
  const now = new Date();
  // Default to next Sunday
  const dayOfWeek = now.getDay(); // 0=Sun
  const daysUntilSunday = dayOfWeek === 0 ? 7 : 7 - dayOfWeek;
  const nextSunday = new Date(now);
  nextSunday.setDate(now.getDate() + daysUntilSunday);
  return nextSunday.toISOString().slice(0, 10);
}

export default function SermonPrep({ orgId, serviceKey, services = [], onServiceKeyChange }: Props) {
  const router = useRouter();
  const { getIdToken, logout } = useAuth();
  const [sermonId, setSermonId] = useState(buildDefaultSermonId());
  const [serviceDate, setServiceDate] = useState(buildDefaultServiceDate());
  const [korean, setKorean] = useState("");
  const [threshold, setThreshold] = useState(0.84);
  const [langSrc, setLangSrc] = useState("ko");
  const [langTgt, setLangTgt] = useState("en");
  const [autoSplit, setAutoSplit] = useState(true);
  const [segments, setSegments] = useState<SermonDraftSegment[]>([]);
  const [busyDraft, setBusyDraft] = useState(false);
  const [busySave, setBusySave] = useState(false);
  const [busyPublish, setBusyPublish] = useState(false);
  const [published, setPublished] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [pageSize, setPageSize] = useState<number>(DEFAULT_PAGE_SIZE);
  const [page, setPage] = useState(1);
  const [jumpRow, setJumpRow] = useState("");

  const readyCount = useMemo(() => segments.filter((row) => row.en.trim().length > 0).length, [segments]);
  const targetLanguageLabel = useMemo(
    () => LANGUAGE_OPTIONS.find((lang) => lang.value === langTgt)?.label || "translation",
    [langTgt],
  );
  const selectedService = useMemo(
    () => services.find((svc) => svc.serviceKey === serviceKey) || null,
    [serviceKey, services],
  );
  const allRowsReady = segments.length > 0 && readyCount === segments.length;
  const firstIncompleteRowId = useMemo(
    () => segments.find((row) => !row.en.trim())?.id ?? null,
    [segments],
  );
  const totalPages = useMemo(
    () => Math.max(1, Math.ceil(Math.max(segments.length, 1) / pageSize)),
    [pageSize, segments.length],
  );
  const currentPage = Math.min(page, totalPages);
  const currentPageStart = (currentPage - 1) * pageSize;
  const currentPageEnd = Math.min(currentPageStart + pageSize, segments.length);
  const visibleSegments = useMemo(
    () => segments.slice(currentPageStart, currentPageEnd),
    [currentPageEnd, currentPageStart, segments],
  );

  const statusTone = message?.startsWith("✅")
    ? "text-emerald-700"
    : message?.startsWith("❌")
      ? "text-rose-700"
      : message?.startsWith("⏳")
        ? "text-cyan-700"
        : "text-slate-600";

  const exportPayload = useMemo(
    () => ({
      sermon_id: sermonId.trim(),
      threshold,
      lang_src: langSrc.trim() || "ko",
      lang_tgt: langTgt.trim() || "en",
      segments: segments.map((row, idx) => ({
        id: idx + 1,
        ko: row.ko.trim(),
        en: row.en.trim(),
      })),
    }),
    [langSrc, langTgt, segments, sermonId, threshold],
  );

  useEffect(() => {
    setPage(1);
  }, [segments.length]);

  useEffect(() => {
    setPage((prev) => Math.min(prev, totalPages));
  }, [totalPages]);

  const isSessionExpiredError = useCallback((raw: string): boolean => {
    const msg = (raw || "").toLowerCase();
    return (
      msg.includes("sign in again") ||
      msg.includes("session is invalid") ||
      msg.includes("invalid id token") ||
      msg.includes("invalid_id_token") ||
      msg.includes("auth_required")
    );
  }, []);

  const redirectToLogin = useCallback(async () => {
    const nextPath = router.asPath || "/admin/sermon-prep";
    await logout();
    void router.replace(`/login?next=${encodeURIComponent(nextPath)}`);
  }, [logout, router]);

  useEffect(() => {
    if (!serviceKey || !orgId || !serviceDate.trim()) return;
    let cancelled = false;

    const loadExistingServiceSermon = async () => {
      try {
        const idToken = await getIdToken(true);
        if (!idToken || cancelled) return;

        const doc = await fetchServiceSermon(idToken, orgId, serviceKey, serviceDate.trim());
        if (cancelled) return;

        const pairs = Array.isArray(doc.pairs) ? doc.pairs : [];
        const nextSegments = pairs.map((pair, idx) => ({
          id: idx + 1,
          ko: String(pair?.source || "").trim(),
          en: String(pair?.target || "").trim(),
        })).filter((row) => row.ko || row.en);

        setSegments(nextSegments);
        setThreshold(typeof doc.threshold === "number" ? doc.threshold : 0.84);
        setLangSrc((doc.langSrc || "ko").trim() || "ko");
        setLangTgt((doc.langTgt || "en").trim() || "en");
        setPublished(doc.status === "published");
        setKorean(nextSegments.map((row) => row.ko).join("\n"));
        setMessage(
          nextSegments.length
            ? `✅ Loaded ${doc.status === "published" ? "published" : "saved"} sermon: ${nextSegments.length} rows.`
            : null,
        );
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        if (cancelled) return;
        if (msg.toLowerCase().includes("sermon_not_found")) {
          setSegments([]);
          setKorean("");
          setPublished(false);
          setMessage(null);
          return;
        }
        if (isSessionExpiredError(msg)) {
          setMessage("❌ Session expired. Redirecting to login...");
          await redirectToLogin();
          return;
        }
        setMessage(`❌ ${msg}`);
      }
    };

    void loadExistingServiceSermon();
    return () => {
      cancelled = true;
    };
  }, [getIdToken, isSessionExpiredError, orgId, redirectToLogin, serviceDate, serviceKey]);

  const onDownloadJson = () => {
    if (!segments.length) {
      setMessage("❌ Generate a draft first.");
      return;
    }
    const fileName = `${sermonId.trim() || "sermon"}-final.json`;
    const blob = new Blob([JSON.stringify(exportPayload, null, 2)], {
      type: "application/json;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = fileName;
    link.click();
    URL.revokeObjectURL(url);
  };

  const onGenerateDraft = async () => {
    if (!orgId) {
      setMessage("❌ Select a church first.");
      return;
    }
    if (!korean.trim()) {
      setMessage("❌ Paste sermon text first.");
      return;
    }
    if (serviceKey && !serviceDate.trim()) {
      setMessage("❌ Select a service date first.");
      return;
    }
    if (!serviceKey && !sermonId.trim()) {
      setMessage("❌ Enter sermon_id first.");
      return;
    }

    setBusyDraft(true);
    setPublished(false);
    setMessage("⏳ Generating draft... Larger sermons can take a few minutes.");
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");

      if (serviceKey) {
        // Service-scoped draft
        const drafted = await draftServiceSermon(idToken, orgId, serviceKey, {
          service_date: serviceDate.trim(),
          korean,
          auto_split: autoSplit,
          threshold,
          lang_src: langSrc,
          lang_tgt: langTgt,
        });
        setSegments(drafted.segments || []);
        setMessage(`✅ Draft ready: ${drafted.segments.length} rows for ${serviceKey} on ${serviceDate}.`);
      } else {
        // Legacy org-level draft
        const drafted = await draftOrgSermon(idToken, orgId, {
          sermon_id: sermonId.trim(),
          korean,
          auto_split: autoSplit,
          threshold,
          lang_src: langSrc,
          lang_tgt: langTgt,
        });
        setSermonId(drafted.sermon_id);
        setThreshold(drafted.threshold);
        setLangSrc(drafted.lang_src);
        setLangTgt(drafted.lang_tgt);
        setSegments(drafted.segments || []);
        setMessage(`✅ Draft ready: ${drafted.segments.length} rows.`);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      if (isSessionExpiredError(msg)) {
        setMessage("❌ Session expired. Redirecting to login...");
        await redirectToLogin();
        return;
      }
      setMessage(`❌ ${msg}`);
    } finally {
      setBusyDraft(false);
    }
  };

  const updateEnglish = (id: number, next: string) => {
    setSegments((prev) => prev.map((row) => (row.id === id ? { ...row, en: next } : row)));
  };

  const goToPage = (nextPage: number) => {
    setPage(Math.min(Math.max(nextPage, 1), totalPages));
  };

  const goToRow = () => {
    const parsed = Number(jumpRow);
    if (!Number.isFinite(parsed)) return;
    const normalized = Math.floor(parsed);
    if (normalized < 1 || normalized > segments.length) return;
    goToPage(Math.ceil(normalized / pageSize));
  };

  const goToFirstIncomplete = () => {
    if (!firstIncompleteRowId) return;
    goToPage(Math.ceil(firstIncompleteRowId / pageSize));
  };

  const rangeLabel =
    segments.length > 0 ? `Rows ${currentPageStart + 1}-${currentPageEnd} of ${segments.length}` : "No rows yet";

  const onSaveDraft = async () => {
    if (!orgId || !serviceKey || !serviceDate.trim() || !segments.length) return;
    setBusySave(true);
    setMessage("⏳ Saving draft...");
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");
      await saveServiceSermon(idToken, orgId, serviceKey, serviceDate.trim(), {
        segments,
        threshold,
        lang_src: langSrc,
        lang_tgt: langTgt,
      });
      setMessage(`✅ Draft saved (${segments.length} rows).`);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setMessage(`❌ ${msg}`);
    } finally {
      setBusySave(false);
    }
  };

  const onPublish = async () => {
    if (!orgId || !serviceKey || !serviceDate.trim() || !segments.length) return;
    if (segments.some((row) => !row.en.trim())) {
      setMessage("❌ Every translation row must be filled before publishing.");
      return;
    }
    setBusyPublish(true);
    setMessage("⏳ Saving and publishing sermon...");
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");
      // Save latest edits first, then publish
      await saveServiceSermon(idToken, orgId, serviceKey, serviceDate.trim(), {
        segments,
        threshold,
        lang_src: langSrc,
        lang_tgt: langTgt,
      });
      await publishServiceSermon(idToken, orgId, serviceKey, serviceDate.trim());
      setPublished(true);
      setMessage(`✅ Published for ${serviceKey} on ${serviceDate}. Sermon is now active for this service.`);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      if (isSessionExpiredError(msg)) {
        setMessage("❌ Session expired. Redirecting to login...");
        await redirectToLogin();
        return;
      }
      setMessage(`❌ ${msg}`);
    } finally {
      setBusyPublish(false);
    }
  };

  const onSaveFinal = async () => {
    if (!orgId) {
      setMessage("❌ Select a church first.");
      return;
    }
    if (!sermonId.trim()) {
      setMessage("❌ Enter sermon_id first.");
      return;
    }
    if (!segments.length) {
      setMessage("❌ Generate a draft first.");
      return;
    }
    if (segments.some((row) => !row.en.trim())) {
      setMessage("❌ Every translation row must be filled before saving.");
      return;
    }

    setBusySave(true);
    setMessage("⏳ Saving final sermon...");
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");

      const finalized = await finalizeOrgSermon(idToken, orgId, {
        sermon_id: sermonId.trim(),
        threshold,
        lang_src: langSrc,
        lang_tgt: langTgt,
        segments,
      });

      setThreshold(finalized.threshold);
      setSegments(finalized.segments || []);
      setMessage(`✅ Saved final JSON: ${finalized.sermon_id} (${finalized.loaded} rows loaded).`);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      if (isSessionExpiredError(msg)) {
        setMessage("❌ Session expired. Redirecting to login...");
        await redirectToLogin();
        return;
      }
      setMessage(`❌ ${msg}`);
    } finally {
      setBusySave(false);
    }
  };

  return (
    <section className="rounded-[32px] border border-slate-200 bg-white p-6 shadow-[0_35px_120px_rgba(3,7,18,0.25)] backdrop-blur">
      <div className="space-y-1">
        <p className="text-xs uppercase tracking-[0.35em] text-slate-500">Sermon Prep</p>
        <h2 className="text-xl font-semibold text-slate-900">Before Sunday</h2>
        <p className="text-sm text-slate-600">
          5-10 minute workflow: paste sermon text, generate a translation draft, polish it, and save final.
        </p>
      </div>

      {serviceKey ? (
        <div className="mt-5 rounded-2xl border border-slate-200 bg-slate-50 p-4">
          <p className="mb-2 text-sm font-medium text-slate-700">Step 1: Choose service and date</p>
          <div className="grid gap-4 md:grid-cols-[1fr_1fr_1fr]">
            <label className="space-y-1 text-sm text-slate-700">
              <span className="font-medium text-slate-700">Service</span>
              <select
                value={serviceKey}
                onChange={(e) => {
                  onServiceKeyChange?.(e.target.value);
                  setPublished(false);
                }}
                disabled={!onServiceKeyChange || services.length === 0}
                className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-sky-500 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60"
              >
                {services.length ? (
                  services.map((svc) => (
                    <option key={svc.serviceKey} value={svc.serviceKey}>
                      {svc.title || svc.serviceKey}
                      {svc.publishedSermonDate ? ` - published ${svc.publishedSermonDate}` : ""}
                    </option>
                  ))
                ) : (
                  <option value={serviceKey}>{selectedService?.title || serviceKey}</option>
                )}
              </select>
            </label>

            <label className="space-y-1 text-sm text-slate-700">
              <span className="font-medium text-slate-700">Service Date</span>
              <input
                type="date"
                value={serviceDate}
                onChange={(e) => { setServiceDate(e.target.value); setPublished(false); }}
                className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-sky-500 focus:outline-none"
              />
            </label>

            <label className="space-y-1 text-sm text-slate-700">
              <span className="font-medium text-slate-700">Rows ready</span>
              <div className="rounded-xl border border-white/15 bg-[#050b16] px-3 py-2 text-sm text-white">
                {readyCount}/{segments.length || 0} translated rows
              </div>
            </label>
          </div>
          <p className="mt-2 text-xs text-slate-500">
            Sermon for: {selectedService?.title || serviceKey}
          </p>
        </div>
      ) : (
        <div className="mt-5 grid gap-4 md:grid-cols-2">
          <label className="space-y-1 text-sm text-slate-700">
            <span className="font-medium text-slate-700">Step 1: Sermon ID</span>
            <input
              value={sermonId}
              onChange={(e) => setSermonId(e.target.value)}
              className="w-full rounded-xl border border-white/15 bg-[#050b16] px-3 py-2 text-sm text-white placeholder-white/35 focus:border-[#22d3ee] focus:outline-none"
              placeholder="2026-03-08-am"
            />
          </label>

          <label className="space-y-1 text-sm text-slate-700">
            <span className="font-medium text-slate-700">Rows ready</span>
            <div className="rounded-xl border border-white/15 bg-[#050b16] px-3 py-2 text-sm text-white">
              {readyCount}/{segments.length || 0} translated rows
            </div>
          </label>
        </div>
      )}

      <label className="mt-4 block space-y-2 text-sm text-slate-700">
        <span className="font-medium text-slate-700">Step 2: Sermon text</span>
        <textarea
          value={korean}
          onChange={(e) => setKorean(e.target.value)}
          rows={8}
          className="w-full rounded-xl border border-white/15 bg-[#050b16] px-3 py-2 text-sm text-white placeholder-white/35 focus:border-[#22d3ee] focus:outline-none"
          placeholder="Paste sermon text here..."
        />
      </label>

      <details className="mt-3 rounded-2xl border border-slate-200 bg-slate-100 p-3 text-sm text-slate-600">
        <summary className="cursor-pointer font-medium text-slate-700">Advanced options</summary>
        <div className="mt-3 grid gap-3 md:grid-cols-3">
          <label className="space-y-1">
            <span>Threshold</span>
            <input
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={threshold}
              onChange={(e) => setThreshold(Number(e.target.value) || 0.8)}
              className="w-full rounded-lg border border-white/15 bg-[#050b16] px-3 py-2 text-sm text-white focus:border-[#22d3ee] focus:outline-none"
            />
          </label>
          <label className="space-y-1">
            <span>Source language</span>
            <select
              value={langSrc}
              onChange={(e) => setLangSrc(e.target.value)}
              className="w-full rounded-lg border border-white/15 bg-[#050b16] px-3 py-2 text-sm text-white focus:border-[#22d3ee] focus:outline-none"
            >
              {LANGUAGE_OPTIONS.map((lang) => (
                <option key={lang.value} value={lang.value}>
                  {lang.label}
                </option>
              ))}
            </select>
          </label>
          <label className="space-y-1">
            <span>Target language</span>
            <select
              value={langTgt}
              onChange={(e) => setLangTgt(e.target.value)}
              className="w-full rounded-lg border border-white/15 bg-[#050b16] px-3 py-2 text-sm text-white focus:border-[#22d3ee] focus:outline-none"
            >
              {LANGUAGE_OPTIONS.map((lang) => (
                <option key={lang.value} value={lang.value}>
                  {lang.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label className="mt-3 inline-flex items-center gap-2">
          <input
            type="checkbox"
            checked={autoSplit}
            onChange={(e) => setAutoSplit(e.target.checked)}
          />
          Auto split by sentence punctuation (ignore soft line breaks)
        </label>
      </details>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={onGenerateDraft}
          disabled={busyDraft || busySave || busyPublish}
          className="inline-flex items-center justify-center gap-2 rounded-2xl bg-[#22d3ee] px-5 py-2.5 text-sm font-semibold text-[#041018] shadow-[0_15px_45px_rgba(34,211,238,0.35)] transition hover:bg-[#00ffff] disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busyDraft ? "Generating..." : "Step 3: Generate draft"}
        </button>

        {serviceKey ? (
          <>
            <button
              type="button"
              onClick={onSaveDraft}
              disabled={busyDraft || busySave || busyPublish || !segments.length}
              className="inline-flex items-center justify-center gap-2 rounded-2xl border border-slate-400 px-5 py-2.5 text-sm font-semibold text-slate-700 transition hover:border-cyan-400 hover:text-cyan-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busySave ? "Saving..." : "Step 4: Save Draft"}
            </button>
            <button
              type="button"
              onClick={onPublish}
              disabled={busyDraft || busySave || busyPublish || !segments.length || !allRowsReady || published}
              className="inline-flex items-center justify-center gap-2 rounded-2xl bg-emerald-500 px-5 py-2.5 text-sm font-semibold text-[#041018] shadow-[0_15px_45px_rgba(16,185,129,0.28)] transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busyPublish ? "Publishing..." : published ? "✓ Published" : "Step 5: Publish for Service"}
            </button>
            {published ? null : (
              <span className="text-xs text-amber-600">
                Publishing freezes this sermon for {serviceKey} on {serviceDate}.
              </span>
            )}
          </>
        ) : (
          <button
            type="button"
            onClick={onSaveFinal}
            disabled={busyDraft || busySave || !segments.length || !allRowsReady}
            className="inline-flex items-center justify-center gap-2 rounded-2xl bg-emerald-500 px-5 py-2.5 text-sm font-semibold text-[#041018] shadow-[0_15px_45px_rgba(16,185,129,0.28)] transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busySave ? "Saving..." : "Step 4: Save as final"}
          </button>
        )}

        <button
          type="button"
          onClick={onDownloadJson}
          disabled={!segments.length}
          className="inline-flex items-center justify-center gap-2 rounded-2xl border border-slate-300 px-5 py-2.5 text-sm font-semibold text-slate-700 transition hover:border-cyan-400 hover:text-cyan-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Download JSON
        </button>
      </div>

      {message ? (
        <p className={`mt-3 text-sm ${statusTone}`} role="status" aria-live="polite">
          {message}
        </p>
      ) : null}

      {segments.length ? (
        <div className="mt-4 space-y-3">
          <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-slate-200 bg-slate-100 px-4 py-3 text-sm text-slate-700">
            <span className="font-medium text-slate-900">{rangeLabel}</span>
            <span className="text-slate-500">Page {currentPage} of {totalPages}</span>
            <label className="inline-flex items-center gap-2">
              <span>Rows/page</span>
              <select
                value={pageSize}
                onChange={(e) => {
                  setPageSize(Number(e.target.value) || DEFAULT_PAGE_SIZE);
                  setPage(1);
                }}
                className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm text-slate-900 focus:border-sky-500 focus:outline-none"
              >
                {PAGE_SIZE_OPTIONS.map((size) => (
                  <option key={size} value={size}>
                    {size}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              onClick={() => goToPage(currentPage - 1)}
              disabled={currentPage <= 1}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 font-medium text-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Prev
            </button>
            <button
              type="button"
              onClick={() => goToPage(currentPage + 1)}
              disabled={currentPage >= totalPages}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 font-medium text-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Next
            </button>
            <label className="inline-flex items-center gap-2">
              <span>Jump to row</span>
              <input
                value={jumpRow}
                onChange={(e) => setJumpRow(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    goToRow();
                  }
                }}
                inputMode="numeric"
                placeholder="e.g. 320"
                className="w-24 rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm text-slate-900 focus:border-sky-500 focus:outline-none"
              />
            </label>
            <button
              type="button"
              onClick={goToRow}
              disabled={!segments.length}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 font-medium text-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Go
            </button>
            <button
              type="button"
              onClick={goToFirstIncomplete}
              disabled={!firstIncompleteRowId}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 font-medium text-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              First incomplete
            </button>
          </div>

          <div className="overflow-x-auto rounded-2xl border border-white/10">
            <div className="min-w-[900px]">
              <div className="grid grid-cols-[56px_minmax(0,1fr)_minmax(0,1fr)] bg-slate-100 text-left text-sm text-slate-700">
                <div className="border-b border-white/10 px-3 py-2 font-semibold">#</div>
                <div className="border-b border-white/10 px-3 py-2 font-semibold">Source text</div>
                <div className="border-b border-white/10 px-3 py-2 font-semibold">{targetLanguageLabel} translation</div>
              </div>
              <div className="divide-y divide-white/10">
                {visibleSegments.map((row) => (
                  <div
                    key={row.id}
                    className="grid grid-cols-[56px_minmax(0,1fr)_minmax(0,1fr)] items-start text-left text-sm text-slate-800"
                  >
                    <div className="px-3 py-3 text-slate-500">{row.id}</div>
                    <div className="min-w-0 px-3 py-3">
                      <div className="min-h-[82px] whitespace-pre-wrap break-words rounded-xl border border-white/10 bg-black/35 px-3 py-2 leading-relaxed text-white/90">
                        {row.ko}
                      </div>
                    </div>
                    <div className="min-w-0 px-3 py-3">
                      <textarea
                        value={row.en}
                        onChange={(e) => updateEnglish(row.id, e.target.value)}
                        rows={3}
                        className="min-h-[82px] w-full resize-y rounded-xl border border-white/15 bg-[#050b16] px-3 py-2 text-sm text-white focus:border-[#22d3ee] focus:outline-none"
                      />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {segments.length ? (
        <div className="mt-4 rounded-2xl border border-white/10 bg-black/25 p-4">
          <p className="text-xs uppercase tracking-[0.35em] text-slate-500">Final JSON preview</p>
          <pre className="mt-3 max-h-64 overflow-auto rounded-xl bg-black/55 p-3 text-xs text-[#9efcff]">
            {JSON.stringify(exportPayload, null, 2)}
          </pre>
          {!allRowsReady ? (
            <p className="mt-2 text-sm text-amber-200">Fill every translation row before saving final.</p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
