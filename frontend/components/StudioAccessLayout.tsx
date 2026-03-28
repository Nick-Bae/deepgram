import Head from "next/head";
import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";

export type StudioAccessAction = {
  href: string;
  label: string;
  accent?: boolean;
};

export type StudioAccessInfoItem = {
  title: string;
  description: string;
};

type StudioAccessLayoutProps = {
  pageTitle: string;
  pageDescription: string;
  panelEyebrow: string;
  panelTitle: string;
  panelDescription: string;
  infoEyebrow: string;
  infoTitle: string;
  infoDescription: string;
  infoItems: StudioAccessInfoItem[];
  headerActions?: StudioAccessAction[];
  children: ReactNode;
};

export const studioPageStyle: CSSProperties = {
  minHeight: "100vh",
  background: "#ede5d8",
  color: "#2e2a28",
  padding: "28px 16px 40px",
  fontFamily: "'Avenir Next', 'Segoe UI', sans-serif",
  position: "relative",
};

export const studioCardStyle: CSSProperties = {
  position: "relative",
  overflow: "hidden",
  border: "1px solid rgba(255,255,255,0.68)",
  background: "linear-gradient(180deg, rgba(255,255,255,0.18), rgba(255,255,255,0.10))",
  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.90), 0 28px 64px rgba(122,101,79,0.18)",
  backdropFilter: "blur(40px)",
  WebkitBackdropFilter: "blur(40px)",
};

export const studioInnerCardStyle: CSSProperties = {
  position: "relative",
  overflow: "hidden",
  border: "1px solid rgba(255,255,255,0.55)",
  background: "rgba(255,255,255,0.38)",
  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.75), 0 8px 20px rgba(122,101,79,0.06)",
  backdropFilter: "blur(14px)",
  WebkitBackdropFilter: "blur(14px)",
};

export const studioLabelStyle: CSSProperties = {
  display: "grid",
  gap: 6,
};

export const studioLabelTextStyle: CSSProperties = {
  fontSize: 13,
  color: "#7d746c",
};

export const studioFieldStyle: CSSProperties = {
  borderRadius: 12,
  border: "1px solid rgba(120,98,78,0.14)",
  background: "rgba(255,255,255,0.60)",
  color: "#26211f",
  padding: "11px 12px",
  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.60)",
};

export const studioTextAreaStyle: CSSProperties = {
  ...studioFieldStyle,
  padding: "12px 14px",
  resize: "vertical",
};

export const studioHelperTextStyle: CSSProperties = {
  fontSize: 12,
  color: "#7d746c",
};

export const studioSubtleTextStyle: CSSProperties = {
  fontSize: 13,
  color: "#6d645c",
  lineHeight: 1.7,
};

export function buildStudioButtonStyle(options?: {
  tone?: "primary" | "secondary" | "danger";
  disabled?: boolean;
  compact?: boolean;
}): CSSProperties {
  const tone = options?.tone || "primary";
  const disabled = Boolean(options?.disabled);
  const compact = Boolean(options?.compact);
  const padding = compact ? "8px 12px" : "12px 16px";

  if (tone === "secondary") {
    return {
      borderRadius: compact ? 12 : 999,
      border: "1px solid rgba(120,98,78,0.16)",
      background: "rgba(255,255,255,0.55)",
      color: "#4a4038",
      fontWeight: 700,
      padding,
      cursor: disabled ? "not-allowed" : "pointer",
      opacity: disabled ? 0.6 : 1,
      boxShadow: "0 8px 18px rgba(122,101,79,0.08)",
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
    };
  }

  if (tone === "danger") {
    return {
      borderRadius: compact ? 12 : 999,
      border: "1px solid rgba(188,95,111,0.3)",
      background: "linear-gradient(145deg, #e38888, #bc5f6f)",
      color: "#f8fafc",
      fontWeight: 800,
      padding,
      cursor: disabled ? "not-allowed" : "pointer",
      opacity: disabled ? 0.6 : 1,
      boxShadow: "0 14px 28px rgba(188,95,111,0.22)",
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
    };
  }

  return {
    borderRadius: compact ? 12 : 999,
    border: "none",
    background: "#c5a263",
    color: "#ffffff",
    fontWeight: 700,
    padding,
    cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? 0.6 : 1,
    boxShadow: "0 14px 30px rgba(110,93,74,0.22)",
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    letterSpacing: "0.02em",
  };
}

export function buildStudioNoticeStyle(tone: "error" | "success" | "info"): CSSProperties {
  if (tone === "error") {
    return {
      margin: 0,
      borderRadius: 14,
      padding: "12px 14px",
      background: "rgba(188,95,111,0.10)",
      border: "1px solid rgba(188,95,111,0.22)",
      color: "#8a2720",
      fontSize: 14,
    };
  }
  if (tone === "success") {
    return {
      margin: 0,
      borderRadius: 14,
      padding: "12px 14px",
      background: "rgba(91,179,130,0.10)",
      border: "1px solid rgba(91,179,130,0.22)",
      color: "#2f6d4f",
      fontSize: 14,
    };
  }
  return {
    margin: 0,
    borderRadius: 14,
    padding: "12px 14px",
    background: "rgba(198,165,109,0.12)",
    border: "1px solid rgba(198,165,109,0.30)",
    color: "#7a5c20",
    fontSize: 14,
  };
}

