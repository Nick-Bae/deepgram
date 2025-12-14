// frontend/utils/useTranslationSocket.ts
'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import { WS_URL } from './urls';
import { d } from './debug';

type Meta = {
  translated?: string;
  mode?: 'pre' | 'realtime' | 'live';
  match_score?: number;
  matched_source?: string | null;
  partial?: boolean;
  segment_id?: string | number;
  rev?: number;
  seq?: number;
  is_final?: boolean;
  kind?: string;
  reference?: string;
  reference_en?: string;
  reference_ko?: string;
  version?: string;
  source_version?: string;
  book?: string;
  book_en?: string;
  chapter?: number;
  verse?: number;
  end_verse?: number;
  source_text?: string;
  fail_open?: boolean;
  reason?: string;
  code?: string;
  message?: string;
  provider?: string;
};

type ServerBroadcast = {
  type: 'translation';
  payload: string;
  lang: string;
  meta?: Meta;
};

type ServerReply = {
  translated: string;
  mode: 'pre' | 'realtime';
  match_score: number;
  matched_source?: string | null;
  original?: string;
  method?: string;
};

type ServerLive = {
  mode: 'live' | 'pre' | 'realtime';
  text: string;
  seq?: number;
  src?: { text?: string; lang?: string };
  tgt?: { lang?: string };
};

export type LastState = {
  text: string;
  lang: string;
  mode: 'pre' | 'realtime';
  matchScore: number;
  matchedSource: string | null;
  preview?: string;
  segmentId?: string | number;
  rev?: number;
  seq: number;               // <- make non-optional for simpler logic
  srcText?: string;
  srcLang?: string;
  meta?: Meta;
};

export type TranslationSocketHook = {
  connected: boolean;
  last: LastState;
  sendProducerText: (
    text: string,
    source: string,
    target: string,
    isPartial: boolean,
    id?: number,
    rev?: number,
    finalFlag?: boolean
  ) => void;
};

export function useTranslationSocket({ isProducer = false }: { isProducer?: boolean } = {}): TranslationSocketHook {
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const [last, setLast] = useState<LastState>({
    text: '',
    lang: 'en',
    mode: 'realtime',
    matchScore: 0,
    matchedSource: null,
    seq: 0,                  // <- start at 0
    meta: undefined,
  });

  const retryRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const aliveRef = useRef(true);

  const seqRef = useRef(0);
  const nextSeq = () => ++seqRef.current;

  useEffect(() => {
    aliveRef.current = true;

    // sanity: catch bad WS_URLs (double paths, missing scheme, etc.)
    if (!/^wss?:\/\/.+/.test(WS_URL)) {
      console.warn('[ws] Suspicious WS_URL:', WS_URL);
    }

    const connect = () => {
      if (!aliveRef.current) return;
      if (retryTimerRef.current) { clearTimeout(retryTimerRef.current); retryTimerRef.current = null; }

      try { wsRef.current?.close(); } catch {}
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      d('ws', 'connecting ' + WS_URL);
      setConnected(false);

      ws.onopen = () => {
        d('ws', 'open');
        setConnected(true);
        retryRef.current = 0;
        // reset local seq on a fresh connection so effects re-run on first message
        seqRef.current = 0;
        if (!isProducer) {
          try { ws.send(JSON.stringify({ type: 'consumer_join' })); } catch {}
        }
      };

      ws.onclose = () => {
        d('ws', 'closed');
        setConnected(false);
        if (!aliveRef.current) return;
        const delay = Math.min(30000, 1000 * Math.pow(2, retryRef.current++));
        d('ws', `reconnect in ${delay}ms`);
        retryTimerRef.current = setTimeout(connect, delay);
      };

      ws.onerror = (e) => {
        d('ws', 'error', e);
      };

      ws.onmessage = (evt: MessageEvent) => {
        // helpful one-line peek at traffic shape:
        // d('ws<-', (evt.data as string).slice(0, 200));
        let raw: any;
        try { raw = JSON.parse(evt.data as string); } catch { return; }
        if (!raw || typeof raw !== 'object') return;

        // Shape 3: { mode: 'live'|'pre'|'realtime', text, seq?, src?, tgt? }
        if (typeof raw.text === 'string' && raw.mode) {
          const mode = (raw.mode === 'live' ? 'realtime' : raw.mode) as 'pre' | 'realtime';
          const seq = typeof raw.seq === 'number' ? raw.seq : nextSeq();
          const srcText = typeof raw?.src?.text === 'string' ? raw.src.text : undefined;
          const srcLang = typeof raw?.src?.lang === 'string' ? raw.src.lang : undefined;
          const liveMeta: Meta = typeof raw.meta === 'object' && raw.meta
            ? { ...raw.meta }
            : {};
          if (typeof liveMeta.is_final !== 'boolean') {
            liveMeta.is_final = raw.mode === 'live';
          }

          setLast({
            text: raw.text,
            lang: (raw.tgt?.lang as string) || 'en',
            mode,
            matchScore: 0,
            matchedSource: null,
            preview: undefined,
            segmentId: seq,
            rev: 0,
            seq,
            srcText,
            srcLang,
            meta: liveMeta,
          });
          return;
        }

        // Shape 1: { type: 'translation', payload, lang, meta }
        if (raw.type === 'translation') {
          const b = raw as ServerBroadcast;
          const meta = b.meta ?? {};
          const isPartial = !!meta.partial;
          const segId = meta.segment_id;
          const rev = typeof meta.rev === 'number' ? meta.rev : 0;
          const seq = typeof meta.seq === 'number' ? meta.seq : nextSeq();

          if (isPartial) {
            setLast((prev) => ({
              ...prev,
              preview: b.payload ?? meta.translated ?? '',
              segmentId: segId,
              rev,
              seq,     // track latest seq even on partials (safe)
              meta: { ...(prev.meta ?? {}), ...meta },
            }));
          } else {
            setLast({
              text: b.payload ?? meta.translated ?? '',
              lang: b.lang ?? 'en',
              mode: (meta.mode === 'live' ? 'realtime' : (meta.mode as 'pre' | 'realtime')) ?? 'realtime',
              matchScore: typeof meta.match_score === 'number' ? meta.match_score : 0,
              matchedSource: (meta.matched_source as string) ?? null,
              preview: undefined,
              segmentId: segId,
              rev,
              seq,
              meta,
            });
          }
          return;
        }

        // Shape 2: { translated, mode, ... }
        if ('translated' in raw) {
          const r = raw as ServerReply;
          setLast({
            text: r.translated,
            lang: 'en',
            mode: r.mode,
            matchScore: r.match_score,
            matchedSource: r.matched_source ?? null,
            preview: undefined,
            segmentId: undefined,
            rev: 0,
            seq: nextSeq(),
            meta: undefined,
          });
          return;
        }

        // ignore everything else
      };
    };

    connect();

    return () => {
      aliveRef.current = false;
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
      try { wsRef.current?.close(); } catch {}
    };
  }, [isProducer]);

  // Producer → server
  const sendProducerText = useCallback(
    (text: string, source: string, target: string, isPartial: boolean, id?: number, rev?: number, finalFlag?: boolean) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;

      const payload = isPartial
        ? { type: 'producer_partial', text, source, target }
        : { type: 'producer_commit', text, source, target, id, rev, final: !!finalFlag };

      try { d('ws->', JSON.stringify(payload)); } catch {}
      ws.send(JSON.stringify(payload));
    },
    []
  );

  return { connected, last, sendProducerText };
}
