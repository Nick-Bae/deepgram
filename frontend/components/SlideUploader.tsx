// components/SlideUploader.tsx
// Design Ref: §5.5 — drag-drop + button file picker for PNG/JPEG slide images.
// Client-side resize happens inside useSlideSync.upload(); this component is
// purely UI/UX for selecting files and surfacing progress + errors.
"use client";
import { useCallback, useRef, useState, type DragEvent, type ChangeEvent } from "react";

type Props = {
  uploading: boolean;
  remainingCapacity: number; // how many more slides can fit before MAX_SLIDES_PER_SERVICE
  onFiles: (files: File[]) => Promise<{ uploaded: number; error?: string }>;
};

const ACCEPTED_TYPES = ["image/png", "image/jpeg"];
const MAX_BYTES = 10 * 1024 * 1024; // matches backend MAX_SLIDE_IMAGE_BYTES default

export default function SlideUploader({ uploading, remainingCapacity, onFiles }: Props) {
  const [dragOver, setDragOver] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const validate = useCallback(
    (files: File[]): { ok: File[]; error: string | null } => {
      const ok: File[] = [];
      for (const f of files) {
        if (!ACCEPTED_TYPES.includes(f.type)) {
          return { ok: [], error: "Only PNG or JPEG images are accepted." };
        }
        if (f.size === 0) {
          return { ok: [], error: "File is empty." };
        }
        if (f.size > MAX_BYTES) {
          return { ok: [], error: `Image must be under ${MAX_BYTES / (1024 * 1024)}MB.` };
        }
        ok.push(f);
      }
      if (!ok.length) return { ok, error: "No valid files selected." };
      if (ok.length > remainingCapacity) {
        return {
          ok: [],
          error: `Only ${remainingCapacity} more slide${remainingCapacity === 1 ? "" : "s"} can be added (limit reached).`,
        };
      }
      return { ok, error: null };
    },
    [remainingCapacity],
  );

  const handleFiles = useCallback(
    async (rawFiles: FileList | File[] | null) => {
      setLocalError(null);
      setLastResult(null);
      const files = Array.from(rawFiles || []);
      if (!files.length) return;
      const { ok, error } = validate(files);
      if (error) {
        setLocalError(error);
        return;
      }
      const result = await onFiles(ok);
      if (result.error) {
        setLocalError(result.error);
      } else if (result.uploaded > 0) {
        setLastResult(`Uploaded ${result.uploaded} slide${result.uploaded === 1 ? "" : "s"}.`);
      }
    },
    [onFiles, validate],
  );

  const onDrop = useCallback(
    (e: DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      setDragOver(false);
      void handleFiles(e.dataTransfer.files);
    },
    [handleFiles],
  );

  const onDragOver = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    if (!dragOver) setDragOver(true);
  }, [dragOver]);

  const onDragLeave = useCallback(() => setDragOver(false), []);

  const onChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      void handleFiles(e.target.files);
      // Reset so the same file selection retriggers onChange.
      if (inputRef.current) inputRef.current.value = "";
    },
    [handleFiles],
  );

  const disabled = uploading || remainingCapacity <= 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div
        onDrop={onDrop}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        style={{
          padding: "20px 18px",
          borderRadius: 14,
          border: dragOver
            ? "2px dashed rgba(52,211,153,0.7)"
            : "2px dashed rgba(120,98,78,0.30)",
          background: dragOver ? "rgba(52,211,153,0.08)" : "rgba(255,255,255,0.45)",
          textAlign: "center",
          transition: "background 150ms ease, border-color 150ms ease",
          color: "#3a322d",
        }}
      >
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>
          Drag &amp; drop slide images here
        </div>
        <div style={{ fontSize: 12, color: "#7d746c", marginBottom: 12 }}>
          PNG or JPEG · up to 10MB each · {remainingCapacity > 0 ? `${remainingCapacity} slot${remainingCapacity === 1 ? "" : "s"} left` : "limit reached"}
        </div>
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          disabled={disabled}
          style={{
            padding: "8px 18px",
            borderRadius: 999,
            border: "1px solid rgba(120,98,78,0.20)",
            background: disabled ? "rgba(120,98,78,0.08)" : "#fff",
            color: disabled ? "#a8a098" : "#27211d",
            fontSize: 13,
            fontWeight: 700,
            cursor: disabled ? "not-allowed" : "pointer",
            letterSpacing: "0.02em",
          }}
        >
          {uploading ? "Uploading…" : "Choose Files"}
        </button>
        <input
          ref={inputRef}
          type="file"
          accept="image/png,image/jpeg"
          multiple
          onChange={onChange}
          style={{ display: "none" }}
        />
      </div>
      {localError && (
        <div style={{ fontSize: 13, color: "#9f3650", fontWeight: 600 }}>
          {localError}
        </div>
      )}
      {lastResult && !localError && (
        <div style={{ fontSize: 13, color: "#2d6a4f", fontWeight: 600 }}>
          {lastResult}
        </div>
      )}
    </div>
  );
}
