'use client'

import { useMemo, useState, useEffect, useRef, useCallback } from 'react'
import { throttle } from '../utils/throttle'
import { useTranslationSocket } from '../utils/useTranslationSocket'
import { API_URL } from '../utils/urls'
import { useDeepgramProducer } from '../lib/useDeepgramProducer'
import type { DeepgramProducerController } from '../lib/useDeepgramProducer'

const DEBUG = process.env.NEXT_PUBLIC_DEBUG === '1';

function clip(s: string, n = 120) {
  const t = (s || '').trim();
  return t.length > n ? t.slice(0, n) + '…' : t;
}

function languageFlag(code: string) {
  const base = (code || '').split('-')[0];
  switch (base) {
    case 'ko': return '🇰🇷';
    case 'en': return '🇺🇸';
    case 'zh': return '🇨🇳';
    case 'es': return '🇪🇸';
    default: return '🌐';
  }
}

const availableLanguages = [
  { code: 'ko', name: 'Korean' },
  { code: 'zh-CN', name: 'Chinese (Simplified)' },
  { code: 'en', name: 'English' },
  { code: 'es', name: 'Spanish' },
]

function languageName(code: string) {
  const raw = (code || '').trim()
  if (!raw) return 'Unknown language'
  const lower = raw.toLowerCase()
  const exact = availableLanguages.find(l => l.code.toLowerCase() === lower)
  if (exact) return exact.name
  const base = lower.split('-')[0]
  const baseMatch = availableLanguages.find(l => l.code.toLowerCase() === base)
  if (baseMatch) return baseMatch.name
  return raw.toUpperCase()
}

const LINGER_MS = 300
const MIN_FINAL_CHARS = 10
const FINALIZE_PULSE_MS = 2600
const MIN_FORCE_FINALIZE_CHARS = 8
const INTRO_HOLD_RE = /(한마디로\s*요약(을)?\s*하면|결론부터\s*말하자면)$/
const EOS_RE = /[.!?。！？]$|(?:습니다|입니다|할까요|했어요|했지요|했네요)$/
const CLIENT_DRIVEN = false
const MIN_PREVIEW_CHARS = 10
const PREVIEW_THROTTLE_MS = 400
const HANGUL_CHAR_RE = /[\uac00-\ud7a3]/

type CancelableFn<Args extends unknown[] = unknown[]> = ((...args: Args) => void) & {
  cancel: () => void
}

