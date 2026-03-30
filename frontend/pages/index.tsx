import Head from "next/head";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { fetchAuthMe } from "../lib/backendAuth";
import { useAuth } from "../lib/authContext";
import { buildDashboardHref, persistDashboardContext, pickPreferredMembership } from "../lib/dashboardRoute";
import { persistAuthToken } from "../utils/streamContext";

// ─── palette ────────────────────────────────────────────────────────────────
const C = {
  cream:    "#f7f4ef",
  navy:     "#0f1f3d",
  navyMid:  "#162844",
  charcoal: "#1c1c1c",
  gold:     "#b89a5e",
  goldLight:"#d4b87a",
  mid:      "#5a5a52",
  border:   "#e4ddd2",
  white:    "#ffffff",
};

// ─── data ────────────────────────────────────────────────────────────────────
const workflowSteps = [
  {
    id: "01",
    icon: "▶",
    title: "Start the service",
    desc: "Choose your church service and begin the live session from the host dashboard.",
  },
  {
    id: "02",
    icon: "⬡",
    title: "Share listener access",
    desc: "Display the QR code or send the listener link so people can join on their phones.",
  },
  {
    id: "03",
    icon: "≡",
    title: "Deliver translation live",
    desc: "Listeners follow the sermon in translated captions while the speaker stays focused.",
  },
];

const benefits = [
  { title: "Simple for the host", desc: "The person leading setup does not need a technical workflow. One dashboard, one start button." },
  { title: "Clear for the listener", desc: "Listeners open one page and follow along live. No account, no download." },
  { title: "Built for worship flow", desc: "Designed for sermons and church services, not generic events or conference tools." },
  { title: "QR code access", desc: "Let listeners join quickly without complicated setup — just scan and follow." },
  { title: "Recurring service structure", desc: "Reuse the same Sunday service slots week after week without rebuilding anything." },
  { title: "Prepared sermon support", desc: "Connect prepared content to improve translation consistency and flow." },
];

const scenarios = [
  { title: "International worship services", desc: "Help multilingual congregations follow the message together in their own language." },
  { title: "Guest listeners and visitors", desc: "Make access simple for newcomers through a QR code and phone link — no account needed." },
  { title: "Large-screen and mobile support", desc: "Use live captions on a display screen or let listeners follow on their own device." },
];

const faqs = [
  { q: "Do listeners need to create an account?", a: "No. Listeners open the listener page directly — no login, no download, no setup required." },
  { q: "Can people use their phones?", a: "Yes. The listener view is fully mobile-optimized. Any smartphone browser works." },
  { q: "Does this work for recurring Sunday services?", a: "Yes. You define service slots once and reuse the same structure every week." },
  { q: "Can we prepare sermon content ahead of time?", a: "Yes. The sermon prep feature lets you connect prepared scripts to improve translation accuracy." },
  { q: "Can we use captions on a screen?", a: "Yes. The listener URL can be displayed on any screen — phone, tablet, or large display." },
  { q: "How quickly can we get started?", a: "Create a church workspace, set up one service, and you can run your first live session the same day." },
];

