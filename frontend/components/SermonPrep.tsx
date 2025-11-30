"use client";

import { useMemo, useState } from "react";
import { API_URL } from "../utils/urls";

type Row = {
  id: number;
  source: string;
  target: string;
  status: "idle" | "loading" | "done" | "error";
  error?: string;
};

const SENTENCE_SPLIT_RE = /(?<=[.!?。？！…])\s+|\n+/;

function splitLines(input: string): string[] {
  return (input || "")
    .split(SENTENCE_SPLIT_RE)
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function SermonPrep() {
  const [raw, setRaw] = useState("");
  const [rows, setRows] = useState<Row[]>([]);
  const [threshold, setThreshold] = useState(0.84);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const completed = useMemo(
    () => rows.filter((r) => r.status === "done").length,
    [rows]
  );

  const kickoff = async () => {
    const sentences = splitLines(raw);
    if (!sentences.length) {
      setMessage("❌ Paste Korean sentences first");
      return;
    }
    setMessage("⏳ Translating…");
    setBusy(true);

    // seed rows
    setRows(
      sentences.map((s, i) => ({
        id: i + 1,
        source: s,
        target: "",
        status: "loading",
      }))
    );

    // translate sequentially for clarity / lower API pressure
    const nextRows: Row[] = [];
    for (let i = 0; i < sentences.length; i++) {
      const source = sentences[i];
      let target = "";
      let status: Row["status"] = "done";
      let error: string | undefined;
      try {
        const res = await fetch(`${API_URL}/api/translate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            text: source,
            source: "ko",
            target: "en",
            final: true,
          }),
        });
        const j = await res.json().catch(() => ({}));
        if (!res.ok || !j.translated) {
          throw new Error(j.detail || res.statusText || "translate failed");
        }
        target = j.translated;
      } catch (err: unknown) {
        status = "error";
        error = err instanceof Error ? err.message : String(err);
      }
      nextRows.push({
        id: i + 1,
        source,
        target,
        status,
        error,
      });
      setRows((prev) => {
        const clone = [...prev];
        clone[i] = nextRows[i];
        return clone;
      });
    }
    setBusy(false);
    setMessage("✅ Finished. Review and edit before uploading.");
  };

  const updateTarget = (id: number, value: string) => {
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, target: value } : r)));
  };

  const upload = async () => {
    const pairs = rows
      .filter((r) => r.target.trim())
      .map((r) => ({ source: r.source.trim(), target: r.target.trim() }));

    if (!pairs.length) {
      setMessage("❌ Nothing to upload");
      return;
    }

    setMessage("⏳ Uploading to pre-script buffer…");
    try {
      const res = await fetch(`${API_URL}/api/script/upload`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ payload: { pairs }, cfg: { threshold } }),
      });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = j && j.detail ? JSON.stringify(j.detail) : res.statusText;
        throw new Error(detail || "Upload failed");
      }
      setMessage(`✅ Uploaded ${j.loaded} pairs @ threshold ${j.threshold}`);
    } catch (err: unknown) {
      setMessage(`❌ ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const download = () => {
    const payload = rows.map(({ source, target }) => ({ source, target }));
    const blob = new Blob([JSON.stringify({ pairs: payload }, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "sermon-prep.json";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <section className="rounded-[32px] border border-white/10 bg-white/5 p-6 shadow-[0_35px_120px_rgba(3,7,18,0.45)] backdrop-blur">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <p className="text-xs uppercase tracking-[0.35em] text-white/60">Prep</p>
          <h2 className="text-xl font-semibold text-white">Sermon pre-translation</h2>
          <p className="text-sm text-white/70">
            Paste Korean text, auto-translate per sentence, edit, then upload to the hybrid buffer.
          </p>
        </div>
        <div className="text-sm text-white/70">
          {completed}/{rows.length} translated
        </div>
      </div>

      <div className="mt-6 space-y-4">
        <textarea
          value={raw}
          onChange={(e) => setRaw(e.target.value)}
          placeholder="Paste Korean sermon paragraphs here…"
          rows={6}
          className="w-full rounded-2xl border border-white/10 bg-[#050b16] px-4 py-3 text-sm text-white placeholder-white/40 focus:border-[#22d3ee] focus:outline-none focus:ring-2 focus:ring-[#22d3ee]/40"
        />

        <div className="flex flex-wrap gap-3 text-sm">
          <button
            type="button"
            onClick={kickoff}
            disabled={busy}
            className={`inline-flex items-center justify-center rounded-2xl px-4 py-2.5 font-semibold transition ${
              busy
                ? "bg-white/20 text-white/60 cursor-not-allowed"
                : "bg-[#22d3ee] text-[#041018] shadow-[0_12px_35px_rgba(34,211,238,0.35)] hover:bg-[#00ffff]"
            }`}
          >
            {busy ? "Translating…" : "Translate & split"}
          </button>

          <div className="inline-flex items-center gap-2 rounded-2xl border border-white/15 px-3 py-2 text-white/80">
            <label className="text-xs uppercase tracking-[0.2em] text-white/60">
              Match threshold
            </label>
            <input
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={threshold}
              onChange={(e) => setThreshold(Number(e.target.value) || 0.84)}
              className="w-20 rounded-lg border border-white/20 bg-black/40 px-2 py-1 text-sm text-white focus:border-[#22d3ee] focus:outline-none"
            />
          </div>

          <button
            type="button"
            onClick={upload}
            className="inline-flex items-center justify-center rounded-2xl border border-emerald-300/40 px-4 py-2.5 text-sm font-semibold text-emerald-100 transition hover:border-emerald-200 hover:text-emerald-50"
          >
            Upload to buffer
          </button>

          <button
            type="button"
            onClick={download}
            className="inline-flex items-center justify-center rounded-2xl border border-white/20 px-4 py-2.5 text-sm font-semibold text-white transition hover:border-white/40"
          >
            Download JSON
          </button>
        </div>

        {message && <div className="text-sm text-white/80">{message}</div>}

        {rows.length > 0 && (
      <div className="mt-4 max-h-[520px] overflow-y-auto pr-1 space-y-3">
        {rows.map((row) => (
          <div
            key={row.id}
            className="rounded-2xl border border-white/10 bg-black/50 p-3 text-sm text-white/90 space-y-2"
          >
            <div className="flex items-center justify-between text-xs uppercase tracking-[0.25em] text-white/50">
              <span>#{row.id}</span>
              <span aria-label={row.status}>
                {row.status === "loading" && "⏳"}
                {row.status === "done" && "✅"}
                {row.status === "error" && "⚠️"}
              </span>
            </div>
            <div className="text-white/80 leading-relaxed">{row.source}</div>
            <textarea
              value={row.target}
              onChange={(e) => updateTarget(row.id, e.target.value)}
              rows={2}
              className="w-full rounded-xl border border-white/10 bg-[#050b16] px-3 py-2 text-sm text-white focus:border-[#22d3ee] focus:outline-none focus:ring-1 focus:ring-[#22d3ee]/40"
              placeholder="English translation"
            />
            {row.error && (
              <div className="text-xs text-rose-200">Error: {row.error}</div>
            )}
          </div>
        ))}
      </div>
    )}
  </div>
</section>
  );
}
