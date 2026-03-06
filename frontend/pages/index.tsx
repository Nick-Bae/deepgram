import Head from "next/head";
import Link from "next/link";

const cards = [
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
  {
    title: "Listener View",
    desc: "Public listener page for live translated subtitles.",
    href: "/c/demo/s/sun-11am",
    cta: "Open demo listener",
    tone: "#a78bfa",
  },
];

export default function HomePage() {
  return (
    <>
      <Head>
        <title>Real-Time Translation</title>
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
              Real-Time Church Translation
            </p>
            <h1 style={{ marginTop: 8, marginBottom: 8, fontSize: "clamp(28px,5vw,44px)", lineHeight: 1.05 }}>Main Home</h1>
            <p style={{ marginTop: 0, marginBottom: 0, opacity: 0.88, maxWidth: 760 }}>
              Choose your entry point: host management, account creation, invite-based joining, or listener playback.
            </p>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 14 }}>
              <Link
                href="/login"
                style={{
                  borderRadius: 10,
                  background: "#22c55e",
                  color: "#052e16",
                  fontWeight: 700,
                  padding: "8px 12px",
                }}
              >
                Host login
              </Link>
              <Link
                href="/signup"
                style={{
                  borderRadius: 10,
                  background: "#e2e8f0",
                  color: "#0f172a",
                  fontWeight: 700,
                  padding: "8px 12px",
                }}
              >
                Create church
              </Link>
            </div>
          </header>

          <section style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" }}>
            {cards.map((card) => (
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
          </section>
        </section>
      </main>
    </>
  );
}
