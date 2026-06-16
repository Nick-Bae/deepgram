import { useCallback, useEffect, useRef } from "react";


export function usePcmAudioPlayer(enabled: boolean) {
  const contextRef = useRef<AudioContext | null>(null);
  const nextStartRef = useRef(0);
  const sourcesRef = useRef<Set<AudioBufferSourceNode>>(new Set());
  const enabledRef = useRef(enabled);

  useEffect(() => {
    enabledRef.current = enabled;
  }, [enabled]);

  const ensureContext = useCallback(() => {
    if (typeof window === "undefined") return null;
    const AudioCtor =
      window.AudioContext ||
      (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioCtor) return null;
    if (!contextRef.current) {
      contextRef.current = new AudioCtor({ sampleRate: 24000 });
    }
    return contextRef.current;
  }, []);

  const unlock = useCallback(async () => {
    const context = ensureContext();
    if (context && context.state !== "running") {
      await context.resume();
    }
  }, [ensureContext]);

  const stop = useCallback(() => {
    for (const source of sourcesRef.current) {
      try { source.stop(); } catch {}
    }
    sourcesRef.current.clear();
    nextStartRef.current = 0;
  }, []);

  const enqueue = useCallback((base64Pcm: string, sampleRate = 24000) => {
    if (!enabledRef.current || !base64Pcm) return false;
    const context = ensureContext();
    if (!context || context.state !== "running") return false;

    let binary = "";
    try {
      binary = window.atob(base64Pcm);
    } catch {
      return false;
    }
    const sampleCount = Math.floor(binary.length / 2);
    if (!sampleCount) return false;

    const audioBuffer = context.createBuffer(1, sampleCount, sampleRate);
    const channel = audioBuffer.getChannelData(0);
    for (let i = 0; i < sampleCount; i++) {
      const low = binary.charCodeAt(i * 2);
      const high = binary.charCodeAt(i * 2 + 1);
      const signed = (high << 8) | low;
      channel[i] = (signed & 0x8000 ? signed - 0x10000 : signed) / 0x8000;
    }

    const source = context.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(context.destination);
    const startAt = Math.max(context.currentTime + 0.03, nextStartRef.current);
    source.start(startAt);
    nextStartRef.current = startAt + audioBuffer.duration;
    sourcesRef.current.add(source);
    source.onended = () => sourcesRef.current.delete(source);
    return true;
  }, [ensureContext]);

  useEffect(() => {
    if (!enabled) stop();
  }, [enabled, stop]);

  useEffect(() => () => {
    stop();
    void contextRef.current?.close();
    contextRef.current = null;
  }, [stop]);

  return { enqueue, unlock, stop };
}
