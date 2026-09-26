"""Window 1 preflight — roster cross-check between reconciler
ticks (Cloud Logging) and Cloud Run instance counts (Cloud
Monitoring).

Backs Steps 1b + 1c of the runbook. **Read-only** — reads
Cloud Logging entries and Cloud Monitoring time-series. Never
writes anywhere.

Contract (fields the runbook decision table pins):

    {
      "kind": "roster_cross_check",
      "command": "roster_cross_check.py",
      "verified_at": "<ISO UTC>",
      "project": "<project>",
      "service_name": "worshiptranslate-backend",
      "tick_window_seconds": <int>,     # e.g. 300
      "metric_freshness_max_age_seconds": <float>,  # 180 by default
      "min_ticks_per_instance": 2,
      "min_tick_span_seconds": 30,
      "max_youngest_tick_age_seconds": 60,
      "tick_roster": [
        {"revision_name": "...", "instance_id": "...",
         "tick_timestamps": [...], "owned_rooms_last_tick": <int>,
         "status": "clean"|"stale_tick"|"non_zero_rooms"|"insufficient_ticks"},
        ...
      ],
      "cloud_run_metric": [
        {"revision_name": "...",
         "container_name": "worshiptranslate-backend",
         "active_plus_idle": <int>,
         "sample_timestamp": "<ISO>",
         "sample_age_seconds": <float>,
         "status": "fresh"|"stale"|"missing"},
        ...
      ],
      "roster_union": [
        {"revision_name": "...", "tick_count": <int>,
         "metric_count": <int|null>,
         "status": "match"|"mismatch"|"missing_metric"|"missing_ticks"},
        ...
      ],
      "all_instances_clean": <bool>,
      "all_revisions_match": <bool>,
      "metric_freshness_ok": <bool>,
      "elapsed_seconds": <float>,
      "rc": <int>,
      "reason": "<short human string>"  # nonzero rc only
    }

Distinct exit codes:
    0  verified — all_instances_clean AND all_revisions_match AND metric_freshness_ok
    1  usage
    2  target/allowlist refusal
    3  malformed upstream data (missing revision_name, wrong-typed instance_id,
       unexpected metric response shape)
    4  incomplete (a ticked instance is missing ticks / owned_rooms>0 /
       youngest tick too old)
    5  stale (metric sample older than the freshness threshold)
    6  permission / authentication failure
    7  timeout / deadline exceeded
    8  unresolved roster mismatch (per-revision tick-count != metric-count)
    9  upstream API failure
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    ALLOWED_REGIONS,
    Deadline,
    EnvMismatch,
    ExitCode,
    RegionRefused,
    TargetRefused,
    check_region,
    check_target,
    diag,
    die,
    emit,
    iso_utc_now,
    positive_float,
    positive_int,
)


DEFAULT_TICK_WINDOW_SEC = 300              # 5 min lookback for reconciler ticks
DEFAULT_METRIC_FRESHNESS_MAX_SEC = 180.0   # Google's 60 s sample + 120 s delay
DEFAULT_METRIC_LOOKBACK_SEC = 240          # cover 3 recent samples
DEFAULT_MIN_TICKS_PER_INSTANCE = 2
DEFAULT_MIN_TICK_SPAN_SEC = 30
DEFAULT_MAX_YOUNGEST_TICK_AGE_SEC = 60
DEFAULT_RPC_TIMEOUT_SEC = 15.0
DEFAULT_DEADLINE_SEC = 90.0
DEFAULT_SERVICE_NAME = "worshiptranslate-backend"
DEFAULT_REGION = "us-central1"


# --- Argparse --------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="roster_cross_check.py",
        description=(
            "Cross-check reconciler ticks (Cloud Logging) against "
            "run.googleapis.com/container/instance_count (Cloud "
            "Monitoring) for the worshiptranslate-backend Cloud "
            "Run service. Read-only."
        ),
    )
    p.add_argument("--project", required=True)
    p.add_argument("--database", required=True)
    p.add_argument("--service-name", default=DEFAULT_SERVICE_NAME)
    p.add_argument(
        "--region", default=DEFAULT_REGION,
        help=(
            "Cloud Run region to pin the query to. Cloud Run "
            "services are regional; a same-named service in a "
            "different region would otherwise contaminate the "
            "roster. Must be on the allowlist "
            f"({sorted(ALLOWED_REGIONS)!r})."
        ),
    )
    p.add_argument(
        "--tick-window-sec", type=positive_int,
        default=DEFAULT_TICK_WINDOW_SEC,
    )
    p.add_argument(
        "--metric-freshness-max-sec", type=positive_float,
        default=DEFAULT_METRIC_FRESHNESS_MAX_SEC,
    )
    p.add_argument(
        "--metric-lookback-sec", type=positive_int,
        default=DEFAULT_METRIC_LOOKBACK_SEC,
    )
    p.add_argument(
        "--rpc-timeout-sec", type=positive_float,
        default=DEFAULT_RPC_TIMEOUT_SEC,
    )
    p.add_argument(
        "--deadline-sec", type=positive_float,
        default=DEFAULT_DEADLINE_SEC,
    )
    return p


# --- Data model ------------------------------------------------------------


def _base_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "kind": "roster_cross_check",
        "command": "roster_cross_check.py",
        "verified_at": iso_utc_now(),
        "project": args.project,
        "service_name": args.service_name,
        "region": args.region,
        "tick_window_seconds": int(args.tick_window_sec),
        "metric_freshness_max_age_seconds": float(args.metric_freshness_max_sec),
        "min_ticks_per_instance": DEFAULT_MIN_TICKS_PER_INSTANCE,
        "min_tick_span_seconds": DEFAULT_MIN_TICK_SPAN_SEC,
        "max_youngest_tick_age_seconds": DEFAULT_MAX_YOUNGEST_TICK_AGE_SEC,
    }


# --- Cloud Logging pass ---------------------------------------------------


def _build_tick_filter(service_name: str, region: str, tick_window_sec: int) -> str:
    """Cloud Logging filter for reconciler_tick events. Extracted
    so tests can assert its shape (region pinning, service pin,
    event filter, timestamp bound) without exercising the full
    fetch."""
    start = datetime.now(timezone.utc) - timedelta(seconds=tick_window_sec)
    return (
        f'resource.type="cloud_run_revision"\n'
        f'resource.labels.service_name="{service_name}"\n'
        f'resource.labels.location="{region}"\n'
        f'jsonPayload.event="reconciler_tick"\n'
        f'timestamp >= "{start.strftime("%Y-%m-%dT%H:%M:%SZ")}"'
    )


def _extract_tick_entry(entry: Any) -> dict[str, Any]:
    """Convert one `LogEntry` proto into the runbook's tick shape:
      {revision_name, instance_id, timestamp_iso,
       timestamp_epoch, owned_rooms}

    Raises ValueError on any missing/wrong-typed field so the
    caller can classify as rc=3 MALFORMED. Extracted so
    round-4 tests can exercise proto-shape handling directly
    without spinning up the whole fetch path.

    `entry.json_payload` on the proto-plus wrapped LogEntry is a
    `MapComposite` (proto-plus's dict-like wrapper around the
    underlying google.protobuf.Struct). It is iterable, sized,
    and can be passed to `dict(...)` — which returns plain
    Python types (numbers become float64). `MessageToDict` does
    NOT work on the MapComposite (no `.DESCRIPTOR`), so we
    avoid it."""
    json_payload = getattr(entry, "json_payload", None)
    if json_payload is None or len(json_payload) == 0:
        raise ValueError(
            "reconciler_tick entry has missing/empty json_payload"
        )
    payload = dict(json_payload)

    resource = getattr(entry, "resource", None)
    resource_labels = dict(getattr(resource, "labels", {}) or {}) if resource else {}
    rev = resource_labels.get("revision_name")
    if not isinstance(rev, str) or not rev:
        raise ValueError(
            f"reconciler_tick entry has missing/wrong-typed "
            f"resource.labels.revision_name: {rev!r}"
        )
    inst = payload.get("instance_id")
    if not isinstance(inst, str) or not inst:
        raise ValueError(
            f"reconciler_tick entry has missing/wrong-typed "
            f"jsonPayload.instance_id: {inst!r}"
        )

    # google.protobuf.Struct only carries numbers as `number_value`
    # (float64), so `MessageToDict` returns floats even for integer
    # source values. Accept floats that are exactly integral; reject
    # bools (which are int subclass) and non-integral floats.
    owned_raw = payload.get("owned_rooms")
    if isinstance(owned_raw, bool):
        raise ValueError(
            f"reconciler_tick entry has bool owned_rooms: {owned_raw!r}"
        )
    if isinstance(owned_raw, int):
        owned = owned_raw
    elif isinstance(owned_raw, float) and owned_raw.is_integer():
        owned = int(owned_raw)
    else:
        raise ValueError(
            f"reconciler_tick entry has missing/wrong-typed "
            f"jsonPayload.owned_rooms: {owned_raw!r}"
        )

    ts = getattr(entry, "timestamp", None)
    if ts is None:
        raise ValueError("reconciler_tick entry has no timestamp")
    # `entry.timestamp` is a google.protobuf.Timestamp on the gapic
    # LogEntry, but the pre-round-4 high-level client wrapped it as
    # a Python datetime. Handle both so tests can pass either shape.
    if hasattr(ts, "astimezone"):
        ts_dt = ts.astimezone(timezone.utc)
    elif hasattr(ts, "ToDatetime"):
        ts_dt = ts.ToDatetime().replace(tzinfo=timezone.utc)
    elif hasattr(ts, "seconds"):
        ts_dt = datetime.fromtimestamp(
            ts.seconds + getattr(ts, "nanos", 0) / 1e9, timezone.utc,
        )
    else:
        raise ValueError(f"unsupported timestamp shape: {type(ts).__name__}")

    return {
        "revision_name": rev,
        "instance_id": inst,
        "timestamp_iso": ts_dt.isoformat(),
        "timestamp_epoch": ts_dt.timestamp(),
        "owned_rooms": owned,
    }


def fetch_tick_events(
    project: str, service_name: str, region: str, tick_window_sec: int,
    deadline: Deadline, rpc_timeout: float,
) -> list[dict[str, Any]]:
    """Return every `reconciler_tick` JSON event in the window,
    each as a dict:
      {revision_name, instance_id, timestamp_iso,
       timestamp_epoch, owned_rooms}

    Uses the generated `LoggingServiceV2Client.list_log_entries`
    (`google.cloud.logging_v2` v3.x); the older high-level
    `logging_v2.Client.list_entries(...)` on that same package
    version did not accept `timeout=` / `retry=` and raised a
    `TypeError` — a round-3 rehearsal caught that. R5 replaced
    the gapic pager's built-in page iteration (which freezes the
    initial `timeout=` across every subsequent page RPC) with a
    manual page-token loop: `deadline.rpc_timeout(rpc_timeout)`
    is recomputed BEFORE each page RPC, `deadline.expired()` is
    checked AFTER each page RPC (R6) and again between entries
    and before returning, and each pager construction consumes
    only its first (already-fetched) page.

    `region` pins `resource.labels.location` — Cloud Run services
    are regional; a same-named service in another region would
    otherwise appear in the results.

    Raises on permission / RPC failure / malformed schema."""
    from google.cloud.logging_v2.services.logging_service_v2 import (  # type: ignore
        LoggingServiceV2Client,
    )
    from google.cloud.logging_v2.types import ListLogEntriesRequest  # type: ignore
    from google.api_core import exceptions as gax  # type: ignore

    client = LoggingServiceV2Client()
    request = ListLogEntriesRequest(
        resource_names=[f"projects/{project}"],
        filter=_build_tick_filter(service_name, region, tick_window_sec),
        order_by="timestamp desc",
        page_size=1000,
    )
    # Manual page control: the gapic pager freezes the initial
    # `timeout=` across every subsequent next-page RPC, so a
    # per-page fetch could still spend the full `rpc_timeout`
    # even after the outer deadline shrinks below it. Round-4
    # relied on `deadline.expired()` between pages, which only
    # noticed the breach AFTER the offending RPC completed.
    # R5 re-computes `deadline.rpc_timeout(rpc_timeout)` before
    # every page and refuses to start a new page RPC once
    # the deadline has expired.
    out: list[dict[str, Any]] = []
    page_token = ""
    while True:
        if deadline.expired():
            raise TimeoutError(
                f"deadline expired before starting Cloud Logging page RPC "
                f"(read {len(out)} entries so far)"
            )
        per_page_timeout = deadline.rpc_timeout(rpc_timeout)
        request.page_token = page_token
        pager = client.list_log_entries(
            request=request,
            timeout=per_page_timeout,
            retry=None,
        )
        # Consume only the first (already-fetched) page. Iterating
        # the pager further would trigger next-page RPCs with the
        # frozen `per_page_timeout` from THIS iteration, defeating
        # the shrinking behavior we just installed.
        response = next(iter(pager.pages))
        # R6: post-RPC deadline gate. The gapic pager's per-page
        # timeout only bounds how long the RPC takes; a final
        # `next_page_token=""` page whose RPC overran the outer
        # budget would otherwise return successfully. Refuse here
        # so wall-clock breach is never silent.
        if deadline.expired():
            raise TimeoutError(
                f"deadline expired after Cloud Logging page RPC returned "
                f"(read {len(out)} entries so far)"
            )
        for entry in response.entries:
            # R6: entry-loop deadline gate. A very large page could
            # push us past the budget purely via extraction time.
            if deadline.expired():
                raise TimeoutError(
                    f"deadline expired while processing Cloud Logging "
                    f"entries (read {len(out)} entries so far)"
                )
            out.append(_extract_tick_entry(entry))
        page_token = response.next_page_token
        if not page_token:
            break
    # R6: pre-return deadline gate. Catches the case where the
    # final entry's extraction (the last iteration's body) pushed
    # us past the budget — the entry-loop check above only fires
    # BEFORE each extract, so a slow last extract slips through.
    if deadline.expired():
        raise TimeoutError(
            f"deadline expired before returning Cloud Logging results "
            f"({len(out)} entries buffered)"
        )
    return out


def build_tick_roster(
    events: list[dict[str, Any]],
    *,
    min_ticks: int, min_span: int, max_age: int, now_epoch: float,
) -> list[dict[str, Any]]:
    """Group events by (revision, instance) and classify each per
    the runbook rules. Newest tick first per group."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for ev in events:
        key = (ev["revision_name"], ev["instance_id"])
        grouped.setdefault(key, []).append(ev)
    roster: list[dict[str, Any]] = []
    for (rev, inst), items in grouped.items():
        # Newest first (fetch is DESCENDING but sort defensively).
        items.sort(key=lambda e: e["timestamp_epoch"], reverse=True)
        timestamps = [e["timestamp_epoch"] for e in items]
        iso_stamps = [e["timestamp_iso"] for e in items]
        youngest_age = now_epoch - timestamps[0]
        span = (timestamps[0] - timestamps[1]) if len(timestamps) >= 2 else 0.0
        last_owned = items[0]["owned_rooms"]
        status = "clean"
        if len(items) < min_ticks:
            status = "insufficient_ticks"
        elif span < min_span:
            status = "insufficient_ticks"
        elif youngest_age > max_age:
            status = "stale_tick"
        elif last_owned != 0:
            status = "non_zero_rooms"
        # A second-tick owned_rooms > 0 is also disqualifying —
        # the runbook requires two consecutive zero-room ticks.
        elif len(items) >= 2 and items[1]["owned_rooms"] != 0:
            status = "non_zero_rooms"
        roster.append({
            "revision_name": rev,
            "instance_id": inst,
            "tick_timestamps": iso_stamps[: max(2, min_ticks)],
            "youngest_tick_age_seconds": round(youngest_age, 3),
            "span_seconds": round(span, 3),
            "owned_rooms_last_tick": last_owned,
            "status": status,
        })
    return roster


