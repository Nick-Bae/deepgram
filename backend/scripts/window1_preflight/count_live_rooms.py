"""Window 1 preflight — exhaustive live-room count via Firestore
collection-group query on `organizations/*/rooms` where
`status == "live"`.

Backs Step 1a of the runbook. **Read-only** — never writes to
Firestore, never opens a transaction, never invokes the admin
gate document. The reconciler uses the same admin store from
`backend/app/services/multichurch_store.py`; this helper reads
that authoritative store instead of a `gcloud` command that
does not exist.

Contract:
  - Emits exactly one structured JSON on stdout (see `common.emit`).
  - Human diagnostics go to stderr.
  - Enforces the (project, database) allowlist AND the
    FIRESTORE_EMULATOR_HOST env-mismatch check BEFORE any
    Firestore RPC. See `common.check_target`.
  - Per-RPC timeout on every page (`--rpc-timeout-sec`,
    default 10 s) AND overall monotonic deadline
    (`--deadline-sec`, default 30 s). Either firing produces
    rc=7 (TIMEOUT).
  - `complete=true` in the output ONLY after the Firestore
    iterator is fully exhausted. A short-circuit (permission
    error, timeout, non-2xx) produces `complete=false` AND a
    nonzero rc.
  - Distinct exit codes:
        0  verified (complete=true, count in payload)
        1  usage / argparse error (bare exit; no JSON body)
        2  target/allowlist refusal (JSON body carries reason)
        4  incomplete — iterator not fully drained
        6  permission / authentication failure
        7  timeout / deadline exceeded
        9  upstream Firestore API failure

The output schema is stable — the runbook decision table pins
these field names:

    {
      "kind": "live_room_count",
      "command": "count_live_rooms.py",
      "verified_at": "<ISO UTC>",
      "project": "<project>",
      "database": "<database>",
      "collection_group": "rooms",
      "filter": "status == 'live'",
      "count": <int|null>,
      "complete": <true|false>,
      "documents_scanned": <int>,
      "elapsed_seconds": <float>,
      "rc": <int>,
      "reason": "<short human string>"    # nonzero rc only
    }
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

# Allow `python backend/scripts/window1_preflight/count_live_rooms.py …`
# to work without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    Deadline,
    EnvMismatch,
    ExitCode,
    TargetRefused,
    check_target,
    diag,
    die,
    emit,
    iso_utc_now,
    positive_float,
)


DEFAULT_RPC_TIMEOUT_SEC = 10.0
DEFAULT_DEADLINE_SEC = 30.0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="count_live_rooms.py",
        description=(
            "Read-only Firestore collection-group count of "
            "organizations/*/rooms where status=='live'. Refuses "
            "any project/database not on the allowlist."
        ),
    )
    p.add_argument(
        "--project", required=True,
        help="Google Cloud project ID. Must be on the allowlist.",
    )
    p.add_argument(
        "--database", required=True,
        help="Firestore database ID. Must be on the allowlist.",
    )
    p.add_argument(
        "--rpc-timeout-sec", type=positive_float, default=DEFAULT_RPC_TIMEOUT_SEC,
        help=(
            "Per-page RPC timeout (default 10 s). Bounded by the "
            "overall deadline — whichever is shorter applies. "
            "Must be a positive finite float."
        ),
    )
    p.add_argument(
        "--deadline-sec", type=positive_float, default=DEFAULT_DEADLINE_SEC,
        help=(
            "Overall wall-clock budget across all paginated RPCs "
            "(default 30 s). Firing produces rc=7. Must be a "
            "positive finite float."
        ),
    )
    return p


def _base_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "kind": "live_room_count",
        "command": "count_live_rooms.py",
        "verified_at": iso_utc_now(),
        "project": args.project,
        "database": args.database,
        "collection_group": "rooms",
        "filter": "status == 'live'",
    }


def _run(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    payload = _base_payload(args)

    # 1) Allowlist + env-mismatch check BEFORE any Firestore import.
    try:
        check_target(args.project, args.database)
    except (TargetRefused, EnvMismatch) as exc:
        payload.update({
            "count": None, "complete": False, "documents_scanned": 0,
            "elapsed_seconds": 0.0, "rc": int(ExitCode.ALLOWLIST_REFUSAL),
            "reason": str(exc),
        })
        diag(f"STOP: {exc}")
        die(ExitCode.ALLOWLIST_REFUSAL, payload)

    # 2) Import Firestore lazily so allowlist refusals do not fail
    #    on a missing SDK install (the operator's laptop may not
    #    have google-cloud-firestore).
    try:
        from google.cloud import firestore  # type: ignore
        from google.api_core import exceptions as gax  # type: ignore
    except Exception as exc:  # pragma: no cover
        payload.update({
            "count": None, "complete": False, "documents_scanned": 0,
            "elapsed_seconds": 0.0, "rc": int(ExitCode.UPSTREAM_API),
            "reason": f"google-cloud-firestore import failed: {exc!r}",
        })
        diag(f"STOP: {payload['reason']}")
        die(ExitCode.UPSTREAM_API, payload)

    deadline = Deadline(float(args.deadline_sec))
    client = firestore.Client(project=args.project, database=args.database)

    # Collection-group query — iterates every `rooms` subcollection
    # across every `organizations/*` document. Same shape the
    # reconciler uses.
    query = client.collection_group("rooms").where(
        filter=firestore.FieldFilter("status", "==", "live"),
    )

    count = 0
    complete = False
    try:
        # `.stream()` yields DocumentSnapshots page-by-page under the
        # hood. We loop with an explicit iterator so we can bound
        # each page's RPC AND consult the overall deadline between
        # documents. `documents_scanned` in the output is the honest
        # per-document count — earlier drafts named the field
        # `pages_read` while incrementing it every 100 documents,
        # which was neither pages nor read from the SDK's page
        # boundaries; renamed and fixed to just equal `count` on
        # success.
        iterator = query.stream(
            timeout=deadline.rpc_timeout(float(args.rpc_timeout_sec)),
            retry=None,
        )
        for _snap in iterator:
            count += 1
            if count % 100 == 0 and deadline.expired():
                raise TimeoutError(
                    f"overall deadline {args.deadline_sec:.1f}s exceeded "
                    f"after reading {count} documents"
                )
        # If the loop completed without raising, the iterator is
        # exhausted — `complete=true` is now safe.
        complete = True
    except (gax.PermissionDenied, gax.Unauthenticated) as exc:
        payload.update({
            "count": None, "complete": False, "documents_scanned": count,
            "elapsed_seconds": round(deadline.elapsed(), 3),
            "rc": int(ExitCode.PERMISSION),
            "reason": f"Firestore permission denied: {exc.message}",
        })
        diag(f"STOP: {payload['reason']}")
        die(ExitCode.PERMISSION, payload)
    except gax.DeadlineExceeded as exc:
        payload.update({
            "count": None, "complete": False, "documents_scanned": count,
            "elapsed_seconds": round(deadline.elapsed(), 3),
            "rc": int(ExitCode.TIMEOUT),
            "reason": f"per-RPC deadline exceeded: {exc.message}",
        })
        diag(f"STOP: {payload['reason']}")
        die(ExitCode.TIMEOUT, payload)
    except TimeoutError as exc:
        payload.update({
            "count": None, "complete": False, "documents_scanned": count,
            "elapsed_seconds": round(deadline.elapsed(), 3),
            "rc": int(ExitCode.TIMEOUT),
            "reason": str(exc),
        })
        diag(f"STOP: {exc}")
        die(ExitCode.TIMEOUT, payload)
    except Exception as exc:
        # Any other error — includes network flakes, unexpected
        # response shape, SDK-internal errors. Do NOT return
        # `complete=true` and do NOT return a count.
        payload.update({
            "count": None, "complete": False, "documents_scanned": count,
            "elapsed_seconds": round(deadline.elapsed(), 3),
            "rc": int(ExitCode.UPSTREAM_API),
            "reason": f"Firestore query failed: {type(exc).__name__}: {exc}",
        })
        diag(f"STOP: {payload['reason']}")
        die(ExitCode.UPSTREAM_API, payload)

    # Success path — count is authoritative because iterator was
    # fully drained.
    payload.update({
        "count": int(count),
        "complete": True,
        "documents_scanned": int(count),
        "elapsed_seconds": round(deadline.elapsed(), 3),
        "rc": int(ExitCode.OK),
    })
    emit(payload)
    return int(ExitCode.OK)


def main() -> None:  # pragma: no cover — tested via `_run`
    sys.exit(_run(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
