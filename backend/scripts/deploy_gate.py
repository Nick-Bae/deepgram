"""Operator CLI for the production `system/deploy_gate` document.

Reference: PR #31 §4b (rollout proposal), PR #33 (reader contract on
this same document). This is the WRITER side that the reviewer
identified as a follow-on to PR #33 (task #133).

Behaviour contract (from the reviewer's task #133 spec):

- Document path is FIXED at `system/deploy_gate`. There is no
  `--path` flag; an arbitrary path is not accepted.
- Actions: `status` (read-only), `block`, `unblock`.
- `block` / `unblock` require `--apply`. Without it the script
  performs a DRY RUN — reads the current state and prints what
  would change without writing anything. Confirmation is required
  for a real write (`--apply` alone is not enough; the operator
  also passes `--confirm 'I understand this affects production'`,
  matching set_super_admin.py's pattern).
- `--project` and `--database` are REQUIRED and must be on the
  allowlist below. The allowlist prevents an accidental
  emulator/wrong-project write from happening because a stray env
  var was set.
- `--expected-revision <int>` is REQUIRED for `block` / `unblock`.
  The transaction only writes if the document's current revision
  equals this value (or the document is absent AND the caller
  passed `--expected-revision 0`).
- State check and write happen in ONE Firestore transaction.
- `revision` increments ONLY on a real state transition (blocked
  changed). Repeated `block` on an already-blocked gate — or
  repeated `unblock` on an already-unblocked gate — is a no-op
  that does NOT increment `revision`.
- Malformed existing fields (missing `blocked`, wrong type on
  `blocked` or `revision`) fail closed — the script refuses to
  proceed without an explicit `--force-repair` (which is NOT
  provided here; a malformed doc requires manual investigation).
- Unknown CLI arguments cause argparse to exit with a usage
  message. No silent-accept.
- Stale expected revision → rejected inside the transaction; no
  write happens.
- FIRESTORE_EMULATOR_HOST must match the target: the production
  target requires the env var to be UNSET (otherwise the Google
  client silently redirects to the emulator even when the caller
  passes a production project); the emulator target requires the
  env var to be SET (otherwise the client talks to real
  Firestore). Fail-closed on either mismatch — no `--force-env`
  escape hatch.
- Firestore reads carry an explicit per-RPC timeout and
  `retry=None` so a single hung RPC cannot stall the operator;
  the transactional wrapper still retries on Firestore conflict
  aborts.
- Output NEVER prints credentials or unrelated document contents.
  Only fields the operator needs to see (blocked, revision,
  reason, blocked_by, blocked_at) and always as literal strings.

This script has no `import` from production application code —
runs standalone against `google.cloud.firestore` so it can be
invoked from an operator's laptop without importing the FastAPI
app or the store singleton.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional


# --- Configuration (allowlists, path, contract constants) ------------------

# The one path this script touches. Not configurable — a `--path`
# knob would defeat the whole point of a purpose-built gate writer.
DEPLOY_GATE_COLLECTION = "system"
DEPLOY_GATE_DOCUMENT = "deploy_gate"

# Only these (project, database) tuples are accepted. Prevents an
# accidental write against a wrong project when a stray env var is
# set. Emulator entries live here too so tests can exercise the
# same allowlist code path.
ALLOWED_TARGETS: set[tuple[str, str]] = {
    ("sturdy-dogfish-472313-k6", "worship-translation"),  # production
    ("cleanup-track1-emulator", "(default)"),             # emulator (unit tests)
}

# Confirmation string the operator MUST type for `--apply` to be honored.
CONFIRMATION_TOKEN = "I understand this affects production"

# Sentinel meaning "the doc must be absent" for `--expected-revision 0`.
_EXPECTED_ABSENT = 0

# Per-RPC timeout applied to every Firestore document read the writer
# performs. Bounds a single hung RPC; the transactional wrapper's own
# conflict retries are untouched.
_RPC_TIMEOUT_SEC = 10.0

# Targets that MUST NOT have FIRESTORE_EMULATOR_HOST set (real Firestore).
_PRODUCTION_TARGETS: set[tuple[str, str]] = {
    ("sturdy-dogfish-472313-k6", "worship-translation"),
}
# Targets that MUST have FIRESTORE_EMULATOR_HOST set (emulator).
_EMULATOR_TARGETS: set[tuple[str, str]] = {
    ("cleanup-track1-emulator", "(default)"),
}
# Sanity check: allowlist is the disjoint union of the two above. Prevents
# a future edit from adding a target that skips the env-mismatch check.
assert ALLOWED_TARGETS == _PRODUCTION_TARGETS | _EMULATOR_TARGETS
assert _PRODUCTION_TARGETS.isdisjoint(_EMULATOR_TARGETS)


class EnvironmentMismatchError(Exception):
    """Raised when FIRESTORE_EMULATOR_HOST is set for a production
    target (would silently redirect writes to the emulator) or unset
    for an emulator target (would talk to real Firestore)."""


def _check_environment_match(target: tuple[str, str], env: dict) -> None:
    """Fail-closed on env/target mismatch. The Google Firestore client
    library treats FIRESTORE_EMULATOR_HOST as absolute: if it is set,
    every request is routed to that host regardless of the caller's
    `project` argument. So we cannot trust `--project` alone — we must
    also verify the env is in the correct shape for the intended
    environment."""
    emulator_host = env.get("FIRESTORE_EMULATOR_HOST") or ""
    if target in _PRODUCTION_TARGETS and emulator_host:
        raise EnvironmentMismatchError(
            f"target {target[0]!r}/{target[1]!r} is production, but "
            f"FIRESTORE_EMULATOR_HOST is set. The Google client would "
            f"silently redirect writes to the emulator. Refuse."
        )
    if target in _EMULATOR_TARGETS and not emulator_host:
        raise EnvironmentMismatchError(
            f"target {target[0]!r}/{target[1]!r} is the emulator, but "
            f"FIRESTORE_EMULATOR_HOST is not set. The Google client "
            f"would talk to real Firestore. Refuse."
        )


# --- Firestore adapters ----------------------------------------------------


class GateDocClient:
    """Thin wrapper around google-cloud-firestore so tests can
    substitute a fake. In production the caller passes
    `firestore.Client(project=..., database=...)`.

    Only exposes the methods the writer needs — a hard-typed
    surface prevents the script from accidentally reading OTHER
    Firestore paths."""

    def __init__(self, project: str, database: str, *, client=None):
        self.project = project
        self.database = database
        if client is None:
            from google.cloud import firestore  # type: ignore
            client = firestore.Client(project=project, database=database)
        self._client = client

    def transaction(self):
        return self._client.transaction()

    def gate_ref(self):
        return (
            self._client
            .collection(DEPLOY_GATE_COLLECTION)
            .document(DEPLOY_GATE_DOCUMENT)
        )

    def read_gate(self, *, transaction=None):
        """Bounded read of the gate document. Applies a per-RPC
        timeout and disables the SDK's default retry so a single
        hung read cannot stall the operator. The `@transactional`
        wrapper's own conflict-abort retries are untouched — this
        knob controls only the raw RPC layer."""
        return self.gate_ref().get(
            transaction=transaction,
            timeout=_RPC_TIMEOUT_SEC,
            retry=None,
        )

    def server_timestamp(self):
        from google.cloud import firestore  # type: ignore
        return firestore.SERVER_TIMESTAMP


# --- Gate parsing (matches PR #33's `_parse_deploy_gate_doc`) --------------


class MalformedGateError(Exception):
    """Raised when an existing gate document has a missing/wrong-typed
    `blocked` or `revision` field. Matches the reader contract on
    PR #33 — the writer refuses to touch a malformed document
    without human review."""


def _parse_gate_doc(data: Optional[dict]) -> dict:
    """Return a normalised view of the doc for the writer. `data`
    is `None` for an absent document.

    Absent → `{"exists": False, "blocked": False, "revision": 0}`.
    Existing well-formed → `{"exists": True, "blocked": bool,
    "revision": int, "reason": str|None, "blocked_by": str|None,
    "blocked_at": <opaque>|None}`.
    Anything else → `MalformedGateError`.

    Note: `revision` MUST be a positive int on an existing doc.
    Zero on an existing doc is malformed (0 is reserved for
    the absent-document sentinel).
    """
    if data is None:
        return {
            "exists": False,
            "blocked": False,
            "revision": 0,
            "reason": None,
            "blocked_by": None,
            "blocked_at": None,
        }
    if not isinstance(data, dict):
        raise MalformedGateError(f"document is not an object: {type(data).__name__}")
    if "blocked" not in data:
        raise MalformedGateError("missing field `blocked`")
    blocked = data["blocked"]
    if not isinstance(blocked, bool):
        raise MalformedGateError(
            f"`blocked` has wrong type {type(blocked).__name__}: {blocked!r}"
        )
    revision = data.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise MalformedGateError(
            f"`revision` must be an int, got {type(revision).__name__}: {revision!r}"
        )
    if revision <= 0:
        raise MalformedGateError(
            f"`revision` must be a positive int on an existing doc; got {revision}"
        )
    return {
        "exists": True,
        "blocked": blocked,
        "revision": revision,
        "reason": data.get("reason"),
        "blocked_by": data.get("blocked_by"),
        "blocked_at": data.get("blocked_at"),
    }


# --- Writer core ------------------------------------------------------------


class StaleRevisionError(Exception):
    """Raised inside the transaction when the doc's current revision
    doesn't equal what the operator asserted via `--expected-revision`."""