export default function TranslationBox() {
  const { connected, last } = useTranslationSocket({ isProducer: true })

  // UI state
  const [text, setText] = useState('')
  const [translated, setTranslated] = useState('')
  const [isListening, setIsListening] = useState(false)
  const [sourceLang, setSourceLang] = useState('ko')
  const [targetLang, setTargetLang] = useState('en')
  const [isMuted, setIsMuted] = useState(false)
  const [volume, setVolume] = useState(1)
  const [selectedVoiceName, setSelectedVoiceName] = useState('')
  const [isBroadcasting, setIsBroadcasting] = useState(true)
  const [aiAssistEnabled, setAiAssistEnabled] = useState(true)
  const [displayOnAir, setDisplayOnAir] = useState(true)
  const [latencyMs, setLatencyMs] = useState<number | null>(null)
  const sourceLabel = useMemo(() => languageName(sourceLang), [sourceLang])
  const targetLabel = useMemo(() => languageName(targetLang), [targetLang])

  // Deepgram mic producer
  const dgController: DeepgramProducerController & { finalize?: () => void } = useDeepgramProducer()
  const { start: dgStart, stop: dgStop, status, partial, errorMsg, finalize } = dgController
  const startProducer = useCallback(async () => {
    const startWithOptions = dgStart as (options?: { sourceLang?: string; targetLang?: string }) => Promise<void>
    await startWithOptions({ sourceLang, targetLang })
  }, [dgStart, sourceLang, targetLang])
  const dgFinalize = useMemo(() => finalize ?? (() => {}), [finalize])

  // TTS refs
  const synthRef = useRef<SpeechSynthesis | null>(null)
  const ttsQueueRef = useRef<string[]>([])
  const speakingRef = useRef(false)
  const lastHandledSeqRef = useRef(0)       // gate: handle each seq once (final or soft-final)
  const currentSpokenRef = useRef('')

  // Clause buffer + timing
  const clauseRef = useRef('')
  const lingerTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lastInterimRef = useRef('')
  const lastFinalizeAtRef = useRef(0)
  const lastPreviewSentRef = useRef('')
  const lastSourceUpdateRef = useRef(0)

  // Track stability of non-final WS lines per seq (for soft-final fallback)
  const softMapRef = useRef<Map<number, { text: string; count: number; first: number; last: number }>>(new Map())
  const segmenterCacheRef = useRef<Record<string, Intl.Segmenter | undefined>>({})

  const formatSourceForDisplay = useCallback((raw: string) => {
    if (typeof raw !== 'string') return ''
    const trimmed = raw.trim()
    if (!trimmed) return ''

    const lang = (sourceLang || '').toLowerCase()
    if (!lang.startsWith('ko')) return trimmed
    if (!HANGUL_CHAR_RE.test(trimmed)) return trimmed
    if (trimmed.includes(' ')) return trimmed
    if (typeof Intl === 'undefined' || typeof Intl.Segmenter !== 'function') return trimmed

    const cacheKey = 'ko'
    let segmenter = segmenterCacheRef.current[cacheKey]
    if (!segmenter) {
      segmenter = new Intl.Segmenter(cacheKey, { granularity: 'word' })
      segmenterCacheRef.current[cacheKey] = segmenter
    }

    try {
      let formatted = ''
      let previousWasWord = false
      for (const part of segmenter.segment(trimmed)) {
        const chunk = part.segment.trim()
        if (!chunk) continue
        if (previousWasWord && part.isWordLike) formatted += ' '
        formatted += chunk
        previousWasWord = !!part.isWordLike
      }
      return formatted || trimmed
    } catch {
      return trimmed
    }
  }, [sourceLang])


  const triggerFinalize = useCallback(
    (reason?: string) => {
      if (!dgFinalize) return
      try {
        dgFinalize()
        lastFinalizeAtRef.current = Date.now()
        if (DEBUG && reason) console.log('[FE][finalize][pulse]', reason)
      } catch (err) {
        if (DEBUG) console.warn('[FE][finalize][pulse][error]', err)
      }
    },
    [dgFinalize]
  )

  // ---------- HTTP translate (client-driven OFF by default) ----------
  const postTranslate = useCallback(async (s: string, finalFlag: boolean) => {
    const body = {
      text: s,
      source: (sourceLang || 'ko').split('-')[0],
      target: (targetLang || 'en').split('-')[0],
      final: finalFlag
    };

    console.log(`[FE][HTTP][${finalFlag ? 'final' : 'preview'}] → /api/translate`, {
      source: body.source,
      target: body.target,
      in: clip(s)
    });

    try {
      const res = await fetch(`${API_URL}/api/translate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const txt = await res.text().catch(() => '');
      console.log('[FE][HTTP][res]', res.status, res.ok, clip(txt));
    } catch (e) {
      console.warn('[FE][HTTP][error]', e);
    }
  }, [sourceLang, targetLang]);

  const sendPreview = useMemo(() =>
    throttle((fullClause: string) => {
        if (!CLIENT_DRIVEN) return;

        const s = (fullClause || '').trim();
        if (!s) return;

        if (s.length < MIN_PREVIEW_CHARS && !EOS_RE.test(s)) return;
        if (s.length < MIN_FINAL_CHARS && INTRO_HOLD_RE.test(s)) return;

        if (!EOS_RE.test(s)) {
          if (s === lastPreviewSentRef.current) return;
          if (Math.abs(s.length - lastPreviewSentRef.current.length) < 2) return;
        }

        if (DEBUG) console.log('[FE][preview][clause]', clip(s));
        lastPreviewSentRef.current = s;
        postTranslate(s, false);
      }, PREVIEW_THROTTLE_MS)
  , [postTranslate]) as CancelableFn<[string]>

  const sendFinalNow = useCallback(
    (s: string) => {
      const clean = (s || '').trim();
      if (!clean) return;

      sendPreview.cancel();

      if (CLIENT_DRIVEN) {
        if (DEBUG) console.log('[FE][final][clause]', clip(clean));
        postTranslate(clean, true);
      } else {
        if (DEBUG) console.log('[FE][final][clause][no-http]', clip(clean));
      }

      lastPreviewSentRef.current = '';
      triggerFinalize('clause complete');
    },
    [postTranslate, sendPreview, triggerFinalize]
  );

  const scheduleFinal = useCallback(() => {
    if (lingerTimerRef.current) clearTimeout(lingerTimerRef.current);
    lingerTimerRef.current = setTimeout(() => {
      const s = clauseRef.current.trim();
      if (!s) return;

      if (s.length < MIN_FINAL_CHARS && !EOS_RE.test(s)) return;
      if (s.length < MIN_FINAL_CHARS && INTRO_HOLD_RE.test(s)) return;

      sendFinalNow(s);
      clauseRef.current = '';
    }, LINGER_MS);
  }, [sendFinalNow]);

  // ---------- Clear stale service-workers (helpful for dev HTTPS mixes) ----------
  useEffect(() => {
    if (typeof window === 'undefined' || !('serviceWorker' in navigator)) return

    const cleanup = async () => {
      try {
        const regs = await navigator.serviceWorker.getRegistrations()
        await Promise.all(regs.map(reg => reg.unregister().catch(() => undefined)))
      } catch (err) {
        console.warn('[FE][SW][cleanup-failed]', err)
      }

      if ('caches' in window) {
        try {
          const keys = await caches.keys()
          await Promise.all(keys.map(key => caches.delete(key).catch(() => false)))
        } catch (err) {
          console.warn('[FE][SW][cache-clear-failed]', err)
        }
      }
    }

    cleanup()
  }, [])

  // ---------- TTS helpers ----------
  function mapToTTSLocale(code: string) {
    const b = (code || '').split('-')[0];
    if (b === 'en') return 'en-US';
    if (b === 'ko') return 'ko-KR';
    if (b === 'zh') return 'zh-CN';
    if (b === 'es') return 'es-ES';
    return code || 'en-US';
  }

  function ensureTTSReady() {
    try {
      if (!synthRef.current) return;
      // Kick the engine so Chrome actually speaks later
      const u = new SpeechSynthesisUtterance(' ');
      u.volume = 0;
      synthRef.current.cancel();
      synthRef.current.speak(u);
      synthRef.current.resume?.();
    } catch {}
  }

  const playNext = useCallback(() => {
    if (speakingRef.current || !synthRef.current || isMuted) return;

    const next = ttsQueueRef.current.shift();
    if (!next) return;

    const utter = new SpeechSynthesisUtterance(next);
    utter.lang = mapToTTSLocale(targetLang);
    utter.volume = volume;

    const voices = synthRef.current.getVoices();
    const sel =
      voices.find(v => v.name === selectedVoiceName) ||
      voices.find(v => v.lang === utter.lang) ||
      voices[0];

    if (sel) utter.voice = sel;

    speakingRef.current = true;
    currentSpokenRef.current = next;

    console.log('[FE][TTS][start]', { text: clip(next), voice: sel?.name, lang: utter.lang });

    utter.onend = () => {
      speakingRef.current = false;
      console.log('[FE][TTS][end]');
      if (ttsQueueRef.current.length) setTimeout(playNext, 300);
    };
    utter.onerror = (e) => {
      speakingRef.current = false;
      console.warn('[FE][TTS][error]', e);
      if (ttsQueueRef.current.length) setTimeout(playNext, 300);
    };

    synthRef.current.speak(utter);
  }, [isMuted, selectedVoiceName, targetLang, volume]);

  const enqueueFinalTTS = useCallback((s: string) => {
    const t = s.trim();
    if (!t) return;

    if (currentSpokenRef.current === t) {
      console.log('[FE][TTS][drop-current-dup]', clip(t));
      return;
    }
    const tail = ttsQueueRef.current[ttsQueueRef.current.length - 1];
    if (tail === t) {
      console.log('[FE][TTS][drop-tail-dup]', clip(t));
      return;
    }

    console.log('[FE][TTS][enqueue]', clip(t));
    ttsQueueRef.current.push(t);
    playNext();
  }, [playNext]);

  // ---------- Speech Synthesis init ----------
  useEffect(() => {
    if (typeof window === 'undefined') return;
    synthRef.current = window.speechSynthesis;

    const onVoices = () => {
      const vs = synthRef.current?.getVoices() || [];
      // console.log('[FE][TTS][voices]', vs.map(v => `${v.name} (${v.lang})`));
      // Prefer a voice that matches targetLang if possible
      if (!selectedVoiceName && vs.length) {
        const want = mapToTTSLocale(targetLang);
        const byLang = vs.find(v => v.lang === want);
        setSelectedVoiceName((byLang || vs[0]).name);
      }
    };

    onVoices();
    window.speechSynthesis.onvoiceschanged = onVoices;

    // Warm-up once on mount
    ensureTTSReady();

    // Keep engine alive when tab regains focus (some browsers pause it)
    const vis = () => { try { synthRef.current?.resume?.(); } catch {} };
    document.addEventListener('visibilitychange', vis);
    return () => document.removeEventListener('visibilitychange', vis);
  }, [selectedVoiceName, targetLang]);

  // ---------- WS: consume broadcasts (single effect, with soft-final fallback) ----------
  useEffect(() => {
    const seq = last.seq ?? 0;
    const incoming = (last.text || '').trim();
    const lastMeta = (last as typeof last & { meta?: { is_final?: boolean } }).meta;
    const isFinal = !!lastMeta?.is_final;
    const committedSrc = typeof last.srcText === 'string' ? last.srcText.trim() : '';

    if (!incoming) return;

    console.log('[FE][WS][in]', { seq, isFinal, out: clip(incoming) });
    setTranslated(incoming);

    if (isFinal) {
      if (seq && seq <= lastHandledSeqRef.current) {
        console.log('[FE][WS][final][skip-already-handled]', seq);
        return;
      }
      if (seq) lastHandledSeqRef.current = seq;

      if (!isMuted && incoming !== currentSpokenRef.current) {
        enqueueFinalTTS(incoming);
      } else {
        console.log('[FE][TTS][skip]', { isMuted, sameAsCurrent: incoming === currentSpokenRef.current });
      }
      softMapRef.current.delete(seq);

      const clauseSnapshot = clauseRef.current.trim();
      const interimSnapshot = lastInterimRef.current.trim();
      const bestSource = committedSrc || clauseSnapshot || interimSnapshot;
      if (bestSource) {
        setText(formatSourceForDisplay(bestSource));
      }
      clauseRef.current = '';
      lastInterimRef.current = '';
      if (lastSourceUpdateRef.current) {
        setLatencyMs(Date.now() - lastSourceUpdateRef.current);
      }
      return;
    }

    if (!seq) return;
    const now = Date.now();
    const prev = softMapRef.current.get(seq);
    if (!prev) {
      softMapRef.current.set(seq, { text: incoming, count: 1, first: now, last: now });
    } else {
      const same = incoming === prev.text;
      const entry = {
        text: incoming,
        count: prev.count + (same ? 1 : 0),
        first: prev.first,
        last: now,
      };
      softMapRef.current.set(seq, entry);
      const stable = entry.count >= 2 || (now - entry.first) > 900;
      if (stable && EOS_RE.test(incoming) && seq > lastHandledSeqRef.current) {
        console.log('[FE][WS][soft-final]', { seq, out: clip(incoming) });
        lastHandledSeqRef.current = seq;
        if (!isMuted && incoming !== currentSpokenRef.current) {
          enqueueFinalTTS(incoming);
        }
      }
    }
  }, [enqueueFinalTTS, formatSourceForDisplay, isMuted, last]);

  // ---------- Deepgram partials → clause buffer ----------
  useEffect(() => {
    const cur = (partial || '').trim();
    if (!cur) return;
    lastSourceUpdateRef.current = Date.now();

    console.log('[FE][DG][partial]', clip(cur));

    const prev = lastInterimRef.current;
    let delta = '';

    if (cur.startsWith(prev)) {
      delta = cur.slice(prev.length);
    } else {
      const old = clauseRef.current.trim();

      if (old) {
        const oldLooksComplete = EOS_RE.test(old) || old.length >= MIN_FINAL_CHARS + 10;
        if (oldLooksComplete) {
          console.log('[FE][clause][rebase->final]', clip(old));
          sendFinalNow(old);
        } else {
          console.log('[FE][clause][rebase->drop-short]', clip(old));
        }
      }
      clauseRef.current = '';
      delta = cur;
    }

    if (delta) {
      console.log('[FE][clause][delta]', clip(delta));
      clauseRef.current += delta;

      sendPreview(clauseRef.current);

      if (EOS_RE.test(clauseRef.current)) {
        sendFinalNow(clauseRef.current);
        clauseRef.current = '';
      } else {
        scheduleFinal();
      }
    }

    lastInterimRef.current = cur;
    const formatted = formatSourceForDisplay(cur);
    setText(formatted || cur);
  }, [formatSourceForDisplay, partial, scheduleFinal, sendFinalNow, sendPreview]);

  // ---------- Keep isListening in sync with Deepgram ----------
  useEffect(() => {
    setIsListening(status === 'streaming')
    if (status !== 'streaming') {
      lastFinalizeAtRef.current = 0
    }
    if (status !== 'streaming' && clauseRef.current.trim()) {
      sendFinalNow(clauseRef.current)
      clauseRef.current = ''
    }
  }, [sendFinalNow, status])

  useEffect(() => {
    if (status !== 'streaming') return

    const interval = setInterval(() => {
      const clause = clauseRef.current.trim()
      if (!clause) return
      if (clause.length < MIN_FORCE_FINALIZE_CHARS && !EOS_RE.test(clause)) return

      const now = Date.now()
      if (now - lastFinalizeAtRef.current < FINALIZE_PULSE_MS * 0.8) return

      triggerFinalize('interval pulse')
    }, FINALIZE_PULSE_MS)

    return () => clearInterval(interval)
  }, [status, triggerFinalize])

  // ---------- Start/Stop mic ----------
  const handleStartListening = async () => {
    lastInterimRef.current = ''
    clauseRef.current = ''
    setText('')
    setTranslated('')
    ttsQueueRef.current = []
    synthRef.current?.cancel()
    speakingRef.current = false

    // Warm up TTS right before we expect to speak
    ensureTTSReady()

    try {
      await startProducer()
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : String(e)
      alert(`Mic start failed: ${message}`)
    }
  }

  const handleStopListening = () => {
    dgStop()
  }

  const previewSnippet = clip((last.preview || ''), 100)
  const ttsAudienceEnabled = !isMuted
  const latencyLabel = latencyMs !== null ? `${Math.max(latencyMs, 0).toFixed(0)} ms` : 'Calibrating…'
  const micActive = isListening && status === 'streaming'
  const waveformBars = Array.from({ length: 5 }, (_, idx) => (
    <span
      key={idx}
      className="inline-flex w-1 rounded-full bg-cyan-300/80 animate-[pulse_1.2s_ease-in-out_infinite]"
      style={{
        animationDelay: `${idx * 0.12}s`,
        height: micActive ? `${8 + idx * 6}px` : '8px'
      }}
    />
  ))

  return (
    <section className="w-full space-y-8 text-slate-100">
      <div className="relative overflow-hidden rounded-3xl border border-white/10 bg-[#10172a] shadow-[0_30px_110px_rgba(0,0,0,0.55)]">
        <div className="pointer-events-none absolute inset-0 opacity-70" style={{ background: 'radial-gradient(circle at 18% 25%, rgba(0,255,255,0.08), transparent 55%)' }} />
        <div className="pointer-events-none absolute -right-28 top-8 h-72 w-72 rounded-full bg-cyan-400/15 blur-3xl" />
        <div className="relative space-y-8 p-6 md:p-10">
          <header className="flex flex-wrap items-center justify-between gap-6 border-b border-white/10 pb-6">
            <div>
              <p className="flex items-center gap-2 text-xs uppercase tracking-[0.35em] text-cyan-200">
                <span className={`inline-flex h-2 w-2 rounded-full ${isBroadcasting ? 'bg-emerald-400 animate-pulse' : 'bg-rose-400'}`} />
                Live
              </p>
              <h2 className="mt-2 text-2xl font-bold leading-tight text-white md:text-3xl">Real-Time Sermon Translation</h2>
              <p className="text-sm text-slate-300">Monitor, refine, and broadcast translations without leaving this console.</p>
            </div>
            <div className="flex flex-col items-start gap-3 text-sm md:flex-row md:items-center md:gap-4">
              <span className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 ${connected ? 'border-emerald-400/40 text-emerald-200' : 'border-rose-400/40 text-rose-200'}`}>
                <span className={`h-2 w-2 rounded-full ${connected ? 'bg-emerald-400 animate-ping' : 'bg-rose-400'}`} />
                WebSocket {connected ? 'Connected' : 'Offline'}
              </span>
              <span className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 ${status === 'streaming' ? 'border-cyan-400/40 text-cyan-100' : 'border-amber-400/40 text-amber-100'}`}>
                <span className={`h-2 w-2 rounded-full ${status === 'streaming' ? 'bg-cyan-300 animate-pulse' : 'bg-amber-400'}`} />
                Deepgram · {status}
              </span>
              <span className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1 text-slate-200">
                <span className="text-xs uppercase tracking-wide text-slate-400">Latency</span>
                <strong className="text-sm text-white">{latencyLabel}</strong>
              </span>
            </div>
          </header>

          {errorMsg && (
            <div className="rounded-2xl border border-rose-500/60 bg-rose-500/20 px-4 py-3 text-sm text-rose-100">
              {errorMsg}
            </div>
          )}

          <div className="grid gap-6 md:grid-cols-2">
            <div className="flex flex-col gap-2">
              <label className="text-xs uppercase tracking-wide text-slate-400">Source language</label>
              <div className="flex items-center gap-3 rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-white">
                <span className="text-2xl">{languageFlag(sourceLang)}</span>
                <select
                  value={sourceLang}
                  onChange={e => setSourceLang(e.target.value)}
                  className="w-full bg-transparent text-base font-semibold focus:outline-none"
                >
                  {availableLanguages.map(l => <option key={l.code} value={l.code} className="text-slate-900">{l.name}</option>)}
                </select>
              </div>
            </div>
            <div className="flex flex-col gap-2">
              <label className="text-xs uppercase tracking-wide text-slate-400">Target language</label>
              <div className="flex items-center gap-3 rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-white">
                <span className="text-2xl">{languageFlag(targetLang)}</span>
                <select
                  value={targetLang}
                  onChange={e => setTargetLang(e.target.value)}
                  className="w-full bg-transparent text-base font-semibold focus:outline-none"
                >
                  {availableLanguages.map(l => <option key={l.code} value={l.code} className="text-slate-900">{l.name}</option>)}
                </select>
              </div>
            </div>
          </div>

          <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_320px]">
            <div className="space-y-6">
              <div className="rounded-3xl border border-white/10 bg-white/5 p-5 lg:p-6">
                <div className="grid gap-6 lg:grid-cols-2">
                  <div className="space-y-4 rounded-2xl border border-white/10 bg-[#0d1424] p-5">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-xs uppercase tracking-[0.35em] text-slate-500">Live Source</p>
                        <p className="text-lg font-semibold text-white">{languageFlag(sourceLang)} {sourceLabel}</p>
                      </div>
                      <div className="flex items-center gap-3 text-sm text-slate-300">
                        <span className={`relative inline-flex h-12 w-12 items-center justify-center rounded-full border ${micActive ? 'border-cyan-400/60 bg-cyan-400/10 text-cyan-100' : 'border-slate-400/40 bg-slate-500/10 text-slate-300'}`}>
                          <span className="text-xl">🎙️</span>
                          {micActive && <span className="absolute inset-0 rounded-full border border-cyan-300/40 animate-ping" />}
                        </span>
                        <div className="flex h-10 items-end gap-1">{waveformBars}</div>
                      </div>
                    </div>
                    <textarea
                      value={text}
                      onChange={e => setText(e.target.value)}
                      placeholder="Speak into the mic or type here…"
                      className="min-h-[170px] w-full resize-none rounded-2xl border border-white/5 bg-black/30 px-4 py-3 text-base text-white placeholder:text-slate-500 focus:border-cyan-400/60 focus:outline-none"
                    />
                  </div>

                  <div className="space-y-4 rounded-2xl border border-cyan-400/30 bg-gradient-to-br from-[#0b2135] via-[#0f2c44] to-[#0b162a] p-5 text-slate-100">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-xs uppercase tracking-[0.35em] text-cyan-200">Translation Output</p>
                        <p className="text-lg font-semibold text-white">{languageFlag(targetLang)} {targetLabel}</p>
                      </div>
                      <span className="rounded-full border border-white/10 bg-white/10 px-3 py-1 text-xs uppercase tracking-wide text-white/80">Broadcast ready</span>
                    </div>
                    <p className="min-h-[170px] whitespace-pre-wrap text-xl leading-relaxed">
                      {translated || 'Waiting for the next sentence…'}
                    </p>
                    <div className="rounded-2xl border border-white/5 bg-black/20 px-4 py-3 text-sm text-slate-300">
                      <p className="text-xs uppercase tracking-[0.35em] text-slate-400">Next sentence preview</p>
                      <p className="mt-1 text-base text-white">{previewSnippet || 'Listening for the next clause…'}</p>
                    </div>
                  </div>
                </div>
              </div>

              <div className="rounded-3xl border border-white/10 bg-white/5 p-5">
                <div className="flex flex-wrap items-center gap-4">
                  <button
                    onClick={isListening ? handleStopListening : handleStartListening}
                    className={`flex-1 min-w-[200px] rounded-2xl px-6 py-3 text-lg font-semibold text-white shadow-lg transition ${isListening ? 'bg-rose-500 hover:bg-rose-600' : 'bg-cyan-500 hover:bg-cyan-400'}`}
                  >
                    {isListening ? 'Stop translation' : 'Start translation'}
                  </button>
                  <button
                    onClick={() => triggerFinalize('manual operator button')}
                    className="rounded-2xl border border-white/20 px-5 py-3 text-sm font-semibold text-white/90 hover:border-cyan-400/50"
                  >
                    Pulse finalize
                  </button>
                  <button
                    onClick={() => enqueueFinalTTS('This is a test of speech synthesis.')}
                    className="rounded-2xl border border-white/20 px-5 py-3 text-sm font-semibold text-white/90 hover:border-cyan-400/50"
                  >
                    Test TTS
                  </button>
                  <div className="ml-auto flex items-center gap-3 text-sm text-slate-300">
                    <span className="text-xs uppercase tracking-wide text-slate-400">Monitor volume</span>
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.05}
                      value={volume}
                      onChange={(e) => setVolume(parseFloat(e.target.value))}
                      className="accent-cyan-400"
                    />
                    <span className="text-white">{Math.round(volume * 100)}%</span>
                  </div>
                </div>
              </div>
            </div>

            <aside className="space-y-6 rounded-3xl border border-white/10 bg-[#0b1324] p-5">
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm font-semibold text-white">Go Live broadcast</p>
                    <p className="text-xs text-slate-400">Send translations to foyer &amp; stream overlays.</p>
                  </div>
                  <button
                    onClick={() => setIsBroadcasting(v => !v)}
                    className={`relative inline-flex h-8 w-14 items-center rounded-full ${isBroadcasting ? 'bg-emerald-500/80' : 'bg-slate-600'}`}
                    aria-pressed={isBroadcasting}
                  >
                    <span className={`inline-block h-6 w-6 rounded-full bg-white transition ${isBroadcasting ? 'translate-x-6' : 'translate-x-1'}`} />
                  </button>
                </div>
                <div className="flex items-center justify-between text-sm text-slate-300">
                  <span>Display monitors</span>
                  <button
                    onClick={() => setDisplayOnAir(v => !v)}
                    className={`text-xs font-semibold uppercase tracking-wide ${displayOnAir ? 'text-emerald-200' : 'text-slate-400'}`}
                  >
                    {displayOnAir ? 'On air' : 'Standby'}
                  </button>
                </div>
              </div>

              <div className="rounded-2xl border border-white/10 bg-white/5 p-4 text-sm text-slate-200">
                <div className="flex items-center justify-between py-2">
                  <span>AI refinement</span>
                  <button
                    onClick={() => setAiAssistEnabled(v => !v)}
                    className={`relative inline-flex h-7 w-12 items-center rounded-full ${aiAssistEnabled ? 'bg-cyan-500/70' : 'bg-slate-600'}`}
                    aria-pressed={aiAssistEnabled}
                  >
                    <span className={`inline-block h-5 w-5 rounded-full bg-white transition ${aiAssistEnabled ? 'translate-x-5' : 'translate-x-1'}`} />
                  </button>
                </div>
                <div className="flex items-center justify-between border-t border-white/10 py-2">
                  <span>Audience TTS</span>
                  <button
                    onClick={() => setIsMuted(m => !m)}
                    className={`relative inline-flex h-7 w-12 items-center rounded-full ${ttsAudienceEnabled ? 'bg-cyan-500/70' : 'bg-slate-600'}`}
                    aria-pressed={ttsAudienceEnabled}
                  >
                    <span className={`inline-block h-5 w-5 rounded-full bg-white transition ${ttsAudienceEnabled ? 'translate-x-5' : 'translate-x-1'}`} />
                  </button>
                </div>
                <div className="flex items-center justify-between border-t border-white/10 py-2">
                  <span>Stage display link</span>
                  <span className={`text-xs font-semibold uppercase tracking-wide ${displayOnAir ? 'text-cyan-200' : 'text-slate-400'}`}>
                    {displayOnAir ? 'Live' : 'Muted'}
                  </span>
                </div>
              </div>

              <div className="rounded-2xl border border-white/10 bg-white/5 p-4 text-sm">
                <p className="mb-3 text-xs uppercase tracking-wide text-slate-400">Connection health</p>
                <div className="space-y-2 text-slate-200">
                  <div className="flex items-center justify-between">
                    <span>Producer socket</span>
                    <span className="font-semibold">{connected ? 'Stable' : 'Reconnecting'}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span>Display sync</span>
                    <span className="font-semibold">{displayOnAir ? 'Mirrored' : 'Off'}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span>TTS monitor</span>
                    <span className="font-semibold">{ttsAudienceEnabled ? 'Audible' : 'Muted'}</span>
                  </div>
                </div>
              </div>

              <div className="flex flex-col gap-3 rounded-2xl border border-white/10 bg-white/5 p-4 text-sm">
                <div>
                  <p className="text-xs uppercase tracking-wide text-slate-400">Admin console</p>
                  <p className="text-slate-200">Invite operators, manage monitors, and review transcripts.</p>
                </div>
                <a
                  href="/producer"
                  target="_blank"
                  className="inline-flex items-center justify-center rounded-2xl border border-cyan-400/50 bg-cyan-500/20 px-4 py-2 text-center text-sm font-semibold text-white hover:bg-cyan-500/30"
                >
                  Launch admin hub
                </a>
              </div>
            </aside>
          </div>
        </div>
      </div>

      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
        <div className="rounded-2xl border border-white/10 bg-[#10172a] p-5 shadow-lg shadow-black/30">
          <p className="text-xs uppercase tracking-wide text-cyan-200">Scripted segments</p>
          <h3 className="mt-2 text-lg font-semibold text-white">Upload-ready workflow</h3>
          <p className="mt-3 text-sm text-slate-300">
            Drop sermon manuscripts or announcements to pre-translate, rehearse, and push to displays on cue.
          </p>
          <button className="mt-4 inline-flex items-center gap-2 rounded-2xl border border-white/10 px-4 py-2 text-sm font-semibold text-white/90 hover:border-cyan-400/40">
            📄 Import script
          </button>
        </div>

        <div className="rounded-2xl border border-white/10 bg-[#10172a] p-5 shadow-lg shadow-black/30">
          <p className="text-xs uppercase tracking-wide text-cyan-200">Hybrid workflow</p>
          <h3 className="mt-2 text-lg font-semibold text-white">Blend live + scripted</h3>
          <p className="mt-3 text-sm text-slate-300">
            Pin key phrases, Scriptures, or benedictions so they surface exactly when the speaker nears those cues.
          </p>
          <button className="mt-4 inline-flex items-center gap-2 rounded-2xl border border-white/10 px-4 py-2 text-sm font-semibold text-white/90 hover:border-cyan-400/40">
            📌 Manage cue board
          </button>
        </div>

        <div className="rounded-2xl border border-white/10 bg-[#10172a] p-5 shadow-lg shadow-black/30">
          <p className="text-xs uppercase tracking-wide text-cyan-200">Team ops</p>
          <h3 className="mt-2 text-lg font-semibold text-white">Admin &amp; monitoring</h3>
          <p className="mt-3 text-sm text-slate-300">
            Invite volunteers, assign auditorium channels, and monitor downstream displays from one command center.
          </p>
          <button className="mt-4 inline-flex items-center gap-2 rounded-2xl border border-white/10 px-4 py-2 text-sm font-semibold text-white/90 hover:border-cyan-400/40">
            🛠 Open admin console
          </button>
        </div>
      </div>
    </section>
  )
}
