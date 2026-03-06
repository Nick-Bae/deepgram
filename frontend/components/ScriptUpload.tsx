// components/ScriptUpload.tsx
"use client";

import { useEffect, useId, useState } from "react";

import { useAuth } from "../lib/authContext";
import { clearOrgScript, fetchOrgScriptStatus, uploadOrgScript } from "../lib/backendAuth";

type Props = {
  orgId: string;
};

export default function ScriptUpload({ orgId }: Props) {
  const { getIdToken } = useAuth();
  const [ko, setKo] = useState("");
  const [en, setEn] = useState("");
  const [threshold, setThreshold] = useState<number>(0.84);
  const [status, setStatus] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const fieldId = useId();
  const koId = `${fieldId}-ko`;
  const enId = `${fieldId}-en`;
  const thresholdId = `${fieldId}-threshold`;

  useEffect(() => {
    let cancelled = false;
    const loadStatus = async () => {
      if (!orgId) {
        setStatus("Select a church to manage pre-script pairs.");
        return;
      }
      try {
        const idToken = await getIdToken();
        if (!idToken || cancelled) return;
        const stats = await fetchOrgScriptStatus(idToken, orgId);
        if (cancelled) return;
        setThreshold(stats.threshold);
        setStatus(`Ready. ${stats.count} pairs loaded.`);
      } catch (err: unknown) {
        if (!cancelled) {
          const msg = err instanceof Error ? err.message : String(err);
          setStatus(`❌ ${msg}`);
        }
      }
    };
    void loadStatus();
    return () => {
      cancelled = true;
    };
  }, [getIdToken, orgId]);

  const upload = async () => {
    if (!orgId) {
      setStatus("❌ Select a church first");
      return;
    }
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
      setBusy(true);
      setStatus("⏳ Uploading…");
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");
      const j = await uploadOrgScript(idToken, orgId, { pairs, threshold });
      setStatus(`✅ Uploaded ${j.loaded} pairs. threshold=${j.threshold}`);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setStatus(`❌ ${msg}`);
    } finally {
      setBusy(false);
    }
  };

  const clearScript = async () => {
    if (!orgId) {
      setStatus("❌ Select a church first");
      return;
    }
    try {
      setBusy(true);
      setStatus("⏳ Clearing script…");
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");
      const j = await clearOrgScript(idToken, orgId);
      setStatus(`🗑️ Cleared pre-script (${j.removed} removed)`);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setStatus(`❌ ${msg}`);
    } finally {
      setBusy(false);
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
          <p className="text-xs uppercase tracking-[0.35em] text-white/60">Legacy Mode</p>
          <h2 className="text-xl font-semibold text-white">Line-by-line pair upload</h2>
          <p className="text-sm text-white/70">
            Keep using one-sentence-per-line KO+EN uploads when needed.
          </p>
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
            disabled={busy || !orgId}
            className="inline-flex items-center justify-center gap-2 rounded-2xl bg-[#22d3ee] px-5 py-2.5 text-sm font-semibold text-[#041018] shadow-[0_15px_45px_rgba(34,211,238,0.35)] transition hover:bg-[#00ffff]"
          >
            {busy ? "Working..." : "Upload to buffer"}
          </button>
          <button
            type="button"
            onClick={clearScript}
            disabled={busy || !orgId}
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
