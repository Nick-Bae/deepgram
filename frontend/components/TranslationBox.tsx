'use client'

import { useMemo, useState, useEffect, useRef, useCallback } from 'react'
import { throttle } from '../utils/throttle'
import { useTranslationSocket } from '../utils/useTranslationSocket'
import { API_URL } from '../utils/urls'
import { getAuthTokenFromSession, contextFromSession } from '../utils/streamContext'
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

type TTSVoiceOption = { value: string; label: string }
type TTSVoicePresetMap = Record<string, TTSVoiceOption[]>

const GOOGLE_TTS_PRESETS: TTSVoicePresetMap = {
  en: [
    { value: 'en-US-Neural2-F', label: 'Neural2 F · warm' },
    { value: 'en-US-Neural2-G', label: 'Neural2 G · broadcast' },
    { value: 'en-US-Journey-D', label: 'Journey D · storyteller' },
  ],
  ko: [
    { value: 'ko-KR-Neural2-A', label: 'Neural2 A · standard' },
    { value: 'ko-KR-Neural2-C', label: 'Neural2 C · bright female' },
  ],
  es: [
    { value: 'es-US-Neural2-A', label: 'Neural2 A · US Spanish' },
    { value: 'es-ES-Neural2-B', label: 'Neural2 B · Castilian' },
  ],
  zh: [
    { value: 'cmn-CN-Wavenet-A', label: 'Wavenet A · Mandarin' },
    { value: 'cmn-CN-Wavenet-D', label: 'Wavenet D · newsy' },
  ],
  default: [
    { value: 'en-US-Neural2-F', label: 'Neural2 F · English' },
  ],
}

const GEMINI_TTS_PRESETS: TTSVoicePresetMap = {
  en: [
    { value: 'Enceladus', label: 'Enceladus · cinematic (Gemini)' },
    { value: 'Kore', label: 'Kore · crisp (Gemini)' },
    { value: 'Zephyr', label: 'Zephyr · airy (Gemini)' },
  ],
  default: [
    { value: 'Enceladus', label: 'Enceladus · cinematic (Gemini)' },
  ],
}

const TTS_PROVIDER_OPTIONS = [
  { value: 'google', label: 'Google Cloud TTS · low latency' },
  { value: 'gemini_flash', label: 'Gemini Flash TTS · expressive' },
] as const

type TTSProvider = (typeof TTS_PROVIDER_OPTIONS)[number]['value']

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

function extractVersionBadge(label?: string | null) {
  if (!label) return undefined
  const match = label.match(/\(([^)]+)\)/)
  if (match?.[1]) {
    const trimmed = match[1].trim()
    return trimmed || undefined
  }
  const compact = label.trim()
  if (compact && compact.length <= 8 && !/\s/.test(compact)) {
    return compact
  }
  return undefined
}

const LINGER_MS = 300
const MIN_FINAL_CHARS = 10
const FINALIZE_PULSE_MS = 2600
const MANUAL_FINALIZE_MIN_GAP_MS = 1400
const MIN_FORCE_FINALIZE_CHARS = 8
const INTRO_HOLD_RE = /(한마디로\s*요약(을)?\s*하면|결론부터\s*말하자면)$/
const EOS_PUNCT_RE = /[.!?。！？…]$/
const STRIP_EOS_PUNCT_RE = /[.!?。！？…]+$/
const KOREAN_EOS_RE = /(?:습니다|입니다|합니다|했습니다|할까요|했어요|했지요|했네요|예요|이에요|에요|일까요|였어요|였습니까|입니까|됩니까|나요|군요|지요|래요|랍니다|라네요|다|아요|어요|에요)$/
const CLIENT_DRIVEN = false
const MIN_PREVIEW_CHARS = 10
const PREVIEW_THROTTLE_MS = 400
const HANGUL_CHAR_RE = /[\uac00-\ud7a3]/
const SILENT_AUDIO_DATA_URL = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AACJWAAACABAAZGF0YQAAAAA='
const DISPLAY_SPEED_MIN = 0.6
const DISPLAY_SPEED_MAX = 1.6
const DISPLAY_SPEED_STEP = 0.1
const DISPLAY_SPEED_STORAGE_KEY = 'display_speed_factor'

const clampDisplaySpeed = (value: number) =>
  Math.max(DISPLAY_SPEED_MIN, Math.min(DISPLAY_SPEED_MAX, value))

type QueuedTTS = { id: number; text: string; url: string }

type CancelableFn<Args extends unknown[] = unknown[]> = ((...args: Args) => void) & {
  cancel: () => void
}

