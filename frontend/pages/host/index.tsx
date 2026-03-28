import Link from "next/link";
import { useRouter } from "next/router";
import { useEffect, useMemo } from "react";

function pickFirst(queryValue: string | string[] | undefined): string {
  if (!queryValue) return "";
  const raw = Array.isArray(queryValue) ? queryValue[0] : queryValue;
  return String(raw || "").trim();
}

export default function HostRedirectPage() {
  const router = useRouter();

  const target = useMemo(() => {
    const slug = pickFirst(router.query.churchSlug) || pickFirst(router.query.slug) || pickFirst(router.query.org);
    if (!slug) return "";

    const serviceKey = pickFirst(router.query.serviceKey) || pickFirst(router.query.service);
    const orgId = pickFirst(router.query.orgId) || pickFirst(router.query.org);

    const params = new URLSearchParams();
    if (serviceKey) params.set("serviceKey", serviceKey);
    if (orgId) params.set("orgId", orgId);

    const query = params.toString();
    return `/host/c/${encodeURIComponent(slug)}/broadcast${query ? `?${query}` : ""}`;
  }, [router.query]);

  useEffect(() => {
    if (!router.isReady || !target) return;
    router.replace(target);
  }, [router, target]);

  return (
    <main style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 24, background: "#ede5d8", position: "relative" }}>
      {/* Bokeh background */}
      <div aria-hidden="true" style={{ position: "fixed", inset: 0, zIndex: 0, overflow: "hidden", pointerEvents: "none" }}>
        <div style={{ position: "absolute", left: "-8%", top: "-6%", width: 700, height: 700, borderRadius: "50%", background: "radial-gradient(circle, rgba(214,172,88,0.72) 0%, transparent 68%)", filter: "blur(80px)" }} />
        <div style={{ position: "absolute", right: "-6%", top: "4%", width: 620, height: 620, borderRadius: "50%", background: "radial-gradient(circle, rgba(170,158,248,0.62) 0%, transparent 68%)", filter: "blur(80px)" }} />
        <div style={{ position: "absolute", left: "28%", top: "30%", width: 800, height: 600, borderRadius: "50%", background: "radial-gradient(circle, rgba(255,250,238,0.88) 0%, transparent 68%)", filter: "blur(100px)" }} />
        <div style={{ position: "absolute", right: "8%", bottom: "-8%", width: 680, height: 580, borderRadius: "50%", background: "radial-gradient(circle, rgba(224,158,112,0.58) 0%, transparent 68%)", filter: "blur(90px)" }} />
      </div>
      <section
        style={{
          position: "relative",
          zIndex: 1,
          width: "100%",
          maxWidth: 440,
          borderRadius: 30,
          border: "1px solid rgba(255,255,255,0.68)",
          background: "linear-gradient(180deg, rgba(255,255,255,0.18), rgba(255,255,255,0.10))",
          boxShadow: "inset 0 1px 0 rgba(255,255,255,0.90), 0 28px 64px rgba(122,101,79,0.18)",
          backdropFilter: "blur(40px)",
          WebkitBackdropFilter: "blur(40px)",
          padding: "36px 32px",
          display: "grid",
          gap: 16,
          color: "#2e2a28",
        }}
      >
        {target ? (
          <p style={{ margin: 0, fontSize: 14, color: "#7d746c" }}>Redirecting to your host dashboard…</p>
        ) : (
          <>
            <div style={{ display: "grid", gap: 6 }}>
              <p style={{ margin: 0, fontSize: 11, fontWeight: 800, letterSpacing: "0.14em", textTransform: "uppercase", color: "#b08b4f" }}>
                Host Console
              </p>
              <h2 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: "#1d1917" }}>Sign in to continue</h2>
            </div>
            <p style={{ margin: 0, fontSize: 14, lineHeight: 1.7, color: "#6d645c" }}>
              Please sign in to access your church host dashboard.
            </p>
            <Link
              href="/login"
              style={{
                padding: "12px 18px",
                borderRadius: 999,
                border: "none",
                background: "#c5a263",
                color: "#ffffff",
                fontSize: 14,
                fontWeight: 700,
                textDecoration: "none",
                textAlign: "center",
                boxShadow: "0 10px 24px rgba(110,93,74,0.22)",
              }}
            >
              Sign In
            </Link>
          </>
        )}
      </section>
    </main>
  );
}
