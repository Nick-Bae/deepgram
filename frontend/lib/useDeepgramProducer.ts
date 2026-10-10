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
import { isTerminalRoomClose } from "./wsCloseClassify";
// F2 v5: shared retry policy. Previously duplicated inline — now consulted
// by both frontend hooks via one source of truth. See wsRetryPolicy.ts.
import { TERMINAL_CLOSE_CODES, retryDecision } from "./wsRetryPolicy";

type StartOptions = {
  sourceLang?: string;
  targetLang?: string;
  earlyCommit?: boolean;
  engine?: "deepgram" | "openai-realtime-translate" | "gemini-live-translate";
  orgId?: string;
  roomId?: string;
  serviceKey?: string;
  churchSlug?: string;
};

export type DeepgramProducerController = {
  status: "idle" | "starting" | "streaming" | "stopped" | "error";
  partial: string;
  lastCommit: string;
  lastTranslation: DeepgramTranslationEvent | null;
  errorMsg: string | null;
  inputLevel: number;
  inputMuted: boolean;
  start: (options?: StartOptions) => Promise<void>;
  setInputMuted: (muted: boolean) => void;
  stop: () => void;
  finalize: () => void;
};

export type DeepgramTranslationEvent = {
  text: string;
  lang: string;
  seq: number;
  srcText?: string;
  srcLang?: string;
  meta?: Record<string, unknown>;
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
    : opts?.engine === "gemini-live-translate"
      ? "/ws/stt/gemini-live-translate"
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
  // F2 follow-up v2: BOTH credentials travel on the WebSocket
  // Sec-WebSocket-Protocol header, never in the URL. The host-upgrade
  // token (`hostToken`) IS a credential — it authorizes host role
  // without membership — so it is NOT appended to the URL query here.
  // See the `new WebSocket(url, [...])` call site below for the
  // RFC-6455-compliant subprotocol list.

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
  const [lastTranslation, setLastTranslation] = useState<DeepgramTranslationEvent | null>(null);
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
  const fallbackSeqRef = useRef(0);

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

  function shutdownProducer(updateState = true, reason = "shutdown") {
    shouldRunRef.current = false;
    terminalErrorRef.current = false;
    startOptionsRef.current = undefined;
    streamContextRef.current = {};
    clearReconnectTimer();
    try {
      const closeReason = reason.slice(0, 120);
      wsRef.current?.close(1000, closeReason);
      console.warn("[FE][DG][shutdown]", { reason: closeReason, updateState });
    } catch {}
    wsRef.current = null;
    releaseMediaPipeline(updateState);
    applyInputMuted(false, updateState);
    if (updateState) {
      setStatus("stopped");
      setPartial("");
    }
  }

  // F2 v5: bounded reconnect uses the shared `retryDecision` policy
  // (lib/wsRetryPolicy.ts). Host cap is higher than the listener cap
  // because a mid-session host should not get dropped by a transient
  // ID-token refresh race. `scheduleReconnect` observes the last close
  // code so terminal 4401/4403 — already handled in `onclose` — never
  // reach this path; `exhausted` is the normal end state.
  const MAX_RECONNECT_ATTEMPTS = 12;
  const lastCloseCodeRef = useRef<number>(1006);
  const lastCloseReasonRef = useRef<string>("");

  function scheduleReconnect() {
    if (!shouldRunRef.current) return;
    clearReconnectTimer();
    const attempt = reconnectAttemptRef.current;
    const decision = retryDecision({
      attempt,
      maxAttempts: MAX_RECONNECT_ATTEMPTS,
      closeCode: lastCloseCodeRef.current,
      closeReason: lastCloseReasonRef.current,
      baseDelayMs: 600,
      maxDelayMs: 8000,
      jitter01: Math.random(),
    });
    if (decision.kind === "terminal") {
      console.warn("[deepgram-producer] terminal", { reason: decision.reason, attempts: attempt });
      shouldRunRef.current = false;
      releaseMediaPipeline();
      applyInputMuted(false);
      setErrorMsg(
        decision.reason === "auth"
          ? "Authentication failed. Please sign in again."
          : decision.reason === "forbidden"
          ? "You are not authorized to host this room."
          : decision.reason === "roomEnded"
          ? "Room has ended."
          : "Lost connection and could not reconnect. Please refresh and sign in again."
      );
      setStatus("error");
      return;
    }
    reconnectAttemptRef.current = decision.nextAttempt;
    setStatus("starting");
    reconnectTimerRef.current = setTimeout(() => {
      reconnectTimerRef.current = null;
      connectWebSocket();
    }, decision.delayMs);
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
    // F2 follow-up v2 — RFC 6455 §4.2.2 compliance:
    //
    // The browser requires the server to echo back one of the
    // subprotocols we offered. We offer BOTH the literal "bearer" AND
    // the `bearer.<idToken>` carrier. The backend verifies the token
    // from the carrier and replies with `subprotocol="bearer"`
    // (never with the token-bearing entry). The host-upgrade token
    // follows the same pattern via the `host-token` literal + carrier
    // pair. Neither credential ever appears in the URL.
    //
    // On a bad-token reject-before-accept, the browser JS observes
    // close code=1006, wasClean=false (the WebSocket spec hides the
    // underlying 403 from JS). The reconnect loop in this file already
    // bounds attempts via `reconnectAttemptRef` + the terminal-close
    // classifier, so a bad token does not spin unbounded.
    const idTokenForProtocol = getAuthTokenFromSession();
    const hostTokenForProtocol = getHostTokenFromSession();
    const subprotocols: string[] = [];
    if (idTokenForProtocol) {
      subprotocols.push("bearer", `bearer.${idTokenForProtocol}`);
    }
    if (hostTokenForProtocol) {
      subprotocols.push("host-token", `host-token.${hostTokenForProtocol}`);
    }
    try {
      const ws = subprotocols.length
        ? new WebSocket(url, subprotocols)
        : new WebSocket(url);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      // Note: We intentionally do NOT reset reconnectAttemptRef on open.
      // A pure `open` event can fire for a server that will immediately
      // close (RFC 6455 §4.2.2 handshake-fail-after-upgrade); only real
      // bytes arriving via onmessage confirm the handshake succeeded.
      // Reset is moved to onmessage below.
      ws.onopen = () => {
        if (wsRef.current !== ws) return;
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

      ws.onclose = (event) => {
        // F2 v6 — stale-socket + unmount guard. A late onclose from an
        // OLD socket (whose replacement was already constructed) MUST
        // NOT mutate the replacement's state. Without this guard a
        // delayed 4401 from a prior socket would release the mic +
        // terminate the active host session that just succeeded. Same
        // hazard after unmount — `shouldRunRef` is already false, but
        // the terminal/release branch below runs regardless of that
        // flag, so we have to bail at the TOP.
        if (!shouldRunRef.current || wsRef.current !== ws) {
          // If this is a replaced socket, close/null the local handle
          // only (do not touch wsRef — that now points at the live one).
          return;
        }
        wsRef.current = null;
        // F2 v5: record close code/reason for `scheduleReconnect` to
        // feed into the shared retry-policy decision function.
        lastCloseCodeRef.current = event.code || 1006;
        lastCloseReasonRef.current = event.reason || "";
        // F2 v5: terminal auth (4401 bad bearer, 4403 host-forbidden)
        // released the media pipeline immediately — no retry, no mic
        // reprompt loop. The subprotocol-reject frame is observable in
        // the test harness via `event.code`; browsers may surface it as
        // 1006 instead (RFC 6455 hides the HTTP 403 from JS), and the
        // attempts cap handles that case.
        if (TERMINAL_CLOSE_CODES.has(event.code)) {
          console.warn("[FE][DG][terminal-auth]", { code: event.code });
          shouldRunRef.current = false;
          terminalErrorRef.current = true;
          clearReconnectTimer();
          releaseMediaPipeline();
          applyInputMuted(false);
          setErrorMsg(
            event.code === 4401
              ? "Authentication failed. Please sign in again."
              : "You are not authorized to host this room."
          );
          setStatus("error");
          return;
        }
        // Terminal (room_ended / 4001) vs transient (1001, 1006,
        // 1012 SIGTERM from Uvicorn, network drops). Classify BEFORE
        // the shouldRunRef / terminalErrorRef checks so a room_ended
        // close doesn't fall through into a reconnect loop.
        const isTerminalRoom = isTerminalRoomClose({ code: event.code, reason: event.reason });
        console.warn("[FE][DG][socket-closed]", {
          code: event.code,
          reason: event.reason || "",
          wasClean: event.wasClean,
          isTerminalRoom,
          reconnecting: !isTerminalRoom
            && shouldRunRef.current
            && !terminalErrorRef.current,
        });
        if (isTerminalRoom) {
          // The room is over. Stop producing, release mic + audio
          // pipeline, do not reconnect. Without this, a host whose
          // room ended would loop reconnecting forever (or bounce
          // between reconnect and backend re-closes).
          shouldRunRef.current = false;
          terminalErrorRef.current = false;
          clearReconnectTimer();
          releaseMediaPipeline();
          applyInputMuted(false);
          setStatus("stopped");
          setPartial("");
          return;
        }
        if (terminalErrorRef.current) {
          setStatus("error");
          return;
        }
        if (!shouldRunRef.current) {
          setStatus("stopped");
          return;
        }
        // Transient close (1001, 1006, 1012, other). Preserve the
        // running intent AND the media pipeline — reconnect uses
        // the existing mic stream without re-prompting the browser.
        scheduleReconnect();
      };

      ws.onerror = () => {
        // F2 v6 — stale/unmount guard: a late onerror from a replaced
        // socket must not surface an error on the live session.
        if (!shouldRunRef.current || wsRef.current !== ws) return;
        setErrorMsg("WebSocket error");
        setStatus("error");
        try { ws.close(); } catch {}
      };

      ws.onmessage = (e) => {
        // F2 v6 — stale/unmount guard (prevents a replaced socket from
        // (a) resetting the live socket's retry counter via a late
        // message, (b) feeding stray data into application state).
        if (!shouldRunRef.current || wsRef.current !== ws) return;
        // Confirmed byte reception: handshake truly succeeded. Only now
        // reset the retry counter. See the comment on `ws.onopen` above.
        if (reconnectAttemptRef.current !== 0) {
          reconnectAttemptRef.current = 0;
        }
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
          if (typeof msg.text === "string" && msg.mode) {
            const meta = msg.meta && typeof msg.meta === "object" ? msg.meta as Record<string, unknown> : {};
            const seq = typeof msg.seq === "number" ? msg.seq : ++fallbackSeqRef.current;
            const text = msg.text || "";
            setLastCommit(text);
            setLastTranslation({
              text,
              lang: typeof msg?.tgt?.lang === "string" ? msg.tgt.lang : "en",
              seq,
              srcText: typeof msg?.src?.text === "string" ? msg.src.text : undefined,
              srcLang: typeof msg?.src?.lang === "string" ? msg.src.lang : undefined,
              meta: {
                ...meta,
                is_final: typeof meta.is_final === "boolean" ? meta.is_final : msg.mode === "live",
              },
            });
          }
          if (msg.type === "translation") {
            const meta = msg.meta && typeof msg.meta === "object" ? msg.meta as Record<string, unknown> : {};
            const text = msg.payload || "";
            const seq = typeof meta.seq === "number" ? meta.seq : ++fallbackSeqRef.current;
            setLastCommit(text);
            setLastTranslation({
              text,
              lang: typeof msg.lang === "string" ? msg.lang : "en",
              seq,
              srcText: typeof meta.source_text === "string" ? meta.source_text : undefined,
              srcLang: typeof meta.source_lang === "string" ? meta.source_lang : undefined,
              meta,
            });
          }
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
        try { wsRef.current?.close(1000, "restart_stale_socket"); } catch {}
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
      shutdownProducer(false, "start_error");
      applyInputMuted(false);
      setErrorMsg(message);
      setStatus("error");
      throw (err instanceof Error ? err : new Error(message));
    }
  }

  function stop() {
    shutdownProducer(true, "manual_stop");
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
      shutdownProducer(true, "pagehide");
    };

    window.addEventListener("pagehide", handlePageHide);
    return () => {
      window.removeEventListener("pagehide", handlePageHide);
      shutdownProducer(false, "component_unmount");
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return {
    status,
    partial,
    lastCommit,
    lastTranslation,
    errorMsg,
    inputLevel,
    inputMuted,
    start,
    setInputMuted,
    stop,
    finalize: finalizeCurrentUtterance,
  };
}