export const studioChipStyle: CSSProperties = {
  borderRadius: 999,
  border: "1px solid rgba(198,165,109,0.40)",
  background: "rgba(198,165,109,0.12)",
  color: "#8c6830",
  fontSize: 12,
  fontWeight: 700,
  padding: "5px 10px",
};

export const studioReadOnlyCardStyle: CSSProperties = {
  ...studioInnerCardStyle,
  borderRadius: 18,
  padding: "14px 16px",
};

function renderHeaderAction(action: StudioAccessAction) {
  const baseStyle = action.accent
    ? buildStudioButtonStyle()
    : {
        borderRadius: 999 as const,
        border: "1px solid rgba(120,98,78,0.16)",
        background: "rgba(255,255,255,0.50)",
        color: "#4a4038",
        fontWeight: 700,
        padding: "9px 18px",
        fontSize: 13,
        display: "inline-flex" as const,
        alignItems: "center" as const,
        justifyContent: "center" as const,
        boxShadow: "none",
      };
  return (
    <Link
      key={`${action.href}:${action.label}`}
      href={action.href}
      style={baseStyle}
    >
      {action.label}
    </Link>
  );
}

export default function StudioAccessLayout({
  pageTitle,
  pageDescription,
  panelEyebrow,
  panelTitle,
  panelDescription,
  infoEyebrow,
  infoTitle,
  infoDescription,
  infoItems,
  headerActions = [],
  children,
}: StudioAccessLayoutProps) {
  return (
    <>
      <Head>
        <title>{pageTitle}</title>
        <meta name="description" content={pageDescription} />
      </Head>
      <main style={studioPageStyle}>
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
          {/* Glass header */}
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
              flexWrap: "wrap" as const,
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
                <p style={{ margin: 0, fontSize: 10, letterSpacing: "0.38em", textTransform: "uppercase" as const, color: "#7d746c" }}>
                  Worship
                </p>
                <p style={{ margin: "3px 0 0", fontSize: 17, fontWeight: 600, letterSpacing: "-0.02em", color: "#1d1917", lineHeight: 1 }}>
                  Translation Studio
                </p>
              </div>
            </div>

            {headerActions.length ? (
              <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 10 }}>
                {headerActions.map(renderHeaderAction)}
              </div>
            ) : null}
          </header>

          {/* Two-column content */}
          <section
            style={{
              display: "grid",
              gap: 18,
              gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 320px), 1fr))",
              alignItems: "stretch",
            }}
          >
            {/* Left: info panel */}
            <aside
              style={{
                ...studioCardStyle,
                borderRadius: 30,
                padding: 22,
                display: "grid",
                gap: 16,
                height: "100%",
                alignContent: "start",
              }}
            >
              <div style={{ ...studioInnerCardStyle, borderRadius: 22, padding: "16px 18px" }}>
                <p style={{ margin: 0, fontSize: 11, color: "#b08b4f", letterSpacing: "0.24em", textTransform: "uppercase" as const, fontWeight: 800 }}>
                  {infoEyebrow}
                </p>
                <h2 style={{ margin: "10px 0 0", fontSize: 28, lineHeight: 1.05, letterSpacing: "-0.03em", color: "#1d1917" }}>{infoTitle}</h2>
              </div>
              <p style={{ margin: 0, fontSize: 15, lineHeight: 1.8, color: "#6d645c" }}>{infoDescription}</p>
              <div style={{ display: "grid", gap: 12 }}>
                {infoItems.map((item) => (
                  <div key={item.title} style={{ ...studioInnerCardStyle, borderRadius: 18, padding: "14px 16px" }}>
                    <p style={{ margin: 0, fontSize: 15, fontWeight: 700, color: "#1d1917" }}>{item.title}</p>
                    <p style={{ margin: "6px 0 0", fontSize: 14, lineHeight: 1.7, color: "#6d645c" }}>{item.description}</p>
                  </div>
                ))}
              </div>
            </aside>

            {/* Right: form panel */}
            <article
              style={{
                ...studioCardStyle,
                borderRadius: 30,
                padding: 22,
                display: "grid",
                gap: 16,
                height: "100%",
                alignContent: "start",
              }}
            >
              <div style={{ ...studioInnerCardStyle, borderRadius: 22, padding: "16px 18px" }}>
                <p style={{ margin: 0, fontSize: 11, color: "#b08b4f", letterSpacing: "0.24em", textTransform: "uppercase" as const, fontWeight: 800 }}>
                  {panelEyebrow}
                </p>
                <h2 style={{ margin: "10px 0 0", fontSize: 28, lineHeight: 1.05, letterSpacing: "-0.03em", color: "#1d1917" }}>{panelTitle}</h2>
                <p style={{ margin: "8px 0 0", fontSize: 14, lineHeight: 1.7, color: "#6d645c" }}>{panelDescription}</p>
              </div>
              {children}
            </article>
          </section>
        </section>
      </main>
    </>
  );
}
