// utils/useSlideSync.ts
// Design Ref: §4 — REST hook for host slide management. Handles list, upload,
// reorder, delete, caption update, and current-index broadcast trigger.
import { useCallback, useEffect, useRef, useState } from "react";
import { API_URL } from "./urls";

export type Slide = {
  slideId: string;
  order: number;
  url: string;
  contentType: string;
  byteSize: number;
  width: number;
  height: number;
  caption?: string | null;
};

export type ApiError = { code: string; message: string };

type Options = {
  orgId: string | null;
  serviceKey: string | null;
  getIdToken: (force?: boolean) => Promise<string | null>;
  enabled?: boolean;
};

const MAX_CLIENT_WIDTH = 1920; // Design §3.2 — defense in depth against bloated uploads.

async function readJsonOrThrow(res: Response): Promise<any> {
  const txt = await res.text();
  let body: any = null;
  try {
    body = txt ? JSON.parse(txt) : null;
  } catch {
    body = null;
  }
  if (!res.ok) {
    const detail = body?.detail;
    if (detail && typeof detail === "object" && detail.code) {
      const err = new Error(detail.message || detail.code);
      (err as any).code = detail.code;
      (err as any).status = res.status;
      throw err;
    }
    const err = new Error(typeof detail === "string" ? detail : `HTTP ${res.status}`);
    (err as any).status = res.status;
    throw err;
  }
  return body?.data ?? body;
}

/**
 * Resize an image client-side to a max width while preserving aspect ratio.
 * Returns the original blob if Pillow-equivalent shrinking isn't needed.
 */
async function maybeResize(file: File, maxWidth: number): Promise<File> {
  if (typeof window === "undefined" || typeof document === "undefined") return file;
  if (!file.type.startsWith("image/")) return file;
  const bitmap = await createImageBitmap(file).catch(() => null);
  if (!bitmap) return file;
  if (bitmap.width <= maxWidth) {
    bitmap.close?.();
    return file;
  }
  const scale = maxWidth / bitmap.width;
  const targetW = Math.round(bitmap.width * scale);
  const targetH = Math.round(bitmap.height * scale);
  const canvas = document.createElement("canvas");
  canvas.width = targetW;
  canvas.height = targetH;
  const ctx = canvas.getContext("2d");
  if (!ctx) {
    bitmap.close?.();
    return file;
  }
  ctx.drawImage(bitmap, 0, 0, targetW, targetH);
  bitmap.close?.();
  const mime = file.type === "image/png" ? "image/png" : "image/jpeg";
  const blob: Blob | null = await new Promise((resolve) =>
    canvas.toBlob(resolve, mime, mime === "image/jpeg" ? 0.92 : undefined),
  );
  if (!blob) return file;
  const newName = file.name.replace(/\.(png|jpg|jpeg)$/i, "") +
    (mime === "image/png" ? ".png" : ".jpg");
  return new File([blob], newName, { type: mime, lastModified: Date.now() });
}

