/**
 * Format an ISO-8601 timestamp as "5 min ago" / "2 hr ago" / "3 days ago".
 *
 * Intended for "when was this last updated" displays where an approximate
 * age is more scannable than an absolute timestamp. Pair with an absolute
 * timestamp in a tooltip or secondary line for precision.
 *
 * Returns "" if the input is missing or unparseable — callers should
 * decide how to render the empty case.
 */
export function formatRelativeTime(iso: string | null | undefined, nowMs?: number): string {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const now = nowMs ?? Date.now();
  const diffSec = Math.round((now - then) / 1000);

  if (diffSec < 0) {
    // Future timestamp (clock skew etc.) — treat as "just now" so it doesn't
    // read as "in the future" which would be confusing.
    return "just now";
  }
  if (diffSec < 45) return "just now";
  if (diffSec < 90) return "1 min ago";
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 45) return `${diffMin} min ago`;
  if (diffMin < 90) return "1 hr ago";
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr} hr ago`;
  const diffDay = Math.round(diffHr / 24);
  if (diffDay < 30) return `${diffDay} day${diffDay === 1 ? "" : "s"} ago`;
  const diffMonth = Math.round(diffDay / 30);
  if (diffMonth < 12) return `${diffMonth} mo ago`;
  const diffYear = Math.round(diffMonth / 12);
  return `${diffYear} yr${diffYear === 1 ? "" : "s"} ago`;
}

/**
 * Format an ISO-8601 timestamp as a compact local absolute display, e.g.
 * "Aug 15, 10:07 AM". Meant for tooltips or secondary lines that need
 * exact time without being overly verbose.
 *
 * Returns "" for missing/unparseable input.
 */
export function formatAbsoluteTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}
