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
  background: "linear-gradient(180deg, #edf1f6 0%, #dfe6ef 54%, #d3dce7 100%)",
  color: "#10213a",
  padding: "28px 16px 40px",
  fontFamily: "'Avenir Next', 'Segoe UI', sans-serif",
};

export const studioCardStyle: CSSProperties = {
  position: "relative",
  overflow: "hidden",
  border: "1px solid rgba(255,255,255,0.66)",
  background: "linear-gradient(180deg, rgba(248,251,255,0.84) 0%, rgba(230,237,246,0.68) 54%, rgba(213,223,236,0.54) 100%)",
  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.54), 0 24px 48px rgba(88,106,137,0.14)",
};

export const studioInnerCardStyle: CSSProperties = {
  position: "relative",
  overflow: "hidden",
  border: "1px solid rgba(255,255,255,0.58)",
  background: "linear-gradient(180deg, rgba(255,255,255,0.62) 0%, rgba(235,241,249,0.5) 58%, rgba(217,227,239,0.34) 100%)",
  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.54), 0 16px 28px rgba(102,120,151,0.08)",
};

export const studioLabelStyle: CSSProperties = {
  display: "grid",
  gap: 6,
};

export const studioLabelTextStyle: CSSProperties = {
  fontSize: 13,
  color: "#5f6f86",
};

export const studioFieldStyle: CSSProperties = {
  borderRadius: 12,
  border: "1px solid rgba(189,200,217,0.92)",
  background: "rgba(247,250,253,0.86)",
  color: "#20324a",
  padding: "11px 12px",
  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.46)",
};

export const studioTextAreaStyle: CSSProperties = {
  ...studioFieldStyle,
  padding: "12px 14px",
  resize: "vertical",
};

export const studioHelperTextStyle: CSSProperties = {
  fontSize: 12,
  color: "#70819a",
};

export const studioSubtleTextStyle: CSSProperties = {
  fontSize: 13,
  color: "#5d6d84",
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
      border: "1px solid rgba(189,200,217,0.92)",
      background: "rgba(247,250,253,0.82)",
      color: "#42556f",
      fontWeight: 700,
      padding,
      cursor: disabled ? "not-allowed" : "pointer",
      opacity: disabled ? 0.6 : 1,
      boxShadow: "0 8px 16px rgba(122,138,163,0.08)",
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
    border: "1px solid rgba(79,115,170,0.28)",
    background: "linear-gradient(145deg, #7fa5db, #4f73aa)",
    color: "#f8fafc",
    fontWeight: 800,
    padding,
    cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? 0.6 : 1,
    boxShadow: "0 14px 30px rgba(79,115,170,0.22)",
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
  };
}

export function buildStudioNoticeStyle(tone: "error" | "success" | "info"): CSSProperties {
  if (tone === "error") {
    return {
      margin: 0,
      borderRadius: 14,
      padding: "12px 14px",
      background: "rgba(188,95,111,0.12)",
      color: "#a33d51",
      fontSize: 14,
    };
  }
  if (tone === "success") {
    return {
      margin: 0,
      borderRadius: 14,
      padding: "12px 14px",
      background: "rgba(91,179,130,0.12)",
      color: "#2f6d4f",
      fontSize: 14,
    };
  }
  return {
    margin: 0,
    borderRadius: 14,
    padding: "12px 14px",
    background: "rgba(79,115,170,0.1)",
    color: "#3e5d8d",
    fontSize: 14,
  };
}

export const studioChipStyle: CSSProperties = {
  borderRadius: 999,
  border: "1px solid rgba(147,197,253,0.4)",
  background: "rgba(79,115,170,0.1)",
  color: "#3e5d8d",
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
        borderRadius: 999,
        border: "1px solid rgba(197,210,228,0.9)",
        background: "rgba(248,250,253,0.96)",
        color: "#465a76",
        fontWeight: 800,
        padding: "12px 18px",
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
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
                  Worship
                </p>
                <h1 style={{ margin: "8px 0 0", fontSize: "clamp(28px, 4vw, 36px)", lineHeight: 1, letterSpacing: "-0.05em" }}>
                  Translation Studio
                </h1>
              </div>
            </div>

            {headerActions.length ? <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>{headerActions.map(renderHeaderAction)}</div> : null}
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
                ...studioCardStyle,
                borderRadius: 30,
                padding: 22,
                display: "grid",
                gap: 16,
                height: "100%",
                alignContent: "start",
              }}
            >
              <div
                style={{
                  ...studioInnerCardStyle,
                  borderRadius: 22,
                  padding: "16px 18px",
                  background: "linear-gradient(180deg, rgba(225,236,250,0.62) 0%, rgba(212,225,243,0.34) 100%)",
                  border: "1px solid rgba(204,218,238,0.92)",
                }}
              >
                <p style={{ margin: 0, fontSize: 11, color: "#7386a2", letterSpacing: "0.24em", textTransform: "uppercase", fontWeight: 800 }}>
                  {infoEyebrow}
                </p>
                <h2 style={{ margin: "10px 0 0", fontSize: 30, lineHeight: 1.05, letterSpacing: "-0.05em" }}>{infoTitle}</h2>
              </div>
              <p style={{ margin: 0, fontSize: 15, lineHeight: 1.8, color: "#50627c" }}>{infoDescription}</p>
              <div style={{ display: "grid", gap: 12 }}>
                {infoItems.map((item) => (
                  <div
                    key={item.title}
                    style={{
                      ...studioInnerCardStyle,
                      borderRadius: 18,
                      padding: "14px 16px",
                    }}
                  >
                    <p style={{ margin: 0, fontSize: 15, fontWeight: 800, color: "#22344c" }}>{item.title}</p>
                    <p style={{ margin: "6px 0 0", fontSize: 14, lineHeight: 1.7, color: "#5d6d84" }}>{item.description}</p>
                  </div>
                ))}
              </div>
            </aside>

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
              <div
                style={{
                  ...studioInnerCardStyle,
                  borderRadius: 22,
                  padding: "16px 18px",
                  background: "linear-gradient(180deg, rgba(246,239,227,0.62) 0%, rgba(236,227,209,0.34) 100%)",
                  border: "1px solid rgba(225,214,191,0.92)",
                }}
              >
                <p style={{ margin: 0, fontSize: 11, color: "#7386a2", letterSpacing: "0.24em", textTransform: "uppercase", fontWeight: 800 }}>
                  {panelEyebrow}
                </p>
                <h2 style={{ margin: "10px 0 0", fontSize: 30, lineHeight: 1.05, letterSpacing: "-0.05em" }}>{panelTitle}</h2>
                <p style={{ margin: "8px 0 0", fontSize: 14, lineHeight: 1.7, color: "#5d6d84" }}>{panelDescription}</p>
              </div>
              {children}
            </article>
          </section>
        </section>
      </main>
    </>
  );
}
