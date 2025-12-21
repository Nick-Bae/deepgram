"use client";

import React, { useEffect, useMemo, useState } from "react";
import { API_URL } from "../utils/urls";
import type { LastState } from "../utils/useTranslationSocket";

type Props = {
  last: LastState;
};

type Status = "idle" | "saving" | "saved" | "error";

export default function CorrectionPad({ last }: Props) {
  const sourceText =
    last.meta?.source_text ||
    last.meta?.matched_source ||
    last.srcText ||
    last.matchedSource ||
    "";
  const [correction, setCorrection] = useState(last.text || "");
  const [status, setStatus] = useState<Status>("idle");
  const [message, setMessage] = useState<string>("");

  useEffect(() => {
    // When a new packet arrives, preload its translation into the editor.
    setCorrection(last.text || "");
    setStatus("idle");
    setMessage("");
  }, [last.text, last.seq]);

  const canSubmit = useMemo(() => {
    return Boolean(sourceText && correction.trim());
  }, [sourceText, correction]);

  const submit = async () => {
    if (!canSubmit || status === "saving") return;
    setStatus("saving");
    setMessage("");
    try {
      const res = await fetch(`${API_URL.replace(/\/+$/, "")}/api/examples/correct`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_lang: last.srcLang || "ko",
          target_lang: last.lang || "en",
          stt_text: sourceText,
          auto_translation: last.text,
          final_translation: correction.trim(),
        }),
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      setStatus("saved");
      setMessage("Saved as a corrected example. Future prompts can reuse it.");
    } catch (err: unknown) {
      setStatus("error");
      if (err instanceof Error) {
        setMessage(err.message || "Failed to save");
      } else {
        setMessage("Failed to save");
      }
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      submit();
    }
  };

  return (
    <section className="rounded-[28px] border border-white/10 bg-[#050910]/90 p-5 text-white shadow-[0_18px_45px_rgba(0,0,0,0.45)] backdrop-blur">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <p className="text-xs uppercase tracking-[0.35em] text-white/60">Feedback loop</p>
          <h3 className="text-lg font-semibold">Log a corrected translation</h3>
          <p className="text-sm text-white/60">
            Edit the latest line and press ⌘/Ctrl+Enter to save. Saved rows feed the few-shot block automatically.
          </p>
        </div>
        <div className="text-xs text-white/60">
          Seq <span className="font-mono text-white">{last.seq || "—"}</span>
        </div>
      </div>

      <div className="mt-4 space-y-3">
        <div className="rounded-2xl border border-white/10 bg-black/60 px-4 py-3 text-sm">
          <div className="text-[11px] uppercase tracking-[0.3em] text-white/50">Source</div>
          <p className="mt-1 text-white/90 whitespace-pre-wrap">
            {sourceText || "Waiting for a packet with source text…"}
          </p>
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between text-[11px] uppercase tracking-[0.3em] text-white/50">
            <span>Corrected translation</span>
            <span className="text-white/60">⌘/Ctrl+Enter to save</span>
          </div>
          <textarea
            value={correction}
            onChange={(e) => setCorrection(e.target.value)}
            onKeyDown={onKeyDown}
            rows={4}
            className="w-full rounded-2xl border border-white/10 bg-[#050b16] px-4 py-3 text-sm text-white placeholder-white/30 focus:border-[#22d3ee] focus:outline-none focus:ring-2 focus:ring-[#22d3ee]/30"
          />
        </div>

        <div className="flex flex-wrap gap-3">
          <button
            type="button"
            onClick={submit}
            disabled={!canSubmit || status === "saving"}
            className={`inline-flex items-center justify-center gap-2 rounded-2xl px-4 py-2.5 text-sm font-semibold transition ${
              canSubmit && status !== "saving"
                ? "bg-emerald-400 text-[#05211a] shadow-[0_12px_35px_rgba(16,185,129,0.35)] hover:bg-emerald-300"
                : "cursor-not-allowed bg-white/10 text-white/40"
            }`}
          >
            {status === "saving" ? "Saving…" : "Save correction"}
          </button>
          {status === "saved" ? (
            <span className="text-sm text-emerald-200">{message || "Saved"}</span>
          ) : status === "error" ? (
            <span className="text-sm text-rose-200">{message || "Failed to save"}</span>
          ) : status === "idle" && message ? (
            <span className="text-sm text-white/70">{message}</span>
          ) : null}
        </div>
      </div>
    </section>
  );
}
