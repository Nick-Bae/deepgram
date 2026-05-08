// Design Ref: §2.0 — Architecture Option C (Pragmatic): single page with co-located PanicCard helper.
// Plan SC: FR-01, FR-02, FR-03, FR-04, FR-05, FR-08, FR-09 — public, anchored, panic copy + scaffolds, no fetches.
import Head from "next/head";
import Link from "next/link";

const LAST_UPDATED = "2026-05-08";

const COLORS = {
  pageBg: "#ede5d8",
  cardBg: "rgba(255,255,255,0.62)",
  cardBorder: "rgba(120,98,78,0.12)",
  ink: "#22344c",
  body: "#3a3631",
  muted: "#5f6f86",
  accent: "#855763",
  accentSoft: "rgba(133,87,99,0.10)",
  comingSoonBg: "rgba(247,197,107,0.14)",
  comingSoonInk: "#946010",
  comingSoonBorder: "rgba(247,197,107,0.32)",
} as const;

type PanicCardProps = {
  title: string;
  symptom: string;
  steps: string[];
  escalation?: string;
};

// TODO: verify each panic case with the actual product behavior — drafted from
// codebase reading (mic permissions, WebSocket reconnect, room state, listener
// resolve). Author may correct in /pdca analyze help-center.
const PANIC_CASES: PanicCardProps[] = [
  {
    title: "The mic isn't picking up audio",
    symptom: "You started a broadcast but the translation feed shows no Korean text and no English appears for listeners.",
    steps: [
      "Look for a microphone icon in the browser address bar. Click it and confirm the site is allowed to use the microphone.",
      "If the dropdown shows the wrong device (e.g. laptop mic instead of your sound-board feed), pick the correct one and refresh the page.",
      "Speak into the mic — the broadcast panel should show audio activity. If it stays silent, your input is muted at the OS level. Check Windows / macOS sound settings → Input.",
      "If you just changed the mic in another app (OBS, Zoom), close that app — only one program can hold the device on some systems.",
      "Stop the broadcast, refresh the page, sign back in, and start broadcasting again.",
    ],
    escalation: "Still silent after a refresh? Switch browsers (Chrome works most reliably), then contact support with your church name and the time the issue started.",
  },
  {
    title: "Listeners can't connect to the live feed",
    symptom: "You shared the listener URL or QR code, but viewers see a 'connecting…' spinner or a 'service not found' message.",
    steps: [
      "Confirm a broadcast is actually running — the host dashboard should show the green 'Live' chip, not 'Standby'.",
      "Re-copy the listener URL using the Copy URL button on the host dashboard (do not type it from memory — service keys are case-sensitive).",
      "Ask the listener to refresh once. The page polls every 8 seconds for the active room, but a manual refresh skips the wait.",
      "If listeners are on church Wi-Fi, ask one to switch to mobile data as a test. Some church firewalls block WebSockets.",
      "Check the date/time on the listener device — a clock more than a few minutes off can break the secure connection.",
    ],
    escalation: "Still no connection? Open the listener URL on your own phone over mobile data. If it works for you, the issue is the listener's network. If it doesn't, contact support.",
  },
  {
    title: "Translation appears but listeners hear silence",
    symptom: "English text scrolls correctly on the listener page, but no audio plays through their headphones or speakers.",
    steps: [
      "Confirm the listener tapped the page once — most browsers (especially Safari and iOS) require a tap before playing audio.",
      "Check the listener's volume: device volume up, browser tab not muted (right-click the tab → Unmute), system output set to the right device (headphones vs speakers).",
      "If they are using AirPods or Bluetooth headphones, disconnect and reconnect them, then reload the listener page.",
      "Try a different browser on the listener device (Chrome on Android, Safari on iPhone are the safest).",
      "On iPhone, make sure the silent-mode switch on the side of the phone is OFF — even with the volume up, silent mode mutes media in some apps.",
    ],
    escalation: "If text streams but no audio plays after these steps, ask the listener to try a wired pair of headphones if available, then contact support.",
  },
  {
    title: "Translation feed froze mid-service",
    symptom: "Translation was working, then it stopped updating — Korean stopped advancing, or English stopped appearing.",
    steps: [
      "Check your internet connection on the host machine — a brief Wi-Fi drop can stall the stream. Wait 10 seconds; the system tries to reconnect automatically.",
      "Look at the host dashboard for a red error banner or a 'Server unreachable — reconnecting…' notice. If you see it, click Retry now.",
      "If the broadcast chip turned grey (Standby), the room ended. Click Start broadcast again to resume.",
      "If you've been live for more than 3 hours, the room may have hit the maximum duration. Stop and start a new broadcast.",
      "If you're on a Trial plan and ran out of minutes, the broadcast ends automatically. Check Billing & Subscription in your dashboard.",
    ],
    escalation: "If none of the above explains it, end the current room, refresh, start a new room, and contact support afterward with the time it froze.",
  },
];

