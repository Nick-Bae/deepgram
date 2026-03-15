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
            minLength={6}
            required
            style={studioFieldStyle}
          />
        </label>
        <label style={studioLabelStyle}>
          <span style={studioLabelTextStyle}>Confirm password</span>
          <input
            type="password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            autoComplete="new-password"
            minLength={6}
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
                style={studioFieldStyle}
              />
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
