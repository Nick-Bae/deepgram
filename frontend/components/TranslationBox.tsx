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

type QueuedTTS = { id: number; text: string; url: string }

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
  const [voicePreference, setVoicePreference] = useState('auto')
  const [ttsProvider, setTtsProvider] = useState<TTSProvider>('google')
  const [isBroadcasting, setIsBroadcasting] = useState(true)
  const [aiAssistEnabled, setAiAssistEnabled] = useState(true)
  const [displayOnAir, setDisplayOnAir] = useState(true)
  const [latencyMs, setLatencyMs] = useState<number | null>(null)
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

  // Deepgram mic producer
  const dgController: DeepgramProducerController & { finalize?: () => void } = useDeepgramProducer()
  const { start: dgStart, stop: dgStop, status, partial, errorMsg, finalize } = dgController
  const startProducer = useCallback(async () => {
    const startWithOptions = dgStart as (options?: { sourceLang?: string; targetLang?: string }) => Promise<void>
    await startWithOptions({ sourceLang, targetLang })
  }, [dgStart, sourceLang, targetLang])
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
      triggerFinalize('clause complete');
    },
    [postTranslate, sendPreview, shouldEmitClause, triggerFinalize]
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

        const response = await fetch(`${API_URL}/api/tts`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
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

    if (!incoming) return;

    console.log('[FE][WS][in]', { seq, isFinal, out: clip(incoming) });
    setTranslated(incoming);

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
        if (!isMuted && incoming !== currentSpokenRef.current) {
          enqueueFinalTTS(incoming);
        } else {
          console.log('[FE][TTS][skip]', { isMuted, sameAsCurrent: incoming === currentSpokenRef.current });
        }
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

  const ttsAudienceEnabled = !isMuted
  const latencyLabel = latencyMs !== null ? `${Math.max(latencyMs, 0).toFixed(0)} ms` : 'Calibrating…'
  const micActive = isListening && status === 'streaming'
  const waveformBars = Array.from({ length: 5 }, (_, idx) => {
    const baseHeight = 12 + idx * 7
    return (
      <span
        key={idx}
        className="voice-bar inline-flex w-1 rounded-full bg-gradient-to-t from-[#1d1e22]/0 via-[#f2c53d]/80 to-[#feda6a] shadow-[0_6px_18px_rgba(254,218,106,0.45)]"
        style={{
          height: `${baseHeight}px`,
          animationDelay: `${idx * 0.12}s`,
          animationPlayState: micActive ? 'running' : 'paused',
          transform: micActive ? undefined : 'scaleY(0.25)',
          opacity: micActive ? 1 : 0.45,
        }}
      />
    )
  })

  return (
    <section className="w-full space-y-8 text-[#f2f5e3]">
      <div className="relative overflow-hidden rounded-3xl border border-[#454543] bg-gradient-to-br from-[#1d1e22] via-[#1d1e22] to-[#393f4d] shadow-[0_30px_110px_rgba(0,0,0,0.55)]">
        <div className="pointer-events-none absolute inset-0 opacity-70" style={{ background: 'radial-gradient(circle at 18% 25%, rgba(254,218,106,0.18), transparent 55%)' }} />
        <div className="pointer-events-none absolute -right-28 top-8 h-72 w-72 rounded-full bg-[#668c4a]/25 blur-3xl" />
        <div className="relative space-y-8 p-6 md:p-10">
          <header className="flex flex-wrap items-center justify-between gap-6 border-b border-[#454543] pb-6">
            <div>
              <p className="flex items-center gap-2 text-xs uppercase tracking-[0.35em] text-[#feda6a]">
                <span className={`inline-flex h-2 w-2 rounded-full ${isBroadcasting ? 'bg-[#668c4a] animate-pulse' : 'bg-[#f2c53d]'}`} />
                Live
              </p>
              <h2 className="mt-2 text-2xl font-bold leading-tight text-[#f2f5e3] md:text-3xl">Real-Time Sermon Translation</h2>
              <p className="text-sm text-[#d4d4dc]">Monitor, refine, and broadcast translations without leaving this console.</p>
            </div>
            <div className="flex flex-col items-start gap-3 text-sm md:flex-row md:items-center md:gap-4">
              <span className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 ${connected ? 'border-[#668c4a]/60 text-[#f2f5e3]' : 'border-[#f2c53d]/60 text-[#f2c53d]'}`}>
                <span className={`h-2 w-2 rounded-full ${connected ? 'bg-[#668c4a] animate-ping' : 'bg-[#f2c53d]'}`} />
                WebSocket {connected ? 'Connected' : 'Offline'}
              </span>
              <span className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 ${status === 'streaming' ? 'border-[#feda6a]/60 text-[#feda6a]' : 'border-[#d4d4dc]/40 text-[#d4d4dc]'}`}>
                <span className={`h-2 w-2 rounded-full ${status === 'streaming' ? 'bg-[#feda6a] animate-pulse' : 'bg-[#d4d4dc]'}`} />
                Deepgram · {status}
              </span>
              <span className="inline-flex items-center gap-2 rounded-full border border-[#454543] bg-[#393f4d] px-3 py-1 text-[#f2f5e3]">
                <span className="text-xs uppercase tracking-wide text-[#b1b1ac]">Latency</span>
                <strong className="text-sm text-[#feda6a]">{latencyLabel}</strong>
              </span>
            </div>
          </header>

          {errorMsg && (
            <div className="rounded-2xl border border-[#f2c53d]/60 bg-[#f2c53d]/15 px-4 py-3 text-sm text-[#feda6a]">
              {errorMsg}
            </div>
          )}

          <div className="grid gap-6 md:grid-cols-2">
            <div className="flex flex-col gap-2">
              <label className="text-xs uppercase tracking-wide text-[#b1b1ac]">Source language</label>
              <div className="flex items-center gap-3 rounded-2xl border border-[#454543] bg-[#1d1e22]/70 px-4 py-3 text-[#f2f5e3]">
                <span className="text-2xl">{languageFlag(sourceLang)}</span>
                <select
                  value={sourceLang}
                  onChange={e => setSourceLang(e.target.value)}
                  className="w-full bg-transparent text-base font-semibold text-[#f2f5e3] focus:outline-none"
                >
                  {availableLanguages.map(l => <option key={l.code} value={l.code} className="text-[#1d1e22]">{l.name}</option>)}
                </select>
              </div>
            </div>
            <div className="flex flex-col gap-2">
              <label className="text-xs uppercase tracking-wide text-[#b1b1ac]">Target language</label>
              <div className="flex items-center gap-3 rounded-2xl border border-[#454543] bg-[#1d1e22]/70 px-4 py-3 text-[#f2f5e3]">
                <span className="text-2xl">{languageFlag(targetLang)}</span>
                <select
                  value={targetLang}
                  onChange={e => setTargetLang(e.target.value)}
                  className="w-full bg-transparent text-base font-semibold text-[#f2f5e3] focus:outline-none"
                >
                  {availableLanguages.map(l => <option key={l.code} value={l.code} className="text-[#1d1e22]">{l.name}</option>)}
                </select>
              </div>
            </div>
          </div>

          <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_320px]">
            <div className="space-y-6">
              <div className="rounded-3xl border border-[#454543] bg-[#1d1e22]/60 p-5 lg:p-6">
                <div className="grid gap-6 lg:grid-cols-2">
                  <div className="space-y-4 rounded-2xl border border-[#454543] bg-[#1d1e22] p-5">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-xs uppercase tracking-[0.35em] text-[#b1b1ac]">Live Source</p>
                        <p className="text-lg font-semibold text-[#f2f5e3]">{languageFlag(sourceLang)} {sourceLabel}</p>
                      </div>
                      <div className="flex items-center gap-3 text-sm text-[#d4d4dc]">
                        <span className={`relative inline-flex h-12 w-12 items-center justify-center rounded-full border ${micActive ? 'border-[#668c4a]/70 bg-[#668c4a]/20 text-[#f2f5e3]' : 'border-[#454543] bg-[#1d1e22]/70 text-[#b1b1ac]'}`}>
                          <span className="text-xl">🎙️</span>
                          {micActive && <span className="absolute inset-0 rounded-full border border-[#668c4a]/40 animate-ping" />}
                        </span>
                        <div className="flex h-10 items-end gap-1">{waveformBars}</div>
                      </div>
                    </div>
                    <textarea
                      value={text}
                      onChange={e => setText(e.target.value)}
                      placeholder="Speak into the mic or type here…"
                      className="min-h-[170px] w-full resize-none rounded-2xl border border-[#454543] bg-[#1d1e22]/80 px-4 py-3 text-base text-[#f2f5e3] placeholder:text-[#b1b1ac] focus:border-[#feda6a] focus:outline-none"
                    />
                  </div>

                  <div className="space-y-4 rounded-2xl border border-[#feda6a]/40 bg-gradient-to-br from-[#393f4d] via-[#1d1e22] to-[#1d1e22] p-5 text-[#f2f5e3]">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-xs uppercase tracking-[0.35em] text-[#feda6a]">Translation Output</p>
                        <p className="text-lg font-semibold text-[#f2f5e3]">{languageFlag(targetLang)} {targetLabel}</p>
                      </div>
                      <span className="rounded-full border border-[#feda6a]/30 bg-[#feda6a]/10 px-3 py-1 text-xs uppercase tracking-wide text-[#feda6a]">Broadcast ready</span>
                    </div>
                    <p className="min-h-[170px] whitespace-pre-wrap text-xl leading-relaxed">
                      {translated || 'Waiting for the next sentence…'}
                    </p>
                    {scriptureMeta && (
                      <div className="rounded-2xl border border-[#feda6a]/40 bg-[#1d1e22]/80 px-4 py-4 text-[#f2f5e3]">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <div>
                            <p className="text-xs uppercase tracking-[0.35em] text-[#feda6a]">Scripture Reference</p>
                            <p className="text-lg font-semibold text-[#f2f5e3]" title={scriptureMeta.versionFull || undefined}>
                              {scriptureMeta.header}
                            </p>
                          </div>
                          <span className="rounded-full border border-[#feda6a]/30 bg-[#feda6a]/10 px-3 py-1 text-xs uppercase tracking-wide text-[#feda6a]">
                            Exact verse
                          </span>
                        </div>
                        {scriptureMeta.sourceText && (
                          <div className="mt-3 rounded-2xl border border-[#454543] bg-[#0f1012]/60 px-3 py-2 text-sm text-[#d4d4dc]">
                            <p className="text-xs uppercase tracking-[0.25em] text-[#b1b1ac]">
                              {scriptureMeta.sourceLabel || 'Korean Source'}
                            </p>
                            <p className="mt-1 text-base text-[#f2f5e3]">{scriptureMeta.sourceText}</p>
                          </div>
                        )}
                      </div>
                    )}
                    <div className="rounded-2xl border border-[#454543] bg-[#1d1e22]/70 px-4 py-3 text-sm text-[#d4d4dc]">
                      <p className="text-xs uppercase tracking-[0.35em] text-[#b1b1ac]">Next sentence preview</p>
                      <p className="mt-1 text-base text-[#f2f5e3]">{previewSnippet || 'Listening for the next clause…'}</p>
                    </div>
                  </div>
                </div>
              </div>

              <div className="rounded-3xl border border-[#454543] bg-[#1d1e22]/60 p-5">
                <div className="flex flex-wrap items-center gap-4">
                  <button
                    onClick={isListening ? handleStopListening : handleStartListening}
                    className={`flex-1 min-w-[200px] rounded-2xl px-6 py-3 text-lg font-semibold shadow-lg transition ${isListening ? 'bg-[#00837e] text-[#f2f5e3] hover:bg-[#454543]' : 'bg-[#feda6a] text-[#1d1e22] shadow-[0_15px_45px_rgba(254,218,106,0.35)] hover:bg-[#f2c53d]'}`}
                  >
                    {isListening ? 'Stop translation' : 'Start translation'}
                  </button>
                  <button
                    onClick={() => triggerFinalize('manual operator button')}
                    className="rounded-2xl border border-[#454543] px-5 py-3 text-sm font-semibold text-[#f2f5e3] transition hover:border-[#feda6a] hover:text-[#feda6a]"
                  >
                    Pulse finalize
                  </button>
                  <button
                    onClick={() => enqueueFinalTTS('This is a test of speech synthesis.')}
                    className="rounded-2xl border border-[#454543] px-5 py-3 text-sm font-semibold text-[#f2f5e3] transition hover:border-[#feda6a] hover:text-[#feda6a]"
                  >
                    Test TTS
                  </button>
                  <div className="ml-auto flex items-center gap-3 text-sm text-[#d4d4dc]">
                    <span className="text-xs uppercase tracking-wide text-[#b1b1ac]">Monitor volume</span>
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.05}
                      value={volume}
                      onChange={(e) => setVolume(parseFloat(e.target.value))}
                      className="accent-[#feda6a]"
                    />
                    <span className="text-[#f2f5e3]">{Math.round(volume * 100)}%</span>
                  </div>
                </div>
              </div>
            </div>

            <aside className="space-y-6 rounded-3xl border border-[#454543] bg-[#1d1e22]/70 p-5">
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm font-semibold text-[#f2f5e3]">Go Live broadcast</p>
                    <p className="text-xs text-[#b1b1ac]">Send translations to foyer &amp; stream overlays.</p>
                  </div>
                  <button
                    onClick={() => setIsBroadcasting(v => !v)}
                    className={`relative inline-flex h-8 w-14 items-center rounded-full ${isBroadcasting ? 'bg-[#668c4a]' : 'bg-[#393f4d]'}`}
                    aria-pressed={isBroadcasting}
                  >
                    <span className={`inline-block h-6 w-6 rounded-full bg-[#f2f5e3] transition ${isBroadcasting ? 'translate-x-6' : 'translate-x-1'}`} />
                  </button>
                </div>
                <div className="flex items-center justify-between text-sm text-[#d4d4dc]">
                  <span>Display monitors</span>
                  <button
                    onClick={() => setDisplayOnAir(v => !v)}
                    className={`text-xs font-semibold uppercase tracking-wide ${displayOnAir ? 'text-[#feda6a]' : 'text-[#b1b1ac]'}`}
                  >
                    {displayOnAir ? 'On air' : 'Standby'}
                  </button>
                </div>
              </div>

              <div className="rounded-2xl border border-[#454543] bg-[#1d1e22]/60 p-4 text-sm text-[#d4d4dc]">
                <div className="flex items-center justify-between py-2">
                  <span>AI refinement</span>
                  <button
                    onClick={() => setAiAssistEnabled(v => !v)}
                    className={`relative inline-flex h-7 w-12 items-center rounded-full ${aiAssistEnabled ? 'bg-[#feda6a]' : 'bg-[#393f4d]'}`}
                    aria-pressed={aiAssistEnabled}
                  >
                    <span className={`inline-block h-5 w-5 rounded-full bg-[#1d1e22] transition ${aiAssistEnabled ? 'translate-x-5' : 'translate-x-1'}`} />
                  </button>
                </div>
                <div className="flex items-center justify-between border-t border-[#454543] py-2">
                  <span>Audience TTS</span>
                  <button
                    onClick={() => setIsMuted(m => !m)}
                    className={`relative inline-flex h-7 w-12 items-center rounded-full ${ttsAudienceEnabled ? 'bg-[#feda6a]' : 'bg-[#393f4d]'}`}
                    aria-pressed={ttsAudienceEnabled}
                  >
                    <span className={`inline-block h-5 w-5 rounded-full bg-[#1d1e22] transition ${ttsAudienceEnabled ? 'translate-x-5' : 'translate-x-1'}`} />
                  </button>
                </div>
                <div className="flex flex-col gap-2 border-t border-[#454543] py-2">
                  <label className="text-xs uppercase tracking-wide text-[#b1b1ac]">Voice engine</label>
                  <select
                    value={ttsProvider}
                    onChange={(e) => setTtsProvider(e.target.value as TTSProvider)}
                    className="rounded-2xl border border-[#454543] bg-[#0f1012] px-3 py-2 text-sm text-[#f2f5e3] focus:border-[#feda6a] focus:outline-none"
                  >
                    {TTS_PROVIDER_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="flex flex-col gap-2 border-t border-[#454543] py-2">
                  <label className="text-xs uppercase tracking-wide text-[#b1b1ac]">Voice preset</label>
                  <select
                    value={voicePreference}
                    onChange={(e) => setVoicePreference(e.target.value)}
                    className="rounded-2xl border border-[#454543] bg-[#0f1012] px-3 py-2 text-sm text-[#f2f5e3] focus:border-[#feda6a] focus:outline-none"
                  >
                    <option value="auto">Auto · match language</option>
                    {voiceOptions.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="flex items-center justify-between border-t border-[#454543] py-2">
                  <span>Stage display link</span>
                  <span className={`text-xs font-semibold uppercase tracking-wide ${displayOnAir ? 'text-[#feda6a]' : 'text-[#b1b1ac]'}`}>
                    {displayOnAir ? 'Live' : 'Muted'}
                  </span>
                </div>
              </div>

              <div className="rounded-2xl border border-[#454543] bg-[#1d1e22]/60 p-4 text-sm">
                <p className="mb-3 text-xs uppercase tracking-wide text-[#b1b1ac]">Connection health</p>
                <div className="space-y-2 text-[#d4d4dc]">
                  <div className="flex items-center justify-between">
                    <span>Producer socket</span>
                    <span className="font-semibold text-[#f2f5e3]">{connected ? 'Stable' : 'Reconnecting'}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span>Display sync</span>
                    <span className="font-semibold text-[#f2f5e3]">{displayOnAir ? 'Mirrored' : 'Off'}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span>TTS monitor</span>
                    <span className="font-semibold text-[#f2f5e3]">{ttsAudienceEnabled ? 'Audible' : 'Muted'}</span>
                  </div>
                </div>
              </div>

              <div className="flex flex-col gap-3 rounded-2xl border border-[#454543] bg-[#1d1e22]/70 p-4 text-sm">
                <div>
                  <p className="text-xs uppercase tracking-wide text-[#b1b1ac]">Admin console</p>
                  <p className="text-[#d4d4dc]">Invite operators, manage monitors, and review transcripts.</p>
                </div>
                <a
                  href="/producer"
                  target="_blank"
                  className="inline-flex items-center justify-center rounded-2xl border border-[#feda6a]/50 bg-[#feda6a]/15 px-4 py-2 text-center text-sm font-semibold text-[#feda6a] hover:bg-[#feda6a]/25"
                >
                  Launch admin hub
                </a>
              </div>
            </aside>
          </div>
        </div>
      </div>

      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
        <div className="rounded-2xl border border-[#454543] bg-gradient-to-b from-[#1d1e22] to-[#393f4d] p-5 shadow-lg shadow-black/30">
          <p className="text-xs uppercase tracking-wide text-[#feda6a]">Scripted segments</p>
          <h3 className="mt-2 text-lg font-semibold text-[#f2f5e3]">Upload-ready workflow</h3>
          <p className="mt-3 text-sm text-[#d4d4dc]">
            Drop sermon manuscripts or announcements to pre-translate, rehearse, and push to displays on cue.
          </p>
          <button className="mt-4 inline-flex items-center gap-2 rounded-2xl border border-[#feda6a]/40 px-4 py-2 text-sm font-semibold text-[#feda6a] hover:border-[#feda6a]">
            📄 Import script
          </button>
        </div>

        <div className="rounded-2xl border border-[#454543] bg-gradient-to-b from-[#1d1e22] to-[#393f4d] p-5 shadow-lg shadow-black/30">
          <p className="text-xs uppercase tracking-wide text-[#feda6a]">Hybrid workflow</p>
          <h3 className="mt-2 text-lg font-semibold text-[#f2f5e3]">Blend live + scripted</h3>
          <p className="mt-3 text-sm text-[#d4d4dc]">
            Pin key phrases, Scriptures, or benedictions so they surface exactly when the speaker nears those cues.
          </p>
          <button className="mt-4 inline-flex items-center gap-2 rounded-2xl border border-[#feda6a]/40 px-4 py-2 text-sm font-semibold text-[#feda6a] hover:border-[#feda6a]">
            📌 Manage cue board
          </button>
        </div>

        <div className="rounded-2xl border border-[#454543] bg-gradient-to-b from-[#1d1e22] to-[#393f4d] p-5 shadow-lg shadow-black/30">
          <p className="text-xs uppercase tracking-wide text-[#feda6a]">Team ops</p>
          <h3 className="mt-2 text-lg font-semibold text-[#f2f5e3]">Admin &amp; monitoring</h3>
          <p className="mt-3 text-sm text-[#d4d4dc]">
            Invite volunteers, assign auditorium channels, and monitor downstream displays from one command center.
          </p>
          <button className="mt-4 inline-flex items-center gap-2 rounded-2xl border border-[#feda6a]/40 px-4 py-2 text-sm font-semibold text-[#feda6a] hover:border-[#feda6a]">
            🛠 Open admin console
          </button>
        </div>
      </div>
    </section>
  )
}
