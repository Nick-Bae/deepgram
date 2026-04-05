import Head from "next/head";
import Link from "next/link";
import { useRouter } from "next/router";
import { FormEvent, useMemo, useState } from "react";

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

function FieldIcon({ kind }: { kind: "email" | "password" }) {
  if (kind === "password") {
    return (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <rect x="5" y="11" width="14" height="9" rx="2.5" stroke="#617089" strokeWidth="1.6" />
        <path d="M8.5 11V8.8a3.5 3.5 0 0 1 7 0V11" stroke="#617089" strokeWidth="1.6" strokeLinecap="round" />
      </svg>
    );
  }

  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M16.5 19v-1.1a3.4 3.4 0 0 0-3.4-3.4H10.9a3.4 3.4 0 0 0-3.4 3.4V19" stroke="#617089" strokeWidth="1.6" strokeLinecap="round" />
      <circle cx="12" cy="8.2" r="3.1" stroke="#617089" strokeWidth="1.6" />
    </svg>
  );
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

function buildLoginNoticeStyle(tone: "error" | "info") {
  if (tone === "error") {
    return {
      margin: 0,
      padding: "12px 14px",
      borderRadius: 18,
      background: "linear-gradient(145deg, rgba(255,233,236,0.94), rgba(255,245,246,0.9))",
      border: "1px solid rgba(208,110,131,0.18)",
      color: "#aa4f65",
      boxShadow: "inset 1px 1px 0 rgba(255,255,255,0.9), 0 12px 24px rgba(194,120,138,0.12)",
      fontSize: 13,
      lineHeight: 1.5,
    } as const;
  }

  return {
    margin: 0,
    padding: "12px 14px",
    borderRadius: 18,
    background: "linear-gradient(145deg, rgba(236,245,255,0.94), rgba(244,249,255,0.9))",
    border: "1px solid rgba(123,157,196,0.16)",
    color: "#516b86",
    boxShadow: "inset 1px 1px 0 rgba(255,255,255,0.9), 0 12px 24px rgba(160,177,198,0.12)",
    fontSize: 13,
    lineHeight: 1.5,
  } as const;
}

