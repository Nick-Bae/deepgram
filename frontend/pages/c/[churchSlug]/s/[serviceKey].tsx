import { useRouter } from "next/router";
import { useEffect, useMemo, useState } from "react";

import { API_URL, WS_URL } from "../../../../utils/urls";
import { useSubtitleSocket } from "../../../../utils/useSubtitleSocket";
import { appendStreamContextToUrl, clearRoomInSession, persistStreamContext } from "../../../../utils/streamContext";

type ResolveResponse = {
  orgId: string;
  slug: string;
  serviceKey: string;
  activeRoomId: string | null;
  roomStatus: string;
  languagePair?: { source?: string; target?: string };
  service?: { title?: string; timezone?: string };
};

const RESOLVE_POLL_MS = 5000;

export default function ChurchServiceListenerPage() {
  const router = useRouter();
  const slug = typeof router.query.churchSlug === "string" ? router.query.churchSlug : "";
  const serviceKey = typeof router.query.serviceKey === "string" ? router.query.serviceKey : "";

  const [loading, setLoading] = useState(true);
  const [resolveData, setResolveData] = useState<ResolveResponse | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!slug || !serviceKey) return;
    let disposed = false;

    const fetchResolve = async () => {
      try {
        const res = await fetch(`${API_URL}/api/c/${encodeURIComponent(slug)}/s/${encodeURIComponent(serviceKey)}/resolve`);
        if (!res.ok) {
          throw new Error(`resolve_failed_${res.status}`);
        }
        const data: ResolveResponse = await res.json();
        if (disposed) return;
        setResolveData(data);
        setErrorMsg(null);
        persistStreamContext({
          orgId: data.orgId,
          roomId: data.activeRoomId || undefined,
          serviceKey: data.serviceKey || serviceKey,
          churchSlug: data.slug || slug,
        });
        if (!data.activeRoomId) clearRoomInSession();
      } catch (err: unknown) {
        if (disposed) return;
        const message = err instanceof Error ? err.message : String(err);
        setErrorMsg(message || "resolve_failed");
      } finally {
        if (!disposed) setLoading(false);
      }
    };

    fetchResolve();
    const timer = window.setInterval(fetchResolve, RESOLVE_POLL_MS);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, [serviceKey, slug]);

  const socketEnabled = !!resolveData?.activeRoomId && resolveData?.roomStatus === "live";
  const scopedWsUrl = useMemo(() => {
    if (!socketEnabled || !resolveData?.orgId || !resolveData.activeRoomId) return undefined;
    return appendStreamContextToUrl(
      WS_URL,
      {
        orgId: resolveData.orgId,
        roomId: resolveData.activeRoomId,
        serviceKey: resolveData.serviceKey || serviceKey,
        churchSlug: resolveData.slug || slug,
      },
      { role: "viewer" },
    );
  }, [resolveData, serviceKey, slug, socketEnabled]);

  const { connected, krLines, enLines } = useSubtitleSocket(scopedWsUrl, {
    maxLines: 4,
    track: "en",
    enabled: socketEnabled,
  });

  const serviceTitle = resolveData?.service?.title || serviceKey || "Service";
  const lastKr = krLines[krLines.length - 1] || "";
  const lastEn = enLines[enLines.length - 1] || "";

  return (
    <main
      style={{
        minHeight: "100vh",
        background: "#000",
        color: "#fff",
        padding: "24px 20px 32px",
        fontFamily: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
      }}
    >
      <div style={{ maxWidth: 1200, margin: "0 auto" }}>
        <div style={{ marginBottom: 18, opacity: 0.86, fontSize: 14 }}>
          {loading ? "Resolving service..." : `${slug} / ${serviceKey}`}
        </div>

        {errorMsg && (
          <div style={{ marginBottom: 16, color: "#fca5a5", fontSize: 14 }}>
            Failed to resolve service: {errorMsg}
          </div>
        )}

        {!socketEnabled ? (
          <section
            style={{
              border: "1px solid rgba(255,255,255,0.22)",
              borderRadius: 18,
              padding: "20px 18px",
              background: "rgba(255,255,255,0.06)",
            }}
          >
            <h1 style={{ margin: 0, fontSize: 28, fontWeight: 700 }}>{serviceTitle}</h1>
            <p style={{ marginTop: 12, marginBottom: 0, opacity: 0.86 }}>
              Waiting for service to start. Keep this page open.
            </p>
          </section>
        ) : (
          <section
            style={{
              border: "1px solid rgba(255,255,255,0.22)",
              borderRadius: 18,
              padding: "22px 20px",
              background: "rgba(0,0,0,0.5)",
              boxShadow: "0 18px 45px rgba(0,0,0,0.4)",
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", gap: 12, marginBottom: 16, fontSize: 14 }}>
              <strong style={{ fontWeight: 700 }}>{serviceTitle}</strong>
              <span style={{ opacity: 0.9 }}>{connected ? "Connected" : "Reconnecting..."}</span>
            </div>
            {lastKr ? (
              <div style={{ opacity: 0.72, fontSize: "clamp(18px, 2.6vw, 42px)", marginBottom: 14, lineHeight: 1.25 }}>{lastKr}</div>
            ) : null}
            <div style={{ fontSize: "clamp(34px, 8vw, 88px)", fontWeight: 700, lineHeight: 1.08 }}>
              {lastEn || "— waiting —"}
            </div>
          </section>
        )}
      </div>
    </main>
  );
}
