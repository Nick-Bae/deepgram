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
    krLines,
    enLines,
  } = useSubtitleSocket(
    process.env.NEXT_PUBLIC_WS_URL
      ? `${process.env.NEXT_PUBLIC_WS_URL}?role=viewer`
      : undefined,
    { maxLines: 4, track: "en" }
  );

  const lastKr = krLines[krLines.length - 1] || "";
  const waitingMessage = connected
    ? "Live translation connected — waiting for speech…"
    : "Waiting for translation stream…";

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
    fontFamily: "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
    transition: "padding 200ms ease",
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
      background: "radial-gradient(ellipse at 50% -10%, rgba(30,50,100,0.55) 0%, transparent 65%), #060b18",
      pointerEvents: "auto",
    });
  }

  return (
    <>
      <style>{`
        @keyframes pulse-live {
          0%   { box-shadow: 0 0 0 0 rgba(52,211,153,0.5); }
          70%  { box-shadow: 0 0 0 7px rgba(52,211,153,0); }
          100% { box-shadow: 0 0 0 0 rgba(52,211,153,0); }
        }
        .dot-live   { animation: pulse-live 2s ease infinite; }
        .toggle-btn:hover { background: rgba(255,255,255,0.18) !important; }
        .toggle-btn:active { transform: scale(0.96); }
      `}</style>

      {/* Toggle button */}
      <button
        type="button"
        onClick={toggleDisplayMode}
        className="toggle-btn"
        style={{
          position: "fixed",
          top: 14,
          right: 14,
          zIndex: 2147483647,
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
        {displayMode === "subtitle" ? "Full Screen" : "Subtitle"} <span style={{ opacity: 0.55, fontWeight: 400 }}>[F]</span>
      </button>

      {/* Connection indicator */}
      <div
        style={{
          position: "fixed",
          top: 14,
          left: 14,
          zIndex: 2147483646,
          display: "flex",
          alignItems: "center",
          gap: 7,
          fontSize: 12,
          fontWeight: 700,
          color: connected ? "rgba(52,211,153,0.9)" : "rgba(255,255,255,0.38)",
          letterSpacing: "0.05em",
          textTransform: "uppercase",
          fontFamily: "Inter, system-ui, sans-serif",
        }}
      >
        <span
          className={connected ? "dot-live" : ""}
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            display: "inline-block",
            background: connected ? "#34d399" : "rgba(255,255,255,0.25)",
            flexShrink: 0,
          }}
        />
        {connected ? "Live" : "Standby"}
      </div>

      <div style={containerStyle}>
        {isSubtitleMode ? (
          <div
            style={{
              width: "100%",
              maxWidth: "100%",
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
        ) : (
          <div
            style={{
              maxWidth: "min(1320px, 94vw)",
              width: "100%",
              margin: "0 auto",
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: "1.6rem",
              textAlign: "center",
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
                fontWeight: 800,
                lineHeight: 1.04,
                letterSpacing: "-0.01em",
                textShadow: "0 4px 48px rgba(0,0,0,0.9), 0 1px 6px rgba(0,0,0,0.6)",
                color: enLines.length > 0 ? "#fff" : "rgba(255,255,255,0.28)",
                fontStyle: enLines.length > 0 ? "normal" : "italic",
                fontWeight: enLines.length > 0 ? 800 : 300,
                transition: "color 300ms ease",
              }}
            >
              {enLines.length > 0 ? enLines[enLines.length - 1] : waitingMessage}
            </div>

            {enLines.length > 1 && (
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
                {enLines.slice(-3, -1).map((line, idx) => (
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
