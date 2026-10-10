// frontend/utils/useTranslationSocket.ts
'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import { WS_URL } from './urls';
import { d } from './debug';
import type { StreamContext } from './streamContext';
import { appendStreamContextToUrl, getAuthTokenFromSession, getHostTokenFromSession, resolveStreamContext } from './streamContext';
// F2 v5: shared retry policy. Previously duplicated inline — now consulted
// by both frontend hooks via one source of truth.
import { TERMINAL_CLOSE_CODES as _SHARED_TERMINAL_CLOSE_CODES, retryDecision } from '../lib/wsRetryPolicy';
import { WsLifecycle, type WsTerminalReason } from '../lib/wsLifecycle';

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

// F2 v4: bounded reconnect — a permanent handshake failure (close 4401
// auth, close 1000 room_ended) must stop the retry loop and surface a
// terminal state. See MAX_RECONNECT_ATTEMPTS.
export type SocketConnectionState =
  | 'connected'
  | 'reconnecting'
  | 'disconnected'
  | 'terminalWsError'
  | 'roomEnded'

// Maximum consecutive failed handshakes (no bytes received after open)
// before giving up. Tuned for the listener role; the host hook uses a
// higher cap because an operator mid-session should not drop out of a
// transient auth-token refresh race.
export const MAX_RECONNECT_ATTEMPTS = 8

