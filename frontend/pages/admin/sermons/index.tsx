// Design Ref: §5.1 + §5.4 — /admin/sermons (List)
// Auth-gated list of sermons for the selected org with status badges,
// reviewed-% display, and linked-service column.

import Head from "next/head";
import Link from "next/link";
import { useRouter } from "next/router";
import { useEffect, useMemo, useState } from "react";

import { useAuth } from "../../../lib/authContext";
import { fetchAuthMe, type OrgMembership } from "../../../lib/backendAuth";
import { deleteSermon, listSermons } from "../../../lib/api/sermons";
import { SermonApiError, SermonSummary } from "../../../lib/types/sermon";

const VIEWER_ROLE = "viewer";
const DELETE_ROLES = new Set(["owner", "admin"]);

export default function AdminSermonsListPage() {
  const router = useRouter();
  const { user, loading: authLoading, getIdToken, logout } = useAuth();
  const queryOrgId =
    typeof router.query.orgId === "string" ? router.query.orgId : "";

  const [memberships, setMemberships] = useState<OrgMembership[]>([]);
  const [selectedOrgId, setSelectedOrgId] = useState("");
  const [membershipLoading, setMembershipLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sermons, setSermons] = useState<SermonSummary[]>([]);
  const [loadingSermons, setLoadingSermons] = useState(false);
  const [deletingSermonId, setDeletingSermonId] = useState("");

  const selectedMembership = useMemo(
    () => memberships.find((row) => row.orgId === selectedOrgId) || null,
    [memberships, selectedOrgId]
  );
  const isViewer =
    (selectedMembership?.role || "").trim().toLowerCase() === VIEWER_ROLE;
  const canDelete = DELETE_ROLES.has(
    (selectedMembership?.role || "").trim().toLowerCase()
  );

  const handleDelete = async (sermon: SermonSummary) => {
    if (!canDelete || deletingSermonId) return;
    const confirmed = window.confirm(
      `Delete "${sermon.title || "(untitled)"}"? This permanently removes the transcript and its reviewed translations.`
    );
    if (!confirmed) return;

    setDeletingSermonId(sermon.sermonId);
    setError(null);
    try {
      const idToken = await getIdToken();
      if (!idToken) {
        setError("You are signed out. Please sign in again.");
        return;
      }
      await deleteSermon(selectedOrgId, sermon.sermonId, { idToken });
      setSermons((current) =>
        current.filter((row) => row.sermonId !== sermon.sermonId)
      );
    } catch (err: unknown) {
      if (err instanceof SermonApiError) setError(err.message);
      else if (err instanceof Error) setError(err.message);
      else setError("Failed to delete sermon.");
    } finally {
      setDeletingSermonId("");
    }
  };

  useEffect(() => {
    if (authLoading) return;
    if (user) return;
    const nextPath = router.asPath || "/admin/sermons";
    void router.replace(`/login?next=${encodeURIComponent(nextPath)}`);
  }, [authLoading, router, user]);

  useEffect(() => {
    if (authLoading || !user || !router.isReady) return;
    let cancelled = false;
    const run = async () => {
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
          queryOrgId && rows.some((r) => r.orgId === queryOrgId)
            ? queryOrgId
            : "";
        const fromCurrent =
          (me.currentOrgId || "") &&
          rows.some((r) => r.orgId === me.currentOrgId)
            ? String(me.currentOrgId)
            : "";
        const chosen = fromQuery || fromCurrent || rows[0].orgId;
        setSelectedOrgId(chosen);
        if (chosen !== queryOrgId) {
          void router.replace(
            { pathname: router.pathname, query: { ...router.query, orgId: chosen } },
            undefined,
            { shallow: true }
          );
        }
      } catch (err: unknown) {
        if (!cancelled) {
          const msg = err instanceof Error ? err.message : String(err);
          if (/sign in again|auth_required|invalid id token/i.test(msg)) {
            await logout();
            void router.replace(
              `/login?next=${encodeURIComponent(router.asPath || "/admin/sermons")}`
            );
            return;
          }
          setError(msg);
        }
      } finally {
        if (!cancelled) setMembershipLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [authLoading, getIdToken, logout, queryOrgId, router, user]);

  useEffect(() => {
    if (!selectedOrgId || !user || isViewer) {
      setSermons([]);
      return;
    }
    let cancelled = false;
    const load = async () => {
      setLoadingSermons(true);
      setError(null);
      try {
        const idToken = await getIdToken();
        if (!idToken || cancelled) return;
        const list = await listSermons(selectedOrgId, { idToken });
        if (!cancelled) setSermons(list);
      } catch (err: unknown) {
        if (!cancelled) {
          if (err instanceof SermonApiError) setError(err.message);
          else if (err instanceof Error) setError(err.message);
          else setError("Failed to load sermons.");
        }
      } finally {
        if (!cancelled) setLoadingSermons(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [selectedOrgId, user, isViewer, getIdToken]);

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
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-xs uppercase tracking-[0.22em] text-slate-500">
                  Admin
                </p>
                <h1 className="text-2xl font-semibold text-slate-900">Sermons</h1>
                <p className="text-sm text-slate-600">
                  Review and edit sermon translations before service.
                </p>
              </div>
              {!isViewer && (
                <Link
                  href={
                    selectedOrgId
                      ? `/admin/sermons/new?orgId=${encodeURIComponent(selectedOrgId)}`
                      : "/admin/sermons/new"
                  }
                  className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-700"
                >
                  + New Sermon
                </Link>
              )}
            </div>

            {memberships.length > 1 && (
              <div>
                <label className="text-xs font-medium text-slate-600">
                  Church
                </label>
                <select
                  value={selectedOrgId}
                  onChange={(e) => {
                    const next = e.target.value;
                    setSelectedOrgId(next);
                    void router.replace(
                      { pathname: router.pathname, query: { ...router.query, orgId: next } },
                      undefined,
                      { shallow: true }
                    );
                  }}
                  className="mt-1 block w-full max-w-md rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-900"
                >
                  {memberships.map((m) => (
                    <option key={m.orgId} value={m.orgId}>
                      {m.name} ({m.role || "member"})
                    </option>
                  ))}
                </select>
              </div>
            )}
          </header>

          {error && (
            <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
              {error}
            </div>
          )}

          {membershipLoading || loadingSermons ? (
            <div className="rounded-2xl border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
              Loading…
            </div>
          ) : isViewer ? (
            <div className="rounded-2xl border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
              Viewers cannot manage sermons. Ask an admin to grant host or
              admin role to edit.
            </div>
          ) : sermons.length === 0 ? (
            <EmptyState orgId={selectedOrgId} />
          ) : (
            <SermonTable
              orgId={selectedOrgId}
              sermons={sermons}
              canDelete={canDelete}
              deletingSermonId={deletingSermonId}
              onDelete={handleDelete}
            />
          )}
        </div>
      </main>
    </>
  );
}

function EmptyState({ orgId }: { orgId: string }) {
  return (
    <div className="rounded-2xl border border-dashed border-slate-300 bg-white p-10 text-center">
      <p className="text-sm text-slate-500">
        No sermons yet — create your first to start translating.
      </p>
      <Link
        href={
          orgId
            ? `/admin/sermons/new?orgId=${encodeURIComponent(orgId)}`
            : "/admin/sermons/new"
        }
        className="mt-4 inline-block rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-700"
      >
        + New Sermon
      </Link>
    </div>
  );
}

function SermonTable({
  orgId,
  sermons,
  canDelete,
  deletingSermonId,
  onDelete,
}: {
  orgId: string;
  sermons: SermonSummary[];
  canDelete: boolean;
  deletingSermonId: string;
  onDelete: (sermon: SermonSummary) => void;
}) {
  return (
    <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
      <table className="min-w-full divide-y divide-slate-200 text-sm">
        <thead className="bg-slate-50 text-xs font-semibold uppercase tracking-wide text-slate-600">
          <tr>
            <th className="px-4 py-3 text-left">Title</th>
            <th className="px-4 py-3 text-left">Segments</th>
            <th className="px-4 py-3 text-left">Status</th>
            <th className="px-4 py-3 text-left">Updated</th>
            {canDelete && <th className="px-4 py-3 text-right">Actions</th>}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {sermons.map((s) => (
            <tr key={s.sermonId} className="hover:bg-slate-50">
              <td className="px-4 py-3">
                <Link
                  href={`/admin/sermons/${encodeURIComponent(s.sermonId)}?orgId=${encodeURIComponent(orgId)}`}
                  className="font-medium text-slate-900 hover:underline"
                >
                  {s.title || "(untitled)"}
                </Link>
              </td>
              <td className="px-4 py-3 text-slate-700">{s.segmentCount}</td>
              <td className="px-4 py-3">
                <StatusBadge
                  reviewed={s.reviewedCount}
                  total={s.segmentCount}
                />
              </td>
              <td className="px-4 py-3 text-slate-600">
                {formatRelative(s.updatedAt)}
              </td>
              {canDelete && (
                <td className="px-4 py-3 text-right">
                  <button
                    type="button"
                    onClick={() => onDelete(s)}
                    disabled={Boolean(deletingSermonId)}
                    className="rounded-lg border border-red-200 px-3 py-1.5 text-xs font-medium text-red-700 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {deletingSermonId === s.sermonId ? "Deleting…" : "Delete"}
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StatusBadge({ reviewed, total }: { reviewed: number; total: number }) {
  if (total === 0) {
    return (
      <span className="rounded-full bg-slate-100 px-2 py-1 text-xs font-medium text-slate-600">
        Empty
      </span>
    );
  }
  if (reviewed === 0) {
    return (
      <span className="rounded-full bg-amber-100 px-2 py-1 text-xs font-medium text-amber-800">
        Draft
      </span>
    );
  }
  if (reviewed === total) {
    return (
      <span className="rounded-full bg-emerald-100 px-2 py-1 text-xs font-medium text-emerald-800">
        Fully reviewed
      </span>
    );
  }
  const pct = Math.round((reviewed / total) * 100);
  return (
    <span className="rounded-full bg-blue-100 px-2 py-1 text-xs font-medium text-blue-800">
      {pct}% reviewed
    </span>
  );
}

function formatRelative(iso: string): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const diffSec = Math.round((Date.now() - t) / 1000);
  if (diffSec < 60) return "just now";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  if (diffSec < 86400 * 7) return `${Math.floor(diffSec / 86400)}d ago`;
  return new Date(t).toLocaleDateString();
}
