'use client'

import { useMemo, useState, useEffect, useRef, useCallback } from 'react'
import type { CSSProperties } from 'react'
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
    { value: 'en-US-Standard-D', label: 'Standard D · male · lowest latency' },
    { value: 'en-US-Neural2-D', label: 'Neural2 D · male · fast narrator' },
    { value: 'en-US-Neural2-A', label: 'Neural2 A · male · fast clear' },
    { value: 'en-US-Neural2-I', label: 'Neural2 I · male · fast bright' },
    { value: 'en-US-Neural2-J', label: 'Neural2 J · male · fast steady' },
    { value: 'en-US-Wavenet-D', label: 'Wavenet D · male · natural' },
  ],
  ko: [
    { value: 'ko-KR-Neural2-A', label: 'Neural2 A · standard' },
    { value: 'ko-KR-Neural2-B', label: 'Neural2 B · bright female' },
    { value: 'ko-KR-Neural2-C', label: 'Neural2 C · steady male' },
  ],
  es: [
    { value: 'es-US-Neural2-A', label: 'Neural2 A · US Spanish' },
    { value: 'es-ES-Neural2-A', label: 'Neural2 A · Castilian female' },
    { value: 'es-ES-Neural2-F', label: 'Neural2 F · Castilian male' },
  ],
  zh: [
    { value: 'cmn-CN-Wavenet-A', label: 'Wavenet A · Mandarin' },
    { value: 'cmn-CN-Wavenet-D', label: 'Wavenet D · newsy' },
  ],
  default: [
    { value: 'en-US-Standard-D', label: 'Standard D · male · lowest latency' },
  ],
}

const TTS_PROVIDER_OPTIONS = [
  { value: 'google', label: 'Google Cloud TTS · low latency' },
] as const

type TTSProvider = (typeof TTS_PROVIDER_OPTIONS)[number]['value']
type BroadcastIssueTone = 'warning' | 'critical'
type BroadcastIssue = {
  id: string
  tone: BroadcastIssueTone
  title: string
  detail: string
}

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
const DISPLAY_SPEED_STEP = 0.05
const DISPLAY_SPEED_STORAGE_KEY = 'display_speed_factor'
const DEEPGRAM_WARNING_DELAY_MS = 3000

const clampDisplaySpeed = (value: number) =>
  Math.max(DISPLAY_SPEED_MIN, Math.min(DISPLAY_SPEED_MAX, value))

type QueuedTTS = { id: number; text: string; url: string }

type CancelableFn<Args extends unknown[] = unknown[]> = ((...args: Args) => void) & {
  cancel: () => void
}

type TranslationBoxProps = {
  autoStartSignal: number
  roomId: string
  sourceLang: string
  targetLang: string
  onAutoStartComplete?: () => void
  onAutoStartFailed?: (message: string) => void
  onSourceLangChange: (value: string) => void
  onTargetLangChange: (value: string) => void
}