# --- Cloud Monitoring pass ------------------------------------------------


def _build_metric_filter(service_name: str, region: str) -> str:
    """Cloud Monitoring filter string. Extracted so tests can
    verify the required labels are present and the removed
    `container_name` label is absent (round-2 blocker: the
    metric documents only `state`, so a container_name filter
    returned zero series in production)."""
    return (
        f'metric.type="run.googleapis.com/container/instance_count" '
        f'AND resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{service_name}" '
        f'AND resource.labels.location="{region}" '
        f'AND (metric.labels.state="active" OR metric.labels.state="idle")'
    )


def _aggregate_metric_series(
    pages, *, now_epoch: float, deadline: Deadline,
) -> list[dict[str, Any]]:
    """Bucket every (rev, state, timestamp) tuple, then pick the
    newest bucket where BOTH states report a value per revision.

    Extracted from `fetch_metric_samples` so tests can exercise
    the alignment logic directly with fake TimeSeries fixtures —
    round-2 tests monkey-patched the whole function, leaving
    this production-critical path unexercised.

    Raises TimeoutError if the deadline fires while iterating,
    ValueError on malformed series (missing/wrong-typed
    revision_name, unexpected metric.labels.state, or a
    revision that reports only one of the two states)."""
    by_rev: dict[str, dict[str, dict[int, int]]] = {}
    for series in pages:
        if deadline.expired():
            raise TimeoutError(
                "deadline expired while reading Monitoring time-series"
            )
        # `series.resource.labels` and `series.metric.labels` on a
        # real `monitoring_v3.TimeSeries` proto are MapField /
        # MapComposite objects, NOT Python dicts. Round-4 code
        # gated on `isinstance(labels, dict)` and treated proto maps
        # as empty — so a real prod series would raise
        # "missing revision_name" regardless of the actual label
        # value. Normalize with `dict(...)` so `.get()` behaves
        # the same for proto maps and for the SimpleNamespace
        # fixture dicts existing tests use.
        resource_raw = getattr(getattr(series, "resource", None), "labels", None)
        resource_labels = dict(resource_raw) if resource_raw else {}
        rev = resource_labels.get("revision_name")
        if not isinstance(rev, str) or not rev:
            raise ValueError(
                f"time-series has missing/wrong-typed "
                f"resource.labels.revision_name: {rev!r}"
            )
        metric_raw = getattr(getattr(series, "metric", None), "labels", None)
        metric_labels = dict(metric_raw) if metric_raw else {}
        state = metric_labels.get("state")
        if state not in ("active", "idle"):
            raise ValueError(
                f"time-series has unexpected metric.labels.state={state!r}"
            )
        by_state = by_rev.setdefault(rev, {"active": {}, "idle": {}})
        for point in list(getattr(series, "points", []) or []):
            _ts_iso, ts_epoch = _extract_point_timestamp(point)
            # Bucket by 1 s so tiny clock skew between the two
            # state series still aligns.
            bucket = int(round(ts_epoch))
            by_state[state][bucket] = int(_extract_point_value(point))

    out: list[dict[str, Any]] = []
    for rev, states in by_rev.items():
        active = states["active"]
        idle = states["idle"]
        # Newest bucket at which BOTH state series report a value.
        common_buckets = sorted(
            set(active.keys()) & set(idle.keys()), reverse=True,
        )
        if not common_buckets:
            # A revision that reports only one of the two states
            # is malformed — every Cloud Run instance is either
            # active OR idle, so a healthy revision reports both
            # (potentially with 0 in one of them). Absent one
            # side, we cannot compute a trustworthy total.
            raise ValueError(
                f"revision {rev!r} has instance_count in only one "
                f"state (active_buckets={len(active)}, "
                f"idle_buckets={len(idle)}) — cannot align"
            )
        aligned_bucket = common_buckets[0]
        aligned_ts = datetime.fromtimestamp(aligned_bucket, timezone.utc)
        total = active[aligned_bucket] + idle[aligned_bucket]
        out.append({
            "revision_name": rev,
            "active_value": active[aligned_bucket],
            "idle_value": idle[aligned_bucket],
            "active_plus_idle": total,
            "aligned_sample_timestamp_iso": aligned_ts.isoformat(),
            "aligned_sample_timestamp_epoch": float(aligned_bucket),
            "sample_age_seconds": round(now_epoch - aligned_bucket, 3),
        })
    return out


