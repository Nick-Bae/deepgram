import Head from "next/head";
import Link from "next/link";
import { useRouter } from "next/router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { API_URL, WS_URL } from "../../../../utils/urls";
import { useSubtitleSocket } from "../../../../utils/useSubtitleSocket";
import { appendStreamContextToUrl, clearRoomInSession, persistStreamContext } from "../../../../utils/streamContext";
import { useTTS } from "../../../../utils/useTTS";
import { usePcmAudioPlayer } from "../../../../utils/usePcmAudioPlayer";

type ResolveResponse = {
  orgId: string;
  slug: string;
  serviceKey: string;
  activeRoomId: string | null;
  roomStatus: string;
  lastRoomId?: string | null;
  lastRoomStatus?: string | null;
  lastEndReason?: string | null;
  languagePair?: { source?: string; target?: string };
  service?: { title?: string; timezone?: string };
};

type LatestSegmentsResponse = {
  activeRoomId: string | null;
  roomStatus: string;
  segments?: Array<{
    seq?: number;
    koreanText?: string;
    englishText?: string;
  }>;
};

const RESOLVE_POLL_MS = 8000;
const SEGMENT_FALLBACK_POLL_MS = 2000;
const DISPLAY_TOGGLE_KEY = "f";
type DisplayMode = "subtitle" | "fullScreen";

function friendlyError(msg: string): string {
  if (msg.includes("404")) return "Service not found. Check your link and try again.";
  if (msg.includes("403")) return "Access denied.";
  if (msg.includes("500") || msg.includes("502") || msg.includes("503")) return "Server error. Will retry automatically.";
  if (msg.includes("Failed to fetch") || msg.includes("NetworkError")) return "Network error. Check your connection.";
  return "Could not connect. Will retry automatically.";
}

function roomEndMessage(reason?: string | null): string | null {
  switch ((reason || "").trim()) {
    case "trial_expired":
      return "Broadcast stopped: trial minutes exhausted.";
    case "monthly_limit_reached":
      return "Broadcast stopped: monthly limit reached.";
    case "host_absent":
      return "Broadcast stopped: host connection was lost.";
    case "idle_timeout":
      return "Broadcast stopped: no audio was detected.";
    case "max_duration":
      return "Broadcast stopped: maximum broadcast duration reached.";
    case "host_end":
      return "Broadcast ended.";
    default:
      return null;
  }
}

