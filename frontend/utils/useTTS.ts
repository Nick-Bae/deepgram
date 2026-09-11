// frontend/utils/useTTS.ts
import { useCallback, useEffect, useRef, useState } from 'react'
import { d, pv } from './debug'

type SpeakMsg = { id: number; rev: number; text: string; final?: boolean }

const langToBcp47 = (code: string) => {
  switch (code) {
    case 'ko': return 'ko-KR'
    case 'en': return 'en-US'
    case 'es': return 'es-ES'
    case 'zh-CN': return 'zh-CN'
    case 'hi': return 'hi-IN'
    default: return 'en-US'
  }
}

export function useTTS(targetLang: string, opts?: { early?: boolean }) {
  const early = !!opts?.early
  const [enabled, setEnabled] = useState(false)
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>([])
  const spokenPairsRef = useRef<Set<string>>(new Set())
  const spokenIdsRef   = useRef<Set<number>>(new Set())

  useEffect(() => {
    if (typeof window === 'undefined' || !('speechSynthesis' in window)) return
    const update = () => setVoices(window.speechSynthesis.getVoices() || [])
    update()
    window.speechSynthesis.onvoiceschanged = update
    return () => { window.speechSynthesis.onvoiceschanged = null as any }
  }, [])

  const pickVoice = useCallback((lang: string) => {
    const v = voices.find(v => v.lang?.toLowerCase().startsWith(lang.toLowerCase()))
          || voices.find(v => v.lang?.toLowerCase().startsWith(lang.slice(0,2)))
          || voices[0]
    return v
  }, [voices])

  const speakCommit = useCallback((msg: SpeakMsg) => {
    if (!enabled) { d('tts', 'disabled; skipping'); return }
    if (typeof window === 'undefined' || !('speechSynthesis' in window)) return

    if (early) {
      if (spokenIdsRef.current.has(msg.id)) { d('tts', `id ${msg.id} already spoken`); return }
      if (msg.rev !== 1) { d('tts', `early mode: rev=${msg.rev} skip`); return }
    } else {
      if (!msg.final) { d('tts', 'non-final; skipping'); return }
    }

    const pairKey = `${msg.id}:${msg.rev}`
    if (spokenPairsRef.current.has(pairKey)) return

    const lang = langToBcp47(targetLang)
    const u = new SpeechSynthesisUtterance(msg.text)
    u.lang = lang
    const v = pickVoice(lang); if (v) u.voice = v
    u.rate = 1.0; u.pitch = 1.0
    u.onstart = () => d('tts', `▶ speak ${pairKey} "${pv(msg.text)}"`)
    u.onend   = () => d('tts', `■ done  ${pairKey}`)
    u.onerror = (e) => d('tts', `!! error ${pairKey}`, e)
    try { window.speechSynthesis.resume() } catch {}
    window.speechSynthesis.speak(u)

    spokenPairsRef.current.add(pairKey)
    if (early) spokenIdsRef.current.add(msg.id)
  }, [enabled, early, pickVoice, targetLang])

  const cancelAll = useCallback(() => {
    if (typeof window === 'undefined' || !('speechSynthesis' in window)) return
    window.speechSynthesis.cancel()
    spokenPairsRef.current.clear()
    spokenIdsRef.current.clear()
  }, [])

  return { enabled, setEnabled, speakCommit, cancelAll, speak: (text: string) => {
    if (!enabled) return
    const lang = langToBcp47(targetLang)
    const u = new SpeechSynthesisUtterance(text)
    u.lang = lang
    const v = pickVoice(lang); if (v) u.voice = v
    try { window.speechSynthesis.resume() } catch {}
    window.speechSynthesis.speak(u)
  } }
}
