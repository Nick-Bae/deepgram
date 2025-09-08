// frontend/pages/producer.tsx
import { useRef, useState } from "react";

function wsDeepgramURL() {
  const env = process.env.NEXT_PUBLIC_WS_URL || "";
  try {
    if (env.startsWith("ws")) {
      const u = new URL(env);
      u.pathname = ""; u.search = ""; u.hash = "";
      return `${u.toString().replace(/\/$/, "")}/ws/stt/deepgram`;
    }
  } catch {}
  const u = new URL(window.location.href);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  u.pathname = ""; u.search = ""; u.hash = "";
  return `${u.toString().replace(/\/$/, "")}/ws/stt/deepgram`;
}

export default function Producer() {
  const [status, setStatus] = useState<"idle"|"starting"|"streaming"|"stopped"|"error">("idle");
  const [partial, setPartial] = useState("");
  const [lastCommit, setLastCommit] = useState("");
  const [errorMsg, setErrorMsg] = useState<string|null>(null);

  const wsRef = useRef<WebSocket|null>(null);
  const portRef = useRef<MessagePort|null>(null);
  const ctxRef = useRef<AudioContext|null>(null);
  const streamRef = useRef<MediaStream|null>(null);

  async function start() {
    try {
      if (status === "streaming") return;
      setStatus("starting"); setErrorMsg(null);

      const ctx = new (window.AudioContext || (window as any).webkitAudioContext)({ sampleRate: 48000 });
      ctxRef.current = ctx;
      await ctx.audioWorklet.addModule("/workers/pcm-worklet-processor.js");

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      });
      streamRef.current = stream;

      const src = ctx.createMediaStreamSource(stream);
      // @ts-ignore
      const worklet = new AudioWorkletNode(ctx, "pcm-worklet", { numberOfInputs: 1, numberOfOutputs: 0 });
      src.connect(worklet);
      portRef.current = worklet.port;

      const url = wsDeepgramURL();
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      ws.onopen = () => setStatus("streaming");
      ws.onclose  = () => setStatus("stopped");
      ws.onerror  = () => { setErrorMsg("WebSocket error"); setStatus("error"); };
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === "error") { setErrorMsg(msg.message || "Server error"); return; }
          if (msg.type === "stt.partial") setPartial(msg.text || "");
          if (msg.type === "translation") setLastCommit(msg.payload || "");
        } catch {}
      };
      wsRef.current = ws;

      portRef.current.onmessage = (evt: MessageEvent) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(evt.data); // 16-bit PCM @ 48k
      };
    } catch (err: any) {
      setErrorMsg(err?.message || String(err));
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

  return (
    <div style={{ padding: 16 }}>
      <h1>Producer (Deepgram)</h1>
      <p>Status: <b>{status}</b></p>
      {errorMsg && <p style={{color:"tomato"}}>Error: {errorMsg}</p>}
      <button onClick={start} disabled={status==="streaming"}>Start</button>
      <button onClick={stop} disabled={status!=="streaming"}>Stop</button>

      <h3>Partial</h3>
      <div style={{ fontSize: 20, minHeight: 32 }}>{partial}</div>

      <h3>Last Commit (translated)</h3>
      <div style={{ fontSize: 24, minHeight: 40 }}>{lastCommit}</div>
    </div>
  );
}
