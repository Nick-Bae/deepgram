import Link from "next/link";
import { useRouter } from "next/router";
import { FormEvent, useEffect, useMemo, useState } from "react";

import { fetchAuthMe } from "../lib/backendAuth";
import { useAuth } from "../lib/authContext";
import { persistAuthToken, persistHostToken } from "../utils/streamContext";

function mapFirebaseError(err: unknown): string {
  const code = typeof err === "object" && err && "code" in err ? String((err as { code?: string }).code || "") : "";
  if (code === "auth/invalid-credential") return "Invalid email or password.";
  if (code === "auth/user-not-found") return "No user found for this email.";
  if (code === "auth/wrong-password") return "Invalid email or password.";
  if (code === "auth/too-many-requests") return "Too many attempts. Try again later.";
  if (err instanceof Error) return err.message;
  return "Login failed.";
}

export default function LoginPage() {
  const router = useRouter();
  const { login, getIdToken, user, loading, configured, missingEnv } = useAuth();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const nextPath = useMemo(() => {
    const raw = typeof router.query.next === "string" ? router.query.next : "";
    if (!raw.startsWith("/") || raw.startsWith("//")) return "";
    return raw;
  }, [router.query.next]);

  const redirectFromMembership = async (idToken: string) => {
    const me = await fetchAuthMe(idToken);
    const preferredOrgId = (me.currentOrgId || "").trim();
    const primary = me.memberships.find((row) => row.orgId === preferredOrgId) || me.memberships[0];
    if (nextPath && nextPath !== "/login" && !nextPath.startsWith("/login?")) {
      await router.replace(nextPath);
      return;
    }
    if (!primary) {
      await router.replace("/onboarding/create-church");
      return;
    }
    if (primary.hostToken) persistHostToken(primary.hostToken);
    const params = new URLSearchParams();
    params.set("orgId", primary.orgId);
    await router.replace(`/host/c/${encodeURIComponent(primary.slug)}?${params.toString()}`);
  };

  useEffect(() => {
    if (loading || !user || !configured) return;
    let cancelled = false;
    const run = async () => {
      try {
        const token = await getIdToken();
        if (!token || cancelled) return;
        persistAuthToken(token);
        await redirectFromMembership(token);
      } catch (err) {
        if (!cancelled) setErrorMsg(mapFirebaseError(err));
      }
    };
    run();
    return () => {
      cancelled = true;
    };
  }, [configured, getIdToken, loading, user]); // eslint-disable-line react-hooks/exhaustive-deps

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErrorMsg(null);
    try {
      const authUser = await login(email.trim(), password);
      const token = await authUser.getIdToken(true);
      persistAuthToken(token);
      await redirectFromMembership(token);
    } catch (err) {
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

        <p style={{ fontSize: 13, marginBottom: 0, marginTop: 12, opacity: 0.85 }}>
          Need an account?{" "}
          <Link href={nextPath ? `/signup?next=${encodeURIComponent(nextPath)}` : "/signup"} style={{ color: "#93c5fd" }}>
            Create one
          </Link>
        </p>
      </section>
    </main>
  );
}
