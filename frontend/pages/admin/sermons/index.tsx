import Head from "next/head";
import Link from "next/link";
import { useRouter } from "next/router";
import { useEffect, useMemo, useState } from "react";

import SermonsManager from "../../../components/SermonsManager";
import { useAuth } from "../../../lib/authContext";
import { fetchAuthMe, type OrgMembership } from "../../../lib/backendAuth";

export default function AdminSermonsListPage() {
  const router = useRouter();
  const { user, loading: authLoading, getIdToken, logout } = useAuth();
  const queryOrgId =
    typeof router.query.orgId === "string" ? router.query.orgId : "";

  const [memberships, setMemberships] = useState<OrgMembership[]>([]);
  const [selectedOrgId, setSelectedOrgId] = useState("");
  const [membershipLoading, setMembershipLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const selectedMembership = useMemo(
    () => memberships.find((row) => row.orgId === selectedOrgId) || null,
    [memberships, selectedOrgId]
  );
  const dashboardHref = selectedMembership?.slug
    ? `/host/c/${encodeURIComponent(selectedMembership.slug)}/broadcast?orgId=${encodeURIComponent(selectedOrgId)}`
    : "";
  const sermonPrepHref = selectedOrgId
    ? `/admin/sermon-prep?orgId=${encodeURIComponent(selectedOrgId)}`
    : "/admin/sermon-prep";

  useEffect(() => {
    if (authLoading || user) return;
    const nextPath = router.asPath || "/admin/sermons";
    void router.replace(`/login?next=${encodeURIComponent(nextPath)}`);
  }, [authLoading, router, user]);

  useEffect(() => {
    if (authLoading || !user || !router.isReady) return;
    let cancelled = false;

    const loadMemberships = async () => {
      setMembershipLoading(true);
      setError(null);
      try {
        const idToken = await getIdToken();
        if (!idToken || cancelled) return;
        const me = await fetchAuthMe(idToken);
        if (cancelled) return;

        const rows = me.memberships || [];
        setMemberships(rows);
        if (!rows.length) {
          setSelectedOrgId("");
          setError("No church memberships found.");
          return;
        }

        const fromQuery =
          queryOrgId && rows.some((row) => row.orgId === queryOrgId)
            ? queryOrgId
            : "";
        const fromCurrent =
          (me.currentOrgId || "").trim() &&
          rows.some((row) => row.orgId === me.currentOrgId)
            ? String(me.currentOrgId)
            : "";
        const chosen = fromQuery || fromCurrent || rows[0].orgId;
        setSelectedOrgId(chosen);

        if (chosen !== queryOrgId) {
          void router.replace(
            {
              pathname: router.pathname,
              query: { ...router.query, orgId: chosen },
            },
            undefined,
            { shallow: true }
          );
        }
      } catch (err: unknown) {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : String(err);
        if (/sign in again|auth_required|invalid id token/i.test(message)) {
          await logout();
          void router.replace(
            `/login?next=${encodeURIComponent(router.asPath || "/admin/sermons")}`
          );
          return;
        }
        setError(message || "Failed to load memberships.");
      } finally {
        if (!cancelled) setMembershipLoading(false);
      }
    };

    void loadMemberships();
    return () => {
      cancelled = true;
    };
  }, [authLoading, getIdToken, logout, queryOrgId, router, user]);

  const onChangeOrg = async (nextOrgId: string) => {
    setSelectedOrgId(nextOrgId);
    await router.replace(
      {
        pathname: router.pathname,
        query: { ...router.query, orgId: nextOrgId },
      },
      undefined,
      { shallow: true }
    );
  };

  if (authLoading || !user) {
    return (
      <main className="grid min-h-screen place-items-center bg-slate-100 text-slate-600">
        Loading…
      </main>
    );
  }

  return (
    <>
      <Head>
        <title>Sermons — Admin</title>
      </Head>
      <main className="min-h-screen bg-slate-100 text-slate-900">
        <div className="mx-auto w-full max-w-6xl px-4 py-6 md:px-6">
          <header className="mb-5 space-y-3 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="text-xs uppercase tracking-[0.22em] text-slate-500">
                  Admin
                </p>
                <h1 className="text-2xl font-semibold text-slate-900">Sermons</h1>
                <p className="text-sm text-slate-600">
                  Review and edit sermon translations before service.
                </p>
              </div>
              <nav className="flex flex-wrap gap-2" aria-label="Admin navigation">
                {dashboardHref ? (
                  <Link
                    href={dashboardHref}
                    className="rounded-lg bg-slate-900 px-3 py-2 text-sm text-white"
                  >
                    Back to Dashboard
                  </Link>
                ) : null}
                <Link
                  href={sermonPrepHref}
                  className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700"
                >
                  Sermon Prep
                </Link>
                <Link
                  href="/admin"
                  className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700"
                >
                  Admin Home
                </Link>
                <Link
                  href="/admin-hybrid"
                  className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700"
                >
                  Hybrid Console
                </Link>
              </nav>
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <label className="text-sm font-medium text-slate-700">Church</label>
              <select
                value={selectedOrgId}
                onChange={(event) => void onChangeOrg(event.target.value)}
                disabled={membershipLoading || !memberships.length}
                className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-sky-500 focus:outline-none"
              >
                {memberships.map((membership) => (
                  <option key={membership.orgId} value={membership.orgId}>
                    {membership.name} ({membership.slug}){" "}
                    {membership.role ? `- ${membership.role}` : ""}
                  </option>
                ))}
              </select>
              {selectedMembership ? (
                <span className="rounded-full border border-slate-300 px-2 py-1 text-xs text-slate-600">
                  Role: {selectedMembership.role || "viewer"}
                </span>
              ) : null}
            </div>
          </header>

          {error ? (
            <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
              {error}
            </div>
          ) : null}

          {membershipLoading ? (
            <div className="rounded-2xl border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
              Loading…
            </div>
          ) : selectedMembership ? (
            <SermonsManager
              orgId={selectedOrgId}
              role={selectedMembership.role}
            />
          ) : null}
        </div>
      </main>
    </>
  );
}
