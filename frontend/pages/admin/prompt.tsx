import { useEffect, useState } from "react";
import type { CSSProperties, FormEvent } from "react";
import { API_URL } from "../../utils/urls";

export default function PromptAdmin() {
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const loadPrompt = async () => {
    setLoading(true);
    setMessage(null);
    try {
      const res = await fetch(`${API_URL}/api/prompt`);
      if (!res.ok) throw new Error(await res.text());
      const j = await res.json();
      setPrompt(j.prompt || "");
    } catch (err: unknown) {
      setMessage(toMessage(err) || "Failed to load prompt");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadPrompt();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const savePrompt = async (e?: FormEvent) => {
    if (e) e.preventDefault();
    setSaving(true);
    setMessage("Saving…");
    try {
      const res = await fetch(`${API_URL}/api/prompt`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt }),
      });
      if (!res.ok) throw new Error(await res.text());
      setMessage("Saved");
    } catch (err: unknown) {
      setMessage(toMessage(err) || "Save failed");
    } finally {
      setSaving(false);
    }
  };

  const clearPrompt = () => {
    setPrompt("");
    setMessage("Cleared (remember to Save)");
  };

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>Custom Translation Prompt</h1>
      <p style={styles.subtitle}>
        Edit the admin-defined guidance appended to the system prompt. Keep it concise and safe; changes apply to new translations after saving.
      </p>

      <form onSubmit={savePrompt} style={styles.form}>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          style={styles.textarea}
          placeholder="Add brief guardrails or style notes. Avoid long essays."
          rows={14}
        />
        <div style={styles.actions}>
          <button type="submit" style={styles.button} disabled={saving || loading}>
            {saving ? "Saving…" : "Save"}
          </button>
          <button type="button" style={styles.secondary} onClick={clearPrompt} disabled={loading}>
            Clear
          </button>
          <button type="button" style={styles.secondary} onClick={loadPrompt} disabled={loading}>
            Refresh
          </button>
          <span style={styles.note}>Chars: {prompt.length}</span>
        </div>
      </form>

      {message && <div style={styles.message}>{message}</div>}
    </div>
  );
}

const toMessage = (err: unknown) => {
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  try {
    return JSON.stringify(err);
  } catch {
    return "Unknown error";
  }
};

const styles: Record<string, CSSProperties> = {
  page: {
    padding: "24px",
    fontFamily: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    background: "#f5f6f8",
    minHeight: "100vh",
    color: "#111827",
  },
  title: {
    fontSize: "24px",
    fontWeight: 700,
    marginBottom: "8px",
  },
  subtitle: {
    color: "#4b5563",
    marginBottom: "16px",
  },
  form: {
    background: "white",
    borderRadius: 12,
    padding: "16px",
    boxShadow: "0 10px 30px rgba(15, 23, 42, 0.06)",
    display: "flex",
    flexDirection: "column",
    gap: "12px",
  },
  textarea: {
    width: "100%",
    minHeight: "280px",
    borderRadius: 10,
    border: "1px solid #d0d5dd",
    padding: "12px",
    fontSize: "14px",
    fontFamily: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    background: "#f8fafc",
    color: "#111827",
  },
  actions: {
    display: "flex",
    gap: "10px",
    alignItems: "center",
    flexWrap: "wrap",
  },
  button: {
    padding: "10px 14px",
    borderRadius: 8,
    border: "none",
    background: "#111827",
    color: "white",
    cursor: "pointer",
  },
  secondary: {
    padding: "10px 14px",
    borderRadius: 8,
    border: "1px solid #d0d5dd",
    background: "white",
    color: "#111827",
    cursor: "pointer",
  },
  note: {
    color: "#6b7280",
    fontSize: "13px",
  },
  message: {
    marginTop: "12px",
    padding: "10px 12px",
    background: "#e0f2fe",
    color: "#075985",
    borderRadius: 8,
  },
};

