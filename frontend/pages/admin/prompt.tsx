import { useRouter } from "next/router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, FormEvent } from "react";

import { useAuth } from "../../lib/authContext";
import { fetchAuthMe, fetchOrgPrompt, saveOrgPrompt, type OrgMembership } from "../../lib/backendAuth";

type MessageTone = "info" | "success" | "error";

export default function PromptAdmin() {
  const router = useRouter();
  const { user, loading: authLoading, getIdToken } = useAuth();

  const queryOrgId = typeof router.query.orgId === "string" ? router.query.orgId : "";
  const [memberships, setMemberships] = useState<OrgMembership[]>([]);
  const [selectedOrgId, setSelectedOrgId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [servicePrompt, setServicePrompt] = useState("");
  const [loadingMemberships, setLoadingMemberships] = useState(false);
  const [loadingPrompt, setLoadingPrompt] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [messageTone, setMessageTone] = useState<MessageTone>("info");
  const promptRequestSeq = useRef(0);

  const selectedMembership = useMemo(
    () => memberships.find((row) => row.orgId === selectedOrgId) || null,
    [memberships, selectedOrgId],
  );

  const setInfo = useCallback((text: string | null, tone: MessageTone = "info") => {
    setMessage(text);
    setMessageTone(tone);
  }, []);

  const syncOrgInUrl = useCallback(
    async (orgId: string) => {
      if (!router.isReady) return;
      const nextQuery = { ...router.query, orgId };
      await router.replace({ pathname: router.pathname, query: nextQuery }, undefined, { shallow: true });
    },
    [router],
  );

  const loadPromptForOrg = useCallback(
    async (orgId: string) => {
      if (!orgId) return;
      const requestSeq = ++promptRequestSeq.current;
      setLoadingPrompt(true);
      setInfo(null);
      try {
        const idToken = await getIdToken();
        if (!idToken) throw new Error("Please sign in again.");
        const payload = await fetchOrgPrompt(idToken, orgId);
        if (requestSeq !== promptRequestSeq.current) return;
        setPrompt(payload.prompt || "");
        setServicePrompt(payload.service_prompt || "");
      } catch (err: unknown) {
        if (requestSeq !== promptRequestSeq.current) return;
        setInfo(toMessage(err) || "Failed to load church prompt settings.", "error");
      } finally {
        if (requestSeq !== promptRequestSeq.current) return;
        setLoadingPrompt(false);
      }
    },
    [getIdToken, setInfo],
  );

  const loadMemberships = useCallback(async () => {
    if (!user) return;
    setLoadingMemberships(true);
    setInfo(null);
    try {
      const idToken = await getIdToken();
      if (!idToken) throw new Error("Please sign in again.");
      const me = await fetchAuthMe(idToken);
      const rows = me.memberships || [];
      setMemberships(rows);
      if (!rows.length) {
        setSelectedOrgId("");
        setPrompt("");
        setServicePrompt("");
        setInfo("No church memberships found. Create or join a church first.", "error");
        return;
      }
      const queryMatch = queryOrgId && rows.some((row) => row.orgId === queryOrgId) ? queryOrgId : "";
      const currentMatch =
        (me.currentOrgId || "").trim() && rows.some((row) => row.orgId === me.currentOrgId)
          ? String(me.currentOrgId)
          : "";
      const fallback = rows[0]?.orgId || "";
      const chosen = queryMatch || currentMatch || fallback;
      setSelectedOrgId(chosen);
      if (chosen && chosen !== queryOrgId) {
        await syncOrgInUrl(chosen);
      }
    } catch (err: unknown) {
      setInfo(toMessage(err) || "Failed to load church memberships.", "error");
    } finally {
      setLoadingMemberships(false);
    }
  }, [getIdToken, queryOrgId, setInfo, syncOrgInUrl, user]);

  useEffect(() => {
    if (authLoading) return;
    if (user) return;
    const nextPath = router.asPath || "/admin/prompt";
    void router.replace(`/login?next=${encodeURIComponent(nextPath)}`);
  }, [authLoading, router, user]);

  useEffect(() => {
    if (!router.isReady || authLoading || !user) return;
    void loadMemberships();
  }, [authLoading, loadMemberships, router.isReady, user]);

  useEffect(() => {
    if (authLoading || !user || !selectedOrgId) return;
    void loadPromptForOrg(selectedOrgId);
  }, [authLoading, loadPromptForOrg, selectedOrgId, user]);

  const savePrompt = async (e?: FormEvent) => {
    if (e) e.preventDefault();
    if (!selectedOrgId) {
      setInfo("Select a church first.", "error");
      return;
    }
    setSaving(true);
    setInfo("Saving...", "info");
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");
      await saveOrgPrompt(idToken, selectedOrgId, { prompt, service_prompt: servicePrompt });
      setInfo("Saved", "success");
    } catch (err: unknown) {
      setInfo(toMessage(err) || "Save failed", "error");
    } finally {
      setSaving(false);
    }
  };

  const clearPrompt = () => {
    setPrompt("");
    setInfo("Global guidance cleared (remember to Save).", "info");
  };

  const clearServicePrompt = () => {
    setServicePrompt("");
    setInfo("Service background cleared (remember to Save).", "info");
  };

  const onSelectOrg = async (nextOrgId: string) => {
    promptRequestSeq.current += 1;
    setSelectedOrgId(nextOrgId);
    setPrompt("");
    setServicePrompt("");
    setInfo(null);
    await syncOrgInUrl(nextOrgId);
  };

  const busy = loadingMemberships || loadingPrompt;
  const disableOrgSelect = loadingMemberships || saving || !memberships.length;

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>Church Translation Prompt</h1>
      <p style={styles.subtitle}>
        This page is church-specific. Use URL format <code>/admin/prompt?orgId=...</code> to open a specific church prompt.
      </p>

      <div style={styles.form}>
        <div style={styles.field}>
          <div style={styles.labelRow}>
            <h3 style={styles.sectionTitle}>Church</h3>
            {selectedMembership ? <span style={styles.helper}>Role: {selectedMembership.role || "viewer"}</span> : null}
          </div>
          <select
            value={selectedOrgId}
            onChange={(e) => {
              void onSelectOrg(e.target.value);
            }}
            disabled={disableOrgSelect}
            style={styles.select}
          >
            {memberships.map((row) => (
              <option key={row.orgId} value={row.orgId}>
                {row.name} ({row.slug})
              </option>
            ))}
          </select>
          {!memberships.length ? <span style={styles.helper}>No memberships available.</span> : null}
        </div>

        <form onSubmit={savePrompt} style={styles.formInner}>
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
              disabled={!selectedOrgId || busy}
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
              placeholder={'Example: Series: Advent Hope. Sermon: "Light in the Darkness". Scripture: Isaiah 9:1-7.'}
              rows={10}
              disabled={!selectedOrgId || busy}
            />
            <div style={styles.inlineMeta}>Chars: {servicePrompt.length}</div>
          </div>

          <div style={styles.actions}>
            <button type="submit" style={styles.button} disabled={saving || busy || !selectedOrgId}>
              {saving ? "Saving..." : "Save both"}
            </button>
            <button type="button" style={styles.secondary} onClick={clearServicePrompt} disabled={busy || !selectedOrgId}>
              Clear service background
            </button>
            <button type="button" style={styles.secondary} onClick={clearPrompt} disabled={busy || !selectedOrgId}>
              Clear global guidance
            </button>
            <button type="button" style={styles.secondary} onClick={() => void loadPromptForOrg(selectedOrgId)} disabled={busy || !selectedOrgId}>
              Refresh
            </button>
          </div>
        </form>
      </div>

      {message ? (
        <div style={messageTone === "error" ? styles.messageError : messageTone === "success" ? styles.messageSuccess : styles.messageInfo}>
          {message}
        </div>
      ) : null}
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
  formInner: {
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
  select: {
    width: "100%",
    borderRadius: 10,
    border: "1px solid #d0d5dd",
    padding: "10px",
    fontSize: "14px",
    background: "#f8fafc",
    color: "#111827",
  },
  textarea: {
    width: "100%",
    minHeight: "240px",
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
  inlineMeta: {
    color: "#6b7280",
    fontSize: "13px",
    textAlign: "right",
  },
  messageInfo: {
    marginTop: "12px",
    padding: "10px 12px",
    background: "#e0f2fe",
    color: "#075985",
    borderRadius: 8,
  },
  messageSuccess: {
    marginTop: "12px",
    padding: "10px 12px",
    background: "#dcfce7",
    color: "#14532d",
    borderRadius: 8,
  },
  messageError: {
    marginTop: "12px",
    padding: "10px 12px",
    background: "#fee2e2",
    color: "#991b1b",
    borderRadius: 8,
  },
};
