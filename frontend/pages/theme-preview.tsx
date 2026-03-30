import Head from "next/head";
import Link from "next/link";

const P = {
  cloud: "#F2F3F4",
  sand: "#DED1C6",
  rose: "#A77693",
  navy: "#174871",
  deep: "#0F2D4D",
};

const paletteCards = [
  {
    label: "Gradient 01",
    top: P.cloud,
    bottom: P.sand,
    text: P.navy,
    chips: [P.cloud, P.sand],
  },
  {
    label: "Gradient 02",
    top: P.sand,
    bottom: P.rose,
    text: P.deep,
    chips: [P.sand, P.rose],
  },
  {
    label: "Gradient 03",
    top: P.rose,
    bottom: P.navy,
    text: P.cloud,
    chips: [P.rose, P.navy],
  },
  {
    label: "Gradient 04",
    top: P.sand,
    middle: P.rose,
    bottom: P.navy,
    text: P.cloud,
    chips: [P.sand, P.rose, P.navy],
  },
  {
    label: "Gradient 05",
    top: P.navy,
    bottom: P.deep,
    text: P.cloud,
    chips: [P.navy, P.deep],
  },
];

const statCards = [
  { label: "Source", value: "Korean", tone: "light" },
  { label: "Target", value: "English", tone: "light" },
  { label: "Producer", value: "Connected", tone: "dark" },
];

const plans = [
  { name: "Starter", price: "$20", note: "Smaller churches", current: false },
  { name: "Growth", price: "$40", note: "Weekly broadcast rhythm", current: true },
  { name: "Premium", price: "$60", note: "Multi-service coverage", current: false },
];

const surfaceGlass = {
  border: "1px solid rgba(255,255,255,0.14)",
  background: "linear-gradient(180deg, rgba(255,255,255,0.10), rgba(255,255,255,0.04))",
  boxShadow: "0 28px 70px rgba(5,18,32,0.30), inset 0 1px 0 rgba(255,255,255,0.12)",
  backdropFilter: "blur(26px)",
  WebkitBackdropFilter: "blur(26px)",
} as const;

const lightPanel = {
  border: "1px solid rgba(255,255,255,0.55)",
  background: "linear-gradient(180deg, rgba(242,243,244,0.94), rgba(222,209,198,0.84))",
  boxShadow: "0 20px 40px rgba(10,34,56,0.12), inset 0 1px 0 rgba(255,255,255,0.9)",
} as const;

const darkPanel = {
  border: "1px solid rgba(255,255,255,0.10)",
  background: "linear-gradient(180deg, rgba(23,72,113,0.96), rgba(15,45,77,0.98))",
  boxShadow: "0 24px 48px rgba(5,18,32,0.28), inset 0 1px 0 rgba(255,255,255,0.08)",
} as const;

