// Design Ref: §5.4 — /admin/sermons/[sermonId] (Detail).
// Header, SermonReviewControls, linked-service section (owner/admin only),
// segments list with pagination.

import Head from "next/head";
import Link from "next/link";
import { useRouter } from "next/router";
import { useCallback, useEffect, useMemo, useState } from "react";

import SermonReviewControls from "../../../components/SermonReviewControls";
import { useAuth } from "../../../lib/authContext";
import {
  fetchAuthMe,
  fetchOrgServices,
  type OrgMembership,
  type OrgServiceSummary,
} from "../../../lib/backendAuth";
import { getSermon, lintSermon, linkSermon } from "../../../lib/api/sermons";
import {
  SegmentStatus,
  Sermon,
  SermonApiError,
  SermonLintReport,
} from "../../../lib/types/sermon";

const ADMIN_ROLES = new Set(["owner", "admin"]);
const PAGE_SIZE = 50;

export default function AdminSermonDetailPage() {
  const router = useRouter();
  const { user, loading: authLoading, getIdToken } = useAuth();
  const sermonId =
    typeof router.query.sermonId === "string" ? router.query.sermonId : "";
  const queryOrgId =
    typeof router.query.orgId === "string" ? router.query.orgId : "";

  const [memberships, setMemberships] = useState<OrgMembership[]>([]);
  const [selectedOrgId, setSelectedOrgId] = useState("");
  const [services, setServices] = useState<OrgServiceSummary[]>([]);
  const [sermon, setSermon] = useState<Sermon | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);

  const [linkServiceKey, setLinkServiceKey] = useState("");
  const [linking, setLinking] = useState(false);
  const [linkError, setLinkError] = useState<string | null>(null);
  const [currentLinkedServiceKey, setCurrentLinkedServiceKey] = useState<string | null>(null);

  const [lintReport, setLintReport] = useState<SermonLintReport | null>(null);
  const [linting_, setLintBusy] = useState(false);
  const [lintError, setLintError] = useState<string | null>(null);

  const role = useMemo(() => {
    const m = memberships.find((row) => row.orgId === selectedOrgId);
    return (m?.role || "").trim().toLowerCase();
  }, [memberships, selectedOrgId]);
  const canLink = ADMIN_ROLES.has(role);
  const isViewer = role === "viewer";
  const selectedLinkService = useMemo(
    () => services.find((service) => service.serviceKey === linkServiceKey) || null,
    [linkServiceKey, services]
  );
  const selectedLinkedSermonId = selectedLinkService?.linkedSermonId || "";
  const selectedServiceLinkedToOther = Boolean(
    selectedLinkedSermonId && selectedLinkedSermonId !== sermonId
  );

  useEffect(() => {
    if (authLoading) return;
    if (user) return;
    void router.replace(
      `/login?next=${encodeURIComponent(router.asPath || "/admin/sermons")}`
    );
  }, [authLoading, router, user]);

  // Bootstrap memberships + select org
  useEffect(() => {
    if (authLoading || !user || !router.isReady) return;
    let cancelled = false;
    const run = async () => {
      try {
        const idToken = await getIdToken();
        if (!idToken || cancelled) return;
        const me = await fetchAuthMe(idToken);
        if (cancelled) return;
        const rows = me.memberships || [];
        setMemberships(rows);
        const chosen =
          (queryOrgId && rows.some((r) => r.orgId === queryOrgId)
            ? queryOrgId
            : "") || rows[0]?.orgId || "";
        setSelectedOrgId(chosen);
      } catch (err: unknown) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [authLoading, getIdToken, queryOrgId, router, user]);

  // Load sermon + services for the org
  const loadAll = useCallback(async () => {
    if (!selectedOrgId || !sermonId || isViewer) return;
    setLoading(true);
    setError(null);
    try {
      const idToken = await getIdToken();
      if (!idToken) return;
      const [s, svcResult] = await Promise.all([
        getSermon(selectedOrgId, sermonId, { idToken }),
        fetchOrgServices(idToken, selectedOrgId).catch(
          () => ({ services: [] as OrgServiceSummary[] })
        ),
      ]);
      setSermon(s);
      setServices(svcResult.services);
      setCurrentLinkedServiceKey(
        svcResult.services.find((service) => service.linkedSermonId === sermonId)
          ?.serviceKey || null
      );
    } catch (err: unknown) {
      if (err instanceof SermonApiError) setError(err.message);
      else if (err instanceof Error) setError(err.message);
      else setError("Failed to load sermon.");
    } finally {
      setLoading(false);
    }
  }, [selectedOrgId, sermonId, isViewer, getIdToken]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  const handleLint = useCallback(async () => {
    if (!selectedOrgId || !sermonId) return;
    setLintBusy(true);
    setLintError(null);
    try {
      const idToken = await getIdToken();
      if (!idToken) return;
      const report = await lintSermon(selectedOrgId, sermonId, { idToken });
      setLintReport(report);
    } catch (err: unknown) {
      if (err instanceof SermonApiError) setLintError(err.message);
      else if (err instanceof Error) setLintError(err.message);
      else setLintError("Failed to lint sermon.");
    } finally {
      setLintBusy(false);
    }
  }, [selectedOrgId, sermonId, getIdToken]);

  const handleLink = useCallback(
    async (replace = false) => {
      if (!selectedOrgId || !sermonId || !linkServiceKey) return;
      setLinkError(null);
      setLinking(true);
      try {
        const idToken = await getIdToken();
        if (!idToken) return;
        await linkSermon(
          { orgId: selectedOrgId, sermonId, serviceKey: linkServiceKey, replace },
          { idToken }
        );
        setCurrentLinkedServiceKey(linkServiceKey);
        setServices((prev) =>
          prev.map((service) =>
            service.serviceKey === linkServiceKey
              ? { ...service, linkedSermonId: sermonId }
              : service.linkedSermonId === sermonId
                ? { ...service, linkedSermonId: null }
                : service
          )
        );
      } catch (err: unknown) {
        if (err instanceof SermonApiError && err.code === "SERVICE_ALREADY_LINKED") {
          if (window.confirm(`That service is already linked to another sermon. Replace the existing link?`)) {
            await handleLink(true);
            return;
          }
        } else if (err instanceof SermonApiError) {
          setLinkError(err.message);
        } else if (err instanceof Error) {
          setLinkError(err.message);
        } else {
          setLinkError("Unknown error.");
        }
      } finally {
        setLinking(false);
      }
    },
    [selectedOrgId, sermonId, linkServiceKey, getIdToken]
  );

  if (authLoading || !user || loading) {
    return (
      <main className="grid min-h-screen place-items-center bg-slate-100 text-slate-600">
        Loading…
      </main>
    );
  }
  if (isViewer) {
    return (
      <main className="grid min-h-screen place-items-center bg-slate-100">
        <p className="text-sm text-slate-500">
          Viewers cannot view sermon details.
        </p>
      </main>
    );
  }
  if (error) {
    return (
      <main className="min-h-screen bg-slate-100 p-6">
        <div className="mx-auto max-w-3xl rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          {error}
        </div>
      </main>
    );
  }
  if (!sermon) return null;

  const reviewedCount = sermon.segments.filter((s) => s.status === "Reviewed").length;
  const totalCount = sermon.segments.length;
  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));
  const pagedSegments = sermon.segments.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  return (
    <>
      <Head>
        <title>{sermon.title} — Sermons</title>
      </Head>
      <main className="min-h-screen bg-slate-100 text-slate-900">
        <div className="mx-auto w-full max-w-6xl space-y-5 px-4 py-6 md:px-6">
          <Link
            href={`/admin/sermons?orgId=${encodeURIComponent(selectedOrgId)}`}
            className="inline-block text-sm text-slate-600 hover:text-slate-900"
          >
            ← Back to sermons
          </Link>

          <header className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="text-xs uppercase tracking-[0.22em] text-slate-500">
                  Sermon
                </p>
                <h1 className="text-2xl font-semibold text-slate-900">{sermon.title}</h1>
                <p className="mt-1 text-sm text-slate-600">
                  {totalCount} segment{totalCount === 1 ? "" : "s"} •{" "}
                  {totalCount === 0
                    ? "Empty"
                    : reviewedCount === totalCount
                      ? "Fully reviewed"
                      : reviewedCount === 0
                        ? "Draft"
                        : `${Math.round((reviewedCount / totalCount) * 100)}% reviewed`}
                </p>
              </div>
            </div>
          </header>

          <SermonReviewControls
            orgId={selectedOrgId}
            sermonId={sermon.sermonId}
            sermonTitle={sermon.title}
            reviewMode={sermon.reviewMode}
            onImportSuccess={() => void loadAll()}
          />

          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
              <div>
                <h3 className="text-base font-semibold text-slate-900">
                  Manuscript Analysis
                </h3>
                <p className="text-xs text-slate-500">
                  Scan for repeated phrases (scripture quotes, refrains) that
                  can confuse the live matcher and delay previews.
                </p>
              </div>
              <button
                type="button"
                onClick={() => void handleLint()}
                disabled={linting_}
                className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
              >
                {linting_ ? "Analyzing…" : "Analyze"}
              </button>
            </div>

            {lintError && (
              <p className="mt-2 text-sm text-red-700">{lintError}</p>
            )}

            {lintReport && (
              <div className="mt-3">
                {lintReport.collisions.length === 0 ? (
                  <p className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
                    Scanned {lintReport.totalSegments} segments — no repeated-phrase
                    collisions found. The live matcher should track cleanly.
                  </p>
                ) : (
                  <>
                    <p className="mb-2 text-sm text-slate-700">
                      Scanned {lintReport.totalSegments} segments — found{" "}
                      <strong>{lintReport.collisions.length}</strong> collision
                      pair
                      {lintReport.collisions.length === 1 ? "" : "s"}. The
                      cursor-sync fix keeps these safe, but reviewing them
                      helps you understand where the matcher has to work
                      hardest.
                    </p>
                    <ul className="divide-y divide-slate-100">
                      {lintReport.collisions.map((c, i) => {
                        const isHigh = c.severity === "high";
                        return (
                          <li key={`${c.shorterSegmentId}-${c.longerSegmentId}-${i}`} className="py-2">
                            <div className="flex flex-wrap items-center gap-2 text-xs">
                              <span
                                className={
                                  isHigh
                                    ? "rounded bg-amber-100 px-2 py-0.5 font-medium text-amber-800"
                                    : "rounded bg-slate-100 px-2 py-0.5 font-medium text-slate-700"
                                }
                              >
                                {isHigh ? "high" : "medium"}
                              </span>
                              <span className="font-mono text-slate-500">
                                {c.shorterSegmentId} ⊂ {c.longerSegmentId}
                              </span>
                              <span className="text-slate-500">
                                gap {c.gap} segments
                              </span>
                            </div>
                            <p className="mt-1 text-sm text-slate-800">
                              {c.matchedText}
                            </p>
                          </li>
                        );
                      })}
                    </ul>
                  </>
                )}
              </div>
            )}
          </section>

          {canLink && (
            <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
              <h3 className="text-base font-semibold text-slate-900">
                Linked Service
              </h3>
              <p className="mb-3 text-xs text-slate-500">
                When linked, the live broadcast prefers this sermon&rsquo;s
                reviewed translations over machine output.
              </p>
              {currentLinkedServiceKey ? (
                <p className="mb-3 text-sm text-emerald-700">
                  Currently linked to:{" "}
                  <strong>{currentLinkedServiceKey}</strong>
                </p>
              ) : (
                <p className="mb-3 text-sm text-slate-600">Not linked.</p>
              )}
              <div className="flex flex-wrap items-center gap-3">
                <select
                  value={linkServiceKey}
                  onChange={(e) => setLinkServiceKey(e.target.value)}
                  className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
                >
                  <option value="">Choose a service…</option>
                  {services.map((s) => (
                    <option key={s.serviceKey} value={s.serviceKey}>
                      {s.title} ({s.serviceKey})
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={() => void handleLink(selectedServiceLinkedToOther)}
                  disabled={!linkServiceKey || linking}
                  className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
                >
                  {linking
                    ? "Linking…"
                    : selectedServiceLinkedToOther
                      ? "Replace Link"
                      : "Link"}
                </button>
              </div>
              {selectedServiceLinkedToOther && (
                <p className="mt-2 text-sm text-amber-700">
                  This service is already linked to another sermon. Replacing
                  it will make this sermon the live sermon for that service.
                </p>
              )}
              {linkError && (
                <p className="mt-2 text-sm text-red-700">{linkError}</p>
              )}
            </section>
          )}

          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-4 flex items-center justify-between">
              <h3 className="text-base font-semibold text-slate-900">
                Segments
              </h3>
              {totalPages > 1 && (
                <div className="flex items-center gap-2 text-sm text-slate-600">
                  <button
                    type="button"
                    disabled={page === 0}
                    onClick={() => setPage((p) => Math.max(0, p - 1))}
                    className="rounded-lg border border-slate-300 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    ←
                  </button>
                  <span>
                    Page {page + 1} of {totalPages}
                  </span>
                  <button
                    type="button"
                    disabled={page >= totalPages - 1}
                    onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                    className="rounded-lg border border-slate-300 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    →
                  </button>
                </div>
              )}
            </div>

            {pagedSegments.length === 0 ? (
              <p className="text-sm text-slate-500">No segments.</p>
            ) : (
              <ul className="divide-y divide-slate-100">
                {pagedSegments.map((seg) => (
                  <li key={seg.segmentId} className="space-y-1 py-3">
                    <div className="flex flex-wrap items-center gap-2 text-xs">
                      <span className="font-mono text-slate-500">
                        {seg.segmentId}
                      </span>
                      <SegmentStatusBadge status={seg.status} />
                    </div>
                    <p className="text-sm text-slate-700">{seg.original}</p>
                    {sermon.reviewMode !== "pre_translated" && (
                      <p className="text-sm text-slate-500">
                        App: {seg.appTranslation || "—"}
                      </p>
                    )}
                    <p className="text-sm text-slate-900">
                      Reviewed: {seg.reviewedTranslation || "—"}
                    </p>
                    {seg.notes && (
                      <p className="text-xs italic text-slate-500">
                        Notes: {seg.notes}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </main>
    </>
  );
}

function SegmentStatusBadge({ status }: { status: SegmentStatus }) {
  const colors: Record<SegmentStatus, string> = {
    Draft: "bg-amber-100 text-amber-800",
    Reviewed: "bg-emerald-100 text-emerald-800",
    Skip: "bg-slate-200 text-slate-700",
  };
  return (
    <span
      className={
        "rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide " +
        colors[status]
      }
    >
      {status}
    </span>
  );
}