function PanicCard({ title, symptom, steps, escalation }: PanicCardProps) {
  return (
    <article
      style={{
        background: COLORS.cardBg,
        border: `1px solid ${COLORS.cardBorder}`,
        borderRadius: 18,
        padding: "20px 22px",
        boxShadow: "0 10px 26px rgba(122,101,79,0.08), inset 0 1px 0 rgba(255,255,255,0.70)",
      }}
    >
      <h3 style={{ margin: 0, fontSize: 18, fontWeight: 700, color: COLORS.ink, letterSpacing: "-0.01em" }}>{title}</h3>
      <p style={{ margin: "8px 0 14px", fontSize: 14, color: COLORS.muted, lineHeight: 1.55 }}>{symptom}</p>
      <ol style={{ margin: 0, paddingLeft: 22, color: COLORS.body, fontSize: 15, lineHeight: 1.7, display: "grid", gap: 6 }}>
        {steps.map((step, idx) => (
          <li key={idx}>{step}</li>
        ))}
      </ol>
      {escalation ? (
        <p
          style={{
            margin: "14px 0 0",
            padding: "10px 14px",
            background: COLORS.accentSoft,
            borderRadius: 12,
            fontSize: 13.5,
            color: COLORS.accent,
            fontWeight: 600,
            lineHeight: 1.5,
          }}
        >
          {escalation}
        </p>
      ) : null}
    </article>
  );
}

function ComingSoon() {
  return (
    <p
      style={{
        margin: "12px 0 0",
        padding: "10px 14px",
        background: COLORS.comingSoonBg,
        border: `1px solid ${COLORS.comingSoonBorder}`,
        borderRadius: 12,
        fontSize: 13.5,
        color: COLORS.comingSoonInk,
        fontWeight: 600,
      }}
    >
      Coming soon — full guide in progress. Need help with this now? Use the Contact support link below.
    </p>
  );
}

const tocLinkStyle = {
  display: "inline-flex",
  alignItems: "center",
  padding: "8px 14px",
  borderRadius: 999,
  background: "rgba(255,255,255,0.62)",
  border: `1px solid ${COLORS.cardBorder}`,
  color: COLORS.ink,
  fontSize: 13,
  fontWeight: 700,
  textDecoration: "none",
  letterSpacing: "0.02em",
} as const;

const sectionHeadingStyle = {
  margin: "0 0 12px",
  fontSize: 22,
  fontWeight: 800,
  color: COLORS.ink,
  letterSpacing: "-0.015em",
  scrollMarginTop: 24,
} as const;

const introStyle = {
  margin: "0 0 6px",
  fontSize: 15,
  color: COLORS.body,
  lineHeight: 1.7,
} as const;

const contactCtaStyle = {
  display: "inline-flex",
  alignItems: "center",
  gap: 8,
  padding: "11px 18px",
  borderRadius: 999,
  background: COLORS.accent,
  color: "#fff",
  fontWeight: 700,
  fontSize: 14,
  textDecoration: "none",
  boxShadow: "0 10px 22px rgba(133,87,99,0.22)",
  letterSpacing: "0.02em",
} as const;

