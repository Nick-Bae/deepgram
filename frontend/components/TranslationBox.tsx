'use client'

import { useMemo, useState, useEffect, useRef, useCallback } from 'react'
import { throttle } from '../utils/throttle'
import { useTranslationSocket } from '../utils/useTranslationSocket'
import { useSentenceBuffer } from '../utils/useSentenceBuffer';
import { useClauseCommit } from '../utils/useClauseCommit';
import { d, g, pv } from '../utils/debug';
import { useDeepgramProducer } from "../lib/useDeepgramProducer";
import { useSpeak } from "../lib/useSpeak";

const availableLanguages = [
  { code: 'ko', name: 'Korean' },
  { code: 'zh-CN', name: 'Chinese (Simplified)' },
  { code: 'en', name: 'English' },
  { code: 'es', name: 'Spanish' },
]

export default function TranslationBox() {
  // 🔌 WebSocket (producer mode)
  const { connected, last, sendProducerText } = useTranslationSocket({ isProducer: true })

  // UI states
  const [text, setText] = useState('')
  const [translated, setTranslated] = useState('')
  const [isListening, setIsListening] = useState(false)
  const [loading, setLoading] = useState(false)
  const [sourceLang, setSourceLang] = useState('ko')
  const [targetLang, setTargetLang] = useState('en')
  const [isMuted, setIsMuted] = useState(false)
  const [volume, setVolume] = useState(1)
  const [pauseListening, setPauseListening] = useState(false)
  const [selectedVoiceName, setSelectedVoiceName] = useState('')
  // targetLang can be whatever you use; "en" shown as example
  const { start: dgStart, stop: dgStop, status, partial, lastCommit, errorMsg } =
    useDeepgramProducer();

  // const isListening = status === "streaming";   // derive listening state from Deepgram
  const { speak } = useSpeak(targetLang);

  // Refs
  const synthRef = useRef<any>(null)
  const recognitionRef = useRef<any>(null)
  const ttsQueueRef = useRef<string[]>([])
  const isSpeakingRef = useRef<boolean>(false)
  const lastSentRef = useRef<string>('') // prevent duplicates to WS
  const wantListeningRef = useRef(false);
  const recActiveRef = useRef(false);
  const restartTimerRef = useRef<number | null>(null);
  const idleTimerRef = useRef<number | null>(null);
  const lastInterimRef = useRef('');
  const segmentCounterRef = useRef(0);
  const currentSegmentRef = useRef<number | null>(null);
  const revRef = useRef(0);
  const finalizedIdxRef = useRef(0);

  const mapToLocale = (code: string) => {
    switch (code) {
      case 'ko': return 'ko-KR';
      case 'en': return 'en-US';
      case 'zh-CN': return 'zh-CN';
      case 'es': return 'es-ES';
      default: return code; // fall back if already a locale
    }
  };
  const MIN_DELTA_CHARS = 2;          // ignore tiny deltas like “은”, “요”
  const NEW_UTTERANCE_DROP = 0.6;     // treat big shrink as a new utterance
  const IDLE_MS = 12000; // silence window before auto-stop (tweak as you like)

  const startRecognition = () => {
    wantListeningRef.current = true;
    if (!recognitionRef.current || recActiveRef.current) return;
    try { recognitionRef.current.start(); } catch { }
  };


  const clauseHandlers = useMemo(() => ({
    onPartial: (t: string) => {
      const src = (sourceLang || 'ko').split('-')[0];
      const tgt = (targetLang || 'en').split('-')[0];
      d('send', `partial → ${src}->${tgt} "${pv(t)}"`);
      sendProducerText(t, src, tgt, true); // partial
    },
    onCommit: ({ id, rev, text, final }: { id: number; rev: number; text: string; final: boolean }) => {
      const src = (sourceLang || 'ko').split('-')[0];
      const tgt = (targetLang || 'en').split('-')[0];
      d('send', `commit  → ${src}->${tgt} id=${id} rev=${rev} final=${final} "${pv(text)}"`);
      // 👇 pass final here
      sendProducerText(text, src, tgt, false, id, rev, final);
    }
  }), [sendProducerText, sourceLang, targetLang]);


  // ✅ stable config object
  const clauseCfg = useMemo(() => ({
    forceAfterMs: 1400,
    connectiveForceAfterMs: 2300,
    vadSilenceMs: 420,
    minChunkChars: 6,        // was 12
    minFirstCommitChars: 8,  // was 16
    commitConnectives: true,
    connectiveCommitAfterMs: 900,
    minConnectiveChars: 6,
  }), [])

  const { feedInterim, feedFinal, tick, reset: resetClause } =
    useClauseCommit(clauseHandlers, clauseCfg)


  // drive time-based commits ~10Hz
  useEffect(() => {
    const id = setInterval(() => tick(), 100)
    return () => clearInterval(id)
  }, [tick])

  const scheduleRestart = (delayMs = 300) => {
    if (restartTimerRef.current) {
      clearTimeout(restartTimerRef.current);
      restartTimerRef.current = null;
    }
    restartTimerRef.current = window.setTimeout(() => {
      if (wantListeningRef.current && !recActiveRef.current && recognitionRef.current) {
        try { recognitionRef.current.start(); } catch { }
      }
    }, delayMs);
  };

  const resetIdleTimer = () => {
    if (idleTimerRef.current) {
      clearTimeout(idleTimerRef.current);
      idleTimerRef.current = null;
    }
    idleTimerRef.current = window.setTimeout(() => {
      // Auto-stop after silence
      wantListeningRef.current = false;
      try { recognitionRef.current?.stop(); } catch { }
      setIsListening(false); // flip button to Start
    }, IDLE_MS);
  };

  // Soft stop that preserves "intent" when keepIntent=true (use for TTS pause)
  const stopRecognition = (keepIntent = false) => {
    if (!keepIntent) wantListeningRef.current = false;
    try { recognitionRef.current?.stop(); } catch { }
  };

  // ✅ Throttled sender to WS (avoid spamming)
  // ✅ Throttled sender to WS (avoid spamming)
  const sendSentence = useCallback((sentence: string) => {
    const s = sentence.trim();
    if (!s) return;
    if (s === lastSentRef.current) return;
    lastSentRef.current = s;

    // Normalize to base language codes like your other handlers:
    const src = (sourceLang || 'ko').split('-')[0];
    const tgt = (targetLang || 'en').split('-')[0];

    // text, src, tgt, isPartial, id?, rev?, final?
    sendProducerText(s, src, tgt, false, undefined, undefined, true);
  }, [sendProducerText, sourceLang, targetLang]);

  useEffect(() => {
    if (partial && partial !== text) setText(partial);
  }, [partial]);

  useEffect(() => {
    const src = (last?.srcText || "").trim();
    if (src && src !== text) {
      setText(src);
    }
  }, [last?.seq]); // runs on each finalized commit


  useEffect(() => {
    setIsListening(status === "streaming");
  }, [status]);



  useEffect(() => {
    const incoming = (last?.text || '').trim();
    if (!incoming) return;
    setTranslated(incoming);
    if (!isMuted && incoming !== 'Translation failed') {
      enqueueTranslation(incoming);
    }
  }, [last?.seq, isMuted]); // 👈 use seq instead of text

  useEffect(() => { setIsListening(status === "streaming"); }, [status]);

  const throttledSendSentence = useRef(throttle(sendSentence, 800)).current
  const buffer = useSentenceBuffer(throttledSendSentence);

  // 🧠 Speech Synthesis init
  useEffect(() => {
    if (typeof window === 'undefined') return
    synthRef.current = window.speechSynthesis

    const loadVoices = () => {
      const voices = synthRef.current.getVoices()
      if (voices?.length) {
        setSelectedVoiceName(voices[0].name)
      } else {
        window.speechSynthesis.onvoiceschanged = () => {
          const updated = synthRef.current.getVoices()
          setSelectedVoiceName(updated[0]?.name || '')
        }
      }
    }
    loadVoices()
  }, [])

  // ====== Your robust recognition effect ======
  // useEffect(() => {
  //   if (typeof window === 'undefined') return
  //   if (!('webkitSpeechRecognition' in window)) return

  //   const recognition = new (window as any).webkitSpeechRecognition()
  //   recognition.lang = mapToLocale(sourceLang)
  //   recognition.continuous = true
  //   recognition.interimResults = true
  //   recognition.maxAlternatives = 1

  //   recognition.onstart = () => {
  //     finalizedIdxRef.current = 0
  //     resetClause()
  //     d('asr', 'start lang=' + mapToLocale(sourceLang))
  //     recActiveRef.current = true
  //     wantListeningRef.current = true
  //     setIsListening(true)
  //     resetIdleTimer()
  //   }

  //   recognition.onend = () => {
  //     d('asr', 'end (willRestart=' + (wantListeningRef.current && !pauseListening) + ')')
  //     recActiveRef.current = false
  //     if (wantListeningRef.current && !pauseListening) {
  //       setIsListening(true)
  //       scheduleRestart(250)
  //     } else {
  //       setIsListening(false)
  //     }
  //   }

  //   recognition.onerror = (e: any) => {
  //     const err = e?.error
  //     if (err === 'no-speech' || err === 'audio-capture' || err === 'network' || err === 'aborted') {
  //       recActiveRef.current = false
  //       if (wantListeningRef.current && !pauseListening) {
  //         setIsListening(true)
  //         scheduleRestart(400)
  //       }
  //       return
  //     }
  //     console.warn('SpeechRecognition error:', err || e)
  //   }

  //   recognition.onresult = (event: any) => {
  //     // guard: Chrome can rebase results
  //     if (finalizedIdxRef.current > event.results.length) finalizedIdxRef.current = 0

  //     let interim = ''
  //     let finals = 0

  //     for (let i = finalizedIdxRef.current; i < event.results.length; i++) {
  //       const r = event.results[i]
  //       const t = r[0]?.transcript ?? ''
  //       if (!t) continue
  //       if (r.isFinal) {
  //         finals++
  //         g('final', 'text', t)
  //         feedFinal(t)
  //         finalizedIdxRef.current = i + 1
  //       } else {
  //         interim += t
  //       }
  //     }

  //     const trimmed = interim.trim()
  //     if (trimmed) {
  //       d('interim', `len=${trimmed.length} finals=${finals} text="${pv(trimmed)}"`)
  //       resetIdleTimer()
  //       feedInterim(trimmed)
  //       setText(trimmed)
  //     } else if (finals) {
  //       d('interim', `none finals=${finals}`)
  //     }
  //   }

  //   recognitionRef.current = recognition
  //   if (isListening && !recActiveRef.current) {
  //     try { recognition.start() } catch { }
  //   }

  //   return () => {
  //     try { recognition.stop() } catch { }
  //     recActiveRef.current = false
  //     wantListeningRef.current = false
  //     if (restartTimerRef.current) clearTimeout(restartTimerRef.current)
  //     if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
  //     finalizedIdxRef.current = 0
  //     resetClause()
  //   }
  //   // ✅ deps are stable now (handlers/config are memoized)
  // }, [sourceLang, pauseListening, isListening, feedInterim, feedFinal, resetClause])


  // ✅ Enqueue translation for TTS playback
  const enqueueTranslation = (translatedText: string) => {
    if (!translatedText.trim()) return
    // Dedup queue to avoid echoes
    if (ttsQueueRef.current.length && ttsQueueRef.current[ttsQueueRef.current.length - 1] === translatedText) {
      return
    }
    if (synthRef.current.speaking || isSpeakingRef.current) {
      ttsQueueRef.current.push(translatedText)
      return
    }
    ttsQueueRef.current.push(translatedText)
    playNextInQueue()
  }

  // ✅ Play next TTS item
  const playNextInQueue = () => {
    if (
      ttsQueueRef.current.length === 0 ||
      isMuted ||
      isSpeakingRef.current ||
      synthRef.current.speaking
    ) return;

    const nextText = ttsQueueRef.current.shift();
    if (!nextText) return;

    const utter = new SpeechSynthesisUtterance(nextText);
    utter.lang = targetLang;
    utter.volume = volume;

    const voices = synthRef.current.getVoices();
    const sel =
      voices.find((v: SpeechSynthesisVoice) => v.name === selectedVoiceName) ||
      voices[0];
    utter.voice = sel;

    isSpeakingRef.current = true;

    // ❌ REMOVE THIS:  (it was causing dropped clauses)
    // if (status === "streaming") { dgStop(); }

    utter.onend = () => {
      isSpeakingRef.current = false;
      // ❌ REMOVE THIS too:
      // if (!pauseListening) { dgStart().catch(() => {}); }

      if (ttsQueueRef.current.length > 0) {
        setTimeout(() => playNextInQueue(), 300);
      }
    };

    utter.onerror = () => {
      isSpeakingRef.current = false;
      if (ttsQueueRef.current.length > 0) {
        setTimeout(() => playNextInQueue(), 300);
      }
    };

    synthRef.current.speak(utter);
  };


  // ====== When you press Start/Stop, use these ======
  const handleStartListening = async () => {
    lastInterimRef.current = '';
    setText('');
    setTranslated('');
    ttsQueueRef.current = [];
    synthRef.current?.cancel();
    isSpeakingRef.current = false;

    try { await dgStart(); } catch (e: any) { alert(`Mic start failed: ${e?.message || e}`); }
  };

  const handleStopListening = () => {
    dgStop();
  };

  // 🧽 Clear
  const handleClear = () => {
    setText('')
    setTranslated('')
    handleStopListening()
    synthRef.current?.cancel()
    ttsQueueRef.current = []
    isSpeakingRef.current = false
  }
  const lastLogged = useRef<string | null>(null);
  useEffect(() => {
    if (!translated) return;
    if (translated === lastLogged.current) return;
    console.log('[RT][commit] translation:', translated);
    lastLogged.current = translated;
  }, [translated]);


  return (
    <div className="w-full max-w-3xl mx-auto p-6 bg-white rounded-xl shadow-md">
      {/* Header */}
      <h2 className="text-2xl font-bold text-gray-700 mb-4 text-center">🎤 Real-Time Translator</h2>
      <div className="flex items-center mb-4 gap-2">
        <span
          className={`inline-block w-3 h-3 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`}
          title={connected ? 'Connected' : 'Disconnected'}
        />
        <span className="text-sm text-gray-600">
          WebSocket: {connected ? 'Connected' : 'Disconnected'} {last.mode ? `· ${last.mode}` : ''}
          {last.mode === 'pre' ? ` (score ${last.matchScore.toFixed(2)})` : ''}
        </span>
      </div>

      {/* Status */}
      {loading && (
        <div className="mb-4 text-blue-500 text-sm text-center animate-pulse">
          Translating... Please wait.
        </div>
      )}

      {/* Language controls */}
      <div className="flex gap-4 mb-4">
        <div className="flex flex-col w-1/2">
          <label className="text-gray-600 mb-1">Source Language</label>
          <select
            value={sourceLang}
            onChange={(e) => setSourceLang(e.target.value)}
            className="p-2 border rounded shadow-sm focus:outline-none"
          >
            {availableLanguages.map((lang) => (
              <option key={lang.code} value={lang.code}>
                {lang.name}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col w-1/2">
          <label className="text-gray-600 mb-1">Target Language</label>
          <select
            value={targetLang}
            onChange={(e) => setTargetLang(e.target.value)}
            className="p-2 border rounded shadow-sm focus:outline-none"
          >
            {availableLanguages.map((lang) => (
              <option key={lang.code} value={lang.code}>
                {lang.name}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Input + Mic */}
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

      {/* Send typed text as one sentence */}
      <div className="flex gap-2 mb-4">
        <button
          onClick={() => buffer.add(text)}
          className="px-3 py-2 bg-gray-200 rounded hover:bg-gray-300"
        >
          Add Chunk
        </button>
        <button
          onClick={() => buffer.flush(true)}
          className="px-3 py-2 bg-gray-200 rounded hover:bg-gray-300"
        >
          Flush Now
        </button>
      </div>

      {/* Translated output (from server broadcast) */}
      {translated && (
        <div className="mt-6">
          <h3 className="text-lg font-medium text-gray-800 mb-2">🖥️ Translated Text:</h3>
          <div className="p-3 bg-gray-100 rounded-md border shadow-sm">
            {translated}
          </div>
        </div>
      )}

      {/* Controls */}
      <div className="flex gap-4 mt-6">
        <button
          onClick={handleClear}
          className="px-4 py-2 bg-red-500 text-white rounded hover:bg-red-600 transition"
        >
          Clear
        </button>
        <button
          onClick={() => setIsMuted(!isMuted)}
          className={`px-4 py-2 rounded transition ${isMuted ? 'bg-gray-400' : 'bg-green-500 text-white hover:bg-green-600'}`}
        >
          {isMuted ? 'Unmute' : 'Mute'}
        </button>
        <div className="flex items-center gap-2">
          <label className="text-gray-600">Volume</label>
          <input
            type="range"
            min={0}
            max={1}
            step={0.1}
            value={volume}
            onChange={(e) => setVolume(parseFloat(e.target.value))}
            className="w-24"
          />
        </div>
      </div>
      <div className="text-sm text-gray-500 mb-1">Partial (Deepgram):</div>
      <div className="min-h-6 text-gray-700 mb-3">{partial || "…"}</div>
      <div className="text-sm text-gray-600 mb-2">
        Deepgram: <b>{status}</b>{errorMsg ? ` · ${errorMsg}` : ""}
      </div>


      {/* Voice selection */}
      <div className="mt-6">
        <label className="text-gray-600 mb-2 block">Choose Voice</label>
        <select
          value={selectedVoiceName}
          onChange={(e) => setSelectedVoiceName(e.target.value)}
          className="p-2 border rounded shadow-sm focus:outline-none w-full"
        >
          {synthRef.current &&
            synthRef.current.getVoices().map((voice: SpeechSynthesisVoice) => (
              <option key={voice.name} value={voice.name}>
                {voice.name} ({voice.lang})
              </option>
            ))}
        </select>
      </div>
    </div>
  )
}
