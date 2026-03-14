import Link from "next/link";
import { useRouter } from "next/router";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { bootstrapOwnerOrg, checkChurchSlugAvailability } from "../lib/backendAuth";
import { useAuth } from "../lib/authContext";
import { normalizeChurchSlug } from "../lib/churchSlug";
import { clearHostToken, persistAuthToken, persistStreamContext } from "../utils/streamContext";

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
  const [confirmPassword, setConfirmPassword] = useState("");
  const [churchName, setChurchName] = useState("");
  const [churchSlug, setChurchSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [slugAvailabilityBusy, setSlugAvailabilityBusy] = useState(false);
  const [slugAvailable, setSlugAvailable] = useState<boolean | null>(null);
  const [slugSuggestions, setSlugSuggestions] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const slugCheckSeqRef = useRef(0);
  const nextPath = useMemo(() => {
    const raw = typeof router.query.next === "string" ? router.query.next : "";
    if (!raw.startsWith("/") || raw.startsWith("//")) return "";
    return raw;
  }, [router.query.next]);
  const inviteJoinFlow = nextPath.startsWith("/join?");
  const passwordMismatch = Boolean(confirmPassword) && password !== confirmPassword;
  const normalizedSlug = normalizeChurchSlug(churchSlug);

  useEffect(() => {
    if (inviteJoinFlow) return;
    if (slugTouched) return;
    setChurchSlug(normalizeChurchSlug(churchName));
  }, [churchName, inviteJoinFlow, slugTouched]);

  useEffect(() => {
    if (inviteJoinFlow) {
      setSlugAvailabilityBusy(false);
      setSlugAvailable(null);
      setSlugSuggestions([]);
      return;
    }
    if (!normalizedSlug) {
      setSlugAvailabilityBusy(false);
      setSlugAvailable(null);
      setSlugSuggestions([]);
      return;
    }
    setSlugAvailabilityBusy(true);
    const seq = slugCheckSeqRef.current + 1;
    slugCheckSeqRef.current = seq;
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const payload = await checkChurchSlugAvailability(normalizedSlug);
          if (slugCheckSeqRef.current !== seq) return;
          setSlugAvailable(Boolean(payload.available));
          const suggestions = (payload.suggestions || [])
            .map((value) => normalizeChurchSlug(value))
            .filter((value, index, rows) => Boolean(value) && value !== normalizedSlug && rows.indexOf(value) === index)
            .slice(0, 3);
          setSlugSuggestions(suggestions);
        } catch {
          if (slugCheckSeqRef.current !== seq) return;
          setSlugAvailable(null);
          setSlugSuggestions([]);
        } finally {
          if (slugCheckSeqRef.current === seq) {
            setSlugAvailabilityBusy(false);
          }
        }
      })();
    }, 260);
    return () => clearTimeout(timer);
  }, [inviteJoinFlow, normalizedSlug]);

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
    setErrorMsg(null);
    if (password !== confirmPassword) {
      setErrorMsg("Passwords do not match.");
      return;
    }
    const safeSlug = normalizeChurchSlug(churchSlug);
    if (!inviteJoinFlow) {
      if (!safeSlug) {
        setErrorMsg("Church slug is required.");
        return;
      }
      if (slugAvailable === false) {
        setErrorMsg("That church URL slug is already in use. Choose one of the suggestions.");
        return;
      }
    }
    setBusy(true);
    try {
      const authUser = await signup(email.trim(), password, name.trim());
      const token = await authUser.getIdToken(true);
      persistAuthToken(token);
      if (inviteJoinFlow && nextPath) {
        await router.replace(nextPath);
        return;
      }
      const created = await bootstrapOwnerOrg(token, {
        churchName: churchName.trim(),
        churchSlug: safeSlug,
        timezone: "America/Chicago",
        source: "ko",
        target: "en",
      });

      const serviceKey = created.services?.[0]?.serviceKey || "sun-11am";
      const org = created.org;
      clearHostToken();
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
      if (!inviteJoinFlow && err instanceof Error && err.message.toLowerCase().includes("slug")) {
        try {
          const payload = await checkChurchSlugAvailability(safeSlug);
          setSlugAvailable(Boolean(payload.available));
          const suggestions = (payload.suggestions || [])
            .map((value) => normalizeChurchSlug(value))
            .filter((value, index, rows) => Boolean(value) && value !== safeSlug && rows.indexOf(value) === index)
            .slice(0, 3);
          setSlugSuggestions(suggestions);
        } catch {
          // no-op; keep original error message.
        }
      }
      setErrorMsg(mapFirebaseError(err));
    } finally {
      setBusy(false);
    }
  };

  const slugBlocked = !inviteJoinFlow && (!normalizedSlug || slugAvailabilityBusy || slugAvailable === false);

  return (
    <main style={{ minHeight: "100vh", display: "grid", placeItems: "center", background: "#0b1220", color: "#f8fafc", padding: 18 }}>
      <section style={{ width: "100%", maxWidth: 470, borderRadius: 14, border: "1px solid rgba(255,255,255,0.14)", background: "rgba(255,255,255,0.04)", padding: 18 }}>
        <h1 style={{ marginTop: 0, marginBottom: 8 }}>Create Host Account</h1>
        <p style={{ marginTop: 0, marginBottom: 14, opacity: 0.82 }}>
          {inviteJoinFlow ? "Sign up to continue joining your invited church workspace." : "Sign up and create your church workspace."}
        </p>
        {!inviteJoinFlow ? (
          <p style={{ marginTop: -2, marginBottom: 14, fontSize: 13, color: "#86efac" }}>
            New church signup includes a 30-minute free host broadcast trial.
          </p>
        ) : null}

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
          <label style={{ display: "grid", gap: 4 }}>
            <span style={{ fontSize: 13, opacity: 0.84 }}>Confirm password</span>
            <input
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              autoComplete="new-password"
              minLength={6}
              required
              style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.24)", background: "#0f172a", color: "#fff", padding: "10px 12px" }}
            />
            {passwordMismatch ? <span style={{ fontSize: 12, color: "#fca5a5" }}>Passwords do not match.</span> : null}
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
                {slugAvailabilityBusy ? <span style={{ fontSize: 12, opacity: 0.76 }}>Checking slug availability…</span> : null}
                {!slugAvailabilityBusy && slugAvailable === true ? <span style={{ fontSize: 12, color: "#86efac" }}>Slug is available.</span> : null}
                {!slugAvailabilityBusy && slugAvailable === false ? (
                  <span style={{ fontSize: 12, color: "#fca5a5" }}>That slug is already taken.</span>
                ) : null}
                {!slugAvailabilityBusy && slugAvailable === false && slugSuggestions.length ? (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                    {slugSuggestions.map((suggestion) => (
                      <button
                        key={suggestion}
                        type="button"
                        onClick={() => {
                          setSlugTouched(true);
                          setChurchSlug(suggestion);
                          setErrorMsg(null);
                        }}
                        style={{
                          borderRadius: 999,
                          border: "1px solid rgba(147,197,253,0.5)",
                          background: "rgba(30,58,138,0.35)",
                          color: "#bfdbfe",
                          fontSize: 12,
                          padding: "4px 10px",
                          cursor: "pointer",
                        }}
                      >
                        Use {suggestion}
                      </button>
                    ))}
                  </div>
                ) : null}
              </label>
            </>
          ) : null}

          <button
            type="submit"
            disabled={!configured || busy || passwordMismatch || slugBlocked}
            style={{
              marginTop: 4,
              borderRadius: 10,
              border: "none",
              padding: "10px 12px",
              fontWeight: 700,
              background: "#22c55e",
              color: "#052e16",
              cursor: !configured || busy || passwordMismatch || slugBlocked ? "not-allowed" : "pointer",
              opacity: !configured || busy || passwordMismatch || slugBlocked ? 0.6 : 1,
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
