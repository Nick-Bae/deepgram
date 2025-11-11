import Head from 'next/head'
import TranslationBox from '../components/TranslationBox'

const featureCards = [
  {
    title: 'Live Speech → Text',
    desc: 'Streaming Deepgram transcription with smart sentence detection.',
    accent: 'from-sky-400 via-blue-500 to-indigo-500',
  },
  {
    title: 'AI-Powered Translation',
    desc: 'OpenAI translation tuned for sermons and fast callbacks.',
    accent: 'from-emerald-400 via-emerald-500 to-lime-400',
  },
  {
    title: 'Audience Broadcast',
    desc: 'WebSocket hub fans out every line to connected listeners and displays.',
    accent: 'from-rose-400 via-pink-500 to-orange-400',
  },
]

export default function Home() {
  return (
    <>
      <Head>
        <title>Real-Time Translator</title>
      </Head>
      <main className="min-h-screen bg-slate-950 text-slate-50">
        <div className="relative overflow-hidden">
          <div className="absolute inset-0 bg-[radial-gradient(circle_at_top,_rgba(56,189,248,0.35),_transparent_55%)]" />
          <div className="absolute inset-0 opacity-40 blur-3xl" style={{ background: 'radial-gradient(circle at 20% 20%, rgba(236,72,153,.45), transparent 45%)' }} />
        </div>
        <div className="relative z-10 max-w-6xl mx-auto px-6 py-10">
          <div className="flex flex-col gap-12 lg:flex-row lg:items-start">
            <div className="flex-1 lg:max-w-xl space-y-8">
              <header className="flex flex-col gap-6 text-center lg:text-left">
                <div className="inline-flex items-center gap-2 mx-auto lg:mx-0 px-4 py-1 rounded-full bg-white/10 border border-white/20 text-sm font-medium">
                  <span className="text-cyan-300">Live beta</span>
                  <span className="text-white/80">Latency tuned for bilingual services</span>
                </div>
                <h1 className="text-3xl md:text-5xl font-semibold leading-tight text-white">
                  Real-Time Sermon Translator
                </h1>
                <p className="text-lg text-slate-200">
                  Stream Korean audio, read the transcript instantly, and deliver polished English translations in under a second. Designed for bilingual congregations, conferences, and live broadcasts.
                </p>
                <div className="flex flex-wrap justify-center lg:justify-start gap-3 text-sm text-slate-300">
                  <span className="inline-flex items-center gap-2 bg-white/5 px-3 py-1.5 rounded-full border border-white/10">
                    <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
                    Low-latency WebSocket delivery
                  </span>
                  <span className="inline-flex items-center gap-2 bg-white/5 px-3 py-1.5 rounded-full border border-white/10">
                    ✨ GPT-4o translation quality
                  </span>
                  <span className="inline-flex items-center gap-2 bg-white/5 px-3 py-1.5 rounded-full border border-white/10">
                    🎧 TTS ready for playback booths
                  </span>
                </div>
              </header>

              <section className="grid gap-4 sm:grid-cols-2">
                {featureCards.map((feature) => (
                  <article
                    key={feature.title}
                    className="rounded-2xl border border-white/10 bg-white/5 backdrop-blur px-5 py-6 shadow-lg hover:border-white/25 transition"
                  >
                    <div className={`h-1.5 w-16 rounded-full bg-gradient-to-r ${feature.accent} mb-4`} />
                    <h3 className="text-xl font-semibold text-white mb-2">{feature.title}</h3>
                    <p className="text-slate-200 text-sm leading-relaxed">{feature.desc}</p>
                  </article>
                ))}
              </section>

              <div className="rounded-3xl border border-cyan-400/30 bg-cyan-400/10 p-6 text-center lg:text-left">
                <p className="text-sm uppercase tracking-[0.25em] text-cyan-200">Need hybrid?</p>
                <h3 className="mt-2 text-2xl font-semibold text-white">Upload-ready for scripted segments</h3>
                <p className="mt-3 text-slate-200">
                  Pre-translate testimony notes or Scripture readings, then hand off to the live console mid-service without losing the broadcast feed.
                </p>
                <a
                  href="#hybrid-mode"
                  className="mt-4 inline-flex items-center justify-center rounded-2xl bg-white/90 px-5 py-2.5 text-sm font-semibold text-slate-900 shadow-lg transition hover:bg-white"
                >
                  Explore hybrid workflow →
                </a>
              </div>
            </div>

            <div className="flex-1 lg:sticky lg:top-6">
              <TranslationBox />
            </div>
          </div>

          <section id="hybrid-mode" className="mt-16 grid gap-8 lg:grid-cols-2">
            <article className="rounded-3xl border border-white/10 bg-white/5 p-6 backdrop-blur">
              <p className="text-sm font-semibold uppercase tracking-[0.3em] text-slate-300">Hybrid mode</p>
              <h3 className="mt-3 text-2xl font-semibold text-white">Upload + broadcast in sync</h3>
              <ul className="mt-4 space-y-3 text-sm text-slate-200">
                <li>• Drop pre-translated paragraphs or slides for songs and responsive readings.</li>
                <li>• Queue them and rehearse pronunciation before they reach the audience.</li>
                <li>• Trigger each line alongside live mic translation to keep bilingual listeners aligned.</li>
              </ul>
            </article>
            <article className="rounded-3xl border border-emerald-400/30 bg-emerald-400/10 p-6">
              <h4 className="text-xl font-semibold text-white">Admin console</h4>
              <p className="mt-3 text-slate-100">
                Use the upload console to stage files, preview translations, and push them to the same WebSocket channel the audience app already listens to.
              </p>
              <a
                href="/admin-hybrid"
                className="mt-6 inline-flex items-center justify-center rounded-2xl bg-emerald-300/90 px-5 py-2.5 text-base font-semibold text-emerald-900 shadow-md transition hover:bg-emerald-200"
              >
                Open hybrid admin
              </a>
            </article>
          </section>
        </div>
      </main>
    </>
  )
}
