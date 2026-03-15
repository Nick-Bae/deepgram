import Head from "next/head";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { fetchAuthMe } from "../lib/backendAuth";
import { useAuth } from "../lib/authContext";
import { buildDashboardHref, persistDashboardContext, pickPreferredMembership } from "../lib/dashboardRoute";
import { persistAuthToken } from "../utils/streamContext";

const publicQuickLinks = [
  {
    title: "Host Console",
    desc: "Sign in to manage church services, room tokens, and broadcast controls.",
    href: "/login",
    cta: "Host Login",
    accent: "linear-gradient(145deg, #7fa5db, #4f73aa)",
  },
  {
    title: "Create Church",
    desc: "Create your church workspace and start with the built-in 30 minute host trial.",
    href: "/signup",
    cta: "Start Free Trial",
    accent: "linear-gradient(145deg, #97b9ef, #6e8fc3)",
  },
  {
    title: "Join By Invite",
    desc: "Accept an invite code and join an existing organization workspace.",
    href: "/join",
    cta: "Open Join Page",
    accent: "linear-gradient(145deg, #f5d38c, #d9a75b)",
  },
];

const signedInQuickLinks = [
  {
    title: "Continue Dashboard",
    desc: "Open your church dashboard and continue the current broadcast workflow.",
    cta: "Open Dashboard",
    accent: "linear-gradient(145deg, #7fa5db, #4f73aa)",
  },
  {
    title: "Join Another Team",
    desc: "Use an invite link to add another church workspace to your account.",
    href: "/join",
    cta: "Open Join Page",
    accent: "linear-gradient(145deg, #f5d38c, #d9a75b)",
  },
];

const landingStats = [
  { value: "30 min", label: "Host trial included" },
  { value: "QR", label: "Listener access flow" },
  { value: "Live", label: "Translated captions" },
];

const workflowSteps = [
  {
    id: "01",
    title: "Start the service",
    desc: "Hosts open the church dashboard, choose a recurring service, and start the room.",
  },
  {
    id: "02",
    title: "Share listener access",
    desc: "Display the QR code or send the listener URL so attendees can join on their phone.",
  },
  {
    id: "03",
    title: "Deliver translation live",
    desc: "Translated captions update continuously while the speaker continues without changing workflow.",
  },
];

