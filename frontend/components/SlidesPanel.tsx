// components/SlidesPanel.tsx
// Design Ref: §5.5 — host-facing slides panel that composes uploader, thumbnail
// strip, navigation controls (prev/next/jump), and a small live-status banner.
// Keyboard shortcuts (←/→) advance slides while this panel is in the DOM.
"use client";
import { useCallback, useEffect, useMemo, useRef } from "react";
import SlideUploader from "./SlideUploader";
import SlideThumbnailStrip from "./SlideThumbnailStrip";
import { useSlideSync, type Slide } from "../utils/useSlideSync";

const MAX_SLIDES = 50; // matches backend MAX_SLIDES_PER_SERVICE default

type LiveStatus = {
  micActive: boolean;
  viewerCount: number | null;
  activeRoomId: string | null;
};

type Props = {
  orgId: string | null;
  serviceKey: string | null;
  serviceTitle?: string | null;
  getIdToken: (force?: boolean) => Promise<string | null>;
  liveStatus: LiveStatus;
  isActiveTab: boolean; // governs keyboard shortcut scope
};

export default function SlidesPanel({
  orgId,
  serviceKey,
  serviceTitle,
  getIdToken,
  liveStatus,
  isActiveTab,
}: Props) {
  const sync = useSlideSync({ orgId, serviceKey, getIdToken, enabled: !!orgId && !!serviceKey });
  const containerRef = useRef<HTMLDivElement | null>(null);

  const slideCount = sync.slides.length;
  const currentIndex = sync.currentSlideIndex;
  const remainingCapacity = Math.max(0, MAX_SLIDES - slideCount);

  const handleJump = useCallback(
    (index: number) => {
      if (index < 0 || index >= sync.slides.length) return;
      void sync.setIndex(index, liveStatus.activeRoomId);
    },
    [sync, liveStatus.activeRoomId],
  );

  const handlePrev = useCallback(() => {
    if (currentIndex == null) {
      if (slideCount > 0) handleJump(0);
      return;
    }
    if (currentIndex <= 0) return;
    handleJump(currentIndex - 1);
  }, [currentIndex, slideCount, handleJump]);

  const handleNext = useCallback(() => {
    if (currentIndex == null) {
      if (slideCount > 0) handleJump(0);
      return;
    }
    if (currentIndex >= slideCount - 1) return;
    handleJump(currentIndex + 1);
  }, [currentIndex, slideCount, handleJump]);

  // Design Ref: Module-3 question 2 — keyboard shortcuts only when this tab is active
  // AND focus is not in a text input (so editing captions doesn't hijack ←/→).
  useEffect(() => {
    if (!isActiveTab) return;
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target) {
        const tag = target.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || target.isContentEditable) return;
      }
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        handlePrev();
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        handleNext();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [isActiveTab, handlePrev, handleNext]);

  const handleDelete = useCallback(
    (slideId: string) => {
      if (typeof window !== "undefined" && !window.confirm("Delete this slide?")) return;
      void sync.remove(slideId);
    },
    [sync],
  );

  const handleReorder = useCallback(
    (orderedIds: string[]) => {
      void sync.reorder(orderedIds);
    },
    [sync],
  );

  const currentSlide: Slide | null = useMemo(() => {
    if (currentIndex == null || currentIndex >= sync.slides.length) return null;
    return sync.slides[currentIndex];
  }, [currentIndex, sync.slides]);

  if (!orgId || !serviceKey) {
    return (
      <div
        ref={containerRef}
        style={{ padding: 20, fontSize: 14, color: "#7d746c" }}
      >
        Select a service before managing slides.
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 16,
        maxWidth: 1280,
        margin: "0 auto",
      }}
    >
      <LiveStatusBanner liveStatus={liveStatus} serviceTitle={serviceTitle} />

      <SlideUploader
        uploading={sync.uploading}
        remainingCapacity={remainingCapacity}
        onFiles={sync.upload}
      />

      {sync.error && (
        <div
          style={{
            background: "rgba(159,54,80,0.08)",
            border: "1px solid rgba(159,54,80,0.25)",
            color: "#9f3650",
            padding: "10px 14px",
            borderRadius: 10,
            fontSize: 13,
            fontWeight: 600,
          }}
        >
          {sync.error}
        </div>
      )}

      {/* Current slide preview + nav controls */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1fr) minmax(220px, 320px)",
          gap: 16,
          alignItems: "stretch",
        }}
      >
        <div
          style={{
            background: "#0c0e12",
            borderRadius: 12,
            aspectRatio: "16 / 9",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            overflow: "hidden",
            minHeight: 220,
          }}
        >
          {currentSlide ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={currentSlide.url}
              alt={currentSlide.caption || `Slide ${currentIndex! + 1}`}
              style={{
                maxWidth: "100%",
                maxHeight: "100%",
                width: "auto",
                height: "auto",
                objectFit: "contain",
              }}
              draggable={false}
            />
          ) : (
            <div style={{ color: "rgba(255,255,255,0.35)", fontSize: 13, fontWeight: 600, letterSpacing: "0.04em", textTransform: "uppercase" }}>
              {slideCount === 0 ? "Upload slides to begin" : "No slide selected"}
            </div>
          )}
        </div>

        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 12,
            background: "rgba(255,255,255,0.55)",
            border: "1px solid rgba(120,98,78,0.16)",
            borderRadius: 12,
            padding: 14,
          }}
        >
          <div style={{ fontSize: 12, fontWeight: 700, letterSpacing: "0.12em", textTransform: "uppercase", color: "#7d746c" }}>
            Navigation
          </div>
          <div style={{ fontSize: 24, fontWeight: 800, color: "#27211d" }}>
            {currentIndex != null ? `${currentIndex + 1} / ${slideCount}` : `— / ${slideCount}`}
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button
              type="button"
              onClick={handlePrev}
              disabled={currentIndex == null || currentIndex <= 0}
              style={navButtonStyle(currentIndex != null && currentIndex > 0)}
            >
              ◀ Prev
            </button>
            <button
              type="button"
              onClick={handleNext}
              disabled={currentIndex == null ? slideCount === 0 : currentIndex >= slideCount - 1}
              style={navButtonStyle(slideCount > 0 && (currentIndex == null || currentIndex < slideCount - 1))}
            >
              Next ▶
            </button>
          </div>
          <div style={{ fontSize: 12, color: "#7d746c", lineHeight: 1.45 }}>
            {isActiveTab ? "Use ← / → keys while this tab is focused." : "Keyboard shortcuts active when this tab is focused."}
          </div>
          {sync.loading && <div style={{ fontSize: 12, color: "#7d746c" }}>Loading slides…</div>}
        </div>
      </div>

      {/* Thumbnail strip */}
      <div
        style={{
          background: "rgba(255,255,255,0.55)",
          border: "1px solid rgba(120,98,78,0.16)",
          borderRadius: 12,
          padding: "10px 12px",
        }}
      >
        <div style={{ fontSize: 12, fontWeight: 700, letterSpacing: "0.12em", textTransform: "uppercase", color: "#7d746c", marginBottom: 6 }}>
          Slide Order ({slideCount})
        </div>
        <SlideThumbnailStrip
          slides={sync.slides}
          currentIndex={currentIndex}
          onJump={handleJump}
          onDelete={handleDelete}
          onReorder={handleReorder}
          disabled={sync.uploading}
        />
      </div>
    </div>
  );
}

