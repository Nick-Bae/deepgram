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
    <main style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 24, background: "#0b1220", color: "#f8fafc" }}>
      <section style={{ width: "100%", maxWidth: 720, border: "1px solid rgba(255,255,255,0.14)", borderRadius: 14, padding: 18, background: "rgba(255,255,255,0.04)" }}>
        {target ? (
          <p style={{ margin: 0, opacity: 0.9 }}>Redirecting to host dashboard...</p>
        ) : (
          <div style={{ display: "grid", gap: 8 }}>
            <p style={{ margin: 0, opacity: 0.9 }}>
              Host URL is now <code>/host/c/&lt;churchSlug&gt;/broadcast</code>.
            </p>
            <p style={{ margin: 0, opacity: 0.8 }}>
              Example: <Link href="/host/c/arkchurch/broadcast" style={{ color: "#93c5fd" }}>/host/c/arkchurch/broadcast</Link>
            </p>
          </div>
        )}
      </section>
    </main>
  );
}
