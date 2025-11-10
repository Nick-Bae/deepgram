'use client'

import { useMemo, useState, useEffect, useRef } from 'react'
import { throttle } from '../utils/throttle'
import { useTranslationSocket } from '../utils/useTranslationSocket'
import { API_URL } from '../utils/urls'
import { useDeepgramProducer } from '../lib/useDeepgramProducer'

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

export default function TranslationBox() {
  const { connected, last, sendProducerText } = useTranslationSocket({ isProducer: true });

  // UI state
  const [text, setText] = useState('')
  const [translated, setTranslated] = useState('')
  const [isListening, setIsListening] = useState(false)
  const [sourceLang, setSourceLang] = useState('ko')
  const [targetLang, setTargetLang] = useState('en')
  const [isMuted, setIsMuted] = useState(false)
  const [volume, setVolume] = useState(1)
  const [selectedVoiceName, setSelectedVoiceName] = useState('')

  // Deepgram mic producer
  const { start: dgStart, stop: dgStop, status, partial, errorMsg } =
    useDeepgramProducer ? useDeepgramProducer() : { start: async () => { }, stop: () => { }, status: 'idle', partial: '', errorMsg: '' }

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

  const LINGER_MS = 300
  const MIN_FINAL_CHARS = 10
  const introHoldRe = /(한마디로\s*요약(을)?\s*하면|결론부터\s*말하자면)$/
  const eosRe = /[.!?。！？]$|(?:습니다|입니다|할까요|했어요|했지요|했네요)$/

  const CLIENT_DRIVEN = false
  const MIN_PREVIEW_CHARS = 10
  const lastPreviewSentRef = useRef('')

  // Track stability of non-final WS lines per seq (for soft-final fallback)
  const softMapRef = useRef<Map<number, { text: string; count: number; first: number; last: number }>>(new Map())

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

  const playNext = () => {
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
  };

  const enqueueFinalTTS = (s: string) => {
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
  };

  // ---------- Speech Synthesis init ----------
  useEffect(() => {
    if (typeof window === 'undefined') return;
    synthRef.current = window.speechSynthesis;

    const onVoices = () => {
      const vs = synthRef.current?.getVoices() || [];
      console.log('[FE][TTS][voices]', vs.map(v => `${v.name} (${v.lang})`));
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
    const seq = Number((last as any)?.seq || 0);
    const incoming = String((last as any)?.text || '').trim();
    const meta = (last as any)?.meta;
    const isFinal = typeof meta?.is_final === 'boolean' ? meta.is_final : false;

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
      if (stable && eosRe.test(incoming) && seq > lastHandledSeqRef.current) {
        console.log('[FE][WS][soft-final]', { seq, out: clip(incoming) });
        lastHandledSeqRef.current = seq;
        if (!isMuted && incoming !== currentSpokenRef.current) {
          enqueueFinalTTS(incoming);
        }
      }
    }
  }, [last, isMuted]);

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
        const oldLooksComplete = eosRe.test(old) || old.length >= MIN_FINAL_CHARS + 10;
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

      if (eosRe.test(clauseRef.current)) {
        sendFinalNow(clauseRef.current);
        clauseRef.current = '';
      } else {
        scheduleFinal();
      }
    }

    lastInterimRef.current = cur;
    setText(cur);
  }, [partial]);

  // ---------- Keep isListening in sync with Deepgram ----------
  useEffect(() => {
    setIsListening(status === 'streaming')
    if (status !== 'streaming' && clauseRef.current.trim()) {
      sendFinalNow(clauseRef.current)
      clauseRef.current = ''
    }
  }, [status])

  // ---------- HTTP translate (client-driven OFF by default) ----------
  async function postTranslate(s: string, finalFlag: boolean) {
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
  }

  const sendPreview = useMemo(
    () =>
      throttle((fullClause: string) => {
        if (!CLIENT_DRIVEN) return;

        const s = (fullClause || '').trim();
        if (!s) return;

        if (s.length < MIN_PREVIEW_CHARS && !eosRe.test(s)) return;
        if (s.length < MIN_FINAL_CHARS && introHoldRe.test(s)) return;

        if (!eosRe.test(s)) {
          if (s === lastPreviewSentRef.current) return;
          if (Math.abs(s.length - lastPreviewSentRef.current.length) < 2) return;
        }

        if (DEBUG) console.log('[FE][preview][clause]', clip(s));
        lastPreviewSentRef.current = s;
        postTranslate(s, false);
      }, 400),
    [CLIENT_DRIVEN, sourceLang, targetLang]
  );

  function sendFinalNow(s: string) {
    const clean = (s || '').trim();
    if (!clean) return;

    if (typeof (sendPreview as any).cancel === 'function') {
      (sendPreview as any).cancel();
    }

    if (CLIENT_DRIVEN) {
      if (DEBUG) console.log('[FE][final][clause]', clip(clean));
      postTranslate(clean, true);
    } else {
      if (DEBUG) console.log('[FE][final][clause][no-http]', clip(clean));
    }

    lastPreviewSentRef.current = '';
  }

  function scheduleFinal() {
    if (lingerTimerRef.current) clearTimeout(lingerTimerRef.current);
    lingerTimerRef.current = setTimeout(() => {
      const s = clauseRef.current.trim();
      if (!s) return;

      if (s.length < MIN_FINAL_CHARS && !eosRe.test(s)) return;
      if (s.length < MIN_FINAL_CHARS && introHoldRe.test(s)) return;

      sendFinalNow(s);
      clauseRef.current = '';
    }, LINGER_MS);
  }

  function onPartialKorean(newChunk: string) {
    if (!newChunk) return
    clauseRef.current += newChunk
    sendPreview(clauseRef.current)
    if (eosRe.test(clauseRef.current)) {
      sendFinalNow(clauseRef.current)
      clauseRef.current = ''
    } else {
      scheduleFinal()
    }
  }

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

    try { await dgStart() } catch (e: any) { alert(`Mic start failed: ${e?.message || e}`) }
  }

  const handleStopListening = () => {
    dgStop()
  }

  return (
    <div className="w-full max-w-3xl mx-auto p-6 bg-white rounded-xl shadow-md">
      <h2 className="text-2xl font-bold text-gray-700 mb-4 text-center">🎤 Real-Time Translator</h2>

      <div className="flex items-center mb-4 gap-2">
        <span className={`inline-block w-3 h-3 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
        <span className="text-sm text-gray-600">
          WebSocket: {connected ? 'Connected' : 'Disconnected'}· Deepgram: {status}{errorMsg ? ` · ${errorMsg}` : ''}
        </span>
      </div>

      <div className="flex gap-4 mb-4">
        <div className="flex flex-col w-1/2">
          <label className="text-gray-600 mb-1">Source Language</label>
          <select value={sourceLang} onChange={e => setSourceLang(e.target.value)} className="p-2 border rounded shadow-sm">
            {availableLanguages.map(l => <option key={l.code} value={l.code}>{l.name}</option>)}
          </select>
        </div>
        <div className="flex flex-col w-1/2">
          <label className="text-gray-600 mb-1">Target Language</label>
          <select value={targetLang} onChange={e => setTargetLang(e.target.value)} className="p-2 border rounded shadow-sm">
            {availableLanguages.map(l => <option key={l.code} value={l.code}>{l.name}</option>)}
          </select>
        </div>
      </div>

      <div className="mb-4 flex gap-2">
        <button
          onClick={() => enqueueFinalTTS('This is a test of speech synthesis.')}
          className="px-3 py-2 bg-gray-200 rounded hover:bg-gray-300"
        >
          Test TTS
        </button>
        <button
          onClick={() => setIsMuted(m => !m)}
          className="px-3 py-2 bg-gray-200 rounded hover:bg-gray-300"
        >
          {isMuted ? 'Unmute' : 'Mute'}
        </button>
        <div className="flex items-center gap-2">
          <label className="text-gray-600">Vol</label>
          <input
            type="range"
            min={0}
            max={1}
            step={0.1}
            value={volume}
            onChange={(e) => setVolume(parseFloat(e.target.value))}
          />
        </div>
      </div>

      <div className="flex gap-4 items-center mb-4">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          className="w-full h-32 p-3 border rounded-md shadow-sm focus:outline-none"
          placeholder="Type or speak here..."
        />
        <button
          onClick={isListening ? handleStopListening : handleStartListening}
          className={`p-3 rounded-full text-white transition ${isListening ? 'bg-red-500 hover:bg-red-600' : 'bg-blue-500 hover:bg-blue-600'}`}
        >
          {isListening ? '🛑 Stop' : '🎤 Start'}
        </button>
      </div>

      {translated && (
        <div className="mt-6">
          <h3 className="text-lg font-medium text-gray-800 mb-2">🖥️ Translated Text:</h3>
          <div className="p-3 bg-gray-100 rounded-md border shadow-sm">
            {translated}
          </div>
        </div>
      )}

      <div className="mt-6">
        <label className="text-gray-600 mb-2 block">Choose Voice</label>
        <select
          value={selectedVoiceName}
          onChange={(e) => setSelectedVoiceName(e.target.value)}
          className="p-2 border rounded shadow-sm focus:outline-none w-full"
        >
          {synthRef.current && synthRef.current.getVoices().map((v: SpeechSynthesisVoice) => (
            <option key={v.name} value={v.name}>
              {v.name} ({v.lang})
            </option>
          ))}
        </select>
      </div>
    </div>
  )
}
