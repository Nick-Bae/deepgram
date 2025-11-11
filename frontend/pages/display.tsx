// pages/display.tsx
"use client";
import { useEffect, useState, type CSSProperties } from "react";
import { useSubtitleSocket } from "../utils/useSubtitleSocket";

const OVERLAY_TOGGLE_KEY = "b";

export default function Display() {
  const [overlayMode, setOverlayMode] = useState(true);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() === OVERLAY_TOGGLE_KEY) {
        setOverlayMode((prev) => !prev);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  const {
    connected,
    // krInterim,    if you want to show a faint preview
    krLines,
    enLines,
  } = useSubtitleSocket(
    process.env.NEXT_PUBLIC_WS_URL
      ? `${process.env.NEXT_PUBLIC_WS_URL}?role=viewer`
      : undefined,
    { maxLines: 3, track: "en" } // "en" | "kr" | "both"
  );

  const lastKr = krLines[krLines.length - 1] || "";

  const containerStyle: CSSProperties = {
    display: "flex",
    flexDirection: "column",
    justifyContent: "flex-end",
    alignItems: "stretch",
    color: "#fff",
    padding: "2.6rem 5vw 2rem",
    boxSizing: "border-box",
    overflow: "hidden",
    fontFamily:
      "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif",
    transition: "background-color 180ms ease, padding 180ms ease",
  };

  if (overlayMode) {
    Object.assign(containerStyle, {
      position: "fixed",
      inset: 0,
      width: "100vw",
      minHeight: "100vh",
      backgroundColor: "transparent",
      pointerEvents: "none",
      zIndex: 2147483646,
      padding: "2.4rem 6vw 1.8rem",
    });
  } else {
    Object.assign(containerStyle, {
      position: "relative",
      width: "100%",
      minHeight: "100vh",
      backgroundColor: "#000",
      pointerEvents: "auto",
    });
  }

  const captionSurfaceStyle: CSSProperties = {
    maxWidth: "min(960px, 96vw)",
    width: "100%",
    margin: "0 auto",
    display: "flex",
    flexDirection: "column",
    justifyContent: "flex-end",
    gap: "1em",
    textAlign: "left",
    transition: "background-color 180ms ease, backdrop-filter 180ms ease, box-shadow 180ms ease",
  };

  if (overlayMode) {
    Object.assign(captionSurfaceStyle, {
      backgroundColor: "rgba(0, 0, 0, 0.65)",
      backdropFilter: "blur(8px)",
      borderRadius: "1.2rem",
      padding: "1.2rem 1.6rem",
      boxShadow: "0 18px 48px rgba(0, 0, 0, 0.4)",
    });
  }

  return (
    <div
      style={{
        height: "100vh",
        width: "100vw",
        display: "flex",
        flexDirection: "column",
        justifyContent: "flex-end",
        alignItems: "stretch",
        background: "#000",
        color: "#fff",
        padding: "2.6rem 5vw 2rem",
        boxSizing: "border-box",
        overflow: "hidden",
        fontFamily:
          "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif",
      }}
    >
      {/* status */}
      <div
        style={{
          position: "fixed",
          top: 12,
          right: 12,
          fontSize: 14,
          opacity: 0.85,
        }}
      >
        {connected ? "🟢 Connected" : "🔴 Disconnected"}
      </div>

      <div
        style={{
          maxWidth: "96vw",
          width: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "flex-end",
          gap: "1em",
          textAlign: "left",
        }}
      >
        {/* Optional: KR preview line (faint) */}
        {/* {krInterim && (
          <div style={{ opacity: 0.5, fontSize: "3vw", marginBottom: "0.25em", fontStyle: "italic" }}>
            {krInterim}
          </div>
        )} */}

        {/* Last KR final (smaller, above EN) */}
        {lastKr && (
          <div
            style={{
              opacity: 0.75,
              fontSize: "clamp(18px, 2.6vw, 44px)",
              letterSpacing: "0.01em",
              textTransform: "none",
              lineHeight: 1.2,
            }}
          >
            {lastKr}
          </div>
        )}

        {/* EN multi-line (show newest at the bottom) */}
        <div
          style={{
            lineHeight: 1.16,
            display: "flex",
            flexDirection: "column",
            gap: "0.35em",
          }}
        >
              {enLines.length > 0 ? (
                enLines.map((line, i) => (
              <div
                key={`${i}-${line.slice(0, 12)}`}
                style={{
                  fontSize:
                    i === enLines.length - 1
                      ? "clamp(26px, 6.6vw, 84px)"
                      : "clamp(18px, 4.4vw, 52px)",
                  fontWeight: i === enLines.length - 1 ? 700 : 400,
                  wordBreak: "break-word",
                  opacity: i === enLines.length - 1 ? 1 : 0.55,
                  background:
                    i === enLines.length - 1 ? "rgba(255, 255, 255, 0.08)" : "transparent",
                  padding: i === enLines.length - 1 ? "0.2em 0.4em" : "0.05em 0",
                  borderRadius: "0.4em",
                  boxShadow:
                    i === enLines.length - 1 ? "0 12px 32px rgba(0,0,0,0.35)" : "none",
                  transition: "all 160ms ease",
                }}
              >
                {line}
              </div>
            ))
          ) : (
            <div style={{ fontSize: "clamp(22px, 5.5vw, 68px)", opacity: 0.6 }}>— waiting —</div>
          )}
        </div>
      </div>
    </div>
  );
}
