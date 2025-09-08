// frontend/lib/useSpeak.ts
import { useEffect, useRef, useState } from "react";

export function useSpeak(targetLang: string = "en") {
  const voicesRef = useRef<SpeechSynthesisVoice[]>([]);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    function load() {
      voicesRef.current = window.speechSynthesis.getVoices();
      setReady(true);
    }
    load();
    window.speechSynthesis.onvoiceschanged = load;
  }, []);

  function speak(text: string) {
    if (!text) return;
    const u = new SpeechSynthesisUtterance(text);
    const langPrefix = (targetLang || "en").toLowerCase();
    const voice = voicesRef.current.find(v => (v.lang || "").toLowerCase().startsWith(langPrefix));
    if (voice) u.voice = voice;
    u.lang = voice?.lang || (targetLang === "zh-CN" ? "zh-CN" : targetLang || "en-US");
    window.speechSynthesis.cancel();  // optional: interrupt previous
    window.speechSynthesis.speak(u);
  }

  return { ready, speak };
}
