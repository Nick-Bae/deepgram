// components/SlideThumbnailStrip.tsx
// Design Ref: §5.5 — horizontal scrolling thumbnail strip with current-slide
// highlight, click-to-jump, and HTML5 native drag-drop reordering. Each
// thumbnail also has a delete button.
"use client";
import { useCallback, useState, type DragEvent } from "react";
import type { Slide } from "../utils/useSlideSync";

type Props = {
  slides: Slide[];
  currentIndex: number | null;
  onJump: (index: number) => void;
  onDelete: (slideId: string) => void;
  onReorder: (orderedIds: string[]) => void;
  disabled?: boolean;
};

export default function SlideThumbnailStrip({
  slides,
  currentIndex,
  onJump,
  onDelete,
  onReorder,
  disabled = false,
}: Props) {
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [hoverId, setHoverId] = useState<string | null>(null);

  const handleDragStart = useCallback((e: DragEvent<HTMLDivElement>, slideId: string) => {
    if (disabled) return;
    setDraggingId(slideId);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", slideId);
  }, [disabled]);

  const handleDragOver = useCallback((e: DragEvent<HTMLDivElement>, slideId: string) => {
    if (disabled || !draggingId || draggingId === slideId) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (hoverId !== slideId) setHoverId(slideId);
  }, [disabled, draggingId, hoverId]);

  const handleDrop = useCallback((e: DragEvent<HTMLDivElement>, targetId: string) => {
    e.preventDefault();
    if (disabled || !draggingId || draggingId === targetId) {
      setDraggingId(null);
      setHoverId(null);
      return;
    }
    const sourceIdx = slides.findIndex((s) => s.slideId === draggingId);
    const targetIdx = slides.findIndex((s) => s.slideId === targetId);
    if (sourceIdx === -1 || targetIdx === -1) {
      setDraggingId(null);
      setHoverId(null);
      return;
    }
    const next = slides.slice();
    const [moved] = next.splice(sourceIdx, 1);
    next.splice(targetIdx, 0, moved);
    onReorder(next.map((s) => s.slideId));
    setDraggingId(null);
    setHoverId(null);
  }, [disabled, draggingId, slides, onReorder]);

  const handleDragEnd = useCallback(() => {
    setDraggingId(null);
    setHoverId(null);
  }, []);

  if (!slides.length) {
    return (
      <div
        style={{
          padding: "24px 16px",
          textAlign: "center",
          color: "#9b938b",
          fontSize: 13,
          fontStyle: "italic",
        }}
      >
        No slides uploaded yet. Drag images above to get started.
      </div>
    );
  }

  return (
    <div
      style={{
        display: "flex",
        gap: 10,
        overflowX: "auto",
        overflowY: "hidden",
        padding: "8px 4px 14px",
        scrollbarWidth: "thin",
      }}
    >
      {slides.map((slide, idx) => {
        const isCurrent = currentIndex === idx;
        const isHover = hoverId === slide.slideId;
        const isDragging = draggingId === slide.slideId;
        return (
          <div
            key={slide.slideId}
            draggable={!disabled}
            onDragStart={(e) => handleDragStart(e, slide.slideId)}
            onDragOver={(e) => handleDragOver(e, slide.slideId)}
            onDrop={(e) => handleDrop(e, slide.slideId)}
            onDragEnd={handleDragEnd}
            style={{
              flex: "0 0 auto",
              width: 140,
              border: isCurrent
                ? "2px solid rgba(52,211,153,0.85)"
                : isHover
                ? "2px dashed rgba(120,98,78,0.6)"
                : "2px solid rgba(120,98,78,0.18)",
              borderRadius: 10,
              background: "#fff",
              boxShadow: isCurrent ? "0 6px 18px rgba(52,211,153,0.18)" : "0 2px 8px rgba(0,0,0,0.05)",
              opacity: isDragging ? 0.45 : 1,
              transition: "border-color 150ms ease, box-shadow 150ms ease, opacity 150ms ease",
              cursor: disabled ? "default" : "grab",
              position: "relative",
              userSelect: "none",
            }}
          >
            <button
              type="button"
              onClick={() => onJump(idx)}
              disabled={disabled}
              style={{
                width: "100%",
                aspectRatio: "16 / 9",
                border: "none",
                borderTopLeftRadius: 8,
                borderTopRightRadius: 8,
                background: "#0c0e12",
                padding: 0,
                cursor: disabled ? "default" : "pointer",
                display: "block",
                overflow: "hidden",
              }}
              aria-label={`Jump to slide ${idx + 1}`}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={slide.url}
                alt={slide.caption || `Slide ${idx + 1}`}
                style={{
                  width: "100%",
                  height: "100%",
                  objectFit: "contain",
                  display: "block",
                  pointerEvents: "none",
                }}
                draggable={false}
              />
            </button>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "5px 8px",
                fontSize: 11,
                fontWeight: 600,
                color: "#5f5852",
              }}
            >
              <span>
                {idx + 1}
                {isCurrent && (
                  <span
                    style={{
                      marginLeft: 6,
                      color: "#2d6a4f",
                      fontWeight: 700,
                      letterSpacing: "0.06em",
                      textTransform: "uppercase",
                    }}
                  >
                    Live
                  </span>
                )}
              </span>
              <button
                type="button"
                onClick={() => onDelete(slide.slideId)}
                disabled={disabled}
                title="Delete slide"
                style={{
                  background: "transparent",
                  border: "none",
                  color: "#9f3650",
                  fontSize: 14,
                  fontWeight: 700,
                  cursor: disabled ? "default" : "pointer",
                  lineHeight: 1,
                  padding: "2px 6px",
                  borderRadius: 6,
                }}
                aria-label={`Delete slide ${idx + 1}`}
              >
                ×
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
