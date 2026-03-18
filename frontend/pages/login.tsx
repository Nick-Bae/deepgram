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

function mapFirebaseError(err: unknown): string {
  const code = typeof err === "object" && err && "code" in err ? String((err as { code?: string }).code || "") : "";
  if (code === "auth/invalid-credential") return "Invalid email or password.";
  if (code === "auth/user-not-found") return "Invalid email or password.";
  if (code === "auth/wrong-password") return "Invalid email or password.";
  if (code === "auth/too-many-requests") return "Too many attempts. Try again later.";
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

export default function LoginPage() {
  const router = useRouter();
  const { login, logout, getIdToken, user, configured, missingEnv } = useAuth();

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
      panelEyebrow="Host Access"
      panelTitle="Host Login"
      panelDescription="Sign in to manage your church services and continue where you left off."
      infoEyebrow="Access Flow"
      infoTitle="Route back to the right church dashboard."
      infoDescription="If your account already belongs to one or more churches, the session is routed back to the most relevant dashboard automatically."
      infoItems={[
        {
          title: "Fast continuation",
          description: "Existing members go straight back into the host workflow instead of repeating onboarding.",
        },
        {
          title: "Multi-church support",
          description: "If you belong to several organizations, the session keeps the right org context attached to your account.",
        },
        {
          title: "Support path",
          description: "Password reset and account recovery are available from here without exposing public account lookup.",
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

      {errorMsg ? <p style={buildStudioNoticeStyle("error")}>Error: {errorMsg}</p> : null}

      {user ? (
        <section style={{ display: "grid", gap: 12 }}>
          <div style={studioReadOnlyCardStyle}>
            <p style={{ margin: 0, fontSize: 13, color: "#5f6f86" }}>
              Already signed in as <strong style={{ color: "#22344c" }}>{user.email || user.uid}</strong>.
            </p>
          </div>
          <button
            type="button"
            onClick={() => {
              void continueExistingSession();
            }}
            disabled={!configured || sessionBusy}
            style={buildStudioButtonStyle({ disabled: !configured || sessionBusy })}
          >
            {sessionBusy ? "Continuing..." : "Continue to Dashboard"}
          </button>
          <button
            type="button"
            onClick={() => {
              void switchAccount();
            }}
            disabled={sessionBusy}
            style={buildStudioButtonStyle({ tone: "secondary", disabled: sessionBusy })}
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
          <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
            <Link href="/recover-account" style={{ color: "#3f6093", fontWeight: 700, fontSize: 13 }}>
              Forgot password?
            </Link>
          </div>

          <button type="submit" disabled={!configured || busy} style={buildStudioButtonStyle({ disabled: !configured || busy })}>
            {busy ? "Signing In..." : "Sign In"}
          </button>
        </form>
      )}

      <div style={{ display: "grid", gap: 8 }}>
        <p style={studioHelperTextStyle}>
          Need an account?{" "}
          <Link href={nextPath ? `/signup?next=${encodeURIComponent(nextPath)}` : "/signup"} style={{ color: "#3f6093", fontWeight: 700 }}>
            Create one
          </Link>
        </p>
        <p style={studioHelperTextStyle}>
          Need help with access or billing?{" "}
          <Link href="/contact" style={{ color: "#3f6093", fontWeight: 700 }}>
            Contact us
          </Link>
        </p>
      </div>
    </StudioAccessLayout>
  );
}