const pricingPlans = [
  {
    name: "Free Trial",
    price: "Free",
    highlight: true,
    features: ["30 minutes included", "Full host workflow", "QR listener access", "No long setup"],
    cta: "Start Free Trial",
    href: "/signup",
  },
  {
    name: "Starter",
    price: "$20",
    period: "/mo",
    highlight: false,
    features: ["Up to 5 services", "Recurring service setup", "Host + admin roles", "Email support"],
    cta: "Get Started",
    href: "/signup",
  },
  {
    name: "Growth",
    price: "$40",
    period: "/mo",
    highlight: false,
    features: ["Up to 12 services", "More service hours", "Priority support", "Sermon prep tools"],
    cta: "Get Started",
    href: "/signup",
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

// ─── sub-components ──────────────────────────────────────────────────────────
function GoldDivider() {
  return <div style={{ width: 40, height: 1, background: C.gold, margin: "20px 0" }} />;
}

function Eyebrow({ children }: { children: React.ReactNode }) {
  return (
    <p style={{ margin: 0, fontSize: "0.72rem", letterSpacing: "0.22em", textTransform: "uppercase", color: C.gold, fontWeight: 500 }}>
      {children}
    </p>
  );
}

function SectionTitle({ children, light }: { children: React.ReactNode; light?: boolean }) {
  return (
    <h2
      style={{
        margin: 0,
        fontFamily: "'Cormorant Garamond', Georgia, serif",
        fontSize: "clamp(2rem, 3.5vw, 3rem)",
        fontWeight: 300,
        lineHeight: 1.12,
        color: light ? C.cream : C.charcoal,
      }}
    >
      {children}
    </h2>
  );
}

function QRPlaceholder({ size = 64 }: { size?: number }) {
  return (
    <div
      style={{
        width: size,
        height: size,
        background: C.white,
        borderRadius: 6,
        padding: 5,
        display: "grid",
        gridTemplateColumns: "repeat(5, 1fr)",
        gap: 2,
        flexShrink: 0,
      }}
    >
      {Array.from({ length: 25 }).map((_, i) => {
        const filled = [0,1,2,3,4,5,9,10,14,15,19,20,21,22,23,24,6,12].includes(i);
        return <div key={i} style={{ borderRadius: 1, background: filled ? C.navy : "transparent" }} />;
      })}
    </div>
  );
}

function ProductMockup({ isLoggedIn, dashboardHref, resolvingDashboard }: {
  isLoggedIn: boolean;
  dashboardHref: string;
  resolvingDashboard: boolean;
}) {
  const ctaHref = isLoggedIn ? dashboardHref : "/signup";
  const ctaLabel = isLoggedIn ? (resolvingDashboard ? "Loading..." : "Open Dashboard") : "Start Free Trial";

  return (
    <div
      style={{
        background: "rgba(10,18,36,0.72)",
        border: "1px solid rgba(255,255,255,0.10)",
        borderRadius: 18,
        overflow: "hidden",
        boxShadow: "0 48px 96px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.06)",
        backdropFilter: "blur(16px)",
      }}
    >
      {/* top bar */}
      <div
        style={{
          background: "rgba(255,255,255,0.04)",
          borderBottom: "1px solid rgba(255,255,255,0.07)",
          padding: "11px 16px",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <div style={{ display: "flex", gap: 5 }}>
          {["rgba(255,95,87,0.65)","rgba(255,188,46,0.65)","rgba(40,200,64,0.65)"].map((bg, i) => (
            <div key={i} style={{ width: 9, height: 9, borderRadius: 999, background: bg }} />
          ))}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <div style={{ width: 7, height: 7, borderRadius: 999, background: "#4ade80", boxShadow: "0 0 5px #4ade80" }} />
          <span style={{ fontSize: 10, color: "rgba(255,255,255,0.45)", letterSpacing: "0.12em", textTransform: "uppercase" }}>Live</span>
        </div>
        <span style={{ fontSize: 10, color: "rgba(255,255,255,0.32)", letterSpacing: "0.04em" }}>Sunday 11 AM</span>
      </div>

      {/* body */}
      <div style={{ padding: "18px 16px", display: "grid", gap: 12 }}>

        {/* listener count row */}
        <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
          <span style={{ fontSize: 10, color: "rgba(255,255,255,0.35)", letterSpacing: "0.16em", textTransform: "uppercase" }}>Listeners</span>
          <span style={{ fontSize: 26, fontWeight: 700, color: C.goldLight, letterSpacing: "-0.05em", lineHeight: 1 }}>24</span>
        </div>

        {/* scripture label */}
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ width: 2, height: 28, background: `linear-gradient(to bottom, ${C.gold}, transparent)`, borderRadius: 2, flexShrink: 0 }} />
          <div>
            <p style={{ margin: 0, fontSize: 9, letterSpacing: "0.18em", textTransform: "uppercase", color: "rgba(255,255,255,0.28)" }}>
              Scripture reference
            </p>
            <p style={{ margin: "2px 0 0", fontSize: 10, color: "rgba(255,255,255,0.45)", letterSpacing: "0.04em" }}>
              John 8:12
            </p>
          </div>
        </div>

        {/* transcript box */}
        <div
          style={{
            background: "rgba(0,0,0,0.28)",
            border: "1px solid rgba(255,255,255,0.05)",
            borderRadius: 10,
            padding: "12px 14px",
          }}
        >
          <p style={{ margin: "0 0 6px", fontSize: 9, letterSpacing: "0.18em", textTransform: "uppercase", color: "rgba(255,255,255,0.25)" }}>
            Korean → English
          </p>
          <p style={{ margin: 0, fontSize: 13, lineHeight: 1.65, color: "rgba(255,255,255,0.82)", fontWeight: 300 }}>
            And he said, whoever believes in me will not walk in darkness, but will have the light of life.
          </p>
        </div>

        {/* copy link row */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 7,
              background: "rgba(255,255,255,0.05)",
              border: "1px solid rgba(255,255,255,0.08)",
              borderRadius: 7,
              padding: "7px 12px",
              cursor: "default",
            }}
          >
            <svg width="11" height="11" viewBox="0 0 12 12" fill="none">
              <path d="M5 2H3a1 1 0 00-1 1v6a1 1 0 001 1h6a1 1 0 001-1V7M8 1h3v3M11 1L5.5 6.5" stroke="rgba(255,255,255,0.4)" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            <span style={{ fontSize: 11, color: "rgba(255,255,255,0.45)" }}>Copy link</span>
          </div>
          <span style={{ fontSize: 10, color: "rgba(255,255,255,0.28)", letterSpacing: "0.04em" }}>Next step ›</span>
        </div>

        {/* CTA + QR */}
        <div style={{ display: "flex", gap: 10, alignItems: "stretch" }}>
          <Link
            href={ctaHref}
            style={{
              flex: 1,
              background: `linear-gradient(135deg, ${C.gold} 0%, ${C.goldLight} 100%)`,
              color: C.navy,
              borderRadius: 9,
              padding: "11px 0",
              fontSize: 11,
              fontWeight: 800,
              textAlign: "center",
              letterSpacing: "0.1em",
              textTransform: "uppercase",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              boxShadow: "0 4px 20px rgba(184,154,94,0.35)",
            }}
          >
            {ctaLabel}
          </Link>
          <QRPlaceholder size={42} />
        </div>
      </div>
    </div>
  );
}

function FAQItem({ q, a }: { q: string; a: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div
      style={{
        borderBottom: `1px solid ${C.border}`,
        padding: "0",
      }}
    >
      <button
        onClick={() => setOpen(!open)}
        style={{
          width: "100%",
          background: "none",
          border: "none",
          padding: "22px 0",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          cursor: "pointer",
          gap: 16,
          textAlign: "left",
        }}
      >
        <span style={{ fontSize: "1rem", fontWeight: 500, color: C.charcoal, lineHeight: 1.4 }}>{q}</span>
        <span
          style={{
            color: C.gold,
            fontSize: 18,
            fontWeight: 300,
            flexShrink: 0,
            transition: "transform 0.2s",
            transform: open ? "rotate(45deg)" : "none",
          }}
        >
          +
        </span>
      </button>
      {open && (
        <p style={{ margin: "0 0 22px", fontSize: "0.95rem", lineHeight: 1.75, color: C.mid }}>
          {a}
        </p>
      )}
    </div>
  );
}

// ─── main page ───────────────────────────────────────────────────────────────
export default function HomePage() {
  const { user, loading: authLoading, configured, getIdToken } = useAuth();
  const [dashboardHref, setDashboardHref] = useState("/onboarding/create-church");
  const [resolvingDashboard, setResolvingDashboard] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const isLoggedIn = Boolean(user);
  const embedVideoUrl = toEmbedVideoUrl(VIDEO_URL);

  useMemo(() => dashboardHref, [dashboardHref]);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 40);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    if (authLoading || !user || !configured) {
      setResolvingDashboard(false);
      return;
    }
    let cancelled = false;
    setResolvingDashboard(true);
    void (async () => {
      try {
        const token = await getIdToken(true);
        if (!token || cancelled) return;
        persistAuthToken(token);
        const me = await fetchAuthMe(token);
        const membership = pickPreferredMembership(me);
        if (!membership || cancelled) {
          setDashboardHref("/onboarding/create-church");
          return;
        }
        persistDashboardContext(membership);
        setDashboardHref(buildDashboardHref(membership));
      } catch {
        if (!cancelled) setDashboardHref("/login");
      } finally {
        if (!cancelled) setResolvingDashboard(false);
      }
    })();
    return () => { cancelled = true; };
  }, [authLoading, configured, getIdToken, user]);

  const primaryCta = isLoggedIn
    ? { label: resolvingDashboard ? "Loading..." : "Open Dashboard", href: dashboardHref }
    : { label: "Start Free Trial", href: "/signup" };

  const MAX_W = 1160;
  const container: React.CSSProperties = { maxWidth: MAX_W, margin: "0 auto", padding: "0 32px" };

  return (
    <>
      <Head>
        <title>Worship Translation — Live Church Translation</title>
        <meta name="description" content="Start the service once, share a QR code, and let translated captions reach listeners in real time. Built for multilingual church worship." />
<style>{`
          *, *::before, *::after { box-sizing: border-box; }
          html { scroll-behavior: smooth; }
          body { margin: 0; padding: 0; font-family: 'DM Sans', 'Segoe UI', sans-serif; background: ${C.cream}; color: ${C.charcoal}; line-height: 1.6; overflow-x: hidden; }
          a { text-decoration: none; color: inherit; }
          button { font-family: inherit; }
        `}</style>
      </Head>

      {/* ── NAV ──────────────────────────────────────────────────────────── */}
      <nav
        style={{
          position: "fixed",
          top: 0, left: 0, right: 0,
          zIndex: 200,
          height: 68,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "0 32px",
          transition: "background 0.3s, box-shadow 0.3s",
          background: scrolled ? "rgba(247,244,239,0.96)" : "transparent",
          backdropFilter: scrolled ? "blur(12px)" : "none",
          boxShadow: scrolled ? "0 1px 0 rgba(0,0,0,0.06)" : "none",
        }}
      >
        {/* logo */}
        <Link href="/" style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div
            style={{
              width: 34, height: 34, borderRadius: 10,
              background: scrolled ? C.navy : "rgba(255,255,255,0.12)",
              border: `1px solid ${scrolled ? "transparent" : "rgba(255,255,255,0.18)"}`,
              display: "grid", placeItems: "center",
              color: C.goldLight, fontSize: 16, fontWeight: 800, fontStyle: "italic",
            }}
          >
            W
          </div>
          <span
            style={{
              fontFamily: "'Cormorant Garamond', Georgia, serif",
              fontSize: "1.2rem",
              fontWeight: 600,
              letterSpacing: "0.04em",
              color: scrolled ? C.charcoal : C.white,
            }}
          >
            Worship Translation
          </span>
        </Link>

        {/* desktop links */}
        <ul
          style={{
            display: "flex", gap: 32, listStyle: "none", margin: 0, padding: 0,
            ["@media(max-width:768px)" as string]: { display: "none" },
          }}
          className="nav-desktop-links"
        >
          {["How It Works", "Pricing", "Demo", "Churches"].map((label) => (
            <li key={label}>
              <a
                href={`#${label.toLowerCase().replace(/\s+/g, "-")}`}
                style={{
                  fontSize: "0.8rem", fontWeight: 500, letterSpacing: "0.1em",
                  textTransform: "uppercase",
                  color: scrolled ? C.mid : "rgba(255,255,255,0.72)",
                  transition: "color 0.2s",
                }}
              >
                {label}
              </a>
            </li>
          ))}
        </ul>

        {/* nav CTAs */}
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          {!authLoading && (
            isLoggedIn ? (
              <Link
                href={dashboardHref}
                style={{
                  padding: "9px 20px",
                  background: C.gold,
                  color: C.white,
                  fontSize: "0.8rem",
                  fontWeight: 700,
                  letterSpacing: "0.08em",
                  textTransform: "uppercase",
                  borderRadius: 4,
                }}
              >
                Dashboard
              </Link>
            ) : (
              <>
                <Link
                  href="/login"
                  style={{
                    padding: "9px 16px",
                    fontSize: "0.8rem",
                    fontWeight: 500,
                    color: scrolled ? C.mid : "rgba(255,255,255,0.72)",
                    letterSpacing: "0.06em",
                  }}
                >
                  Sign In
                </Link>
                <Link
                  href="/signup"
                  style={{
                    padding: "9px 20px",
                    background: C.gold,
                    color: C.white,
                    fontSize: "0.8rem",
                    fontWeight: 700,
                    letterSpacing: "0.08em",
                    textTransform: "uppercase",
                    borderRadius: 4,
                    transition: "background 0.2s",
                  }}
                >
                  Free Trial
                </Link>
              </>
            )
          )}
          <button
            onClick={() => setMobileNavOpen(!mobileNavOpen)}
            aria-label="Toggle menu"
            style={{
              background: "none", border: "none", cursor: "pointer",
              display: "none", padding: 4,
              color: scrolled ? C.charcoal : C.white,
            }}
            className="nav-mobile-toggle"
          >
            <svg width="22" height="16" viewBox="0 0 22 16" fill="none">
              <rect width="22" height="2" rx="1" fill="currentColor"/>
              <rect y="7" width="16" height="2" rx="1" fill="currentColor"/>
              <rect y="14" width="22" height="2" rx="1" fill="currentColor"/>
            </svg>
          </button>
        </div>
      </nav>

      {/* mobile nav drawer */}
      {mobileNavOpen && (
        <div
          style={{
            position: "fixed", inset: 0, zIndex: 190,
            background: C.navy,
            display: "flex", flexDirection: "column",
            padding: "80px 32px 32px",
            gap: 8,
          }}
        >
          {["How It Works", "Pricing", "Demo", "Churches"].map((label) => (
            <a
              key={label}
              href={`#${label.toLowerCase().replace(/\s+/g, "-")}`}
              onClick={() => setMobileNavOpen(false)}
              style={{
                fontSize: "1.5rem", color: C.cream, fontWeight: 300,
                fontFamily: "'Cormorant Garamond', Georgia, serif",
                padding: "12px 0",
                borderBottom: "1px solid rgba(255,255,255,0.08)",
              }}
            >
              {label}
            </a>
          ))}
          <div style={{ marginTop: 24, display: "flex", gap: 12 }}>
            {isLoggedIn ? (
              <Link href={dashboardHref} onClick={() => setMobileNavOpen(false)}
                style={{ padding: "13px 24px", background: C.gold, color: C.white, borderRadius: 4, fontSize: "0.9rem", fontWeight: 700 }}>
                {resolvingDashboard ? "Loading..." : "Dashboard"}
              </Link>
            ) : (
              <>
                <Link href="/login" onClick={() => setMobileNavOpen(false)}
                  style={{ padding: "13px 20px", border: `1px solid rgba(255,255,255,0.2)`, color: C.cream, borderRadius: 4, fontSize: "0.9rem" }}>
                  Sign In
                </Link>
                <Link href="/signup" onClick={() => setMobileNavOpen(false)}
                  style={{ padding: "13px 24px", background: C.gold, color: C.white, borderRadius: 4, fontSize: "0.9rem", fontWeight: 700 }}>
                  Free Trial
                </Link>
              </>
            )}
          </div>
        </div>
      )}

      <main>
        {/* ── HERO ───────────────────────────────────────────────────────── */}
        <section
          id="hero"
          style={{
            background: `linear-gradient(155deg, #060d1a 0%, #0d1e38 45%, #0f2040 100%)`,
            minHeight: "100vh",
            paddingTop: 68,
            display: "grid",
            alignItems: "center",
            position: "relative",
            overflow: "hidden",
          }}
        >
          {/* background glow */}
          <div aria-hidden style={{ position: "absolute", top: "10%", left: "-4%", width: 560, height: 560, borderRadius: "50%", background: "radial-gradient(circle, rgba(184,154,94,0.07) 0%, transparent 68%)", pointerEvents: "none" }} />
          <div aria-hidden style={{ position: "absolute", bottom: "8%", right: "2%", width: 440, height: 440, borderRadius: "50%", background: "radial-gradient(circle, rgba(80,120,200,0.10) 0%, transparent 70%)", pointerEvents: "none" }} />
          <div aria-hidden style={{ position: "absolute", top: "50%", left: "50%", transform: "translate(-50%, -50%)", width: 700, height: 300, borderRadius: "50%", background: "radial-gradient(ellipse, rgba(184,154,94,0.04) 0%, transparent 65%)", pointerEvents: "none" }} />

          <div style={{ ...container, padding: "80px 32px" }}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 72, alignItems: "center" }}>

              {/* left text */}
              <div>
                <Eyebrow>Live translation for multilingual worship</Eyebrow>
                <GoldDivider />
                <h1
                  style={{
                    margin: 0,
                    fontFamily: "'Cormorant Garamond', Georgia, serif",
                    fontSize: "clamp(3rem, 5.5vw, 5.2rem)",
                    fontWeight: 300,
                    lineHeight: 1.04,
                    letterSpacing: "-0.02em",
                    color: C.white,
                  }}
                >
                  Help every listener{" "}
                  <em style={{ fontStyle: "italic", color: "rgba(255,255,255,0.72)", fontWeight: 300 }}>follow the sermon live.</em>
                </h1>
                <p style={{ margin: "24px 0 0", fontSize: "0.97rem", lineHeight: 1.75, color: "rgba(255,255,255,0.5)", maxWidth: 400, fontWeight: 300 }}>
                  Start the service once, share a QR code, and let translated captions reach listeners in real time.
                </p>

                <div style={{ marginTop: 40 }}>
                  <Link
                    href={primaryCta.href}
                    style={{
                      display: "inline-block",
                      padding: "15px 36px",
                      background: C.gold,
                      color: C.white,
                      fontSize: "0.78rem",
                      fontWeight: 700,
                      letterSpacing: "0.14em",
                      textTransform: "uppercase",
                      borderRadius: 6,
                      boxShadow: "0 8px 32px rgba(184,154,94,0.28)",
                    }}
                  >
                    {primaryCta.label}
                  </Link>
                </div>

                <p style={{ marginTop: 28, fontSize: "0.74rem", color: "rgba(255,255,255,0.38)", letterSpacing: "0.06em" }}>
                  No listener login required&nbsp;&nbsp;·&nbsp;&nbsp;Works on phones&nbsp;&nbsp;·&nbsp;&nbsp;Built for church services
                </p>
              </div>

              {/* right mockup */}
              <div>
                <ProductMockup isLoggedIn={isLoggedIn} dashboardHref={dashboardHref} resolvingDashboard={resolvingDashboard} />
              </div>
            </div>
          </div>
        </section>

        {/* ── HOW IT WORKS ───────────────────────────────────────────────── */}
        <section id="how-it-works" style={{ background: C.white, padding: "100px 0" }}>
          <div style={container}>
            <div style={{ textAlign: "center", marginBottom: 64 }}>
              <Eyebrow>Simple by design</Eyebrow>
              <GoldDivider />
              <div style={{ width: 40, height: 1, background: C.gold, margin: "0 auto 20px" }} />
              <SectionTitle>Three steps from setup to live service.</SectionTitle>
            </div>

            <div id="workflow-grid" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 280px), 1fr))", gap: 2 }}>
              {workflowSteps.map((step, idx) => (
                <div
                  key={step.id}
                  style={{
                    padding: "48px 36px",
                    borderLeft: idx > 0 ? `1px solid ${C.border}` : "none",
                    position: "relative",
                  }}
                >
                  <div
                    style={{
                      width: 48, height: 48, borderRadius: "50%",
                      background: "rgba(184,154,94,0.1)",
                      border: `1px solid rgba(184,154,94,0.25)`,
                      display: "grid", placeItems: "center",
                      color: C.gold, fontSize: 14, marginBottom: 24,
                    }}
                  >
                    {step.id}
                  </div>
                  <h3 style={{ margin: "0 0 12px", fontSize: "1.1rem", fontWeight: 700, color: C.charcoal, letterSpacing: "-0.02em" }}>
                    {step.title}
                  </h3>
                  <p style={{ margin: 0, fontSize: "0.92rem", lineHeight: 1.75, color: C.mid }}>
                    {step.desc}
                  </p>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* ── PRODUCT PREVIEW ────────────────────────────────────────────── */}
        <section id="churches" style={{ background: C.cream, padding: "100px 0" }}>
          <div style={container}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 80, alignItems: "center" }}>
              {/* callouts left */}
              <div>
                <Eyebrow>Host console</Eyebrow>
                <GoldDivider />
                <SectionTitle>A host console built for live worship.</SectionTitle>
                <p style={{ margin: "20px 0 40px", fontSize: "1rem", lineHeight: 1.75, color: C.mid }}>
                  Keep the service calm and organized while the translation runs in the background.
                </p>
                <div style={{ display: "grid", gap: 16 }}>
                  {[
                    "Start once for the whole service",
                    "Monitor translated output live",
                    "Share listener access in seconds",
                    "Reuse recurring service settings",
                  ].map((item) => (
                    <div key={item} style={{ display: "flex", gap: 14, alignItems: "start" }}>
                      <div style={{ width: 20, height: 20, flexShrink: 0, marginTop: 2, borderRadius: "50%", background: "rgba(184,154,94,0.15)", border: `1px solid rgba(184,154,94,0.3)`, display: "grid", placeItems: "center" }}>
                        <svg width="8" height="6" viewBox="0 0 8 6" fill="none"><path d="M1 3l2 2 4-4" stroke={C.gold} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
                      </div>
                      <p style={{ margin: 0, fontSize: "0.92rem", color: C.charcoal, lineHeight: 1.6 }}>{item}</p>
                    </div>
                  ))}
                </div>
              </div>

              {/* mockup right */}
              <div style={{ background: C.navy, borderRadius: 16, padding: 24, boxShadow: "0 40px 80px rgba(15,31,61,0.2)" }}>
                <ProductMockup isLoggedIn={isLoggedIn} dashboardHref={dashboardHref} resolvingDashboard={resolvingDashboard} />
              </div>
            </div>
          </div>
        </section>

        {/* ── BENEFITS ───────────────────────────────────────────────────── */}
        <section style={{ background: C.white, padding: "100px 0" }}>
          <div style={container}>
            <div style={{ marginBottom: 64 }}>
              <Eyebrow>Why it works</Eyebrow>
              <GoldDivider />
              <SectionTitle>Simple for the host. Clear for the listener.<br />Built for worship.</SectionTitle>
            </div>

            <div id="benefits-grid" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 300px), 1fr))", gap: 1, border: `1px solid ${C.border}` }}>
              {benefits.map((b, idx) => (
                <div
                  key={b.title}
                  style={{
                    padding: "36px 32px",
                    borderRight: (idx % 3 !== 2) ? `1px solid ${C.border}` : "none",
                    borderBottom: idx < 3 ? `1px solid ${C.border}` : "none",
                  }}
                >
                  <h3 style={{ margin: "0 0 12px", fontSize: "1rem", fontWeight: 700, color: C.charcoal }}>
                    {b.title}
                  </h3>
                  <p style={{ margin: 0, fontSize: "0.9rem", lineHeight: 1.75, color: C.mid }}>
                    {b.desc}
                  </p>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* ── CHURCH SCENARIOS ───────────────────────────────────────────── */}
        <section style={{ background: C.cream, padding: "100px 0" }}>
          <div style={container}>
            <div style={{ textAlign: "center", marginBottom: 64 }}>
              <Eyebrow>Real church use</Eyebrow>
              <GoldDivider />
              <div style={{ width: 40, height: 1, background: C.gold, margin: "0 auto 20px" }} />
              <SectionTitle>Designed for the way churches actually serve people.</SectionTitle>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 280px), 1fr))", gap: 24 }}>
              {scenarios.map((s) => (
                <div
                  key={s.title}
                  style={{
                    padding: "40px 32px",
                    background: C.white,
                    border: `1px solid ${C.border}`,
                    borderTop: `3px solid ${C.gold}`,
                  }}
                >
                  <h3 style={{ margin: "0 0 14px", fontSize: "1.05rem", fontWeight: 700, color: C.charcoal }}>
                    {s.title}
                  </h3>
                  <p style={{ margin: 0, fontSize: "0.92rem", lineHeight: 1.75, color: C.mid }}>
                    {s.desc}
                  </p>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* ── TRUST ──────────────────────────────────────────────────────── */}
        <section style={{ background: C.navy, padding: "80px 0" }}>
          <div style={{ ...container, textAlign: "center" }}>
            <Eyebrow>Dependable</Eyebrow>
            <GoldDivider />
            <div style={{ width: 40, height: 1, background: C.gold, margin: "0 auto 20px" }} />
            <h2
              style={{
                margin: "0 0 20px",
                fontFamily: "'Cormorant Garamond', Georgia, serif",
                fontSize: "clamp(1.8rem, 3vw, 2.6rem)",
                fontWeight: 300,
                color: C.white,
              }}
            >
              Calm, dependable service-day workflow.
            </h2>
            <p style={{ margin: "0 auto", maxWidth: 560, fontSize: "1rem", color: "rgba(255,255,255,0.5)", lineHeight: 1.75 }}>
              From setup to live service, the experience stays simple and repeatable.
            </p>
            <div style={{ marginTop: 48, display: "flex", justifyContent: "center", flexWrap: "wrap", gap: 0, borderTop: "1px solid rgba(255,255,255,0.08)" }}>
              {[
                ["Start in minutes", "No waiting, no configuration marathon."],
                ["No complicated listener setup", "Scan a QR, follow along. That's it."],
                ["Repeatable every Sunday", "Same setup, same structure, every week."],
                ["Built for real services", "Not a demo product — built for live worship."],
              ].map(([title, desc]) => (
                <div
                  key={title}
                  style={{
                    flex: "1 1 220px",
                    padding: "36px 28px",
                    borderRight: "1px solid rgba(255,255,255,0.08)",
                    borderBottom: "1px solid rgba(255,255,255,0.08)",
                    textAlign: "left",
                  }}
                >
                  <div style={{ width: 24, height: 1, background: C.gold, marginBottom: 16 }} />
                  <p style={{ margin: "0 0 8px", fontSize: "0.95rem", fontWeight: 700, color: C.cream }}>{title}</p>
                  <p style={{ margin: 0, fontSize: "0.85rem", color: "rgba(255,255,255,0.4)", lineHeight: 1.65 }}>{desc}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* ── DEMO VIDEO ─────────────────────────────────────────────────── */}
        <section id="demo" style={{ background: C.white, padding: "100px 0" }}>
          <div style={container}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 80, alignItems: "center" }}>
              <div>
                <Eyebrow>See it in action</Eyebrow>
                <GoldDivider />
                <SectionTitle>See the full host-to-listener flow.</SectionTitle>
                <div style={{ marginTop: 40, display: "grid", gap: 20 }}>
                  {[
                    ["Start the service", "Open the host dashboard and choose a service slot."],
                    ["Show the QR", "Listeners scan to join — no login required."],
                    ["Watch translation appear live", "Captions update in real time as the speaker continues."],
                  ].map(([title, desc]) => (
                    <div key={title} style={{ display: "flex", gap: 16, alignItems: "start" }}>
                      <div style={{ width: 28, height: 28, flexShrink: 0, borderRadius: "50%", background: "rgba(184,154,94,0.1)", border: `1px solid rgba(184,154,94,0.25)`, display: "grid", placeItems: "center" }}>
                        <div style={{ width: 6, height: 6, borderRadius: "50%", background: C.gold }} />
                      </div>
                      <div>
                        <p style={{ margin: "0 0 4px", fontSize: "0.92rem", fontWeight: 700, color: C.charcoal }}>{title}</p>
                        <p style={{ margin: 0, fontSize: "0.88rem", color: C.mid, lineHeight: 1.6 }}>{desc}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <div>
                {embedVideoUrl ? (
                  <div style={{ position: "relative", width: "100%", paddingTop: "56.25%", borderRadius: 8, overflow: "hidden", boxShadow: "0 24px 60px rgba(0,0,0,0.12)", border: `1px solid ${C.border}` }}>
                    <iframe
                      src={embedVideoUrl}
                      title="Worship Translation demo"
                      allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                      allowFullScreen
                      style={{ position: "absolute", inset: 0, width: "100%", height: "100%", border: "none" }}
                    />
                  </div>
                ) : (
                  <div
                    style={{
                      aspectRatio: "16/9",
                      background: C.cream,
                      border: `1px solid ${C.border}`,
                      borderRadius: 8,
                      display: "flex",
                      flexDirection: "column",
                      alignItems: "center",
                      justifyContent: "center",
                      gap: 12,
                    }}
                  >
                    <div style={{ width: 56, height: 56, borderRadius: "50%", background: "rgba(184,154,94,0.1)", border: `1px solid rgba(184,154,94,0.25)`, display: "grid", placeItems: "center" }}>
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none"><path d="M6 4l9 5-9 5V4z" fill={C.gold}/></svg>
                    </div>
                    <p style={{ margin: 0, fontSize: "0.82rem", color: C.mid, textAlign: "center", lineHeight: 1.6 }}>
                      Set <code style={{ fontSize: "0.78rem", background: C.border, padding: "2px 6px", borderRadius: 3 }}>NEXT_PUBLIC_HOW_IT_WORKS_VIDEO_URL</code>
                    </p>
                  </div>
                )}
              </div>
            </div>
          </div>
        </section>

        {/* ── PRICING ────────────────────────────────────────────────────── */}
        <section id="pricing" style={{ background: C.cream, padding: "100px 0" }}>
          <div style={container}>
            <div style={{ textAlign: "center", marginBottom: 64 }}>
              <Eyebrow>Pricing</Eyebrow>
              <GoldDivider />
              <div style={{ width: 40, height: 1, background: C.gold, margin: "0 auto 20px" }} />
              <SectionTitle>Start free. Scale when you&rsquo;re ready.</SectionTitle>
              <p style={{ margin: "16px auto 0", maxWidth: 480, fontSize: "1rem", color: C.mid }}>
                The free trial includes everything you need to run your first live service.
              </p>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 280px), 1fr))", gap: 24, alignItems: "stretch" }}>
              {pricingPlans.map((plan) => (
                <div
                  key={plan.name}
                  style={{
                    padding: "40px 32px",
                    background: plan.highlight ? C.navy : C.white,
                    border: plan.highlight ? "none" : `1px solid ${C.border}`,
                    display: "flex",
                    flexDirection: "column",
                    gap: 24,
                    position: "relative",
                  }}
                >
                  {plan.highlight && (
                    <div style={{ position: "absolute", top: -1, left: 32, right: 32, height: 3, background: C.gold }} />
                  )}
                  <div>
                    <p style={{ margin: "0 0 8px", fontSize: "0.75rem", letterSpacing: "0.18em", textTransform: "uppercase", color: plan.highlight ? C.goldLight : C.gold }}>
                      {plan.name}
                    </p>
                    <div style={{ display: "flex", alignItems: "baseline", gap: 4 }}>
                      <span style={{ fontSize: "2.4rem", fontWeight: 700, letterSpacing: "-0.04em", color: plan.highlight ? C.white : C.charcoal, fontFamily: "'Cormorant Garamond', serif" }}>
                        {plan.price}
                      </span>
                      {plan.period && <span style={{ fontSize: "0.9rem", color: plan.highlight ? "rgba(255,255,255,0.4)" : C.mid }}>{plan.period}</span>}
                    </div>
                  </div>

                  <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "grid", gap: 10, flex: 1 }}>
                    {plan.features.map((f) => (
                      <li key={f} style={{ display: "flex", gap: 10, alignItems: "start", fontSize: "0.88rem", color: plan.highlight ? "rgba(255,255,255,0.7)" : C.mid }}>
                        <span style={{ color: plan.highlight ? C.goldLight : C.gold, flexShrink: 0 }}>·</span>
                        {f}
                      </li>
                    ))}
                  </ul>

                  <Link
                    href={plan.href}
                    style={{
                      padding: "13px 24px",
                      textAlign: "center",
                      fontSize: "0.8rem",
                      fontWeight: 700,
                      letterSpacing: "0.1em",
                      textTransform: "uppercase",
                      borderRadius: 4,
                      background: plan.highlight ? C.gold : "transparent",
                      color: plan.highlight ? C.white : C.charcoal,
                      border: plan.highlight ? "none" : `1px solid ${C.border}`,
                      transition: "background 0.2s",
                    }}
                  >
                    {plan.cta}
                  </Link>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* ── FAQ ────────────────────────────────────────────────────────── */}
        <section style={{ background: C.white, padding: "100px 0" }}>
          <div style={{ ...container, maxWidth: 720 }}>
            <div style={{ marginBottom: 64 }}>
              <Eyebrow>Common questions</Eyebrow>
              <GoldDivider />
              <SectionTitle>Everything you need to know.</SectionTitle>
            </div>
            <div style={{ borderTop: `1px solid ${C.border}` }}>
              {faqs.map((faq) => <FAQItem key={faq.q} q={faq.q} a={faq.a} />)}
            </div>
          </div>
        </section>

        {/* ── FINAL CTA ──────────────────────────────────────────────────── */}
        <section
          style={{
            background: `linear-gradient(160deg, #0a1628 0%, ${C.navy} 100%)`,
            padding: "120px 0",
            textAlign: "center",
            position: "relative",
            overflow: "hidden",
          }}
        >
          <div aria-hidden style={{ position: "absolute", top: "50%", left: "50%", transform: "translate(-50%,-50%)", width: 600, height: 400, background: "radial-gradient(ellipse, rgba(184,154,94,0.08) 0%, transparent 70%)", pointerEvents: "none" }} />
          <div style={container}>
            <Eyebrow>Get started today</Eyebrow>
            <GoldDivider />
            <div style={{ width: 40, height: 1, background: C.gold, margin: "0 auto 20px" }} />
            <h2
              style={{
                margin: "0 auto 20px",
                maxWidth: 640,
                fontFamily: "'Cormorant Garamond', Georgia, serif",
                fontSize: "clamp(2rem, 4vw, 3.2rem)",
                fontWeight: 300,
                lineHeight: 1.1,
                color: C.white,
              }}
            >
              Ready to try live worship translation in your church?
            </h2>
            <p style={{ margin: "0 auto 48px", maxWidth: 480, fontSize: "1rem", color: "rgba(255,255,255,0.5)", lineHeight: 1.75 }}>
              Start a trial, test the host flow, and see how listeners follow the message live.
            </p>
            <div style={{ display: "flex", justifyContent: "center", gap: 16, flexWrap: "wrap" }}>
              <Link
                href={primaryCta.href}
                style={{
                  padding: "16px 36px",
                  background: C.gold,
                  color: C.white,
                  fontSize: "0.82rem",
                  fontWeight: 700,
                  letterSpacing: "0.1em",
                  textTransform: "uppercase",
                  borderRadius: 4,
                  boxShadow: "0 8px 32px rgba(184,154,94,0.25)",
                }}
              >
                {primaryCta.label}
              </Link>
              <a
                href="#demo"
                style={{
                  padding: "16px 36px",
                  border: "1px solid rgba(255,255,255,0.18)",
                  color: "rgba(255,255,255,0.72)",
                  fontSize: "0.82rem",
                  fontWeight: 500,
                  letterSpacing: "0.1em",
                  textTransform: "uppercase",
                  borderRadius: 4,
                }}
              >
                Watch Demo
              </a>
            </div>
          </div>
        </section>

        {/* ── FOOTER ─────────────────────────────────────────────────────── */}
        <footer style={{ background: "#080e1c", padding: "60px 0 40px" }}>
          <div style={container}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: 40, marginBottom: 48 }}>
              <div>
                <Link href="/" style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
                  <div style={{ width: 30, height: 30, borderRadius: 8, background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.1)", display: "grid", placeItems: "center", color: C.goldLight, fontSize: 14, fontWeight: 800, fontStyle: "italic" }}>
                    W
                  </div>
                  <span style={{ fontFamily: "'Cormorant Garamond', Georgia, serif", fontSize: "1.1rem", fontWeight: 600, color: "rgba(255,255,255,0.7)" }}>
                    Worship Translation
                  </span>
                </Link>
                <p style={{ margin: 0, fontSize: "0.85rem", color: "rgba(255,255,255,0.3)", maxWidth: 280, lineHeight: 1.7 }}>
                  Live translation for multilingual worship services.
                </p>
              </div>

              <div style={{ display: "flex", gap: 48, flexWrap: "wrap" }}>
                <div style={{ display: "grid", gap: 12, alignContent: "start" }}>
                  <p style={{ margin: 0, fontSize: "0.72rem", letterSpacing: "0.18em", textTransform: "uppercase", color: "rgba(255,255,255,0.3)" }}>Product</p>
                  {[["How It Works", "#how-it-works"], ["Pricing", "#pricing"], ["Demo", "#demo"]].map(([label, href]) => (
                    <a key={label} href={href} style={{ fontSize: "0.85rem", color: "rgba(255,255,255,0.45)", transition: "color 0.2s" }}>{label}</a>
                  ))}
                </div>
                <div style={{ display: "grid", gap: 12, alignContent: "start" }}>
                  <p style={{ margin: 0, fontSize: "0.72rem", letterSpacing: "0.18em", textTransform: "uppercase", color: "rgba(255,255,255,0.3)" }}>Account</p>
                  {[["Sign In", "/login"], ["Start Free Trial", "/signup"], ["Contact", "/contact"]].map(([label, href]) => (
                    <Link key={label} href={href} style={{ fontSize: "0.85rem", color: "rgba(255,255,255,0.45)" }}>{label}</Link>
                  ))}
                </div>
              </div>
            </div>

            <div style={{ borderTop: "1px solid rgba(255,255,255,0.06)", paddingTop: 28, display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
              <p style={{ margin: 0, fontSize: "0.78rem", color: "rgba(255,255,255,0.2)" }}>
                © {new Date().getFullYear()} Worship Translation. All rights reserved.
              </p>
              <div style={{ display: "flex", gap: 20 }}>
                {[["Terms", "/terms"], ["Privacy", "/privacy"]].map(([label, href]) => (
                  <Link key={label} href={href} style={{ fontSize: "0.78rem", color: "rgba(255,255,255,0.2)" }}>{label}</Link>
                ))}
              </div>
            </div>
          </div>
        </footer>
      </main>

      {/* responsive overrides */}
      <style>{`
        @media (max-width: 900px) {
          #hero > div > div { grid-template-columns: 1fr !important; }
          #hero > div > div > div:last-child { display: none; }
          #churches > div > div { grid-template-columns: 1fr !important; }
          #churches > div > div > div:last-child { display: none; }
          #demo > div > div { grid-template-columns: 1fr !important; }
          footer > div > div:first-child { grid-template-columns: 1fr !important; }
          footer > div > div:first-child > div:last-child { gap: 24px !important; }
        }
        @media (max-width: 768px) {
          .nav-desktop-links { display: none !important; }
          .nav-mobile-toggle { display: block !important; }
          nav { padding-left: 20px !important; padding-right: 20px !important; }
          section { padding-top: 64px !important; padding-bottom: 64px !important; }
          footer { padding-top: 48px !important; padding-bottom: 32px !important; }
          section > div, footer > div { padding-left: 20px !important; padding-right: 20px !important; }
          #hero > div { padding: 56px 20px !important; }
          #workflow-grid > div { border-left: none !important; border-top: 1px solid #e4ddd2; padding: 36px 24px !important; }
          #workflow-grid > div:first-child { border-top: none !important; }
          #benefits-grid > div { border-right: none !important; padding: 28px 24px !important; }
        }
        @media (max-width: 480px) {
          nav { padding-left: 16px !important; padding-right: 16px !important; }
          section { padding-top: 48px !important; padding-bottom: 48px !important; }
          footer { padding-top: 40px !important; padding-bottom: 24px !important; }
          section > div, footer > div { padding-left: 16px !important; padding-right: 16px !important; }
          #hero > div { padding: 40px 16px !important; }
          #workflow-grid > div { padding: 28px 16px !important; }
          #benefits-grid > div { padding: 24px 16px !important; }
        }
        a:hover { opacity: 0.85; }
      `}</style>
    </>
  );
}