export default function ChurchServiceListenerPage() {
  const router = useRouter();
  const slug = typeof router.query.churchSlug === "string" ? router.query.churchSlug : "";
  const serviceKey = typeof router.query.serviceKey === "string" ? router.query.serviceKey : "";

  const [loading, setLoading] = useState(true);
  const [resolveData, setResolveData] = useState<ResolveResponse | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [displayMode, setDisplayMode] = useState<DisplayMode>("subtitle");
  const [fallbackKrLines, setFallbackKrLines] = useState<string[]>([]);
  const [fallbackEnLines, setFallbackEnLines] = useState<string[]>([]);
  const fallbackSeqRef = useRef(0);

  const toggleDisplayMode = useCallback(() => {
    setDisplayMode((prev) => (prev === "subtitle" ? "fullScreen" : "subtitle"));
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() !== DISPLAY_TOGGLE_KEY) return;
      event.preventDefault();
      toggleDisplayMode();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [toggleDisplayMode]);

  useEffect(() => {
    if (!slug || !serviceKey) return;
    let disposed = false;

    const fetchResolve = async () => {
      if (typeof document !== "undefined" && document.hidden) return;
      try {
        const res = await fetch(`${API_URL}/api/c/${encodeURIComponent(slug)}/s/${encodeURIComponent(serviceKey)}/resolve`);
        if (!res.ok) {
          throw new Error(`resolve_failed_${res.status}`);
        }
        const data: ResolveResponse = await res.json();
        if (disposed) return;
        setResolveData(data);
        setErrorMsg(null);
        persistStreamContext({
          orgId: data.orgId,
          roomId: data.activeRoomId || undefined,
          serviceKey: data.serviceKey || serviceKey,
          churchSlug: data.slug || slug,
        });
        if (!data.activeRoomId) clearRoomInSession();
      } catch (err: unknown) {
        if (disposed) return;
        const message = err instanceof Error ? err.message : String(err);
        setErrorMsg(message || "resolve_failed");
      } finally {
        if (!disposed) setLoading(false);
      }
    };

    fetchResolve();
    const timer = window.setInterval(fetchResolve, RESOLVE_POLL_MS);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, [serviceKey, slug]);

  const socketEnabled = !!resolveData?.activeRoomId && resolveData?.roomStatus === "live";
  const scopedWsUrl = useMemo(() => {
    if (!socketEnabled || !resolveData?.orgId || !resolveData.activeRoomId) return undefined;
    return appendStreamContextToUrl(
      WS_URL,
      {
        orgId: resolveData.orgId,
        roomId: resolveData.activeRoomId,
        serviceKey: resolveData.serviceKey || serviceKey,
        churchSlug: resolveData.slug || slug,
      },
      { role: "viewer" },
    );
  }, [resolveData, serviceKey, slug, socketEnabled]);

  const targetLang = resolveData?.languagePair?.target || "en";
  const { enabled: ttsEnabled, setEnabled: setTtsEnabled, speak } = useTTS(targetLang);
  const { enqueue: enqueuePcmAudio, unlock: unlockPcmAudio } = usePcmAudioPlayer(ttsEnabled);
  const lastNativeAudioAtRef = useRef(0);
  const handleTranslatedAudio = useCallback((data: string, sampleRate: number) => {
    if (enqueuePcmAudio(data, sampleRate)) {
      lastNativeAudioAtRef.current = Date.now();
    }
  }, [enqueuePcmAudio]);

  const { connected, krLines, enLines } = useSubtitleSocket(scopedWsUrl, {
    maxLines: 4,
    track: "both",
    enabled: socketEnabled,
    onTranslatedAudio: handleTranslatedAudio,
  });

  const lastSpokenLineRef = useRef("");

  useEffect(() => {
    fallbackSeqRef.current = 0;
    setFallbackKrLines([]);
    setFallbackEnLines([]);
  }, [resolveData?.activeRoomId]);

  useEffect(() => {
    if (!slug || !serviceKey || !socketEnabled) return;
    let disposed = false;
    const pollSegments = async () => {
      if (typeof document !== "undefined" && document.hidden) return;
      try {
        const params = new URLSearchParams({
          after_seq: String(fallbackSeqRef.current || 0),
          limit: "10",
        });
        const res = await fetch(
          `${API_URL}/api/c/${encodeURIComponent(slug)}/s/${encodeURIComponent(serviceKey)}/segments/latest?${params.toString()}`,
        );
        if (!res.ok) return;
        const data: LatestSegmentsResponse = await res.json();
        if (disposed || data.activeRoomId !== resolveData?.activeRoomId || data.roomStatus !== "live") return;
        const rows = (data.segments || [])
          .map((row) => ({
            seq: Number(row.seq || 0),
            ko: String(row.koreanText || "").trim(),
            en: String(row.englishText || "").trim(),
          }))
          .filter((row) => row.seq > fallbackSeqRef.current && row.en);
        if (!rows.length) return;
        fallbackSeqRef.current = Math.max(fallbackSeqRef.current, ...rows.map((row) => row.seq));
        setFallbackEnLines((prev) => prev.concat(rows.map((row) => row.en)).slice(-4));
        setFallbackKrLines((prev) => prev.concat(rows.map((row) => row.ko).filter(Boolean)).slice(-4));
      } catch {
        // WebSocket remains primary; polling is a recovery path.
      }
    };
    void pollSegments();
    const timer = window.setInterval(pollSegments, SEGMENT_FALLBACK_POLL_MS);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, [resolveData?.activeRoomId, serviceKey, slug, socketEnabled]);

  useEffect(() => {
    const displayLines = fallbackEnLines.length ? fallbackEnLines : enLines;
    const lastLine = displayLines[displayLines.length - 1];
    if (!lastLine || lastLine === lastSpokenLineRef.current) return;
    lastSpokenLineRef.current = lastLine;
    if (Date.now() - lastNativeAudioAtRef.current < 2500) return;
    speak(lastLine);
  }, [enLines, fallbackEnLines, speak]);

  // iOS Safari: speechSynthesis stalls after ~30s. Resume it on a timer while audio is enabled.
  useEffect(() => {
    if (!ttsEnabled) return;
    if (typeof window === "undefined" || !("speechSynthesis" in window)) return;
    const id = window.setInterval(() => {
      try { window.speechSynthesis.resume(); } catch {}
    }, 10000);
    return () => clearInterval(id);
  }, [ttsEnabled]);

  const serviceTitle = resolveData?.service?.title || serviceKey || "Service";
  const displayKrLines = fallbackKrLines.length ? fallbackKrLines : krLines;
  const displayEnLines = fallbackEnLines.length ? fallbackEnLines : enLines;
  const lastKr = displayKrLines[displayKrLines.length - 1] || "";
  const lastEn = displayEnLines[displayEnLines.length - 1] || "";
  const waitingMessage = loading
    ? `Loading ${serviceTitle}…`
    : !resolveData
      ? `Waiting for ${serviceTitle} to begin…`
      : !resolveData.activeRoomId || resolveData.roomStatus !== "live"
        ? roomEndMessage(resolveData.lastEndReason) || `Waiting for ${serviceTitle} to begin…`
      : connected
        ? "Live — waiting for speech…"
        : "Connecting…";
  const currentEn = lastEn || waitingMessage;
  const recentEn = displayEnLines.slice(0, -1).slice(-2);

  const isLive = connected;
  const isConnecting = socketEnabled && !connected;
  const dotColor = isLive ? "#34d399" : isConnecting ? "#f59e0b" : "rgba(222,209,198,0.35)";
  const labelColor = isLive ? "rgba(52,211,153,0.9)" : isConnecting ? "rgba(245,158,11,0.85)" : "rgba(222,209,198,0.5)";
  const statusLabel = isLive ? "Live" : isConnecting ? "Connecting" : "Standby";

  return (
    <>
    <Head>
      <meta name="apple-mobile-web-app-capable" content="yes" />
      <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
    </Head>
    <main
      style={{
        minHeight: "100vh",
        background: "#0F2D4D",
        color: "#F2F3F4",
        fontFamily: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        position: "relative",
        overflow: "hidden",
      }}
    >
      <style>{`
        @keyframes drift-mauve {
          0%, 100% { transform: translate(0, 0) scale(1); }
          40%       { transform: translate(6%, -10%) scale(1.08); }
          75%       { transform: translate(-4%, 5%) scale(0.95); }
        }
        @keyframes drift-blue {
          0%, 100% { transform: translate(0, 0) scale(1); }
          35%       { transform: translate(-8%, 6%) scale(0.94); }
          70%       { transform: translate(5%, -4%) scale(1.06); }
        }
        @keyframes drift-warm {
          0%, 100% { transform: translate(0, 0); }
          50%       { transform: translate(3%, 8%); }
        }
        @keyframes pulse-live {
          0%   { box-shadow: 0 0 0 0 rgba(52,211,153,0.55); }
          70%  { box-shadow: 0 0 0 8px rgba(52,211,153,0); }
          100% { box-shadow: 0 0 0 0 rgba(52,211,153,0); }
        }
        @keyframes pulse-connecting {
          0%   { box-shadow: 0 0 0 0 rgba(245,158,11,0.55); }
          70%  { box-shadow: 0 0 0 8px rgba(245,158,11,0); }
          100% { box-shadow: 0 0 0 0 rgba(245,158,11,0); }
        }
        @keyframes text-in {
          from { opacity: 0; transform: translateY(6px); }
          to   { opacity: 1; transform: translateY(0); }
        }
        .orb-mauve     { animation: drift-mauve 24s ease-in-out infinite; }
        .orb-blue      { animation: drift-blue 30s ease-in-out infinite; }
        .orb-warm      { animation: drift-warm 36s ease-in-out infinite; }
        .dot-live      { animation: pulse-live 2s ease infinite; }
        .dot-connecting{ animation: pulse-connecting 1.2s ease infinite; }
        .ctrl-btn { transition: background 150ms ease, opacity 150ms ease, transform 100ms ease; }
        .ctrl-btn:hover  { background: rgba(167,118,147,0.2) !important; opacity: 1 !important; }
        .ctrl-btn:active { transform: scale(0.95); }
        @media (hover: none) { .kb-hint { display: none; } }
      `}</style>

      {/* Animated background orbs */}
      <div style={{ position: "fixed", inset: 0, zIndex: 0, pointerEvents: "none", overflow: "hidden" }}>
        <div
          className="orb-mauve"
          style={{
            position: "absolute",
            width: "80vmax",
            height: "80vmax",
            borderRadius: "50%",
            background: "radial-gradient(circle, rgba(167,118,147,0.52) 0%, transparent 68%)",
            bottom: "-28vmax",
            left: "-18vmax",
            filter: "blur(32px)",
          }}
        />
        <div
          className="orb-blue"
          style={{
            position: "absolute",
            width: "65vmax",
            height: "65vmax",
            borderRadius: "50%",
            background: "radial-gradient(circle, rgba(23,72,113,0.72) 0%, transparent 68%)",
            top: "-18vmax",
            right: "-10vmax",
            filter: "blur(28px)",
          }}
        />
        <div
          className="orb-warm"
          style={{
            position: "absolute",
            width: "35vmax",
            height: "35vmax",
            borderRadius: "50%",
            background: "radial-gradient(circle, rgba(222,209,198,0.14) 0%, transparent 70%)",
            top: "25vh",
            left: "35vw",
            filter: "blur(48px)",
          }}
        />
      </div>

      {/* Status indicator */}
      <div
        style={{
          position: "fixed",
          top: 20,
          left: 20,
          zIndex: 20,
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "6px 12px 6px 10px",
          borderRadius: 999,
          background: (isLive || isConnecting) ? "rgba(15,45,77,0.6)" : "transparent",
          border: (isLive || isConnecting) ? "1px solid rgba(255,255,255,0.08)" : "none",
          backdropFilter: (isLive || isConnecting) ? "blur(12px)" : "none",
          transition: "background 300ms ease",
        }}
      >
        <span
          className={isLive ? "dot-live" : isConnecting ? "dot-connecting" : ""}
          style={{
            width: 7,
            height: 7,
            borderRadius: "50%",
            display: "inline-block",
            background: dotColor,
            flexShrink: 0,
          }}
        />
        {(isLive || isConnecting) && (
          <span style={{
            fontSize: 11,
            fontWeight: 600,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            color: labelColor,
          }}>
            {statusLabel}
          </span>
        )}
      </div>

      {/* Top-right controls */}
      <div style={{ position: "fixed", top: 20, right: 20, zIndex: 20, display: "flex", gap: 6 }}>
        <button
          type="button"
          onClick={() => {
            const next = !ttsEnabled;
            setTtsEnabled(next);
            if (next) {
              void unlockPcmAudio();
            }
            if (next && typeof window !== "undefined" && "speechSynthesis" in window) {
              try {
                window.speechSynthesis.cancel();
                const unlock = new SpeechSynthesisUtterance("");
                window.speechSynthesis.speak(unlock);
              } catch {}
            }
          }}
          className="ctrl-btn"
          style={{
            background: ttsEnabled ? "rgba(52,211,153,0.15)" : "rgba(15,45,77,0.5)",
            color: ttsEnabled ? "rgba(52,211,153,0.95)" : "rgba(222,209,198,0.55)",
            border: ttsEnabled ? "1px solid rgba(52,211,153,0.4)" : "1px solid rgba(167,118,147,0.25)",
            padding: "7px 14px",
            borderRadius: 999,
            fontSize: 11,
            letterSpacing: "0.07em",
            textTransform: "uppercase",
            fontWeight: 600,
            cursor: "pointer",
            backdropFilter: "blur(12px)",
          }}
          aria-pressed={ttsEnabled}
        >
          {ttsEnabled ? "♪ Audio" : "Audio"}
        </button>

        <button
          type="button"
          onClick={toggleDisplayMode}
          className="ctrl-btn"
          style={{
            background: "rgba(15,45,77,0.5)",
            color: "rgba(222,209,198,0.55)",
            border: "1px solid rgba(167,118,147,0.25)",
            padding: "7px 14px",
            borderRadius: 999,
            fontSize: 11,
            letterSpacing: "0.07em",
            textTransform: "uppercase",
            fontWeight: 600,
            cursor: "pointer",
            backdropFilter: "blur(12px)",
          }}
          aria-pressed={displayMode === "fullScreen"}
        >
          {displayMode === "subtitle" ? "Expand" : "Subtitle"}
          <span className="kb-hint" style={{ opacity: 0.45, fontWeight: 400 }}> [F]</span>
        </button>
      </div>

      {/* Error banner */}
      {errorMsg && (
        <div
          style={{
            position: "fixed",
            top: 56,
            left: "50%",
            transform: "translateX(-50%)",
            zIndex: 20,
            color: "#fca5a5",
            fontSize: 12,
            background: "rgba(15,45,77,0.8)",
            border: "1px solid rgba(252,165,165,0.25)",
            borderRadius: 12,
            padding: "8px 16px",
            backdropFilter: "blur(12px)",
            maxWidth: "calc(100vw - 40px)",
            textAlign: "center",
            letterSpacing: "0.02em",
          }}
        >
          {friendlyError(errorMsg)}
        </div>
      )}

      {/* Subtitle mode */}
      {displayMode === "subtitle" ? (
        <div
          style={{
            position: "fixed",
            bottom: 0,
            left: 0,
            right: 0,
            zIndex: 10,
            pointerEvents: "none",
            padding: "8vh 5vw 5vh",
            display: "flex",
            justifyContent: "center",
          }}
        >
          <div
            style={{
              width: "min(1800px, 90vw)",
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: "0.5em",
              textAlign: "center",
            }}
          >
            {lastKr && (
              <div
                style={{
                  fontSize: "clamp(12px, 1.4vw, 20px)",
                  color: "rgba(167,118,147,0.75)",
                  letterSpacing: "0.12em",
                  lineHeight: 1.4,
                  fontWeight: 300,
                  fontStyle: "italic",
                }}
              >
                {lastKr}
              </div>
            )}

            {lastKr && enLines.length > 0 && (
              <div style={{
                width: 32,
                height: 1,
                background: "rgba(167,118,147,0.3)",
                borderRadius: 1,
                margin: "0.1em 0",
              }} />
            )}

            {enLines.length > 0 ? (
              enLines.map((line, i) => {
                const isCurrent = i === enLines.length - 1;
                return (
                  <div
                    key={`${i}-${line.slice(0, 12)}`}
                    style={{
                      fontSize: isCurrent ? "clamp(28px, 4.5vw, 68px)" : "clamp(16px, 2.6vw, 40px)",
                      fontWeight: isCurrent ? 700 : 300,
                      wordBreak: "break-word",
                      lineHeight: 1.2,
                      opacity: isCurrent ? 1 : 0.35,
                      color: isCurrent ? "#F2F3F4" : "#DED1C6",
                      letterSpacing: isCurrent ? "-0.01em" : "0",
                      textShadow: isCurrent ? "0 2px 24px rgba(15,45,77,0.8), 0 0 60px rgba(167,118,147,0.15)" : "none",
                      transition: "opacity 300ms ease",
                      animation: isCurrent ? "text-in 250ms ease forwards" : "none",
                    }}
                  >
                    {line}
                  </div>
                );
              })
            ) : (
              <div
                style={{
                  fontSize: "clamp(18px, 3vw, 44px)",
                  fontWeight: 300,
                  opacity: 0.3,
                  fontStyle: "italic",
                  color: "#DED1C6",
                  letterSpacing: "0.01em",
                }}
              >
                {waitingMessage}
              </div>
            )}
          </div>
        </div>
      ) : (
        /* Fullscreen mode */
        <div
          style={{
            position: "fixed",
            inset: 0,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            padding: "12vh 7vw 14vh",
            textAlign: "center",
            zIndex: 8,
          }}
        >
          {lastKr && (
            <div
              style={{
                fontSize: "clamp(15px, 2.2vw, 36px)",
                fontWeight: 300,
                fontStyle: "italic",
                letterSpacing: "0.1em",
                color: "rgba(167,118,147,0.7)",
                marginBottom: "1.2rem",
              }}
            >
              {lastKr}
            </div>
          )}

          <div
            style={{
              maxWidth: "min(1400px, 92vw)",
              fontSize: "clamp(52px, 11vw, 160px)",
              fontWeight: enLines.length > 0 ? 700 : 200,
              lineHeight: 1.05,
              letterSpacing: enLines.length > 0 ? "-0.02em" : "0.02em",
              color: enLines.length > 0 ? "#F2F3F4" : "rgba(222,209,198,0.2)",
              fontStyle: enLines.length > 0 ? "normal" : "italic",
              textShadow: enLines.length > 0 ? "0 0 80px rgba(167,118,147,0.2), 0 4px 32px rgba(15,45,77,0.6)" : "none",
              transition: "color 400ms ease, font-weight 400ms ease",
              animation: enLines.length > 0 ? "text-in 300ms ease forwards" : "none",
            }}
          >
            {currentEn}
          </div>

          {recentEn.length > 0 && (
            <div
              style={{
                marginTop: "2rem",
                display: "flex",
                flexDirection: "column",
                gap: "0.3em",
                opacity: 0.3,
                fontSize: "clamp(14px, 2.4vw, 36px)",
                fontWeight: 300,
                color: "#DED1C6",
                letterSpacing: "0.01em",
              }}
            >
              {recentEn.map((line, idx) => (
                <div key={`${idx}-${line.slice(0, 12)}`}>{line}</div>
              ))}
            </div>
          )}
        </div>
      )}
      {/* Plan SC: FR-07 — corner Help link, low opacity, does not occlude feed */}
      <Link
        href="/help"
        aria-label="Help"
        style={{
          position: "fixed",
          bottom: 12,
          right: 14,
          padding: "6px 12px",
          borderRadius: 999,
          fontSize: 12,
          fontWeight: 600,
          color: "rgba(242,243,244,0.55)",
          background: "rgba(15,45,77,0.45)",
          border: "1px solid rgba(242,243,244,0.18)",
          textDecoration: "none",
          letterSpacing: "0.04em",
          backdropFilter: "blur(6px)",
          WebkitBackdropFilter: "blur(6px)",
          zIndex: 50,
          transition: "color 200ms ease, background 200ms ease",
        }}
        onMouseEnter={(e) => { e.currentTarget.style.color = "#F2F3F4"; e.currentTarget.style.background = "rgba(15,45,77,0.85)"; }}
        onMouseLeave={(e) => { e.currentTarget.style.color = "rgba(242,243,244,0.55)"; e.currentTarget.style.background = "rgba(15,45,77,0.45)"; }}
      >
        Help
      </Link>
    </main>
    </>
  );
}