def fetch_metric_samples(
    project: str, service_name: str, region: str,
    lookback_sec: int, deadline: Deadline, rpc_timeout: float,
) -> list[dict[str, Any]]:
    """Return per-revision Cloud Run instance count samples in
    the window. Each entry:
      {revision_name, active_value, idle_value, active_plus_idle,
       aligned_sample_timestamp_iso, aligned_sample_timestamp_epoch,
       sample_age_seconds}

    Thin composition of `_build_metric_filter` and
    `_aggregate_metric_series` around the Cloud Monitoring
    client. See those helpers for the reasoning behind the
    filter shape and alignment rule.

    Raises on permission / malformed schema / RPC failure."""
    from google.cloud import monitoring_v3  # type: ignore
    from google.api_core import exceptions as gax  # type: ignore

    client = monitoring_v3.MetricServiceClient()
    now = datetime.now(timezone.utc)
    interval = monitoring_v3.TimeInterval({
        "start_time": now - timedelta(seconds=lookback_sec),
        "end_time": now,
    })
    request = monitoring_v3.ListTimeSeriesRequest({
        "name": f"projects/{project}",
        "filter": _build_metric_filter(service_name, region),
        "interval": interval,
        "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
    })
    # Manual page control: same rationale as `fetch_tick_events`
    # in R5. The gapic pager freezes the initial `timeout=` across
    # every subsequent next-page RPC, and the outer deadline check
    # inside `_aggregate_metric_series` only fires AFTER a page
    # arrives. Re-compute `deadline.rpc_timeout(rpc_timeout)`
    # before every page RPC; refuse to start a new page RPC once
    # the deadline has expired.
    series_pages: list = []
    page_token = ""
    while True:
        if deadline.expired():
            raise TimeoutError(
                f"deadline expired before starting Cloud Monitoring page RPC "
                f"(read {len(series_pages)} series so far)"
            )
        per_page_timeout = deadline.rpc_timeout(rpc_timeout)
        request.page_token = page_token
        pager = client.list_time_series(
            request=request,
            timeout=per_page_timeout,
            retry=None,
        )
        response = next(iter(pager.pages))
        # R6: post-RPC deadline gate. Same rationale as the tick
        # side — a `next_page_token=""` final page whose RPC
        # overran the outer budget would otherwise slip through
        # to a "successful" aggregation.
        if deadline.expired():
            raise TimeoutError(
                f"deadline expired after Cloud Monitoring page RPC returned "
                f"(read {len(series_pages)} series so far)"
            )
        series_pages.extend(response.time_series)
        page_token = response.next_page_token
        if not page_token:
            break
    # R6: pre-aggregation deadline gate. `_aggregate_metric_series`
    # has its own per-iteration check, but there is no gate between
    # the last page RPC and the start of aggregation. Refuse here
    # so wall-clock breach cannot hide behind that seam.
    if deadline.expired():
        raise TimeoutError(
            f"deadline expired before aggregating Cloud Monitoring series "
            f"({len(series_pages)} series buffered)"
        )
    aggregated = _aggregate_metric_series(
        series_pages, now_epoch=now.timestamp(), deadline=deadline,
    )
    # R7: post-aggregation deadline gate. The per-iteration check
    # inside `_aggregate_metric_series` fires at the TOP of each
    # series iteration, so processing the final series can push us
    # past budget without any inner check firing again. Refuse
    # here so a wall-clock breach during the last series's
    # processing does not slip through as a "successful" return.
    if deadline.expired():
        raise TimeoutError(
            f"deadline expired before returning Cloud Monitoring results "
            f"({len(aggregated)} revision entries buffered)"
        )
    return aggregated