const productNotes = [
  {
    title: "Built for service flow",
    desc: "The console keeps room control, translation monitoring, and service scheduling in one place.",
  },
  {
    title: "Works for real church teams",
    desc: "Invite hosts and admins, keep recurring services organized, and reuse the same structure every week.",
  },
  {
    title: "Simple listener experience",
    desc: "Attendees do not need a host login. They open the listener page and follow the translated feed live.",
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
  const { user, loading: authLoading, configured, getIdToken } = useAuth();
  const [dashboardHref, setDashboardHref] = useState("/onboarding/create-church");
  const [resolvingDashboard, setResolvingDashboard] = useState(false);
  const isLoggedIn = Boolean(user);
  const quickLinks = useMemo(
    () =>
      isLoggedIn
        ? signedInQuickLinks.map((card) => ({ ...card, href: card.href || dashboardHref }))
        : publicQuickLinks,
    [dashboardHref, isLoggedIn],
  );
  const embedVideoUrl = toEmbedVideoUrl(VIDEO_URL);

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
        if (!cancelled) {
          setDashboardHref("/login");
        }
      } finally {
        if (!cancelled) {
          setResolvingDashboard(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authLoading, configured, getIdToken, user]);

  return (
    <>
      <Head>
        <title>Worship | Live Church Translation</title>
        <meta
          name="description"
          content="Launch live translated worship services, share QR listener access, and manage church broadcasts from one console."
        />
      </Head>
      <main
        style={{
          minHeight: "100vh",
          position: "relative",
          overflow: "hidden",
          background:
            "radial-gradient(circle at top left, rgba(154,179,219,0.3), transparent 28%), radial-gradient(circle at 86% 10%, rgba(223,190,131,0.18), transparent 22%), linear-gradient(180deg, #edf1f6 0%, #dfe6ef 54%, #d3dce7 100%)",
          color: "#10213a",
          padding: "28px 16px 40px",
          fontFamily: "'Avenir Next', 'Segoe UI', sans-serif",
        }}
      >
        <div
          aria-hidden="true"
          style={{
            position: "absolute",
            top: -120,
            left: -100,
            width: 360,
            height: 360,
            borderRadius: 999,
            background: "radial-gradient(circle, rgba(157,180,214,0.3) 0%, rgba(157,180,214,0) 72%)",
            filter: "blur(12px)",
          }}
        />
        <div
          aria-hidden="true"
          style={{
            position: "absolute",
            right: -120,
            top: 240,
            width: 320,
            height: 320,
            borderRadius: 999,
            background: "radial-gradient(circle, rgba(228,236,247,0.7) 0%, rgba(228,236,247,0) 72%)",
            filter: "blur(14px)",
          }}
        />

        <section style={{ position: "relative", maxWidth: 1180, margin: "0 auto", display: "grid", gap: 22 }}>
          <header
            style={{
              position: "relative",
              overflow: "hidden",
              borderRadius: 30,
              border: "1px solid rgba(255,255,255,0.14)",
              background: "linear-gradient(135deg, #566983 0%, #323d50 38%, #171d27 100%)",
              boxShadow: "0 28px 56px rgba(32,42,58,0.24), inset 0 1px 0 rgba(255,255,255,0.08)",
              padding: "18px 22px",
              color: "#f8fafc",
            }}
          >
            <div
              aria-hidden="true"
              style={{
                position: "absolute",
                inset: "-30% auto auto -8%",
                width: 240,
                height: 180,
                borderRadius: 999,
                background: "radial-gradient(circle, rgba(141, 172, 214, 0.36) 0%, rgba(141, 172, 214, 0) 74%)",
                filter: "blur(10px)",
              }}
            />
            <div
              aria-hidden="true"
              style={{
                position: "absolute",
                inset: "auto -8% -80px auto",
                width: 260,
                height: 180,
                borderRadius: 999,
                background: "radial-gradient(circle, rgba(255, 183, 3, 0.14) 0%, rgba(255, 183, 3, 0) 74%)",
                filter: "blur(12px)",
              }}
            />
            <div
              style={{
                position: "relative",
                display: "flex",
                flexWrap: "wrap",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 16,
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
                <div
                  style={{
                    width: 58,
                    height: 58,
                    borderRadius: 18,
                    background: "linear-gradient(145deg, rgba(255,255,255,0.12), rgba(255,255,255,0.04))",
                    border: "1px solid rgba(255,255,255,0.1)",
                    color: "#ffb703",
                    display: "grid",
                    placeItems: "center",
                    fontSize: 20,
                    fontWeight: 900,
                    fontStyle: "italic",
                    boxShadow: "0 16px 28px rgba(13,18,28,0.22)",
                  }}
                >
                  W
                </div>
                <div>
                  <h1
                    style={{
                      margin: 0,
                      fontSize: "clamp(26px, 4vw, 34px)",
                      lineHeight: 1,
                      letterSpacing: "-0.05em",
                      fontWeight: 800,
                      color: "#f8fafc",
                    }}
                  >
                    Worship
                  </h1>
                  <p
                    style={{
                      margin: "6px 0 0",
                      fontSize: 11,
                      letterSpacing: "0.36em",
                      textTransform: "uppercase",
                      color: "#b9c6da",
                    }}
                  >
                    Translation Studio
                  </p>
                </div>
              </div>

              <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
                {authLoading ? (
                  <span
                    style={{
                      borderRadius: 999,
                      padding: "12px 16px",
                      background: "rgba(255,255,255,0.08)",
                      border: "1px solid rgba(255,255,255,0.14)",
                      color: "#d9e3f2",
                      fontSize: 13,
                      fontWeight: 700,
                    }}
                  >
                    Checking session...
                  </span>
                ) : isLoggedIn ? (
                  <>
                    <Link
                      href={dashboardHref}
                      style={{
                        borderRadius: 999,
                        padding: "12px 18px",
                        background: "linear-gradient(145deg, #7fa5db, #4f73aa)",
                        color: "#f8fafc",
                        fontWeight: 800,
                        boxShadow: "0 14px 30px rgba(79,115,170,0.28)",
                      }}
                    >
                      {resolvingDashboard ? "Locating Dashboard..." : "Open Dashboard"}
                    </Link>
                    <Link
                      href="/join"
                      style={{
                        borderRadius: 999,
                        padding: "12px 18px",
                        border: "1px solid rgba(255,255,255,0.14)",
                        background: "rgba(255,255,255,0.08)",
                        color: "#d9e3f2",
                        fontWeight: 700,
                        boxShadow: "inset 0 1px 0 rgba(255,255,255,0.04)",
                      }}
                    >
                      Join Team
                    </Link>
                  </>
                ) : (
                  <>
                    <Link
                      href="/c/demo/s/sun-11am"
                      style={{
                        borderRadius: 999,
                        padding: "12px 18px",
                        background: "rgba(255,255,255,0.08)",
                        border: "1px solid rgba(255,255,255,0.14)",
                        color: "#d9e3f2",
                        fontWeight: 700,
                      }}
                    >
                      Listener Demo
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
                  </>
                )}
              </div>
            </div>
          </header>

          <section
            style={{
              display: "grid",
              gap: 22,
              gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
              alignItems: "stretch",
            }}
          >
            <article
              style={{
                borderRadius: 34,
                border: "1px solid rgba(255,255,255,0.78)",
                background: "linear-gradient(145deg, rgba(248,251,255,0.96), rgba(225,233,244,0.92))",
                boxShadow: "28px 28px 60px rgba(122,138,163,0.16), -18px -18px 34px rgba(255,255,255,0.7)",
                padding: "30px 28px",
                display: "grid",
                gap: 22,
              }}
            >
              <div style={{ display: "grid", gap: 12 }}>
                <p
                  style={{
                    margin: 0,
                    color: "#7386a2",
                    fontSize: 12,
                    fontWeight: 800,
                    letterSpacing: "0.26em",
                    textTransform: "uppercase",
                  }}
                >
                  Live Multilingual Worship
                </p>
                <h2
                  style={{
                    margin: 0,
                    fontSize: "clamp(34px, 6vw, 60px)",
                    lineHeight: 0.96,
                    letterSpacing: "-0.06em",
                    fontWeight: 800,
                    maxWidth: 620,
                  }}
                >
                  Broadcast live translation without making the service feel technical.
                </h2>
                <p style={{ margin: 0, fontSize: 17, lineHeight: 1.7, color: "#50627c", maxWidth: 620 }}>
                  Hosts start the room once, listeners join by QR, and translated captions keep moving while the speaker stays focused on the sermon.
                </p>
              </div>

              <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
                {authLoading ? (
                  <span style={{ fontSize: 14, color: "#5f6f86", fontWeight: 700 }}>Checking your session...</span>
                ) : isLoggedIn && resolvingDashboard ? (
                  <span style={{ fontSize: 14, color: "#5f6f86", fontWeight: 700 }}>Locating your dashboard...</span>
                ) : isLoggedIn ? (
                  <Link
                    href={dashboardHref}
                    style={{
                      borderRadius: 999,
                      background: "linear-gradient(145deg, #7fa5db, #4f73aa)",
                      color: "#f8fafc",
                      fontWeight: 800,
                      padding: "14px 20px",
                      boxShadow: "0 16px 34px rgba(79,115,170,0.24)",
                      display:"flex",
                      alignItems: "center"
                    }}
                  >
                    Continue To Dashboard
                  </Link>
                ) : (
                  <>
                    <Link
                      href="/signup"
                      style={{
                        borderRadius: 999,
                        background: "linear-gradient(145deg, #7fa5db, #4f73aa)",
                        color: "#f8fafc",
                        fontWeight: 800,
                        padding: "14px 20px",
                        boxShadow: "0 16px 34px rgba(79,115,170,0.24)",
                        display:"flex",
                        alignItems: "center"
                      }}
                    >
                      Start Free Trial
                    </Link>
                    <Link
                      href="/c/demo/s/sun-11am"
                      style={{
                        borderRadius: 999,
                        border: "1px solid rgba(189,200,217,0.94)",
                        background: "rgba(247,250,253,0.84)",
                        color: "#42556f",
                        fontWeight: 700,
                        padding: "14px 20px",
                      }}
                    >
                      Try Listener Demo
                    </Link>
                  </>
                )}
              </div>

              <div
                style={{
                  display: "grid",
                  gap: 12,
                  gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
                }}
              >
                {landingStats.map((stat) => (
                  <div
                    key={stat.label}
                    style={{
                      borderRadius: 22,
                      border: "1px solid rgba(255,255,255,0.8)",
                      background: "rgba(255,255,255,0.58)",
                      padding: "16px 18px",
                      boxShadow: "12px 12px 24px rgba(122,138,163,0.12), -10px -10px 20px rgba(255,255,255,0.78)",
                    }}
                  >
                    <p style={{ margin: 0, fontSize: 28, fontWeight: 800, letterSpacing: "-0.05em" }}>{stat.value}</p>
                    <p style={{ margin: "4px 0 0", fontSize: 13, color: "#667791" }}>{stat.label}</p>
                  </div>
                ))}
              </div>
            </article>

            <article
              style={{
                borderRadius: 34,
                border: "1px solid rgba(255,255,255,0.84)",
                background: "linear-gradient(145deg, rgba(240,245,251,0.96), rgba(220,228,239,0.9))",
                boxShadow: "28px 28px 60px rgba(122,138,163,0.16), -18px -18px 34px rgba(255,255,255,0.7)",
                padding: "24px 22px",
                display: "grid",
                gap: 18,
                minHeight: 430,
              }}
            >
              <div style={{ display: "flex", flexWrap: "wrap", justifyContent: "space-between", gap: 12, alignItems: "center" }}>
                <div>
                  <p style={{ margin: 0, fontSize: 12, color: "#7386a2", fontWeight: 800, letterSpacing: "0.22em", textTransform: "uppercase" }}>
                    Studio Preview
                  </p>
                  <h3 style={{ margin: "8px 0 0", fontSize: 30, lineHeight: 1, letterSpacing: "-0.05em" }}>Broadcast control, translated output, listener-ready.</h3>
                </div>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  <span style={{ borderRadius: 999, padding: "8px 12px", background: "rgba(124,187,160,0.14)", color: "#3b7d5c", fontSize: 12, fontWeight: 700 }}>
                    Connected
                  </span>
                  <span style={{ borderRadius: 999, padding: "8px 12px", background: "rgba(255,255,255,0.6)", color: "#61748e", fontSize: 12, fontWeight: 700 }}>
                    English Output
                  </span>
                </div>
              </div>

              <div
                style={{
                  display: "grid",
                  gap: 14,
                  gridTemplateColumns: "minmax(0, 1.3fr) minmax(250px, 1fr)",
                  alignItems: "stretch",
                  flex: 1,
                }}
              >
                <div
                  style={{
                    borderRadius: 26,
                    border: "1px solid rgba(255,255,255,0.84)",
                    background: "rgba(255,255,255,0.54)",
                    padding: 18,
                    display: "grid",
                    gap: 16,
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center" }}>
                    <div>
                      <p style={{ margin: 0, fontSize: 11, color: "#7c8ba3", letterSpacing: "0.22em", textTransform: "uppercase", fontWeight: 800 }}>
                        Live Audio Stream
                      </p>
                      <p style={{ margin: "6px 0 0", fontSize: 14, color: "#23354d", fontWeight: 700 }}>KR Korean</p>
                    </div>
                    <div style={{ display: "flex", alignItems: "end", gap: 5, height: 28 }}>
                      {[0.3, 0.72, 0.46, 0.88, 0.5].map((height, index) => (
                        <span
                          key={index}
                          style={{
                            width: 6,
                            height: `${Math.round(28 * height)}px`,
                            borderRadius: 999,
                            background: index === 2 ? "#4f73aa" : "#c2cfdf",
                          }}
                        />
                      ))}
                    </div>
                  </div>

                  <div
                    style={{
                      borderRadius: 20,
                      background: "rgba(248,250,253,0.78)",
                      border: "1px solid rgba(228,233,241,0.92)",
                      minHeight: 150,
                      padding: 18,
                    }}
                  >
                    <p style={{ margin: 0, color: "#8696ad", fontSize: 14 }}>
                      Listening to the speaker in Korean...
                    </p>
                  </div>

                  <div
                    style={{
                      borderRadius: 18,
                      background: "linear-gradient(145deg, #7fa5db, #4f73aa)",
                      color: "#f8fafc",
                      textAlign: "center",
                      fontSize: 13,
                      fontWeight: 800,
                      letterSpacing: "0.24em",
                      textTransform: "uppercase",
                      padding: "14px 16px",
                      boxShadow: "0 16px 32px rgba(79,115,170,0.22)",
                      display:"flex",
                      alignItems: "center",
                      justifyContent: "center"
                    }}
                  >
                    Start Translation
                  </div>
                </div>

                <div style={{ display: "grid", gap: 14 }}>
                  <div
                    style={{
                      borderRadius: 24,
                      border: "1px solid rgba(255,255,255,0.84)",
                      background: "rgba(255,255,255,0.58)",
                      padding: 18,
                      display: "grid",
                      gap: 10,
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "center" }}>
                      <div>
                        <p style={{ margin: 0, fontSize: 11, color: "#7c8ba3", letterSpacing: "0.22em", textTransform: "uppercase", fontWeight: 800 }}>
                          Translation Output
                        </p>
                        <p style={{ margin: "6px 0 0", fontSize: 14, color: "#23354d", fontWeight: 700 }}>US English</p>
                      </div>
                      <span style={{ borderRadius: 999, padding: "7px 10px", background: "rgba(233,222,177,0.46)", color: "#7e6332", fontSize: 11, fontWeight: 800 }}>
                        Broadcast Ready
                      </span>
                    </div>
                    <p style={{ margin: "10px 0 0", fontSize: 26, lineHeight: 1.15, fontWeight: 700, color: "#1b2a40", letterSpacing: "-0.04em" }}>
                      The message is delivered with clarity and stays easy to follow.
                    </p>
                  </div>

                  <div
                    style={{
                      borderRadius: 24,
                      border: "1px solid rgba(255,255,255,0.84)",
                      background: "rgba(255,255,255,0.58)",
                      padding: 18,
                      display: "grid",
                      gap: 10,
                    }}
                  >
                    <p style={{ margin: 0, fontSize: 11, color: "#7c8ba3", letterSpacing: "0.22em", textTransform: "uppercase", fontWeight: 800 }}>
                      Broadcast Output
                    </p>
                    <p style={{ margin: 0, fontSize: 14, color: "#5f6f86", lineHeight: 1.7 }}>
                      Stage and listener display feeds stay in sync while the speaker keeps moving through the service.
                    </p>
                    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                      <span style={{ borderRadius: 999, background: "rgba(247,250,253,0.82)", padding: "7px 10px", fontSize: 12, color: "#50627c", fontWeight: 700 }}>
                        QR access
                      </span>
                      <span style={{ borderRadius: 999, background: "rgba(247,250,253,0.82)", padding: "7px 10px", fontSize: 12, color: "#50627c", fontWeight: 700 }}>
                        Listener URL
                      </span>
                      <span style={{ borderRadius: 999, background: "rgba(247,250,253,0.82)", padding: "7px 10px", fontSize: 12, color: "#50627c", fontWeight: 700 }}>
                        Live room status
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            </article>
          </section>

          <section style={{ display: "grid", gap: 16, gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" }}>
            {quickLinks.map((card) => (
              <article
                key={card.title}
                style={{
                  borderRadius: 24,
                  border: "1px solid rgba(255,255,255,0.82)",
                  background: "linear-gradient(145deg, rgba(248,251,254,0.96), rgba(228,235,244,0.9))",
                  boxShadow: "20px 20px 40px rgba(122,138,163,0.12), -14px -14px 28px rgba(255,255,255,0.76)",
                  padding: 18,
                  display: "grid",
                  gap: 12,
                }}
              >
                <div style={{ width: 56, height: 6, borderRadius: 999, background: card.accent }} />
                <h2 style={{ margin: 0, fontSize: 21, letterSpacing: "-0.04em" }}>{card.title}</h2>
                <p style={{ margin: 0, fontSize: 14, lineHeight: 1.7, color: "#5d6d84" }}>{card.desc}</p>
                <Link
                  href={card.href}
                  style={{
                    marginTop: 6,
                    justifySelf: "start",
                    borderRadius: 999,
                    padding: "11px 16px",
                    background: "rgba(247,250,253,0.84)",
                    border: "1px solid rgba(189,200,217,0.94)",
                    color: "#33465f",
                    fontSize: 13,
                    fontWeight: 800,
                  }}
                >
                  {card.cta}
                </Link>
              </article>
            ))}
          </section>

          <section
            style={{
              display: "grid",
              gap: 18,
              gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
            }}
          >
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
                  borderRadius: 20,
                  padding: "14px 16px",
                  background: "linear-gradient(145deg, rgba(225,236,250,0.96), rgba(212,225,243,0.9))",
                  border: "1px solid rgba(204,218,238,0.92)",
                  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.34)",
                }}
              >
                <p style={{ margin: 0, fontSize: 11, color: "#7386a2", letterSpacing: "0.24em", textTransform: "uppercase", fontWeight: 800 }}>
                  How It Works
                </p>
                <h2 style={{ margin: "10px 0 0", fontSize: 30, lineHeight: 1.05, letterSpacing: "-0.05em" }}>
                  One host workflow. Clear listener experience.
                </h2>
              </div>
              <div style={{ display: "grid", gap: 14 }}>
                {workflowSteps.map((step) => (
                  <div
                    key={step.id}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "auto minmax(0, 1fr)",
                      gap: 14,
                      alignItems: "start",
                      padding: "14px 0",
                      borderTop: step.id === "01" ? "none" : "1px solid rgba(210,220,232,0.88)",
                    }}
                  >
                    <span
                      style={{
                        width: 42,
                        height: 42,
                        borderRadius: 14,
                        display: "grid",
                        placeItems: "center",
                        background: "rgba(79,115,170,0.1)",
                        color: "#4f73aa",
                        fontWeight: 800,
                        fontSize: 12,
                      }}
                    >
                      {step.id}
                    </span>
                    <div>
                      <p style={{ margin: 0, fontSize: 16, fontWeight: 800, color: "#22344c" }}>{step.title}</p>
                      <p style={{ margin: "6px 0 0", fontSize: 14, lineHeight: 1.7, color: "#5d6d84" }}>{step.desc}</p>
                    </div>
                  </div>
                ))}
              </div>
              <div style={{ display: "grid", gap: 8, fontSize: 14, color: "#50627c" }}>
                <p style={{ margin: 0 }}>
                  1. Create church with <code>/signup</code> or sign in at <code>/login</code>.
                </p>
                <p style={{ margin: 0 }}>
                  2. Hosts land at <code>/host/c/&lt;churchSlug&gt;/broadcast</code>.
                </p>
                <p style={{ margin: 0 }}>
                  3. Listeners open <code>/c/&lt;churchSlug&gt;/s/&lt;serviceKey&gt;</code>.
                </p>
              </div>
            </article>

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
                  borderRadius: 20,
                  padding: "14px 16px",
                  background: "linear-gradient(145deg, rgba(246,239,227,0.96), rgba(236,227,209,0.9))",
                  border: "1px solid rgba(225,214,191,0.92)",
                  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.3)",
                }}
              >
                <p style={{ margin: 0, fontSize: 11, color: "#7386a2", letterSpacing: "0.24em", textTransform: "uppercase", fontWeight: 800 }}>
                  Demo Video
                </p>
                <h2 style={{ margin: "10px 0 0", fontSize: 30, lineHeight: 1.05, letterSpacing: "-0.05em" }}>
                  Watch the full host-to-listener flow.
                </h2>
              </div>
              {embedVideoUrl ? (
                <div
                  style={{
                    position: "relative",
                    width: "100%",
                    paddingTop: "56.25%",
                    borderRadius: 24,
                    overflow: "hidden",
                    border: "1px solid rgba(255,255,255,0.84)",
                    boxShadow: "inset 0 0 0 1px rgba(255,255,255,0.26)",
                  }}
                >
                  <iframe
                    src={embedVideoUrl}
                    title="Yebon demo video"
                    allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                    allowFullScreen
                    style={{ position: "absolute", inset: 0, width: "100%", height: "100%", border: "none" }}
                  />
                </div>
              ) : (
                <div
                  style={{
                    borderRadius: 22,
                    border: "1px dashed rgba(189,200,217,0.96)",
                    background: "rgba(247,250,253,0.72)",
                    padding: 16,
                    fontSize: 14,
                    color: "#5d6d84",
                    lineHeight: 1.7,
                  }}
                >
                  Add <code>NEXT_PUBLIC_HOW_IT_WORKS_VIDEO_URL</code> in <code>frontend/.env.local</code> with a Loom or YouTube URL.
                </div>
              )}
              <div style={{ display: "grid", gap: 12 }}>
                {productNotes.map((note) => (
                  <div
                    key={note.title}
                    style={{
                      borderRadius: 18,
                      background: "rgba(255,255,255,0.56)",
                      border: "1px solid rgba(219,227,238,0.9)",
                      padding: "14px 16px",
                    }}
                  >
                    <p style={{ margin: 0, fontSize: 15, fontWeight: 800, color: "#22344c" }}>{note.title}</p>
                    <p style={{ margin: "6px 0 0", fontSize: 14, lineHeight: 1.7, color: "#5d6d84" }}>{note.desc}</p>
                  </div>
                ))}
              </div>
            </article>
          </section>
        </section>
      </main>
    </>
  );
}