export function useSlideSync(opts: Options) {
  const { orgId, serviceKey, getIdToken, enabled = true } = opts;
  const [slides, setSlides] = useState<Slide[]>([]);
  const [currentSlideIndex, setCurrentSlideIndex] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef<AbortController | null>(null);

  const baseUrl = useCallback(() => {
    if (!orgId || !serviceKey) return null;
    return `${API_URL}/api/org/${encodeURIComponent(orgId)}/services/${encodeURIComponent(serviceKey)}/slides`;
  }, [orgId, serviceKey]);

  const authHeaders = useCallback(async (): Promise<Record<string, string>> => {
    const token = await getIdToken();
    if (!token) throw new Error("not_authenticated");
    return { Authorization: `Bearer ${token}` };
  }, [getIdToken]);

  const refresh = useCallback(async () => {
    const url = baseUrl();
    if (!url) return;
    inFlight.current?.abort();
    const ctrl = new AbortController();
    inFlight.current = ctrl;
    setLoading(true);
    setError(null);
    try {
      const headers = await authHeaders();
      const res = await fetch(url, { headers, signal: ctrl.signal });
      const data = await readJsonOrThrow(res);
      setSlides((data?.slides as Slide[]) || []);
      const idx = data?.currentSlideIndex;
      setCurrentSlideIndex(typeof idx === "number" ? idx : null);
    } catch (e: any) {
      if (e?.name === "AbortError") return;
      setError(e?.message || "Failed to load slides.");
    } finally {
      setLoading(false);
    }
  }, [authHeaders, baseUrl]);

  const upload = useCallback(
    async (files: File[]): Promise<{ uploaded: number; error?: string }> => {
      const url = baseUrl();
      if (!url) return { uploaded: 0, error: "not_ready" };
      if (!files.length) return { uploaded: 0 };
      setUploading(true);
      setError(null);
      try {
        const headers = await authHeaders();
        const form = new FormData();
        for (const file of files) {
          const resized = await maybeResize(file, MAX_CLIENT_WIDTH);
          form.append("files", resized, resized.name);
        }
        const res = await fetch(url, { method: "POST", headers, body: form });
        const data = await readJsonOrThrow(res);
        const newSlides = (data?.slides as Slide[]) || [];
        setSlides((prev) => [...prev, ...newSlides].sort((a, b) => a.order - b.order));
        return { uploaded: newSlides.length };
      } catch (e: any) {
        const msg = e?.message || "Upload failed.";
        setError(msg);
        return { uploaded: 0, error: msg };
      } finally {
        setUploading(false);
      }
    },
    [authHeaders, baseUrl],
  );

  const remove = useCallback(
    async (slideId: string): Promise<boolean> => {
      const url = baseUrl();
      if (!url) return false;
      setError(null);
      try {
        const headers = await authHeaders();
        const res = await fetch(`${url}/${encodeURIComponent(slideId)}`, {
          method: "DELETE",
          headers,
        });
        await readJsonOrThrow(res);
        setSlides((prev) => prev.filter((s) => s.slideId !== slideId).map((s, i) => ({ ...s, order: i })));
        return true;
      } catch (e: any) {
        setError(e?.message || "Delete failed.");
        return false;
      }
    },
    [authHeaders, baseUrl],
  );

  const reorder = useCallback(
    async (orderedIds: string[]): Promise<boolean> => {
      const url = baseUrl();
      if (!url) return false;
      setError(null);
      // Optimistic local reorder.
      setSlides((prev) => {
        const lookup = new Map(prev.map((s) => [s.slideId, s]));
        const next: Slide[] = [];
        orderedIds.forEach((id, i) => {
          const found = lookup.get(id);
          if (found) next.push({ ...found, order: i });
        });
        return next;
      });
      try {
        const headers = { ...(await authHeaders()), "Content-Type": "application/json" };
        const res = await fetch(`${url}/order`, {
          method: "PATCH",
          headers,
          body: JSON.stringify({ orderedSlideIds: orderedIds }),
        });
        const data = await readJsonOrThrow(res);
        if (data?.slides) setSlides(data.slides as Slide[]);
        return true;
      } catch (e: any) {
        setError(e?.message || "Reorder failed.");
        await refresh();
        return false;
      }
    },
    [authHeaders, baseUrl, refresh],
  );

  const updateCaption = useCallback(
    async (slideId: string, caption: string): Promise<boolean> => {
      const url = baseUrl();
      if (!url) return false;
      setError(null);
      try {
        const headers = { ...(await authHeaders()), "Content-Type": "application/json" };
        const res = await fetch(`${url}/${encodeURIComponent(slideId)}`, {
          method: "PATCH",
          headers,
          body: JSON.stringify({ caption }),
        });
        const data = await readJsonOrThrow(res);
        if (data?.slide) {
          setSlides((prev) => prev.map((s) => (s.slideId === slideId ? (data.slide as Slide) : s)));
        }
        return true;
      } catch (e: any) {
        setError(e?.message || "Update failed.");
        return false;
      }
    },
    [authHeaders, baseUrl],
  );

  const setIndex = useCallback(
    async (index: number, roomId?: string | null): Promise<boolean> => {
      const url = baseUrl();
      if (!url) return false;
      setError(null);
      // Optimistic update.
      const previousIndex = currentSlideIndex;
      setCurrentSlideIndex(index);
      try {
        const headers = { ...(await authHeaders()), "Content-Type": "application/json" };
        const res = await fetch(`${url}/index`, {
          method: "POST",
          headers,
          body: JSON.stringify({ index, ...(roomId ? { roomId } : {}) }),
        });
        const data = await readJsonOrThrow(res);
        if (typeof data?.currentSlideIndex === "number") {
          setCurrentSlideIndex(data.currentSlideIndex);
        }
        return true;
      } catch (e: any) {
        setError(e?.message || "Failed to advance slide.");
        setCurrentSlideIndex(previousIndex);
        return false;
      }
    },
    [authHeaders, baseUrl, currentSlideIndex],
  );

  // Initial load + reload on org/service change.
  useEffect(() => {
    if (!enabled || !orgId || !serviceKey) return;
    void refresh();
    return () => inFlight.current?.abort();
  }, [enabled, orgId, serviceKey, refresh]);

  return {
    slides,
    currentSlideIndex,
    loading,
    uploading,
    error,
    refresh,
    upload,
    remove,
    reorder,
    updateCaption,
    setIndex,
  };
}