class GateOperation:
    """One `block` or `unblock` operation. Bundles the arguments,
    computes the effect on-demand from a snapshot, and knows how
    to stage its write on a Firestore transaction.

    Kept object-shaped so tests can drive it against fake data
    without actually opening a transaction."""

    def __init__(
        self,
        *,
        action: str,
        expected_revision: int,
        reason: str,
        blocked_by: str,
    ):
        assert action in ("block", "unblock")
        self.action = action
        self.expected_revision = expected_revision
        self.reason = reason
        self.blocked_by = blocked_by

    def desired_blocked(self) -> bool:
        return self.action == "block"

    def is_noop(self, current_view: dict) -> bool:
        return current_view["blocked"] == self.desired_blocked()

    def check_revision(self, current_view: dict) -> None:
        actual = current_view["revision"]
        if actual != self.expected_revision:
            raise StaleRevisionError(
                f"expected revision {self.expected_revision}, "
                f"current is {actual}"
            )

    def next_revision(self, current_view: dict) -> int:
        return int(current_view["revision"]) + 1


def _run_write(
    client: GateDocClient,
    op: GateOperation,
    *,
    server_ts,
) -> dict:
    """Run the operation inside ONE Firestore transaction.

    Returns a result dict:
      {"kind": "committed", "before": <view>, "after": <view>}
      {"kind": "noop",      "before": <view>}
    Raises MalformedGateError or StaleRevisionError on stops."""
    ref = client.gate_ref()
    tx = client.transaction()
    result: dict = {}

    try:
        from google.cloud.firestore import transactional  # type: ignore
    except Exception:  # pragma: no cover — production always has it
        raise

    @transactional
    def _tx(transaction):
        snap = client.read_gate(transaction=transaction)
        current_view = _parse_gate_doc(snap.to_dict() if snap.exists else None)
        op.check_revision(current_view)
        if op.is_noop(current_view):
            result["kind"] = "noop"
            result["before"] = current_view
            return
        new_revision = op.next_revision(current_view)
        payload = {
            "blocked": op.desired_blocked(),
            "revision": new_revision,
            "reason": op.reason,
            "blocked_by": op.blocked_by,
            "blocked_at": server_ts,
        }
        transaction.set(ref, payload)
        result["kind"] = "committed"
        result["before"] = current_view
        result["after"] = {
            "exists": True,
            "blocked": op.desired_blocked(),
            "revision": new_revision,
            "reason": op.reason,
            "blocked_by": op.blocked_by,
            "blocked_at": "<server-timestamp>",
        }

    _tx(tx)
    return result


