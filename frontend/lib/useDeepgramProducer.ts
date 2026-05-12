// frontend/lib/useDeepgramProducer.ts
import { useEffect, useRef, useState } from "react";
import {
  getAuthTokenFromSession,
  getHostTokenFromSession,
  persistStreamContext,
  resolveStreamContext,
  type StreamContext,
} from "../utils/streamContext";
import { enforceSecureProtocol } from "../utils/urls";

type StartOptions = {
  sourceLang?: string;
  targetLang?: string;
  earlyCommit?: boolean;
  engine?: "deepgram" | "openai-realtime-translate";
  orgId?: string;
  roomId?: string;
  serviceKey?: string;
  churchSlug?: string;
};

export type DeepgramProducerController = {
  status: "idle" | "starting" | "streaming" | "stopped" | "error";
  partial: string;
  lastCommit: string;
  errorMsg: string | null;
  inputLevel: number;
  inputMuted: boolean;
  start: (options?: StartOptions) => Promise<void>;
  setInputMuted: (muted: boolean) => void;
  stop: () => void;
  finalize: () => void;
};

const INPUT_LEVEL_NOISE_FLOOR = 0.012;
const INPUT_LEVEL_RELEASE = 0.72;
const INPUT_LEVEL_ATTACK = 0.42;
const INPUT_LEVEL_EMIT_INTERVAL_MS = 50;

function sanitizeLang(code?: string) {
  if (!code) return "";
  return code.trim().toLowerCase();
}

function hasSessionCredentials() {
  return Boolean(getHostTokenFromSession() || getAuthTokenFromSession());
}

function wsDeepgramURL(opts?: StartOptions, streamContext?: StreamContext) {
  const env = enforceSecureProtocol(process.env.NEXT_PUBLIC_WS_URL || "");
  const path = opts?.engine === "openai-realtime-translate"
    ? "/ws/stt/openai-realtime-translate"
    : "/ws/stt/deepgram";
  const params = new URLSearchParams();
  const src = sanitizeLang(opts?.sourceLang);
  const tgt = sanitizeLang(opts?.targetLang);
  const early = opts?.earlyCommit ? "1" : "0";
  if (src) params.set("source", src);
  if (tgt) params.set("target", tgt);
  if (early === "1") params.set("early", "1");
  if (streamContext?.serviceKey) params.set("serviceKey", streamContext.serviceKey);
  if (streamContext?.orgId) params.set("orgId", streamContext.orgId);
  if (streamContext?.roomId) params.set("roomId", streamContext.roomId);
  if (streamContext?.churchSlug) params.set("churchSlug", streamContext.churchSlug);
  const hostToken = getHostTokenFromSession();
  if (hostToken) params.set("hostToken", hostToken);
  const idToken = getAuthTokenFromSession();
  if (idToken) params.set("idToken", idToken);

  const suffix = params.toString() ? `?${params.toString()}` : "";
  try {
    if (env.startsWith("ws")) {
      const u = new URL(env);
      u.pathname = ""; u.search = ""; u.hash = "";
      return `${u.toString().replace(/\/$/, "")}${path}${suffix}`;
    }
  } catch { }
  const u = new URL(window.location.href);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  u.pathname = ""; u.search = ""; u.hash = "";
  return `${u.toString().replace(/\/$/, "")}${path}${suffix}`;
}

const PCM_WORKLET_INLINE = `
class PCMWorkletProcessor extends AudioWorkletProcessor {
  process(inputs, outputs) {
    const input = inputs[0];
    const output = outputs[0];
    if (output) {
      for (const channel of output) {
        channel.fill(0);
      }
    }
    if (!input || !input[0]) return true;
    const samples = input[0];
    const buffer = new ArrayBuffer(samples.length * 2);
    const view = new DataView(buffer);
    for (let i = 0; i < samples.length; i++) {
      const clamped = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    }
    this.port.postMessage(buffer, [buffer]);
    return true;
  }
}
registerProcessor('pcm-worklet', PCMWorkletProcessor);
`;

