import Link from "next/link";
import { useRouter } from "next/router";
import { useEffect, useMemo, useState } from "react";

import { previewOrgInvite, redeemOrgInvite, type InvitePreviewResponse } from "../lib/backendAuth";
import { useAuth } from "../lib/authContext";
import { clearHostToken, persistAuthToken, persistStreamContext } from "../utils/streamContext";

function readError(err: unknown): string {
  if (err instanceof Error) return err.message;
  return "Failed to process invite link.";
}

export default function JoinByInvitePage() {
  const router = useRouter();
  const { user, loading, configured, missingEnv, getIdToken } = useAuth();

  const [preview, setPreview] = useState<InvitePreviewResponse | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [joinBusy, setJoinBusy] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const inviteCode = useMemo(() => {
    const raw = typeof router.query.code === "string" ? router.query.code : "";
    return raw.trim();
  }, [router.query.code]);

  useEffect(() => {
    if (!router.isReady || loading || !configured || !user || !inviteCode) return;
    let cancelled = false;
    const run = async () => {
      setPreviewBusy(true);
      setErrorMsg(null);
      try {
        const idToken = await getIdToken();
        if (!idToken || cancelled) return;
        persistAuthToken(idToken);
        const resolved = await previewOrgInvite(idToken, inviteCode);
        if (cancelled) return;
        setPreview(resolved);
      } catch (err: unknown) {
        if (cancelled) return;
        setPreview(null);
        setErrorMsg(readError(err));
      } finally {
        if (!cancelled) setPreviewBusy(false);
      }
    };
    run();
    return () => {
      cancelled = true;
    };
  }, [configured, getIdToken, inviteCode, loading, router.isReady, user]);

  const onJoin = async () => {
    if (!inviteCode) {
      setErrorMsg("Invite code is missing.");
      return;
    }
    setJoinBusy(true);
    setErrorMsg(null);
    try {
      const idToken = await getIdToken(true);
      if (!idToken) throw new Error("Please sign in again.");
      persistAuthToken(idToken);
      const joined = await redeemOrgInvite(idToken, inviteCode);
      clearHostToken();
      persistStreamContext({ orgId: joined.orgId, churchSlug: joined.slug });
      const params = new URLSearchParams();
      params.set("orgId", joined.orgId);
      await router.replace(`/host/c/${encodeURIComponent(joined.slug)}/broadcast?${params.toString()}`);
    } catch (err: unknown) {
      setErrorMsg(readError(err));
    } finally {
      setJoinBusy(false);
    }
  };

  const showJoinButton = Boolean(preview && user && configured);
  const nextJoinPath = `/join?code=${encodeURIComponent(inviteCode || "")}`;

  return (
    <main style={{ minHeight: "100vh", display: "grid", placeItems: "center", background: "#0b1220", color: "#f8fafc", padding: 18 }}>
      <section style={{ width: "100%", maxWidth: 560, borderRadius: 14, border: "1px solid rgba(255,255,255,0.14)", background: "rgba(255,255,255,0.04)", padding: 18 }}>
        <h1 style={{ marginTop: 0, marginBottom: 8 }}>Join Church Workspace</h1>
        <p style={{ marginTop: 0, marginBottom: 12, opacity: 0.84 }}>Use your invite link to join a church organization.</p>

        {!configured ? (
          <div style={{ borderRadius: 10, border: "1px solid rgba(252,165,165,0.45)", background: "rgba(127,29,29,0.3)", padding: 12, color: "#fecaca", fontSize: 13 }}>
            Firebase config is missing in <code>frontend/.env.local</code>: {missingEnv.join(", ")}
          </div>
        ) : null}

        {!inviteCode ? (
          <div style={{ borderRadius: 10, border: "1px solid rgba(252,165,165,0.45)", background: "rgba(127,29,29,0.3)", padding: 12, color: "#fecaca" }}>
            Invite code is missing in the URL. Open the full invite link.
          </div>
        ) : null}

        {previewBusy ? <p style={{ marginTop: 12, marginBottom: 0, opacity: 0.82 }}>Checking invite...</p> : null}

        {preview ? (
          <div style={{ marginTop: 12, border: "1px solid rgba(255,255,255,0.16)", borderRadius: 12, padding: 12 }}>
            <p style={{ marginTop: 0, marginBottom: 6 }}><strong>Church:</strong> {preview.name}</p>
            <p style={{ marginTop: 0, marginBottom: 6 }}><strong>Slug:</strong> {preview.slug}</p>
            <p style={{ marginTop: 0, marginBottom: 6 }}><strong>Role:</strong> {preview.role}</p>
            <p style={{ marginTop: 0, marginBottom: 0, opacity: 0.82 }}>
              {preview.alreadyMember ? "You are already a member. Continue to dashboard." : "Click below to join this church workspace."}
            </p>
          </div>
        ) : null}

        {errorMsg ? <p style={{ color: "#fca5a5", marginTop: 12, marginBottom: 0 }}>Error: {errorMsg}</p> : null}

        {showJoinButton ? (
          <button
            onClick={onJoin}
            disabled={joinBusy}
            style={{
              marginTop: 14,
              borderRadius: 10,
              border: "none",
              padding: "10px 14px",
              fontWeight: 700,
              background: "#22c55e",
              color: "#052e16",
              cursor: joinBusy ? "not-allowed" : "pointer",
              opacity: joinBusy ? 0.6 : 1,
            }}
          >
            {joinBusy ? "Joining..." : preview?.alreadyMember ? "Go to dashboard" : "Join church"}
          </button>
        ) : null}

        {!user && configured && inviteCode ? (
          <div style={{ marginTop: 12, display: "grid", gap: 8 }}>
            <p style={{ margin: 0, opacity: 0.85 }}>
              This invite requires an account.
            </p>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              <Link
                href={`/login?next=${encodeURIComponent(nextJoinPath)}`}
                style={{
                  borderRadius: 8,
                  background: "#22c55e",
                  color: "#052e16",
                  fontWeight: 700,
                  padding: "8px 12px",
                }}
              >
                Sign in to join
              </Link>
              <Link
                href={`/signup?next=${encodeURIComponent(nextJoinPath)}`}
                style={{
                  borderRadius: 8,
                  border: "1px solid rgba(255,255,255,0.28)",
                  color: "#e2e8f0",
                  fontWeight: 700,
                  padding: "8px 12px",
                }}
              >
                Create account to join
              </Link>
            </div>
          </div>
        ) : null}
      </section>
    </main>
  );
}
