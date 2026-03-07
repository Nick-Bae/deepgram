import { useRouter } from "next/router";
import { useCallback, useEffect, useMemo, useState } from "react";

import { API_URL, WS_URL } from "../../../../utils/urls";
import { useSubtitleSocket } from "../../../../utils/useSubtitleSocket";
import { appendStreamContextToUrl, clearRoomInSession, persistStreamContext } from "../../../../utils/streamContext";

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

  const serviceTitle = resolveData?.service?.title || serviceKey || "Service";
  const lastKr = krLines[krLines.length - 1] || "";
  const lastEn = enLines[enLines.length - 1] || "";
  const waitingMessage = loading
    ? "Resolving service..."
    : !resolveData || !resolveData.activeRoomId || resolveData.roomStatus !== "live"
      ? "Waiting for host to start translation."
      : connected
        ? "Live translation is connected. Waiting for the first line..."
        : "Host started translation. Connecting now...";
  const currentEn = lastEn || waitingMessage;
  const recentEn = enLines.slice(0, -1).slice(-2);
  const connectionLabel = connected ? "Connection: Connected" : socketEnabled ? "Connection: Connecting..." : "Connection: Standby";
  const connectionColor = connected ? "#34d399" : socketEnabled ? "#f59e0b" : "#6b7280";
  const stageWidth = "min(92vw, calc(92vh * 16 / 9))";

  return (
    <main
      style={{
        minHeight: "100vh",
        background: "#000",
        color: "#fff",
        fontFamily: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        position: "relative",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          position: "fixed",
          top: 14,
          left: 14,
          zIndex: 20,
          display: "flex",
          alignItems: "center",
          gap: 8,
          fontSize: 12,
          fontWeight: 600,
          color: "rgba(255,255,255,0.85)",
        }}
      >
        <span
          style={{
            width: 10,
            height: 10,
            borderRadius: 999,
            display: "inline-block",
            background: connectionColor,
            boxShadow: `0 0 0 3px ${connected ? "rgba(16,185,129,0.16)" : socketEnabled ? "rgba(245,158,11,0.12)" : "rgba(107,114,128,0.12)"}`,
          }}
        />
        {connectionLabel}
      </div>

      <button
        type="button"
        onClick={toggleDisplayMode}
        style={{
          position: "fixed",
          top: 12,
          right: 12,
          zIndex: 20,
          borderRadius: 999,
          border: "1px solid rgba(255,255,255,0.28)",
          background: "rgba(0,0,0,0.52)",
          color: "#fff",
          padding: "7px 14px",
          fontSize: 12,
          fontWeight: 700,
          cursor: "pointer",
          letterSpacing: "0.03em",
          textTransform: "uppercase",
        }}
      >
        {displayMode === "subtitle" ? "Full Screen (F)" : "Subtitle (F)"}
      </button>

      {errorMsg ? (
        <div
          style={{
            position: "fixed",
            top: 48,
            left: "50%",
            transform: "translateX(-50%)",
            zIndex: 20,
            color: "#fca5a5",
            fontSize: 13,
            background: "rgba(127,29,29,0.35)",
            border: "1px solid rgba(252,165,165,0.4)",
            borderRadius: 10,
            padding: "6px 10px",
          }}
        >
          Failed to resolve service: {errorMsg}
        </div>
      ) : null}

      {loading ? (
        <div
          style={{
            position: "fixed",
            top: 48,
            left: "50%",
            transform: "translateX(-50%)",
            zIndex: 19,
            fontSize: 12,
            opacity: 0.75,
          }}
        >
          Resolving {serviceTitle}...
        </div>
      ) : null}

      {displayMode === "subtitle" ? (
        <div
          style={{
            position: "fixed",
            left: 0,
            right: 0,
            bottom: 42,
            padding: "0 5vw",
            display: "flex",
            justifyContent: "center",
            zIndex: 10,
          }}
        >
          <section
            style={{
              width: `min(${stageWidth}, 100%)`,
              border: "1px solid rgba(255,255,255,0.12)",
              background: "rgba(0,0,0,0.56)",
              boxShadow: "0 20px 45px rgba(0,0,0,0.45)",
              padding: "18px 22px",
              display: "flex",
              flexDirection: "column",
              gap: "0.7em",
              textAlign: "left",
            }}
          >
            {lastKr ? (
              <div
                style={{
                  opacity: 0.72,
                  fontSize: "clamp(16px, 2.2vw, 38px)",
                  letterSpacing: "0.01em",
                  lineHeight: 1.22,
                }}
              >
                {lastKr}
              </div>
            ) : null}
            <div style={{ lineHeight: 1.14, display: "flex", flexDirection: "column", gap: "0.28em" }}>
              {enLines.length > 0 ? (
                enLines.map((line, i) => {
                  const isCurrent = i === enLines.length - 1;
                  return (
                    <div
                      key={`${i}-${line.slice(0, 12)}`}
                      style={{
                        fontSize: isCurrent ? "clamp(26px, 4.7vw, 78px)" : "clamp(22px, 4.1vw, 70px)",
                        fontWeight: isCurrent ? 700 : 500,
                        wordBreak: "break-word",
                        opacity: isCurrent ? 1 : 0.6,
                        color: isCurrent ? "#fff" : "rgba(255,255,255,0.76)",
                        background: isCurrent ? "rgba(255,255,255,0.1)" : "transparent",
                        borderRadius: isCurrent ? 12 : 0,
                        padding: isCurrent ? "0.12em 0.28em" : "0",
                        boxShadow: isCurrent ? "0 10px 26px rgba(0,0,0,0.35)" : "none",
                      }}
                    >
                      {line}
                    </div>
                  );
                })
              ) : (
                <div style={{ fontSize: "clamp(22px, 4.1vw, 58px)", opacity: 0.68 }}>{waitingMessage}</div>
              )}
            </div>
          </section>
        </div>
      ) : (
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
          <section
            style={{
              width: stageWidth,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: "1.2rem",
            }}
          >
            {lastKr ? (
              <div
                style={{
                  opacity: 0.76,
                  fontSize: "clamp(20px, 3vw, 56px)",
                  letterSpacing: "0.02em",
                  lineHeight: 1.2,
                }}
              >
                {lastKr}
              </div>
            ) : null}

            <div style={{ fontSize: "clamp(72px, 11vw, 180px)", fontWeight: 700, lineHeight: 1.04 }}>
              {currentEn}
            </div>

            {recentEn.length > 0 ? (
              <div style={{ display: "flex", flexDirection: "column", gap: "0.22em", opacity: 0.52, fontSize: "clamp(22px, 3.3vw, 50px)" }}>
                {recentEn.map((line, idx) => (
                  <div key={`${idx}-${line.slice(0, 12)}`}>{line}</div>
                ))}
              </div>
            ) : null}
          </section>
        </div>
      )}
    </main>
  );
}
