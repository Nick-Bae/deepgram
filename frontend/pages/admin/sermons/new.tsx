// Design Ref: §5.4 — /admin/sermons/new. Thin wrapper around SermonIngestForm
// that handles auth + org resolution and redirects to the detail page on
// success.

import Head from "next/head";
import Link from "next/link";
import { useRouter } from "next/router";
import { useEffect, useMemo, useState } from "react";

import SermonIngestForm from "../../../components/SermonIngestForm";
import { useAuth } from "../../../lib/authContext";
import { fetchAuthMe, type OrgMembership } from "../../../lib/backendAuth";

const VIEWER_ROLE = "viewer";

export default function AdminSermonNewPage() {
  const router = useRouter();
  const { user, loading: authLoading, getIdToken } = useAuth();
  const queryOrgId =
    typeof router.query.orgId === "string" ? router.query.orgId : "";

  const [memberships, setMemberships] = useState<OrgMembership[]>([]);
  const [selectedOrgId, setSelectedOrgId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const selectedMembership = useMemo(
    () => memberships.find((m) => m.orgId === selectedOrgId) || null,
    [memberships, selectedOrgId]
  );
  const isViewer =
    (selectedMembership?.role || "").trim().toLowerCase() === VIEWER_ROLE;

  useEffect(() => {
    if (authLoading) return;
    if (user) return;
    void router.replace(
      `/login?next=${encodeURIComponent(router.asPath || "/admin/sermons/new")}`
    );
  }, [authLoading, router, user]);

  useEffect(() => {
    if (authLoading || !user || !router.isReady) return;
    let cancelled = false;
    const run = async () => {
      setLoading(true);
      try {
        const idToken = await getIdToken();
        if (!idToken || cancelled) return;
        const me = await fetchAuthMe(idToken);
        if (cancelled) return;
        const rows = me.memberships || [];
        setMemberships(rows);
        if (!rows.length) {
          setError("No church memberships found.");
          return;
        }
        const fromQuery =
          queryOrgId && rows.some((r) => r.orgId === queryOrgId)
            ? queryOrgId
            : "";
        const chosen = fromQuery || rows[0].orgId;
        setSelectedOrgId(chosen);
      } catch (err: unknown) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [authLoading, getIdToken, queryOrgId, router, user]);

  if (authLoading || !user || loading) {
    return (
      <main className="grid min-h-screen place-items-center bg-slate-100 text-slate-600">
        Loading…
      </main>
    );
  }

  return (
    <>
      <Head>
        <title>New Sermon — Admin</title>
      </Head>
      <main className="min-h-screen bg-slate-100 text-slate-900">
        <div className="mx-auto w-full max-w-3xl px-4 py-6 md:px-6">
          <Link
            href={
              selectedOrgId
                ? `/admin/sermons?orgId=${encodeURIComponent(selectedOrgId)}`
                : "/admin/sermons"
            }
            className="mb-4 inline-block text-sm text-slate-600 hover:text-slate-900"
          >
            ← Back to sermons
          </Link>

          {error && (
            <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
              {error}
            </div>
          )}

          {isViewer ? (
            <div className="rounded-2xl border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
              Viewers cannot create sermons. Ask an admin to grant host or
              admin role.
            </div>
          ) : selectedOrgId ? (
            <SermonIngestForm
              orgId={selectedOrgId}
              onCancel={() =>
                router.push(
                  `/admin/sermons?orgId=${encodeURIComponent(selectedOrgId)}`
                )
              }
              onSuccess={(result) => {
                void router.replace(
                  `/admin/sermons/${encodeURIComponent(result.sermonId)}?orgId=${encodeURIComponent(selectedOrgId)}`
                );
              }}
            />
          ) : null}
        </div>
      </main>
    </>
  );
}
