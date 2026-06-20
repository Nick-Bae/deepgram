"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { useAuth } from "../lib/authContext";
import { deleteSermon, listSermons } from "../lib/api/sermons";
import { SermonApiError, SermonSummary } from "../lib/types/sermon";

const DELETE_ROLES = new Set(["owner", "admin"]);

type Props = {
  orgId: string;
  role?: string;
};

export default function SermonsManager({ orgId, role = "" }: Props) {
  const { getIdToken } = useAuth();
  const normalizedRole = role.trim().toLowerCase();
  const isViewer = normalizedRole === "viewer";
  const canDelete = DELETE_ROLES.has(normalizedRole);
  const [sermons, setSermons] = useState<SermonSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deletingSermonId, setDeletingSermonId] = useState("");

  useEffect(() => {
    if (!orgId || isViewer) {
      setSermons([]);
      return;
    }

    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const idToken = await getIdToken();
        if (!idToken || cancelled) return;
        const rows = await listSermons(orgId, { idToken });
        if (!cancelled) setSermons(rows);
      } catch (err: unknown) {
        if (!cancelled) setError(errorMessage(err, "Failed to load sermons."));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void load();
    return () => {
      cancelled = true;
    };
  }, [getIdToken, isViewer, orgId]);

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
      await deleteSermon(orgId, sermon.sermonId, { idToken });
      setSermons((current) =>
        current.filter((row) => row.sermonId !== sermon.sermonId)
      );
    } catch (err: unknown) {
      setError(errorMessage(err, "Failed to delete sermon."));
    } finally {
      setDeletingSermonId("");
    }
  };

  return (
    <section className="rounded-[32px] border border-slate-200 bg-white p-6 shadow-[0_35px_120px_rgba(3,7,18,0.25)]">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-1">
          <p className="text-xs uppercase tracking-[0.35em] text-slate-500">
            Sermon Prep
          </p>
          <h2 className="text-xl font-semibold text-slate-900">Sermons</h2>
          <p className="text-sm text-slate-600">
            Import, review, and edit sermon translations before service.
          </p>
        </div>
        {!isViewer && orgId ? (
          <Link
            href={`/admin/sermons/new?orgId=${encodeURIComponent(orgId)}`}
            className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-700"
          >
            + New Sermon
          </Link>
        ) : null}
      </div>

      {error ? (
        <div className="mt-5 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
          {error}
        </div>
      ) : null}

      <div className="mt-5">
        {loading ? (
          <PanelMessage>Loading…</PanelMessage>
        ) : isViewer ? (
          <PanelMessage>
            Viewers cannot manage sermons. Ask an admin to grant host or admin
            access.
          </PanelMessage>
        ) : sermons.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-slate-300 bg-slate-50 p-10 text-center">
            <p className="text-sm text-slate-500">
              No sermons yet — create your first to start translating.
            </p>
            {orgId ? (
              <Link
                href={`/admin/sermons/new?orgId=${encodeURIComponent(orgId)}`}
                className="mt-4 inline-block rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
              >
                + New Sermon
              </Link>
            ) : null}
          </div>
        ) : (
          <div className="overflow-x-auto rounded-2xl border border-slate-200">
            <table className="min-w-full divide-y divide-slate-200 text-sm">
              <thead className="bg-slate-50 text-xs font-semibold uppercase tracking-wide text-slate-600">
                <tr>
                  <th className="px-4 py-3 text-left">Title</th>
                  <th className="px-4 py-3 text-left">Segments</th>
                  <th className="px-4 py-3 text-left">Status</th>
                  <th className="px-4 py-3 text-left">Updated</th>
                  {canDelete ? (
                    <th className="px-4 py-3 text-right">Actions</th>
                  ) : null}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {sermons.map((sermon) => (
                  <tr key={sermon.sermonId} className="hover:bg-slate-50">
                    <td className="px-4 py-3">
                      <Link
                        href={`/admin/sermons/${encodeURIComponent(sermon.sermonId)}?orgId=${encodeURIComponent(orgId)}`}
                        className="font-medium text-slate-900 hover:underline"
                      >
                        {sermon.title || "(untitled)"}
                      </Link>
                    </td>
                    <td className="px-4 py-3 text-slate-700">
                      {sermon.segmentCount}
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge
                        reviewed={sermon.reviewedCount}
                        total={sermon.segmentCount}
                      />
                    </td>
                    <td className="px-4 py-3 text-slate-600">
                      {formatRelative(sermon.updatedAt)}
                    </td>
                    {canDelete ? (
                      <td className="px-4 py-3 text-right">
                        <button
                          type="button"
                          onClick={() => void handleDelete(sermon)}
                          disabled={Boolean(deletingSermonId)}
                          className="rounded-lg border border-red-200 px-3 py-1.5 text-xs font-medium text-red-700 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {deletingSermonId === sermon.sermonId
                            ? "Deleting…"
                            : "Delete"}
                        </button>
                      </td>
                    ) : null}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

function PanelMessage({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-slate-50 p-8 text-center text-sm text-slate-500">
      {children}
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

function formatRelative(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  const diffMs = Date.now() - date.getTime();
  const diffDays = Math.floor(diffMs / 86_400_000);
  if (diffDays <= 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 30) return `${diffDays} days ago`;
  return date.toLocaleDateString();
}

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof SermonApiError) return err.message;
  if (err instanceof Error) return err.message;
  return fallback;
}
