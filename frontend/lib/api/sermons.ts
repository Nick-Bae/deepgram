// Design Ref: §4 API Specification.
// Thin fetch wrappers for the 6 sermon-review endpoints. All functions take
// an idToken (obtained via lib/authContext.getIdToken) so authentication is
// always explicit at the call site.

import { API_URL } from "../../utils/urls";
import {
  ApiErrorPayload,
  IngestResult,
  LinkResult,
  Sermon,
  SermonApiError,
  SermonSummary,
  SourceType,
  ValidationReport,
} from "../types/sermon";

const REQUEST_TIMEOUT_MS = 30_000;

type FetchOptions = {
  idToken: string;
  signal?: AbortSignal;
  timeoutMs?: number;
};

async function parseError(res: Response): Promise<SermonApiError> {
  try {
    const json = (await res.json()) as { error?: ApiErrorPayload };
    if (json && typeof json === "object" && json.error) {
      return new SermonApiError(json.error, res.status);
    }
  } catch {
    // fall through
  }
  return new SermonApiError(
    {
      code: res.statusText || "INTERNAL_ERROR",
      message: `HTTP ${res.status}`,
    },
    res.status
  );
}

function authHeaders(idToken: string): HeadersInit {
  return { Authorization: `Bearer ${idToken}` };
}

function withTimeout(
  signal: AbortSignal | undefined,
  timeoutMs: number
): { signal: AbortSignal; cleanup: () => void } {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const onAbort = () => controller.abort();
  signal?.addEventListener("abort", onAbort, { once: true });
  return {
    signal: controller.signal,
    cleanup: () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
    },
  };
}

export type IngestJsonInput = {
  orgId: string;
  title: string;
  sourceType: Extract<SourceType, "paste" | "google_docs">;
  text?: string;
  url?: string;
  /** Google OAuth access token (from Firebase Google sign-in) for sourceType=google_docs. */
  googleAccessToken?: string;
};

export async function ingestSermonJson(
  input: IngestJsonInput,
  options: FetchOptions
): Promise<IngestResult> {
  const form = new FormData();
  form.set("sourceType", input.sourceType);
  form.set("title", input.title);
  if (input.text !== undefined) form.set("text", input.text);
  if (input.url !== undefined) form.set("url", input.url);

  const headers: Record<string, string> = {
    Authorization: `Bearer ${options.idToken}`,
  };
  if (input.googleAccessToken) {
    headers["X-Google-Access-Token"] = input.googleAccessToken;
  }

  const { signal, cleanup } = withTimeout(
    options.signal,
    options.timeoutMs ?? REQUEST_TIMEOUT_MS
  );
  try {
    const res = await fetch(
      `${API_URL}/api/org/${encodeURIComponent(input.orgId)}/sermons/ingest`,
      {
        method: "POST",
        headers,
        body: form,
        signal,
      }
    );
    if (!res.ok) throw await parseError(res);
    const json = (await res.json()) as { data: IngestResult };
    return json.data;
  } finally {
    cleanup();
  }
}

export type IngestFileInput = {
  orgId: string;
  title: string;
  sourceType: Extract<SourceType, "file_txt" | "file_docx">;
  file: File;
};

export async function ingestSermonFile(
  input: IngestFileInput,
  options: FetchOptions
): Promise<IngestResult> {
  const form = new FormData();
  form.set("sourceType", input.sourceType);
  form.set("title", input.title);
  form.set("file", input.file);

  const { signal, cleanup } = withTimeout(
    options.signal,
    options.timeoutMs ?? REQUEST_TIMEOUT_MS
  );
  try {
    const res = await fetch(
      `${API_URL}/api/org/${encodeURIComponent(input.orgId)}/sermons/ingest`,
      {
        method: "POST",
        headers: authHeaders(options.idToken),
        body: form,
        signal,
      }
    );
    if (!res.ok) throw await parseError(res);
    const json = (await res.json()) as { data: IngestResult };
    return json.data;
  } finally {
    cleanup();
  }
}

export async function listSermons(
  orgId: string,
  options: FetchOptions
): Promise<SermonSummary[]> {
  const { signal, cleanup } = withTimeout(
    options.signal,
    options.timeoutMs ?? REQUEST_TIMEOUT_MS
  );
  try {
    const res = await fetch(
      `${API_URL}/api/org/${encodeURIComponent(orgId)}/sermons`,
      { method: "GET", headers: authHeaders(options.idToken), signal }
    );
    if (!res.ok) throw await parseError(res);
    const json = (await res.json()) as { data: SermonSummary[] };
    return json.data;
  } finally {
    cleanup();
  }
}

