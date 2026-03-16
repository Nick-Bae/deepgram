import Link from "next/link";
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
import { useAuth } from "../lib/authContext";

function mapRecoveryError(err: unknown): string {
  const code = typeof err === "object" && err && "code" in err ? String((err as { code?: string }).code || "") : "";
  if (code === "auth/invalid-email") return "Please enter a valid email address.";
  if (code === "auth/too-many-requests") return "Too many reset attempts. Please try again later.";
  if (err instanceof Error && err.message) return err.message;
  return "Failed to start password reset.";
}

export default function RecoverAccountPage() {
  const { sendPasswordReset, configured, missingEnv } = useAuth();
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const accountRecoveryHref = useMemo(() => {
    const params = new URLSearchParams();
    params.set("topic", "account");
    params.set(
      "message",
      [
        "I need help finding the email used for my account.",
        "",
        "My name:",
        "Church name:",
        "What I remember:",
      ].join("\n"),
    );
    return `/contact?${params.toString()}`;
  }, []);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setErrorMsg(null);
    setNotice(null);
    try {
      await sendPasswordReset(email.trim());
      setNotice("If an account exists for that email, a password reset link has been sent.");
    } catch (err) {
      const code = typeof err === "object" && err && "code" in err ? String((err as { code?: string }).code || "") : "";
      if (code === "auth/user-not-found") {
        setNotice("If an account exists for that email, a password reset link has been sent.");
      } else {
        setErrorMsg(mapRecoveryError(err));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <StudioAccessLayout
      pageTitle="Recover Account | Worship"
      pageDescription="Reset your password or request help finding the email used for your host account."
      panelEyebrow="Account Recovery"
      panelTitle="Recover Access"
      panelDescription="Reset your password or request help if you cannot remember the email used for your account."
      infoEyebrow="Recovery Flow"
      infoTitle="Password reset is self-serve. Email lookup is handled more carefully."
      infoDescription="This app uses your email address as the login ID, so public account lookup is not exposed. Password reset can still be started immediately."
      infoItems={[
        {
          title: "Reset password by email",
          description: "Enter the email you used to sign in and we will send a password reset link if the account exists.",
        },
        {
          title: "Forgot the login email?",
          description: "Use the account-help path instead of public lookup. That avoids exposing whether an account exists.",
        },
        {
          title: "Church context helps recovery",
          description: "When requesting account help, include your name and church name so support can verify the request faster.",
        },
      ]}
      headerActions={[
        { href: "/login", label: "Back to Login" },
        { href: "/contact", label: "Contact Us", accent: true },
      ]}
    >
      {!configured ? (
        <div style={buildStudioNoticeStyle("error")}>
          Firebase config is missing in <code>frontend/.env.local</code>: {missingEnv.join(", ")}
        </div>
      ) : null}

      {errorMsg ? <p style={buildStudioNoticeStyle("error")}>Error: {errorMsg}</p> : null}
      {notice ? <p style={buildStudioNoticeStyle("success")}>{notice}</p> : null}

      <section style={{ display: "grid", gap: 14 }}>
        <form onSubmit={onSubmit} style={{ display: "grid", gap: 12 }}>
          <label style={studioLabelStyle}>
            <span style={studioLabelTextStyle}>Email used for login</span>
            <input
              type="email"
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
              style={studioFieldStyle}
            />
          </label>
          <p style={studioHelperTextStyle}>
            We show the same confirmation whether or not the email exists, so account status is not exposed publicly.
          </p>
          <button type="submit" disabled={!configured || busy} style={buildStudioButtonStyle({ disabled: !configured || busy })}>
            {busy ? "Sending Reset Link..." : "Send Password Reset Link"}
          </button>
        </form>

        <div style={studioReadOnlyCardStyle}>
          <p style={{ margin: 0, fontSize: 14, fontWeight: 800, color: "#22344c" }}>Can&apos;t remember the email used for login?</p>
          <p style={{ margin: "8px 0 0", fontSize: 14, lineHeight: 1.7, color: "#5d6d84" }}>
            Because the login ID is the email address, public account lookup is intentionally not available. Use support instead and include your name and church name.
          </p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 12 }}>
            <Link href={accountRecoveryHref} style={buildStudioButtonStyle({ tone: "secondary" })}>
              Request Help Finding Login Email
            </Link>
            <Link href="/contact" style={buildStudioButtonStyle({ tone: "secondary" })}>
              Open Contact Form
            </Link>
          </div>
        </div>
      </section>
    </StudioAccessLayout>
  );
}
