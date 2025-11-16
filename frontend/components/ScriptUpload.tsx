// components/ScriptUpload.tsx
"use client";

import { useId, useState } from "react";
import { API_URL } from "../utils/urls";

export default function ScriptUpload() {
  const [ko, setKo] = useState("");
  const [en, setEn] = useState("");
  const [threshold, setThreshold] = useState<number>(0.84);
  const [status, setStatus] = useState<string>("");
  const fieldId = useId();
  const koId = `${fieldId}-ko`;
  const enId = `${fieldId}-en`;
  const thresholdId = `${fieldId}-threshold`;

  const upload = async () => {
    const koLines = ko
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    const enLines = en
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    if (koLines.length !== enLines.length) {
      setStatus(`❌ Lines mismatch: KO=${koLines.length} vs EN=${enLines.length}`);
      return;
    }
    const pairs = koLines.map((k, i) => ({ source: k, target: enLines[i] }));

    try {
      setStatus("⏳ Uploading…");
      const res = await fetch(`${API_URL}/api/script/upload`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          payload: { pairs },
          cfg: { threshold },
        }),
      });

      const j = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = j && j.detail ? JSON.stringify(j.detail) : res.statusText;
        throw new Error(detail || "Upload failed");
      }
      setStatus(`✅ Uploaded ${j.loaded} pairs. threshold=${j.threshold}`);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setStatus(`❌ ${msg}`);
    }
  };

  const clearScript = async () => {
    try {
      setStatus("⏳ Clearing script…");
      const res = await fetch(`${API_URL}/api/script`, { method: "DELETE" });
      const j = await res.json();
      if (!res.ok) throw new Error(j.detail || "Clear failed");
      setStatus("🗑️ Cleared pre-script");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setStatus(`❌ ${msg}`);
    }
  };

  const statusTone = status.startsWith("✅")
    ? "text-emerald-300"
    : status.startsWith("❌")
    ? "text-rose-300"
    : status.startsWith("⏳")
    ? "text-cyan-200"
    : "text-slate-300";

  return (
    <section className="rounded-[32px] border border-white/10 bg-white/5 p-6 shadow-[0_35px_120px_rgba(3,7,18,0.55)] backdrop-blur">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-1">
          <p className="text-xs uppercase tracking-[0.35em] text-white/60">Pre-script</p>
          <h2 className="text-xl font-semibold text-white">Upload bilingual pairs</h2>
          <p className="text-sm text-white/70">Paste Korean + English lines to prime the hybrid translator.</p>
        </div>
        <span className="text-xs font-semibold text-white/60">Threshold • {threshold.toFixed(2)}</span>
      </div>

      <div className="mt-6 space-y-5">
        <div className="space-y-2">
          <label htmlFor={koId} className="text-sm font-medium text-white/90">
            Korean (one sentence per line)
          </label>
          <textarea
            id={koId}
            value={ko}
            onChange={(e) => setKo(e.target.value)}
            rows={8}
            spellCheck={false}
            className="min-h-[160px] w-full rounded-2xl border border-white/10 bg-[#050b16] px-4 py-3 text-sm text-white placeholder-white/40 focus:border-[#22d3ee] focus:outline-none focus:ring-2 focus:ring-[#22d3ee]/40"
            placeholder="예: 오늘 하나님은…"
          />
        </div>

        <div className="space-y-2">
          <label htmlFor={enId} className="text-sm font-medium text-white/90">
            English (one sentence per line)
          </label>
          <textarea
            id={enId}
            value={en}
            onChange={(e) => setEn(e.target.value)}
            rows={8}
            spellCheck={false}
            className="min-h-[160px] w-full rounded-2xl border border-white/10 bg-[#050b16] px-4 py-3 text-sm text-white placeholder-white/40 focus:border-[#22d3ee] focus:outline-none focus:ring-2 focus:ring-[#22d3ee]/40"
            placeholder="e.g., Today God reminds us…"
          />
        </div>

        <div className="space-y-2">
          <label htmlFor={thresholdId} className="text-sm font-medium text-white/90">
            Match threshold
          </label>
          <input
            id={thresholdId}
            type="number"
            step="0.01"
            min={0}
            max={1}
            value={threshold}
            onChange={(e) => {
              const next = Number(e.target.value);
              setThreshold(Number.isFinite(next) ? next : 0.84);
            }}
            className="w-full rounded-2xl border border-white/10 bg-[#050b16] px-4 py-2 text-sm text-white focus:border-[#22d3ee] focus:outline-none focus:ring-2 focus:ring-[#22d3ee]/40"
          />
        </div>

        <div className="flex flex-wrap gap-3">
          <button
            type="button"
            onClick={upload}
            className="inline-flex items-center justify-center gap-2 rounded-2xl bg-[#22d3ee] px-5 py-2.5 text-sm font-semibold text-[#041018] shadow-[0_15px_45px_rgba(34,211,238,0.35)] transition hover:bg-[#00ffff]"
          >
            Upload to buffer
          </button>
          <button
            type="button"
            onClick={clearScript}
            className="inline-flex items-center justify-center gap-2 rounded-2xl border border-white/20 px-5 py-2.5 text-sm font-semibold text-white transition hover:border-rose-300/80 hover:text-rose-200"
          >
            Clear script
          </button>
        </div>

        <div className={`text-sm ${statusTone}`} role="status" aria-live="polite">
          {status || "Waiting for pairs…"}
        </div>
      </div>
    </section>
  );
}
