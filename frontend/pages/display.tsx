// pages/display.tsx
"use client";
import { useCallback, useEffect, useState, type CSSProperties } from "react";
import { useSubtitleSocket } from "../utils/useSubtitleSocket";

const DISPLAY_TOGGLE_KEY = "f";
type DisplayMode = "subtitle" | "fullScreen";

export default function Display() {
  const [displayMode, setDisplayMode] = useState<DisplayMode>("subtitle");

  const toggleDisplayMode = useCallback(() => {
    setDisplayMode((prev) => (prev === "subtitle" ? "fullScreen" : "subtitle"));
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() === DISPLAY_TOGGLE_KEY) {
        event.preventDefault();
        toggleDisplayMode();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [toggleDisplayMode]);

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

  const isSubtitleMode = displayMode === "subtitle";

  const containerStyle: CSSProperties = {
    display: "flex",
    flexDirection: "column",
    justifyContent: isSubtitleMode ? "flex-end" : "center",
    alignItems: isSubtitleMode ? "stretch" : "center",
    color: "#fff",
    padding: isSubtitleMode ? "2.4rem 6vw 1.8rem" : "5vh 6vw",
    boxSizing: "border-box",
    overflow: "hidden",
    fontFamily:
      "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif",
    transition: "background-color 180ms ease, padding 180ms ease",
  };

  if (isSubtitleMode) {
    Object.assign(containerStyle, {
      position: "fixed",
      inset: 0,
      width: "100vw",
      minHeight: "100vh",
      backgroundColor: "transparent",
      pointerEvents: "none",
      zIndex: 2147483646,
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

  const subtitleSurfaceStyle: CSSProperties = {
    width: "100%",
    maxWidth: "100%",
    margin: 0,
    display: "flex",
    flexDirection: "column",
    justifyContent: "flex-end",
    gap: "1em",
    textAlign: "left",
    transition: "background-color 180ms ease, backdrop-filter 180ms ease, box-shadow 180ms ease",
    backgroundColor: "rgba(0, 0, 0, 0.65)",
    backdropFilter: "blur(8px)",
    borderRadius: 0,
    padding: "1.2rem 1.6rem",
    boxShadow: "0 18px 48px rgba(0, 0, 0, 0.4)",
  };

  const fullScreenSurfaceStyle: CSSProperties = {
    maxWidth: "min(1280px, 94vw)",
    width: "100%",
    margin: "0 auto",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    gap: "1.8rem",
    textAlign: "center",
  };

  return (
    <>
      <button
        type="button"
        onClick={toggleDisplayMode}
        style={{
          position: "fixed",
          top: 12,
          right: 12,
          zIndex: 2147483647,
          background: "rgba(0,0,0,0.65)",
          color: "#fff",
          border: "1px solid rgba(255,255,255,0.35)",
          padding: "0.45rem 1.2rem",
          borderRadius: "999px",
          fontSize: 14,
          letterSpacing: "0.03em",
          textTransform: "uppercase",
          fontWeight: 600,
          cursor: "pointer",
          backdropFilter: "blur(6px)",
          transition: "background 160ms ease, color 160ms ease",
        }}
        aria-pressed={displayMode === "fullScreen"}
      >
        {displayMode === "subtitle" ? "Full Screen (F)" : "Subtitle (F)"}
      </button>

      <div
        style={{
          position: "fixed",
          top: 12,
          left: 12,
          fontSize: 14,
          opacity: 0.85,
          color: "#fff",
          zIndex: 2147483646,
        }}
      >
        {connected ? "🟢 Connected" : "🔴 Disconnected"}
      </div>

      <div style={containerStyle}>
        {isSubtitleMode ? (
          <div style={subtitleSurfaceStyle}>
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

            <div
              style={{
                lineHeight: 1.16,
                display: "flex",
                flexDirection: "column",
                gap: "0.35em",
              }}
            >
              {enLines.length > 0 ? (
                enLines.map((line, i) => {
                  const isCurrent = i === enLines.length - 1;
                  return (
                    <div
                      key={`${i}-${line.slice(0, 12)}`}
                      style={{
                        fontSize: "clamp(26px, 6.6vw, 84px)",
                        fontWeight: isCurrent ? 700 : 500,
                        wordBreak: "break-word",
                        opacity: isCurrent ? 1 : 0.72,
                        background: isCurrent ? "rgba(255, 255, 255, 0.08)" : "transparent",
                        padding: "0.15em 0.35em",
                        borderRadius: "0.45em",
                        boxShadow: isCurrent ? "0 12px 32px rgba(0,0,0,0.35)" : "none",
                        transition: "all 160ms ease",
                        filter: isCurrent ? "none" : "blur(0px)",
                        color: isCurrent ? "#fff" : "rgba(255,255,255,0.85)",
                      }}
                    >
                      {line}
                    </div>
                  );
                })
              ) : (
                <div style={{ fontSize: "clamp(22px, 5.5vw, 68px)", opacity: 0.6 }}>— waiting —</div>
              )}
            </div>
          </div>
        ) : (
          <div style={fullScreenSurfaceStyle}>
            {lastKr && (
              <div
                style={{
                  opacity: 0.8,
                  fontSize: "clamp(20px, 3vw, 56px)",
                  letterSpacing: "0.02em",
                  textTransform: "none",
                  lineHeight: 1.2,
                }}
              >
                {lastKr}
              </div>
            )}

            <div
              style={{
                fontSize: "clamp(48px, 12vw, 160px)",
                fontWeight: 700,
                lineHeight: 1.05,
                padding: "0.25em 0",
              }}
            >
              {enLines.length > 0 ? enLines[enLines.length - 1] : "— waiting —"}
            </div>

            {enLines.length > 1 && (
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: "0.35em",
                  opacity: 0.6,
                  fontSize: "clamp(20px, 3.5vw, 48px)",
                }}
              >
                {enLines
                  .slice(-3, -1)
                  .map((line, idx) => (
                    <div key={`${idx}-${line.slice(0, 12)}`}>{line}</div>
                  ))}
              </div>
            )}
          </div>
        )}
      </div>
    </>
  );
}