# --- CLI -------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deploy_gate.py",
        description=(
            "Operator writer for the production system/deploy_gate "
            "document. Reader lives at "
            "backend/app/services/multichurch_store.py "
            "(FirestoreMultiChurchStore._deploy_gate_ref)."
        ),
    )
    p.add_argument(
        "--project", required=True,
        help="Google Cloud project ID. MUST be on the allowlist.",
    )
    p.add_argument(
        "--database", required=True,
        help="Firestore database ID. MUST be on the allowlist.",
    )
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("status", help="Read-only: print the gate's current state.")

    for cmd in ("block", "unblock"):
        sp = sub.add_parser(cmd, help=f"Set the gate to {cmd}ed.")
        sp.add_argument(
            "--expected-revision", type=int, required=True,
            help=(
                "The gate's current revision as the operator last "
                "observed it. 0 means the document is expected to "
                "be ABSENT. The transaction refuses to write if the "
                "actual revision differs."
            ),
        )
        sp.add_argument(
            "--reason", required=True,
            help="Short human-readable audit note. Written to the doc.",
        )
        sp.add_argument(
            "--blocked-by", required=True,
            help="Operator identity. Written to the doc.",
        )
        sp.add_argument(
            "--apply", action="store_true",
            help=(
                "Perform the write. Without this flag the script "
                "does a dry-run (reads only, prints what would "
                "change)."
            ),
        )
        sp.add_argument(
            "--confirm", default="",
            help=(
                f"Required for --apply. Must be exactly "
                f"'{CONFIRMATION_TOKEN}'."
            ),
        )
    return p


