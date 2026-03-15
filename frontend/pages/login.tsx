import Link from "next/link";
import { useRouter } from "next/router";
import { FormEvent, useMemo, useState } from "react";

import { fetchAuthMe, type OrgMembership } from "../lib/backendAuth";
import { useAuth } from "../lib/authContext";
import { buildDashboardHref, persistDashboardContext, pickPreferredMembership } from "../lib/dashboardRoute";
import { clearHostToken, persistAuthToken } from "../utils/streamContext";

function mapFirebaseError(err: unknown): string {
  const code = typeof err === "object" && err && "code" in err ? String((err as { code?: string }).code || "") : "";
  if (code === "auth/invalid-credential") return "Invalid email or password.";
  if (code === "auth/user-not-found") return "No user found for this email.";
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
    <main style={{ minHeight: "100vh", display: "grid", placeItems: "center", background: "#0b1220", color: "#f8fafc", padding: 18 }}>
      <section style={{ width: "100%", maxWidth: 420, borderRadius: 14, border: "1px solid rgba(255,255,255,0.14)", background: "rgba(255,255,255,0.04)", padding: 18 }}>
        <h1 style={{ marginTop: 0, marginBottom: 8 }}>Host Login</h1>
        <p style={{ marginTop: 0, marginBottom: 14, opacity: 0.82 }}>Sign in to manage your church services.</p>

        {!configured ? (
          <div style={{ borderRadius: 10, border: "1px solid rgba(252,165,165,0.45)", background: "rgba(127,29,29,0.3)", padding: 12, color: "#fecaca", fontSize: 13 }}>
            Firebase config is missing in <code>frontend/.env.local</code>: {missingEnv.join(", ")}
          </div>
        ) : null}

        {errorMsg ? (
          <p style={{ color: "#fca5a5", marginTop: 12, marginBottom: 0 }}>Error: {errorMsg}</p>
        ) : null}

        {user ? (
          <section style={{ marginTop: 12, display: "grid", gap: 10 }}>
            <p style={{ margin: 0, fontSize: 13, opacity: 0.84 }}>
              Already signed in as <strong>{user.email || user.uid}</strong>.
            </p>
            <button
              type="button"
              onClick={() => {
                void continueExistingSession();
              }}
              disabled={!configured || sessionBusy}
              style={{
                borderRadius: 10,
                border: "none",
                padding: "10px 12px",
                fontWeight: 700,
                background: "#22c55e",
                color: "#052e16",
                cursor: !configured || sessionBusy ? "not-allowed" : "pointer",
                opacity: !configured || sessionBusy ? 0.6 : 1,
              }}
            >
              {sessionBusy ? "Continuing..." : "Continue to dashboard"}
            </button>
            <button
              type="button"
              onClick={() => {
                void switchAccount();
              }}
              disabled={sessionBusy}
              style={{
                borderRadius: 10,
                border: "1px solid rgba(255,255,255,0.3)",
                padding: "10px 12px",
                fontWeight: 600,
                background: "transparent",
                color: "#e2e8f0",
                cursor: sessionBusy ? "not-allowed" : "pointer",
                opacity: sessionBusy ? 0.6 : 1,
              }}
            >
              Use a different account
            </button>
          </section>
        ) : (
          <form onSubmit={onSubmit} style={{ display: "grid", gap: 10, marginTop: 12 }}>
            <label style={{ display: "grid", gap: 4 }}>
              <span style={{ fontSize: 13, opacity: 0.84 }}>Email</span>
              <input
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.24)", background: "#0f172a", color: "#fff", padding: "10px 12px" }}
              />
            </label>
            <label style={{ display: "grid", gap: 4 }}>
              <span style={{ fontSize: 13, opacity: 0.84 }}>Password</span>
              <input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.24)", background: "#0f172a", color: "#fff", padding: "10px 12px" }}
              />
            </label>

            <button
              type="submit"
              disabled={!configured || busy}
              style={{
                marginTop: 4,
                borderRadius: 10,
                border: "none",
                padding: "10px 12px",
                fontWeight: 700,
                background: "#22c55e",
                color: "#052e16",
                cursor: !configured || busy ? "not-allowed" : "pointer",
                opacity: !configured || busy ? 0.6 : 1,
              }}
            >
              {busy ? "Signing in..." : "Sign in"}
            </button>
          </form>
        )}

        <p style={{ fontSize: 13, marginBottom: 0, marginTop: 12, opacity: 0.85 }}>
          Need an account?{" "}
          <Link href={nextPath ? `/signup?next=${encodeURIComponent(nextPath)}` : "/signup"} style={{ color: "#93c5fd" }}>
            Create one
          </Link>
        </p>
        <p style={{ fontSize: 13, marginBottom: 0, marginTop: 8, opacity: 0.8 }}>
          Need help with access or billing?{" "}
          <Link href="/contact" style={{ color: "#93c5fd" }}>
            Contact us
          </Link>
        </p>
      </section>
    </main>
  );
}
