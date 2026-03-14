import Head from "next/head";
import Link from "next/link";

import { useAuth } from "../lib/authContext";

const publicQuickLinks = [
  {
    title: "Host Dashboard",
    desc: "Manage church services, start rooms, and generate team invite links.",
    href: "/login",
    cta: "Host login",
    tone: "#22c55e",
  },
  {
    title: "Create Church",
    desc: "Create your account and bootstrap your first church workspace.",
    href: "/signup",
    cta: "Sign up",
    tone: "#38bdf8",
  },
  {
    title: "Join by Invite",
    desc: "Use your invite code to join an existing church organization.",
    href: "/join",
    cta: "Open join page",
    tone: "#f59e0b",
  },
];

const signedInQuickLinks = [
  {
    title: "Continue Dashboard",
    desc: "Open your organization dashboard and continue hosting.",
    href: "/onboarding/create-church",
    cta: "Open dashboard",
    tone: "#22c55e",
  },
  {
    title: "Join by Invite",
    desc: "Accept an invite link to join another organization workspace.",
    href: "/join",
    cta: "Open join page",
    tone: "#f59e0b",
  },
];

const VIDEO_URL = (process.env.NEXT_PUBLIC_HOW_IT_WORKS_VIDEO_URL || "").trim();

function toEmbedVideoUrl(raw: string): string {
  const input = (raw || "").trim();
  if (!input) return "";
  if (input.includes("youtube.com/watch?v=")) {
    const token = input.split("v=")[1]?.split("&")[0] || "";
    return token ? `https://www.youtube.com/embed/${token}` : input;
  }
  if (input.includes("youtu.be/")) {
    const token = input.split("youtu.be/")[1]?.split("?")[0] || "";
    return token ? `https://www.youtube.com/embed/${token}` : input;
  }
  return input;
}

