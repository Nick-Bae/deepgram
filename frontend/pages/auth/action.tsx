import { applyActionCode } from "firebase/auth";
import Link from "next/link";
import { useRouter } from "next/router";
import { useEffect, useState } from "react";

import StudioAccessLayout, {
  buildStudioButtonStyle,
  buildStudioNoticeStyle,
  studioSubtleTextStyle,
} from "../../components/StudioAccessLayout";
import { getFirebaseClient } from "../../lib/firebaseClient";

type Status = "loading" | "success" | "error";

function mapActionCodeError(err: unknown): string {
  const code =
    typeof err === "object" && err && "code" in err
      ? String((err as { code?: string }).code || "")
      : "";
  if (code === "auth/expired-action-code")
    return "This verification link has expired. Please request a new verification email from the app.";
  if (code === "auth/invalid-action-code")
    return "This verification link has already been used or is invalid. Please request a new one.";
  if (code === "auth/user-disabled")
    return "This account has been disabled. Please contact support.";
  if (code === "auth/user-not-found")
    return "No account was found for this link. Please sign up first.";
  if (err instanceof Error && err.message) return err.message;
  return "Verification failed. The link may be expired or already used.";
}

export default function AuthActionPage() {
  const router = useRouter();
  const [status, setStatus] = useState<Status>("loading");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [continueUrl, setContinueUrl] = useState<string>("/host");

  useEffect(() => {
    if (!router.isReady) return;

    const { mode, oobCode, continueUrl: rawContinueUrl } = router.query;
    const modeStr = typeof mode === "string" ? mode : null;
    const code = typeof oobCode === "string" ? oobCode : null;

    if (typeof rawContinueUrl === "string" && rawContinueUrl.startsWith("/")) {
      setContinueUrl(rawContinueUrl);
    }

    if (!code || !modeStr) {
      setStatus("error");
      setErrorMsg("Invalid or missing verification link. Please use the link from your email.");
      return;
    }

    if (modeStr === "verifyEmail") {
      const client = getFirebaseClient();
      if (!client) {
        setStatus("error");
        setErrorMsg("App is not configured. Please contact support.");
        return;
      }
      applyActionCode(client.auth, code)
        .then(() => setStatus("success"))
        .catch((err: unknown) => {
          setStatus("error");
          setErrorMsg(mapActionCodeError(err));
        });
      return;
    }

    setStatus("error");
    setErrorMsg("Unrecognized action. Please use the link from your email.");
  }, [router.isReady, router.query]);

  return (
    <StudioAccessLayout
      pageTitle={
        status === "success"
          ? "Email Verified | Worship"
          : status === "error"
            ? "Verification Failed | Worship"
            : "Verifying Email | Worship"
      }
      pageDescription="Email address verification for your Worship Translation account."
      panelEyebrow="Email Verification"
      panelTitle={
        status === "loading"
          ? "Verifying your email…"
          : status === "success"
            ? "Email verified"
            : "Verification failed"
      }
      panelDescription={
        status === "loading"
          ? "Please wait while we confirm your email address."
          : status === "success"
            ? "Your email address has been confirmed."
            : "We could not verify your email address."
      }
      infoEyebrow="Account Security"
      infoTitle="Email verification protects your account."
      infoDescription="Confirming your email address ensures you can receive account notices and keeps your workspace secure."
      infoItems={[
        {
          title: "One-click verification",
          description:
            "Clicking the link in your email is all it takes — no code entry required.",
        },
        {
          title: "Link expires after use",
          description:
            "Each verification link is single-use and expires after 24 hours for security.",
        },
        {
          title: "Need a new link?",
          description:
            "If your link expired, return to the app and click 'Resend verification email' from the host page.",
        },
      ]}
      headerActions={[
        { href: "/login", label: "Sign In" },
        { href: "/contact", label: "Contact Us", accent: true },
      ]}
    >
      {status === "loading" && (
        <p style={{ ...studioSubtleTextStyle, margin: 0 }}>
          Verifying your email address…
        </p>
      )}

      {status === "success" && (
        <div style={{ display: "grid", gap: 14 }}>
          <p style={buildStudioNoticeStyle("success")}>
            Your email address has been verified. You can now return to the app.
          </p>
          <Link href={continueUrl} style={buildStudioButtonStyle()}>
            Go to Dashboard
          </Link>
        </div>
      )}

      {status === "error" && (
        <div style={{ display: "grid", gap: 14 }}>
          <p style={buildStudioNoticeStyle("error")}>{errorMsg}</p>
          <Link href="/login" style={buildStudioButtonStyle({ tone: "secondary" })}>
            Return to Sign In
          </Link>
          <Link href="/contact" style={buildStudioButtonStyle({ tone: "secondary" })}>
            Contact Support
          </Link>
        </div>
      )}
    </StudioAccessLayout>
  );
}
