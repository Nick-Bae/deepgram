// components/ProducerBox.tsx
"use client";

import { useState } from "react";
import { useTranslationSocket } from "../utils/useTranslationSocket";
import { useSentenceBuffer } from "../utils/useSentenceBuffer";

export default function ProducerBox() {
  const { connected, sendProducerText } = useTranslationSocket({ isProducer: true });
  const [debugBuf, setDebugBuf] = useState("");

  const buffer = useSentenceBuffer(
    (sentence) => {
      // When a full sentence is ready, send to backend as a final commit
      sendProducerText(sentence, "ko", "en", false, undefined, undefined, true);
      setDebugBuf(""); // cleared after sending
    },
    { timeoutMs: 1200, minLength: 4 }
  );

  const [input, setInput] = useState("");
  const chunk = input.trim();

  const pushChunk = () => {
    if (!chunk) return;
    buffer.add(chunk);
    setDebugBuf(buffer.peek());
    setInput("");
  };

  const forceFlush = () => buffer.flush(true);

  return (
    <section className="rounded-[32px] border border-white/10 bg-[#050910]/90 p-6 text-white shadow-[0_25px_65px_rgba(0,0,0,0.55)] backdrop-blur">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-1">
          <p className="text-xs uppercase tracking-[0.35em] text-white/60">Producer Console</p>
          <h2 className="text-xl font-semibold">Manual chunk control</h2>
          <p className="text-sm text-white/70">
            Paste interim STT text, then flush to commit a finalized sentence to the hybrid translation feed.
          </p>
        </div>
        <span
          className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-semibold ${
            connected
              ? "border-emerald-400/50 bg-emerald-500/10 text-emerald-200"
              : "border-rose-400/50 bg-rose-500/10 text-rose-200"
          }`}
        >
          <span
            className={`h-2 w-2 rounded-full transition duration-500 ${
              connected
                ? "bg-emerald-300 shadow-[0_0_8px_rgba(16,185,129,0.8)]"
                : "bg-rose-300"
            }`}
          />
          {connected ? "WS online" : "WS reconnecting"}
        </span>
      </div>

      <div className="mt-6 space-y-4">
        <textarea
          rows={3}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Type partial STT chunks… (e.g., '오늘 하나님은', then '사랑이십니다.')"
          className="w-full rounded-2xl border border-white/10 bg-[#050b16] px-4 py-3 text-sm text-white placeholder-white/40 focus:border-[#22d3ee] focus:outline-none focus:ring-2 focus:ring-[#22d3ee]/40"
        />
        <div className="flex flex-wrap gap-3">
          <button
            type="button"
            onClick={pushChunk}
            disabled={!chunk}
            className={`inline-flex items-center justify-center gap-2 rounded-2xl px-4 py-2.5 text-sm font-semibold transition ${
              chunk
                ? "bg-[#22d3ee] text-[#041018] shadow-[0_12px_35px_rgba(34,211,238,0.35)] hover:bg-[#00ffff]"
                : "cursor-not-allowed bg-white/10 text-white/40"
            }`}
          >
            Queue chunk
          </button>
          <button
            type="button"
            onClick={forceFlush}
            className="inline-flex items-center justify-center gap-2 rounded-2xl border border-white/20 px-4 py-2.5 text-sm font-semibold text-white transition hover:border-amber-200/80 hover:text-amber-200"
          >
            Force flush (silence)
          </button>
        </div>

        <div className="space-y-2 text-sm text-white/70">
          <div className="flex items-center justify-between text-xs uppercase tracking-[0.35em] text-white/50">
            <span>Buffer</span>
            <span>Auto flush 1.2s</span>
          </div>
          <div className="rounded-2xl border border-white/10 bg-black/60 px-4 py-3 font-mono text-[#9efcff]">
            {debugBuf || "— idle —"}
          </div>
        </div>
      </div>
    </section>
  );
}