export default function HomePage() {
  const { user, loading: authLoading } = useAuth();
  const isLoggedIn = Boolean(user);
  const quickLinks = isLoggedIn ? signedInQuickLinks : publicQuickLinks;
  const embedVideoUrl = toEmbedVideoUrl(VIDEO_URL);

  return (
    <>
      <Head>
        <title>WorshipTranslate | Live Church Translation</title>
      </Head>
      <main
        style={{
          minHeight: "100vh",
          color: "#f8fafc",
          background:
            "radial-gradient(circle at 12% 14%, rgba(56,189,248,0.16), transparent 35%), radial-gradient(circle at 88% 4%, rgba(245,158,11,0.15), transparent 32%), #0b1220",
          padding: "24px 16px 36px",
        }}
      >
        <section style={{ maxWidth: 1040, margin: "0 auto", display: "grid", gap: 18 }}>
          <header
            style={{
              border: "1px solid rgba(255,255,255,0.14)",
              borderRadius: 18,
              background: "rgba(2,6,23,0.66)",
              padding: "18px 18px 16px",
            }}
          >
            <p style={{ margin: 0, fontSize: 12, letterSpacing: "0.08em", textTransform: "uppercase", opacity: 0.72 }}>
              WorshipTranslate
            </p>
            <h1 style={{ marginTop: 8, marginBottom: 8, fontSize: "clamp(28px,5vw,44px)", lineHeight: 1.05 }}>
              Live Translation For Worship Services
            </h1>
            <p style={{ marginTop: 0, marginBottom: 0, opacity: 0.88, maxWidth: 760 }}>
              Start a service, display a QR code, and let listeners follow live translated captions.
            </p>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 14 }}>
              {authLoading ? (
                <span style={{ fontSize: 13, opacity: 0.8 }}>Checking your session...</span>
              ) : isLoggedIn ? (
                <Link
                  href="/onboarding/create-church"
                  style={{
                    borderRadius: 10,
                    background: "#22c55e",
                    color: "#052e16",
                    fontWeight: 700,
                    padding: "8px 12px",
                  }}
                >
                  Continue to Dashboard
                </Link>
              ) : (
                <>
                  <Link
                    href="/c/demo/s/sun-11am"
                    style={{
                      borderRadius: 10,
                      background: "#38bdf8",
                      color: "#082f49",
                      fontWeight: 700,
                      padding: "8px 12px",
                    }}
                  >
                    Try Listener Demo
                  </Link>
                  <Link
                    href="/signup"
                    style={{
                      borderRadius: 10,
                      background: "#22c55e",
                      color: "#052e16",
                      fontWeight: 700,
                      padding: "8px 12px",
                    }}
                  >
                    Start Free Trial (30 minutes)
                  </Link>
                  <Link
                    href="/login"
                    style={{
                      borderRadius: 10,
                      border: "1px solid rgba(255,255,255,0.28)",
                      color: "#e2e8f0",
                      fontWeight: 700,
                      padding: "8px 12px",
                    }}
                  >
                    Host Login
                  </Link>
                </>
              )}
            </div>
          </header>

          <section
            style={{
              border: "1px solid rgba(255,255,255,0.14)",
              borderRadius: 14,
              background: "rgba(2,6,23,0.62)",
              padding: 14,
              display: "grid",
              gap: 10,
            }}
          >
            <p style={{ margin: 0, fontWeight: 700 }}>How It Works (60-90 sec)</p>
            {embedVideoUrl ? (
              <div style={{ position: "relative", width: "100%", paddingTop: "56.25%", borderRadius: 10, overflow: "hidden", border: "1px solid rgba(255,255,255,0.18)" }}>
                <iframe
                  src={embedVideoUrl}
                  title="WorshipTranslate demo video"
                  allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                  allowFullScreen
                  style={{ position: "absolute", inset: 0, width: "100%", height: "100%", border: "none" }}
                />
              </div>
            ) : (
              <div style={{ borderRadius: 10, border: "1px dashed rgba(255,255,255,0.24)", padding: 12, fontSize: 13, opacity: 0.86 }}>
                Add <code>NEXT_PUBLIC_HOW_IT_WORKS_VIDEO_URL</code> in <code>frontend/.env.local</code> (Loom, YouTube, or unlisted video URL).
              </div>
            )}
            <p style={{ margin: 0, opacity: 0.86, fontSize: 14 }}>
              1) Host starts a service. 2) Listeners scan QR. 3) Live translated captions are delivered instantly.
            </p>
          </section>

          <section style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" }}>
            {quickLinks.map((card) => (
              <article
                key={card.title}
                style={{
                  border: "1px solid rgba(255,255,255,0.16)",
                  borderRadius: 14,
                  background: "rgba(15,23,42,0.75)",
                  padding: 14,
                  display: "grid",
                  gap: 10,
                }}
              >
                <div style={{ width: 46, height: 5, borderRadius: 999, background: card.tone }} />
                <h2 style={{ margin: 0, fontSize: 18 }}>{card.title}</h2>
                <p style={{ margin: 0, opacity: 0.84, fontSize: 14 }}>{card.desc}</p>
                <Link
                  href={card.href}
                  style={{
                    marginTop: 2,
                    borderRadius: 9,
                    border: "1px solid rgba(255,255,255,0.24)",
                    color: "#e2e8f0",
                    fontSize: 14,
                    fontWeight: 700,
                    textAlign: "center",
                    padding: "8px 10px",
                  }}
                >
                  {card.cta}
                </Link>
              </article>
            ))}
          </section>

          <section
            style={{
              border: "1px solid rgba(255,255,255,0.14)",
              borderRadius: 14,
              background: "rgba(2,6,23,0.62)",
              padding: 14,
              fontSize: 14,
            }}
          >
            <p style={{ margin: 0, fontWeight: 700 }}>Quick Start</p>
            <p style={{ margin: "8px 0 0", opacity: 0.86 }}>
              1) Create church with <code>/signup</code> or sign in at <code>/login</code>.
            </p>
            <p style={{ margin: "4px 0 0", opacity: 0.86 }}>
              2) Hosts land at <code>/host/c/&lt;churchSlug&gt;/broadcast</code>.
            </p>
            <p style={{ margin: "4px 0 0", opacity: 0.86 }}>
              3) Listeners use <code>/c/&lt;churchSlug&gt;/s/&lt;serviceKey&gt;</code>.
            </p>
            <p style={{ margin: "4px 0 0", opacity: 0.86 }}>
              4) New church signup starts with a <strong>30-minute host trial</strong>.
            </p>
          </section>
        </section>
      </main>
    </>
  );
}
