// utils/useSubtitleSocket.ts
import { useEffect, useMemo, useRef, useState } from "react";

type InterimKR = { type: "interim_kr"; text: string };
type FinalKR   = { type: "final_kr";  text: string };
type FastFinal = { type: "fast_final"; en: string; from?: string };
type Translation = {
  type: "translation";
  payload?: string;
  lang?: string;
  meta?: {
    translated?: string;
    partial?: boolean;
    seq?: number;
    [key: string]: unknown;
  };
};

function isInterim(m: any): m is InterimKR { return m && m.type === "interim_kr" && typeof m.text === "string"; }
function isFinalKR(m: any): m is FinalKR   { return m && m.type === "final_kr"  && typeof m.text === "string"; }
function isFastFinal(m: any): m is FastFinal { return m && m.type === "fast_final" && typeof m.en === "string"; }
function isTranslation(m: any): m is Translation { return m && m.type === "translation"; }

type Options = {
  maxLines?: number;           // how many lines to keep on screen
  track?: "en" | "kr" | "both" // which language(s) to keep as lines
};

export function useSubtitleSocket(explicitUrl?: string, opts: Options = {}) {
  const maxLines = Math.max(1, opts.maxLines ?? 3);
  const track = opts.track ?? "en";

  const [connected, setConnected] = useState(false);

  // live preview (KR interim)
  const [krInterim, setKrInterim] = useState("");

  // latest single finals (if you still want them)
  const [krFinal, setKrFinal] = useState("");
  const [enFinal, setEnFinal] = useState("");

  // NEW: rolling lines
  const [krLines, setKrLines] = useState<string[]>([]);
  const [enLines, setEnLines] = useState<string[]>([]);

  // throttle interim
  const rafId = useRef(0);
  const pendingInterim = useRef<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const backoff = useRef(0);
  const stopFlag = useRef(false);

  // Resolve viewing WS URL (append role=viewer)
  const resolvedUrl = useMemo(() => {
    const withViewer = (u: string) => (u.includes("?") ? `${u}&role=viewer` : `${u}?role=viewer`);
    if (explicitUrl) return withViewer(explicitUrl);

    const env = process.env.NEXT_PUBLIC_WS_URL;
    if (env && /^wss?:\/\//i.test(env)) return withViewer(env);

    if (typeof window !== "undefined") {
      const { protocol, host } = window.location;
      const wsProto = protocol === "https:" ? "wss:" : "ws:";
      return `${wsProto}//${host}/ws/translate?role=viewer`;
    }
    return "";
  }, [explicitUrl]);

  function splitSentences(text: string): string[] {
    const normalized = text.replace(/\r/g, "").trim();
    if (!normalized) return [];
    const sentenceSplit = normalized
      .split(/(?<=[.!?。？！])\s+(?=[A-Z가-힣0-9])/u)
      .map((s) => s.trim())
      .filter(Boolean);
    if (sentenceSplit.length > 1) return sentenceSplit;
    const newlineSplit = normalized.split(/\n+/).map((s) => s.trim()).filter(Boolean);
    if (newlineSplit.length > 1) return newlineSplit;
    return [normalized];
  }

  // --- English display pacing state ---
  type DisplayEntry = { id: number; seq: number | null; text: string; addedAt: number };
  const enQueueRef = useRef<DisplayEntry[]>([]);
  const enDisplayRef = useRef<DisplayEntry[]>([]);
  const nextSlotAtRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  const idCounterRef = useRef(0);

  function pushLine(setter: React.Dispatch<React.SetStateAction<string[]>>, text: string) {
    setter((prev) => prev.concat(text).slice(-maxLines));
  }

  function computeLingerMs(text: string) {
    const len = text.length;
    const base = 900;            // short sentences get ~1.4s
    const perChar = 55;          // scale with length so long sentences stay longer
    const minMs = 1400;
    const maxMs = 4200;
    return Math.max(minMs, Math.min(maxMs, base + len * perChar));
  }

  function scheduleDrain() {
    if (timerRef.current !== null) return;
    const delay = Math.max(0, nextSlotAtRef.current - Date.now());
    timerRef.current = window.setTimeout(drainQueue, delay);
  }

  function replaceSeq(seq: number | null, entries: DisplayEntry[]): boolean {
    // Try replacing in the pending queue first (latest block with this seq).
    for (let i = enQueueRef.current.length - 1; i >= 0; i--) {
      if (enQueueRef.current[i].seq === seq) {
        let start = i;
        while (start > 0 && enQueueRef.current[start - 1].seq === seq) start--;
        enQueueRef.current.splice(start, enQueueRef.current.length - start, ...entries);
        return true;
      }
      if (enQueueRef.current[i].seq !== seq) break;
    }

    // Otherwise, replace the tail block already on screen.
    for (let i = enDisplayRef.current.length - 1; i >= 0; i--) {
      if (enDisplayRef.current[i].seq === seq) {
        let start = i;
        while (start > 0 && enDisplayRef.current[start - 1].seq === seq) start--;
        let end = i + 1;
        while (end < enDisplayRef.current.length && enDisplayRef.current[end].seq === seq) end++;
        const preservedAddedAt = enDisplayRef.current[start]?.addedAt ?? Date.now();
        const stamped = entries.map((e) => ({ ...e, addedAt: preservedAddedAt }));
        enDisplayRef.current.splice(start, end - start, ...stamped);
        enDisplayRef.current = enDisplayRef.current.slice(-maxLines);
        setEnLines(enDisplayRef.current.map((e) => e.text));
        return true;
      }
      if (enDisplayRef.current[i].seq !== seq) break;
    }
    return false;
  }

  function drainQueue() {
    timerRef.current = null;
    const now = Date.now();
    if (now < nextSlotAtRef.current - 5) {
      scheduleDrain();
      return;
    }
    const next = enQueueRef.current.shift();
    if (!next) return;

    const entry = { ...next, addedAt: now };
    enDisplayRef.current = enDisplayRef.current.concat(entry).slice(-maxLines);
    setEnLines(enDisplayRef.current.map((e) => e.text));

    const dwell = computeLingerMs(entry.text);
    nextSlotAtRef.current = now + dwell;
    scheduleDrain();
  }

  function enqueueEnglish(seq: number | null, lines: string[]) {
    if (!lines.length) return;
    const entries = lines.map((text) => ({
      id: idCounterRef.current++,
      seq,
      text,
      addedAt: 0,
    }));

    if (replaceSeq(seq, entries)) {
      // Updated content for the same segment; no need to change pacing.
      return;
    }

    enQueueRef.current.push(...entries);
    scheduleDrain();
  }

  useEffect(() => {
    if (!resolvedUrl) return;
    stopFlag.current = false;
    enQueueRef.current = [];
    enDisplayRef.current = [];
    nextSlotAtRef.current = 0;
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    setEnLines([]);
    setKrLines([]);

    function scheduleReconnect() {
      if (stopFlag.current) return;
      backoff.current = Math.min(backoff.current * 2 || 800, 8000);
      const jitter = 0.5 + Math.random() * 0.5;
      setTimeout(connect, Math.round(backoff.current * jitter));
    }

    function connect() {
      if (stopFlag.current) return;
      try {
        const ws = new WebSocket(resolvedUrl);
        wsRef.current = ws;

        ws.onopen = () => { setConnected(true); backoff.current = 0; };
        ws.onclose = () => { setConnected(false); wsRef.current = null; scheduleReconnect(); };
        ws.onerror = () => { /* close will follow */ };

        ws.onmessage = (e) => {
          try {
            const raw = typeof e.data === "string" ? e.data : new TextDecoder().decode(e.data);
            const msg: unknown = JSON.parse(raw);

            if (isInterim(msg)) {
              const txt = msg.text || "";
              pendingInterim.current = txt;
              if (!rafId.current) {
                rafId.current = requestAnimationFrame(() => {
                  rafId.current = 0;
                  setKrInterim(pendingInterim.current || "");
                  pendingInterim.current = null;
                });
              }
              return;
            }

            if (isFinalKR(msg)) {
              const t = (msg.text || "").trim();
              setKrFinal(t);
              setKrInterim("");
              if (track === "kr" || track === "both") pushLine(setKrLines, t);
              return;
            }

            if (isTranslation(msg)) {
              const seq =
                typeof msg.meta?.seq === "number"
                  ? msg.meta.seq
                  : typeof (msg.meta as any)?.segment_id === "number"
                  ? Number((msg.meta as any).segment_id)
                  : null;
              if (msg.meta?.partial || seq === null) {
                return; // ignore previews lacking a final sequence id
              }
              const text =
                (typeof msg.payload === "string" && msg.payload) ||
                (typeof msg.meta?.translated === "string" && msg.meta.translated) ||
                "";
              const t = text.trim();
              if (!t) return;
              setEnFinal(t);
              if (track === "en" || track === "both") enqueueEnglish(seq, splitSentences(t));
              return;
            }

            if (isFastFinal(msg)) {
              // Skip fast_final previews on the public display to avoid showing drafts
              return;
            }
          } catch { /* ignore non-JSON */ }
        };
      } catch {
        scheduleReconnect();
      }
    }

    connect();
    return () => {
      stopFlag.current = true;
      if (rafId.current) cancelAnimationFrame(rafId.current);
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      try { wsRef.current?.close(); } catch {}
      wsRef.current = null;
    };
  }, [resolvedUrl, track, maxLines]);

  return {
    connected,
    // live preview
    krInterim,
    // latest final (single)
    krFinal,
    enFinal,
    // rolling lines for multi-line display
    krLines,
    enLines,
  };
}