export async function getSermon(
  orgId: string,
  sermonId: string,
  options: FetchOptions
): Promise<Sermon> {
  const { signal, cleanup } = withTimeout(
    options.signal,
    options.timeoutMs ?? REQUEST_TIMEOUT_MS
  );
  try {
    const res = await fetch(
      `${API_URL}/api/org/${encodeURIComponent(orgId)}/sermons/${encodeURIComponent(sermonId)}`,
      { method: "GET", headers: authHeaders(options.idToken), signal }
    );
    if (!res.ok) throw await parseError(res);
    const json = (await res.json()) as { data: Sermon };
    return json.data;
  } finally {
    cleanup();
  }
}

export type ExportReviewFileResult = {
  blob: Blob;
  filename: string;
};

export async function exportReviewFile(
  orgId: string,
  sermonId: string,
  options: FetchOptions
): Promise<ExportReviewFileResult> {
  const { signal, cleanup } = withTimeout(
    options.signal,
    options.timeoutMs ?? REQUEST_TIMEOUT_MS
  );
  try {
    const res = await fetch(
      `${API_URL}/api/org/${encodeURIComponent(orgId)}/sermons/${encodeURIComponent(sermonId)}/review-file.xlsx`,
      { method: "GET", headers: authHeaders(options.idToken), signal }
    );
    if (!res.ok) throw await parseError(res);
    const blob = await res.blob();
    const filename = extractFilename(res.headers.get("content-disposition"));
    return { blob, filename };
  } finally {
    cleanup();
  }
}

function extractFilename(contentDisposition: string | null): string {
  if (!contentDisposition) return "sermon-review.xlsx";
  const encodedMatch = /filename\*=UTF-8''([^;]+)/i.exec(contentDisposition);
  if (encodedMatch?.[1]) {
    try {
      return decodeURIComponent(encodedMatch[1]);
    } catch {
      // Fall back to the ASCII filename below.
    }
  }
  const match = /filename="([^"]+)"/.exec(contentDisposition);
  return match?.[1] ?? "sermon-review.xlsx";
}

/** Trigger a browser download from a Blob. */
export function triggerBlobDownload(blob: Blob, filename: string): void {
  if (typeof document === "undefined") return;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Defer revoke so the click has time to start the download.
  setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

export async function importReviewFile(
  orgId: string,
  sermonId: string,
  file: File,
  options: FetchOptions
): Promise<ValidationReport> {
  const form = new FormData();
  form.set("file", file);

  const { signal, cleanup } = withTimeout(
    options.signal,
    options.timeoutMs ?? REQUEST_TIMEOUT_MS
  );
  try {
    const res = await fetch(
      `${API_URL}/api/org/${encodeURIComponent(orgId)}/sermons/${encodeURIComponent(sermonId)}/review-file`,
      {
        method: "POST",
        headers: authHeaders(options.idToken),
        body: form,
        signal,
      }
    );
    if (!res.ok) throw await parseError(res);
    const json = (await res.json()) as { data: ValidationReport };
    return json.data;
  } finally {
    cleanup();
  }
}

export type LinkSermonInput = {
  orgId: string;
  sermonId: string;
  serviceKey: string;
  replace?: boolean;
};

export async function linkSermon(
  input: LinkSermonInput,
  options: FetchOptions
): Promise<LinkResult> {
  const { signal, cleanup } = withTimeout(
    options.signal,
    options.timeoutMs ?? REQUEST_TIMEOUT_MS
  );
  try {
    const res = await fetch(
      `${API_URL}/api/org/${encodeURIComponent(input.orgId)}/sermons/${encodeURIComponent(input.sermonId)}/link`,
      {
        method: "POST",
        headers: {
          ...authHeaders(options.idToken),
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          serviceKey: input.serviceKey,
          replace: input.replace ?? false,
        }),
        signal,
      }
    );
    if (!res.ok) throw await parseError(res);
    const json = (await res.json()) as { data: LinkResult };
    return json.data;
  } finally {
    cleanup();
  }
}
