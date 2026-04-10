import Link from "next/link";
import { useRouter } from "next/router";
import { FormEvent, useMemo, useState } from "react";

import StudioAccessLayout, {
  buildStudioButtonStyle,
  buildStudioNoticeStyle,
  studioFieldStyle,
  studioHelperTextStyle,
  studioLabelStyle,
  studioLabelTextStyle,
  studioReadOnlyCardStyle,
} from "../components/StudioAccessLayout";
import { fetchAuthMe, type OrgMembership } from "../lib/backendAuth";
import { useAuth } from "../lib/authContext";
import { buildDashboardHref, persistDashboardContext, pickPreferredMembership } from "../lib/dashboardRoute";
import { clearHostToken, persistAuthToken } from "../utils/streamContext";
import { API_URL } from "../utils/urls";

async function hashEmail(email: string): Promise<string> {
  const data = new TextEncoder().encode(email.toLowerCase().trim());
  const buffer = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(buffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function reportLoginFailed(email: string): Promise<void> {
  try {
    const emailHash = await hashEmail(email);
    await fetch(`${API_URL}/api/auth/login-failed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email_hash: emailHash }),
    });
  } catch {
    // fire-and-forget; never surfaces to the user
  }
}

function mapFirebaseError(err: unknown): string {
  const code = typeof err === "object" && err && "code" in err ? String((err as { code?: string }).code || "") : "";
  if (code === "auth/invalid-credential") return "Invalid email or password.";
  if (code === "auth/user-not-found") return "Invalid email or password.";
  if (code === "auth/wrong-password") return "Invalid email or password.";
  if (code === "auth/too-many-requests") return "Too many attempts. Try again later.";
  if (code === "auth/popup-closed-by-user") return "Google sign-in was cancelled.";
  if (code === "auth/popup-blocked") return "Your browser blocked the Google sign-in popup. Please allow popups and try again.";
  if (code === "auth/operation-not-allowed") return "Google sign-in is not enabled for this Firebase project yet.";
  if (code === "auth/google-signup-required") return "No Google account is registered here yet. Sign up first.";
  if (code === "auth/account-exists-with-different-credential") {
    return "An account already exists for this email with another sign-in method. Sign in with email and password first.";
  }
  if (err instanceof Error) return err.message;
  return "Login failed.";
}

function isInvalidSessionError(err: unknown): boolean {
  const message = err instanceof Error ? err.message.toLowerCase() : String(err || "").toLowerCase();
  return message.includes("invalid_id_token") || message.includes("session is invalid") || message.includes("invalid id token");
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

type NextRouteTarget = {
  orgId?: string;
  slug?: string;
};

function parseNextRouteTarget(nextPath: string): NextRouteTarget {
  try {
    const parsed = new URL(nextPath, "http://localhost");
    const orgId = (parsed.searchParams.get("orgId") || parsed.searchParams.get("org_id") || "").trim() || undefined;
    const hostMatch = parsed.pathname.match(/^\/host\/c\/([^/]+)/i);
    const slug = hostMatch ? decodeURIComponent(hostMatch[1] || "").trim() || undefined : undefined;
    return { orgId, slug };
  } catch {
    return {};
  }
}

function membershipForTarget(target: NextRouteTarget, memberships: OrgMembership[]): OrgMembership | undefined {
  if (target.orgId) {
    const byOrg = memberships.find((row) => row.orgId === target.orgId);
    if (byOrg) return byOrg;
  }
  if (target.slug) return memberships.find((row) => row.slug === target.slug);
  return undefined;
}

function GoogleMarkIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true" focusable="false">
      <path fill="#FFC107" d="M43.611 20.083H42V20H24v8h11.303C33.653 32.657 29.223 36 24 36c-6.627 0-12-5.373-12-12s5.373-12 12-12c3.059 0 5.842 1.154 7.961 3.039l5.657-5.657C34.046 6.053 29.268 4 24 4 12.955 4 4 12.955 4 24s8.955 20 20 20 20-8.955 20-20c0-1.341-.138-2.65-.389-3.917z" />
      <path fill="#FF3D00" d="m6.306 14.691 6.571 4.819C14.655 15.108 18.961 12 24 12c3.059 0 5.842 1.154 7.961 3.039l5.657-5.657C34.046 6.053 29.268 4 24 4 16.318 4 9.656 8.337 6.306 14.691z" />
      <path fill="#4CAF50" d="M24 44c5.166 0 9.86-1.977 13.409-5.192l-6.19-5.238C29.211 35.091 26.715 36 24 36c-5.202 0-9.619-3.317-11.283-7.946l-6.522 5.025C9.505 39.556 16.227 44 24 44z" />
      <path fill="#1976D2" d="M43.611 20.083H42V20H24v8h11.303a12.04 12.04 0 0 1-4.087 5.571l.003-.002 6.19 5.238C36.971 39.205 44 34 44 24c0-1.341-.138-2.65-.389-3.917z" />
    </svg>
  );
}

export default function LoginPage() {
  const router = useRouter();
  const { login, loginWithGoogle, logout, getIdToken, user, configured, missingEnv } = useAuth();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [sessionBusy, setSessionBusy] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const nextPath = useMemo(() => {
    const raw = typeof router.query.next === "string" ? router.query.next : "";
    if (!raw.startsWith("/") || raw.startsWith("//")) return "";
    return raw;
  }, [router.query.next]);
  const signupHref = useMemo(
    () => (nextPath ? `/signup?next=${encodeURIComponent(nextPath)}` : "/signup"),
    [nextPath],
  );

  const redirectFromMembership = async (idToken: string) => {
    const me = await fetchAuthMe(idToken);
    const memberships = me.memberships || [];
    const primary = pickPreferredMembership(me);
    const target = parseNextRouteTarget(nextPath);
    const targetMembership = membershipForTarget(target, memberships);
    const hasOrgScopedTarget = Boolean(target.orgId || target.slug);

    if (nextPath && nextPath !== "/login" && !nextPath.startsWith("/login?") && (!hasOrgScopedTarget || targetMembership)) {
      const sessionMembership = targetMembership || primary;
      if (sessionMembership) {
        persistDashboardContext(sessionMembership);
      }
      await router.replace(nextPath);
      return;
    }
    if (!primary) {
      clearHostToken();
      await router.replace("/onboarding/create-church");
      return;
    }
    persistDashboardContext(primary);
    await router.replace(buildDashboardHref(primary));
  };

  const redirectWithFreshSession = async () => {
    let lastError: unknown = null;
    for (let attempt = 0; attempt < 4; attempt += 1) {
      const token = await getIdToken(true);
      if (!token) throw new Error("Please sign in again.");
      persistAuthToken(token);
      try {
        await redirectFromMembership(token);
        return;
      } catch (err) {
        if (!isInvalidSessionError(err)) throw err;
        lastError = err;
        await delay(250 * (attempt + 1));
      }
    }
    if (lastError) throw lastError;
    throw new Error("Please sign in again.");
  };

  const continueExistingSession = async () => {
    setSessionBusy(true);
    setErrorMsg(null);
    try {
      await redirectWithFreshSession();
    } catch (err) {
      if (isInvalidSessionError(err)) {
        try {
          await logout();
        } catch {}
      }
      setErrorMsg(mapFirebaseError(err));
    } finally {
      setSessionBusy(false);
    }
  };

  const switchAccount = async () => {
    setSessionBusy(true);
    setErrorMsg(null);
    try {
      await logout();
      clearHostToken();
      setEmail("");
      setPassword("");
    } catch (err) {
      setErrorMsg(mapFirebaseError(err));
    } finally {
      setSessionBusy(false);
    }
  };

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErrorMsg(null);
    try {
      await login(email.trim(), password);
      await redirectWithFreshSession();
    } catch (err) {
      const code = typeof err === "object" && err && "code" in err ? String((err as { code?: string }).code || "") : "";
      if (code === "auth/too-many-requests" || code === "auth/wrong-password" || code === "auth/invalid-credential") {
        void reportLoginFailed(email.trim());
      }
      if (isInvalidSessionError(err)) {
        try {
          await logout();
        } catch {}
      }
      setErrorMsg(mapFirebaseError(err));
    } finally {
      setBusy(false);
    }
  };

  const signInWithGoogle = async () => {
    setBusy(true);
    setErrorMsg(null);
    try {
      await loginWithGoogle();
      await redirectWithFreshSession();
    } catch (err) {
      if (isInvalidSessionError(err)) {
        try {
          await logout();
        } catch {}
      }
      setErrorMsg(mapFirebaseError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <StudioAccessLayout
      pageTitle="Host Login | Worship"
      pageDescription="Sign in to manage church services, rooms, and team access."
      panelEyebrow={user ? "Current Session" : "Host Access"}
      panelTitle={user ? "Resume session" : "Sign In"}
      panelDescription={
        user
          ? "Continue with the authenticated account below or switch to another account before entering the dashboard."
          : "Use your host account to manage services, team access, and church broadcast settings."
      }
      infoEyebrow="Access Flow"
      infoTitle={nextPath ? "Sign in and return to the page that sent you here." : "One host login can route into the right church workspace."}
      infoDescription={
        nextPath
          ? "Protected routes can send you to the login page first. After a successful sign-in, the app restores the original destination automatically."
          : "Email/password and Google sign-in both use the same account routing flow, so you land back in the correct church dashboard after authentication."
      }
      infoItems={[
        {
          title: "Redirect-safe sign-in",
          description: nextPath
            ? "This login keeps the original target so the session can continue exactly where it was interrupted."
            : "When your account belongs to multiple churches, the app picks the preferred membership and routes there automatically.",
        },
        {
          title: "Use the current session or switch",
          description: "If this browser already has a valid session, you can continue immediately or sign out and use a different account.",
        },
        {
          title: "Google or password",
          description: "Google sign-in and email/password both feed the same host access flow and end at the same dashboard routing logic.",
        },
      ]}
      headerActions={[
        { href: "/", label: "Back Home" },
        { href: "/contact", label: "Contact Us", accent: true },
      ]}
    >
      {nextPath ? (
        <p style={buildStudioNoticeStyle("info")}>
          After sign-in, you&apos;ll be returned to <code>{nextPath}</code>.
        </p>
      ) : null}

      {!configured ? (
        <div style={buildStudioNoticeStyle("error")}>
          Firebase config is missing in <code>frontend/.env.local</code>: {missingEnv.join(", ")}
        </div>
      ) : null}

      {errorMsg ? <p style={buildStudioNoticeStyle("error")}>Error: {errorMsg}</p> : null}

      {user ? (
        <section style={{ display: "grid", gap: 14 }}>
          <div style={studioReadOnlyCardStyle}>
            <p style={{ margin: 0, fontSize: 11, fontWeight: 800, letterSpacing: "0.18em", textTransform: "uppercase", color: "#8b6533" }}>
              Active session
            </p>
            <p style={{ margin: "10px 0 0", fontSize: 16, fontWeight: 700, color: "#22344c", wordBreak: "break-word" }}>
              {user.email || user.uid}
            </p>
            <p style={{ margin: "8px 0 0", fontSize: 14, lineHeight: 1.7, color: "#5d6d84" }}>
              Continue with this session or switch accounts before entering the dashboard.
            </p>
          </div>

          <button
            type="button"
            onClick={() => {
              void continueExistingSession();
            }}
            disabled={!configured || sessionBusy}
            style={{ ...buildStudioButtonStyle({ disabled: !configured || sessionBusy }), width: "100%" }}
          >
            {sessionBusy ? "Continuing..." : "Continue to Dashboard"}
          </button>

          <button
            type="button"
            onClick={() => {
              void switchAccount();
            }}
            disabled={sessionBusy}
            style={{ ...buildStudioButtonStyle({ tone: "secondary", disabled: sessionBusy }), width: "100%" }}
          >
            Use a Different Account
          </button>
        </section>
      ) : (
        <form onSubmit={onSubmit} style={{ display: "grid", gap: 12 }}>
          <label style={studioLabelStyle}>
            <span style={studioLabelTextStyle}>Email</span>
            <input
              type="email"
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoFocus
              style={studioFieldStyle}
            />
          </label>

          <label style={studioLabelStyle}>
            <span style={studioLabelTextStyle}>Password</span>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              style={studioFieldStyle}
            />
          </label>

          <button
            type="submit"
            disabled={!configured || busy}
            style={{ ...buildStudioButtonStyle({ disabled: !configured || busy }), width: "100%" }}
          >
            {busy ? "Signing In..." : "Sign In"}
          </button>

          <div style={{ display: "flex", alignItems: "center", gap: 12, margin: "4px 0" }}>
            <div style={{ flex: 1, height: 1, background: "rgba(120,98,78,0.16)" }} />
            <span style={{ fontSize: 11, fontWeight: 800, letterSpacing: "0.18em", textTransform: "uppercase", color: "#8f8378" }}>
              or
            </span>
            <div style={{ flex: 1, height: 1, background: "rgba(120,98,78,0.16)" }} />
          </div>

          <button
            type="button"
            onClick={() => {
              void signInWithGoogle();
            }}
            disabled={!configured || busy}
            style={{ ...buildStudioButtonStyle({ tone: "secondary", disabled: !configured || busy }), width: "100%", gap: 10 }}
          >
            <GoogleMarkIcon />
            Continue with Google
          </button>
        </form>
      )}

      {!user ? (
        <div style={{ display: "grid", gap: 8 }}>
          <p style={studioHelperTextStyle}>
            Forgot your password or can&apos;t remember the login email?{" "}
            <Link href="/recover-account" style={{ color: "#3f6093", fontWeight: 700 }}>
              Recover account
            </Link>
          </p>
          <p style={studioHelperTextStyle}>
            Need a host account first?{" "}
            <Link href={signupHref} style={{ color: "#3f6093", fontWeight: 700 }}>
              Create one
            </Link>
          </p>
        </div>
      ) : null}
    </StudioAccessLayout>
  );
}
