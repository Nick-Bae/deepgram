// frontend/lib/useDeepgramProducer.ts
import { useRef, useState } from "react";

export type DeepgramProducerController = {
  status: "idle" | "starting" | "streaming" | "stopped" | "error";
  partial: string;
  lastCommit: string;
  errorMsg: string | null;
  start: () => Promise<void>;
  stop: () => void;
  finalize: () => void;
};

function wsDeepgramURL() {
  const env = process.env.NEXT_PUBLIC_WS_URL || "";
  try {
    if (env.startsWith("ws")) {
      const u = new URL(env);
      u.pathname = ""; u.search = ""; u.hash = "";
      return `${u.toString().replace(/\/$/, "")}/ws/stt/deepgram`;
    }
  } catch { }
  const u = new URL(window.location.href);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  u.pathname = ""; u.search = ""; u.hash = "";
  return `${u.toString().replace(/\/$/, "")}/ws/stt/deepgram`;
}

const PCM_WORKLET_INLINE = `
class PCMWorkletProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
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

  const wsRef = useRef<WebSocket | null>(null);
  const portRef = useRef<MessagePort | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);

  async function start() {
    try {
      if (status === "streaming") return;
      setStatus("starting"); setErrorMsg(null);

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
      const worklet = new AudioWorkletNode(ctx, "pcm-worklet", { numberOfInputs: 1, numberOfOutputs: 0 });
      src.connect(worklet);
      portRef.current = worklet.port;

      const url = wsDeepgramURL();
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      ws.onopen = () => setStatus("streaming");
      ws.onclose = () => setStatus("stopped");
      ws.onerror = () => { setErrorMsg("WebSocket error"); setStatus("error"); };
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === "error") { setErrorMsg(msg.message || "Server error"); return; }
          if (msg.type === "stt.partial") setPartial(msg.text || "");
          if (msg.type === "translation") setLastCommit(msg.payload || "");
        } catch { }
      };
      wsRef.current = ws;

      portRef.current.onmessage = (evt: MessageEvent) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(evt.data); // 16-bit PCM @ 48k
      };
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setErrorMsg(message);
      setStatus("error");
    }
  }

  function stop() {
    try {
      wsRef.current?.close(); wsRef.current = null;
      portRef.current?.close?.(); portRef.current = null;
      ctxRef.current?.close(); ctxRef.current = null;
      streamRef.current?.getTracks().forEach(t => t.stop());
      streamRef.current = null;
    } finally {
      setStatus("stopped"); setPartial("");
    }
  }

  function finalizeCurrentUtterance() {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try {
      ws.send(JSON.stringify({ type: "finalize" }));
    } catch {}
  }

  return { status, partial, lastCommit, errorMsg, start, stop, finalize: finalizeCurrentUtterance };
}