def _extract_point_timestamp(point: Any) -> tuple[str, float]:
    interval = getattr(point, "interval", None)
    end = getattr(interval, "end_time", None) if interval is not None else None
    if end is None:
        raise ValueError("time-series point missing interval.end_time")
    # google.protobuf Timestamp: has .seconds + .nanos, and
    # ToDatetime() in newer SDK versions. Handle both.
    if hasattr(end, "isoformat"):
        return end.astimezone(timezone.utc).isoformat(), end.timestamp()
    if hasattr(end, "ToDatetime"):
        dt = end.ToDatetime().replace(tzinfo=timezone.utc)
        return dt.isoformat(), dt.timestamp()
    if hasattr(end, "seconds"):
        dt = datetime.fromtimestamp(
            end.seconds + getattr(end, "nanos", 0) / 1e9, timezone.utc,
        )
        return dt.isoformat(), dt.timestamp()
    raise ValueError(f"unsupported timestamp shape: {type(end).__name__}")


def _extract_point_value(point: Any) -> int:
    """Return the integer value of a TimeSeries `Point`.

    `run.googleapis.com/container/instance_count` reports values
    via `TypedValue.int64_value`. R5 accepted `double_value` as a
    fallback and short-circuited on `int64_value=0` (falsy) so
    `TypedValue(bool_value=True)` and `TypedValue(double_value=1.5)`
    silently coerced to `int(0)` / `int(1)`. R6 rewrites to
    inspect the real proto oneof and accept ONLY the
    `int64_value` branch (including zero); every other set
    branch — double, bool, string, distribution — and every
    unset value raise ValueError.

    Proto-plus wraps the raw protobuf message; the raw one is
    available via `type(value).pb(value)`. `WhichOneof('value')`
    returns the name of the set branch, or None if none is set."""
    value = getattr(point, "value", None)
    if value is None:
        raise ValueError("time-series point missing value")
    if hasattr(type(value), "pb"):
        pb = type(value).pb(value)
    else:
        pb = value
    which = pb.WhichOneof("value") if hasattr(pb, "WhichOneof") else None
    if which is None:
        raise ValueError(
            "time-series point TypedValue has no oneof branch set — "
            "expected int64_value"
        )
    if which != "int64_value":
        raise ValueError(
            f"time-series point TypedValue oneof branch is {which!r} — "
            f"only int64_value is accepted for instance_count"
        )
    return int(value.int64_value)


