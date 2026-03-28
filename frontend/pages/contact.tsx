import Head from "next/head";
import Link from "next/link";
import { useRouter } from "next/router";
import Script from "next/script";
import { useEffect, useRef, useState } from "react";

import { fetchAuthMe, type OrgMembership } from "../lib/backendAuth";
import { useAuth } from "../lib/authContext";
import { buildDashboardHref, persistDashboardContext, pickPreferredMembership } from "../lib/dashboardRoute";

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

const turnstileSiteKey = (process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY || "").trim();

function decodeEmail(codes: number[]): string {
  return codes.map((code) => String.fromCharCode(code)).join("");
}

function buildSupportEmail(): string {
  return decodeEmail([115, 117, 112, 112, 111, 114, 116, 64, 119, 111, 114, 115, 104, 105, 112, 116, 114, 97, 110, 115, 108, 97, 116, 105, 111, 110, 46, 99, 111, 109]);
}

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
  const router = useRouter();
  const { user, loading, configured, getIdToken } = useAuth();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [organization, setOrganization] = useState("");
  const [topic, setTopic] = useState<ContactTopic>("billing");
  const [message, setMessage] = useState("");
  const [website, setWebsite] = useState("");
  const [turnstileReady, setTurnstileReady] = useState(false);
  const [turnstileToken, setTurnstileToken] = useState("");
  const [preferredMembership, setPreferredMembership] = useState<OrgMembership | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [directEmail, setDirectEmail] = useState("");
  const [directEmailMsg, setDirectEmailMsg] = useState<string | null>(null);
  const [copiedDirectEmail, setCopiedDirectEmail] = useState(false);
  const [unconfiguredFallback, setUnconfiguredFallback] = useState(false);
  const [busy, setBusy] = useState(false);
  const widgetHostRef = useRef<HTMLDivElement | null>(null);
  const widgetIdRef = useRef<string | null>(null);
  const queryPrefillRef = useRef(false);

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
        if (!cancelled) {
          setPreferredMembership(membership || null);
        }
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
    if (!router.isReady || queryPrefillRef.current) return;
    const topicQuery = typeof router.query.topic === "string" ? router.query.topic.trim().toLowerCase() : "";
    const organizationQuery = typeof router.query.organization === "string" ? router.query.organization.trim().slice(0, 160) : "";
    const messageQuery = typeof router.query.message === "string" ? router.query.message.replace(/\r\n/g, "\n").trim().slice(0, 4000) : "";

    if (topicQuery && TOPIC_OPTIONS.some((entry) => entry.value === topicQuery)) {
      setTopic(topicQuery as ContactTopic);
    }
    if (organizationQuery && !organization) {
      setOrganization(organizationQuery);
    }
    if (messageQuery && !message) {
      setMessage(messageQuery);
    }
    queryPrefillRef.current = true;
  }, [message, organization, router.isReady, router.query.message, router.query.organization, router.query.topic]);

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

  const revealSupportEmail = () => {
    setDirectEmailMsg(null);
    setCopiedDirectEmail(false);
    if (needsTurnstile && !turnstileToken) {
      setDirectEmailMsg("Complete the spam protection check to reveal the support email.");
      return;
    }
    setDirectEmail(buildSupportEmail());
  };

  const copySupportEmail = async () => {
    if (!directEmail || typeof navigator === "undefined" || !navigator.clipboard?.writeText) {
      setDirectEmailMsg("Copy is not available in this browser. Use the email link instead.");
      return;
    }
    try {
      await navigator.clipboard.writeText(directEmail);
      setCopiedDirectEmail(true);
      setDirectEmailMsg("Support email copied.");
    } catch {
      setDirectEmailMsg("Copy failed. Use the email link instead.");
    }
  };

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
      const msg = err instanceof Error ? err.message : "Failed to submit contact request.";
      if (msg === "Support inbox is not configured yet.") {
        setUnconfiguredFallback(true);
      } else {
        setErrorMsg(msg);
      }
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
          background: "#ede5d8",
          color: "#2e2a28",
          padding: "28px 16px 40px",
          fontFamily: "'Avenir Next', 'Segoe UI', sans-serif",
          position: "relative",
        }}
      >
        {/* Bokeh background */}
        <div aria-hidden="true" style={{ position: "fixed", inset: 0, zIndex: 0, overflow: "hidden", pointerEvents: "none" }}>
          <div style={{ position: "absolute", left: "-8%", top: "-6%", width: 700, height: 700, borderRadius: "50%", background: "radial-gradient(circle, rgba(214,172,88,0.72) 0%, transparent 68%)", filter: "blur(80px)" }} />
          <div style={{ position: "absolute", right: "-6%", top: "4%", width: 620, height: 620, borderRadius: "50%", background: "radial-gradient(circle, rgba(170,158,248,0.62) 0%, transparent 68%)", filter: "blur(80px)" }} />
          <div style={{ position: "absolute", right: "8%", bottom: "-8%", width: 680, height: 580, borderRadius: "50%", background: "radial-gradient(circle, rgba(224,158,112,0.58) 0%, transparent 68%)", filter: "blur(90px)" }} />
          <div style={{ position: "absolute", left: "5%", bottom: "10%", width: 540, height: 520, borderRadius: "50%", background: "radial-gradient(circle, rgba(210,168,178,0.50) 0%, transparent 68%)", filter: "blur(70px)" }} />
          <div style={{ position: "absolute", left: "28%", top: "30%", width: 800, height: 600, borderRadius: "50%", background: "radial-gradient(circle, rgba(255,250,238,0.88) 0%, transparent 68%)", filter: "blur(100px)" }} />
          <div style={{ position: "absolute", right: "22%", top: "52%", width: 440, height: 400, borderRadius: "50%", background: "radial-gradient(circle, rgba(130,190,200,0.38) 0%, transparent 68%)", filter: "blur(70px)" }} />
        </div>
        <section style={{ position: "relative", zIndex: 1, maxWidth: 1120, margin: "0 auto", display: "grid", gap: 22 }}>
          <header
            style={{
              borderRadius: 30,
              border: "1px solid rgba(255,255,255,0.68)",
              background: "linear-gradient(180deg, rgba(255,255,255,0.18), rgba(255,255,255,0.10))",
              boxShadow: "0 28px 64px rgba(122,101,79,0.18), inset 0 1px 0 rgba(255,255,255,0.90)",
              backdropFilter: "blur(40px)",
              WebkitBackdropFilter: "blur(40px)",
              padding: "14px 22px",
              display: "flex",
              flexWrap: "wrap",
              justifyContent: "space-between",
              alignItems: "center",
              gap: 16,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
              <div
                style={{
                  width: 46,
                  height: 46,
                  borderRadius: 14,
                  background: "#2d3650",
                  color: "#f1d35c",
                  display: "grid",
                  placeItems: "center",
                  fontSize: 18,
                  fontWeight: 900,
                  fontStyle: "italic",
                  flexShrink: 0,
                }}
              >
                W
              </div>
              <div>
                <p style={{ margin: 0, fontSize: 10, letterSpacing: "0.38em", textTransform: "uppercase", color: "#7d746c" }}>
                  Worship Support
                </p>
                <h1 style={{ margin: "3px 0 0", fontSize: 17, fontWeight: 600, letterSpacing: "-0.02em", color: "#1d1917", lineHeight: 1 }}>
                  Contact Us
                </h1>
              </div>
            </div>

            <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
              <Link
                href="/"
                style={{
                  borderRadius: 999,
                  padding: "9px 18px",
                  background: "rgba(255,255,255,0.50)",
                  border: "1px solid rgba(120,98,78,0.16)",
                  color: "#4a4038",
                  fontWeight: 700,
                  fontSize: 13,
                }}
              >
                Back Home
              </Link>
              {user ? (
                <button
                  type="button"
                  onClick={() => {
                    if (preferredMembership) {
                      persistDashboardContext(preferredMembership);
                      void router.push(buildDashboardHref(preferredMembership));
                      return;
                    }
                    void router.push("/host");
                  }}
                  style={{
                    borderRadius: 999,
                    border: "none",
                    padding: "9px 18px",
                    background: "#c5a263",
                    color: "#ffffff",
                    fontWeight: 700,
                    fontSize: 13,
                    boxShadow: "0 10px 24px rgba(110,93,74,0.20)",
                    cursor: "pointer",
                  }}
                >
                  Open Dashboard
                </button>
              ) : (
                <Link
                  href="/login"
                  style={{
                    borderRadius: 999,
                    padding: "9px 18px",
                    background: "#c5a263",
                    color: "#ffffff",
                    fontWeight: 700,
                    fontSize: 13,
                    boxShadow: "0 10px 24px rgba(110,93,74,0.20)",
                  }}
                >
                  Host Login
                </Link>
              )}
            </div>
          </header>

          <section
            style={{
              display: "grid",
              gap: 18,
              gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 320px), 1fr))",
              alignItems: "stretch",
            }}
          >
            <aside
              style={{
                borderRadius: 30,
                border: "1px solid rgba(255,255,255,0.68)",
                background: "linear-gradient(180deg, rgba(255,255,255,0.18), rgba(255,255,255,0.10))",
                boxShadow: "inset 0 1px 0 rgba(255,255,255,0.90), 0 28px 64px rgba(122,101,79,0.18)",
                backdropFilter: "blur(40px)",
                WebkitBackdropFilter: "blur(40px)",
                padding: 22,
                display: "grid",
                gap: 16,
                height: "100%",
                alignContent: "start",
              }}
            >
              <div
                style={{
                  borderRadius: 22,
                  padding: "16px 18px",
                  background: "rgba(255,255,255,0.38)",
                  border: "1px solid rgba(255,255,255,0.55)",
                  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.75)",
                  backdropFilter: "blur(14px)",
                  WebkitBackdropFilter: "blur(14px)",
                }}
              >
                <p style={{ margin: 0, fontSize: 11, color: "#b08b4f", letterSpacing: "0.24em", textTransform: "uppercase", fontWeight: 800 }}>
                  Support Topics
                </p>
                <h2 style={{ margin: "10px 0 0", fontSize: 28, lineHeight: 1.05, letterSpacing: "-0.03em", color: "#1d1917" }}>
                  Billing, setup, and bugs.
                </h2>
              </div>

              <p style={{ margin: 0, fontSize: 15, lineHeight: 1.8, color: "#6d645c" }}>
                Use this form for billing, access, setup, or translation issues.
              </p>

              <div style={{ display: "grid", gap: 12 }}>
                {TOPIC_OPTIONS.map((entry) => (
                  <div
                    key={entry.value}
                    style={{
                      borderRadius: 18,
                      background: "rgba(255,255,255,0.38)",
                      border: "1px solid rgba(255,255,255,0.55)",
                      boxShadow: "inset 0 1px 0 rgba(255,255,255,0.75)",
                      backdropFilter: "blur(14px)",
                      WebkitBackdropFilter: "blur(14px)",
                      padding: "14px 16px",
                    }}
                  >
                    <p style={{ margin: 0, fontSize: 15, fontWeight: 700, color: "#1d1917" }}>{entry.label}</p>
                  </div>
                ))}
              </div>

              <div
                style={{
                  borderRadius: 22,
                  padding: "16px 18px",
                  background: "rgba(255,255,255,0.38)",
                  border: "1px solid rgba(255,255,255,0.55)",
                  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.75)",
                  backdropFilter: "blur(14px)",
                  WebkitBackdropFilter: "blur(14px)",
                  display: "grid",
                  gap: 8,
                }}
              >
                <p style={{ margin: 0, fontSize: 15, fontWeight: 800, color: "#22344c" }}>Spam protection</p>
                <p style={{ margin: 0, fontSize: 14, lineHeight: 1.7, color: "#5d6d84" }}>
                  Anonymous requests are screened before delivery. Signed-in users can submit with less friction.
                </p>
                <div
                  style={{
                    borderRadius: 18,
                    background: "rgba(244,247,252,0.72)",
                    border: "1px solid rgba(219,227,238,0.9)",
                    padding: "14px 16px",
                    display: "grid",
                    gap: 10,
                  }}
                >
                  <p style={{ margin: 0, fontSize: 14, fontWeight: 800, color: "#22344c" }}>Prefer direct email?</p>
                  <p style={{ margin: 0, fontSize: 13, lineHeight: 1.7, color: "#5d6d84" }}>
                    Use the form when possible. If needed, reveal the support inbox below.
                  </p>
                  {directEmail ? (
                    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10 }}>
                      <a
                        href={`mailto:${directEmail}`}
                        style={{
                          color: "#3f6093",
                          fontWeight: 700,
                          textDecoration: "none",
                          wordBreak: "break-all",
                        }}
                      >
                        {directEmail}
                      </a>
                      <button
                        type="button"
                        onClick={() => {
                          void copySupportEmail();
                        }}
                        style={{
                          borderRadius: 999,
                          border: "1px solid rgba(79,115,170,0.2)",
                          background: "rgba(255,255,255,0.74)",
                          color: "#314965",
                          fontWeight: 700,
                          padding: "8px 12px",
                          cursor: "pointer",
                        }}
                      >
                        {copiedDirectEmail ? "Copied" : "Copy"}
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={revealSupportEmail}
                      style={{
                        justifySelf: "start",
                        borderRadius: 999,
                        border: "1px solid rgba(79,115,170,0.2)",
                        background: "rgba(255,255,255,0.74)",
                        color: "#314965",
                        fontWeight: 700,
                        padding: "10px 14px",
                        cursor: "pointer",
                      }}
                    >
                      Reveal support email
                    </button>
                  )}
                  {directEmailMsg ? (
                    <p style={{ margin: 0, fontSize: 12, lineHeight: 1.6, color: "#6b7b92" }}>{directEmailMsg}</p>
                  ) : null}
                </div>
              </div>
            </aside>

            <article
              style={{
                borderRadius: 30,
                border: "1px solid rgba(255,255,255,0.68)",
                background: "linear-gradient(180deg, rgba(255,255,255,0.18), rgba(255,255,255,0.10))",
                boxShadow: "inset 0 1px 0 rgba(255,255,255,0.90), 0 28px 64px rgba(122,101,79,0.18)",
                backdropFilter: "blur(40px)",
                WebkitBackdropFilter: "blur(40px)",
                padding: 22,
                display: "grid",
                gap: 16,
                height: "100%",
                alignContent: "start",
              }}
            >
              <div
                style={{
                  borderRadius: 22,
                  padding: "16px 18px",
                  background: "rgba(255,255,255,0.38)",
                  border: "1px solid rgba(255,255,255,0.55)",
                  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.75)",
                  backdropFilter: "blur(14px)",
                  WebkitBackdropFilter: "blur(14px)",
                }}
              >
                <p style={{ margin: 0, fontSize: 11, color: "#b08b4f", letterSpacing: "0.24em", textTransform: "uppercase", fontWeight: 800 }}>
                  Submit Request
                </p>
                <h2 style={{ margin: "10px 0 0", fontSize: 28, lineHeight: 1.05, letterSpacing: "-0.03em", color: "#1d1917" }}>
                  Describe the issue.
                </h2>
              </div>

              {unconfiguredFallback ? (
                <div
                  style={{
                    margin: 0,
                    borderRadius: 14,
                    padding: "16px 18px",
                    background: "rgba(249,245,230,0.9)",
                    border: "1px solid rgba(210,190,130,0.5)",
                    color: "#5a4a20",
                    display: "grid",
                    gap: 10,
                  }}
                >
                  <p style={{ margin: 0, fontSize: 14, fontWeight: 700 }}>
                    Contact form is temporarily unavailable.
                  </p>
                  <p style={{ margin: 0, fontSize: 14, lineHeight: 1.7 }}>
                    Please email us directly:
                  </p>
                  <a
                    href={`mailto:${buildSupportEmail()}`}
                    style={{ color: "#3f6093", fontWeight: 700, wordBreak: "break-all" }}
                  >
                    {buildSupportEmail()}
                  </a>
                </div>
              ) : errorMsg ? (
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
                <div style={{ display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))" }}>
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

                <div style={{ display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))" }}>
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
                    placeholder="Describe the issue and which church or page is affected."
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
                    Choose the closest topic so support can route it faster.
                  </p>
                  <button
                    type="submit"
                    disabled={busy}
                    style={{
                      borderRadius: 999,
                      border: "none",
                      background: "#c5a263",
                      color: "#ffffff",
                      fontWeight: 700,
                      padding: "13px 22px",
                      cursor: busy ? "not-allowed" : "pointer",
                      opacity: busy ? 0.7 : 1,
                      boxShadow: "0 14px 30px rgba(110,93,74,0.22)",
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