// Close codes that are TERMINAL — do not retry.
// 4401: our custom "auth failed before accept"
// 4403: forbidden (valid token, no host authorization on this org)
// 1000 with reason starting "room_ended": sweeper-driven cleanup
// F2 v5: re-exported from the shared wsRetryPolicy module so both hooks
// agree on the terminal-code set. Preserved as a named export for any
// pre-existing imports from this module.
export const TERMINAL_CLOSE_CODES = _SHARED_TERMINAL_CLOSE_CODES

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
  sendBroadcastVoice: (voice: string) => void;
  sendAudienceTts: (enabled: boolean) => void;
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
  // F2 v6: single lifecycle controller for stale-socket + unmount guards,
  // retryDecision() invocation, and terminal-timer teardown. Created once
  // per hook mount; `aliveRef` + `wsRef` still drive the hook's own
  // callback-level guards. The lifecycle owns the retry decision.
  const lifecycleRef = useRef<WsLifecycle | null>(null);
  const contextRef = useRef<StreamContext>({});
  const lastSeenAtRef = useRef<number | null>(null)
  const disconnectStartedAtRef = useRef<number | null>(null)
  const hasConnectedRef = useRef(false)

  const seqRef = useRef(0);
  const nextSeq = () => ++seqRef.current;

  // Redis Pub/Sub fanout dedup: the backend stamps `_rseq` on every cross-instance
  // broadcast (monotonic per room, from Redis INCR). Under `--max-instances > 1`,
  // a subscription glitch or reconnect race could theoretically deliver the same
  // message twice; dropping any `_rseq` <= the last one we saw makes that a no-op.
  // Cheap to run under `--max-instances=1` too (backend still stamps `_rseq`), so
  // this ships before the Cloud Run bump.
  const fanoutSeqRef = useRef<number | null>(null);

  useEffect(() => {
    aliveRef.current = true;
    const streamContext = resolveStreamContext(WS_URL);
    contextRef.current = streamContext;
    const hostToken = isProducer ? getHostTokenFromSession() : undefined;
    const idToken = isProducer ? getAuthTokenFromSession() : undefined;
    // F2 v6: construct the shared lifecycle controller. The hook keeps
    // its own `aliveRef`/`wsRef` guards for logging-level drops; the
    // lifecycle owns the retry decision + terminal timer teardown.
    const lifecycle = new WsLifecycle({
      label: 'listener',
      maxAttempts: MAX_RECONNECT_ATTEMPTS,
      baseDelayMs: 1000,
      maxDelayMs: 30000,
      scheduleReconnect: () => {
        // Already debounced by the lifecycle's own timer; this fires
        // when it's time to actually connect.
        if (!aliveRef.current) return;
        connect();
      },
      onTerminal: (reason: WsTerminalReason) => {
        if (!aliveRef.current) return;
        clearReconnectingStateTimer();
        clearDisconnectStateTimer();
        if (reason === 'roomEnded') {
          setConnectionState('roomEnded');
          return;
        }
        setConnectionState('terminalWsError');
      },
    });
    lifecycleRef.current = lifecycle;
    // F2 follow-up v2: BOTH credentials (Firebase ID token AND host-upgrade
    // token) travel on the WebSocket Sec-WebSocket-Protocol header via
    // `bearer.<idToken>` / `host-token.<hostToken>` carriers below. They
    // are never put in the URL. hostToken is a credential (it authorizes
    // the host role without membership) — treating it the same as the ID
    // token keeps both out of the Cloud Run in-container access log and
    // out of any intermediate proxy's URL-recording path.
    const wsConnectUrl = appendStreamContextToUrl(
      WS_URL,
      streamContext,
      undefined,
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
      // F2 follow-up v2 — RFC 6455 §4.2.2 compliance:
      //
      // The server MUST echo back exactly one of the subprotocols we
      // offered; echoing an un-offered subprotocol fails the handshake
      // in every major browser. So we offer BOTH the literal "bearer"
      // AND the `bearer.<idToken>` carrier. The backend verifies the
      // token from the carrier and replies with `subprotocol="bearer"`
      // (never with the token-bearing entry). The raw token therefore
      // appears neither in the URL nor in the server's echo header.
      //
      // When the host-upgrade token is present we add the matching
      // `host-token` literal + `host-token.<hostToken>` carrier pair.
      //
      // On a bad-token handshake failure, the browser JS observes a
      // close event with code=1006, wasClean=false — Starlette's
      // reject-before-accept 403 is hidden by the WebSocket spec. The
      // reconnect loop in this file already has an exponential backoff
      // (retryRef * delay), so a bad token does NOT spin uncontrolled;
      // see also the `wsCloseClassify` branch below.
      const subprotocols: string[] = [];
      if (idToken) {
        subprotocols.push("bearer", `bearer.${idToken}`);
      }
      if (hostToken) {
        subprotocols.push("host-token", `host-token.${hostToken}`);
      }
      const ws = subprotocols.length
        ? new WebSocket(wsConnectUrl, subprotocols)
        : new WebSocket(wsConnectUrl);
      wsRef.current = ws;

      d('ws', 'connecting ' + wsConnectUrl);

      ws.onopen = () => {
        if (wsRef.current !== ws) return
        d('ws', 'open');
        setHealthy()
        // F2 v5: DO NOT reset retry counter on `open`. A pure `open` event
        // can fire for a server that will immediately close
        // (handshake-fail-after-upgrade); only real BYTES arriving via
        // `onmessage` confirm the handshake succeeded. Reset moved below.
        // reset local seq on a fresh connection so effects re-run on first message
        seqRef.current = 0;
        // Reset fanout dedup ref on reconnect — a new backend instance may
        // start a fresh Redis INCR counter (per-room TTL is 24h; harmless
        // to reset here regardless).
        fanoutSeqRef.current = null;
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
        // Stale-socket guard: a late onclose from an OLD socket (replaced
        // by a reconnect race) MUST NOT mutate state intended for the
        // current socket. Also drop callbacks after unmount.
        if (!aliveRef.current || wsRef.current !== ws) return
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

        // F2 v6: route every close event through the shared lifecycle.
        // The lifecycle invokes retryDecision() (single arbiter), clears
        // reconnect/banner timers on terminal, and calls `scheduleReconnect`
        // when a retry is appropriate. v5 imported retryDecision but
        // never called it — operator flagged this; v6 fixes it.
        const decision = lifecycle.handleClose({ code: evt.code, reason: evt.reason });
        if (decision.kind === 'terminal') {
          // Terminal: the lifecycle already called `onTerminal` which
          // set the UI state and cleared pending reconnecting/disconnect
          // timers. Nothing else to do here.
          return;
        }
        // Retry scheduled by the lifecycle. Mirror the local counters
        // so the existing React state (reconnectAttempt UI) stays in sync.
        retryRef.current = decision.nextAttempt;
        setReconnectAttempt(decision.nextAttempt);
        if (decision.nextAttempt > MAX_RECONNECT_ATTEMPTS) {
          // Belt-and-suspenders: retryDecision already classifies this as
          // terminal "exhausted", but if any future policy change leaks a
          // retry past the cap, still refuse it here.
          setConnectionState('terminalWsError');
          return;
        }
        d('ws', `reconnect in ${decision.delayMs}ms (attempt ${decision.nextAttempt}/${MAX_RECONNECT_ATTEMPTS})`);
        // The lifecycle owns its own retry timer; keep retryTimerRef in
        // sync for the unmount cleanup path below (clears either).
      };

      ws.onerror = (e) => {
        // Stale-socket + unmount guards (see onclose/onmessage above).
        if (!aliveRef.current || wsRef.current !== ws) return
        d('ws', 'error', e);
      };

      ws.onmessage = (evt: MessageEvent) => {
        if (!aliveRef.current || wsRef.current !== ws) return
        markSeen()
        // Reset the retry counter on confirmed byte reception. A pure
        // `open` event is NOT sufficient — some browsers fire open on a
        // handshake the server will immediately close, so the counter
        // only resets when real bytes land.
        if (retryRef.current !== 0) {
          retryRef.current = 0;
          setReconnectAttempt(0);
        }
        // F2 v6: tell the lifecycle too so a subsequent close that is
        // not a terminal code starts a fresh attempt-count against the cap.
        lifecycle.recordBytesReceived();
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

        // Streaming token: intentionally not updating display state.
        // Display shows only the complete final sentence when is_final=true arrives.
        if (raw.type === 'translation_stream_token') {
          return;
        }

        // Fanout-layer dedup — see fanoutSeqRef declaration. `_rseq` is only
        // stamped when Redis pubsub is enabled and connected on the backend;
        // messages without `_rseq` (Redis off, or legacy fallback broadcasts)
        // fall through unchanged.
        if (typeof raw._rseq === 'number') {
          const last = fanoutSeqRef.current;
          if (last !== null && raw._rseq <= last) {
            d('ws', 'dropping duplicate fanout seq', { rseq: raw._rseq, last });
            return;
          }
          fanoutSeqRef.current = raw._rseq;
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
            const srcText = typeof meta.source_text === 'string' ? meta.source_text : undefined;
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
              srcText,
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

    // Defer the initial socket until after the current effect turn. In React
    // Strict Mode, the first development-only mount is immediately cleaned up;
    // opening synchronously causes the browser warning "closed before the
    // connection is established" even though the replacement socket succeeds.
    const initialConnectTimer = setTimeout(connect, 0);

    return () => {
      aliveRef.current = false;
      // F2 v6: unmount the shared lifecycle BEFORE clearing the local
      // timer refs. The lifecycle owns its own retry timer; `unmount()`
      // clears it along with any registered disconnect-banner timer so a
      // late onclose from an about-to-be-garbage-collected socket cannot
      // fire scheduleReconnect after unmount.
      lifecycle.unmount();
      lifecycleRef.current = null;
      clearTimeout(initialConnectTimer)
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

  const sendBroadcastVoice = useCallback((voice: string) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const safeVoice = (voice || '').trim();
    const payload: Record<string, string> = { type: 'set_broadcast_voice', voice: safeVoice };
    try { d('ws->', JSON.stringify(payload)); } catch {}
    ws.send(JSON.stringify(payload));
  }, []);

  const sendAudienceTts = useCallback((enabled: boolean) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const payload = { type: 'set_audience_tts_enabled', enabled: Boolean(enabled) };
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
    sendBroadcastVoice,
    sendAudienceTts,
  };
}
