import type { User } from "firebase/auth";
import Link from "next/link";
import { useRouter } from "next/router";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import StudioAccessLayout, {
  buildStudioButtonStyle,
  buildStudioNoticeStyle,
  studioChipStyle,
  studioFieldStyle,
  studioHelperTextStyle,
  studioLabelStyle,
  studioLabelTextStyle,
} from "../components/StudioAccessLayout";
import { bootstrapOwnerOrg, checkChurchSlugAvailability } from "../lib/backendAuth";
import { useAuth } from "../lib/authContext";
import { normalizeChurchSlug } from "../lib/churchSlug";
import { clearHostToken, persistAuthToken, persistStreamContext } from "../utils/streamContext";

function passwordStrength(pwd: string): "weak" | "ok" | "strong" | null {
  if (!pwd) return null;
  if (pwd.length < 8) return "weak";
  if (!/[^a-zA-Z]/.test(pwd)) return "weak";
  if (pwd.length >= 10) return "strong";
  return "ok";
}

function mapFirebaseError(err: unknown): string {
  const code = typeof err === "object" && err && "code" in err ? String((err as { code?: string }).code || "") : "";
  if (code === "auth/email-already-in-use") return "This email is already in use.";
  if (code === "auth/invalid-email") return "Email format is invalid.";
  if (code === "auth/weak-password") return "Password must be at least 8 characters.";
  if (code === "auth/popup-closed-by-user") return "Google sign-up was cancelled.";
  if (code === "auth/popup-blocked") return "Your browser blocked the Google sign-up popup. Please allow popups and try again.";
  if (code === "auth/operation-not-allowed") return "Google sign-up is not enabled for this Firebase project yet.";
  if (code === "auth/account-exists-with-different-credential") {
    return "An account already exists for this email with another sign-in method. Sign in with email and password first.";
  }
  if (err instanceof Error) return err.message;
  return "Sign up failed.";
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

export default function SignupPage() {
  const router = useRouter();
  const { loginWithGoogle, signup, user, loading, configured, missingEnv } = useAuth();

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
  const slugExample = "chicago-ark";

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

  const validateChurchSetup = (): string | null => {
    const safeSlug = normalizeChurchSlug(churchSlug);
    if (inviteJoinFlow) return safeSlug;
    if (!churchName.trim()) {
      setErrorMsg("Church name is required.");
      return null;
    }
    if (!safeSlug) {
      setErrorMsg("Church slug is required.");
      return null;
    }
    if (slugAvailable === false) {
      setErrorMsg("That church URL slug is already in use. Choose one of the suggestions.");
      return null;
    }
    return safeSlug;
  };

  const refreshSlugAvailability = async (safeSlug: string) => {
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
  };

  const completeSignup = async (authUser: User, safeSlug: string) => {
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
  };

  const runSignupFlow = async (authAction: () => Promise<User>) => {
    setErrorMsg(null);
    const safeSlug = validateChurchSetup();
    if (safeSlug === null) return;
    setBusy(true);
    try {
      const authUser = await authAction();
      await completeSignup(authUser, safeSlug);
    } catch (err) {
      if (!inviteJoinFlow && err instanceof Error && err.message.toLowerCase().includes("slug")) {
        await refreshSlugAvailability(safeSlug);
      }
      setErrorMsg(mapFirebaseError(err));
    } finally {
      setBusy(false);
    }
  };

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (password !== confirmPassword) {
      setErrorMsg("Passwords do not match.");
      return;
    }
    await runSignupFlow(() => signup(email.trim(), password, name.trim()));
  };

  const onGoogleSignup = async () => {
    await runSignupFlow(() => loginWithGoogle());
  };

  const slugBlocked = !inviteJoinFlow && (!normalizedSlug || slugAvailabilityBusy || slugAvailable === false);
  const googleSignupDisabled = !configured || busy || slugBlocked;

  return (
    <StudioAccessLayout
      pageTitle="Create Account | Worship"
      pageDescription="Create a host account and start a church workspace."
      panelEyebrow={inviteJoinFlow ? "Join Existing Church" : "Create Workspace"}
      panelTitle="Create Host Account"
      panelDescription={
        inviteJoinFlow
          ? "Sign up to continue joining your invited church workspace."
          : "Create your host account and set up your church workspace."
      }
      infoEyebrow="Setup Flow"
      infoTitle={inviteJoinFlow ? "Create an account, then finish joining the invited church." : "Start a church workspace with the first service already ready."}
      infoDescription={
        inviteJoinFlow
          ? "The invite flow keeps the join target so the new account can land inside the right church immediately after signup."
          : "New church signup creates the organization, the initial service slot, and the host context in one step."
      }
      infoItems={
        inviteJoinFlow
          ? [
              {
                title: "Join-safe signup",
                description: "The invite redirect is preserved so the new account continues into the church join flow automatically.",
              },
              {
                title: "One account, multiple churches",
                description: "You can later accept more invites without creating separate logins for each organization.",
              },
              {
                title: "Need help first?",
                description: "Support is available before you create the account if setup or access looks wrong.",
              },
            ]
          : [
              {
                title: "30-minute host trial",
                description: "New church signup includes the built-in host broadcast trial so you can test the full workflow immediately.",
              },
              {
                title: "Church URL reserved",
                description: "Slug checks happen during signup so the listener-facing URL is validated before the workspace is created.",
              },
              {
                title: "Ready for broadcast",
                description: "After signup, the first service is created and the host is routed straight into the dashboard.",
              },
            ]
      }
      headerActions={[
        { href: "/", label: "Back Home" },
        { href: "/contact", label: "Contact Us", accent: true },
      ]}
    >
      {!inviteJoinFlow ? <p style={buildStudioNoticeStyle("success")}>New church signup includes a 30-minute free host broadcast trial.</p> : null}

      {!configured ? (
        <div style={buildStudioNoticeStyle("error")}>
          Firebase config is missing in <code>frontend/.env.local</code>: {missingEnv.join(", ")}
        </div>
      ) : null}

      {errorMsg ? <p style={buildStudioNoticeStyle("error")}>Error: {errorMsg}</p> : null}

      <form onSubmit={onSubmit} style={{ display: "grid", gap: 12 }}>
        <label style={studioLabelStyle}>
          <span style={studioLabelTextStyle}>Your name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" required style={studioFieldStyle} />
        </label>
        <label style={studioLabelStyle}>
          <span style={studioLabelTextStyle}>Email</span>
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required style={studioFieldStyle} />
        </label>
        <label style={studioLabelStyle}>
          <span style={studioLabelTextStyle}>Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            minLength={8}
            required
            style={studioFieldStyle}
          />
          {(() => {
            const s = passwordStrength(password);
            if (!s) return null;
            const color = s === "weak" ? "#a33d51" : s === "ok" ? "#b07d29" : "#2f6d4f";
            const label = s === "weak" ? "Weak" : s === "ok" ? "OK" : "Strong";
            return <span style={{ ...studioHelperTextStyle, color }}>{label} password</span>;
          })()}
        </label>
        <label style={studioLabelStyle}>
          <span style={studioLabelTextStyle}>Confirm password</span>
          <input
            type="password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            autoComplete="new-password"
            minLength={8}
            required
            style={studioFieldStyle}
          />
          {passwordMismatch ? <span style={{ ...studioHelperTextStyle, color: "#a33d51" }}>Passwords do not match.</span> : null}
        </label>

        {!inviteJoinFlow ? (
          <>
            <label style={studioLabelStyle}>
              <span style={studioLabelTextStyle}>Church name</span>
              <input value={churchName} onChange={(e) => setChurchName(e.target.value)} required style={studioFieldStyle} />
            </label>
            <label style={studioLabelStyle}>
              <span style={studioLabelTextStyle}>Church URL slug</span>
              <input
                value={churchSlug}
                onChange={(e) => {
                  setSlugTouched(true);
                  setChurchSlug(normalizeChurchSlug(e.target.value));
                }}
                required
                pattern="[a-z0-9-]+"
                title="Use lowercase letters, numbers, and hyphens."
                placeholder={slugExample}
                style={studioFieldStyle}
              />
              <div
                style={{
                  borderRadius: 16,
                  border: "1px solid rgba(224,163,86,0.34)",
                  background: "linear-gradient(145deg, rgba(252,247,236,0.94), rgba(244,233,208,0.86))",
                  color: "#7a5525",
                  padding: "12px 14px",
                  display: "grid",
                  gap: 6,
                  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.5)",
                }}
              >
                <p style={{ margin: 0, fontSize: 12, fontWeight: 800, letterSpacing: "0.18em", textTransform: "uppercase" }}>
                  Important
                </p>
                <p style={{ margin: 0, fontSize: 13, lineHeight: 1.7 }}>
                  Keep the slug simple. Use English lowercase letters, numbers, and hyphens only.
                </p>
                <p style={{ margin: 0, fontSize: 13, lineHeight: 1.7 }}>
                  This becomes part of your public church URL and <strong>cannot be changed later</strong>.
                </p>
                <p style={{ margin: 0, fontSize: 13, lineHeight: 1.7 }}>
                  Example slug: <strong>{slugExample}</strong>
                </p>
                <div
                  style={{
                    borderRadius: 12,
                    border: "1px solid rgba(196,140,70,0.34)",
                    background: "rgba(255,255,255,0.56)",
                    padding: "10px 12px",
                    display: "grid",
                    gap: 6,
                  }}
                >
                  <p style={{ margin: 0, fontSize: 11, fontWeight: 800, letterSpacing: "0.18em", textTransform: "uppercase", color: "#8b6533" }}>
                    Preview URL
                  </p>
                  <p style={{ margin: 0, fontSize: 13, lineHeight: 1.7, wordBreak: "break-all" }}>
                    Example: <strong>/c/{slugExample}/s/sun-11am</strong>
                  </p>
                  {normalizedSlug ? (
                    <p style={{ margin: 0, fontSize: 13, lineHeight: 1.7, wordBreak: "break-all", color: "#5d4017" }}>
                      Yours: <strong>/c/{normalizedSlug}/s/sun-11am</strong>
                    </p>
                  ) : null}
                </div>
              </div>
              {slugAvailabilityBusy ? <span style={studioHelperTextStyle}>Checking slug availability...</span> : null}
              {!slugAvailabilityBusy && slugAvailable === true ? <span style={{ ...studioHelperTextStyle, color: "#2f6d4f" }}>Slug is available.</span> : null}
              {!slugAvailabilityBusy && slugAvailable === false ? <span style={{ ...studioHelperTextStyle, color: "#a33d51" }}>That slug is already taken.</span> : null}
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
                      style={{ ...studioChipStyle, cursor: "pointer" }}
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
          style={buildStudioButtonStyle({ disabled: !configured || busy || passwordMismatch || slugBlocked })}
        >
          {busy ? "Creating Account..." : inviteJoinFlow ? "Sign Up and Continue" : "Sign Up and Create Church"}
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
            void onGoogleSignup();
          }}
          disabled={googleSignupDisabled}
          style={{ ...buildStudioButtonStyle({ tone: "secondary", disabled: googleSignupDisabled }), gap: 10 }}
        >
          <GoogleMarkIcon />
          {inviteJoinFlow ? "Continue with Google" : "Continue with Google and Create Church"}
        </button>
      </form>

      <div style={{ display: "grid", gap: 8 }}>
        <p style={studioHelperTextStyle}>
          Already have an account?{" "}
          <Link href={nextPath ? `/login?next=${encodeURIComponent(nextPath)}` : "/login"} style={{ color: "#3f6093", fontWeight: 700 }}>
            Sign in
          </Link>
        </p>
        <p style={studioHelperTextStyle}>
          Need help getting set up?{" "}
          <Link href="/contact" style={{ color: "#3f6093", fontWeight: 700 }}>
            Contact us
          </Link>
        </p>
      </div>
    </StudioAccessLayout>
  );
}
