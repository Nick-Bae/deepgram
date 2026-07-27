// Design Ref: §5.4 Page UI Checklist — "/admin/sermons/new (Ingest)".
// 3-tab form supporting Google Docs URL / .txt or .docx upload / paste text.
// Calls module-3's POST /api/org/{orgId}/sermons/ingest via lib/api/sermons.

"use client";

import { FormEvent, useCallback, useId, useMemo, useState } from "react";

import { useAuth } from "../lib/authContext";
import { ingestSermonFile, ingestSermonJson } from "../lib/api/sermons";
import { openGoogleDocsPicker } from "../lib/googlePicker";
import { IngestResult, ReviewMode, SermonApiError } from "../lib/types/sermon";

const GOOGLE_PICKER_API_KEY = process.env.NEXT_PUBLIC_GOOGLE_API_KEY ?? "";

type Tab = "paste" | "file" | "google_docs";

type Props = {
  orgId: string;
  onSuccess?: (result: IngestResult) => void;
  onCancel?: () => void;
};

const TXT_DOCX_ACCEPT = ".txt,.docx,text/plain,application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const FILE_SIZE_HINT = "Max 1 MB";

export default function SermonIngestForm({ orgId, onSuccess, onCancel }: Props) {
  const {
    getIdToken,
    getGoogleAccessToken,
    hasGoogleLinked,
    connectGoogleForDocs,
  } = useAuth();
  const titleId = useId();
  const pasteId = useId();
  const urlId = useId();
  const fileId = useId();

  const [tab, setTab] = useState<Tab>("paste");
  const [reviewMode, setReviewMode] = useState<ReviewMode>("app_assisted");
  const [title, setTitle] = useState("");
  const [pasteText, setPasteText] = useState("");
  const [docUrl, setDocUrl] = useState("");
  const [docName, setDocName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [picking, setPicking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = useMemo(() => {
    if (busy || !title.trim()) return false;
    if (tab === "paste") return pasteText.trim().length > 0;
    if (tab === "file") return file !== null;
    if (tab === "google_docs") return docUrl.trim().length > 0;
    return false;
  }, [busy, title, tab, pasteText, file, docUrl]);

  const handleConnectGoogle = useCallback(async () => {
    setError(null);
    setPicking(true);
    try {
      await connectGoogleForDocs();
    } catch (err) {
      const code = (err as { code?: string } | null)?.code;
      if (code === "auth/popup-closed-by-user" || code === "auth/cancelled-popup-request") {
        // user canceled — no message
      } else if (code === "auth/credential-already-in-use") {
        setError(
          "That Google account is already linked to a different user. Sign in as that user, or pick a different Google account."
        );
      } else {
        setError(
          err instanceof Error
            ? `Couldn't connect Google: ${err.message}`
            : "Couldn't connect Google."
        );
      }
    } finally {
      setPicking(false);
    }
  }, [connectGoogleForDocs]);

  const handlePickGoogleDoc = useCallback(async () => {
    setError(null);
    if (!GOOGLE_PICKER_API_KEY) {
      setError(
        "Google Picker is not configured. Set NEXT_PUBLIC_GOOGLE_API_KEY (with the Picker API enabled) and reload."
      );
      return;
    }
    let accessToken = getGoogleAccessToken();
    if (!accessToken) {
      try {
        accessToken = await connectGoogleForDocs();
      } catch (err) {
        const code = (err as { code?: string } | null)?.code;
        if (code === "auth/popup-closed-by-user" || code === "auth/cancelled-popup-request") {
          return; // user canceled
        }
        setError(
          err instanceof Error
            ? `Couldn't connect Google: ${err.message}`
            : "Couldn't connect Google."
        );
        return;
      }
    }
    setPicking(true);
    try {
      const result = await openGoogleDocsPicker({
        accessToken,
        apiKey: GOOGLE_PICKER_API_KEY,
        title: "Choose your sermon Google Doc",
      });
      if (!result) return; // user canceled
      setDocUrl(result.url);
      setDocName(result.name);
      if (!title.trim()) setTitle(result.name);
    } catch (err) {
      setError(
        err instanceof Error
          ? `Couldn't open Google Picker: ${err.message}`
          : "Couldn't open Google Picker."
      );
    } finally {
      setPicking(false);
    }
  }, [connectGoogleForDocs, getGoogleAccessToken, title]);

  const inferredFileSourceType = useCallback((selected: File): "file_txt" | "file_docx" | null => {
    const name = selected.name.toLowerCase();
    if (name.endsWith(".txt")) return "file_txt";
    if (name.endsWith(".docx")) return "file_docx";
    return null;
  }, []);

  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      setError(null);
      if (!orgId) {
        setError("Select a church first.");
        return;
      }
      const idToken = await getIdToken();
      if (!idToken) {
        setError("You are signed out. Please sign in again.");
        return;
      }

      setBusy(true);
      try {
        let result: IngestResult;
        if (tab === "paste") {
          result = await ingestSermonJson(
            {
              orgId,
              title: title.trim(),
              sourceType: "paste",
              reviewMode,
              text: pasteText,
            },
            { idToken }
          );
        } else if (tab === "google_docs") {
          const googleAccessToken = getGoogleAccessToken();
          if (!googleAccessToken) {
            setError(
              "Sign in with Google to import from Google Docs. Or paste the text / upload a .txt or .docx file instead."
            );
            return;
          }
          result = await ingestSermonJson(
            {
              orgId,
              title: title.trim(),
              sourceType: "google_docs",
              reviewMode,
              url: docUrl.trim(),
              googleAccessToken,
            },
            { idToken }
          );
        } else {
          if (!file) {
            setError("Choose a .txt or .docx file.");
            return;
          }
          const sourceType = inferredFileSourceType(file);
          if (!sourceType) {
            setError("Only .txt and .docx files are accepted.");
            return;
          }
          result = await ingestSermonFile(
            { orgId, title: title.trim(), sourceType, reviewMode, file },
            { idToken }
          );
        }
        onSuccess?.(result);
      } catch (err: unknown) {
        if (err instanceof SermonApiError) {
          if (err.code === "GOOGLE_OAUTH_NOT_CONFIGURED") {
            setError(
              "Your Google sign-in expired or wasn't authorized for Docs. Sign out and back in with Google, or paste/upload instead."
            );
          } else if (err.code === "GOOGLE_RATE_LIMITED") {
            setError(
              "Google Docs rate limit hit. Please wait a minute and try again."
            );
          } else {
            setError(err.message);
          }
        } else if (err instanceof Error) {
          setError(err.message);
        } else {
          setError("Unknown error.");
        }
      } finally {
        setBusy(false);
      }
    },
    [
      orgId,
      getIdToken,
      getGoogleAccessToken,
      tab,
      reviewMode,
      title,
      pasteText,
      docUrl,
      file,
      inferredFileSourceType,
      onSuccess,
    ]
  );

  return (
    <form
      onSubmit={handleSubmit}
      className="space-y-6 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm"
    >
      <header className="space-y-1">
        <h2 className="text-xl font-semibold text-slate-900">New Sermon</h2>
        <p className="text-sm text-slate-600">
          Choose a source, then decide whether the app should translate it now
          or create a blank template for your own translation.
        </p>
      </header>

      <div>
        <label htmlFor={titleId} className="block text-sm font-medium text-slate-700">
          Title
        </label>
        <input
          id={titleId}
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          maxLength={200}
          required
          className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-slate-900 shadow-sm focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
          placeholder="e.g. Easter Sermon — Romans 8"
        />
      </div>

      <div>
        <span className="block text-sm font-medium text-slate-700">
          Translation workflow
        </span>
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          {(
            [
              {
                id: "app_assisted",
                title: "App translates first",
                description: "Create a review file with app translations prefilled.",
              },
              {
                id: "pre_translated",
                title: "I have my own translation",
                description: "Create a three-column file with blank translation cells.",
              },
            ] as const
          ).map((option) => {
            const selected = reviewMode === option.id;
            return (
              <button
                key={option.id}
                type="button"
                onClick={() => setReviewMode(option.id)}
                className={
                  "rounded-lg border p-3 text-left transition " +
                  (selected
                    ? "border-slate-900 bg-slate-50"
                    : "border-slate-200 bg-white hover:border-slate-300")
                }
                aria-pressed={selected}
              >
                <span className="block text-sm font-medium text-slate-900">
                  {option.title}
                </span>
                <span className="mt-1 block text-xs text-slate-500">
                  {option.description}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      <div role="tablist" aria-label="Sermon source" className="flex gap-2 border-b border-slate-200">
        {(
          [
            { id: "paste", label: "Paste Text" },
            { id: "file", label: "Upload File" },
            { id: "google_docs", label: "Google Docs" },
          ] as const
        ).map(({ id, label }) => {
          const selected = tab === id;
          return (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={selected}
              onClick={() => setTab(id)}
              className={
                "px-4 py-2 text-sm font-medium transition " +
                (selected
                  ? "border-b-2 border-slate-900 text-slate-900"
                  : "text-slate-500 hover:text-slate-700")
              }
            >
              {label}
            </button>
          );
        })}
      </div>

      {tab === "paste" && (
        <div>
          <label htmlFor={pasteId} className="block text-sm font-medium text-slate-700">
            Korean sermon text
          </label>
          <textarea
            id={pasteId}
            value={pasteText}
            onChange={(e) => setPasteText(e.target.value)}
            rows={10}
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-sm text-slate-900 shadow-sm focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
            placeholder="오늘 우리는 하나님의 은혜를 보려고 합니다…"
          />
          <p className="mt-1 text-xs text-slate-500">
            {pasteText.length.toLocaleString()} characters
          </p>
        </div>
      )}

      {tab === "file" && (
        <div>
          <label htmlFor={fileId} className="block text-sm font-medium text-slate-700">
            .txt or .docx file
          </label>
          <input
            id={fileId}
            type="file"
            accept={TXT_DOCX_ACCEPT}
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="mt-1 block w-full text-sm text-slate-700 file:mr-3 file:rounded-lg file:border-0 file:bg-slate-900 file:px-4 file:py-2 file:text-sm file:font-medium file:text-white hover:file:bg-slate-700"
          />
          <p className="mt-1 text-xs text-slate-500">{FILE_SIZE_HINT}</p>
          {file && (
            <p className="mt-1 text-xs text-slate-700">
              Selected: {file.name} ({(file.size / 1024).toFixed(1)} KB)
            </p>
          )}
        </div>
      )}

      {tab === "google_docs" && (
        <div>
          <span id={urlId} className="block text-sm font-medium text-slate-700">
            Google Doc
          </span>
          <div className="mt-2 flex items-center gap-3">
            <button
              type="button"
              onClick={handlePickGoogleDoc}
              disabled={picking}
              aria-labelledby={urlId}
              className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {picking
                ? "Opening Picker…"
                : docUrl
                ? "Pick a different doc"
                : !getGoogleAccessToken() && !hasGoogleLinked()
                ? "Connect Google to pick a Doc"
                : "Pick from Google Drive"}
            </button>
            {docUrl && (
              <span className="truncate text-sm text-slate-700" title={docName || docUrl}>
                {docName || docUrl}
              </span>
            )}
          </div>
          <p className="mt-2 text-xs text-slate-500">
            {hasGoogleLinked() || getGoogleAccessToken()
              ? "The picker only shows documents your Google account can read."
              : "Connect your Google account once — your existing email/password login stays the same. Or paste/upload below instead."}
          </p>
          {!getGoogleAccessToken() && hasGoogleLinked() && (
            <button
              type="button"
              onClick={handleConnectGoogle}
              disabled={picking}
              className="mt-2 text-xs text-slate-600 underline hover:text-slate-900 disabled:cursor-not-allowed disabled:opacity-60"
            >
              Re-authorize Google access
            </button>
          )}
        </div>
      )}

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      <div className="flex justify-end gap-3 pt-2">
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            Cancel
          </button>
        )}
        <button
          type="submit"
          disabled={!canSubmit}
          className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {busy
            ? reviewMode === "pre_translated"
              ? "Creating…"
              : "Translating…"
            : reviewMode === "pre_translated"
              ? "Create Template"
              : "Translate"}
        </button>
      </div>
    </form>
  );
}
