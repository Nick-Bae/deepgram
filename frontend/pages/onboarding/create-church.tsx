import { useRouter } from "next/router";
import { FormEvent, useEffect, useState } from "react";

import { bootstrapOwnerOrg, fetchAuthMe } from "../../lib/backendAuth";
import { useAuth } from "../../lib/authContext";
import { normalizeChurchSlug } from "../../lib/churchSlug";
import { persistAuthToken, persistHostToken, persistStreamContext } from "../../utils/streamContext";

export default function CreateChurchOnboardingPage() {
  const router = useRouter();
  const { user, loading, configured, missingEnv, getIdToken } = useAuth();

  const [busy, setBusy] = useState(false);
  const [checkingMembership, setCheckingMembership] = useState(true);
  const [churchName, setChurchName] = useState("");
  const [churchSlug, setChurchSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    if (slugTouched) return;
    setChurchSlug(normalizeChurchSlug(churchName));
  }, [churchName, slugTouched]);

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login?next=%2Fonboarding%2Fcreate-church");
      return;
    }
    if (!configured) {
      setCheckingMembership(false);
      return;
    }
    let cancelled = false;
    const run = async () => {
      try {
        const token = await getIdToken();
        if (!token || cancelled) return;
        persistAuthToken(token);
        const me = await fetchAuthMe(token);
        const preferredOrgId = (me.currentOrgId || "").trim();
        const primary = me.memberships.find((row) => row.orgId === preferredOrgId) || me.memberships[0];
        if (!primary || cancelled) return;
        if (primary.hostToken) persistHostToken(primary.hostToken);
        const params = new URLSearchParams();
        params.set("orgId", primary.orgId);
        await router.replace(`/host/c/${encodeURIComponent(primary.slug)}/broadcast?${params.toString()}`);
      } catch (err) {
        if (!cancelled && err instanceof Error) {
          setErrorMsg(err.message);
        }
      } finally {
        if (!cancelled) setCheckingMembership(false);
      }
    };
    run();
    return () => {
      cancelled = true;
    };
  }, [configured, getIdToken, loading, router, user]);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!configured) return;
    setBusy(true);
    setErrorMsg(null);
    try {
      const token = await getIdToken(true);
      if (!token) {
        throw new Error("Please sign in again.");
      }
      persistAuthToken(token);
      const safeSlug = normalizeChurchSlug(churchSlug);
      if (!safeSlug) throw new Error("Church slug is required.");
      const created = await bootstrapOwnerOrg(token, {
        churchName: churchName.trim(),
        churchSlug: safeSlug,
        timezone: "America/Chicago",
        source: "ko",
        target: "en",
      });
      const org = created.org;
      const serviceKey = created.services?.[0]?.serviceKey || "sun-11am";
      if (created.hostToken) persistHostToken(created.hostToken);
      persistStreamContext({
        orgId: org.orgId,
        serviceKey,
        churchSlug: org.slug,
      });
      const params = new URLSearchParams();
      params.set("orgId", org.orgId);
      params.set("serviceKey", serviceKey);
      await router.replace(`/host/c/${encodeURIComponent(org.slug)}/broadcast?${params.toString()}`);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Failed to create church.");
    } finally {
      setBusy(false);
    }
  };

  const disabled = !configured || busy || checkingMembership;

  return (
    <main style={{ minHeight: "100vh", display: "grid", placeItems: "center", background: "#0b1220", color: "#f8fafc", padding: 18 }}>
      <section style={{ width: "100%", maxWidth: 470, borderRadius: 14, border: "1px solid rgba(255,255,255,0.14)", background: "rgba(255,255,255,0.04)", padding: 18 }}>
        <h1 style={{ marginTop: 0, marginBottom: 8 }}>Create Your Church</h1>
        <p style={{ marginTop: 0, marginBottom: 14, opacity: 0.82 }}>Create your organization before starting services.</p>

        {!configured ? (
          <div style={{ borderRadius: 10, border: "1px solid rgba(252,165,165,0.45)", background: "rgba(127,29,29,0.3)", padding: 12, color: "#fecaca", fontSize: 13 }}>
            Firebase config is missing in <code>frontend/.env.local</code>: {missingEnv.join(", ")}
          </div>
        ) : null}

        {errorMsg ? (
          <p style={{ color: "#fca5a5", marginTop: 12, marginBottom: 0 }}>Error: {errorMsg}</p>
        ) : null}

        <form onSubmit={onSubmit} style={{ display: "grid", gap: 10, marginTop: 12 }}>
          <label style={{ display: "grid", gap: 4 }}>
            <span style={{ fontSize: 13, opacity: 0.84 }}>Church name</span>
            <input
              value={churchName}
              onChange={(e) => setChurchName(e.target.value)}
              required
              style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.24)", background: "#0f172a", color: "#fff", padding: "10px 12px" }}
            />
          </label>
          <label style={{ display: "grid", gap: 4 }}>
            <span style={{ fontSize: 13, opacity: 0.84 }}>Church URL slug</span>
            <input
              value={churchSlug}
              onChange={(e) => {
                setSlugTouched(true);
                setChurchSlug(normalizeChurchSlug(e.target.value));
              }}
              required
              pattern="[a-z0-9-]+"
              title="Use lowercase letters, numbers, and hyphens."
              style={{ borderRadius: 10, border: "1px solid rgba(255,255,255,0.24)", background: "#0f172a", color: "#fff", padding: "10px 12px" }}
            />
          </label>
          <button
            type="submit"
            disabled={disabled}
            style={{
              marginTop: 4,
              borderRadius: 10,
              border: "none",
              padding: "10px 12px",
              fontWeight: 700,
              background: "#22c55e",
              color: "#052e16",
              cursor: disabled ? "not-allowed" : "pointer",
              opacity: disabled ? 0.6 : 1,
            }}
          >
            {busy ? "Creating church..." : checkingMembership ? "Checking..." : "Create church"}
          </button>
        </form>
      </section>
    </main>
  );
}
