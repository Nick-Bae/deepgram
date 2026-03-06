import Head from "next/head";
import Link from "next/link";
import { useRouter } from "next/router";
import { useEffect, useMemo, useState } from "react";

import SermonPrep from "../../components/SermonPrep";
import { useAuth } from "../../lib/authContext";
import { fetchAuthMe, type OrgMembership } from "../../lib/backendAuth";

const SCRIPT_MANAGER_ROLES = new Set(["owner", "admin", "host"]);

export default function AdminSermonPrepPage() {
  const router = useRouter();
  const { user, loading: authLoading, getIdToken } = useAuth();
  const queryOrgId = typeof router.query.orgId === "string" ? router.query.orgId : "";
  const [memberships, setMemberships] = useState<OrgMembership[]>([]);
  const [selectedOrgId, setSelectedOrgId] = useState("");
  const [membershipLoading, setMembershipLoading] = useState(true);
  const [membershipError, setMembershipError] = useState<string | null>(null);

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
    const nextPath = router.asPath || "/admin/sermon-prep";
    void router.replace(`/login?next=${encodeURIComponent(nextPath)}`);
  }, [authLoading, router, user]);

  useEffect(() => {
    if (authLoading || !user || !router.isReady) return;
    let cancelled = false;
    const loadMemberships = async () => {
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
          void router.replace({ pathname: router.pathname, query: { ...router.query, orgId: chosen } }, undefined, {
            shallow: true,
          });
        }
      } catch (err: unknown) {
        if (!cancelled) {
          const msg = err instanceof Error ? err.message : String(err);
          setMembershipError(msg || "Failed to load memberships.");
        }
      } finally {
        if (!cancelled) setMembershipLoading(false);
      }
    };
    void loadMemberships();
    return () => {
      cancelled = true;
    };
  }, [authLoading, getIdToken, queryOrgId, router, user]);

  const onChangeOrg = async (nextOrgId: string) => {
    setSelectedOrgId(nextOrgId);
    await router.replace({ pathname: router.pathname, query: { ...router.query, orgId: nextOrgId } }, undefined, {
      shallow: true,
    });
  };

  return (
    <>
      <Head>
        <title>Sermon Prep</title>
      </Head>
      <main className="min-h-screen bg-slate-100 text-slate-900">
        <div className="mx-auto w-full max-w-6xl px-4 py-6 md:px-6">
          <header className="mb-5 space-y-3 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-xs uppercase tracking-[0.22em] text-slate-500">Admin</p>
                <h1 className="text-2xl font-semibold text-slate-900">Sermon Prep</h1>
                <p className="text-sm text-slate-600">
                  Generate a Korean-to-English draft, edit it, and save final script pairs before service.
                </p>
              </div>
              <div className="flex gap-2">
                <Link href="/admin" className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700">
                  Admin Home
                </Link>
                <Link href="/admin-hybrid" className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700">
                  Hybrid Console
                </Link>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <label className="text-sm font-medium text-slate-700">Church</label>
              <select
                value={selectedOrgId}
                onChange={(e) => {
                  void onChangeOrg(e.target.value);
                }}
                disabled={membershipLoading || !memberships.length}
                className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-sky-500 focus:outline-none"
              >
                {memberships.map((row) => (
                  <option key={row.orgId} value={row.orgId}>
                    {row.name} ({row.slug}) {row.role ? `- ${row.role}` : ""}
                  </option>
                ))}
              </select>
              {selectedMembership ? (
                <span className="rounded-full border border-slate-300 px-2 py-1 text-xs text-slate-600">
                  Role: {selectedMembership.role || "viewer"}
                </span>
              ) : null}
            </div>
            {membershipError ? <p className="text-sm text-rose-600">{membershipError}</p> : null}
          </header>

          {canManageScript ? (
            <SermonPrep orgId={selectedOrgId} />
          ) : (
            <div className="rounded-2xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
              Sermon prep is available to owner/admin/host roles.
            </div>
          )}
        </div>
      </main>
    </>
  );
}
