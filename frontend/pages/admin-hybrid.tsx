// pages/admin-hybrid.tsx
"use client";

import Head from "next/head";
import { useRouter } from "next/router";
import { useEffect, useMemo, useState } from "react";
import { fetchAuthMe, type OrgMembership } from "../lib/backendAuth";
import { useAuth } from "../lib/authContext";
import ScriptUpload from "../components/ScriptUpload";
import SermonPrep from "../components/SermonPrep";
import { useTranslationSocket } from "../utils/useTranslationSocket";
import CorrectionPad from "../components/CorrectionPad";

const SCRIPT_MANAGER_ROLES = new Set(["owner", "admin", "host"]);

export default function AdminHybrid() {
  const router = useRouter();
  const { user, loading: authLoading, getIdToken, logout } = useAuth();
  const queryOrgId = typeof router.query.orgId === "string" ? router.query.orgId : "";
  const [memberships, setMemberships] = useState<OrgMembership[]>([]);
  const [selectedOrgId, setSelectedOrgId] = useState("");
  const [membershipLoading, setMembershipLoading] = useState(true);
  const [membershipError, setMembershipError] = useState<string | null>(null);

  const { last } = useTranslationSocket();

  const selectedMembership = useMemo(
    () => memberships.find((row) => row.orgId === selectedOrgId) || null,
    [memberships, selectedOrgId],
  );
  const canManageScript = useMemo(() => {
    const role = (selectedMembership?.role || "").trim().toLowerCase();
    return SCRIPT_MANAGER_ROLES.has(role);
  }, [selectedMembership?.role]);

  useEffect(() => {
    if (authLoading) return;
    if (user) return;
    const nextPath = router.asPath || "/admin-hybrid";
    void router.replace(`/login?next=${encodeURIComponent(nextPath)}`);
  }, [authLoading, router, user]);

  useEffect(() => {
    if (authLoading || !user || !router.isReady) return;
    let cancelled = false;
    const load = async () => {
      setMembershipLoading(true);
      setMembershipError(null);
      try {
        const idToken = await getIdToken();
        if (!idToken || cancelled) return;
        const me = await fetchAuthMe(idToken);
        if (cancelled) return;
        const rows = me.memberships || [];
        setMemberships(rows);
        if (!rows.length) {
          setSelectedOrgId("");
          setMembershipError("No church memberships found. Join or create a church first.");
          return;
        }
        const fromQuery = queryOrgId && rows.some((row) => row.orgId === queryOrgId) ? queryOrgId : "";
        const fromCurrent =
          (me.currentOrgId || "").trim() && rows.some((row) => row.orgId === me.currentOrgId)
            ? String(me.currentOrgId)
            : "";
        const chosen = fromQuery || fromCurrent || rows[0].orgId;
        setSelectedOrgId(chosen);
        if (chosen !== queryOrgId) {
          void router.replace({ pathname: router.pathname, query: { ...router.query, orgId: chosen } }, undefined, { shallow: true });
        }
      } catch (err: unknown) {
        if (!cancelled) {
          const msg = err instanceof Error ? err.message : String(err);
          const lower = (msg || "").toLowerCase();
          if (
            lower.includes("sign in again") ||
            lower.includes("session is invalid") ||
            lower.includes("invalid id token") ||
            lower.includes("invalid_id_token") ||
            lower.includes("auth_required")
          ) {
            setMembershipError("Session expired. Redirecting to login...");
            const nextPath = router.asPath || "/admin-hybrid";
            await logout();
            void router.replace(`/login?next=${encodeURIComponent(nextPath)}`);
            return;
          }
          setMembershipError(msg || "Failed to load memberships.");
        }
      } finally {
        if (!cancelled) setMembershipLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [authLoading, getIdToken, logout, queryOrgId, router, user]);

  const onChangeOrg = async (nextOrgId: string) => {
    setSelectedOrgId(nextOrgId);
    await router.replace({ pathname: router.pathname, query: { ...router.query, orgId: nextOrgId } }, undefined, { shallow: true });
  };

  return (
    <>
      <Head>
        <title>Hybrid Admin Console</title>
      </Head>
      <main className="min-h-screen bg-gradient-to-b from-[#0b1220] via-[#0f172a] to-[#0b1220] text-slate-100">
        <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 px-5 py-8 md:py-10">
          <section className="rounded-[28px] border border-white/10 bg-black/25 p-5 shadow-[0_20px_50px_rgba(0,0,0,0.4)] backdrop-blur">
            <div className="space-y-3">
              <p className="text-xs uppercase tracking-[0.35em] text-white/55">Church workspace</p>
              <div className="flex flex-wrap items-center gap-3">
                <p className="text-sm font-medium text-white/80">Church</p>
                <select
                  value={selectedOrgId}
                  onChange={(e) => {
                    void onChangeOrg(e.target.value);
                  }}
                  disabled={membershipLoading || !memberships.length}
                  className="min-w-[260px] rounded-xl border border-white/20 bg-[#050b16] px-3 py-2 text-sm text-white focus:border-[#22d3ee] focus:outline-none"
                >
                  {memberships.map((row) => (
                    <option key={row.orgId} value={row.orgId}>
                      {row.name} ({row.slug}) {row.role ? `- ${row.role}` : ""}
                    </option>
                  ))}
                </select>
                {selectedMembership ? (
                  <span className="rounded-full border border-white/15 px-2 py-1 text-xs text-white/70">
                    Role: {selectedMembership.role || "viewer"}
                  </span>
                ) : null}
              </div>
              {membershipError ? <p className="mt-2 text-sm text-rose-200">{membershipError}</p> : null}
            </div>
          </section>

          <section className="flex flex-col gap-6">
            {canManageScript ? (
              <>
                <SermonPrep orgId={selectedOrgId} />
                <details className="rounded-[28px] border border-white/10 bg-black/25 p-5">
                  <summary className="cursor-pointer text-sm font-semibold text-white/85">
                    Legacy one-sentence-per-line upload
                  </summary>
                  <div className="mt-4">
                    <ScriptUpload orgId={selectedOrgId} />
                  </div>
                </details>
              </>
            ) : (
              <div className="rounded-[32px] border border-rose-300/25 bg-rose-500/10 p-5 text-sm text-rose-100">
                You do not have permission to manage sermon prep for this church.
              </div>
            )}
          </section>

          <section>
            <CorrectionPad last={last} />
          </section>
        </div>
      </main>
    </>
  );
}
