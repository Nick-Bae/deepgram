// ✅ TranslationForm.tsx (with Web Speech API and volume/mute controls)
// Path: frontend/components/TranslationForm.tsx

'use client'

import { useState, useRef, useEffect } from 'react'

export default function TranslationForm() {
  const [text, setText] = useState('')
  const [translated, setTranslated] = useState('')
  const [loading, setLoading] = useState(false)
  const [isMuted, setIsMuted] = useState(false)
  const [volume, setVolume] = useState(1)

  const synthRef = useRef<SpeechSynthesis | null>(null)

  useEffect(() => {
    if (typeof window === 'undefined') return
    synthRef.current = window.speechSynthesis
  }, [])


  const handleTranslate = async () => {
    if (!text.trim()) return
    setLoading(true)
    try {
      const response = await fetch('http://localhost:8000/api/translate', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          text: text,
          source: 'Korean',
          target: 'Chinese',
        }),
      })
      const data = await response.json()
      setTranslated(data.translated || 'Translation failed')
      if (!isMuted && data.translated) {
        const utterance = new SpeechSynthesisUtterance(data.translated)
        utterance.lang = 'zh-CN'
        utterance.volume = volume // Volume: 0.0 to 1.0
        synthRef.current?.speak(utterance)
      }
    } catch (error) {
      console.error('Error translating:', error)
      setTranslated('Error during translation')
    }
    setLoading(false)
  }

  return (
    <div className="w-full max-w-2xl mx-auto p-4 bg-white rounded-xl shadow-md">
      <h2 className="text-xl font-semibold text-gray-700 mb-2">Korean to Chinese Translation</h2>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        className="w-full h-32 p-2 border border-gray-300 rounded-md mb-4"
        placeholder="Enter Korean sermon line here..."
      ></textarea>
      <div className="flex items-center gap-4 mb-4">
        <button
          onClick={handleTranslate}
          disabled={loading}
          className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700 transition"
        >
          {loading ? 'Translating...' : 'Translate'}
        </button>
        <button
          onClick={() => setIsMuted(!isMuted)}
          className={`px-4 py-2 rounded ${isMuted ? 'bg-gray-400' : 'bg-green-600 text-white'} hover:opacity-90 transition`}
        >
          {isMuted ? 'Unmute' : 'Mute'}
        </button>
        <div className="flex items-center">
          <label className="mr-2 text-gray-600">Volume</label>
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
      {translated && (
        <div className="mt-6">
          <h3 className="text-lg font-medium text-gray-800 mb-2">Translated Text (Chinese):</h3>
          <div className="p-3 bg-gray-100 rounded-md border border-gray-300 whitespace-pre-wrap">
            {translated}
          </div>
        </div>
      )}
    </div>
  )
}
