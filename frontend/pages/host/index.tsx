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
    <main style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 24, background: "linear-gradient(180deg, #e9edf4 0%, #dde4ee 56%, #d4dce8 100%)" }}>
      <section
        style={{
          width: "100%",
          maxWidth: 440,
          borderRadius: 24,
          border: "1px solid rgba(255,255,255,0.72)",
          background: "linear-gradient(180deg, rgba(248,251,255,0.92) 0%, rgba(228,237,250,0.78) 100%)",
          boxShadow: "inset 0 1px 0 rgba(255,255,255,0.6), 0 24px 48px rgba(88,106,137,0.14)",
          padding: "36px 32px",
          display: "grid",
          gap: 16,
          color: "#10213a",
        }}
      >
        {target ? (
          <p style={{ margin: 0, fontSize: 14, color: "#4a5f7a" }}>Redirecting to your host dashboard…</p>
        ) : (
          <>
            <div style={{ display: "grid", gap: 6 }}>
              <p style={{ margin: 0, fontSize: 11, fontWeight: 800, letterSpacing: "0.14em", textTransform: "uppercase", color: "#6b82a0" }}>
                Host Console
              </p>
              <h2 style={{ margin: 0, fontSize: 22, fontWeight: 800, color: "#10213a" }}>Sign in to continue</h2>
            </div>
            <p style={{ margin: 0, fontSize: 14, lineHeight: 1.7, color: "#4a5f7a" }}>
              Please sign in to access your church host dashboard.
            </p>
            <Link
              href="/login"
              style={{
                padding: "12px 18px",
                borderRadius: 999,
                border: "1px solid rgba(79,115,170,0.28)",
                background: "linear-gradient(145deg, #7fa5db, #4f73aa)",
                color: "#f8fafc",
                fontSize: 14,
                fontWeight: 800,
                textDecoration: "none",
                textAlign: "center",
                boxShadow: "0 10px 24px rgba(79,115,170,0.22)",
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