export default function TranslationBox({
  autoStartSignal,
  roomId,
  sourceLang,
  targetLang,
  onAutoStartComplete,
  onAutoStartFailed,
  onSourceLangChange,
  onTargetLangChange,
}: TranslationBoxProps) {
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
  const [isMuted, setIsMuted] = useState(false)
  const [volume, setVolume] = useState(1)
  const [voicePreference, setVoicePreference] = useState('en-US-Standard-D')
  const [ttsProvider, setTtsProvider] = useState<TTSProvider>('google')
  const [isBroadcasting, setIsBroadcasting] = useState(true)
  const [earlyCommitEnabled, setEarlyCommitEnabled] = useState(false)
  const [displaySpeed, setDisplaySpeed] = useState(1)
  const [latencyMs, setLatencyMs] = useState<number | null>(null)
  const [socketClock, setSocketClock] = useState(() => Date.now())
  const [deepgramStartingAt, setDeepgramStartingAt] = useState<number | null>(null)

  const [committedLines, setCommittedLines] = useState<{ id: number; srcText: string; translated: string }[]>([])
  const [correcting, setCorrecting] = useState<number | null>(null)
  const [correctionDraft, setCorrectionDraft] = useState('')
  const [correctionSaved, setCorrectionSaved] = useState<Set<number>>(new Set())
  const [showAdvancedControls, setShowAdvancedControls] = useState(false)
  const [hoveredSourceAction, setHoveredSourceAction] = useState<'mute' | 'listen' | null>(null)
  const sourceLabel = useMemo(() => languageName(sourceLang), [sourceLang])
  const targetLabel = useMemo(() => languageName(targetLang), [targetLang])
  const targetBaseLang = (targetLang || 'en').split('-')[0]
  const voiceOptions = useMemo(() => {
    return GOOGLE_TTS_PRESETS[targetBaseLang] ?? GOOGLE_TTS_PRESETS.default
  }, [targetBaseLang])
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
  const dgController: DeepgramProducerController = useDeepgramProducer()
  const { start: dgStart, stop: dgStop, status, partial, errorMsg, inputLevel, inputMuted, setInputMuted, finalize } = dgController
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
  const lastFinalTranslatedAtRef = useRef(0) // timestamp of last final translation display
  const currentSpokenRef = useRef('')
  const pendingControllersRef = useRef<Set<AbortController>>(new Set())
  const pendingRequestsRef = useRef<Map<string, Promise<string>>>(new Map())
  const audioUnlockedRef = useRef(false)
  const ttsIdRef = useRef(0)
  const ttsEffectBootRef = useRef(false)
  const autoStartHandledRef = useRef(0)
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
    const t = s.trim().replace(/\[\[T\d+\]\]/g, '').replace(/  +/g, ' ').trim();
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
      if (isFinal) {
        lastFinalTranslatedAtRef.current = Date.now();
        setTranslated(incoming);
      } else {
        const dwellElapsed = Date.now() - lastFinalTranslatedAtRef.current;
        if (dwellElapsed >= 1500 || incoming.length >= 30) {
          setTranslated(incoming);
        }
      }
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
      clauseRef.current += delta;
      console.log('[FE][clause][delta]', clip(clauseRef.current));

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
    if (status === 'starting') {
      setDeepgramStartingAt(prev => prev ?? Date.now())
      return
    }
    setDeepgramStartingAt(null)
  }, [status])

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
  const handleStartListening = useCallback(async (options?: { suppressAlert?: boolean; onError?: (message: string) => void }) => {
    lastInterimRef.current = ''
    clauseRef.current = ''
    lastClauseSentRef.current = ''
    lastKRFromServerRef.current = ''
    lastFinalTranslatedAtRef.current = 0
    setText('')
    setTranslated('')
    flushTTSQueue()
    unlockAudio()

    try {
      await startProducer()
      return true
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : String(e)
      options?.onError?.(message)
      if (!options?.suppressAlert) {
        alert(`Mic start failed: ${message}`)
      }
      return false
    }
  }, [flushTTSQueue, startProducer, unlockAudio])

  const handleStopListening = () => {
    dgStop()
  }

  const handleToggleInputMuted = useCallback(() => {
    if (!isListening) return
    setInputMuted(!inputMuted)
  }, [inputMuted, isListening, setInputMuted])

  useEffect(() => {
    if (!roomId || autoStartSignal <= 0) return
    if (autoStartHandledRef.current === autoStartSignal) return

    autoStartHandledRef.current = autoStartSignal
    let cancelled = false
    const timer = window.setTimeout(() => {
      void handleStartListening({
        suppressAlert: true,
        onError: (message) => {
          if (!cancelled) onAutoStartFailed?.(message)
        },
      }).then((started) => {
        if (cancelled || !started) return
        onAutoStartComplete?.()
      })
    }, 0)

    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [autoStartSignal, handleStartListening, onAutoStartComplete, onAutoStartFailed, roomId])

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
  const palette = {
    cloud: '#F2F3F4',
    sand: '#DED1C6',
    rose: '#A77693',
    navy: '#174871',
    deep: '#0F2D4D',
    ink: '#102238',
    muted: '#627186',
  } as const
  const socketStatusLabel =
    connectionState === 'connected'
      ? 'Connected'
      : connectionState === 'disconnected'
      ? 'Disconnected'
      : 'Reconnecting...'
  const socketStatusStyle =
    connectionState === 'connected'
      ? { background: 'rgba(91,179,130,0.14)', boxShadow: 'inset 0 0 0 1px rgba(91,179,130,0.20)', color: '#2f6d4f' }
      : connectionState === 'disconnected'
      ? { background: 'rgba(188,95,111,0.12)', boxShadow: 'inset 0 0 0 1px rgba(188,95,111,0.18)', color: '#8a2720' }
      : { background: 'rgba(198,165,109,0.14)', boxShadow: 'inset 0 0 0 1px rgba(198,165,109,0.22)', color: '#7a5c20' }
  const socketDotStyle =
    connectionState === 'connected'
      ? { background: '#6ee7a7' }
      : connectionState === 'disconnected'
      ? { background: '#fda4af' }
      : { background: '#fbbf24' }
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
  const deepgramDowntimeLabel = useMemo(() => {
    if (!deepgramStartingAt || status !== 'starting') return '0s'
    const ageSeconds = Math.max(1, Math.floor((socketClock - deepgramStartingAt) / 1000))
    return `${ageSeconds}s`
  }, [deepgramStartingAt, socketClock, status])
  const deepgramRecovering =
    status === 'starting' &&
    !!deepgramStartingAt &&
    socketClock - deepgramStartingAt >= DEEPGRAM_WARNING_DELAY_MS
  const broadcastIssues = useMemo<BroadcastIssue[]>(() => {
    const issues: BroadcastIssue[] = []

    if (connectionState === 'disconnected') {
      issues.push({
        id: 'translation-socket-disconnected',
        tone: 'critical',
        title: 'Live feed disconnected',
        detail: `The broadcast socket has been offline for ${socketDowntimeLabel}. Listeners may stop receiving live updates until the app reconnects.`,
      })
    } else if (connectionState === 'reconnecting') {
      issues.push({
        id: 'translation-socket-reconnecting',
        tone: 'warning',
        title: 'Reconnecting to the live feed',
        detail: `The app is retrying the broadcast socket${disconnectStartedAt ? ` after ${socketDowntimeLabel} offline` : ''}${reconnectAttempt > 0 ? ` (attempt ${reconnectAttemptLabel})` : ''}.`,
      })
    }

    if (status === 'error' && errorMsg) {
      issues.push({
        id: 'deepgram-error',
        tone: 'critical',
        title: 'Microphone or Deepgram connection failed',
        detail: errorMsg,
      })
    } else if (deepgramRecovering) {
      issues.push({
        id: 'deepgram-reconnecting',
        tone: 'warning',
        title: 'Reconnecting microphone and speech engine',
        detail: `Audio capture is still reconnecting after ${deepgramDowntimeLabel}.`,
      })
    }

    if (failOpenMeta) {
      issues.push({
        id: 'translation-provider',
        tone: 'warning',
        title: 'Translation output is temporarily paused',
        detail: failReasonLabel,
      })
    }

    return issues
  }, [
    connectionState,
    deepgramDowntimeLabel,
    deepgramRecovering,
    disconnectStartedAt,
    errorMsg,
    failOpenMeta,
    failReasonLabel,
    reconnectAttempt,
    reconnectAttemptLabel,
    socketDowntimeLabel,
    status,
  ])
  const hasCriticalBroadcastIssue = broadcastIssues.some(issue => issue.tone === 'critical')
  const broadcastAlertStyle = hasCriticalBroadcastIssue
    ? {
        background: 'linear-gradient(180deg, rgba(253,232,236,0.96), rgba(255,245,246,0.92))',
        boxShadow: 'inset 0 0 0 1px rgba(188,95,111,0.22), 0 18px 34px rgba(159,54,80,0.08)',
        color: '#7d1d32',
      }
    : {
        background: 'linear-gradient(180deg, rgba(255,248,231,0.96), rgba(255,251,242,0.92))',
        boxShadow: 'inset 0 0 0 1px rgba(198,165,109,0.24), 0 18px 34px rgba(122,92,32,0.08)',
        color: '#7a5c20',
      }
  const broadcastAlertBadgeStyle = hasCriticalBroadcastIssue
    ? { background: 'rgba(188,95,111,0.14)', color: '#8a2720' }
    : { background: 'rgba(198,165,109,0.16)', color: '#7a5c20' }
  const micActive = !inputMuted && (isListening || inputLevel > 0.004)
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
  const pairedPanelStyle = {
    borderRadius: 24,
    padding: '18px 18px 16px',
    background: 'linear-gradient(180deg, rgba(255,255,255,0.90), rgba(248,250,252,0.82))',
    boxShadow: '0 18px 40px rgba(15,45,77,0.08)',
    backdropFilter: 'blur(18px)',
    WebkitBackdropFilter: 'blur(18px)',
  } as const
  const dashboardCardStyle = {
    borderRadius: 24,
    padding: '18px 18px 20px',
    background: 'linear-gradient(180deg, rgba(255,255,255,0.86), rgba(248,250,252,0.80))',
    boxShadow: '0 18px 40px rgba(15,45,77,0.08)',
    backdropFilter: 'blur(18px)',
    WebkitBackdropFilter: 'blur(18px)',
  } as const
  const softPanelStyle = {
    background: 'linear-gradient(180deg, rgba(255,255,255,0.96), rgba(247,249,252,0.92))',
    boxShadow: 'none',
  } as const
  const sourceSurfaceStyle = {
    background: 'transparent',
    boxShadow: 'none',
  } as const
  const targetSurfaceStyle = {
    ...sourceSurfaceStyle,
  } as const
  const topSupplementStyle = {
    background: 'rgba(247,249,252,0.72)',
    boxShadow: 'none',
  } as const
  const controlSurfaceStyle = {
    ...softPanelStyle,
    background: 'rgba(249,250,252,0.94)',
  } as const
  const historySurfaceStyle = {
    ...softPanelStyle,
    background: 'rgba(251,252,254,0.96)',
  } as const
  const chipStyle = {
    background: 'rgba(247,249,252,0.96)',
    color: palette.deep,
    boxShadow: 'none',
  } as const
  const inputStyle = {
    background: 'rgba(255,255,255,0.98)',
    color: palette.ink,
    boxShadow: 'none',
  } as const
  const primaryActionStyle = {
    background: 'linear-gradient(145deg, #d4b87a, #b89a5e)',
    boxShadow: '0 14px 28px rgba(184,154,94,0.24)',
    color: '#ffffff',
    cursor: 'pointer',
  } as const
  const primaryActionHoverStyle = {
    background: 'linear-gradient(145deg, #e1c989, #c7a967)',
    boxShadow: '0 18px 34px rgba(184,154,94,0.34)',
    transform: 'translateY(-1px)',
  } as const
  const stopActionStyle = {
    background: '#102238',
    boxShadow: '0 14px 28px rgba(16,34,56,0.22)',
    color: '#f8fafc',
    cursor: 'pointer',
  } as const
  const stopActionHoverStyle = {
    background: '#193653',
    boxShadow: '0 18px 34px rgba(16,34,56,0.32)',
    transform: 'translateY(-1px)',
  } as const
  const secondaryButtonStyle = {
    background: 'rgba(255,255,255,0.96)',
    boxShadow: 'none',
    color: palette.ink,
    cursor: 'pointer',
  } as const
  const secondaryButtonHoverStyle = {
    background: '#ffffff',
    boxShadow: '0 12px 24px rgba(15,45,77,0.10), inset 0 0 0 1px rgba(15,45,77,0.08)',
    transform: 'translateY(-1px)',
  } as const
  const mutedActionStyle = {
    background: 'rgba(188,95,111,0.12)',
    boxShadow: 'inset 0 0 0 1px rgba(188,95,111,0.16)',
    color: '#8a2720',
    cursor: 'pointer',
  } as const
  const mutedActionHoverStyle = {
    background: 'rgba(188,95,111,0.18)',
    boxShadow: '0 12px 24px rgba(188,95,111,0.12), inset 0 0 0 1px rgba(188,95,111,0.24)',
    transform: 'translateY(-1px)',
  } as const
  const disabledActionStyle = {
    ...secondaryButtonStyle,
    opacity: 0.48,
    cursor: 'not-allowed' as const,
  }
  const sourceMuteButtonStyle: CSSProperties = !isListening
    ? disabledActionStyle
    : {
        ...(inputMuted ? mutedActionStyle : secondaryButtonStyle),
        ...(hoveredSourceAction === 'mute' ? (inputMuted ? mutedActionHoverStyle : secondaryButtonHoverStyle) : {}),
      }
  const sourceListenButtonStyle: CSSProperties = {
    ...(isListening ? stopActionStyle : primaryActionStyle),
    ...(hoveredSourceAction === 'listen' ? (isListening ? stopActionHoverStyle : primaryActionHoverStyle) : {}),
  }
  const sectionHeadingStyle = {
    margin: 0,
    fontSize: 'clamp(1.2rem, 1.4vw, 1.55rem)',
    fontWeight: 800,
    letterSpacing: '-0.04em',
    color: palette.ink,
  } as const
  const panelHeadingStyle = {
    fontSize: 14,
    fontWeight: 700,
    letterSpacing: '-0.02em',
    color: palette.ink,
  } as const
  const utilityLabelStyle = {
    fontSize: 11,
    fontWeight: 800,
    letterSpacing: '0.16em',
    textTransform: 'uppercase' as const,
    color: 'rgba(15,45,77,0.52)',
  } as const
  const utilityBodyStyle = {
    fontSize: 13,
    lineHeight: 1.7,
    color: 'rgba(16,34,56,0.66)',
  } as const
  const toggleOnStyle = {
    background: 'linear-gradient(145deg, #d4b87a, #b89a5e)',
    boxShadow: 'inset 0 0 0 1px rgba(184,154,94,0.12)',
  } as const
  const toggleOffStyle = {
    background: 'rgba(120,98,78,0.14)',
    boxShadow: 'inset 0 0 0 1px rgba(120,98,78,0.08)',
  } as const
  const tableHeaderStyle = {
    fontSize: 10,
    fontWeight: 800,
    letterSpacing: '0.16em',
    textTransform: 'uppercase' as const,
    color: 'rgba(16,34,56,0.44)',
  } as const
  const translationFontSize = translated.length > 180 ? '1rem' : translated.length > 90 ? '1.25rem' : 'clamp(1.85rem, 3vw, 2.65rem)'
  const historyLines = [...committedLines].reverse()

  return (
    <section className="w-full" style={{ color: palette.ink }}>
      <div className="relative overflow-hidden py-1">
        <div className="relative grid gap-6">
          {broadcastIssues.length > 0 ? (
            <section
              className="grid gap-3 rounded-[1.6rem] px-4 py-4 md:px-5"
              style={broadcastAlertStyle}
              role="alert"
              aria-live={hasCriticalBroadcastIssue ? 'assertive' : 'polite'}
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span
                      className="inline-flex items-center rounded-full px-3 py-1 text-[11px] font-bold uppercase tracking-[0.14em]"
                      style={broadcastAlertBadgeStyle}
                    >
                      {hasCriticalBroadcastIssue ? 'Broadcast warning' : 'Broadcast notice'}
                    </span>
                    <span className="text-sm font-semibold" style={{ color: 'inherit' }}>
                      {hasCriticalBroadcastIssue ? 'Live broadcast needs immediate attention.' : 'Live broadcast is degraded.'}
                    </span>
                  </div>
                  <p className="mt-2 text-sm" style={{ marginBottom: 0, color: 'inherit', opacity: 0.92, lineHeight: 1.7 }}>
                    {hasCriticalBroadcastIssue
                      ? 'Some listeners may stop receiving captions or translations until the connection recovers.'
                      : 'The app is still running, but one or more broadcast services are recovering.'}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  {connectionState !== 'connected' ? (
                    <span className="inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-[11px] font-semibold" style={socketStatusStyle}>
                      <span className={`h-2 w-2 rounded-full ${connectionState === 'reconnecting' ? 'animate-pulse' : ''}`} style={socketDotStyle} />
                      {socketStatusLabel}
                    </span>
                  ) : null}
                  {status !== 'streaming' && status !== 'idle' && status !== 'stopped' ? (
                    <span className="inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-[11px] font-semibold" style={broadcastAlertBadgeStyle}>
                      Deepgram {status}
                    </span>
                  ) : null}
                </div>
              </div>

              <div className="grid gap-2">
                {broadcastIssues.map(issue => (
                  <div
                    key={issue.id}
                    className="rounded-[1.1rem] px-4 py-3"
                    style={{
                      background: issue.tone === 'critical' ? 'rgba(255,255,255,0.72)' : 'rgba(255,255,255,0.62)',
                      boxShadow: issue.tone === 'critical'
                        ? 'inset 0 0 0 1px rgba(188,95,111,0.14)'
                        : 'inset 0 0 0 1px rgba(198,165,109,0.14)',
                    }}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span
                        className="inline-flex items-center rounded-full px-2.5 py-1 text-[10px] font-bold uppercase tracking-[0.12em]"
                        style={issue.tone === 'critical' ? { background: 'rgba(188,95,111,0.14)', color: '#8a2720' } : { background: 'rgba(198,165,109,0.16)', color: '#7a5c20' }}
                      >
                        {issue.tone === 'critical' ? 'Critical' : 'Recovering'}
                      </span>
                      <span className="text-sm font-semibold" style={{ color: issue.tone === 'critical' ? '#7d1d32' : '#7a5c20' }}>
                        {issue.title}
                      </span>
                    </div>
                    <p className="mt-2 text-sm" style={{ marginBottom: 0, color: issue.tone === 'critical' ? '#7d1d32' : '#7a5c20', lineHeight: 1.7, opacity: 0.92 }}>
                      {issue.detail}
                    </p>
                  </div>
                ))}
              </div>
            </section>
          ) : null}

          {/* Source + Target panels */}
          <div className="grid gap-4 xl:grid-cols-2 xl:items-stretch">

              <section className="grid gap-0" style={pairedPanelStyle}>
                <div className="flex flex-wrap items-center justify-between gap-3 pb-3">
                  <div className="flex min-w-0 items-center gap-2">
                    <span style={utilityLabelStyle}>Source</span>
                    <span style={panelHeadingStyle}>{languageFlag(sourceLang)} {sourceLabel}</span>
                  </div>
                  <select
                    value={sourceLang}
                    onChange={e => onSourceLangChange(e.target.value)}
                    className="text-xs font-semibold focus:outline-none"
                    style={{ ...inputStyle, border: 'none', borderRadius: 999, padding: '6px 12px' }}
                  >
                    {availableLanguages.map(l => <option key={l.code} value={l.code} className="bg-white text-slate-900">{l.name}</option>)}
                  </select>
                </div>

                <div className="broadcast-scroll px-1 py-2" style={{ ...sourceSurfaceStyle, minHeight: 280, maxHeight: 380, overflowY: 'auto' as const }}>
                  {text ? (
                    <p style={{ margin: 0, color: palette.ink, fontSize: '1.05rem', lineHeight: 1.75, fontWeight: 400 }}>{text}</p>
                  ) : (
                    <p style={{ margin: 0, color: 'rgba(16,34,56,0.32)', fontSize: '1.05rem', lineHeight: 1.75, fontStyle: 'italic' }}>
                      {inputMuted && isListening ? 'Microphone is muted. Audio input is paused.' : `Listening for ${sourceLabel} speech…`}
                    </p>
                  )}
                </div>

                <div className="flex flex-wrap items-center gap-3 pt-3">
                  <div className="flex h-7 min-w-[84px] flex-1 items-end gap-1">{waveformBars}</div>
                  <button
                    type="button"
                    onClick={handleToggleInputMuted}
                    disabled={!isListening}
                    aria-pressed={inputMuted}
                    onMouseEnter={() => setHoveredSourceAction('mute')}
                    onMouseLeave={() => setHoveredSourceAction(null)}
                    onFocus={() => setHoveredSourceAction('mute')}
                    onBlur={() => setHoveredSourceAction(null)}
                    className="rounded-[1rem] px-4 py-2 text-xs font-black uppercase tracking-[0.16em] transition"
                    style={sourceMuteButtonStyle}
                  >
                    {inputMuted ? 'Unmute' : 'Mute'}
                  </button>
                  <button
                    onClick={isListening ? handleStopListening : () => { void handleStartListening() }}
                    onMouseEnter={() => setHoveredSourceAction('listen')}
                    onMouseLeave={() => setHoveredSourceAction(null)}
                    onFocus={() => setHoveredSourceAction('listen')}
                    onBlur={() => setHoveredSourceAction(null)}
                    className="rounded-[1rem] px-4 py-2 text-xs font-black uppercase tracking-[0.16em] transition"
                    style={sourceListenButtonStyle}
                  >
                    {isListening ? 'Stop' : 'Start'}
                  </button>
                </div>
              </section>

              <section className="grid gap-0" style={pairedPanelStyle}>
                <div className="flex flex-wrap items-center justify-between gap-3 pb-3">
                  <div className="flex min-w-0 items-center gap-2">
                    <span style={utilityLabelStyle}>Target</span>
                    <span style={panelHeadingStyle}>{languageFlag(targetLang)} {targetLabel}</span>
                  </div>
                  <select
                    value={targetLang}
                    onChange={e => onTargetLangChange(e.target.value)}
                    className="text-xs font-semibold focus:outline-none"
                    style={{ ...inputStyle, border: 'none', borderRadius: 999, padding: '6px 12px' }}
                  >
                    {availableLanguages.map(l => <option key={l.code} value={l.code} className="bg-white text-slate-900">{l.name}</option>)}
                  </select>
                </div>

                <div className="broadcast-scroll px-1 py-2" style={{ ...targetSurfaceStyle, minHeight: 280, maxHeight: 380, overflowY: 'auto' as const }}>
                  {[...committedLines].slice(-4).map(line => (
                    <p key={line.id} style={{ margin: '0 0 12px', color: 'rgba(16,34,56,0.38)', fontSize: '0.95rem', lineHeight: 1.65, fontWeight: 400 }}>
                      {line.translated}
                    </p>
                  ))}
                  <p style={{ margin: 0, color: translated ? palette.ink : 'rgba(16,34,56,0.28)', fontSize: translationFontSize, lineHeight: 1.45, fontWeight: 500, fontStyle: translated ? 'normal' : 'italic' }}>
                    {translated || 'Audience output is standing by…'}
                  </p>
                </div>

                <div className="pt-3">
                  {scriptureMeta ? (
                    <div className="rounded-[1rem] px-3 py-2" style={topSupplementStyle}>
                      <p style={utilityLabelStyle}>Scripture · {scriptureMeta.header}</p>
                      {scriptureMeta.sourceText ? <p className="mt-1 text-xs" style={{ margin: '4px 0 0', color: 'rgba(16,34,56,0.62)', lineHeight: 1.6 }}>{scriptureMeta.sourceText}</p> : null}
                    </div>
                  ) : previewSnippet ? (
                    <div className="rounded-[1rem] px-3 py-2" style={topSupplementStyle}>
                      <p style={utilityLabelStyle}>Next phrase</p>
                      <p className="mt-1 text-sm" style={{ margin: '4px 0 0', color: 'rgba(16,34,56,0.62)', lineHeight: 1.6 }}>{previewSnippet}</p>
                    </div>
                  ) : null}
                </div>
              </section>
          </div>

          <div className="grid gap-4">
            <div className="grid gap-4 xl:grid-cols-[minmax(280px,0.42fr)_minmax(0,1fr)] xl:items-end">
              <div>
                <h3 style={sectionHeadingStyle}>Stream Controls</h3>
              </div>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <h3 style={sectionHeadingStyle}>Transcription History</h3>
                {historyLines.length > 0 ? (
                  <span className="rounded-full px-3 py-1 text-[10px] font-bold uppercase tracking-[0.12em]" style={{ ...chipStyle, color: palette.ink }}>
                    {historyLines.length} lines
                  </span>
                ) : null}
              </div>
            </div>

            <div className="grid gap-6 xl:grid-cols-[minmax(280px,0.42fr)_minmax(0,1fr)]">

            <section className="grid gap-4">

              <div className="grid gap-2.5" style={dashboardCardStyle}>
                {([
                  { label: 'Broadcast output', desc: isBroadcasting ? 'Listeners are receiving translated output.' : 'Output is paused for listeners.', value: isBroadcasting, onToggle: () => setIsBroadcasting(v => !v) },
                  { label: 'Early preview', desc: earlyCommitEnabled ? 'Preview text is shown before final commit.' : 'Only finalized clauses are displayed.', value: earlyCommitEnabled, onToggle: () => setEarlyCommitEnabled(v => !v) },
                  { label: 'Audience TTS', desc: ttsAudienceEnabled ? 'Speech synthesis is active.' : 'Speech synthesis is muted.', value: ttsAudienceEnabled, onToggle: () => setIsMuted(m => !m) },
                ] as const).map(item => (
                  <div key={item.label} className="flex items-center justify-between gap-4 rounded-[1.1rem] px-4 py-3" style={controlSurfaceStyle}>
                    <div className="min-w-0">
                      <p className="text-sm font-semibold" style={{ margin: 0, color: palette.ink }}>{item.label}</p>
                      <p className="text-xs" style={{ margin: '2px 0 0', color: 'rgba(16,34,56,0.50)', lineHeight: 1.6 }}>{item.desc}</p>
                    </div>
                    <button
                      onClick={item.onToggle}
                      className="relative inline-flex h-7 w-12 flex-shrink-0 items-center rounded-full transition"
                      style={item.value ? toggleOnStyle : toggleOffStyle}
                      aria-pressed={item.value}
                    >
                      <span className={`inline-block h-5 w-5 rounded-full transition ${item.value ? 'translate-x-6' : 'translate-x-1'}`} style={{ background: item.value ? '#ffffff' : 'rgba(255,255,255,0.78)' }} />
                    </button>
                  </div>
                ))}
              </div>

              <div className="grid gap-4" style={dashboardCardStyle}>
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p style={{ ...utilityLabelStyle, margin: 0 }}>Advanced Controls</p>
                  </div>
                  <button
                    onClick={() => setShowAdvancedControls(v => !v)}
                    className="rounded-full px-4 py-1.5 text-[11px] font-bold uppercase tracking-[0.14em] transition"
                    style={{ ...chipStyle, border: 'none', cursor: 'pointer' }}
                  >
                    {showAdvancedControls ? 'Hide' : 'Show'}
                  </button>
                </div>
              </div>

              {showAdvancedControls ? (
                <div className="grid gap-3" style={dashboardCardStyle}>
                  <div className="grid gap-2 rounded-[1.1rem] px-4 py-3" style={controlSurfaceStyle}>
                    <div className="flex items-center justify-between gap-4">
                      <span style={utilityBodyStyle}>Display speed</span>
                      <span className="text-sm font-semibold" style={{ color: palette.ink }}>{displaySpeed.toFixed(2)}x</span>
                    </div>
                    <div className="flex gap-2">
                      <button onClick={() => applyDisplaySpeed(displaySpeed + DISPLAY_SPEED_STEP)} className="flex-1 rounded-[0.9rem] px-3 py-2 text-xs font-semibold" style={secondaryButtonStyle}>Slower</button>
                      <button onClick={() => applyDisplaySpeed(displaySpeed - DISPLAY_SPEED_STEP)} className="flex-1 rounded-[0.9rem] px-3 py-2 text-xs font-semibold" style={secondaryButtonStyle}>Faster</button>
                    </div>
                  </div>
                  <div className="grid gap-1.5 rounded-[1.1rem] px-4 py-3" style={controlSurfaceStyle}>
                    <label style={utilityLabelStyle}>Voice Engine</label>
                    <select value={ttsProvider} onChange={e => setTtsProvider(e.target.value as TTSProvider)} className="w-full rounded-[0.9rem] px-3 py-2 text-sm font-medium focus:outline-none" style={{ ...inputStyle, border: 'none' }}>
                      {TTS_PROVIDER_OPTIONS.map(o => <option key={o.value} value={o.value} className="bg-white text-slate-900">{o.label}</option>)}
                    </select>
                  </div>
                  <div className="grid gap-1.5 rounded-[1.1rem] px-4 py-3" style={controlSurfaceStyle}>
                    <label style={utilityLabelStyle}>Voice Preset</label>
                    <select value={voicePreference} onChange={e => setVoicePreference(e.target.value)} className="w-full rounded-[0.9rem] px-3 py-2 text-sm font-medium focus:outline-none" style={{ ...inputStyle, border: 'none' }}>
                      <option value="auto" className="bg-white text-slate-900">Auto · match language</option>
                      {voiceOptions.map(o => <option key={o.value} value={o.value} className="bg-white text-slate-900">{o.label}</option>)}
                    </select>
                  </div>
                  <div className="grid gap-2 rounded-[1.1rem] px-4 py-3" style={controlSurfaceStyle}>
                    <div className="flex items-center justify-between gap-3">
                      <span style={utilityBodyStyle}>Monitor volume</span>
                      <span className="text-sm font-semibold" style={{ color: palette.ink }}>{Math.round(volume * 100)}%</span>
                    </div>
                    <input type="range" min={0} max={1} step={0.05} value={volume} onChange={e => setVolume(parseFloat(e.target.value))} className="w-full accent-[#1f3a5b]" />
                  </div>
                  <div className="flex gap-2">
                    <button onClick={() => triggerFinalize('manual operator button')} className="flex-1 rounded-[0.9rem] px-3 py-2 text-xs font-semibold" style={secondaryButtonStyle}>Pulse Finalize</button>
                    <button onClick={() => enqueueFinalTTS('This is a test of speech synthesis.')} className="flex-1 rounded-[0.9rem] px-3 py-2 text-xs font-semibold" style={secondaryButtonStyle}>Test TTS</button>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {[
                      `Heartbeat · ${lastHeartbeatLabel}`,
                      `Reconnect · ${reconnectAttemptLabel}`,
                      `Downtime · ${socketDowntimeLabel}`,
                      `Latency · ${latencyMs === null ? '—' : `${latencyMs}ms`}`,
                      `Deepgram · ${status}`,
                    ].map(label => (
                      <span key={label} className="rounded-full px-2.5 py-1 text-[10px] font-semibold" style={chipStyle}>{label}</span>
                    ))}
                  </div>
                </div>
              ) : null}
            </section>

            <section className="grid gap-4">
              {historyLines.length > 0 ? (
                <div className="broadcast-scroll overflow-y-auto" style={{ ...dashboardCardStyle, maxHeight: 520, padding: '10px 12px 12px' }}>
                  <div className="grid items-center rounded-[1rem] px-4 py-3" style={{ ...historySurfaceStyle, gridTemplateColumns: 'minmax(0,1fr) minmax(0,1.25fr) 108px' }}>
                    <span style={tableHeaderStyle}>Source</span>
                    <span style={tableHeaderStyle}>Translation</span>
                    <span style={{ ...tableHeaderStyle, textAlign: 'right' }}>Status</span>
                  </div>
                  <div className="mt-2 grid gap-2">
                    {historyLines.map(line => {
                      const isEditing = correcting === line.id
                      const isSaved = correctionSaved.has(line.id)
                      const statusLabel = isEditing ? 'Editing' : isSaved ? 'Verified' : 'Ready'
                      const statusStyle = isEditing
                        ? { background: 'rgba(245,158,11,0.12)', color: '#9a6700' }
                        : isSaved
                        ? { background: 'rgba(91,179,130,0.14)', color: '#1d6b4f' }
                        : { background: 'rgba(15,45,77,0.06)', color: 'rgba(15,45,77,0.62)' }

                      return (
                        <div key={line.id} className="grid items-start gap-3 rounded-[1rem] px-4 py-3" style={{ ...historySurfaceStyle, gridTemplateColumns: 'minmax(0,1fr) minmax(0,1.25fr) 108px' }}>
                          <p className="text-xs" style={{ margin: 0, color: 'rgba(16,34,56,0.54)', lineHeight: 1.7 }}>{line.srcText}</p>
                          <div className="min-w-0">
                            {isEditing ? (
                              <div className="flex flex-wrap gap-1.5">
                                <input
                                  className="min-w-[140px] flex-1 rounded-[0.75rem] px-2 py-1 text-xs focus:outline-none"
                                  style={{ ...inputStyle, border: 'none' }}
                                  value={correctionDraft}
                                  onChange={e => setCorrectionDraft(e.target.value)}
                                  onKeyDown={e => { if (e.key === 'Enter') submitCorrection(line); if (e.key === 'Escape') setCorrecting(null); }}
                                  autoFocus
                                />
                                <button onClick={() => submitCorrection(line)} className="rounded-[0.75rem] px-2 py-1 text-[10px] font-semibold" style={primaryActionStyle}>Save</button>
                                <button onClick={() => setCorrecting(null)} className="rounded-[0.75rem] px-2 py-1 text-[10px] font-semibold" style={secondaryButtonStyle}>Cancel</button>
                              </div>
                            ) : (
                              <p className="text-xs" style={{ margin: 0, color: isSaved ? '#1d6b4f' : palette.ink, lineHeight: 1.7 }}>
                                {line.translated}
                              </p>
                            )}
                          </div>
                          <div className="flex flex-col items-end gap-1.5">
                            <span className="rounded-full px-2.5 py-1 text-[10px] font-bold uppercase tracking-[0.12em]" style={statusStyle}>
                              {statusLabel}
                            </span>
                            {!isEditing && !isSaved ? (
                              <button
                                className="rounded-full px-2.5 py-1 text-[10px] font-bold uppercase tracking-[0.1em]"
                                style={secondaryButtonStyle}
                                onClick={() => { setCorrecting(line.id); setCorrectionDraft(line.translated); }}
                              >
                                Edit
                              </button>
                            ) : null}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              ) : (
                <div style={dashboardCardStyle}>
                  <p style={{ margin: 0, color: 'rgba(16,34,56,0.36)', fontSize: '0.85rem', lineHeight: 1.7, fontStyle: 'italic' }}>
                    Finalized translation lines will appear here.
                  </p>
                </div>
              )}
            </section>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