function buildLoginButtonStyle(options?: { tone?: "primary" | "secondary"; disabled?: boolean }) {
  const tone = options?.tone || "primary";
  const disabled = Boolean(options?.disabled);

  if (tone === "secondary") {
    return {
      width: "100%",
      borderRadius: 999,
      border: "1px solid rgba(255,255,255,0.9)",
      background: "linear-gradient(145deg, #eef3f9, #ffffff)",
      color: "#5d697d",
      fontSize: 15,
      fontWeight: 800,
      padding: "15px 18px",
      cursor: disabled ? "not-allowed" : "pointer",
      opacity: disabled ? 0.62 : 1,
      boxShadow: "10px 10px 20px rgba(163,177,198,0.20), -10px -10px 20px rgba(255,255,255,0.92), inset 1px 1px 0 rgba(255,255,255,0.9)",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      gap: 10,
    } as const;
  }

  return {
    width: "100%",
    borderRadius: 999,
    border: "1px solid rgba(107,212,245,0.2)",
    background: "linear-gradient(180deg, #67d1f4 0%, #4bb9dd 100%)",
    color: "#ffffff",
    fontSize: 17,
    fontWeight: 900,
    letterSpacing: "0.01em",
    padding: "16px 18px",
    cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? 0.62 : 1,
    boxShadow: "0 20px 26px rgba(75,185,221,0.26), inset 0 1px 0 rgba(255,255,255,0.42), inset 0 -2px 0 rgba(48,139,169,0.18)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    gap: 10,
  } as const;
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

  const cardShellStyle = {
    position: "relative" as const,
    width: "min(100%, 380px)",
  };
  const cardStyle = {
    position: "relative" as const,
    borderRadius: 34,
    padding: "28px 24px 22px",
    background: "linear-gradient(145deg, #e7edf5 0%, #d9e2ed 100%)",
    border: "1px solid rgba(255,255,255,0.84)",
    boxShadow: "22px 22px 44px rgba(162,176,198,0.28), -16px -16px 34px rgba(255,255,255,0.92), inset 1px 1px 0 rgba(255,255,255,0.88)",
    overflow: "hidden" as const,
  };
  const fieldWrapStyle = {
    display: "flex",
    alignItems: "center",
    gap: 12,
    minHeight: 56,
    padding: "0 16px",
    borderRadius: 999,
    background: "linear-gradient(145deg, #dfe7f2, #ffffff)",
    border: "1px solid rgba(255,255,255,0.95)",
    boxShadow: "inset 8px 8px 16px rgba(176,190,208,0.22), inset -8px -8px 16px rgba(255,255,255,0.98), 0 12px 22px rgba(162,176,198,0.12)",
  };
  const inputStyle = {
    width: "100%",
    border: "none",
    outline: "none",
    background: "transparent",
    color: "#2f3948",
    fontSize: 15,
    fontWeight: 700,
  };
  const footerLinkStyle = {
    color: "#62708a",
    fontWeight: 800,
    textDecoration: "none",
  };
  const srOnlyStyle = {
    position: "absolute" as const,
    width: 1,
    height: 1,
    padding: 0,
    margin: -1,
    overflow: "hidden" as const,
    clip: "rect(0, 0, 0, 0)",
    whiteSpace: "nowrap" as const,
    border: 0,
  };

  return (
    <>
      <Head>
        <title>Host Login | Worship</title>
        <meta name="description" content="Sign in to manage church services, rooms, and team access." />
      </Head>
      <style>{`
        @media (max-width: 480px) {
          .login-card { padding: 24px 18px 20px !important; border-radius: 30px !important; }
          .login-card h1 { font-size: 36px !important; }
        }
      `}</style>
      <main
        style={{
          minHeight: "100vh",
          display: "grid",
          placeItems: "center",
          padding: "28px 16px",
          background: "radial-gradient(circle at top, rgba(255,255,255,0.95) 0%, rgba(228,236,247,0.92) 38%, #dce4ef 100%)",
          position: "relative",
          overflow: "hidden",
          fontFamily: "'Avenir Next', 'Segoe UI', sans-serif",
        }}
      >
        <div aria-hidden="true" style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
          <div style={{ position: "absolute", top: -160, left: -120, width: 320, height: 320, borderRadius: "50%", background: "radial-gradient(circle, rgba(255,255,255,0.96), transparent 72%)", filter: "blur(18px)" }} />
          <div style={{ position: "absolute", right: -80, top: 80, width: 260, height: 260, borderRadius: "50%", background: "radial-gradient(circle, rgba(119,213,245,0.16), transparent 70%)", filter: "blur(10px)" }} />
          <div style={{ position: "absolute", left: "18%", bottom: -80, width: 340, height: 280, borderRadius: "50%", background: "radial-gradient(circle, rgba(163,177,198,0.14), transparent 72%)", filter: "blur(20px)" }} />
        </div>

        <section style={cardShellStyle}>
          <div aria-hidden="true" style={{ position: "absolute", inset: "12px 18px -12px", borderRadius: 34, background: "rgba(166,180,199,0.18)", filter: "blur(18px)" }} />
          <article className="login-card" style={cardStyle}>
            <div aria-hidden="true" style={{ position: "absolute", inset: 0, background: "linear-gradient(135deg, rgba(255,255,255,0.55), transparent 28%, transparent 72%, rgba(255,255,255,0.18) 100%)", pointerEvents: "none" }} />

            <div style={{ position: "relative", display: "grid", gap: 18 }}>
              <div style={{ display: "grid", placeItems: "center", gap: 12 }}>
                <div
                  style={{
                    width: 84,
                    height: 84,
                    borderRadius: "50%",
                    background: "linear-gradient(160deg, #0f1323, #2a3152 62%, #101522)",
                    boxShadow: "0 18px 26px rgba(66,78,99,0.28), -8px -8px 20px rgba(255,255,255,0.86), inset 0 0 0 4px rgba(255,255,255,0.92), inset 0 10px 18px rgba(255,255,255,0.08)",
                    display: "grid",
                    placeItems: "center",
                  }}
                >
                  <div style={{ display: "grid", placeItems: "center", gap: 3 }}>
                    <div style={{ fontSize: 24, fontWeight: 900, lineHeight: 1, color: "#7c69ff", letterSpacing: "-0.08em" }}>W</div>
                    <div style={{ fontSize: 8, fontWeight: 800, letterSpacing: "0.12em", textTransform: "uppercase", color: "#f3f6fb" }}>Host</div>
                  </div>
                </div>

                <div style={{ textAlign: "center" }}>
                  <h1 style={{ margin: 0, fontSize: 40, lineHeight: 1.02, letterSpacing: "-0.06em", color: "#111827", fontWeight: 900 }}>
                    Host Login
                  </h1>
                  <p style={{ margin: "8px 0 0", color: "#758198", fontSize: 15, fontWeight: 700, letterSpacing: "0.18em", textTransform: "uppercase" }}>
                    Translation Studio
                  </p>
                  <p style={{ margin: "8px 0 0", color: "#7d8798", fontSize: 14, fontWeight: 600 }}>
                    Smooth access. 3D card. Same auth flow.
                  </p>
                </div>
              </div>

              {!configured ? (
                <div style={buildLoginNoticeStyle("error")}>
                  Firebase config is missing in <code>frontend/.env.local</code>: {missingEnv.join(", ")}
                </div>
              ) : null}

              {errorMsg ? <p style={buildLoginNoticeStyle("error")}>Error: {errorMsg}</p> : null}

              {user ? (
                <section style={{ display: "grid", gap: 12 }}>
                  <div style={{ ...fieldWrapStyle, minHeight: 74, borderRadius: 24, alignItems: "flex-start", paddingTop: 14, paddingBottom: 14 }}>
                    <div style={{ marginTop: 2 }}>
                      <FieldIcon kind="email" />
                    </div>
                    <div style={{ display: "grid", gap: 4 }}>
                      <span style={{ fontSize: 11, fontWeight: 800, letterSpacing: "0.16em", textTransform: "uppercase", color: "#7e8aa0" }}>
                        Active session
                      </span>
                      <span style={{ fontSize: 14, color: "#2f3948", fontWeight: 700 }}>{user.email || user.uid}</span>
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      void continueExistingSession();
                    }}
                    disabled={!configured || sessionBusy}
                    style={buildLoginButtonStyle({ disabled: !configured || sessionBusy })}
                  >
                    {sessionBusy ? "Continuing..." : "Continue to Dashboard"}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      void switchAccount();
                    }}
                    disabled={sessionBusy}
                    style={buildLoginButtonStyle({ tone: "secondary", disabled: sessionBusy })}
                  >
                    Use a Different Account
                  </button>
                </section>
              ) : (
                <form onSubmit={onSubmit} style={{ display: "grid", gap: 16 }}>
                  <label style={{ display: "grid", gap: 0 }}>
                    <span style={srOnlyStyle}>Email</span>
                    <div style={fieldWrapStyle}>
                      <FieldIcon kind="email" />
                      <input
                        type="email"
                        autoComplete="email"
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                        required
                        placeholder="Email address"
                        style={inputStyle}
                      />
                    </div>
                  </label>

                  <label style={{ display: "grid", gap: 0 }}>
                    <span style={srOnlyStyle}>Password</span>
                    <div style={fieldWrapStyle}>
                      <FieldIcon kind="password" />
                      <input
                        type="password"
                        autoComplete="current-password"
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        required
                        placeholder="Password"
                        style={inputStyle}
                      />
                    </div>
                  </label>

                  <button type="submit" disabled={!configured || busy} style={buildLoginButtonStyle({ disabled: !configured || busy })}>
                    {busy ? "Signing In..." : "Login"}
                  </button>

                  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <div style={{ flex: 1, height: 1, background: "rgba(123,134,151,0.22)" }} />
                    <span style={{ fontSize: 11, fontWeight: 800, letterSpacing: "0.18em", textTransform: "uppercase", color: "#8692a6" }}>
                      or
                    </span>
                    <div style={{ flex: 1, height: 1, background: "rgba(123,134,151,0.22)" }} />
                  </div>

                  <button
                    type="button"
                    onClick={() => {
                      void signInWithGoogle();
                    }}
                    disabled={!configured || busy}
                    style={buildLoginButtonStyle({ tone: "secondary", disabled: !configured || busy })}
                  >
                    <GoogleMarkIcon />
                    Continue with Google
                  </button>
                </form>
              )}

              <div style={{ display: "grid", gap: 10, textAlign: "center" }}>
                <p style={{ margin: 0, fontSize: 13, color: "#7b8697", fontWeight: 700 }}>
                  <Link href="/recover-account" style={footerLinkStyle}>
                    Forgot password?
                  </Link>{" "}
                  or{" "}
                  <Link href={nextPath ? `/signup?next=${encodeURIComponent(nextPath)}` : "/signup"} style={footerLinkStyle}>
                    Sign Up
                  </Link>
                </p>
                <p style={{ margin: 0, fontSize: 12, color: "#8a93a3" }}>
                  <Link href="/" style={footerLinkStyle}>
                    Back Home
                  </Link>{" "}
                  |{" "}
                  <Link href="/contact" style={footerLinkStyle}>
                    Contact Us
                  </Link>
                </p>
              </div>
            </div>
          </article>
        </section>
      </main>
    </>
  );
}
