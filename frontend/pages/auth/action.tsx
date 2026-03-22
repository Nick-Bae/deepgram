import { applyActionCode, confirmPasswordReset } from "firebase/auth";
import Link from "next/link";
import { useRouter } from "next/router";
import { useEffect, useRef, useState } from "react";

import StudioAccessLayout, {
  buildStudioButtonStyle,
  buildStudioNoticeStyle,
  studioSubtleTextStyle,
} from "../../components/StudioAccessLayout";
import { getFirebaseClient } from "../../lib/firebaseClient";

type Status = "loading" | "success" | "error" | "reset_form" | "reset_success";

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

function mapPasswordResetError(err: unknown): string {
  const code =
    typeof err === "object" && err && "code" in err
      ? String((err as { code?: string }).code || "")
      : "";
  if (code === "auth/expired-action-code")
    return "This password reset link has expired. Please request a new one.";
  if (code === "auth/invalid-action-code")
    return "This password reset link has already been used or is invalid. Please request a new one.";
  if (code === "auth/weak-password")
    return "Password is too weak. Please use at least 6 characters.";
  if (code === "auth/user-disabled")
    return "This account has been disabled. Please contact support.";
  if (err instanceof Error && err.message) return err.message;
  return "Password reset failed. The link may be expired or already used.";
}

export default function AuthActionPage() {
  const router = useRouter();
  const [status, setStatus] = useState<Status>("loading");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [continueUrl, setContinueUrl] = useState<string>("/host");
  const oobCodeRef = useRef<string | null>(null);

  // Password reset form state
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [resetError, setResetError] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);

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

    if (modeStr === "resetPassword") {
      oobCodeRef.current = code;
      setStatus("reset_form");
      return;
    }

    setStatus("error");
    setErrorMsg("Unrecognized action. Please use the link from your email.");
  }, [router.isReady, router.query]);

  async function handlePasswordReset(e: React.FormEvent) {
    e.preventDefault();
    setResetError(null);

    if (newPassword.length < 6) {
      setResetError("Password must be at least 6 characters.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setResetError("Passwords do not match.");
      return;
    }

    const client = getFirebaseClient();
    if (!client || !oobCodeRef.current) {
      setResetError("App is not configured. Please contact support.");
      return;
    }

    setResetting(true);
    try {
      await confirmPasswordReset(client.auth, oobCodeRef.current, newPassword);
      setStatus("reset_success");
    } catch (err: unknown) {
      setResetError(mapPasswordResetError(err));
    } finally {
      setResetting(false);
    }
  }

  const isResetMode = status === "reset_form" || status === "reset_success";

  return (
    <StudioAccessLayout
      pageTitle={
        status === "success" || status === "reset_success"
          ? isResetMode ? "Password Reset | Worship" : "Email Verified | Worship"
          : status === "error"
            ? "Verification Failed | Worship"
            : status === "reset_form"
              ? "Reset Password | Worship"
              : "Verifying Email | Worship"
      }
      pageDescription={
        isResetMode
          ? "Reset your password for your Worship Translation account."
          : "Email address verification for your Worship Translation account."
      }
      panelEyebrow={isResetMode ? "Password Reset" : "Email Verification"}
      panelTitle={
        status === "loading"
          ? "Verifying your email…"
          : status === "success"
            ? "Email verified"
            : status === "reset_form"
              ? "Choose a new password"
              : status === "reset_success"
                ? "Password updated"
                : "Verification failed"
      }
      panelDescription={
        status === "loading"
          ? "Please wait while we confirm your email address."
          : status === "success"
            ? "Your email address has been confirmed."
            : status === "reset_form"
              ? "Enter a new password for your account."
              : status === "reset_success"
                ? "Your password has been successfully updated."
                : "We could not verify your email address."
      }
      infoEyebrow="Account Security"
      infoTitle={isResetMode ? "Secure your account." : "Email verification protects your account."}
      infoDescription={
        isResetMode
          ? "Choose a strong password to keep your Worship Translation account secure."
          : "Confirming your email address ensures you can receive account notices and keeps your workspace secure."
      }
      infoItems={
        isResetMode
          ? [
              {
                title: "Use a strong password",
                description: "At least 6 characters. Mix letters, numbers, and symbols for best security.",
              },
              {
                title: "Link is single-use",
                description: "Once your password is reset, this link will no longer work.",
              },
              {
                title: "Sign in after reset",
                description: "You will be redirected to sign in with your new password.",
              },
            ]
          : [
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
            ]
      }
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

      {status === "reset_form" && (
        <form onSubmit={handlePasswordReset} style={{ display: "grid", gap: 14 }}>
          <div style={{ display: "grid", gap: 6 }}>
            <label htmlFor="new-password" style={{ fontSize: 13, fontWeight: 600, color: "#0f172a" }}>
              New password
            </label>
            <input
              id="new-password"
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder="At least 6 characters"
              required
              autoFocus
              style={{
                padding: "10px 14px",
                borderRadius: 8,
                border: "1.5px solid #e2e8f0",
                fontSize: 15,
                outline: "none",
                width: "100%",
                boxSizing: "border-box",
              }}
            />
          </div>
          <div style={{ display: "grid", gap: 6 }}>
            <label htmlFor="confirm-password" style={{ fontSize: 13, fontWeight: 600, color: "#0f172a" }}>
              Confirm new password
            </label>
            <input
              id="confirm-password"
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder="Repeat your new password"
              required
              style={{
                padding: "10px 14px",
                borderRadius: 8,
                border: "1.5px solid #e2e8f0",
                fontSize: 15,
                outline: "none",
                width: "100%",
                boxSizing: "border-box",
              }}
            />
          </div>
          {resetError && (
            <p style={buildStudioNoticeStyle("error")}>{resetError}</p>
          )}
          <button
            type="submit"
            disabled={resetting}
            style={buildStudioButtonStyle({ disabled: resetting })}
          >
            {resetting ? "Updating…" : "Set new password"}
          </button>
        </form>
      )}

      {status === "reset_success" && (
        <div style={{ display: "grid", gap: 14 }}>
          <p style={buildStudioNoticeStyle("success")}>
            Your password has been updated. You can now sign in with your new password.
          </p>
          <Link href="/login" style={buildStudioButtonStyle()}>
            Sign In
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