function resolveWorkletUrl(base: string): string {
  if (/^https?:\/\//i.test(base)) return base;
  if (typeof window === 'undefined') return base;
  const url = new URL(base, window.location.origin);
  if (window.location.protocol === 'https:' && url.protocol === 'http:') {
    url.protocol = 'https:';
  }
  return url.toString();
}

async function addInlineWorklet(ctx: AudioContext) {
  const blob = new Blob([PCM_WORKLET_INLINE], { type: 'application/javascript' });
  const blobUrl = URL.createObjectURL(blob);
  try {
    await ctx.audioWorklet.addModule(blobUrl);
  } finally {
    URL.revokeObjectURL(blobUrl);
  }
}

async function ensurePcmWorklet(ctx: AudioContext) {
  const override = process.env.NEXT_PUBLIC_PCM_WORKLET_URL;
  if (!override) {
    await addInlineWorklet(ctx);
    return;
  }

  const target = resolveWorkletUrl(override);
  try {
    await ctx.audioWorklet.addModule(target);
  } catch (err) {
    console.warn('[DG] audio worklet load failed, falling back to inline blob', err);
    await addInlineWorklet(ctx);
  }
}

export function useDeepgramProducer(): DeepgramProducerController {
  const [status, setStatus] = useState<"idle" | "starting" | "streaming" | "stopped" | "error">("idle");
  const [partial, setPartial] = useState("");
  const [lastCommit, setLastCommit] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [inputLevel, setInputLevel] = useState(0);
  const [inputMuted, setInputMutedState] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const portRef = useRef<MessagePort | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptRef = useRef(0);
  const shouldRunRef = useRef(false);
  const startOptionsRef = useRef<StartOptions | undefined>(undefined);
  const streamContextRef = useRef<StreamContext>({});
  const terminalErrorRef = useRef(false);
  const inputLevelRef = useRef(0);
  const inputMutedRef = useRef(false);
  const lastInputLevelEmitRef = useRef(0);

  function resetInputLevel(updateState = true) {
    inputLevelRef.current = 0;
    lastInputLevelEmitRef.current = 0;
    if (updateState) setInputLevel(0);
  }

  function applyInputMuted(nextMuted: boolean, updateState = true) {
    inputMutedRef.current = nextMuted;
    resetInputLevel(updateState);
    if (updateState) setInputMutedState(nextMuted);
  }

  function updateInputLevel(pcmBuffer: ArrayBuffer) {
    const samples = new Int16Array(pcmBuffer);
    if (!samples.length) return;

    let sumSquares = 0;
    for (let i = 0; i < samples.length; i++) {
      const normalized = samples[i] / 0x8000;
      sumSquares += normalized * normalized;
    }

    const rms = Math.sqrt(sumSquares / samples.length);
    const gated = rms <= INPUT_LEVEL_NOISE_FLOOR ? 0 : rms;
    const previous = inputLevelRef.current;
    const attack = gated > previous ? INPUT_LEVEL_ATTACK : 1 - INPUT_LEVEL_RELEASE;
    const smoothed = previous + (gated - previous) * attack;
    const nextLevel = smoothed < 0.004 ? 0 : smoothed;

    inputLevelRef.current = nextLevel;

    const now = typeof performance !== "undefined" ? performance.now() : Date.now();
    if (
      now - lastInputLevelEmitRef.current >= INPUT_LEVEL_EMIT_INTERVAL_MS ||
      (nextLevel === 0 && previous !== 0)
    ) {
      lastInputLevelEmitRef.current = now;
      setInputLevel(nextLevel);
    }
  }

  function clearReconnectTimer() {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }

  function releaseMediaPipeline(updateState = true) {
    try { portRef.current?.close?.(); } catch {}
    portRef.current = null;
    try { ctxRef.current?.close(); } catch {}
    ctxRef.current = null;
    try { streamRef.current?.getTracks().forEach((t) => t.stop()); } catch {}
    streamRef.current = null;
    resetInputLevel(updateState);
  }

  function shutdownProducer(updateState = true) {
    shouldRunRef.current = false;
    terminalErrorRef.current = false;
    startOptionsRef.current = undefined;
    streamContextRef.current = {};
    clearReconnectTimer();
    try { wsRef.current?.close(); } catch {}
    wsRef.current = null;
    releaseMediaPipeline(updateState);
    applyInputMuted(false, updateState);
    if (updateState) {
      setStatus("stopped");
      setPartial("");
    }
  }

  function scheduleReconnect() {
    if (!shouldRunRef.current) return;
    clearReconnectTimer();
    const attempt = reconnectAttemptRef.current++;
    const baseDelay = Math.min(8000, 600 * Math.pow(2, attempt));
    const jitter = 0.5 + Math.random() * 0.5;
    const delay = Math.round(baseDelay * jitter);
    setStatus("starting");
    reconnectTimerRef.current = setTimeout(() => {
      reconnectTimerRef.current = null;
      connectWebSocket();
    }, delay);
  }

  function connectWebSocket() {
    if (!shouldRunRef.current) return;
    if (!hasSessionCredentials()) {
      shouldRunRef.current = false;
      clearReconnectTimer();
      releaseMediaPipeline();
      applyInputMuted(false);
      setErrorMsg("Host session ended. Please sign in again.");
      setStatus("error");
      return;
    }
    const url = wsDeepgramURL(startOptionsRef.current, streamContextRef.current);
    try {
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = () => {
        if (wsRef.current !== ws) return;
        reconnectAttemptRef.current = 0;
        terminalErrorRef.current = false;
        setErrorMsg(null);
        setStatus("streaming");
        try {
          const payload: Record<string, string> = { type: "producer_join", role: "host" };
          const ctx = streamContextRef.current;
          if (ctx.orgId) payload.orgId = ctx.orgId;
          if (ctx.roomId) payload.roomId = ctx.roomId;
          if (ctx.serviceKey) {
            payload.serviceKey = ctx.serviceKey;
            payload.service_key = ctx.serviceKey;
          }
          if (ctx.churchSlug) payload.churchSlug = ctx.churchSlug;
          const hostToken = getHostTokenFromSession();
          if (hostToken) payload.hostToken = hostToken;
          const idToken = getAuthTokenFromSession();
          if (idToken) payload.idToken = idToken;
          ws.send(JSON.stringify(payload));
        } catch {}
      };

      ws.onclose = () => {
        if (wsRef.current === ws) wsRef.current = null;
        if (terminalErrorRef.current) {
          setStatus("error");
          return;
        }
        if (!shouldRunRef.current) {
          setStatus("stopped");
          return;
        }
        scheduleReconnect();
      };

      ws.onerror = () => {
        setErrorMsg("WebSocket error");
        setStatus("error");
        try { ws.close(); } catch {}
      };

      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === "error") {
            terminalErrorRef.current = true;
            applyInputMuted(false);
            setErrorMsg(msg.message || "Server error");
            setStatus("error");
            shouldRunRef.current = false;
            clearReconnectTimer();
            try { ws.close(); } catch {}
            wsRef.current = null;
            releaseMediaPipeline();
            return;
          }
          if (msg.type === "stt.partial") setPartial(msg.text || "");
          if (msg.type === "translation") setLastCommit(msg.payload || "");
        } catch {}
      };
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setErrorMsg(message || "WebSocket connect failed");
      setStatus("error");
      scheduleReconnect();
    }
  }

  async function start(options?: StartOptions) {
    try {
      if (shouldRunRef.current) {
        const wsState = wsRef.current?.readyState;
        const activeSocket = wsState === WebSocket.CONNECTING || wsState === WebSocket.OPEN;
        if (status === "streaming" || status === "starting" || activeSocket) return;
        // Recover from stale error states where shouldRunRef stayed true.
        shouldRunRef.current = false;
        clearReconnectTimer();
        try { wsRef.current?.close(); } catch {}
        wsRef.current = null;
        releaseMediaPipeline();
      }
      if (!hasSessionCredentials()) {
        throw new Error("Please sign in again.");
      }
      shouldRunRef.current = true;
      terminalErrorRef.current = false;
      startOptionsRef.current = options;
      const resolvedContext = resolveStreamContext();
      streamContextRef.current = persistStreamContext({
        orgId: options?.orgId ?? resolvedContext.orgId,
        roomId: options?.roomId ?? resolvedContext.roomId,
        serviceKey: options?.serviceKey ?? resolvedContext.serviceKey,
        churchSlug: options?.churchSlug ?? resolvedContext.churchSlug,
      });
      reconnectAttemptRef.current = 0;
      clearReconnectTimer();
      setStatus("starting");
      setErrorMsg(null);

      const AudioCtor =
        window.AudioContext ||
        (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;

      if (!AudioCtor) {
        throw new Error("Web Audio API is not supported in this browser");
      }

      const ctx = new AudioCtor({ sampleRate: 48000 });
      ctxRef.current = ctx;
      await ensurePcmWorklet(ctx);

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          sampleRate: 48000,
          echoCancellation: true,       // ✅ key
          noiseSuppression: true,       // ✅ helps
          autoGainControl: false
        }
      });
      streamRef.current = stream;

      const src = ctx.createMediaStreamSource(stream);
      const worklet = new AudioWorkletNode(ctx, "pcm-worklet", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
      });
      const mutedMonitor = ctx.createGain();
      mutedMonitor.gain.value = 0;
      src.connect(worklet);
      worklet.connect(mutedMonitor);
      mutedMonitor.connect(ctx.destination);
      portRef.current = worklet.port;

      portRef.current.onmessage = (evt: MessageEvent) => {
        if (inputMutedRef.current) {
          resetInputLevel();
          return;
        }
        updateInputLevel(evt.data as ArrayBuffer);
        const ws = wsRef.current;
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(evt.data); // 16-bit PCM @ 48k
      };

      if (ctx.state !== "running") {
        await ctx.resume();
      }
      if (ctx.state !== "running") {
        throw new Error("Microphone audio context did not start");
      }
      connectWebSocket();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      shutdownProducer(false);
      applyInputMuted(false);
      setErrorMsg(message);
      setStatus("error");
      throw (err instanceof Error ? err : new Error(message));
    }
  }

  function stop() {
    shutdownProducer(true);
  }

  function setInputMuted(muted: boolean) {
    applyInputMuted(Boolean(muted));
  }

  function finalizeCurrentUtterance() {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try {
      ws.send(JSON.stringify({ type: "finalize" }));
    } catch {}
  }

  useEffect(() => {
    if (typeof window === "undefined") return;

    const handlePageHide = () => {
      shutdownProducer(true);
    };

    window.addEventListener("pagehide", handlePageHide);
    return () => {
      window.removeEventListener("pagehide", handlePageHide);
      shutdownProducer(false);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return {
    status,
    partial,
    lastCommit,
    errorMsg,
    inputLevel,
    inputMuted,
    start,
    setInputMuted,
    stop,
    finalize: finalizeCurrentUtterance,
  };
}