export default function HelpPage() {
  return (
    <>
      <Head>
        <title>Help — Worship Translation</title>
        <meta name="description" content="Worship Translation help center: Sunday-morning troubleshooting, setup walkthroughs, and contact support." />
      </Head>
      <main
        style={{
          minHeight: "100vh",
          background: COLORS.pageBg,
          color: COLORS.body,
          fontFamily: "'Manrope', 'Segoe UI', sans-serif",
          padding: "40px 16px 80px",
        }}
      >
        <div style={{ maxWidth: 800, margin: "0 auto", display: "grid", gap: 28 }}>
          {/* Top nav — home link + smart back */}
          <nav
            aria-label="Help page navigation"
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              flexWrap: "wrap" as const,
            }}
          >
            <Link
              href="/"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 8,
                textDecoration: "none",
                color: COLORS.ink,
                fontWeight: 800,
                fontSize: 14,
                letterSpacing: "-0.01em",
                padding: "8px 14px",
                borderRadius: 999,
                background: "rgba(255,255,255,0.62)",
                border: `1px solid ${COLORS.cardBorder}`,
              }}
            >
              <span style={{ color: COLORS.accent }}>←</span>
              <span>Worship Translation</span>
            </Link>
            <button
              type="button"
              onClick={() => {
                if (typeof window !== "undefined") {
                  if (window.history.length > 1) window.history.back();
                  else window.location.href = "/";
                }
              }}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "8px 14px",
                borderRadius: 999,
                background: "transparent",
                border: `1px solid ${COLORS.cardBorder}`,
                color: COLORS.muted,
                fontSize: 13,
                fontWeight: 700,
                cursor: "pointer",
                fontFamily: "inherit",
              }}
            >
              Back
            </button>
          </nav>

          {/* Page header */}
          <header style={{ display: "grid", gap: 6 }}>
            <span style={{ fontSize: 11, fontWeight: 800, letterSpacing: "0.22em", textTransform: "uppercase" as const, color: COLORS.accent }}>
              Worship Translation · Help
            </span>
            <h1 style={{ margin: 0, fontSize: 34, fontWeight: 800, color: COLORS.ink, letterSpacing: "-0.02em", lineHeight: 1.1 }}>
              Help Center
            </h1>
            <p style={{ margin: 0, fontSize: 13, color: COLORS.muted }}>Last updated: {LAST_UPDATED}</p>
          </header>

          {/* Table of contents */}
          <nav aria-label="Table of contents" style={{ display: "flex", flexWrap: "wrap" as const, gap: 8 }}>
            <a href="#panic" style={tocLinkStyle}>Sunday panic</a>
            <a href="#setup" style={tocLinkStyle}>First-time setup</a>
            <a href="#display" style={tocLinkStyle}>Display &amp; OBS</a>
            <a href="#billing" style={tocLinkStyle}>Billing</a>
          </nav>

          {/* Section 1 — Sunday-morning panic guide */}
          <section id="panic" aria-labelledby="panic-heading" style={{ display: "grid", gap: 16, scrollMarginTop: 24 }}>
            <h2 id="panic-heading" style={sectionHeadingStyle}>Sunday-morning panic guide</h2>
            <p style={introStyle}>
              You&rsquo;re minutes from a service and something just broke. Find the closest match below and try the steps in order.
              If a step doesn&rsquo;t apply, skip to the next one. Each card ends with what to do if you&rsquo;re still stuck.
            </p>
            {PANIC_CASES.map((c) => (
              <PanicCard key={c.title} {...c} />
            ))}
            {/* Inline contact CTA after panic section */}
            <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" as const, marginTop: 4 }}>
              <span style={{ fontSize: 14, color: COLORS.muted }}>None of these match? We can help.</span>
              <Link href="/contact" style={contactCtaStyle}>Contact support</Link>
            </div>
          </section>

          {/* Section 2 — First-time setup */}
          <section id="setup" aria-labelledby="setup-heading" style={{ display: "grid", gap: 6, scrollMarginTop: 24 }}>
            <h2 id="setup-heading" style={sectionHeadingStyle}>First-time setup</h2>
            <p style={introStyle}>
              How to get from a brand-new account to broadcasting your first service: create your church, add a service, start the
              broadcast, and share the listener URL with your congregation.
            </p>
            <ComingSoon />
          </section>

          {/* Section 3 — Display / OBS / projector / PPT */}
          <section id="display" aria-labelledby="display-heading" style={{ display: "grid", gap: 6, scrollMarginTop: 24 }}>
            <h2 id="display-heading" style={sectionHeadingStyle}>Display, OBS, projector, and PPT subtitles</h2>
            <p style={introStyle}>
              Where to point your projector, how to add the translation feed as an OBS browser source, and how to overlay subtitles
              on a slideshow during the sermon.
            </p>
            <ComingSoon />
          </section>

          {/* Section 4 — Billing & plans */}
          <section id="billing" aria-labelledby="billing-heading" style={{ display: "grid", gap: 6, scrollMarginTop: 24 }}>
            <h2 id="billing-heading" style={sectionHeadingStyle}>Billing &amp; plans</h2>
            <p style={introStyle}>
              How trial minutes work, what each plan includes, how to upgrade, and what happens when you reach a usage cap.
            </p>
            <ComingSoon />
          </section>

          {/* Footer CTA */}
          <footer
            style={{
              marginTop: 12,
              padding: "22px 24px",
              borderRadius: 22,
              background: "rgba(255,255,255,0.50)",
              border: `1px solid ${COLORS.cardBorder}`,
              boxShadow: "0 10px 22px rgba(122,101,79,0.08), inset 0 1px 0 rgba(255,255,255,0.78)",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 18,
              flexWrap: "wrap" as const,
            }}
          >
            <div style={{ minWidth: 0 }}>
              <p style={{ margin: 0, fontSize: 16, fontWeight: 800, color: COLORS.ink }}>Still stuck?</p>
              <p style={{ margin: "4px 0 0", fontSize: 14, color: COLORS.muted, lineHeight: 1.5 }}>
                Send us the details and we&rsquo;ll get back to you. Email reply, no extra account required.
              </p>
            </div>
            <Link href="/contact" style={contactCtaStyle}>Contact support</Link>
          </footer>
        </div>
      </main>
    </>
  );
}
