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
      // Only handle first true-final per seq
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
      // We handled a real final; clear any soft cache for this seq
      softMapRef.current.delete(seq);

      const clauseSnapshot = clauseRef.current.trim();
      const interimSnapshot = lastInterimRef.current.trim();
      const bestSource = committedSrc || clauseSnapshot || interimSnapshot;
      if (bestSource) {
        setText(formatSourceForDisplay(bestSource));
      }
      clauseRef.current = '';
      lastInterimRef.current = '';
      return;
    }

    // Soft-final fallback: when finals aren’t flagged by backend
    if (!seq) return; // require seq to avoid accidental repeats
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

      // Consider it "stable enough" if repeated at least twice, OR lingered > 900ms
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

  return (
    <section className="w-full">
      <div className="relative overflow-hidden rounded-3xl border border-white/60 bg-white shadow-[0_25px_60px_rgba(15,23,42,0.18)]">
        <div className="pointer-events-none absolute inset-0 opacity-70" style={{ background: 'radial-gradient(circle at 20% 20%, rgba(56,189,248,0.18), transparent 50%)' }} />
        <div className="pointer-events-none absolute -right-32 top-0 h-64 w-64 rounded-full bg-cyan-200/30 blur-3xl" />
        <div className="relative p-6 md:p-10 space-y-8">
          <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
            <div>
              <p className="text-sm uppercase tracking-widest text-slate-400">Live producer console</p>
              <h2 className="text-2xl md:text-3xl font-bold text-slate-900">Translation &amp; Broadcast Hub</h2>
            </div>
            <div className="flex flex-wrap gap-3">
              <span className={`inline-flex items-center gap-2 rounded-full px-4 py-1.5 text-sm font-semibold ${connected ? 'bg-emerald-100 text-emerald-700' : 'bg-rose-100 text-rose-600'}`}>
                <span className={`h-2 w-2 rounded-full ${connected ? 'bg-emerald-500 animate-pulse' : 'bg-rose-500'}`} />
                WebSocket {connected ? 'Connected' : 'Offline'}
              </span>
              <span className={`inline-flex items-center gap-2 rounded-full px-4 py-1.5 text-sm font-semibold ${status === 'streaming' ? 'bg-sky-100 text-sky-700' : 'bg-amber-100 text-amber-700'}`}>
                <span className={`h-2 w-2 rounded-full ${status === 'streaming' ? 'bg-sky-500 animate-pulse' : 'bg-amber-500'}`} />
                Deepgram: {status}
              </span>
            </div>
          </div>

          {errorMsg && (
            <div className="rounded-2xl border border-rose-100 bg-rose-50 px-4 py-3 text-sm text-rose-700">
              {errorMsg}
            </div>
          )}

          <div className="grid gap-6 md:grid-cols-2">
            <div className="flex flex-col gap-2">
              <label className="text-sm font-semibold text-slate-600">Source language</label>
              <select
                value={sourceLang}
                onChange={e => setSourceLang(e.target.value)}
                className="rounded-2xl border border-slate-200 bg-white px-4 py-3 text-slate-800 shadow-sm focus:border-slate-400 focus:outline-none"
              >
                {availableLanguages.map(l => <option key={l.code} value={l.code}>{l.name}</option>)}
              </select>
            </div>
            <div className="flex flex-col gap-2">
              <label className="text-sm font-semibold text-slate-600">Target language</label>
              <select
                value={targetLang}
                onChange={e => setTargetLang(e.target.value)}
                className="rounded-2xl border border-slate-200 bg-white px-4 py-3 text-slate-800 shadow-sm focus:border-slate-400 focus:outline-none"
              >
                {availableLanguages.map(l => <option key={l.code} value={l.code}>{l.name}</option>)}
              </select>
            </div>
          </div>

          <div className="grid gap-6 lg:grid-cols-[1.15fr,0.85fr]">
            <div className="space-y-6">
              <div className="rounded-2xl border border-slate-100 bg-slate-50/60 p-4 shadow-inner">
                <div className="flex items-center justify-between mb-3">
                  <div>
                    <p className="text-xs uppercase tracking-wide text-slate-500">Live transcript</p>
                    <p className="text-sm font-medium text-slate-700">{sourceLabel} input</p>
                  </div>
                  <span className="text-xs text-slate-400">Auto-updated</span>
                </div>
                <textarea
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  className="h-40 w-full resize-none rounded-xl border border-slate-200 bg-white px-4 py-3 text-slate-800 shadow-sm focus:border-slate-400 focus:outline-none"
                  placeholder="Speak into the mic or type here..."
                />
              </div>

              <div className="rounded-2xl border border-slate-100 bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900 p-5 text-slate-100 shadow-lg">
                <div className="flex items-center justify-between mb-4">
                  <div>
                    <p className="text-xs uppercase tracking-[0.3em] text-slate-400">Translation</p>
                    <p className="text-lg font-semibold">{targetLabel} output</p>
                  </div>
                  <span className="text-xs text-slate-400">Broadcast ready</span>
                </div>
                <p className="min-h-[110px] whitespace-pre-wrap text-lg leading-relaxed">
                  {translated || 'Waiting for the next sentence...'}
                </p>
              </div>
            </div>

            <div className="space-y-6">
              <div className="rounded-2xl border border-slate-100 bg-white p-5 shadow-sm">
                <p className="text-sm font-semibold text-slate-700 mb-4">Session controls</p>
                <div className="flex flex-wrap gap-3">
                  <button
                    onClick={isListening ? handleStopListening : handleStartListening}
                    className={`flex-1 min-w-[160px] rounded-2xl px-5 py-3.5 text-base font-semibold text-white shadow-lg transition ${isListening ? 'bg-rose-500 hover:bg-rose-600' : 'bg-sky-600 hover:bg-sky-500'}`}
                  >
                    {isListening ? '🛑 Stop Listening' : '🎤 Start Listening'}
                  </button>
                  <button
                    onClick={() => enqueueFinalTTS('This is a test of speech synthesis.')}
                    className="flex-1 min-w-[150px] rounded-2xl border border-slate-200 px-4 py-3 text-sm font-semibold text-slate-700 hover:border-slate-300"
                  >
                    🔊 Test TTS
                  </button>
                </div>
                <div className="mt-4 flex flex-wrap gap-4 text-sm text-slate-500">
                  <div className="flex items-center gap-2">
                    <span className="inline-flex h-8 w-8 items-center justify-center rounded-xl bg-slate-100 text-base">{isMuted ? '🔇' : '🔈'}</span>
                    <button
                      onClick={() => setIsMuted(m => !m)}
                      className="font-medium text-slate-700 underline-offset-4 hover:underline"
                    >
                      {isMuted ? 'Unmute monitoring' : 'Mute monitoring'}
                    </button>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">Volume</span>
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.1}
                      value={volume}
                      onChange={(e) => setVolume(parseFloat(e.target.value))}
                      className="h-1 w-32 rounded-full accent-sky-500"
                    />
                  </div>
                </div>
              </div>

              <div className="rounded-2xl border border-slate-100 bg-white p-5 shadow-sm">
                <p className="text-sm font-semibold text-slate-700 mb-3">Voice for speech synthesis</p>
                <select
                  value={selectedVoiceName}
                  onChange={(e) => setSelectedVoiceName(e.target.value)}
                  className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-slate-800 shadow-sm focus:border-slate-400 focus:outline-none"
                >
                  {synthRef.current && synthRef.current.getVoices().map((v: SpeechSynthesisVoice) => (
                    <option key={v.name} value={v.name}>
                      {v.name} ({v.lang})
                    </option>
                  ))}
                </select>
                <p className="mt-2 text-xs text-slate-400">
                  Chrome voices refresh automatically when new languages become available.
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
