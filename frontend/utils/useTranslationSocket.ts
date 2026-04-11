// frontend/utils/useTranslationSocket.ts
'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import { WS_URL } from './urls';
import { d } from './debug';
import type { StreamContext } from './streamContext';
import { appendStreamContextToUrl, getAuthTokenFromSession, getHostTokenFromSession, resolveStreamContext } from './streamContext';

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

export type SocketConnectionState = 'connected' | 'reconnecting' | 'disconnected'

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
  connectionState: SocketConnectionState;
  reconnectAttempt: number;
  lastSeenAt: number | null;
  disconnectStartedAt: number | null;
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
  sendDisplayConfig: (speed: number) => void;
};

const HEARTBEAT_INTERVAL_MS = 10000
const HEARTBEAT_TIMEOUT_MS = 18000
const RECONNECTING_UI_DELAY_MS = 2000
const DISCONNECTED_UI_DELAY_MS = 3000

export function useTranslationSocket({ isProducer = false }: { isProducer?: boolean } = {}): TranslationSocketHook {
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const [connectionState, setConnectionState] = useState<SocketConnectionState>('reconnecting')
  const [reconnectAttempt, setReconnectAttempt] = useState(0)
  const [lastSeenAt, setLastSeenAt] = useState<number | null>(null)
  const [disconnectStartedAt, setDisconnectStartedAt] = useState<number | null>(null)
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
  const heartbeatTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectingStateTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const disconnectStateTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const aliveRef = useRef(true);
  const contextRef = useRef<StreamContext>({});
  const lastSeenAtRef = useRef<number | null>(null)
  const disconnectStartedAtRef = useRef<number | null>(null)
  const hasConnectedRef = useRef(false)

  const seqRef = useRef(0);
  const nextSeq = () => ++seqRef.current;

  useEffect(() => {
    aliveRef.current = true;
    const streamContext = resolveStreamContext(WS_URL);
    contextRef.current = streamContext;
    const hostToken = isProducer ? getHostTokenFromSession() : undefined;
    const idToken = isProducer ? getAuthTokenFromSession() : undefined;
    const wsConnectUrl = appendStreamContextToUrl(
      WS_URL,
      streamContext,
      hostToken || idToken
        ? {
            ...(hostToken ? { hostToken } : {}),
            ...(idToken ? { idToken } : {}),
          }
        : undefined,
    );

    // sanity: catch bad WS_URLs (double paths, missing scheme, etc.)
    if (!/^wss?:\/\/.+/.test(wsConnectUrl)) {
      console.warn('[ws] Suspicious WS_URL:', wsConnectUrl);
    }

    const clearHeartbeatTimer = () => {
      if (heartbeatTimerRef.current) {
        clearInterval(heartbeatTimerRef.current)
        heartbeatTimerRef.current = null
      }
    }

    const clearDisconnectStateTimer = () => {
      if (disconnectStateTimerRef.current) {
        clearTimeout(disconnectStateTimerRef.current)
        disconnectStateTimerRef.current = null
      }
    }

    const clearReconnectingStateTimer = () => {
      if (reconnectingStateTimerRef.current) {
        clearTimeout(reconnectingStateTimerRef.current)
        reconnectingStateTimerRef.current = null
      }
    }

    const markSeen = (seenAt = Date.now()) => {
      lastSeenAtRef.current = seenAt
      setLastSeenAt(seenAt)
    }

    const setHealthy = (seenAt = Date.now()) => {
      hasConnectedRef.current = true
      clearReconnectingStateTimer()
      clearDisconnectStateTimer()
      disconnectStartedAtRef.current = null
      setDisconnectStartedAt(null)
      setConnected(true)
      setConnectionState('connected')
      markSeen(seenAt)
    }

    const markUnhealthy = (startedAt = disconnectStartedAtRef.current ?? Date.now()) => {
      setConnected(false)
      disconnectStartedAtRef.current = startedAt
      setDisconnectStartedAt(startedAt)
      clearReconnectingStateTimer()
      clearDisconnectStateTimer()
      const elapsed = Date.now() - startedAt
      if (hasConnectedRef.current && elapsed < RECONNECTING_UI_DELAY_MS) {
        setConnectionState('connected')
        reconnectingStateTimerRef.current = setTimeout(() => {
          const activeWs = wsRef.current
          const socketOpen = !!activeWs && activeWs.readyState === WebSocket.OPEN
          if (!socketOpen && disconnectStartedAtRef.current === startedAt) {
            setConnectionState('reconnecting')
          }
        }, Math.max(0, RECONNECTING_UI_DELAY_MS - elapsed))
      } else if (elapsed < DISCONNECTED_UI_DELAY_MS) {
        setConnectionState('reconnecting')
      } else {
        setConnectionState('disconnected')
      }
      const remaining = Math.max(0, DISCONNECTED_UI_DELAY_MS - (Date.now() - startedAt))
      disconnectStateTimerRef.current = setTimeout(() => {
        const activeWs = wsRef.current
        const socketOpen = !!activeWs && activeWs.readyState === WebSocket.OPEN
        if (!socketOpen && disconnectStartedAtRef.current === startedAt) {
          setConnectionState('disconnected')
        }
      }, remaining)
    }

    const startHeartbeat = (ws: WebSocket) => {
      clearHeartbeatTimer()
      heartbeatTimerRef.current = setInterval(() => {
        if (!aliveRef.current || wsRef.current !== ws || ws.readyState !== WebSocket.OPEN) {
          clearHeartbeatTimer()
          return
        }
        const now = Date.now()
        const lastSeen = lastSeenAtRef.current ?? now
        if (now - lastSeen > HEARTBEAT_TIMEOUT_MS) {
          console.warn('[ws] heartbeat timeout', {
            url: wsConnectUrl,
            retryAttempt: retryRef.current,
            idleMs: now - lastSeen,
          })
          try {
            ws.close(4001, 'heartbeat_timeout')
          } catch {}
          return
        }
        try {
          ws.send(JSON.stringify({ type: 'ping', clientTs: now }))
        } catch (err) {
          d('ws', 'heartbeat send failed', err)
        }
      }, HEARTBEAT_INTERVAL_MS)
    }

    const connect = () => {
      if (!aliveRef.current) return;
      if (retryTimerRef.current) { clearTimeout(retryTimerRef.current); retryTimerRef.current = null; }
      const current = wsRef.current
      if (current && (current.readyState === WebSocket.OPEN || current.readyState === WebSocket.CONNECTING)) {
        return
      }
      markUnhealthy()
      const ws = new WebSocket(wsConnectUrl);
      wsRef.current = ws;

      d('ws', 'connecting ' + wsConnectUrl);

      ws.onopen = () => {
        if (wsRef.current !== ws) return
        d('ws', 'open');
        setHealthy()
        retryRef.current = 0;
        setReconnectAttempt(0)
        // reset local seq on a fresh connection so effects re-run on first message
        seqRef.current = 0;
        startHeartbeat(ws)
        try {
          const payload: Record<string, string> = { type: 'consumer_join', role: isProducer ? 'host' : 'listener' };
          if (streamContext.orgId) payload.orgId = streamContext.orgId;
          if (streamContext.roomId) payload.roomId = streamContext.roomId;
          if (streamContext.serviceKey) {
            payload.serviceKey = streamContext.serviceKey;
            payload.service_key = streamContext.serviceKey;
          }
          if (streamContext.churchSlug) payload.churchSlug = streamContext.churchSlug;
          if (hostToken) payload.hostToken = hostToken;
          if (idToken) payload.idToken = idToken;
          ws.send(JSON.stringify(payload));
        } catch {}
      };

      ws.onclose = (evt) => {
        if (wsRef.current !== ws) return
        wsRef.current = null
        clearHeartbeatTimer()
        d('ws', 'closed', { code: evt.code, reason: evt.reason, wasClean: evt.wasClean });
        console.warn('[ws] closed', {
          url: wsConnectUrl,
          code: evt.code,
          reason: evt.reason,
          wasClean: evt.wasClean,
          retryAttempt: retryRef.current,
        })
        markUnhealthy()
        if (!aliveRef.current) return;
        const nextAttempt = retryRef.current + 1
        retryRef.current = nextAttempt
        setReconnectAttempt(nextAttempt)
        const delay = Math.min(30000, 1000 * Math.pow(2, nextAttempt - 1));
        d('ws', `reconnect in ${delay}ms`);
        retryTimerRef.current = setTimeout(connect, delay);
      };

      ws.onerror = (e) => {
        if (wsRef.current !== ws) return
        d('ws', 'error', e);
      };

      ws.onmessage = (evt: MessageEvent) => {
        if (wsRef.current !== ws) return
        markSeen()
        // helpful one-line peek at traffic shape:
        // d('ws<-', (evt.data as string).slice(0, 200));
        let raw: any;
        try { raw = JSON.parse(evt.data as string); } catch { return; }
        if (!raw || typeof raw !== 'object') return;
        if (raw.type === 'pong') return;
        if (raw.type === 'ping') {
          try {
            ws.send(JSON.stringify({ type: 'pong', clientTs: raw.clientTs, serverTs: Date.now() }))
          } catch {}
          return
        }

        // Streaming token: append tokens progressively until final message arrives
        if (raw.type === 'translation_stream_token') {
          const token = typeof raw.token === 'string' ? raw.token : '';
          const tokenSeq = typeof raw.seq === 'number' ? raw.seq : -1;
          if (!token || tokenSeq < 0) return;
          setLast(prev => {
            const isNewSeq = prev.seq !== tokenSeq;
            return {
              ...prev,
              text: isNewSeq ? token : prev.text + token,
              seq: tokenSeq,
              meta: { ...(prev.meta ?? {}), is_final: false },
            };
          });
          return;
        }

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
      clearHeartbeatTimer()
      clearReconnectingStateTimer()
      clearDisconnectStateTimer()
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
      try { wsRef.current?.close(); } catch {}
      wsRef.current = null
      setConnected(false)
    };
  }, [isProducer]);

  // Producer → server
  const sendProducerText = useCallback(
    (text: string, source: string, target: string, isPartial: boolean, id?: number, rev?: number, finalFlag?: boolean) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const ctx = contextRef.current;

      const payload = isPartial
        ? { type: 'producer_partial', text, source, target }
        : { type: 'producer_commit', text, source, target, id, rev, final: !!finalFlag };
      if (ctx.orgId) (payload as any).orgId = ctx.orgId;
      if (ctx.roomId) (payload as any).roomId = ctx.roomId;
      if (ctx.serviceKey) (payload as any).serviceKey = ctx.serviceKey;
      if (ctx.churchSlug) (payload as any).churchSlug = ctx.churchSlug;
      const hostToken = isProducer ? getHostTokenFromSession() : undefined;
      const idToken = isProducer ? getAuthTokenFromSession() : undefined;
      if (hostToken) (payload as any).hostToken = hostToken;
      if (idToken) (payload as any).idToken = idToken;

      try { d('ws->', JSON.stringify(payload)); } catch {}
      ws.send(JSON.stringify(payload));
    },
    []
  );

  const sendDisplayConfig = useCallback((speed: number) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const safeSpeed = Number.isFinite(speed) ? speed : 1;
    const payload = { type: 'display_config', speed: safeSpeed };
    try { d('ws->', JSON.stringify(payload)); } catch {}
    ws.send(JSON.stringify(payload));
  }, []);

  return {
    connected,
    connectionState,
    reconnectAttempt,
    lastSeenAt,
    disconnectStartedAt,
    last,
    sendProducerText,
    sendDisplayConfig,
  };
}
