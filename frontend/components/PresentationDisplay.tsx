// components/PresentationDisplay.tsx
// Design Ref: §5.1, §5.5 — Split-layout display: slide image (top ~70vh) +
// live translation subtitle (bottom ~30vh). object-fit: contain preserves the
// slide's aspect ratio (letterboxing acceptable, never squashed).
"use client";
import { useEffect, useRef, useState, type CSSProperties } from "react";
import type { Slide } from "../utils/useSlideSync";

type Props = {
  slide: Slide | null;
  lastKr?: string;
  enLines: string[];
  connected: boolean;
  waitingMessage: string;
};

const SLIDE_AREA_VH = 70;
const SUBTITLE_AREA_VH = 30;
const SLIDE_FADE_MS = 150;

export default function PresentationDisplay({
  slide,
  lastKr,
  enLines,
  connected,
  waitingMessage,
}: Props) {
  // Cross-fade between slides without a layout shift.
  const [renderedSlide, setRenderedSlide] = useState<Slide | null>(slide);
  const [imgError, setImgError] = useState(false);
  const fadeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (slide?.slideId === renderedSlide?.slideId) return;
    setImgError(false);
    if (fadeTimerRef.current) clearTimeout(fadeTimerRef.current);
    // Brief opacity dip before swap (handled by the img inline style).
    fadeTimerRef.current = setTimeout(() => {
      setRenderedSlide(slide);
    }, SLIDE_FADE_MS);
    return () => {
      if (fadeTimerRef.current) clearTimeout(fadeTimerRef.current);
    };
  }, [slide, renderedSlide]);

  const slideAreaStyle: CSSProperties = {
    flex: `0 0 ${SLIDE_AREA_VH}vh`,
    height: `${SLIDE_AREA_VH}vh`,
    width: "100%",
    background: "#000",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    overflow: "hidden",
    position: "relative",
  };

  const slideImgStyle: CSSProperties = {
    maxWidth: "100%",
    maxHeight: "100%",
    width: "auto",
    height: "auto",
    objectFit: "contain",
    transition: `opacity ${SLIDE_FADE_MS}ms ease`,
    opacity: slide?.slideId === renderedSlide?.slideId ? 1 : 0.5,
  };

  const subtitleAreaStyle: CSSProperties = {
    flex: `0 0 ${SUBTITLE_AREA_VH}vh`,
    height: `${SUBTITLE_AREA_VH}vh`,
    width: "100%",
    background:
      "linear-gradient(135deg, rgba(59,54,95,0.96) 0%, rgba(15,58,96,0.94) 100%)",
    color: "#fff",
    display: "flex",
    flexDirection: "column",
    justifyContent: "center",
    padding: "1.4rem clamp(1.8rem, 4vw, 4rem)",
    boxSizing: "border-box",
    borderTop: connected ? "3px solid rgba(52,211,153,0.6)" : "3px solid rgba(255,255,255,0.1)",
    boxShadow: "0 -6px 28px rgba(0,0,0,0.45)",
    transition: "border-color 400ms ease",
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        width: "100vw",
        height: "100vh",
        display: "flex",
        flexDirection: "column",
        backgroundColor: "#000",
        zIndex: 2147483645,
        fontFamily: "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
      }}
    >
      {/* Slide area (top) */}
      <div style={slideAreaStyle}>
        {renderedSlide && !imgError ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            key={renderedSlide.slideId}
            src={renderedSlide.url}
            alt={`Slide ${renderedSlide.order + 1}`}
            style={slideImgStyle}
            onError={() => setImgError(true)}
            draggable={false}
          />
        ) : (
          <SlidePlaceholder
            label={
              renderedSlide
                ? `Slide ${renderedSlide.order + 1} — image unavailable`
                : "No slide selected"
            }
          />
        )}
      </div>

      {/* Subtitle area (bottom) */}
      <div style={subtitleAreaStyle}>
        {lastKr && (
          <div
            style={{
              opacity: 0.7,
              fontSize: "clamp(15px, 1.8vw, 32px)",
              letterSpacing: "0.01em",
              lineHeight: 1.25,
              color: "rgba(255,255,255,0.85)",
              marginBottom: "0.4em",
            }}
          >
            {lastKr}
          </div>
        )}
        {enLines.length > 0 ? (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: "0.18em",
              lineHeight: 1.15,
              maxHeight: "100%",
              overflow: "hidden",
            }}
          >
            {enLines.slice(-2).map((line, i, arr) => {
              const isCurrent = i === arr.length - 1;
              return (
                <div
                  key={`${i}-${line.slice(0, 12)}`}
                  style={{
                    fontSize: isCurrent
                      ? "clamp(28px, 4.4vw, 72px)"
                      : "clamp(20px, 3.2vw, 52px)",
                    fontWeight: isCurrent ? 800 : 500,
                    opacity: isCurrent ? 1 : 0.55,
                    color: isCurrent ? "#fff" : "rgba(255,255,255,0.7)",
                    textShadow: isCurrent ? "0 2px 18px rgba(0,0,0,0.55)" : "none",
                    wordBreak: "break-word",
                    transition: "opacity 200ms ease, font-size 200ms ease",
                  }}
                >
                  {line}
                </div>
              );
            })}
          </div>
        ) : (
          <div
            style={{
              fontSize: "clamp(18px, 3vw, 44px)",
              opacity: 0.5,
              fontStyle: "italic",
              fontWeight: 400,
            }}
          >
            {waitingMessage}
          </div>
        )}
      </div>
    </div>
  );
}

function SlidePlaceholder({ label }: { label: string }) {
  return (
    <div
      style={{
        color: "rgba(255,255,255,0.3)",
        fontSize: "clamp(16px, 2vw, 28px)",
        fontWeight: 500,
        letterSpacing: "0.04em",
        textTransform: "uppercase",
        userSelect: "none",
      }}
    >
      {label}
    </div>
  );
}
