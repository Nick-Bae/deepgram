import Head from 'next/head'
import Link from 'next/link'
import TranslationBox from '../components/TranslationBox'

const featureCards = [
  {
    title: 'Live Speech → Text',
    desc: 'Streaming Deepgram transcription with sentence smoothing that keeps bilingual congregations in sync.',
    accent: 'from-[#feda6a] via-[#f2c53d] to-[#393f4d]',
    stat: '↘ 420 ms avg latency',
  },
  {
    title: 'AI-Powered Translation',
    desc: 'OpenAI translation tuned for sermons with context memory and tone-safe callbacks.',
    accent: 'from-[#668c4a] via-[#f2f5e3] to-[#d4d4dc]',
    stat: 'Tone-aware + glossary boost',
  },
  {
    title: 'Audience Broadcast',
    desc: 'WebSocket hub fans out every finalized clause to mobile listeners, foyer displays, and livestream overlays.',
    accent: 'from-[#454543] via-[#393f4d] to-[#1d1e22]',
    stat: '182 listeners on-air',
  },
]

const palette = {
  midnight: '#1d1e22',
  graphite: '#393f4d',
  carbon: '#454543',
  fog: '#d4d4dc',
  bone: '#f2f5e3',
  ash: '#b1b1ac',
  sunshine: '#feda6a',
  amber: '#f2c53d',
  olive: '#668c4a',
}