# --- Cross-check + status roll-up -----------------------------------------


def cross_check(
    tick_roster: list[dict[str, Any]],
    metric_samples: list[dict[str, Any]],
    *, freshness_max_age: float,
) -> tuple[list[dict[str, Any]], bool, bool, bool]:
    """Return (roster_union, all_instances_clean, all_revisions_match,
    metric_freshness_ok)."""
    ticks_by_rev: dict[str, set[str]] = {}
    for entry in tick_roster:
        ticks_by_rev.setdefault(entry["revision_name"], set()).add(
            entry["instance_id"]
        )
    metric_by_rev: dict[str, dict[str, Any]] = {
        m["revision_name"]: m for m in metric_samples
    }

    all_revs = set(ticks_by_rev) | set(metric_by_rev)
    union: list[dict[str, Any]] = []
    all_match = True
    freshness_ok = True
    for rev in sorted(all_revs):
        tick_count = len(ticks_by_rev.get(rev, set()))
        metric_entry = metric_by_rev.get(rev)
        metric_count = (
            metric_entry["active_plus_idle"] if metric_entry else None
        )
        status = "match"
        if metric_entry is None:
            status = "missing_metric"
            all_match = False
        elif metric_entry["sample_age_seconds"] > freshness_max_age:
            status = "match" if tick_count == metric_count else "mismatch"
            # A stale metric is also a freshness violation regardless
            # of whether the counts happen to agree.
            freshness_ok = False
            if status == "mismatch":
                all_match = False
        elif tick_count != metric_count:
            status = "mismatch"
            all_match = False
        if tick_count == 0 and metric_count and metric_count > 0:
            status = "missing_ticks"
            all_match = False
        union.append({
            "revision_name": rev,
            "tick_count": tick_count,
            "metric_count": metric_count,
            "metric_sample_age_seconds": (
                metric_entry["sample_age_seconds"] if metric_entry else None
            ),
            "status": status,
        })

    # Empty telemetry is UNRESOLVED, not clean. Both a truly
    # scaled-to-zero service AND a broken query that returns no
    # series look the same from here. The runbook's "silence is
    # unresolved" rule requires the operator to distinguish
    # those cases out-of-band (e.g., by asking Cloud Run
    # directly whether the service is currently scaled to zero)
    # before declaring the window ready — the helper cannot
    # decide it on its own.
    if not tick_roster and not metric_samples:
        # Report the union as a single synthetic entry so the
        # rc classifier surfaces it as UNRESOLVED_MISMATCH.
        union.append({
            "revision_name": "<empty>",
            "tick_count": 0,
            "metric_count": None,
            "metric_sample_age_seconds": None,
            "status": "no_evidence",
        })
        return union, False, False, freshness_ok

    all_clean = all(entry["status"] == "clean" for entry in tick_roster) \
        and bool(tick_roster) is True
    return union, all_clean, all_match, freshness_ok