def _sanitize_view(view: dict) -> dict:
    """The set of keys the operator is allowed to see. Never prints
    credentials, secretRefs, or unrelated document contents."""
    return {
        k: view.get(k)
        for k in ("exists", "blocked", "revision", "reason",
                  "blocked_by", "blocked_at")
    }


def _print_result(payload: dict) -> None:
    """Sanitized single-line JSON — same format the helper's audit
    trail records. Never prints credentials."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def _run(argv: list[str], *, client_factory=None) -> int:
    """Entry point separated from `main` so tests can drive it
    with a fake `client_factory` that returns a substitute
    `GateDocClient`."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    target = (args.project, args.database)
    if target not in ALLOWED_TARGETS:
        print(
            f"STOP: (project={args.project!r}, database={args.database!r}) "
            f"is not on the allowlist. Refuse.",
            file=sys.stderr,
        )
        return 2

    try:
        _check_environment_match(target, dict(os.environ))
    except EnvironmentMismatchError as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 6

    if client_factory is None:
        client = GateDocClient(project=args.project, database=args.database)
    else:
        client = client_factory(project=args.project, database=args.database)

    if args.action == "status":
        snap = client.read_gate()
        try:
            view = _parse_gate_doc(snap.to_dict() if snap.exists else None)
        except MalformedGateError as exc:
            _print_result({
                "action": "status",
                "kind": "malformed",
                "malformed_reason": str(exc),
            })
            return 3
        _print_result({"action": "status", "kind": "ok", "view": _sanitize_view(view)})
        return 0

    # block / unblock
    if args.action not in ("block", "unblock"):
        print(f"STOP: unknown action {args.action!r}", file=sys.stderr)
        return 2

    op = GateOperation(
        action=args.action,
        expected_revision=int(args.expected_revision),
        reason=args.reason,
        blocked_by=args.blocked_by,
    )

    # Dry-run path (no writes). Also enforces the --apply +
    # --confirm requirement.
    if not args.apply:
        # Read current state through the same client (no
        # transaction; dry run is intentionally not
        # transactional).
        snap = client.read_gate()
        try:
            current = _parse_gate_doc(snap.to_dict() if snap.exists else None)
        except MalformedGateError as exc:
            _print_result({
                "action": args.action,
                "kind": "malformed",
                "malformed_reason": str(exc),
            })
            return 3
        try:
            op.check_revision(current)
        except StaleRevisionError as exc:
            _print_result({
                "action": args.action,
                "kind": "stale_expected_revision",
                "stale_reason": str(exc),
                "current": _sanitize_view(current),
            })
            return 4
        preview = {
            "action": args.action,
            "kind": "dry_run",
            "would_be_noop": op.is_noop(current),
            "before": _sanitize_view(current),
        }
        if not op.is_noop(current):
            preview["would_write_revision"] = op.next_revision(current)
            preview["would_write_blocked"] = op.desired_blocked()
        _print_result(preview)
        return 0

    # Apply path — --apply required --confirm to match.
    if args.confirm != CONFIRMATION_TOKEN:
        print(
            "STOP: --apply requires --confirm exactly matching "
            f"{CONFIRMATION_TOKEN!r}. Refuse.",
            file=sys.stderr,
        )
        return 5

    try:
        write_result = _run_write(client, op, server_ts=client.server_timestamp())
    except MalformedGateError as exc:
        _print_result({
            "action": args.action,
            "kind": "malformed",
            "malformed_reason": str(exc),
        })
        return 3
    except StaleRevisionError as exc:
        _print_result({
            "action": args.action,
            "kind": "stale_expected_revision",
            "stale_reason": str(exc),
        })
        return 4

    payload: dict[str, Any] = {"action": args.action, "kind": write_result["kind"]}
    payload["before"] = _sanitize_view(write_result["before"])
    if write_result["kind"] == "committed":
        payload["after"] = _sanitize_view(write_result["after"])
    _print_result(payload)
    return 0


def main() -> None:  # pragma: no cover — tested via `_run`
    sys.exit(_run(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
