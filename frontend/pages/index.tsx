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
        <div className="relative z-10 max-w-6xl mx-auto px-6 py-10 flex flex-col gap-12">
          <header className="flex flex-col gap-6 text-center md:text-left">
            <div className="inline-flex items-center gap-2 mx-auto md:mx-0 px-4 py-1 rounded-full bg-white/10 border border-white/20 text-sm font-medium">
              <span className="text-cyan-300">Live beta</span>
              <span className="text-white/80">Latency tuned for bilingual services</span>
            </div>
            <h1 className="text-3xl md:text-5xl font-semibold leading-tight text-white">
              Real-Time Sermon Translator
            </h1>
            <p className="text-lg text-slate-200 max-w-3xl">
              Stream Korean audio, read the transcript instantly, and deliver polished English translations in under a second. Designed for bilingual congregations, conferences, and live broadcasts.
            </p>
            <div className="flex flex-wrap gap-4 text-sm text-slate-300">
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

          <section className="grid gap-6 md:grid-cols-3">
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

          <section>
            <TranslationBox />
          </section>
        </div>
      </main>
    </>
  )
}
