// pages/admin-hybrid.tsx
"use client";

import Head from "next/head";
import ScriptUpload from "../components/ScriptUpload";
import ProducerBox from "../components/ProducerBox";
import SermonPrep from "../components/SermonPrep";
import { useTranslationSocket } from "../utils/useTranslationSocket";

export default function AdminHybrid() {
  const { last, connected } = useTranslationSocket();

  const displayText = last.preview?.trim() ? last.preview : last.text;
  const sourceText =
    last.meta?.source_text ||
    last.meta?.reference ||
    last.meta?.reference_en ||
    last.matchedSource ||
    last.srcText ||
    "";
  const matchScore = last.matchScore > 0 ? `${(last.matchScore * 100).toFixed(1)}%` : "—";
  const langPair = `${(last.srcLang ?? "ko").toUpperCase()} → ${(last.lang ?? "en").toUpperCase()}`;
  const commitLabel = last.meta?.is_final === false ? "Preview (partial)" : "Finalized";
  const modeLabel = last.mode === "pre" ? "Pre-script" : "Realtime";
  const seqLabel = last.seq ? `#${last.seq}` : "—";

  const metaEntries = [
    { label: "Status", value: commitLabel },
    { label: "Mode", value: modeLabel },
    { label: "Match", value: matchScore },
    { label: "Lang Pair", value: langPair },
  ];

  return (
    <>
      <Head>
        <title>Hybrid Admin Console</title>
      </Head>
      <main className="min-h-screen bg-gradient-to-b from-[#0b1220] via-[#0f172a] to-[#0b1220] text-slate-100">
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-10 px-5 py-10">
          <header className="flex flex-col gap-6">
            <div className="flex flex-wrap items-start justify-between gap-6">
              <div className="space-y-3">
                <p className="text-xs uppercase tracking-[0.3em] text-[#94a3b8]">Hybrid Ops</p>
                <h1 className="text-3xl font-semibold text-white md:text-4xl">Hybrid Translation Admin</h1>
                <p className="text-base text-white/70 md:max-w-2xl">
                  Prep bilingual lines, tweak translations, and push them live without interrupting the realtime feed.
                </p>
              </div>
              <div
                className={`inline-flex items-center gap-2 rounded-full border px-4 py-2 text-sm font-semibold shadow-lg transition ${
                  connected
                    ? "border-emerald-400/50 bg-emerald-500/10 text-emerald-200"
                    : "border-rose-400/50 bg-rose-500/10 text-rose-200"
                }`}
              >
                <span
                  className={`h-2.5 w-2.5 rounded-full transition duration-500 ${
                    connected
                      ? "bg-emerald-300 shadow-[0_0_10px_rgba(16,185,129,0.75)]"
                      : "bg-rose-300"
                  }`}
                />
                {connected ? "Consumer socket online" : "Reconnecting to socket"}
              </div>
            </div>

            <div className="grid gap-3 text-sm text-white/70 md:grid-cols-3">
              <div className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.35em] text-white/60">Last Seq</p>
                <p className="text-lg font-semibold text-white">{seqLabel}</p>
              </div>
              <div className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.35em] text-white/60">Match</p>
                <p className="text-lg font-semibold text-white">{matchScore}</p>
              </div>
              <div className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.35em] text-white/60">Mode</p>
                <p className="text-lg font-semibold text-white">{modeLabel}</p>
              </div>
            </div>
          </header>

          <section className="grid gap-8 lg:grid-cols-2 items-start">
            <div className="flex flex-col gap-6">
              <ScriptUpload />
            </div>

            <div className="flex flex-col gap-6 h-full">
              <ProducerBox />

              <div className="rounded-[32px] border border-white/10 bg-[#050910]/90 p-6 shadow-[0_25px_65px_rgba(0,0,0,0.55)] backdrop-blur h-full">
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div className="space-y-1">
                    <p className="text-xs uppercase tracking-[0.35em] text-white/60">Monitor</p>
                    <h2 className="text-xl font-semibold text-white">Last Broadcast</h2>
                    <p className="text-sm text-white/60">Peek at the final packet that went out to listeners.</p>
                  </div>
                  <span className="text-sm text-white/70">Seq {seqLabel}</span>
                </div>

                <div className="mt-5 space-y-4">
                  <div className="rounded-2xl border border-cyan-400/20 bg-black/70 p-4 text-sm text-cyan-100">
                    <div className="mb-1 text-xs uppercase tracking-[0.35em] text-white/40">
                      {commitLabel}
                    </div>
                    <p className="font-mono text-base leading-relaxed text-[#9efcff] whitespace-pre-wrap">
                      {displayText || "No packets yet. Trigger a chunk from the producer console."}
                    </p>
                  </div>

                  {sourceText ? (
                    <div className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3">
                      <p className="text-xs uppercase tracking-[0.35em] text-white/50">Source</p>
                      <p className="text-sm text-white">{sourceText}</p>
                    </div>
                  ) : null}

                  <dl className="grid gap-3 sm:grid-cols-2">
                    {metaEntries.map((entry) => (
                      <div key={entry.label} className="rounded-2xl border border-white/5 bg-white/5 px-4 py-3 text-sm">
                        <dt className="text-xs uppercase tracking-[0.35em] text-white/50">{entry.label}</dt>
                        <dd className="mt-1 text-base text-white">{entry.value}</dd>
                      </div>
                    ))}
                  </dl>

                  <details className="rounded-2xl border border-white/10 bg-black/60 p-4">
                    <summary className="cursor-pointer text-sm font-medium text-white/80">Raw payload</summary>
                    <pre className="mt-3 max-h-64 overflow-auto rounded-xl bg-black/70 p-4 text-xs text-[#0f0] whitespace-pre-wrap">
{JSON.stringify(last, null, 2)}
                    </pre>
                  </details>
                </div>
              </div>
            </div>
          </section>

          <section className="mt-2">
            <SermonPrep />
          </section>
        </div>
      </main>
    </>
  );
}
