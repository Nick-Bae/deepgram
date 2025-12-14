import { useEffect, useState } from "react";
import type { CSSProperties, FormEvent } from "react";
import { API_URL } from "../../utils/urls";

export default function PromptAdmin() {
  const [prompt, setPrompt] = useState("");
  const [servicePrompt, setServicePrompt] = useState("");
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
      setServicePrompt(j.service_prompt || j.servicePrompt || "");
    } catch (err: unknown) {
      setMessage(toMessage(err) || "Failed to load prompt");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadPrompt();
  }, []);

  const savePrompt = async (e?: FormEvent) => {
    if (e) e.preventDefault();
    setSaving(true);
    setMessage("Saving…");
    try {
      const res = await fetch(`${API_URL}/api/prompt`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, service_prompt: servicePrompt }),
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

  const clearServicePrompt = () => {
    setServicePrompt("");
    setMessage("Service background cleared (remember to Save)");
  };

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>Custom Translation Prompt</h1>
      <p style={styles.subtitle}>
        Edit the admin-defined guidance appended to the system prompt. Keep it concise and safe; changes apply to new translations after saving.
      </p>

      <form onSubmit={savePrompt} style={styles.form}>
        <div style={styles.field}>
          <div style={styles.labelRow}>
            <h3 style={styles.sectionTitle}>Global guidance</h3>
            <span style={styles.helper}>Always on; tweak sparingly.</span>
          </div>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            style={styles.textarea}
            placeholder="Add brief guardrails or style notes. Avoid long essays."
            rows={10}
          />
          <div style={styles.inlineMeta}>Chars: {prompt.length}</div>
        </div>

        <div style={styles.field}>
          <div style={styles.labelRow}>
            <h3 style={styles.sectionTitle}>Service background (today&apos;s sermon)</h3>
            <span style={styles.helper}>Update before each service; clear afterwards.</span>
          </div>
          <textarea
            value={servicePrompt}
            onChange={(e) => setServicePrompt(e.target.value)}
            style={styles.textarea}
            placeholder={'Example: Series: Advent Hope. Sermon: "Light in the Darkness". Scripture: Isaiah 9:1-7. Emphasis: hope, waiting, Christ as true light. Audience: mixed ages.'}
            rows={10}
          />
          <div style={styles.inlineMeta}>Chars: {servicePrompt.length}</div>
        </div>

        <div style={styles.actions}>
          <button type="submit" style={styles.button} disabled={saving || loading}>
            {saving ? "Saving…" : "Save both"}
          </button>
          <button type="button" style={styles.secondary} onClick={clearServicePrompt} disabled={loading}>
            Clear service background
          </button>
          <button type="button" style={styles.secondary} onClick={clearPrompt} disabled={loading}>
            Clear global guidance
          </button>
          <button type="button" style={styles.secondary} onClick={loadPrompt} disabled={loading}>
            Refresh
          </button>
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
  field: {
    display: "flex",
    flexDirection: "column",
    gap: "8px",
  },
  labelRow: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "baseline",
    gap: "12px",
    flexWrap: "wrap",
  },
  sectionTitle: {
    margin: 0,
    fontSize: "18px",
    fontWeight: 700,
  },
  helper: {
    color: "#6b7280",
    fontSize: "13px",
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
  inlineMeta: {
    color: "#6b7280",
    fontSize: "13px",
    textAlign: "right",
  },
  message: {
    marginTop: "12px",
    padding: "10px 12px",
    background: "#e0f2fe",
    color: "#075985",
    borderRadius: 8,
  },
};
