import Head from "next/head";
import { useRouter } from "next/router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { API_URL, WS_URL } from "../../../../utils/urls";
import { useSubtitleSocket } from "../../../../utils/useSubtitleSocket";
import { appendStreamContextToUrl, clearRoomInSession, persistStreamContext } from "../../../../utils/streamContext";
import { useTTS } from "../../../../utils/useTTS";

type ResolveResponse = {
  orgId: string;
  slug: string;
  serviceKey: string;
  activeRoomId: string | null;
  roomStatus: string;
  languagePair?: { source?: string; target?: string };
  service?: { title?: string; timezone?: string };
};

const RESOLVE_POLL_MS = 8000;
const DISPLAY_TOGGLE_KEY = "f";
type DisplayMode = "subtitle" | "fullScreen";

function friendlyError(msg: string): string {
  if (msg.includes("404")) return "Service not found. Check your link and try again.";
  if (msg.includes("403")) return "Access denied.";
  if (msg.includes("500") || msg.includes("502") || msg.includes("503")) return "Server error. Will retry automatically.";
  if (msg.includes("Failed to fetch") || msg.includes("NetworkError")) return "Network error. Check your connection.";
  return "Could not connect. Will retry automatically.";
}

export default function ChurchServiceListenerPage() {
  const router = useRouter();
  const slug = typeof router.query.churchSlug === "string" ? router.query.churchSlug : "";
  const serviceKey = typeof router.query.serviceKey === "string" ? router.query.serviceKey : "";

  const [loading, setLoading] = useState(true);
  const [resolveData, setResolveData] = useState<ResolveResponse | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [displayMode, setDisplayMode] = useState<DisplayMode>("fullScreen");

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

  const { connected, krLines, enLines } = useSubtitleSocket(scopedWsUrl, {
    maxLines: 4,
    track: "both",
    enabled: socketEnabled,
  });

  const targetLang = resolveData?.languagePair?.target || "en";
  const { enabled: ttsEnabled, setEnabled: setTtsEnabled, speak } = useTTS(targetLang);
  const lastSpokenLineRef = useRef("");

  useEffect(() => {
    const lastLine = enLines[enLines.length - 1];
    if (!lastLine || lastLine === lastSpokenLineRef.current) return;
    lastSpokenLineRef.current = lastLine;
    speak(lastLine);
  }, [enLines, speak]);

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
  const lastKr = krLines[krLines.length - 1] || "";
  const lastEn = enLines[enLines.length - 1] || "";
  const waitingMessage = loading
    ? `Loading ${serviceTitle}…`
    : !resolveData || !resolveData.activeRoomId || resolveData.roomStatus !== "live"
      ? `Waiting for ${serviceTitle} to begin…`
      : connected
        ? "Live — waiting for speech…"
        : "Connecting…";
  const currentEn = lastEn || waitingMessage;
  const recentEn = enLines.slice(0, -1).slice(-2);

  const isLive = connected;
  const isConnecting = socketEnabled && !connected;
  const dotColor = isLive ? "#34d399" : isConnecting ? "#f59e0b" : "rgba(255,255,255,0.25)";
  const labelColor = isLive ? "rgba(52,211,153,0.9)" : isConnecting ? "rgba(245,158,11,0.85)" : "rgba(255,255,255,0.38)";
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
        background: "radial-gradient(ellipse at 50% -10%, rgba(30,50,100,0.55) 0%, transparent 65%), #060b18",
        color: "#fff",
        fontFamily: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        position: "relative",
        overflow: "hidden",
      }}
    >
      <style>{`
        @keyframes pulse-live {
          0%   { box-shadow: 0 0 0 0 rgba(52,211,153,0.5); }
          70%  { box-shadow: 0 0 0 7px rgba(52,211,153,0); }
          100% { box-shadow: 0 0 0 0 rgba(52,211,153,0); }
        }
        @keyframes pulse-connecting {
          0%   { box-shadow: 0 0 0 0 rgba(245,158,11,0.5); }
          70%  { box-shadow: 0 0 0 7px rgba(245,158,11,0); }
          100% { box-shadow: 0 0 0 0 rgba(245,158,11,0); }
        }
        .dot-live        { animation: pulse-live 2s ease infinite; }
        .dot-connecting  { animation: pulse-connecting 1.2s ease infinite; }
        .toggle-btn:hover  { background: rgba(255,255,255,0.18) !important; }
        .toggle-btn:active { transform: scale(0.96); }
        @media (hover: none) { .kb-hint { display: none; } }
      `}</style>

      {/* Connection indicator */}
      <div
        style={{
          position: "fixed",
          top: 14,
          left: 14,
          zIndex: 20,
          display: "flex",
          alignItems: "center",
          gap: 7,
          fontSize: 12,
          fontWeight: 700,
          color: labelColor,
          letterSpacing: "0.05em",
          textTransform: "uppercase",
          fontFamily: "Inter, system-ui, sans-serif",
        }}
      >
        <span
          className={isLive ? "dot-live" : isConnecting ? "dot-connecting" : ""}
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            display: "inline-block",
            background: dotColor,
            flexShrink: 0,
          }}
        />
        {statusLabel}
      </div>

      {/* Top-right controls */}
      <div style={{ position: "fixed", top: 14, right: 14, zIndex: 20, display: "flex", gap: 8 }}>
        {/* Audio toggle */}
        <button
          type="button"
          onClick={() => {
            const next = !ttsEnabled;
            setTtsEnabled(next);
            // Must call speechSynthesis inside the user gesture to unlock it on iOS/Android.
            if (next && typeof window !== "undefined" && "speechSynthesis" in window) {
              try {
                window.speechSynthesis.cancel();
                const unlock = new SpeechSynthesisUtterance("");
                window.speechSynthesis.speak(unlock);
              } catch {}
            }
          }}
          className="toggle-btn"
          style={{
            background: ttsEnabled ? "rgba(52,211,153,0.18)" : "rgba(0,0,0,0.55)",
            color: ttsEnabled ? "rgba(52,211,153,1)" : "rgba(255,255,255,0.6)",
            border: ttsEnabled ? "1px solid rgba(52,211,153,0.5)" : "1px solid rgba(255,255,255,0.22)",
            padding: "6px 14px",
            borderRadius: 999,
            fontSize: 12,
            letterSpacing: "0.06em",
            textTransform: "uppercase",
            fontWeight: 700,
            cursor: "pointer",
            backdropFilter: "blur(8px)",
            transition: "all 150ms ease",
          }}
          aria-pressed={ttsEnabled}
          title={ttsEnabled ? "Audio on — tap to mute" : "Tap to hear translation"}
        >
          {ttsEnabled ? "🔊 Audio" : "🔇 Audio"}
        </button>

        {/* Display mode toggle */}
        <button
          type="button"
          onClick={toggleDisplayMode}
          className="toggle-btn"
          style={{
            position: "relative",
            background: "rgba(0,0,0,0.55)",
            color: "rgba(255,255,255,0.9)",
            border: "1px solid rgba(255,255,255,0.22)",
            padding: "6px 14px",
            borderRadius: 999,
            fontSize: 12,
            letterSpacing: "0.06em",
            textTransform: "uppercase",
            fontWeight: 700,
            cursor: "pointer",
            backdropFilter: "blur(8px)",
            transition: "background 150ms ease, transform 100ms ease",
          }}
          aria-pressed={displayMode === "fullScreen"}
        >
          {displayMode === "subtitle" ? "Full Screen" : "Subtitle"}{" "}
          <span className="kb-hint" style={{ opacity: 0.55, fontWeight: 400 }}>[F]</span>
        </button>
      </div>

      {/* Error banner */}
      {errorMsg && (
        <div
          style={{
            position: "fixed",
            top: 48,
            left: "50%",
            transform: "translateX(-50%)",
            zIndex: 20,
            color: "#fca5a5",
            fontSize: 13,
            background: "rgba(127,29,29,0.45)",
            border: "1px solid rgba(252,165,165,0.35)",
            borderRadius: 10,
            padding: "7px 14px",
            backdropFilter: "blur(8px)",
            whiteSpace: "nowrap",
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
            inset: 0,
            display: "flex",
            flexDirection: "column",
            justifyContent: "flex-end",
            padding: "2.4rem 6vw 1.8rem",
            boxSizing: "border-box",
            pointerEvents: "none",
            zIndex: 10,
          }}
        >
          <div
            style={{
              width: "100%",
              display: "flex",
              flexDirection: "column",
              gap: "0.75em",
              textAlign: "left",
              background: "linear-gradient(to right, rgba(0,0,0,0.78), rgba(0,0,0,0.62))",
              backdropFilter: "blur(10px)",
              borderLeft: connected ? "3px solid rgba(52,211,153,0.6)" : "3px solid rgba(255,255,255,0.1)",
              padding: "1.1rem 1.6rem 1.1rem 1.4rem",
              boxShadow: "0 -4px 40px rgba(0,0,0,0.5)",
              transition: "border-color 400ms ease",
            }}
          >
            {lastKr && (
              <div
                style={{
                  opacity: 0.68,
                  fontSize: "clamp(17px, 2.4vw, 42px)",
                  letterSpacing: "0.01em",
                  lineHeight: 1.25,
                  color: "rgba(255,255,255,0.85)",
                }}
              >
                {lastKr}
              </div>
            )}
            <div style={{ display: "flex", flexDirection: "column", gap: "0.28em", lineHeight: 1.15 }}>
              {enLines.length > 0 ? (
                enLines.map((line, i) => {
                  const isCurrent = i === enLines.length - 1;
                  return (
                    <div
                      key={`${i}-${line.slice(0, 12)}`}
                      style={{
                        fontSize: isCurrent ? "clamp(28px, 6.5vw, 88px)" : "clamp(24px, 5.8vw, 78px)",
                        fontWeight: isCurrent ? 800 : 500,
                        wordBreak: "break-word",
                        opacity: isCurrent ? 1 : 0.55,
                        color: isCurrent ? "#fff" : "rgba(255,255,255,0.7)",
                        textShadow: isCurrent ? "0 2px 24px rgba(0,0,0,0.8)" : "none",
                        transition: "opacity 200ms ease, font-size 200ms ease",
                      }}
                    >
                      {line}
                    </div>
                  );
                })
              ) : (
                <div
                  style={{
                    fontSize: "clamp(20px, 4.4vw, 56px)",
                    opacity: 0.42,
                    fontStyle: "italic",
                    fontWeight: 400,
                  }}
                >
                  {waitingMessage}
                </div>
              )}
            </div>
          </div>
        </div>
      ) : (
        /* Fullscreen mode */
        <div
          style={{
            position: "fixed",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: "10vh 6vw 12vh",
            textAlign: "center",
            zIndex: 8,
          }}
        >
          <div
            style={{
              maxWidth: "min(1320px, 94vw)",
              width: "100%",
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: "1.6rem",
            }}
          >
            {lastKr && (
              <div
                style={{
                  opacity: 0.72,
                  fontSize: "clamp(18px, 2.8vw, 52px)",
                  letterSpacing: "0.04em",
                  lineHeight: 1.3,
                  color: "rgba(255,255,255,0.8)",
                }}
              >
                {lastKr}
              </div>
            )}

            <div
              style={{
                fontSize: "clamp(52px, 12vw, 164px)",
                fontWeight: enLines.length > 0 ? 800 : 300,
                lineHeight: 1.04,
                letterSpacing: "-0.01em",
                textShadow: enLines.length > 0 ? "0 4px 48px rgba(0,0,0,0.9), 0 1px 6px rgba(0,0,0,0.6)" : "none",
                color: enLines.length > 0 ? "#fff" : "rgba(255,255,255,0.28)",
                fontStyle: enLines.length > 0 ? "normal" : "italic",
                transition: "color 300ms ease",
              }}
            >
              {currentEn}
            </div>

            {recentEn.length > 0 && (
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: "0.3em",
                  opacity: 0.45,
                  fontSize: "clamp(18px, 3.2vw, 46px)",
                  fontWeight: 400,
                }}
              >
                {recentEn.map((line, idx) => (
                  <div key={`${idx}-${line.slice(0, 12)}`}>{line}</div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </main>
    </>
  );
}
