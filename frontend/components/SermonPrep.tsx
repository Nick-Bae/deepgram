"use client";

import { useMemo, useState } from "react";

import { useAuth } from "../lib/authContext";
import { draftOrgSermon, finalizeOrgSermon, type SermonDraftSegment } from "../lib/backendAuth";

type Props = {
  orgId: string;
};

function buildDefaultSermonId(): string {
  const now = new Date();
  const yyyy = now.getFullYear();
  const mm = String(now.getMonth() + 1).padStart(2, "0");
  const dd = String(now.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}-am`;
}

export default function SermonPrep({ orgId }: Props) {
  const { getIdToken } = useAuth();
  const [sermonId, setSermonId] = useState(buildDefaultSermonId());
  const [korean, setKorean] = useState("");
  const [threshold, setThreshold] = useState(0.8);
  const [langSrc, setLangSrc] = useState("ko");
  const [langTgt, setLangTgt] = useState("en");
  const [autoSplit, setAutoSplit] = useState(true);
  const [segments, setSegments] = useState<SermonDraftSegment[]>([]);
  const [busyDraft, setBusyDraft] = useState(false);
  const [busySave, setBusySave] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const readyCount = useMemo(() => segments.filter((row) => row.en.trim().length > 0).length, [segments]);
  const allRowsReady = segments.length > 0 && readyCount === segments.length;

  const statusTone = message?.startsWith("✅")
    ? "text-emerald-200"
    : message?.startsWith("❌")
      ? "text-rose-200"
      : message?.startsWith("⏳")
        ? "text-cyan-200"
        : "text-white/70";

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
    if (!sermonId.trim()) {
      setMessage("❌ Enter sermon_id first.");
      return;
    }
    if (!korean.trim()) {
      setMessage("❌ Paste Korean sermon text first.");
      return;
    }

    setBusyDraft(true);
    setMessage("⏳ Generating draft...");
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");

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
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setMessage(`❌ ${msg}`);
    } finally {
      setBusyDraft(false);
    }
  };

  const updateEnglish = (id: number, next: string) => {
    setSegments((prev) => prev.map((row) => (row.id === id ? { ...row, en: next } : row)));
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
      setMessage("❌ Every English row must be filled before saving.");
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
      setMessage(`❌ ${msg}`);
    } finally {
      setBusySave(false);
    }
  };

  return (
    <section className="rounded-[32px] border border-white/10 bg-white/5 p-6 shadow-[0_35px_120px_rgba(3,7,18,0.55)] backdrop-blur">
      <div className="space-y-1">
        <p className="text-xs uppercase tracking-[0.35em] text-white/60">Sermon Prep</p>
        <h2 className="text-xl font-semibold text-white">Before Sunday (KO → EN)</h2>
        <p className="text-sm text-white/70">
          5-10 minute workflow: paste Korean, generate draft, polish English, save final.
        </p>
      </div>

      <div className="mt-5 grid gap-4 md:grid-cols-2">
        <label className="space-y-1 text-sm text-white/80">
          <span className="font-medium">Step 1: Sermon ID</span>
          <input
            value={sermonId}
            onChange={(e) => setSermonId(e.target.value)}
            className="w-full rounded-xl border border-white/15 bg-[#050b16] px-3 py-2 text-sm text-white placeholder-white/35 focus:border-[#22d3ee] focus:outline-none"
            placeholder="2026-03-08-am"
          />
        </label>

        <label className="space-y-1 text-sm text-white/80">
          <span className="font-medium">Rows ready</span>
          <div className="rounded-xl border border-white/15 bg-[#050b16] px-3 py-2 text-sm text-white">
            {readyCount}/{segments.length || 0} EN rows
          </div>
        </label>
      </div>

      <label className="mt-4 block space-y-2 text-sm text-white/80">
        <span className="font-medium">Step 2: Korean sermon text (paste only Korean)</span>
        <textarea
          value={korean}
          onChange={(e) => setKorean(e.target.value)}
          rows={8}
          className="w-full rounded-xl border border-white/15 bg-[#050b16] px-3 py-2 text-sm text-white placeholder-white/35 focus:border-[#22d3ee] focus:outline-none"
          placeholder="오늘 본문은 ..."
        />
      </label>

      <details className="mt-3 rounded-2xl border border-white/10 bg-black/20 p-3 text-sm text-white/70">
        <summary className="cursor-pointer font-medium text-white/80">Advanced options</summary>
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
            <span>Source lang</span>
            <input
              value={langSrc}
              onChange={(e) => setLangSrc(e.target.value)}
              className="w-full rounded-lg border border-white/15 bg-[#050b16] px-3 py-2 text-sm text-white focus:border-[#22d3ee] focus:outline-none"
            />
          </label>
          <label className="space-y-1">
            <span>Target lang</span>
            <input
              value={langTgt}
              onChange={(e) => setLangTgt(e.target.value)}
              className="w-full rounded-lg border border-white/15 bg-[#050b16] px-3 py-2 text-sm text-white focus:border-[#22d3ee] focus:outline-none"
            />
          </label>
        </div>
        <label className="mt-3 inline-flex items-center gap-2">
          <input
            type="checkbox"
            checked={autoSplit}
            onChange={(e) => setAutoSplit(e.target.checked)}
          />
          Auto split by sentence punctuation
        </label>
      </details>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={onGenerateDraft}
          disabled={busyDraft || busySave}
          className="inline-flex items-center justify-center gap-2 rounded-2xl bg-[#22d3ee] px-5 py-2.5 text-sm font-semibold text-[#041018] shadow-[0_15px_45px_rgba(34,211,238,0.35)] transition hover:bg-[#00ffff] disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busyDraft ? "Generating..." : "Step 3: Generate draft"}
        </button>

        <button
          type="button"
          onClick={onSaveFinal}
          disabled={busyDraft || busySave || !segments.length || !allRowsReady}
          className="inline-flex items-center justify-center gap-2 rounded-2xl bg-emerald-500 px-5 py-2.5 text-sm font-semibold text-[#041018] shadow-[0_15px_45px_rgba(16,185,129,0.28)] transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busySave ? "Saving..." : "Step 4: Save as final"}
        </button>

        <button
          type="button"
          onClick={onDownloadJson}
          disabled={!segments.length}
          className="inline-flex items-center justify-center gap-2 rounded-2xl border border-white/20 px-5 py-2.5 text-sm font-semibold text-white transition hover:border-cyan-200/70 hover:text-cyan-100 disabled:cursor-not-allowed disabled:opacity-40"
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
        <div className="mt-4 overflow-x-auto rounded-2xl border border-white/10">
          <table className="min-w-full border-collapse text-left text-sm text-white/90">
            <thead className="bg-white/10 text-white/80">
              <tr>
                <th className="w-16 border-b border-white/10 px-3 py-2">#</th>
                <th className="w-1/2 border-b border-white/10 px-3 py-2">Korean (read-only)</th>
                <th className="w-1/2 border-b border-white/10 px-3 py-2">English (editable)</th>
              </tr>
            </thead>
            <tbody>
              {segments.map((row) => (
                <tr key={row.id} className="align-top">
                  <td className="border-b border-white/10 px-3 py-3 text-white/60">{row.id}</td>
                  <td className="border-b border-white/10 px-3 py-3">
                    <div className="min-h-[82px] rounded-xl border border-white/10 bg-black/35 px-3 py-2 leading-relaxed text-white/90">
                      {row.ko}
                    </div>
                  </td>
                  <td className="border-b border-white/10 px-3 py-3">
                    <textarea
                      value={row.en}
                      onChange={(e) => updateEnglish(row.id, e.target.value)}
                      rows={3}
                      className="w-full resize-y rounded-xl border border-white/15 bg-[#050b16] px-3 py-2 text-sm text-white focus:border-[#22d3ee] focus:outline-none"
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {segments.length ? (
        <div className="mt-4 rounded-2xl border border-white/10 bg-black/25 p-4">
          <p className="text-xs uppercase tracking-[0.35em] text-white/55">Final JSON preview</p>
          <pre className="mt-3 max-h-64 overflow-auto rounded-xl bg-black/55 p-3 text-xs text-[#9efcff]">
            {JSON.stringify(exportPayload, null, 2)}
          </pre>
          {!allRowsReady ? (
            <p className="mt-2 text-sm text-amber-200">Fill every English row before saving final.</p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
