import Link from "next/link";
import { useRouter } from "next/router";
import { useEffect, useMemo, useState } from "react";

import StudioAccessLayout, {
  buildStudioButtonStyle,
  buildStudioNoticeStyle,
  studioReadOnlyCardStyle,
  studioSubtleTextStyle,
} from "../components/StudioAccessLayout";
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
  const inviteChurchName = useMemo(() => {
    const raw = typeof router.query.church === "string" ? router.query.church : "";
    return raw.trim().slice(0, 120);
  }, [router.query.church]);
  const inviteChurchLabel = (preview?.name || inviteChurchName || "").trim();

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
  const nextJoinPath = useMemo(() => {
    const params = new URLSearchParams();
    if (inviteCode) params.set("code", inviteCode);
    if (inviteChurchName) params.set("church", inviteChurchName);
    return `/join?${params.toString()}`;
  }, [inviteChurchName, inviteCode]);

  return (
    <StudioAccessLayout
      pageTitle="Join Church Workspace | Worship"
      pageDescription="Use an invite link to join a church workspace."
      panelEyebrow="Invite Access"
      panelTitle={inviteChurchLabel ? `Join ${inviteChurchLabel}` : "Join Church Workspace"}
      panelDescription={
        inviteChurchLabel
          ? `Use your invite link to join ${inviteChurchLabel}.`
          : "Use your invite link to join a church organization."
      }
      infoEyebrow="Invite Flow"
      infoTitle={inviteChurchLabel ? `Accept the invite for ${inviteChurchLabel}.` : "Accept the invite and land in the right workspace."}
      infoDescription="Invite links preserve church context so the account can be attached to the correct organization before routing into the dashboard."
      infoItems={[
        {
          title: "Single-use join flow",
          description: "The invite preview confirms the church, role, and destination before the membership is attached to your account.",
        },
        {
          title: "Account required",
          description: "If you are not signed in yet, the invite path is preserved through login or signup so you can continue where you left off.",
        },
        {
          title: "Direct to dashboard",
          description: "After redemption, the session moves directly into the church host dashboard with the org context already stored.",
        },
      ]}
      headerActions={[
        { href: "/", label: "Back Home" },
        { href: "/contact", label: "Contact Us", accent: true },
      ]}
    >
      {!configured ? (
        <div style={buildStudioNoticeStyle("error")}>
          Firebase config is missing in <code>frontend/.env.local</code>: {missingEnv.join(", ")}
        </div>
      ) : null}

      {!inviteCode ? <div style={buildStudioNoticeStyle("error")}>Invite code is missing in the URL. Open the full invite link.</div> : null}

      {previewBusy ? <p style={buildStudioNoticeStyle("info")}>Checking invite...</p> : null}

      {!preview && inviteChurchLabel ? (
        <div style={studioReadOnlyCardStyle}>
          <p style={{ marginTop: 0, marginBottom: 0, ...studioSubtleTextStyle }}>
            <strong style={{ color: "#22344c" }}>Invited church:</strong> {inviteChurchLabel}
          </p>
        </div>
      ) : null}

      {preview ? (
        <div style={studioReadOnlyCardStyle}>
          <p style={{ marginTop: 0, marginBottom: 6, ...studioSubtleTextStyle }}>
            <strong style={{ color: "#22344c" }}>Church:</strong> {preview.name}
          </p>
          <p style={{ marginTop: 0, marginBottom: 6, ...studioSubtleTextStyle }}>
            <strong style={{ color: "#22344c" }}>Slug:</strong> {preview.slug}
          </p>
          <p style={{ marginTop: 0, marginBottom: 6, ...studioSubtleTextStyle }}>
            <strong style={{ color: "#22344c" }}>Role:</strong> {preview.role}
          </p>
          <p style={{ marginTop: 0, marginBottom: 0, ...studioSubtleTextStyle }}>
            {preview.alreadyMember ? "You are already a member. Continue to dashboard." : "Click below to join this church workspace."}
          </p>
        </div>
      ) : null}

      {errorMsg ? <p style={buildStudioNoticeStyle("error")}>Error: {errorMsg}</p> : null}

      {showJoinButton ? (
        <button onClick={onJoin} disabled={joinBusy} style={buildStudioButtonStyle({ disabled: joinBusy })}>
          {joinBusy ? "Joining..." : preview?.alreadyMember ? "Go to Dashboard" : "Join Church"}
        </button>
      ) : null}

      {!user && configured && inviteCode ? (
        <div style={{ display: "grid", gap: 10 }}>
          <p style={studioSubtleTextStyle}>
            {inviteChurchLabel ? `Create an account to continue joining ${inviteChurchLabel}.` : "Create an account to continue joining this church."}
          </p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
            <Link href={`/signup?next=${encodeURIComponent(nextJoinPath)}`} style={buildStudioButtonStyle()}>
              Continue to Join
            </Link>
          </div>
          <p style={{ ...studioSubtleTextStyle, margin: 0, fontSize: 12 }}>
            Already have an account?{" "}
            <Link href={`/login?next=${encodeURIComponent(nextJoinPath)}`} style={{ color: "#3f6093", fontWeight: 700, textDecoration: "none" }}>
              Sign in
            </Link>
          </p>
        </div>
      ) : null}
    </StudioAccessLayout>
  );
}
