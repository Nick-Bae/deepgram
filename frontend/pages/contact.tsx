import Head from "next/head";
import Link from "next/link";
import Script from "next/script";
import { useEffect, useRef, useState } from "react";

import { fetchAuthMe } from "../lib/backendAuth";
import { useAuth } from "../lib/authContext";
import { pickPreferredMembership } from "../lib/dashboardRoute";

type ContactTopic = "billing" | "setup" | "translation" | "bug" | "sales" | "account" | "other";

const TOPIC_OPTIONS: Array<{ value: ContactTopic; label: string }> = [
  { value: "billing", label: "Billing or subscription" },
  { value: "setup", label: "Church setup or onboarding" },
  { value: "translation", label: "Translation quality" },
  { value: "bug", label: "Bug report" },
  { value: "sales", label: "Sales or demo" },
  { value: "account", label: "Account access" },
  { value: "other", label: "Other" },
];

const supportEmail = (process.env.NEXT_PUBLIC_SUPPORT_EMAIL || "").trim();
const turnstileSiteKey = (process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY || "").trim();

type TurnstileApi = {
  render: (container: HTMLElement, options: Record<string, unknown>) => string;
  reset: (widgetId?: string) => void;
  remove: (widgetId?: string) => void;
};

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

export default function ContactPage() {
  const { user, loading, configured, getIdToken } = useAuth();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [organization, setOrganization] = useState("");
  const [topic, setTopic] = useState<ContactTopic>("billing");
  const [message, setMessage] = useState("");
  const [website, setWebsite] = useState("");
  const [turnstileReady, setTurnstileReady] = useState(false);
  const [turnstileToken, setTurnstileToken] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const widgetHostRef = useRef<HTMLDivElement | null>(null);
  const widgetIdRef = useRef<string | null>(null);

  const needsTurnstile = Boolean(turnstileSiteKey) && !user;

  useEffect(() => {
    if (!user) return;
    if (!name && user.displayName?.trim()) setName(user.displayName.trim());
    if (!email && user.email?.trim()) setEmail(user.email.trim());
  }, [email, name, user]);

  useEffect(() => {
    if (loading || !user || !configured) return;
    let cancelled = false;
    void (async () => {
      try {
        const idToken = await getIdToken(false);
        if (!idToken || cancelled) return;
        const me = await fetchAuthMe(idToken);
        const membership = pickPreferredMembership(me) || me.memberships?.[0];
        if (!cancelled && membership?.name && !organization) {
          setOrganization(membership.name);
        }
      } catch {
        // Leave the field editable if prefill fails.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [configured, getIdToken, loading, organization, user]);

  useEffect(() => {
    if (!needsTurnstile || !turnstileReady || !widgetHostRef.current || widgetIdRef.current || !window.turnstile) return;
    widgetIdRef.current = window.turnstile.render(widgetHostRef.current, {
      sitekey: turnstileSiteKey,
      callback: (token: unknown) => {
        setTurnstileToken(typeof token === "string" ? token : "");
      },
      "expired-callback": () => {
        setTurnstileToken("");
      },
      "error-callback": () => {
        setTurnstileToken("");
      },
      theme: "light",
    });
  }, [needsTurnstile, turnstileReady]);

  useEffect(() => {
    return () => {
      if (widgetIdRef.current && window.turnstile) {
        window.turnstile.remove(widgetIdRef.current);
      }
    };
  }, []);

  const onSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setErrorMsg(null);
    setNotice(null);

    if (message.trim().length < 20) {
      setBusy(false);
      setErrorMsg("Please enter a more detailed message.");
      return;
    }
    if (needsTurnstile && !turnstileToken) {
      setBusy(false);
      setErrorMsg("Please complete the spam protection check.");
      return;
    }

    try {
      const idToken = user && configured ? await getIdToken(false) : null;
      const res = await fetch("/api/contact", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          name,
          email,
          organization,
          topic,
          message,
          website,
          turnstileToken,
          idToken,
        }),
      });
      const payload = (await res.json()) as { ok?: boolean; error?: string };
      if (!res.ok || !payload.ok) {
        throw new Error(payload.error || "Failed to submit contact request.");
      }

      setNotice("Your message was sent. Support will follow up soon.");
      setMessage("");
      setTopic("billing");
      setWebsite("");
      setTurnstileToken("");
      if (widgetIdRef.current && window.turnstile) {
        window.turnstile.reset(widgetIdRef.current);
      }
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Failed to submit contact request.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Head>
        <title>Contact Us | Worship</title>
        <meta
          name="description"
          content="Contact Worship support for billing, onboarding, account access, or translation issues."
        />
      </Head>
      {needsTurnstile ? (
        <Script
          src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"
          strategy="afterInteractive"
          onLoad={() => setTurnstileReady(true)}
        />
      ) : null}
      <main
        style={{
          minHeight: "100vh",
          background:
            "radial-gradient(circle at 8% 10%, rgba(154,179,219,0.28), transparent 28%), radial-gradient(circle at 90% 12%, rgba(223,190,131,0.18), transparent 24%), linear-gradient(180deg, #edf1f6 0%, #dfe6ef 54%, #d3dce7 100%)",
          color: "#10213a",
          padding: "28px 16px 40px",
          fontFamily: "'Avenir Next', 'Segoe UI', sans-serif",
        }}
      >
        <section style={{ maxWidth: 1120, margin: "0 auto", display: "grid", gap: 22 }}>
          <header
            style={{
              borderRadius: 30,
              border: "1px solid rgba(255,255,255,0.14)",
              background: "linear-gradient(135deg, #566983 0%, #323d50 38%, #171d27 100%)",
              boxShadow: "0 28px 56px rgba(32,42,58,0.24), inset 0 1px 0 rgba(255,255,255,0.08)",
              padding: "20px 22px",
              color: "#f8fafc",
              display: "flex",
              flexWrap: "wrap",
              justifyContent: "space-between",
              alignItems: "center",
              gap: 16,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
              <div
                style={{
                  width: 56,
                  height: 56,
                  borderRadius: 18,
                  background: "linear-gradient(145deg, rgba(255,255,255,0.12), rgba(255,255,255,0.04))",
                  border: "1px solid rgba(255,255,255,0.1)",
                  color: "#ffb703",
                  display: "grid",
                  placeItems: "center",
                  fontSize: 20,
                  fontWeight: 900,
                  fontStyle: "italic",
                }}
              >
                W
              </div>
              <div>
                <p style={{ margin: 0, fontSize: 11, letterSpacing: "0.34em", textTransform: "uppercase", color: "#b9c6da" }}>
                  Worship Support
                </p>
                <h1 style={{ margin: "8px 0 0", fontSize: "clamp(28px, 4vw, 36px)", lineHeight: 1, letterSpacing: "-0.05em" }}>
                  Contact Us
                </h1>
              </div>
            </div>

            <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
              <Link
                href="/"
                style={{
                  borderRadius: 999,
                  padding: "12px 18px",
                  background: "rgba(255,255,255,0.08)",
                  border: "1px solid rgba(255,255,255,0.14)",
                  color: "#d9e3f2",
                  fontWeight: 700,
                }}
              >
                Back Home
              </Link>
              <Link
                href="/login"
                style={{
                  borderRadius: 999,
                  padding: "12px 18px",
                  background: "linear-gradient(145deg, #7fa5db, #4f73aa)",
                  color: "#f8fafc",
                  fontWeight: 800,
                  boxShadow: "0 14px 30px rgba(79,115,170,0.28)",
                }}
              >
                Host Login
              </Link>
            </div>
          </header>

          <section
            style={{
              display: "grid",
              gap: 18,
              gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
              alignItems: "start",
            }}
          >
            <aside
              style={{
                borderRadius: 30,
                border: "1px solid rgba(255,255,255,0.82)",
                background: "linear-gradient(145deg, rgba(248,251,254,0.96), rgba(228,235,244,0.9))",
                boxShadow: "24px 24px 48px rgba(122,138,163,0.14), -16px -16px 30px rgba(255,255,255,0.76)",
                padding: 22,
                display: "grid",
                gap: 16,
              }}
            >
              <div
                style={{
                  borderRadius: 22,
                  padding: "16px 18px",
                  background: "linear-gradient(145deg, rgba(225,236,250,0.96), rgba(212,225,243,0.9))",
                  border: "1px solid rgba(204,218,238,0.92)",
                }}
              >
                <p style={{ margin: 0, fontSize: 11, color: "#7386a2", letterSpacing: "0.24em", textTransform: "uppercase", fontWeight: 800 }}>
                  Support Topics
                </p>
                <h2 style={{ margin: "10px 0 0", fontSize: 30, lineHeight: 1.05, letterSpacing: "-0.05em" }}>
                  Billing, onboarding, bugs, and church setup.
                </h2>
              </div>

              <p style={{ margin: 0, fontSize: 15, lineHeight: 1.8, color: "#50627c" }}>
                Use this form if you need help with billing, access, live translation quality, or getting a church workspace set up correctly.
              </p>

              <div style={{ display: "grid", gap: 12 }}>
                {TOPIC_OPTIONS.map((entry) => (
                  <div
                    key={entry.value}
                    style={{
                      borderRadius: 18,
                      background: "rgba(255,255,255,0.56)",
                      border: "1px solid rgba(219,227,238,0.9)",
                      padding: "14px 16px",
                    }}
                  >
                    <p style={{ margin: 0, fontSize: 15, fontWeight: 800, color: "#22344c" }}>{entry.label}</p>
                  </div>
                ))}
              </div>

              <div
                style={{
                  borderRadius: 22,
                  padding: "16px 18px",
                  background: "rgba(255,255,255,0.58)",
                  border: "1px solid rgba(219,227,238,0.9)",
                  display: "grid",
                  gap: 8,
                }}
              >
                <p style={{ margin: 0, fontSize: 15, fontWeight: 800, color: "#22344c" }}>Spam protection</p>
                <p style={{ margin: 0, fontSize: 14, lineHeight: 1.7, color: "#5d6d84" }}>
                  Anonymous requests are throttled and screened before delivery. Logged-in users can submit with less friction.
                </p>
                {supportEmail ? (
                  <p style={{ margin: 0, fontSize: 14, lineHeight: 1.7, color: "#5d6d84" }}>
                    Fallback email: <a href={`mailto:${supportEmail}`} style={{ color: "#3f6093", fontWeight: 700 }}>{supportEmail}</a>
                  </p>
                ) : null}
              </div>
            </aside>

            <article
              style={{
                borderRadius: 30,
                border: "1px solid rgba(255,255,255,0.82)",
                background: "linear-gradient(145deg, rgba(248,251,254,0.96), rgba(228,235,244,0.9))",
                boxShadow: "24px 24px 48px rgba(122,138,163,0.14), -16px -16px 30px rgba(255,255,255,0.76)",
                padding: 22,
                display: "grid",
                gap: 16,
              }}
            >
              <div
                style={{
                  borderRadius: 22,
                  padding: "16px 18px",
                  background: "linear-gradient(145deg, rgba(246,239,227,0.96), rgba(236,227,209,0.9))",
                  border: "1px solid rgba(225,214,191,0.92)",
                }}
              >
                <p style={{ margin: 0, fontSize: 11, color: "#7386a2", letterSpacing: "0.24em", textTransform: "uppercase", fontWeight: 800 }}>
                  Submit Request
                </p>
                <h2 style={{ margin: "10px 0 0", fontSize: 30, lineHeight: 1.05, letterSpacing: "-0.05em" }}>
                  Tell us what is blocked.
                </h2>
              </div>

              {errorMsg ? (
                <p style={{ margin: 0, borderRadius: 14, padding: "12px 14px", background: "rgba(188,95,111,0.12)", color: "#a33d51", fontSize: 14 }}>
                  {errorMsg}
                </p>
              ) : null}
              {notice ? (
                <p style={{ margin: 0, borderRadius: 14, padding: "12px 14px", background: "rgba(91,179,130,0.12)", color: "#2f6d4f", fontSize: 14 }}>
                  {notice}
                </p>
              ) : null}

              <form onSubmit={onSubmit} style={{ display: "grid", gap: 14 }}>
                <div style={{ display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" }}>
                  <label style={{ display: "grid", gap: 6 }}>
                    <span style={{ fontSize: 13, color: "#5f6f86" }}>Name</span>
                    <input
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      autoComplete="name"
                      required
                      style={{ borderRadius: 12, border: "1px solid rgba(189,200,217,0.92)", background: "rgba(247,250,253,0.86)", color: "#20324a", padding: "11px 12px" }}
                    />
                  </label>

                  <label style={{ display: "grid", gap: 6 }}>
                    <span style={{ fontSize: 13, color: "#5f6f86" }}>Email</span>
                    <input
                      type="email"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      autoComplete="email"
                      required
                      style={{ borderRadius: 12, border: "1px solid rgba(189,200,217,0.92)", background: "rgba(247,250,253,0.86)", color: "#20324a", padding: "11px 12px" }}
                    />
                  </label>
                </div>

                <div style={{ display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" }}>
                  <label style={{ display: "grid", gap: 6 }}>
                    <span style={{ fontSize: 13, color: "#5f6f86" }}>Organization</span>
                    <input
                      value={organization}
                      onChange={(e) => setOrganization(e.target.value)}
                      autoComplete="organization"
                      placeholder="Church or organization name"
                      style={{ borderRadius: 12, border: "1px solid rgba(189,200,217,0.92)", background: "rgba(247,250,253,0.86)", color: "#20324a", padding: "11px 12px" }}
                    />
                  </label>

                  <label style={{ display: "grid", gap: 6 }}>
                    <span style={{ fontSize: 13, color: "#5f6f86" }}>Topic</span>
                    <select
                      value={topic}
                      onChange={(e) => setTopic(e.target.value as ContactTopic)}
                      style={{ borderRadius: 12, border: "1px solid rgba(189,200,217,0.92)", background: "rgba(247,250,253,0.86)", color: "#20324a", padding: "11px 12px" }}
                    >
                      {TOPIC_OPTIONS.map((entry) => (
                        <option key={entry.value} value={entry.value}>
                          {entry.label}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>

                <label style={{ display: "grid", gap: 6 }}>
                  <span style={{ fontSize: 13, color: "#5f6f86" }}>Message</span>
                  <textarea
                    value={message}
                    onChange={(e) => setMessage(e.target.value)}
                    required
                    minLength={20}
                    rows={8}
                    placeholder="Describe the issue, what you expected, and which church or page is affected."
                    style={{ borderRadius: 16, border: "1px solid rgba(189,200,217,0.92)", background: "rgba(247,250,253,0.86)", color: "#20324a", padding: "12px 14px", resize: "vertical" }}
                  />
                </label>

                <label style={{ display: "none" }}>
                  Website
                  <input
                    value={website}
                    onChange={(e) => setWebsite(e.target.value)}
                    autoComplete="off"
                    tabIndex={-1}
                  />
                </label>

                {needsTurnstile ? (
                  <div style={{ display: "grid", gap: 8 }}>
                    <span style={{ fontSize: 13, color: "#5f6f86" }}>Spam protection</span>
                    <div
                      ref={widgetHostRef}
                      style={{
                        minHeight: 66,
                        borderRadius: 14,
                        border: "1px dashed rgba(189,200,217,0.92)",
                        background: "rgba(247,250,253,0.72)",
                        padding: 10,
                      }}
                    />
                  </div>
                ) : null}

                <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
                  <p style={{ margin: 0, fontSize: 13, color: "#6b7b92" }}>
                    {supportEmail ? `Need a fallback? Email ${supportEmail}.` : "Support replies are handled through the configured support inbox."}
                  </p>
                  <button
                    type="submit"
                    disabled={busy}
                    style={{
                      borderRadius: 999,
                      border: "1px solid rgba(79,115,170,0.28)",
                      background: "linear-gradient(145deg, #7fa5db, #4f73aa)",
                      color: "#f8fafc",
                      fontWeight: 800,
                      padding: "13px 22px",
                      cursor: busy ? "not-allowed" : "pointer",
                      opacity: busy ? 0.7 : 1,
                      boxShadow: "0 14px 30px rgba(79,115,170,0.22)",
                    }}
                  >
                    {busy ? "Sending..." : "Send Message"}
                  </button>
                </div>
              </form>
            </article>
          </section>
        </section>
      </main>
    </>
  );
}
