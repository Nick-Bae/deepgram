import Link from "next/link";
import { useRouter } from "next/router";
import { FormEvent, useEffect, useMemo, useState } from "react";

import { bootstrapOwnerOrg } from "../lib/backendAuth";
import { useAuth } from "../lib/authContext";
import { normalizeChurchSlug } from "../lib/churchSlug";
import { persistAuthToken, persistHostToken, persistStreamContext } from "../utils/streamContext";

function mapFirebaseError(err: unknown): string {
  const code = typeof err === "object" && err && "code" in err ? String((err as { code?: string }).code || "") : "";
  if (code === "auth/email-already-in-use") return "This email is already in use.";
  if (code === "auth/invalid-email") return "Email format is invalid.";
  if (code === "auth/weak-password") return "Password must be at least 6 characters.";
  if (err instanceof Error) return err.message;
  return "Sign up failed.";
}

export default function SignupPage() {
  const router = useRouter();
  const { signup, user, loading, configured, missingEnv } = useAuth();

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [churchName, setChurchName] = useState("");
  const [churchSlug, setChurchSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const nextPath = useMemo(() => {
    const raw = typeof router.query.next === "string" ? router.query.next : "";
    if (!raw.startsWith("/") || raw.startsWith("//")) return "";
    return raw;
  }, [router.query.next]);
  const inviteJoinFlow = nextPath.startsWith("/join?");

  useEffect(() => {
    if (inviteJoinFlow) return;
    if (slugTouched) return;
    setChurchSlug(normalizeChurchSlug(churchName));
  }, [churchName, inviteJoinFlow, slugTouched]);

  useEffect(() => {
    if (loading || !user || !configured || busy) return;
    if (nextPath && nextPath !== "/signup" && !nextPath.startsWith("/signup?")) {
      router.replace(nextPath);
      return;
    }
    router.replace("/onboarding/create-church");
  }, [busy, configured, loading, nextPath, router, user]);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErrorMsg(null);
    try {
      const authUser = await signup(email.trim(), password, name.trim());
      const token = await authUser.getIdToken(true);
      persistAuthToken(token);
      if (inviteJoinFlow && nextPath) {
        await router.replace(nextPath);
        return;
      }
      const safeSlug = normalizeChurchSlug(churchSlug);
      if (!safeSlug) throw new Error("Church slug is required.");
      const created = await bootstrapOwnerOrg(token, {
        churchName: churchName.trim(),
        churchSlug: safeSlug,
        timezone: "America/Chicago",
        source: "ko",
        target: "en",
      });

      const serviceKey = created.services?.[0]?.serviceKey || "sun-11am";
      const org = created.org;
      if (created.hostToken) persistHostToken(created.hostToken);
      persistStreamContext({
        orgId: org.orgId,
        serviceKey,
        churchSlug: org.slug,
      });

      const params = new URLSearchParams();
      params.set("orgId", org.orgId);
      params.set("serviceKey", serviceKey);
      await router.replace(`/host/c/${encodeURIComponent(org.slug)}/broadcast?${params.toString()}`);
    } catch (err) {
      setErrorMsg(mapFirebaseError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main style={{ minHeight: "100vh", display: "grid", placeItems: "center", background: "#0b1220", color: "#f8fafc", padding: 18 }}>
      <section style={{ width: "100%", maxWidth: 470, borderRadius: 14, border: "1px solid rgba(255,255,255,0.14)", background: "rgba(255,255,255,0.04)", padding: 18 }}>
        <h1 style={{ marginTop: 0, marginBottom: 8 }}>Create Host Account</h1>
        <p style={{ marginTop: 0, marginBottom: 14, opacity: 0.82 }}>
          {inviteJoinFlow ? "Sign up to continue joining your invited church workspace." : "Sign up and create your church workspace."}
        </p>

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
            <span style={{ fontSize: 13, opacity: 0.84 }}>Your name</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="name"
              required
              style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.24)", background: "#0f172a", color: "#fff", padding: "10px 12px" }}
            />
          </label>
          <label style={{ display: "grid", gap: 4 }}>
            <span style={{ fontSize: 13, opacity: 0.84 }}>Email</span>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              required
              style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.24)", background: "#0f172a", color: "#fff", padding: "10px 12px" }}
            />
          </label>
          <label style={{ display: "grid", gap: 4 }}>
            <span style={{ fontSize: 13, opacity: 0.84 }}>Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
              minLength={6}
              required
              style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.24)", background: "#0f172a", color: "#fff", padding: "10px 12px" }}
            />
          </label>
          {!inviteJoinFlow ? (
            <>
              <label style={{ display: "grid", gap: 4 }}>
                <span style={{ fontSize: 13, opacity: 0.84 }}>Church name</span>
                <input
                  value={churchName}
                  onChange={(e) => setChurchName(e.target.value)}
                  required
                  style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.24)", background: "#0f172a", color: "#fff", padding: "10px 12px" }}
                />
              </label>
              <label style={{ display: "grid", gap: 4 }}>
                <span style={{ fontSize: 13, opacity: 0.84 }}>Church URL slug</span>
                <input
                  value={churchSlug}
                  onChange={(e) => {
                    setSlugTouched(true);
                    setChurchSlug(normalizeChurchSlug(e.target.value));
                  }}
                  required
                  pattern="[a-z0-9-]+"
                  title="Use lowercase letters, numbers, and hyphens."
                  style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.24)", background: "#0f172a", color: "#fff", padding: "10px 12px" }}
                />
              </label>
            </>
          ) : null}

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
            {busy ? "Creating account..." : inviteJoinFlow ? "Sign up and continue" : "Sign up and create church"}
          </button>
        </form>

        <p style={{ fontSize: 13, marginBottom: 0, marginTop: 12, opacity: 0.85 }}>
          Already have an account?{" "}
          <Link href={nextPath ? `/login?next=${encodeURIComponent(nextPath)}` : "/login"} style={{ color: "#93c5fd" }}>
            Sign in
          </Link>
        </p>
      </section>
    </main>
  );
}