function navButtonStyle(active: boolean) {
  return {
    flex: 1,
    padding: "10px 14px",
    borderRadius: 10,
    border: "1px solid rgba(120,98,78,0.20)",
    background: active ? "#27211d" : "rgba(120,98,78,0.06)",
    color: active ? "#fff" : "#a8a098",
    fontSize: 13,
    fontWeight: 700,
    letterSpacing: "0.04em",
    cursor: active ? "pointer" : "not-allowed",
    transition: "background 150ms ease",
  } as const;
}

function LiveStatusBanner({
  liveStatus,
  serviceTitle,
}: {
  liveStatus: LiveStatus;
  serviceTitle?: string | null;
}) {
  const live = liveStatus.activeRoomId != null;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "10px 14px",
        borderRadius: 10,
        background: live ? "rgba(52,211,153,0.10)" : "rgba(120,98,78,0.06)",
        border: live ? "1px solid rgba(52,211,153,0.32)" : "1px solid rgba(120,98,78,0.18)",
        fontSize: 13,
        color: "#3a322d",
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: live ? "#34d399" : "rgba(120,98,78,0.35)",
          flexShrink: 0,
        }}
      />
      <span style={{ fontWeight: 700, letterSpacing: "0.06em", textTransform: "uppercase", fontSize: 11 }}>
        {live ? "Live broadcast active" : "Standby"}
      </span>
      {serviceTitle && (
        <span style={{ fontWeight: 600, color: "#5f5852" }}>· {serviceTitle}</span>
      )}
      <span style={{ flex: 1 }} />
      {liveStatus.viewerCount != null && live && (
        <span style={{ fontSize: 12, color: "#5f5852" }}>
          {liveStatus.viewerCount} viewer{liveStatus.viewerCount === 1 ? "" : "s"}
        </span>
      )}
      {liveStatus.micActive && (
        <span
          style={{
            fontSize: 11,
            fontWeight: 700,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            color: "#2d6a4f",
          }}
        >
          Mic on
        </span>
      )}
    </div>
  );
}