export default function TranslationBox() {
  const {
    connected,
    connectionState,
    reconnectAttempt,
    lastSeenAt,
    disconnectStartedAt,
    last,
    sendDisplayConfig,
  } = useTranslationSocket({ isProducer: true })

  // UI state
  const [text, setText] = useState('')
  const [translated, setTranslated] = useState('')
  const [isListening, setIsListening] = useState(false)
  const [sourceLang, setSourceLang] = useState('ko')
  const [targetLang, setTargetLang] = useState('en')
  const [isMuted, setIsMuted] = useState(false)
  const [volume, setVolume] = useState(1)
  const [voicePreference, setVoicePreference] = useState('auto')
  const [ttsProvider, setTtsProvider] = useState<TTSProvider>('google')
  const [isBroadcasting, setIsBroadcasting] = useState(true)
  const [earlyCommitEnabled, setEarlyCommitEnabled] = useState(false)
  const [displaySpeed, setDisplaySpeed] = useState(1)
  const [latencyMs, setLatencyMs] = useState<number | null>(null)
  const [socketClock, setSocketClock] = useState(() => Date.now())

  const [committedLines, setCommittedLines] = useState<{ id: number; srcText: string; translated: string }[]>([])
  const [correcting, setCorrecting] = useState<number | null>(null)
  const [correctionDraft, setCorrectionDraft] = useState('')
  const [correctionSaved, setCorrectionSaved] = useState<Set<number>>(new Set())
  const sourceLabel = useMemo(() => languageName(sourceLang), [sourceLang])
  const targetLabel = useMemo(() => languageName(targetLang), [targetLang])
  const targetBaseLang = (targetLang || 'en').split('-')[0]
  const voiceOptions = useMemo(() => {
    const presets = ttsProvider === 'gemini_flash' ? GEMINI_TTS_PRESETS : GOOGLE_TTS_PRESETS
    return presets[targetBaseLang] ?? presets.default
  }, [targetBaseLang, ttsProvider])
  const scriptureMeta = useMemo(() => {
    const meta = last?.meta
    if (!meta || meta.kind !== 'scripture') return null

    const referenceEn = typeof meta.reference_en === 'string' ? meta.reference_en : undefined
    const referenceDefault = typeof meta.reference === 'string' ? meta.reference : undefined
    const referenceKo = typeof meta.reference_ko === 'string' ? meta.reference_ko : undefined
    const displayReference = referenceEn || referenceDefault || referenceKo || 'Scripture'

    const versionFull = typeof meta.version === 'string' ? meta.version : undefined
    const versionShort = extractVersionBadge(versionFull)
    const header = versionShort ? `${displayReference} (${versionShort})` : displayReference

    const sourceText = typeof meta.source_text === 'string' ? meta.source_text : undefined
    const sourceVersionFull = typeof meta.source_version === 'string' ? meta.source_version : undefined
    const sourceVersionShort = extractVersionBadge(sourceVersionFull)
    const sourceReference = referenceKo || referenceDefault
    const sourceParts: string[] = []
    if (sourceReference) sourceParts.push(sourceReference)
    const sourceBadge = sourceVersionShort || sourceVersionFull
    if (sourceBadge) sourceParts.push(sourceBadge)

    return {
      header,
      versionFull,
      sourceText,
      sourceLabel: sourceParts.join(' · ') || undefined,
    }
  }, [last])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const raw = window.localStorage.getItem(DISPLAY_SPEED_STORAGE_KEY)
    const parsed = raw ? Number(raw) : 1
    if (Number.isFinite(parsed)) {
      setDisplaySpeed(clampDisplaySpeed(parsed))
    }
  }, [])

  useEffect(() => {
    const timer = window.setInterval(() => setSocketClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!connected) return
    sendDisplayConfig(displaySpeed)
  }, [connected, displaySpeed, sendDisplayConfig])

  const applyDisplaySpeed = useCallback(
    (next: number) => {
      const clamped = clampDisplaySpeed(Number(next.toFixed(2)))
      setDisplaySpeed(clamped)
      if (typeof window !== 'undefined') {
        window.localStorage.setItem(DISPLAY_SPEED_STORAGE_KEY, String(clamped))
      }
      sendDisplayConfig(clamped)
    },
    [sendDisplayConfig]
  )

  // Deepgram mic producer
  const dgController: DeepgramProducerController & { finalize?: () => void } = useDeepgramProducer()
  const { start: dgStart, stop: dgStop, status, partial, errorMsg, inputLevel, finalize } = dgController
  const startProducer = useCallback(async () => {
    const startWithOptions = dgStart as (options?: { sourceLang?: string; targetLang?: string; earlyCommit?: boolean }) => Promise<void>
    await startWithOptions({ sourceLang, targetLang, earlyCommit: earlyCommitEnabled })
  }, [dgStart, earlyCommitEnabled, sourceLang, targetLang])
  const dgFinalize = useMemo(() => finalize ?? (() => {}), [finalize])

  // TTS refs
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const ttsQueueRef = useRef<QueuedTTS[]>([])
  const speakingRef = useRef(false)
  const lastHandledSeqRef = useRef(0)       // gate: handle each seq once (final or soft-final)
  const currentSpokenRef = useRef('')
  const pendingControllersRef = useRef<Set<AbortController>>(new Set())
  const pendingRequestsRef = useRef<Map<string, Promise<string>>>(new Map())
  const audioUnlockedRef = useRef(false)
  const ttsIdRef = useRef(0)
  const ttsEffectBootRef = useRef(false)
  const lastClauseSentRef = useRef('')
  const lastKRFromServerRef = useRef('')

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

  const previewSource = useMemo(() => {
    const rawPreview = typeof last?.preview === 'string' ? last.preview.trim() : ''
    if (!rawPreview) return ''

    const formatted = formatSourceForDisplay(rawPreview)
    return formatted || rawPreview
  }, [formatSourceForDisplay, last])
  const previewSnippet = clip(previewSource, 100)

  type FailOpenMeta = {
    fail_open?: boolean
    reason?: string
    code?: string
    message?: string
    provider?: string
  }

  const failOpenMeta = useMemo<FailOpenMeta | null>(() => {
    const meta = last?.meta as unknown
    if (meta && typeof meta === 'object' && (meta as FailOpenMeta).fail_open) {
      return meta as FailOpenMeta
    }
    return null
  }, [last])

  const failReasonLabel = useMemo(() => {
    if (!failOpenMeta) return ''
    const code = typeof failOpenMeta.code === 'string' ? failOpenMeta.code : ''
    const reason = typeof failOpenMeta.reason === 'string' ? failOpenMeta.reason : ''
    const msg = typeof failOpenMeta.message === 'string' ? failOpenMeta.message : ''
    if (code === 'insufficient_quota' || reason === 'openai_quota') return 'OpenAI quota exceeded'
    if (reason === 'timeout') return 'Translation timed out'
    if (reason === 'auth_error') return 'Translator auth error'
    return msg || 'Translation failed; output was held'
  }, [failOpenMeta])

  const endsWithSentenceBoundary = useCallback((raw: string) => {
    const trimmed = (raw || '').trim()
    if (!trimmed) return false
    const base = (sourceLang || '').split('-')[0].toLowerCase()
    if (base === 'ko') {
      const withoutPunct = trimmed.replace(STRIP_EOS_PUNCT_RE, '')
      if (!withoutPunct) return false
      return KOREAN_EOS_RE.test(withoutPunct)
    }
    return EOS_PUNCT_RE.test(trimmed)
  }, [sourceLang])


  const triggerFinalize = useCallback(
    (reason?: string) => {
      if (!dgFinalize) return
      const now = Date.now()
      if (now - lastFinalizeAtRef.current < MANUAL_FINALIZE_MIN_GAP_MS) {
        if (DEBUG && reason) console.log('[FE][finalize][skip-rate-limit]', reason)
        return
      }
      try {
        dgFinalize()
        lastFinalizeAtRef.current = now
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
      const idToken = getAuthTokenFromSession();
      const res = await fetch(`${API_URL}/api/translate`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(idToken ? { Authorization: `Bearer ${idToken}` } : {}),
        },
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

        if (s.length < MIN_PREVIEW_CHARS && !endsWithSentenceBoundary(s)) return;
        if (s.length < MIN_FINAL_CHARS && INTRO_HOLD_RE.test(s)) return;

        if (!endsWithSentenceBoundary(s)) {
          if (s === lastPreviewSentRef.current) return;
          if (Math.abs(s.length - lastPreviewSentRef.current.length) < 2) return;
        }

        if (DEBUG) console.log('[FE][preview][clause]', clip(s));
        lastPreviewSentRef.current = s;
        postTranslate(s, false);
      }, PREVIEW_THROTTLE_MS)
  , [endsWithSentenceBoundary, postTranslate]) as CancelableFn<[string]>

  const shouldEmitClause = useCallback((clean: string) => {
    const prev = lastClauseSentRef.current
    if (prev && prev.length > clean.length && prev.endsWith(clean)) {
      if (DEBUG) console.log('[FE][final][clause][skip-suffix]', clip(clean))
      return false
    }
    lastClauseSentRef.current = clean
    return true
  }, [])

  const sendFinalNow = useCallback(
    (s: string) => {
      const clean = (s || '').trim();
      if (!clean) return;
      if (!shouldEmitClause(clean)) return;

      sendPreview.cancel();

      if (CLIENT_DRIVEN) {
        if (DEBUG) console.log('[FE][final][clause]', clip(clean));
        postTranslate(clean, true);
      } else {
        if (DEBUG) console.log('[FE][final][clause][no-http]', clip(clean));
      }

      lastPreviewSentRef.current = '';
      // Avoid over-segmenting Korean by only forcing finalize at sentence boundaries.
      if (endsWithSentenceBoundary(clean)) {
        triggerFinalize('clause complete');
      }
    },
    [endsWithSentenceBoundary, postTranslate, sendPreview, shouldEmitClause, triggerFinalize]
  );

  const scheduleFinal = useCallback(() => {
    if (lingerTimerRef.current) clearTimeout(lingerTimerRef.current);
    lingerTimerRef.current = setTimeout(() => {
      const s = clauseRef.current.trim();
      if (!s) return;

      if (s.length < MIN_FINAL_CHARS && !endsWithSentenceBoundary(s)) return;
      if (s.length < MIN_FINAL_CHARS && INTRO_HOLD_RE.test(s)) return;

      sendFinalNow(s);
      clauseRef.current = '';
    }, LINGER_MS);
  }, [endsWithSentenceBoundary, sendFinalNow]);

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

  const ensureAudioElement = useCallback(() => {
    if (typeof window === 'undefined' || typeof Audio === 'undefined') return null;
    if (!audioRef.current) {
      const audio = new Audio();
      audio.preload = 'auto';
      audioRef.current = audio;
    }
    if (audioRef.current) {
      audioRef.current.volume = Math.max(0, Math.min(1, volume));
    }
    return audioRef.current;
  }, [volume]);

  const unlockAudio = useCallback(() => {
    if (audioUnlockedRef.current) return;
    const audio = ensureAudioElement();
    if (!audio) return;
    audioUnlockedRef.current = true;
    const prevMuted = audio.muted;
    audio.muted = true;
    audio.src = SILENT_AUDIO_DATA_URL;
    const attempt = audio.play();
    const reset = () => {
      try {
        audio.pause();
        audio.currentTime = 0;
      } catch {}
      audio.src = '';
      audio.muted = prevMuted || isMuted;
    };
    if (attempt && typeof attempt.finally === 'function') {
      attempt.then(reset).catch(err => {
        console.warn('[FE][TTS][unlock-failed]', err);
        audioUnlockedRef.current = false;
        reset();
      });
    } else {
      reset();
    }
  }, [ensureAudioElement, isMuted]);

  const flushTTSQueue = useCallback(() => {
    pendingControllersRef.current.forEach(ctrl => ctrl.abort());
    pendingControllersRef.current.clear();
    ttsQueueRef.current.forEach(chunk => URL.revokeObjectURL(chunk.url));
    ttsQueueRef.current = [];
    speakingRef.current = false;
    currentSpokenRef.current = '';
    const audio = audioRef.current;
    if (audio) {
      try {
        audio.pause();
        audio.currentTime = 0;
        audio.src = '';
      } catch {}
    }
  }, []);

  const fetchTTSAudio = useCallback((sentence: string) => {
    const trimmed = sentence.trim();
    if (!trimmed) return Promise.reject(new Error('TTS text missing'));

    const langPref = mapToTTSLocale(targetLang);
    const cacheKey = [ttsProvider, voicePreference || 'auto', langPref, trimmed].join('::');

    const cached = pendingRequestsRef.current.get(cacheKey);
    if (cached) return cached;

    const request = (async () => {
      const controller = new AbortController();
      pendingControllersRef.current.add(controller);
      try {
        const body: Record<string, unknown> = {
          text: trimmed,
          lang: langPref,
          provider: ttsProvider,
        }
        if (voicePreference !== 'auto') {
          body.voice = voicePreference
        }
        const idToken = getAuthTokenFromSession();

        const response = await fetch(`${API_URL}/api/tts`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(idToken ? { Authorization: `Bearer ${idToken}` } : {}),
          },
          body: JSON.stringify(body),
          signal: controller.signal,
        });
        if (!response.ok) {
          const detail = await response.text().catch(() => '');
          throw new Error(detail || `TTS failed (${response.status})`);
        }
        const buffer = await response.arrayBuffer();
        const mime = response.headers.get('content-type') || 'audio/mpeg';
        const blob = new Blob([buffer], { type: mime });
        return URL.createObjectURL(blob);
      } finally {
        pendingControllersRef.current.delete(controller);
      }
    })().finally(() => {
      pendingRequestsRef.current.delete(cacheKey);
    });

    pendingRequestsRef.current.set(cacheKey, request);
    return request;
  }, [targetLang, ttsProvider, voicePreference]);

  const playNext = useCallback(() => {
    if (speakingRef.current || isMuted) return;
    const next = ttsQueueRef.current[0];
    if (!next) return;

    const audio = ensureAudioElement();
    if (!audio) return;

    speakingRef.current = true;
    currentSpokenRef.current = next.text;

    const finalize = () => {
      const finished = ttsQueueRef.current.shift();
      if (finished) {
        URL.revokeObjectURL(finished.url);
      }
      speakingRef.current = false;
      currentSpokenRef.current = '';
      if (!isMuted && ttsQueueRef.current.length) {
        setTimeout(() => playNext(), 200);
      }
    };

    audio.onended = finalize;
    audio.onerror = (err) => {
      console.warn('[FE][TTS][error]', err);
      finalize();
    };
    audio.src = next.url;
    audio.currentTime = 0;
    const attempt = audio.play();
    if (attempt && typeof attempt.catch === 'function') {
      attempt
        .then(() => {
          console.log('[FE][TTS][start]', { text: clip(next.text) });
        })
        .catch(err => {
          console.warn('[FE][TTS][play-rejected]', err);
          finalize();
        });
    }
  }, [ensureAudioElement, isMuted]);

  const enqueueFinalTTS = useCallback((s: string) => {
    const t = s.trim();
    if (!t || isMuted) return;

    if (currentSpokenRef.current === t) {
      console.log('[FE][TTS][drop-current-dup]', clip(t));
      return;
    }
    const tail = ttsQueueRef.current[ttsQueueRef.current.length - 1];
    if (tail?.text === t) {
      console.log('[FE][TTS][drop-tail-dup]', clip(t));
      return;
    }

    unlockAudio();
    fetchTTSAudio(t)
      .then((url) => {
        const chunk: QueuedTTS = { id: ++ttsIdRef.current, text: t, url };
        ttsQueueRef.current.push(chunk);
        console.log('[FE][TTS][enqueue]', clip(t));
        if (!speakingRef.current) {
          playNext();
        }
      })
      .catch((err) => {
        console.warn('[FE][TTS][fetch-error]', err);
      });
  }, [fetchTTSAudio, isMuted, playNext, unlockAudio]);

  useEffect(() => () => flushTTSQueue(), [flushTTSQueue]);

  useEffect(() => {
    if (audioRef.current) {
      audioRef.current.volume = Math.max(0, Math.min(1, volume));
    }
  }, [volume]);

  useEffect(() => {
    if (voicePreference !== 'auto' && !voiceOptions.some(v => v.value === voicePreference)) {
      setVoicePreference('auto')
    }
  }, [voiceOptions, voicePreference])

  useEffect(() => {
    if (!ttsEffectBootRef.current) {
      ttsEffectBootRef.current = true;
      return;
    }
    if (isMuted) {
      flushTTSQueue();
    } else {
      unlockAudio();
      playNext();
    }
  }, [flushTTSQueue, isMuted, playNext, unlockAudio]);

  useEffect(() => {
    flushTTSQueue();
  }, [flushTTSQueue, targetLang, ttsProvider, voicePreference]);

  // ---------- WS: consume broadcasts (single effect, with soft-final fallback) ----------
  useEffect(() => {
    const seq = last.seq ?? 0;
    const incoming = (last.text || '').trim();
    const lastMeta = (last as typeof last & { meta?: { is_final?: boolean } }).meta;
    const isFinal = !!lastMeta?.is_final;
    const committedSrc = typeof last.srcText === 'string' ? last.srcText.trim() : '';

    if (!incoming && !committedSrc) return;

    if (incoming) {
      console.log('[FE][WS][in]', { seq, isFinal, out: clip(incoming) });
      setTranslated(incoming);
    } else if (isFinal) {
      console.log('[FE][WS][in][suppressed-target]', { seq, src: clip(committedSrc || '(none)') });
      setTranslated('');
    }

    if (isFinal) {
      if (seq && seq <= lastHandledSeqRef.current) {
        console.log('[FE][WS][final][skip-already-handled]', seq);
        return;
      }
      if (seq) lastHandledSeqRef.current = seq;

      const isDuplicateKR =
        !!committedSrc &&
        !!lastKRFromServerRef.current &&
        lastKRFromServerRef.current.length > committedSrc.length &&
        lastKRFromServerRef.current.endsWith(committedSrc);

      if (isDuplicateKR) {
        console.log('[FE][WS][dedupe-kr]', clip(committedSrc));
      } else {
        if (committedSrc) {
          lastKRFromServerRef.current = committedSrc;
        }
        if (incoming && !isMuted && incoming !== currentSpokenRef.current) {
          enqueueFinalTTS(incoming);
        } else if (incoming) {
          console.log('[FE][TTS][skip]', { isMuted, sameAsCurrent: incoming === currentSpokenRef.current });
        }
        if (incoming) {
          const srcForLog = committedSrc || clauseRef.current.trim() || lastInterimRef.current.trim()
          setCommittedLines(prev => [
            ...prev.slice(-19),
            { id: Date.now(), srcText: srcForLog, translated: incoming },
          ])
        }
      }

      const bestSourceForLog = committedSrc || clauseRef.current.trim() || lastInterimRef.current.trim()
      console.log('[FE][WS][final][ko]', { seq, src: clip(bestSourceForLog || '(none)') })
      if (incoming) {
        console.log('[FE][WS][final][en]', { seq, out: clip(incoming) })
      }
      softMapRef.current.delete(seq);

      const clauseSnapshot = clauseRef.current.trim();
      const interimSnapshot = lastInterimRef.current.trim();
      const bestSource = committedSrc || clauseSnapshot || interimSnapshot;
      if (bestSource) {
        const displaySource = formatSourceForDisplay(bestSource);
        setText(displaySource);
        if (DEBUG) {
          console.log('[FE][WS][final][src]', { seq, src: clip(displaySource) });
        }
      }
      clauseRef.current = '';
      lastInterimRef.current = '';
      if (lastSourceUpdateRef.current) {
        setLatencyMs(Date.now() - lastSourceUpdateRef.current);
      }
      return;
    }

    if (!seq || !incoming) return;
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
      if (stable && endsWithSentenceBoundary(incoming) && seq > lastHandledSeqRef.current) {
        console.log('[FE][WS][soft-final]', { seq, out: clip(incoming) });
        lastHandledSeqRef.current = seq;
        if (!isMuted && incoming !== currentSpokenRef.current) {
          enqueueFinalTTS(incoming);
        }
      }
    }
  }, [enqueueFinalTTS, endsWithSentenceBoundary, formatSourceForDisplay, isMuted, last]);

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
        const oldLooksComplete = endsWithSentenceBoundary(old) || old.length >= MIN_FINAL_CHARS + 10;
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

      if (endsWithSentenceBoundary(clauseRef.current)) {
        sendFinalNow(clauseRef.current);
        clauseRef.current = '';
      } else {
        scheduleFinal();
      }
    }

    lastInterimRef.current = cur;
    const formatted = formatSourceForDisplay(cur);
    setText(formatted || cur);
  }, [endsWithSentenceBoundary, formatSourceForDisplay, partial, scheduleFinal, sendFinalNow, sendPreview]);

  // ---------- Keep isListening in sync with Deepgram ----------
  useEffect(() => {
    const sessionActive = status === 'streaming' || status === 'starting'
    setIsListening(sessionActive)
    if (!sessionActive) {
      lastFinalizeAtRef.current = 0
    }
    if (!sessionActive && clauseRef.current.trim()) {
      sendFinalNow(clauseRef.current)
      clauseRef.current = ''
    }
  }, [sendFinalNow, status])

  useEffect(() => {
    if (status !== 'streaming') return

    const interval = setInterval(() => {
      const clause = clauseRef.current.trim()
      if (!clause) return
      if (clause.length < MIN_FORCE_FINALIZE_CHARS && !endsWithSentenceBoundary(clause)) return

      const now = Date.now()
      if (now - lastFinalizeAtRef.current < FINALIZE_PULSE_MS * 0.8) return

      triggerFinalize('interval pulse')
    }, FINALIZE_PULSE_MS)

    return () => clearInterval(interval)
  }, [endsWithSentenceBoundary, status, triggerFinalize])

  // ---------- Start/Stop mic ----------
  const handleStartListening = async () => {
    lastInterimRef.current = ''
    clauseRef.current = ''
    lastClauseSentRef.current = ''
    lastKRFromServerRef.current = ''
    setText('')
    setTranslated('')
    flushTTSQueue()
    unlockAudio()

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

  const submitCorrection = useCallback(async (line: { id: number; srcText: string; translated: string }) => {
    if (!correctionDraft.trim() || correctionDraft === line.translated) {
      setCorrecting(null)
      return
    }
    const { orgId } = contextFromSession()
    const idToken = getAuthTokenFromSession()
    try {
      await fetch(`${API_URL}/examples/correct`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(idToken ? { Authorization: `Bearer ${idToken}` } : {}),
        },
        body: JSON.stringify({
          stt_text: line.srcText,
          auto_translation: line.translated,
          final_translation: correctionDraft.trim(),
          org_id: orgId || undefined,
        }),
      })
      setCorrectionSaved(prev => new Set([...prev, line.id]))
    } catch {
      // silent fail — non-critical
    } finally {
      setCorrecting(null)
    }
  }, [correctionDraft])

  const ttsAudienceEnabled = !isMuted
  const latencyLabel = latencyMs !== null ? `${Math.max(latencyMs, 0).toFixed(0)} ms` : 'Calibrating…'
  const socketStatusLabel =
    connectionState === 'connected'
      ? 'Connected'
      : connectionState === 'disconnected'
      ? 'Disconnected'
      : 'Reconnecting...'
  const socketStatusClasses =
    connectionState === 'connected'
      ? 'border-emerald-200 bg-emerald-50/80 text-emerald-700'
      : connectionState === 'disconnected'
      ? 'border-rose-200 bg-rose-50/80 text-rose-600'
      : 'border-amber-200 bg-amber-50/80 text-amber-600'
  const socketDotClasses =
    connectionState === 'connected'
      ? 'bg-emerald-500 animate-pulse'
      : connectionState === 'disconnected'
      ? 'bg-rose-500'
      : 'bg-amber-500 animate-pulse'
  const lastHeartbeatLabel = useMemo(() => {
    if (!lastSeenAt) return 'Waiting'
    const ageSeconds = Math.max(0, Math.floor((socketClock - lastSeenAt) / 1000))
    return ageSeconds <= 0 ? 'Just now' : `${ageSeconds}s ago`
  }, [lastSeenAt, socketClock])
  const socketDowntimeLabel = useMemo(() => {
    if (!disconnectStartedAt || connected) return '0s'
    const ageSeconds = Math.max(1, Math.floor((socketClock - disconnectStartedAt) / 1000))
    return `${ageSeconds}s`
  }, [connected, disconnectStartedAt, socketClock])
  const reconnectAttemptLabel = reconnectAttempt > 0 ? `#${reconnectAttempt}` : '0'
  const micActive = isListening || inputLevel > 0.004
  const waveformActivity = micActive ? Math.min(1, Math.pow(inputLevel * 18, 0.8)) : 0
  const waveformBars = Array.from({ length: 5 }, (_, idx) => {
    const baseHeight = [10, 16, 13, 20, 11][idx] ?? 12
    const barBoost = [0.45, 0.78, 1.08, 0.8, 0.52][idx] ?? 0.6
    const barScale = micActive ? Math.min(1.16, 0.18 + waveformActivity * barBoost) : 0.18
    const barOpacity = micActive ? 0.4 + waveformActivity * 0.56 : 0.32
    return (
      <span
        key={idx}
        className="inline-flex w-1.5 rounded-full bg-gradient-to-t from-[#d7dee7] via-[#9aa9bb] to-[#334155] shadow-[0_6px_16px_rgba(100,116,139,0.24)]"
        style={{
          height: `${baseHeight}px`,
          transform: `scaleY(${barScale})`,
          transformOrigin: 'center bottom',
          opacity: barOpacity,
          transition: 'transform 90ms ease-out, opacity 120ms ease-out',
        }}
      />
    )
  })
  const shellStyle = {
    background: '#ffffff',
    boxShadow: '0 4px 24px rgba(15,31,61,0.07)',
    border: '1px solid #e4ddd2',
  } as const
  const panelStyle = {
    background: '#f7f4ef',
    boxShadow: 'none',
  } as const
  const insetStyle = {
    background: '#ffffff',
    boxShadow: 'none',
  } as const
  const accentPanelStyle = {
    background: 'rgba(184,154,94,0.06)',
    boxShadow: 'none',
  } as const
  const pillStyle = {
    background: '#ffffff',
    boxShadow: 'none',
  } as const
  const primaryActionStyle = {
    background: '#b89a5e',
    boxShadow: '0 4px 16px rgba(184,154,94,0.3)',
  } as const
  const stopActionStyle = {
    background: '#9f3650',
    boxShadow: '0 4px 16px rgba(159,54,80,0.25)',
  } as const

  return (
    <section className="w-full space-y-8 text-slate-800">
      <div className="relative overflow-hidden px-6 py-6 md:px-8 md:py-8" style={{ ...shellStyle, borderRadius: 12 }}>
        <div
          className="pointer-events-none absolute inset-0 opacity-60"
          style={{ background: 'radial-gradient(circle at 18% 18%, rgba(184,154,94,0.08), transparent 42%)' }}
        />
        <div className="pointer-events-none absolute -right-24 top-10 h-64 w-64 rounded-full blur-3xl" style={{ background: 'rgba(184,154,94,0.06)' }} />
        <div className="relative space-y-6">
          <header className="flex flex-wrap items-center justify-between gap-6 border-b border-[#e4ddd2] pb-6">
            <div>
              <p className="flex items-center gap-2 text-xs font-bold uppercase tracking-[0.28em] text-[#b89a5e]">
                <span className={`inline-flex h-2 w-2 rounded-full ${isBroadcasting ? 'bg-[#b89a5e] animate-pulse' : 'bg-[#c8bfb0]'}`} />
                Live
              </p>
              <h2 className="mt-2 font-semibold leading-tight text-[#0f1f3d]" style={{ fontFamily: "'Cormorant Garamond', Georgia, serif", fontSize: 'clamp(22px,2.6vw,30px)' }}>Real-Time Sermon Translation</h2>
              <p className="text-sm text-[#5a5a52]">Monitor, refine, and broadcast translations without leaving this console.</p>
            </div>
            <div className="flex flex-col items-start gap-3 text-sm md:flex-row md:items-center md:gap-4">
              <span className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-semibold ${socketStatusClasses}`} style={pillStyle}>
                <span className={`h-2 w-2 rounded-full ${socketDotClasses}`} />
                Producer socket · {socketStatusLabel}
              </span>
              <span
                className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-semibold ${
                  status === 'streaming'
                    ? 'border-emerald-200 bg-emerald-50/80 text-emerald-700'
                    : 'border-[#e4ddd2] bg-white text-[#5a5a52]'
                }`}
              >
                <span className={`h-2 w-2 rounded-full ${status === 'streaming' ? 'bg-emerald-500 animate-pulse' : 'bg-[#c8bfb0]'}`} />
                Deepgram · {status}
              </span>
              <span className="inline-flex items-center gap-2 rounded-full border border-[#e4ddd2] bg-white px-3 py-1.5 text-xs font-semibold text-[#5a5a52]">
                Latency · {latencyLabel}
              </span>
            </div>
          </header>

          {errorMsg && (
            <div className="rounded-2xl border border-amber-200 bg-amber-50/80 px-4 py-3 text-sm font-medium text-amber-700" style={insetStyle}>
              {errorMsg}
            </div>
          )}

          <div className="grid gap-6 md:grid-cols-2">
            <div className="flex flex-col gap-2 rounded-xl border border-[#e4ddd2] px-4 py-4" style={panelStyle}>
              <label className="text-xs font-bold uppercase tracking-[0.22em] text-[#b89a5e]">Source Language</label>
              <div className="mt-3 flex items-center gap-3">
                <span className="text-lg leading-none" style={{ display: 'flex', alignItems: 'center', lineHeight: 1 }}>{languageFlag(sourceLang)}</span>
                <select
                  value={sourceLang}
                  onChange={e => setSourceLang(e.target.value)}
                  className="w-full bg-transparent text-base font-semibold text-slate-800 focus:outline-none"
                >
                  {availableLanguages.map(l => <option key={l.code} value={l.code} className="text-slate-900">{l.name}</option>)}
                </select>
              </div>
            </div>
            <div className="flex flex-col gap-2 rounded-xl border border-[#e4ddd2] px-4 py-4" style={panelStyle}>
              <label className="text-xs font-bold uppercase tracking-[0.22em] text-[#b89a5e]">Target Language</label>
              <div className="mt-3 flex items-center gap-3">
                <span className="text-lg leading-none" style={{ display: 'flex', alignItems: 'center', lineHeight: 1 }}>{languageFlag(targetLang)}</span>
                <select
                  value={targetLang}
                  onChange={e => setTargetLang(e.target.value)}
                  className="w-full bg-transparent text-base font-semibold text-slate-800 focus:outline-none"
                >
                  {availableLanguages.map(l => <option key={l.code} value={l.code} className="text-slate-900">{l.name}</option>)}
                </select>
              </div>
            </div>
          </div>

          <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_320px] lg:items-stretch">
            <div className="space-y-6 h-full">
              <div className="rounded-xl border border-[#e4ddd2] p-5 lg:p-6 h-full" style={panelStyle}>
                <div className="grid gap-6 lg:grid-cols-2 h-full lg:items-stretch">
                  <div className="flex flex-col gap-4">
                    <div className="flex-1 space-y-4 rounded-xl border border-[#e4ddd2] p-5" style={insetStyle}>
                      <div className="flex items-center justify-between gap-4">
                        <div>
                          <p className="text-xs font-bold uppercase tracking-[0.22em] text-[#b89a5e]">Live Audio Stream</p>
                          <p className="mt-1 flex items-center gap-1.5 text-base font-semibold text-[#0f1f3d]"><span className="leading-none" style={{ lineHeight: 1 }}>{languageFlag(sourceLang)}</span><span>{sourceLabel}</span></p>
                        </div>
                        <div className="flex items-center gap-3 text-sm text-slate-500">
                          <span className={`relative inline-flex h-11 w-11 items-center justify-center rounded-full border ${micActive ? 'border-emerald-200 bg-emerald-50 text-emerald-700' : 'border-white/80 bg-white/60 text-slate-400'}`}>
                            <span className="text-lg">🎙️</span>
                            {micActive && <span className="absolute inset-0 rounded-full border border-emerald-300/60 animate-ping" />}
                          </span>
                          <div className="flex h-8 items-end gap-1.5">{waveformBars}</div>
                        </div>
                      </div>
                      <textarea
                        value={text}
                        onChange={e => setText(e.target.value)}
                        placeholder="Listening to the speaker in Korean..."
                        className="min-h-[170px] w-full resize-none rounded-lg border border-[#e4ddd2] bg-white px-4 py-3 text-base text-[#1c1c1c] placeholder:text-[#c8bfb0] focus:border-[#b89a5e] focus:outline-none"
                      />
                    </div>

                    <button
                      onClick={isListening ? handleStopListening : handleStartListening}
                      className="w-full rounded-[1.5rem] px-6 py-4 text-sm font-black uppercase tracking-[0.24em] text-white transition"
                      style={isListening ? stopActionStyle : primaryActionStyle}
                    >
                      {isListening ? 'Stop Translation' : 'Start Translation'}
                    </button>
                  </div>

                  <div className="flex flex-col gap-4 rounded-xl border border-[#e4ddd2] p-5 text-[#1c1c1c]" style={accentPanelStyle}>
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-xs font-bold uppercase tracking-[0.22em] text-[#b89a5e]">Translation Output</p>
                        <p className="flex items-center gap-1.5 text-base font-semibold text-[#0f1f3d]"><span className="leading-none" style={{ lineHeight: 1 }}>{languageFlag(targetLang)}</span><span>{targetLabel}</span></p>
                      </div>
                      <span className="rounded-full border border-[#e4ddd2] bg-white px-3 py-1 text-xs font-bold uppercase tracking-[0.18em] text-[#b89a5e]">Broadcast Ready</span>
                    </div>
                    {failOpenMeta && (
                      <div className="rounded-xl border border-amber-200 bg-amber-50/80 px-3 py-2 text-sm font-medium text-amber-700" style={insetStyle}>
                        Translation temporarily unavailable. {failReasonLabel}
                      </div>
                    )}
                    <p className={`min-h-[100px] max-h-[220px] overflow-y-auto whitespace-pre-wrap font-medium leading-relaxed text-[#0f1f3d] ${translated.length > 120 ? 'text-base' : translated.length > 60 ? 'text-lg' : 'text-xl'}`}>
                      {translated || 'Waiting for the next sentence...'}
                    </p>
                    {scriptureMeta && (
                      <div className="rounded-xl border border-[#e4ddd2] px-4 py-4 text-[#1c1c1c]" style={insetStyle}>
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <div>
                            <p className="text-xs font-bold uppercase tracking-[0.22em] text-[#b89a5e]">Scripture Reference</p>
                            <p className="text-base font-semibold text-[#0f1f3d]" title={scriptureMeta.versionFull || undefined}>
                              {scriptureMeta.header}
                            </p>
                          </div>
                          <span className="rounded-full border border-[#e4ddd2] bg-white px-3 py-1 text-xs font-bold uppercase tracking-[0.18em] text-[#b89a5e]">
                            Exact Verse
                          </span>
                        </div>
                        {scriptureMeta.sourceText && (
                          <div className="mt-3 rounded-lg border border-[#e4ddd2] bg-[#f7f4ef] px-3 py-2 text-sm">
                            <p className="text-xs font-bold uppercase tracking-[0.2em] text-[#b89a5e]">
                              {scriptureMeta.sourceLabel || 'Korean Source'}
                            </p>
                            <p className="mt-1 text-base text-[#1c1c1c]">{scriptureMeta.sourceText}</p>
                          </div>
                        )}
                      </div>
                    )}
                    <div className="rounded-xl border border-[#e4ddd2] px-4 py-3 text-sm text-[#5a5a52]" style={insetStyle}>
                      <p className="text-xs font-bold uppercase tracking-[0.22em] text-[#b89a5e]">Next Sentence Preview</p>
                      <p className="mt-1 text-base text-[#1c1c1c]">{previewSnippet || 'Listening for the next clause...'}</p>
                    </div>
                    {committedLines.length > 0 && (
                      <div className="rounded-xl border border-[#e4ddd2] px-4 py-3" style={insetStyle}>
                        <p className="mb-2 text-xs font-bold uppercase tracking-[0.22em] text-[#b89a5e]">Recent Translations</p>
                        <div className="max-h-48 space-y-2 overflow-y-auto">
                          {[...committedLines].reverse().map(line => (
                            <div key={line.id} className="group flex items-start gap-2">
                              <div className="min-w-0 flex-1">
                                <p className="truncate text-xs text-[#c8bfb0]">{line.srcText}</p>
                                {correcting === line.id ? (
                                  <div className="mt-1 flex gap-1">
                                    <input
                                      className="flex-1 rounded border border-slate-300 px-2 py-0.5 text-sm text-slate-800 focus:outline-none focus:border-slate-400"
                                      value={correctionDraft}
                                      onChange={e => setCorrectionDraft(e.target.value)}
                                      onKeyDown={e => { if (e.key === 'Enter') submitCorrection(line); if (e.key === 'Escape') setCorrecting(null); }}
                                      autoFocus
                                    />
                                    <button
                                      onClick={() => submitCorrection(line)}
                                      className="rounded px-2 py-0.5 text-xs text-white"
                                      style={{ background: '#b89a5e' }}
                                    >Save</button>
                                    <button
                                      onClick={() => setCorrecting(null)}
                                      className="rounded px-2 py-0.5 text-xs text-slate-500 hover:text-slate-700"
                                    >Cancel</button>
                                  </div>
                                ) : (
                                  <p className={`text-sm ${correctionSaved.has(line.id) ? 'text-green-700' : 'text-slate-700'}`}>
                                    {line.translated}
                                    {correctionSaved.has(line.id) && <span className="ml-1 text-xs text-green-600">✓</span>}
                                  </p>
                                )}
                              </div>
                              {correcting !== line.id && !correctionSaved.has(line.id) && (
                                <button
                                  title="Correct this translation"
                                  className="mt-0.5 shrink-0 text-xs text-slate-300 opacity-0 transition-opacity hover:text-slate-600 group-hover:opacity-100"
                                  onClick={() => { setCorrecting(line.id); setCorrectionDraft(line.translated); }}
                                >✎</button>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>

            <aside className="space-y-6 rounded-xl border border-[#e4ddd2] p-5" style={panelStyle}>
              <div className="space-y-4 rounded-xl border border-[#e4ddd2] p-4" style={insetStyle}>
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-semibold text-[#0f1f3d]">Broadcast Output</p>
                    <p className="text-xs text-[#5a5a52]">Enable or mute stage and display feeds.</p>
                  </div>
                  <button
                    onClick={() => setIsBroadcasting(v => !v)}
                    className={`relative inline-flex h-8 w-14 items-center rounded-full ${isBroadcasting ? 'bg-[#0f1f3d]' : 'bg-[#d4ccc2]'}`}
                    aria-pressed={isBroadcasting}
                  >
                    <span className={`inline-block h-6 w-6 rounded-full bg-white transition ${isBroadcasting ? 'translate-x-6' : 'translate-x-1'}`} />
                  </button>
                </div>
                <div className="flex items-center justify-between border-t border-[#e4ddd2] pt-3 text-sm text-[#5a5a52]">
                  <div className="flex flex-col">
                    <span>Display speed</span>
                    <span className="text-xs text-[#c8bfb0]">{displaySpeed.toFixed(2)}x</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => applyDisplaySpeed(displaySpeed + DISPLAY_SPEED_STEP)}
                      className="rounded-xl border border-white/80 px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:border-slate-300"
                      style={pillStyle}
                    >
                      Slower
                    </button>
                    <button
                      onClick={() => applyDisplaySpeed(displaySpeed - DISPLAY_SPEED_STEP)}
                      className="rounded-xl border border-white/80 px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:border-slate-300"
                      style={pillStyle}
                    >
                      Faster
                    </button>
                  </div>
                </div>
              </div>

              <div className="rounded-xl border border-[#e4ddd2] p-4 text-sm" style={insetStyle}>
                <p className="mb-3 text-xs font-bold uppercase tracking-[0.22em] text-[#b89a5e]">System Status</p>
                <div className="space-y-2 text-[#5a5a52]">
                  <div className="flex items-center justify-between">
                    <span>Last heartbeat</span>
                    <span className="font-semibold text-[#0f1f3d]">{lastHeartbeatLabel}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span>Reconnect attempt</span>
                    <span className="font-semibold text-[#0f1f3d]">{reconnectAttemptLabel}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span>Socket downtime</span>
                    <span className="font-semibold text-[#0f1f3d]">{socketDowntimeLabel}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span>Deepgram engine</span>
                    <span className="font-semibold text-[#0f1f3d]">{status}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span>Latency</span>
                    <span className="font-semibold text-[#0f1f3d]">{latencyLabel}</span>
                  </div>
                </div>
              </div>

              <details className="rounded-xl border border-[#e4ddd2] p-4 text-sm text-[#5a5a52]" style={insetStyle}>
                <summary className="cursor-pointer text-xs font-bold uppercase tracking-[0.22em] text-[#b89a5e]">
                  Advanced Controls
                </summary>
                <div className="mt-4 space-y-4">
                  <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-2 rounded-xl border border-[#e4ddd2] px-4 py-4" style={panelStyle}>
                    <div className="min-w-0">
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="text-sm font-semibold text-[#0f1f3d]">Early Preview</span>
                        <span
                          className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-slate-300 bg-white/75 text-[10px] font-black text-slate-500"
                          title="Shows faster preview text before the clause is finalized. Lower latency, but less stable and sometimes less accurate."
                          aria-label="Shows faster preview text before the clause is finalized. Lower latency, but less stable and sometimes less accurate."
                        >
                          ?
                        </span>
                      </div>
                      <p className="mt-1 text-xs leading-5 text-[#5a5a52]">
                        Shows translation sooner, but the wording may shift before the final sentence is committed.
                      </p>
                    </div>
                    <button
                      onClick={() => setEarlyCommitEnabled(v => !v)}
                      title="Shows faster preview text before the clause is finalized. Lower latency, but less stable and sometimes less accurate."
                      aria-label="Toggle early preview translation"
                      className={`relative inline-flex h-8 w-14 min-w-[56px] shrink-0 items-center rounded-full transition ${earlyCommitEnabled ? 'bg-[#0f1f3d]' : 'bg-[#d4ccc2]'}`}
                      aria-pressed={earlyCommitEnabled}
                    >
                      <span className={`inline-block h-6 w-6 rounded-full bg-white transition ${earlyCommitEnabled ? 'translate-x-7' : 'translate-x-1'}`} />
                    </button>
                  </div>
                  <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-2 rounded-xl border border-[#e4ddd2] px-4 py-4" style={panelStyle}>
                    <div className="min-w-0">
                      <span className="text-sm font-semibold text-[#0f1f3d]">Audience TTS</span>
                      <p className="mt-1 text-xs leading-5 text-[#5a5a52]">
                        Plays the translated sentence through the local monitor output as each final line is committed.
                      </p>
                    </div>
                    <button
                      onClick={() => setIsMuted(m => !m)}
                      className={`relative inline-flex h-8 w-14 min-w-[56px] shrink-0 items-center rounded-full transition ${ttsAudienceEnabled ? 'bg-[#0f1f3d]' : 'bg-[#d4ccc2]'}`}
                      aria-pressed={ttsAudienceEnabled}
                    >
                      <span className={`inline-block h-6 w-6 rounded-full bg-white transition ${ttsAudienceEnabled ? 'translate-x-7' : 'translate-x-1'}`} />
                    </button>
                  </div>
                  <div className="rounded-xl border border-[#e4ddd2] px-4 py-3" style={panelStyle}>
                    <label className="text-[10px] font-bold uppercase tracking-[0.22em] text-[#b89a5e]">Voice engine</label>
                    <select
                      value={ttsProvider}
                      onChange={(e) => setTtsProvider(e.target.value as TTSProvider)}
                      className="mt-2 w-full rounded-lg border border-[#e4ddd2] bg-white px-3 py-2 text-sm font-medium text-[#1c1c1c] focus:border-[#b89a5e] focus:outline-none"
                    >
                      {TTS_PROVIDER_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="rounded-xl border border-[#e4ddd2] px-4 py-3" style={panelStyle}>
                    <label className="text-[10px] font-bold uppercase tracking-[0.22em] text-[#b89a5e]">Voice preset</label>
                    <select
                      value={voicePreference}
                      onChange={(e) => setVoicePreference(e.target.value)}
                      className="mt-2 w-full rounded-lg border border-[#e4ddd2] bg-white px-3 py-2 text-sm font-medium text-[#1c1c1c] focus:border-[#b89a5e] focus:outline-none"
                    >
                      <option value="auto">Auto · match language</option>
                      {voiceOptions.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                          {opt.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="rounded-xl border border-[#e4ddd2] px-4 py-3" style={panelStyle}>
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-[10px] font-bold uppercase tracking-[0.22em] text-[#b89a5e]">Monitor volume</span>
                      <span className="font-semibold text-[#0f1f3d]">{Math.round(volume * 100)}%</span>
                    </div>
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.05}
                      value={volume}
                      onChange={(e) => setVolume(parseFloat(e.target.value))}
                      className="mt-3 w-full accent-[#b89a5e]"
                    />
                  </div>
                  <div className="grid gap-2">
                    <button
                      onClick={() => triggerFinalize('manual operator button')}
                      className="w-full rounded-lg border border-[#e4ddd2] bg-white px-4 py-2 text-xs font-semibold text-[#5a5a52] hover:border-[#b89a5e]"
                      style={pillStyle}
                    >
                      Pulse finalize
                    </button>
                    <button
                      onClick={() => enqueueFinalTTS('This is a test of speech synthesis.')}
                      className="w-full rounded-lg border border-[#e4ddd2] bg-white px-4 py-2 text-xs font-semibold text-[#5a5a52] hover:border-[#b89a5e]"
                      style={pillStyle}
                    >
                      Test TTS
                    </button>
                  </div>
                </div>
              </details>
            </aside>
          </div>
        </div>
      </div>
    </section>
  )
}