# --- Entry ----------------------------------------------------------------


def _run(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    payload = _base_payload(args)

    try:
        check_target(args.project, args.database)
        check_region(args.region)
    except (TargetRefused, EnvMismatch, RegionRefused) as exc:
        payload.update({
            "tick_roster": [], "cloud_run_metric": [], "roster_union": [],
            "all_instances_clean": False, "all_revisions_match": False,
            "metric_freshness_ok": False,
            "elapsed_seconds": 0.0,
            "rc": int(ExitCode.ALLOWLIST_REFUSAL),
            "reason": str(exc),
        })
        diag(f"STOP: {exc}")
        die(ExitCode.ALLOWLIST_REFUSAL, payload)

    try:
        from google.api_core import exceptions as gax  # type: ignore
        from google.auth import exceptions as gauth  # type: ignore
    except Exception as exc:  # pragma: no cover
        payload.update({
            "tick_roster": [], "cloud_run_metric": [], "roster_union": [],
            "all_instances_clean": False, "all_revisions_match": False,
            "metric_freshness_ok": False,
            "elapsed_seconds": 0.0,
            "rc": int(ExitCode.UPSTREAM_API),
            "reason": f"google-api-core import failed: {exc!r}",
        })
        diag(f"STOP: {payload['reason']}")
        die(ExitCode.UPSTREAM_API, payload)

    deadline = Deadline(float(args.deadline_sec))
    now_epoch = time.time()

    # --- Cloud Logging pass -------------------------------------------
    try:
        events = fetch_tick_events(
            args.project, args.service_name, args.region,
            args.tick_window_sec, deadline, args.rpc_timeout_sec,
        )
    except gauth.DefaultCredentialsError as exc:
        # ADC not configured — authentication-family failure. The
        # generic Exception handler below would misclassify this as
        # rc=9 upstream API failure; the runbook branches on the
        # distinction so we catch it explicitly.
        _die_upstream(payload, deadline, ExitCode.PERMISSION,
                      f"Application Default Credentials not found: {exc}")
    except (gax.PermissionDenied, gax.Unauthenticated) as exc:
        _die_upstream(payload, deadline, ExitCode.PERMISSION,
                      f"Cloud Logging permission denied: {exc.message}")
    except gax.DeadlineExceeded as exc:
        _die_upstream(payload, deadline, ExitCode.TIMEOUT,
                      f"Cloud Logging deadline exceeded: {exc.message}")
    except TimeoutError as exc:
        _die_upstream(payload, deadline, ExitCode.TIMEOUT, str(exc))
    except ValueError as exc:
        _die_upstream(payload, deadline, ExitCode.MALFORMED,
                      f"Cloud Logging entry malformed: {exc}")
    except Exception as exc:
        _die_upstream(payload, deadline, ExitCode.UPSTREAM_API,
                      f"Cloud Logging failed: {type(exc).__name__}: {exc}")

    tick_roster = build_tick_roster(
        events,
        min_ticks=DEFAULT_MIN_TICKS_PER_INSTANCE,
        min_span=DEFAULT_MIN_TICK_SPAN_SEC,
        max_age=DEFAULT_MAX_YOUNGEST_TICK_AGE_SEC,
        now_epoch=now_epoch,
    )

    # --- Cloud Monitoring pass ----------------------------------------
    try:
        metric_samples = fetch_metric_samples(
            args.project, args.service_name, args.region,
            args.metric_lookback_sec, deadline, args.rpc_timeout_sec,
        )
    except gauth.DefaultCredentialsError as exc:
        _die_upstream(payload, deadline, ExitCode.PERMISSION,
                      f"Application Default Credentials not found: {exc}")
    except (gax.PermissionDenied, gax.Unauthenticated) as exc:
        _die_upstream(payload, deadline, ExitCode.PERMISSION,
                      f"Cloud Monitoring permission denied: {exc.message}")
    except gax.DeadlineExceeded as exc:
        _die_upstream(payload, deadline, ExitCode.TIMEOUT,
                      f"Cloud Monitoring deadline exceeded: {exc.message}")
    except TimeoutError as exc:
        _die_upstream(payload, deadline, ExitCode.TIMEOUT, str(exc))
    except ValueError as exc:
        _die_upstream(payload, deadline, ExitCode.MALFORMED,
                      f"Cloud Monitoring response malformed: {exc}")
    except Exception as exc:
        _die_upstream(payload, deadline, ExitCode.UPSTREAM_API,
                      f"Cloud Monitoring failed: {type(exc).__name__}: {exc}")

    union, all_clean, all_match, freshness_ok = cross_check(
        tick_roster, metric_samples,
        freshness_max_age=float(args.metric_freshness_max_sec),
    )

    payload.update({
        "tick_roster": tick_roster,
        "cloud_run_metric": metric_samples,
        "roster_union": union,
        "all_instances_clean": all_clean,
        "all_revisions_match": all_match,
        "metric_freshness_ok": freshness_ok,
        "elapsed_seconds": round(deadline.elapsed(), 3),
    })

    # Priority order for classification:
    #   MALFORMED > TIMEOUT > STALE > UNRESOLVED_MISMATCH > INCOMPLETE > OK
    if not freshness_ok:
        payload["rc"] = int(ExitCode.STALE)
        payload["reason"] = (
            "Cloud Monitoring sample older than "
            f"{args.metric_freshness_max_sec:.0f}s for at least one revision"
        )
        diag(f"STOP: {payload['reason']}")
        die(ExitCode.STALE, payload)
    if not all_match:
        payload["rc"] = int(ExitCode.UNRESOLVED_MISMATCH)
        payload["reason"] = (
            "per-revision tick count does not equal instance-count metric "
            "for at least one revision"
        )
        diag(f"STOP: {payload['reason']}")
        die(ExitCode.UNRESOLVED_MISMATCH, payload)
    if not all_clean:
        payload["rc"] = int(ExitCode.INCOMPLETE)
        payload["reason"] = (
            "at least one instance is not clean "
            "(insufficient ticks / stale tick / non-zero owned_rooms)"
        )
        diag(f"STOP: {payload['reason']}")
        die(ExitCode.INCOMPLETE, payload)

    payload["rc"] = int(ExitCode.OK)
    emit(payload)
    return int(ExitCode.OK)


def _die_upstream(
    payload: dict[str, Any], deadline: Deadline,
    rc: ExitCode, reason: str,
) -> None:
    payload.update({
        "tick_roster": [], "cloud_run_metric": [], "roster_union": [],
        "all_instances_clean": False, "all_revisions_match": False,
        "metric_freshness_ok": False,
        "elapsed_seconds": round(deadline.elapsed(), 3),
        "rc": int(rc), "reason": reason,
    })
    diag(f"STOP: {reason}")
    die(rc, payload)


def main() -> None:  # pragma: no cover
    sys.exit(_run(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