export default function Home() {
  const scrollToConsole = () => {
    if (typeof window === 'undefined') return
    document.getElementById('live-console')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return (
    <>
      <Head>
        <title>Real-Time Translator</title>
      </Head>
      <main className="min-h-screen bg-[#1d1e22] text-[#d4d4dc]">
        <div className="relative overflow-hidden">
          <div
            className="absolute inset-0"
            style={{
              background: `radial-gradient(circle at 12% 20%, ${palette.sunshine}33, transparent 55%)`,
            }}
          />
          <div
            className="absolute inset-0"
            style={{
              background: `radial-gradient(circle at 82% 0%, ${palette.olive}22, transparent 45%)`,
            }}
          />
        </div>
        <div className="relative z-10 max-w-6xl mx-auto px-6 py-10 flex flex-col gap-14">
          <section className="grid gap-12 lg:grid-cols-[1.05fr_0.95fr] items-center">
            <header className="flex flex-col gap-6 text-left">
              <div className="inline-flex items-center gap-2 px-4 py-1 rounded-full bg-[#f2f5e3]/10 border border-[#f2f5e3]/30 text-sm font-medium text-[#f2f5e3]">
                <span className="inline-flex w-2.5 h-2.5 items-center justify-center">
                  <span className="h-2.5 w-2.5 rounded-full bg-[#668c4a] animate-ping" />
                </span>
                Live beta • bilingual latency tuned
              </div>
              <h1 className="text-3xl md:text-5xl font-semibold leading-tight text-[#f2f5e3]">
                Real-Time Sermon Translator
              </h1>
              <p className="text-lg text-[#d4d4dc] max-w-3xl">
                Stream Korean audio, read the transcript instantly, and deliver polished English translations in under a second.
                Built for bilingual congregations, conferences, and broadcast booths.
              </p>
              <div className="flex flex-wrap gap-3 text-sm text-[#b1b1ac]">
                <span className="inline-flex items-center gap-2 bg-[#393f4d] px-3 py-1.5 rounded-full border border-[#454543]">
                  <span className="w-2 h-2 rounded-full bg-[#668c4a] animate-pulse" />
                  Sub-0.5s WebSocket delivery
                </span>
                <span className="inline-flex items-center gap-2 bg-[#393f4d] px-3 py-1.5 rounded-full border border-[#454543]">
                  ✨ GPT-4o / GPT-4o mini tandem
                </span>
                <span className="inline-flex items-center gap-2 bg-[#393f4d] px-3 py-1.5 rounded-full border border-[#454543]">
                  🎧 Booth-ready TTS returns
                </span>
              </div>
              <div className="flex flex-wrap gap-3 text-sm">
                <button
                  type="button"
                  onClick={scrollToConsole}
                  className="rounded-xl bg-[#feda6a] px-5 py-2.5 font-semibold text-[#1d1e22] shadow-[0_15px_45px_rgba(254,218,106,0.35)] transition hover:bg-[#f2c53d]"
                >
                  Open Live Producer Console
                </button>
                <Link
                  href="/admin-hybrid"
                  className="rounded-xl border border-[#454543] px-4 py-2.5 font-semibold text-[#d4d4dc] transition hover:text-[#feda6a] hover:border-[#feda6a]"
                >
                  Admin Hybrid Mode
                </Link>
              </div>
            </header>

            <div className="relative rounded-[32px] border border-[#454543] bg-gradient-to-br from-[#393f4d] via-[#1d1e22] to-[#1d1e22] p-8 shadow-[0_35px_120px_rgba(0,0,0,0.55)] overflow-hidden">
              <div className="absolute -right-10 top-0 h-48 w-48 bg-[#feda6a]/20 blur-3xl" />
              <div className="absolute -left-20 bottom-0 h-44 w-44 bg-[#668c4a]/20 blur-3xl" />
              <div className="relative flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.35em] text-[#b1b1ac]">
                <span className="inline-flex h-2.5 w-2.5 items-center justify-center">
                  <span className="h-2.5 w-2.5 rounded-full bg-[#feda6a] animate-pulse" />
                </span>
                Control Booth Snapshot
              </div>
              <h2 className="relative mt-5 text-3xl font-semibold text-[#feda6a]">Producer Console</h2>
              <p className="relative mt-3 text-sm text-[#d4d4dc]/80">
                Translate, stage, and broadcast in the same surface. Feed dashboard drops source + translated ribbons to display walls and mobile listeners.
              </p>
              <div className="relative mt-8 grid gap-4 sm:grid-cols-2">
                <div className="rounded-2xl border border-[#454543] bg-[#1d1e22]/80 p-4 text-[#f2f5e3]">
                  <p className="text-xs uppercase tracking-[0.35em] text-[#b1b1ac]">Latency</p>
                  <p className="mt-3 text-3xl font-semibold text-[#feda6a]">420&nbsp;ms</p>
                  <p className="text-xs text-[#d4d4dc]/70">Average over last 30s</p>
                </div>
                <div className="rounded-2xl border border-[#b1b1ac]/50 bg-[#f2f5e3] p-4 text-[#1d1e22]">
                  <p className="text-xs uppercase tracking-[0.35em] text-[#668c4a]">AI Assist</p>
                  <p className="mt-3 text-3xl font-semibold text-[#393f4d]">Active</p>
                  <p className="text-xs text-[#454543]">Glossary boost · 98% confidence</p>
                </div>
              </div>
              <div className="relative mt-6 flex flex-col gap-4 rounded-2xl bg-[#feda6a] px-5 py-4 text-[#1d1e22] sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p className="text-xs uppercase tracking-[0.45em]">Broadcast</p>
                  <p className="mt-2 text-2xl font-semibold">182 listeners live</p>
                  <p className="text-xs text-[#454543]">Display, foyer, livestream</p>
                </div>
                <Link
                  href="/display"
                  className="inline-flex items-center justify-center rounded-xl bg-[#393f4d] px-4 py-2 text-sm font-semibold text-[#feda6a] shadow-[0_10px_30px_rgba(18,18,18,0.4)] hover:bg-[#1d1e22]"
                >
                  View audience wall →
                </Link>
              </div>
            </div>
          </section>

          <section className="flex flex-col gap-12">
            <section className="grid gap-5 md:grid-cols-3">
              {featureCards.map((feature) => (
                <article
                  key={feature.title}
                  className="rounded-3xl border border-[#feda6a]/25 bg-[#f2f5e3]/10 px-6 py-7 shadow-[0_25px_60px_rgba(0,0,0,0.35)] backdrop-blur transition hover:border-[#feda6a]/60"
                >
                  <div className={`relative mb-5 h-2 w-24 rounded-full bg-gradient-to-r ${feature.accent} ring-1 ring-white/30 shadow-[0_8px_20px_rgba(0,0,0,0.45)]`} />
                  <p className="text-xs uppercase tracking-[0.35em] text-[#b1b1ac]">{feature.stat}</p>
                  <h3 className="mt-3 text-xl font-semibold text-[#f2f5e3]">{feature.title}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-[#d4d4dc]">{feature.desc}</p>
                </article>
              ))}
            </section>

            <div className="rounded-3xl border border-[#668c4a]/40 bg-gradient-to-r from-[#1d1e22] via-[#393f4d] to-[#1d1e22] p-8 text-left shadow-[0_25px_60px_rgba(0,0,0,0.45)]">
              <p className="text-sm uppercase tracking-[0.35em] text-[#668c4a]">Need hybrid?</p>
              <h3 className="mt-2 text-2xl font-semibold text-[#f2f5e3]">Upload-ready for scripted segments</h3>
              <p className="mt-3 text-[#d4d4dc]/90">
                Pre-translate testimony notes or Scripture readings, then hand off to the live console mid-service without losing the broadcast feed. Seamlessly blend scheduled lines with spontaneous preaching.
              </p>
              <a
                href="#hybrid-mode"
                className="mt-5 inline-flex items-center justify-center rounded-2xl bg-[#f2f5e3] px-5 py-2.5 text-sm font-semibold text-[#1d1e22] shadow-[0_15px_45px_rgba(242,245,227,0.4)] transition hover:bg-[#d4d4dc]"
              >
                Explore hybrid workflow →
              </a>
            </div>
          </section>

          <section id="live-console" className="scroll-mt-32 md:scroll-mt-5 rounded-[32px] border border-[#454543] bg-[#1d1e22]/80 p-4 shadow-[0_25px_70px_rgba(0,0,0,0.5)] backdrop-blur">
            <TranslationBox />
          </section>

          <section id="hybrid-mode" className="mt-16 grid gap-8 lg:grid-cols-2">
            <article className="rounded-3xl border border-[#454543] bg-[#393f4d]/70 p-6 backdrop-blur">
              <p className="text-sm font-semibold uppercase tracking-[0.3em] text-[#b1b1ac]">Hybrid mode</p>
              <h3 className="mt-3 text-2xl font-semibold text-[#f2f5e3]">Upload + broadcast in sync</h3>
              <ul className="mt-4 space-y-3 text-sm text-[#d4d4dc]">
                <li>• Drop pre-translated paragraphs or slides for songs and responsive readings.</li>
                <li>• Queue them and rehearse pronunciation before they reach the audience.</li>
                <li>• Trigger each line alongside live mic translation to keep bilingual listeners aligned.</li>
              </ul>
            </article>
            <article className="rounded-3xl border border-[#668c4a]/40 bg-[#668c4a]/15 p-6">
              <h4 className="text-xl font-semibold text-[#f2f5e3]">Admin console</h4>
              <p className="mt-3 text-[#f2f5e3]/80">
                Use the upload console to stage files, preview translations, and push them to the same WebSocket channel the audience app already listens to.
              </p>
              <Link
                href="/admin-hybrid"
                className="mt-6 inline-flex items-center justify-center rounded-2xl bg-[#feda6a] px-5 py-2.5 text-base font-semibold text-[#1d1e22] shadow-md transition hover:bg-[#f2c53d]"
              >
                Open hybrid admin
              </Link>
            </article>
          </section>
        </div>
      </main>
    </>
  )
}