function PaletteRail() {
  return (
    <section style={{ display: "grid", gap: 18 }}>
      <div style={{ display: "grid", gap: 6 }}>
        <p style={{ margin: 0, color: "rgba(242,243,244,0.68)", fontSize: 12, letterSpacing: "0.22em", textTransform: "uppercase" }}>Palette Study</p>
        <h2 style={{ margin: 0, color: P.cloud, fontFamily: "'Cormorant Garamond', Georgia, serif", fontSize: "clamp(34px, 5vw, 56px)", lineHeight: 0.96 }}>
          Before changing the live app, preview the color system in context.
        </h2>
        <p style={{ margin: 0, maxWidth: 720, color: "rgba(242,243,244,0.74)", fontSize: 15, lineHeight: 1.7 }}>
          This page applies your uploaded palette to realistic host-dashboard UI blocks so you can judge the tone, readability, and brand feel first.
        </p>
      </div>

      <div className="preview-swatches" style={{ display: "grid", gap: 16, gridTemplateColumns: "repeat(5, minmax(0, 1fr))" }}>
        {paletteCards.map((card) => {
          const gradient = card.middle
            ? `linear-gradient(180deg, ${card.top} 0%, ${card.middle} 36%, ${card.bottom} 100%)`
            : `linear-gradient(180deg, ${card.top} 0%, ${card.bottom} 100%)`;
          return (
            <article
              key={card.label}
              style={{
                minHeight: 420,
                padding: "18px 16px",
                borderRadius: 24,
                background: gradient,
                color: card.text,
                boxShadow: "0 22px 50px rgba(6,18,32,0.18), inset 0 1px 0 rgba(255,255,255,0.4)",
                display: "grid",
                alignContent: "space-between",
              }}
            >
              <div style={{ display: "grid", gap: 10 }}>
                {card.chips.map((chip) => (
                  <span key={`${card.label}:${chip}`} style={{ fontSize: 13, fontWeight: 800, letterSpacing: "0.16em", textTransform: "uppercase" }}>
                    {chip}
                  </span>
                ))}
              </div>
              <div style={{ display: "grid", placeItems: "center" }}>
                <div style={{ transform: "rotate(-90deg)", whiteSpace: "nowrap", fontSize: 34, fontWeight: 900, letterSpacing: "-0.05em" }}>
                  {card.label}
                </div>
              </div>
              <div style={{ fontSize: 13, fontWeight: 800, letterSpacing: "0.16em", textTransform: "uppercase" }}>
                {card.chips[card.chips.length - 1]}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function HostDashboardPreview() {
  return (
    <section style={{ display: "grid", gap: 18 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 18, alignItems: "end", flexWrap: "wrap" }}>
        <div style={{ display: "grid", gap: 6 }}>
          <p style={{ margin: 0, color: "rgba(242,243,244,0.68)", fontSize: 12, letterSpacing: "0.22em", textTransform: "uppercase" }}>Host Dashboard Sample</p>
          <h3 style={{ margin: 0, color: P.cloud, fontSize: "clamp(28px, 4vw, 42px)", lineHeight: 1, letterSpacing: "-0.04em" }}>
            Cooler, branded, and more atmospheric than the current warm theme.
          </h3>
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <span style={{ borderRadius: 999, padding: "8px 12px", background: "rgba(242,243,244,0.10)", color: P.cloud, fontSize: 13, fontWeight: 700 }}>Good for login + dashboard</span>
          <span style={{ borderRadius: 999, padding: "8px 12px", background: "rgba(167,118,147,0.18)", color: P.cloud, fontSize: 13, fontWeight: 700 }}>Rose works best as accent</span>
        </div>
      </div>

      <article style={{ ...surfaceGlass, borderRadius: 34, padding: 20, display: "grid", gap: 18 }}>
        <header style={{ ...surfaceGlass, borderRadius: 24, padding: "14px 18px", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 18, flexWrap: "wrap" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
            <div style={{ width: 44, height: 44, borderRadius: 14, background: `linear-gradient(180deg, ${P.navy}, ${P.deep})`, color: P.cloud, display: "grid", placeItems: "center", fontWeight: 900 }}>E</div>
            <div>
              <div style={{ color: P.cloud, fontSize: 18, fontWeight: 800 }}>Email Test</div>
              <div style={{ color: "rgba(242,243,244,0.58)", fontSize: 10, letterSpacing: "0.32em", textTransform: "uppercase" }}>Translation Studio</div>
            </div>
          </div>
          <nav style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            {["Live Broadcast", "Church Settings", "Billing & Subscription", "Team"].map((item, index) => (
              <span
                key={item}
                style={{
                  borderRadius: 999,
                  padding: "8px 14px",
                  background: index === 0 ? `linear-gradient(180deg, ${P.cloud}, ${P.sand})` : "rgba(242,243,244,0.08)",
                  color: index === 0 ? P.deep : "rgba(242,243,244,0.76)",
                  fontWeight: 700,
                  fontSize: 13,
                }}
              >
                {item}
              </span>
            ))}
          </nav>
        </header>

        <div className="preview-two-col" style={{ display: "grid", gap: 18, gridTemplateColumns: "minmax(0, 1.5fr) minmax(320px, 0.9fr)" }}>
          <div style={{ display: "grid", gap: 18 }}>
            <div style={{ ...lightPanel, borderRadius: 28, padding: 22, display: "grid", gap: 18 }}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
                <div>
                  <p style={{ margin: 0, color: P.rose, fontSize: 11, letterSpacing: "0.22em", textTransform: "uppercase", fontWeight: 800 }}>Live Translation</p>
                  <h4 style={{ margin: "8px 0 0", color: P.deep, fontFamily: "'Cormorant Garamond', Georgia, serif", fontSize: 38, lineHeight: 0.94 }}>
                    Real-Time Sermon Translation
                  </h4>
                </div>
                <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                  {statCards.map((stat) => (
                    <div
                      key={stat.label}
                      style={{
                        borderRadius: 999,
                        padding: "10px 14px",
                        background: stat.tone === "dark" ? `linear-gradient(180deg, ${P.navy}, ${P.deep})` : "rgba(255,255,255,0.74)",
                        color: stat.tone === "dark" ? P.cloud : P.deep,
                        fontSize: 13,
                        fontWeight: 700,
                        boxShadow: "0 12px 24px rgba(15,45,77,0.10)",
                      }}
                    >
                      {stat.label} · {stat.value}
                    </div>
                  ))}
                </div>
              </div>

              <div className="preview-pairs" style={{ display: "grid", gap: 16, gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
                <div style={{ borderRadius: 20, padding: 18, background: "rgba(255,255,255,0.64)", border: "1px solid rgba(15,45,77,0.08)" }}>
                  <div style={{ color: P.rose, fontSize: 11, letterSpacing: "0.2em", textTransform: "uppercase", fontWeight: 800 }}>Source Language</div>
                  <div style={{ marginTop: 14, color: P.deep, fontSize: 22, fontWeight: 800 }}>Korean</div>
                </div>
                <div style={{ borderRadius: 20, padding: 18, background: "rgba(255,255,255,0.64)", border: "1px solid rgba(15,45,77,0.08)" }}>
                  <div style={{ color: P.rose, fontSize: 11, letterSpacing: "0.2em", textTransform: "uppercase", fontWeight: 800 }}>Target Language</div>
                  <div style={{ marginTop: 14, color: P.deep, fontSize: 22, fontWeight: 800 }}>English</div>
                </div>
              </div>

              <div className="preview-console" style={{ display: "grid", gap: 16, gridTemplateColumns: "1fr 1fr 280px" }}>
                <div style={{ borderRadius: 22, padding: 18, background: "rgba(255,255,255,0.58)", border: "1px solid rgba(15,45,77,0.08)", display: "grid", gap: 14 }}>
                  <div>
                    <div style={{ color: P.rose, fontSize: 11, letterSpacing: "0.2em", textTransform: "uppercase", fontWeight: 800 }}>Live Audio Stream</div>
                    <div style={{ marginTop: 6, color: P.deep, fontWeight: 700 }}>KR Korean</div>
                  </div>
                  <div style={{ minHeight: 150, borderRadius: 16, background: "rgba(242,243,244,0.85)", border: "1px solid rgba(15,45,77,0.08)", padding: 14, color: "#6c7385", lineHeight: 1.7 }}>
                    Listening to the speaker in Korean...
                  </div>
                  <button style={{ borderRadius: 999, border: "none", padding: "16px 18px", background: `linear-gradient(180deg, ${P.rose}, #8d6380)`, color: P.cloud, fontWeight: 900, fontSize: 14 }}>
                    Start Translation
                  </button>
                </div>

                <div style={{ borderRadius: 22, padding: 18, background: `linear-gradient(180deg, rgba(167,118,147,0.12), rgba(23,72,113,0.08))`, border: "1px solid rgba(15,45,77,0.08)", display: "grid", gap: 14 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "center" }}>
                    <div>
                      <div style={{ color: P.rose, fontSize: 11, letterSpacing: "0.2em", textTransform: "uppercase", fontWeight: 800 }}>Translation Output</div>
                      <div style={{ marginTop: 6, color: P.deep, fontWeight: 700 }}>US English</div>
                    </div>
                    <span style={{ borderRadius: 999, padding: "7px 12px", background: "rgba(255,255,255,0.68)", color: P.navy, fontSize: 11, fontWeight: 800, letterSpacing: "0.16em", textTransform: "uppercase" }}>
                      Broadcast Ready
                    </span>
                  </div>
                  <div style={{ minHeight: 112, color: P.deep, fontSize: 26, lineHeight: 1.2, fontWeight: 800 }}>
                    Grace and peace to all who joined this morning.
                  </div>
                  <div style={{ borderRadius: 16, background: "rgba(255,255,255,0.72)", border: "1px solid rgba(15,45,77,0.08)", padding: 14 }}>
                    <div style={{ color: P.rose, fontSize: 11, letterSpacing: "0.2em", textTransform: "uppercase", fontWeight: 800 }}>Next Sentence Preview</div>
                    <div style={{ marginTop: 8, color: "#61697a" }}>Waiting for the next clause...</div>
                  </div>
                </div>

                <aside style={{ ...darkPanel, borderRadius: 22, padding: 18, color: P.cloud, display: "grid", gap: 16 }}>
                  <div style={{ display: "grid", gap: 6 }}>
                    <div style={{ fontSize: 16, fontWeight: 800 }}>Broadcast Output</div>
                    <div style={{ color: "rgba(242,243,244,0.64)", fontSize: 12 }}>Enable or mute stage and display feeds.</div>
                  </div>
                  <div style={{ borderRadius: 999, height: 42, width: 78, justifySelf: "end", background: "rgba(242,243,244,0.14)", padding: 4, display: "grid", alignItems: "center" }}>
                    <div style={{ width: 34, height: 34, borderRadius: "50%", background: P.cloud, marginLeft: "auto", boxShadow: "0 10px 18px rgba(0,0,0,0.18)" }} />
                  </div>
                  <div style={{ borderTop: "1px solid rgba(242,243,244,0.10)", paddingTop: 14, display: "grid", gap: 10 }}>
                    {[
                      ["Last heartbeat", "6s ago"],
                      ["Reconnect attempt", "0"],
                      ["Socket downtime", "0s"],
                      ["Deepgram engine", "idle"],
                    ].map(([label, value]) => (
                      <div key={label} style={{ display: "flex", justifyContent: "space-between", gap: 12, fontSize: 13 }}>
                        <span style={{ color: "rgba(242,243,244,0.66)" }}>{label}</span>
                        <strong style={{ color: P.cloud }}>{value}</strong>
                      </div>
                    ))}
                  </div>
                </aside>
              </div>
            </div>
          </div>

          <div style={{ display: "grid", gap: 18 }}>
            <section style={{ ...lightPanel, borderRadius: 28, padding: 22, display: "grid", gap: 16 }}>
              <div>
                <p style={{ margin: 0, color: P.rose, fontSize: 11, letterSpacing: "0.2em", textTransform: "uppercase", fontWeight: 800 }}>Billing Preview</p>
                <h4 style={{ margin: "8px 0 0", color: P.deep, fontSize: 28, lineHeight: 1, letterSpacing: "-0.04em" }}>Plans in one row feel stronger in this palette.</h4>
              </div>
              <div style={{ display: "grid", gap: 12 }}>
                {plans.map((plan) => (
                  <div
                    key={plan.name}
                    style={{
                      borderRadius: 20,
                      padding: 16,
                      background: plan.current ? `linear-gradient(180deg, rgba(167,118,147,0.16), rgba(23,72,113,0.10))` : "rgba(255,255,255,0.7)",
                      border: plan.current ? `2px solid rgba(167,118,147,0.42)` : "1px solid rgba(15,45,77,0.08)",
                      display: "grid",
                      gap: 6,
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center" }}>
                      <strong style={{ color: P.deep, fontSize: 18 }}>{plan.name}</strong>
                      {plan.current ? (
                        <span style={{ borderRadius: 999, padding: "6px 10px", background: `linear-gradient(180deg, ${P.navy}, ${P.deep})`, color: P.cloud, fontSize: 11, fontWeight: 800, letterSpacing: "0.14em", textTransform: "uppercase" }}>
                          Current
                        </span>
                      ) : null}
                    </div>
                    <div style={{ color: P.navy, fontWeight: 900, fontSize: 28, letterSpacing: "-0.05em" }}>{plan.price}</div>
                    <div style={{ color: "#657085", fontSize: 13 }}>{plan.note}</div>
                  </div>
                ))}
              </div>
            </section>

            <section style={{ ...darkPanel, borderRadius: 28, padding: 22, color: P.cloud, display: "grid", gap: 14 }}>
              <p style={{ margin: 0, color: "rgba(242,243,244,0.68)", fontSize: 11, letterSpacing: "0.2em", textTransform: "uppercase", fontWeight: 800 }}>Recommendation</p>
              <h4 style={{ margin: 0, fontSize: 30, lineHeight: 1.02, letterSpacing: "-0.05em" }}>
                Strong enough for login, dashboard, billing, and empty states.
              </h4>
              <p style={{ margin: 0, color: "rgba(242,243,244,0.74)", fontSize: 14, lineHeight: 1.7 }}>
                Keep `#A77693` as the accent, not the dominant surface color. Let the blues carry structure and depth, and use the warm neutrals for readable content panels.
              </p>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                <Link href="/login" style={{ borderRadius: 999, padding: "12px 16px", textDecoration: "none", background: `linear-gradient(180deg, ${P.cloud}, ${P.sand})`, color: P.deep, fontWeight: 800 }}>
                  Back to Login
                </Link>
                <Link href="/host/index" style={{ borderRadius: 999, padding: "12px 16px", textDecoration: "none", background: "rgba(242,243,244,0.10)", color: P.cloud, fontWeight: 800, border: "1px solid rgba(242,243,244,0.16)" }}>
                  Existing Host App
                </Link>
              </div>
            </section>
          </div>
        </div>
      </article>
    </section>
  );
}

export default function ThemePreviewPage() {
  return (
    <>
      <Head>
        <title>Theme Preview | Worship</title>
        <meta
          name="description"
          content="Standalone preview for the proposed cooler blue-rose theme palette before applying it to the live application."
        />
      </Head>
      <style>{`
        @media (max-width: 1180px) {
          .preview-swatches { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
          .preview-two-col { grid-template-columns: 1fr !important; }
          .preview-console { grid-template-columns: 1fr !important; }
        }
        @media (max-width: 760px) {
          .preview-swatches { grid-template-columns: 1fr !important; }
          .preview-pairs { grid-template-columns: 1fr !important; }
        }
      `}</style>

      <main
        style={{
          minHeight: "100vh",
          padding: "28px 18px 48px",
          background: `radial-gradient(circle at 12% 16%, rgba(242,243,244,0.22), transparent 28%), radial-gradient(circle at 88% 22%, rgba(167,118,147,0.28), transparent 30%), linear-gradient(180deg, #2b6287 0%, ${P.navy} 38%, ${P.deep} 100%)`,
          position: "relative",
          overflow: "hidden",
          fontFamily: "'Avenir Next', 'Segoe UI', sans-serif",
        }}
      >
        <div aria-hidden="true" style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
          <div style={{ position: "absolute", left: "-6%", bottom: -80, width: 560, height: 260, background: "linear-gradient(180deg, rgba(242,243,244,0.12), rgba(15,45,77,0.02))", clipPath: "polygon(0 100%, 24% 54%, 42% 70%, 60% 42%, 80% 68%, 100% 24%, 100% 100%)" }} />
          <div style={{ position: "absolute", right: "-4%", bottom: -60, width: 620, height: 280, background: "linear-gradient(180deg, rgba(242,243,244,0.08), rgba(15,45,77,0.02))", clipPath: "polygon(0 100%, 20% 56%, 40% 76%, 58% 38%, 76% 62%, 100% 44%, 100% 100%)" }} />
        </div>

        <section style={{ position: "relative", zIndex: 1, maxWidth: 1440, margin: "0 auto", display: "grid", gap: 32 }}>
          <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 18, flexWrap: "wrap" }}>
            <div style={{ display: "grid", gap: 6 }}>
              <p style={{ margin: 0, color: "rgba(242,243,244,0.72)", fontSize: 12, letterSpacing: "0.22em", textTransform: "uppercase" }}>Preview Route</p>
              <h1 style={{ margin: 0, color: P.cloud, fontSize: "clamp(30px, 5vw, 58px)", lineHeight: 0.92, letterSpacing: "-0.06em" }}>
                Blue-Rose Theme Preview
              </h1>
            </div>
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
              <Link href="/" style={{ borderRadius: 999, padding: "12px 16px", textDecoration: "none", background: "rgba(242,243,244,0.10)", color: P.cloud, fontWeight: 800, border: "1px solid rgba(242,243,244,0.16)" }}>
                Back Home
              </Link>
              <Link href="/login" style={{ borderRadius: 999, padding: "12px 16px", textDecoration: "none", background: `linear-gradient(180deg, ${P.cloud}, ${P.sand})`, color: P.deep, fontWeight: 800 }}>
                Current Login
              </Link>
            </div>
          </header>

          <PaletteRail />
          <HostDashboardPreview />
        </section>
      </main>
    </>
  );
}
