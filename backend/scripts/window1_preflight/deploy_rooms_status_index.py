"""Deploy the `rooms.status` COLLECTION_GROUP ASCENDING field
override to the production `worship-translation` Firestore
database, with fail-closed pre/post/final snapshots, semantic
diffs of every composite index and field override, bounded
deploy + polling, and a signal-safe post-snapshot trap.

This is the versioned operator driver for PR #42. It is
invoked from `deploy_rooms_status_index.sh` (thin operator
wrapper) or directly from CI fixture tests. All external CLI
paths (`gcloud`, `firebase`) come from env vars so the same
driver runs against real production CLIs AND against the
fake-CLI fixture harness under `backend/tests/deploy_index_fixtures/`.

Contract
--------
- Reads pre-snapshot, target confirmation, PR #42 static
  invariants, delta report — all fail-closed — BEFORE deploy.
- Runs `firebase deploy --only firestore:indexes` under a bounded
  timeout, capturing rc + stdout + stderr.
- Always takes a post-deploy snapshot regardless of outcome
  (success, non-zero, timeout, or SIGINT/SIGTERM) via a signal
  handler that mirrors the shell EXIT-trap idiom.
- Diffs post-snapshot against pre-snapshot AND the desired
  state semantically — every composite index and every field
  override, not just `rooms.status`.
- Polls with a bounded deadline until the new
  COLLECTION_GROUP ASCENDING entry reaches READY.
- Takes a FINAL snapshot after READY and diffs it against
  the desired state.

Exit codes
----------
    0  success — deploy committed, READY, all diffs match
    1  usage / argparse
    2  preconditions (CLI missing, wrong version, ADC absent)
    3  pre-snapshot failure
    4  target confirmation failure (project / database / firebase.json)
    5  static invariants / delta failure (PR #42 file wrong shape)
    6  deploy command failed (non-zero or timeout)
    7  post-snapshot or post-snapshot diff failure
    8  polling failure (NEEDS_REPAIR / MISSING / timeout)
    9  final snapshot or final-diff failure
   99  internal / unexpected error

Environment variables
---------------------
- `PR42_INDEX_AUDIT_DIR` — root of the audit directory tree.
  Must exist, be owner-only, and be empty at driver start.
- `PR42_HELPER_SHA` — the exact PR #42 head commit to pin the
  detached worktree to. See `deploy_rooms_status_index.sh`.
- `PR42_WORKTREE` — path to the detached worktree pinned to
  `PR42_HELPER_SHA`.
- `PR42_FIREBASE_TOOLS_VERSION_PIN` — exact `firebase-tools`
  version the driver requires (matches `firebase --version`).
- `PR42_GCLOUD` — path to gcloud (defaults to `gcloud`).
- `PR42_FIREBASE` — path to firebase (defaults to `firebase`).
- `PR42_DEPLOY_TIMEOUT_SEC` — deploy hard cap (default 300).
- `PR42_POLL_TIMEOUT_SEC` — READY polling budget (default 1800).
- `PR42_POLL_INTERVAL_SEC` — poll interval (default 30).
- `PR42_DRY_RUN` — set to `1` to skip only the `firebase deploy`
  invocation; all snapshots/diffs still run. Used by the
  fixture tests' "would-run-clean" scenario.

Fixture-test wiring
-------------------
`backend/tests/deploy_index_fixtures/` contains fake `gcloud`
and `firebase` shim scripts that this driver invokes when
`PR42_GCLOUD` / `PR42_FIREBASE` point at them. Each fixture
scenario controls the shims through a small state file the
fake CLIs read — see `test_deploy_rooms_status_index.py`.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCRIPT_VERSION = "3.0.0"  # R4: retained Popen + wait(), exact-shape rooms.status validators, env-default finiteness enforcement, final drift regressions

# --- Exit codes ---------------------------------------------------------

class RC:
    OK = 0
    USAGE = 1
    PRECONDITIONS = 2
    PRE_SNAPSHOT = 3
    TARGET_CONFIRM = 4
    STATIC_INVARIANTS = 5
    DEPLOY = 6
    POST_DIFF = 7
    POLL = 8
    FINAL_DIFF = 9
    INTERNAL = 99


# --- Config -------------------------------------------------------------

PROJECT_ID = "sturdy-dogfish-472313-k6"
DATABASE_ID = "worship-translation"
LOCATION_ID = "us-central1"

# R3 correction: `gcloud firestore indexes fields list` accepts
# `--collection-group` as OPTIONAL — omitting it lists explicit
# field overrides across EVERY collection group. R2 wrongly claimed
# a database-wide listing did not exist and used a hardcoded seven-
# group allowlist, which silently missed overrides in unknown or
# newly-introduced collection groups. The driver now runs the
# single db-wide list.
DEFAULT_DEPLOY_TIMEOUT_SEC = 300
DEFAULT_POLL_TIMEOUT_SEC = 1800
DEFAULT_POLL_INTERVAL_SEC = 30

# Firestore's documented single-field index lifecycle states.
# `MISSING` is the driver's absence sentinel returned by
# `_current_rs_state` when the CG_ASC entry is not in the describe
# response — it is NOT a real API state, so once the post-deploy
# snapshot confirms the override exists, a subsequent MISSING
# observation from polling means the entry regressed and must
# hard-stop immediately.
FIRESTORE_INDEX_STATES: frozenset[str] = frozenset({
    "CREATING", "READY", "NEEDS_REPAIR",
})

# Sentinel `ancestorField` name segment for the implicit-default
# fields config that appears in `fields list` without being an
# explicit override. Filter it out of the field-override set.
_ANCESTOR_DEFAULT_FIELDPATH_MARKER = "/collectionGroups/__default__/fields/"


# --- IO helpers ---------------------------------------------------------


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _diag(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.flush()


def _iso_now() -> str:
    n = datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --- Global state for the post-snapshot trap ----------------------------

_POST_SNAPSHOT_TAKEN = False
# R4: the driver retains the Popen (not just the pgid) so the trap
# can (a) terminate the process group AND (b) `wait()` on the child
# to REAP it before snapshotting. Without wait(), a killed-but-not-
# yet-reaped child could still be flushing writes when we read
# state.
_DEPLOY_POPEN: "subprocess.Popen | None" = None


def _terminate_deploy_child() -> None:
    """Terminate the retained deploy child's entire process group
    and wait() on the Popen so the OS reaps it BEFORE the trap
    snapshots. Best-effort; swallows individual OSError so the
    caller can still snapshot."""
    global _DEPLOY_POPEN
    popen = _DEPLOY_POPEN
    if popen is None:
        return
    _DEPLOY_POPEN = None
    if popen.poll() is not None:
        return  # already exited
    try:
        pgid = os.getpgid(popen.pid)
    except (ProcessLookupError, PermissionError):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    # Wait up to 5 s for the group to exit, then SIGKILL.
    try:
        popen.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            popen.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            # Something is severely wrong — write a durable failure
            # marker so the operator can see the trap couldn't reap.
            pass


def _install_trap(audit_dir: Path, gcloud: str) -> None:
    """Install a signal handler + atexit hook that:
      1. terminates the retained deploy Popen's process group and
         waits for it to reap so firebase cannot keep mutating
         production while we snapshot;
      2. writes the post-snapshot to `audit_dir/post/`;
      3. on ANY failure of step (1) or (2), writes a durable
         failure marker `audit_dir/trap-failure.txt` and preserves
         the exception so the operator sees exactly what went
         wrong even after signal-triggered exit;
      4. re-raises the original signal so the process exits with
         the canonical signal exit code.
    Fires exactly once across atexit + SIGINT + SIGTERM."""
    import atexit
    trap_failure_path = audit_dir / "trap-failure.txt"

    def _record_trap_failure(stage: str, exc: BaseException) -> None:
        try:
            with trap_failure_path.open("a") as f:
                f.write(f"{_iso_now()} {stage}: "
                        f"{type(exc).__name__}: {exc}\n")
        except Exception:
            # Even the marker write failed. Diag only.
            _diag(f"CRITICAL: could not write trap-failure marker for {stage}")

    def _run_once():
        global _POST_SNAPSHOT_TAKEN
        if _POST_SNAPSHOT_TAKEN:
            return
        _POST_SNAPSHOT_TAKEN = True
        # (1) Terminate + reap child FIRST. If this fails, we still
        # try to snapshot, but the marker records that we cannot be
        # certain firebase stopped mutating.
        try:
            _terminate_deploy_child()
        except BaseException as exc:  # pragma: no cover
            _record_trap_failure("terminate_deploy_child", exc)
        # (2) Snapshot.
        post_dir = audit_dir / "post"
        try:
            _take_snapshot(gcloud, post_dir, label="post")
        except BaseException as exc:
            _record_trap_failure("take_snapshot(post)", exc)

    def _handler(signum, _frame):
        _run_once()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    atexit.register(_run_once)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handler)


# --- Snapshot -----------------------------------------------------------


def _run_gcloud(gcloud: str, *args: str, timeout: float = 60.0) -> str:
    """Run a gcloud invocation and return stdout. Raises on
    non-zero rc so a snapshot cannot silently return partial data."""
    proc = subprocess.run(
        [gcloud, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gcloud {args!r} rc={proc.returncode}: "
            f"{proc.stderr.strip()[:300]}"
        )
    return proc.stdout


def _take_snapshot(gcloud: str, snapshot_dir: Path, *, label: str) -> None:
    """Take a database-wide snapshot: composite indexes list,
    field overrides list (across ALL collection groups — no
    `--collection-group` filter, per the R3 correction), and the
    rooms.status describe response. Writes one JSON per gcloud
    call + a `manifest.json` + a `snapshot.sha256`. Also records
    the `firestore databases describe` metadata so target
    confirmation has a canonical anchor."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir.chmod(0o700)

    manifest: dict[str, Any] = {
        "label": label,
        "captured_at": _iso_now(),
        "project": PROJECT_ID,
        "database": DATABASE_ID,
        "location": LOCATION_ID,
    }

    # Database-wide composite indexes.
    composite_path = snapshot_dir / "composite-indexes.json"
    composite_json = _run_gcloud(
        gcloud, "firestore", "indexes", "composite", "list",
        f"--project={PROJECT_ID}", f"--database={DATABASE_ID}",
        "--format=json",
    )
    composite_path.write_text(composite_json)
    manifest["composite_count"] = len(json.loads(composite_json))

    # Database-wide field overrides (NO --collection-group filter).
    # Includes the `__default__` ancestor sentinel plus every
    # explicit override across every collection group in the db —
    # the R2 hardcoded seven-group allowlist could not observe an
    # override in an unknown or newly-introduced collection group.
    fields_all_path = snapshot_dir / "fields-list.json"
    fields_all_json = _run_gcloud(
        gcloud, "firestore", "indexes", "fields", "list",
        f"--project={PROJECT_ID}", f"--database={DATABASE_ID}",
        "--format=json",
    )
    fields_all_path.write_text(fields_all_json)
    parsed = json.loads(fields_all_json)
    manifest["fields_list_total"] = len(parsed)
    manifest["fields_list_explicit_overrides"] = sum(
        1 for e in parsed
        if _ANCESTOR_DEFAULT_FIELDPATH_MARKER not in (e.get("name") or "")
    )

    # rooms.status field-level state (the one we care most about).
    rs_path = snapshot_dir / "rooms-status.json"
    rs_json = _run_gcloud(
        gcloud, "firestore", "indexes", "fields", "describe", "status",
        f"--project={PROJECT_ID}", f"--database={DATABASE_ID}",
        "--collection-group=rooms", "--format=json",
    )
    rs_path.write_text(rs_json)

    # Database metadata — canonical target confirmation anchor.
    db_path = snapshot_dir / "database.json"
    db_json = _run_gcloud(
        gcloud, "firestore", "databases", "describe",
        f"--project={PROJECT_ID}", f"--database={DATABASE_ID}",
        "--format=json",
    )
    db_path.write_text(db_json)
    db_meta = json.loads(db_json)
    manifest["database_metadata"] = {
        "name": db_meta.get("name"),
        "type": db_meta.get("type"),
        "locationId": db_meta.get("locationId"),
    }

    (snapshot_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )

    files = sorted(p for p in snapshot_dir.rglob("*") if p.is_file())
    sha_lines = [f"{_sha256(p)}  {p.relative_to(snapshot_dir)}" for p in files
                 if p.name != "snapshot.sha256"]
    (snapshot_dir / "snapshot.sha256").write_text("\n".join(sha_lines) + "\n")


# --- Semantic comparison ------------------------------------------------


def _load_composite(snapshot_dir: Path) -> list[dict[str, Any]]:
    """Return a canonicalized list of composite indexes with only
    the fields our diff cares about."""
    raw = json.loads((snapshot_dir / "composite-indexes.json").read_text())
    out = []
    for entry in raw:
        out.append({
            "collectionGroup": entry.get("collectionGroup") or _parse_cg_from_name(entry.get("name", "")),
            "fields": [
                {"fieldPath": f.get("fieldPath"),
                 "order": f.get("order"),
                 "arrayConfig": f.get("arrayConfig")}
                for f in entry.get("fields", [])
            ],
            "queryScope": entry.get("queryScope"),
            "state": entry.get("state"),
        })
    return out


def _parse_cg_from_name(name: str) -> str | None:
    # Names look like:
    #   projects/.../databases/.../collectionGroups/<cg>/indexes/...
    m = re.search(r"/collectionGroups/([^/]+)/", name)
    return m.group(1) if m else None


def _parse_field_path_from_name(name: str) -> str | None:
    # projects/.../collectionGroups/<cg>/fields/<field>
    m = re.search(r"/fields/([^/]+)$", name)
    return m.group(1) if m else None


def _parse_cg_from_field_name(name: str) -> str | None:
    m = re.search(r"/collectionGroups/([^/]+)/fields/", name)
    return m.group(1) if m else None


def _canonicalize_index_entry(entry: dict) -> dict:
    """Convert one `Index` proto entry into a flat comparison
    record. The REAL gcloud shape nests `order` / `arrayConfig`
    inside `fields[]`. Single-field overrides always have exactly
    one `fields` element (the override's own field path); the
    driver captures that inner order/arrayConfig alongside
    queryScope + state as a canonical shape."""
    fields = entry.get("fields") or []
    inner = fields[0] if fields else {}
    return {
        "fieldPath": inner.get("fieldPath"),
        "order": inner.get("order"),
        "arrayConfig": inner.get("arrayConfig"),
        "queryScope": entry.get("queryScope"),
        "state": entry.get("state"),
    }


def _load_field_overrides(snapshot_dir: Path) -> list[dict[str, Any]]:
    """Return every explicit field override across the database.
    R3 reads `fields-list.json` (the db-wide list) and filters
    out the `__default__` ancestor sentinel — that entry describes
    the default per-field policy, not an explicit override.

    Each returned dict has:
      collectionGroup, fieldPath, indexes[canonical_entry], name
    """
    out = []
    fields_all = json.loads((snapshot_dir / "fields-list.json").read_text())
    for entry in fields_all:
        name = entry.get("name") or ""
        if _ANCESTOR_DEFAULT_FIELDPATH_MARKER in name:
            # Ancestor default — NOT an explicit override.
            continue
        cg = _parse_cg_from_field_name(name)
        fp = _parse_field_path_from_name(name)
        out.append({
            "name": name,
            "collectionGroup": cg,
            "fieldPath": fp,
            "indexes": [
                _canonicalize_index_entry(e)
                for e in entry.get("indexConfig", {}).get("indexes", [])
            ],
            "usesAncestorConfig": entry.get("indexConfig", {}).get("usesAncestorConfig"),
        })
    return out


def _load_rooms_status(snapshot_dir: Path) -> dict[str, Any]:
    """rooms.status describe → canonical (nested-fields shape)."""
    raw = json.loads((snapshot_dir / "rooms-status.json").read_text())
    canonical = [
        _canonicalize_index_entry(e)
        for e in raw.get("indexConfig", {}).get("indexes", [])
    ]
    return {
        "usesAncestorConfig": raw.get("indexConfig", {}).get("usesAncestorConfig"),
        "indexes": sorted(
            canonical,
            key=lambda c: (str(c.get("order") or ""),
                           str(c.get("arrayConfig") or ""),
                           str(c.get("queryScope") or "")),
        ),
    }


def _diff_composites(pre: list[dict], post: list[dict]) -> dict[str, list]:
    """Composites are keyed by (collectionGroup, tuple(fields), queryScope).
    Return {'added': [...], 'removed': [...]}, ignoring `state` for
    membership (READY vs CREATING is not an add/remove event)."""
    def key(c):
        return (
            c.get("collectionGroup"),
            tuple((f.get("fieldPath"), f.get("order"), f.get("arrayConfig"))
                  for f in c.get("fields", [])),
            c.get("queryScope"),
        )
    pre_keys = {key(c) for c in pre}
    post_keys = {key(c) for c in post}
    return {
        "added": sorted(str(k) for k in post_keys - pre_keys),
        "removed": sorted(str(k) for k in pre_keys - post_keys),
    }


def _diff_field_overrides(pre: list[dict], post: list[dict]) -> dict[str, list]:
    """Field overrides keyed by (collectionGroup, fieldPath, sorted
    tuple(indexes)). Ignore `state` for membership.

    Sort keys coerce `None` values (e.g., `order` on an
    ARRAY_CONTAINS entry, `arrayConfig` on an ordered entry) to
    empty strings so tuple ordering is stable across Python 3
    without hitting `TypeError: '<' not supported between …`."""
    def _entry_key(i):
        return (str(i.get("order") or ""),
                str(i.get("arrayConfig") or ""),
                str(i.get("queryScope") or ""))

    def key(o):
        entries = tuple(sorted(
            ((i.get("order"), i.get("arrayConfig"), i.get("queryScope"))
             for i in o.get("indexes", [])),
            key=lambda t: (str(t[0] or ""), str(t[1] or ""), str(t[2] or "")),
        ))
        return (o.get("collectionGroup"), o.get("fieldPath"), entries)
    pre_keys = {key(o) for o in pre}
    post_keys = {key(o) for o in post}
    return {
        "added": sorted(str(k) for k in post_keys - pre_keys),
        "removed": sorted(str(k) for k in pre_keys - post_keys),
    }


# --- PR #42 file static invariants + delta ------------------------------


def _load_pr42_indexes_json(worktree: Path) -> dict[str, Any]:
    path = worktree / "firestore.indexes.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing in worktree")
    return json.loads(path.read_text())


_ALLOWED_TOP_KEYS: frozenset[str] = frozenset({"indexes", "fieldOverrides"})
_ALLOWED_OVERRIDE_KEYS: frozenset[str] = frozenset({
    "collectionGroup", "fieldPath", "indexes",
})
_ALLOWED_ENTRY_KEYS: frozenset[str] = frozenset({
    "order", "arrayConfig", "queryScope",
})


def _static_invariants(pr42_cfg: dict[str, Any]) -> list[str]:
    """R3 finding 8: strict schema. Reject unknown top-level keys,
    unknown override keys, unknown entry keys, wrong types, and
    duplicate entries.

    Required shape:
      {"indexes": [],
       "fieldOverrides": [
         {"collectionGroup": "rooms",
          "fieldPath": "status",
          "indexes": [ EXACTLY four entries; see body ]}
       ]}

    Returns a list of failure reasons; empty means pass."""
    problems: list[str] = []

    # Top-level must be a dict with only allowed keys.
    if not isinstance(pr42_cfg, dict):
        return [f"top level must be a JSON object, got {type(pr42_cfg).__name__}"]
    unknown_top = sorted(set(pr42_cfg.keys()) - _ALLOWED_TOP_KEYS)
    if unknown_top:
        problems.append(f"unknown top-level keys: {unknown_top!r}")

    # `indexes` must be present and equal to [].
    if pr42_cfg.get("indexes") != []:
        problems.append(
            f"expected `indexes` to be [], got {pr42_cfg.get('indexes')!r}"
        )

    overrides = pr42_cfg.get("fieldOverrides")
    if not isinstance(overrides, list):
        problems.append(
            f"expected `fieldOverrides` to be a list, got "
            f"{type(overrides).__name__}"
        )
        return problems
    if len(overrides) != 1:
        problems.append(
            f"expected exactly one fieldOverride, got {len(overrides)}"
        )
        return problems

    o = overrides[0]
    if not isinstance(o, dict):
        problems.append(f"fieldOverride must be an object, got {type(o).__name__}")
        return problems

    unknown_ov = sorted(set(o.keys()) - _ALLOWED_OVERRIDE_KEYS)
    if unknown_ov:
        problems.append(f"unknown fieldOverride keys: {unknown_ov!r}")

    if o.get("collectionGroup") != "rooms":
        problems.append(
            f"collectionGroup must be 'rooms', got {o.get('collectionGroup')!r}"
        )
    if o.get("fieldPath") != "status":
        problems.append(
            f"fieldPath must be 'status', got {o.get('fieldPath')!r}"
        )

    entries = o.get("indexes")
    if not isinstance(entries, list):
        problems.append(
            f"override `indexes` must be a list, got {type(entries).__name__}"
        )
        return problems

    # Each entry must have only allowed keys, correct types.
    normalized_entries: list[tuple] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(f"entries[{i}] must be an object, got {type(entry).__name__}")
            continue
        unknown_e = sorted(set(entry.keys()) - _ALLOWED_ENTRY_KEYS)
        if unknown_e:
            problems.append(f"entries[{i}] unknown keys: {unknown_e!r}")
        order = entry.get("order")
        array_cfg = entry.get("arrayConfig")
        scope = entry.get("queryScope")
        # Enum discipline: exactly one of `order` / `arrayConfig` set.
        if (order is None) == (array_cfg is None):
            problems.append(
                f"entries[{i}] must set exactly one of `order`/`arrayConfig`, "
                f"got order={order!r} arrayConfig={array_cfg!r}"
            )
        if order is not None and order not in ("ASCENDING", "DESCENDING"):
            problems.append(f"entries[{i}] invalid order={order!r}")
        if array_cfg is not None and array_cfg != "CONTAINS":
            problems.append(f"entries[{i}] invalid arrayConfig={array_cfg!r}")
        if scope not in ("COLLECTION", "COLLECTION_GROUP"):
            problems.append(f"entries[{i}] invalid queryScope={scope!r}")
        normalized_entries.append((order, array_cfg, scope))

    # No duplicates.
    if len(normalized_entries) != len(set(normalized_entries)):
        seen = set()
        dupes = []
        for e in normalized_entries:
            if e in seen:
                dupes.append(e)
            seen.add(e)
        problems.append(f"duplicate entries: {dupes!r}")

    # REQUIRED exact four-entry set.
    required = {
        ("ASCENDING", None, "COLLECTION"),
        ("DESCENDING", None, "COLLECTION"),
        (None, "CONTAINS", "COLLECTION"),
        ("ASCENDING", None, "COLLECTION_GROUP"),
    }
    actual = set(normalized_entries)
    missing = required - actual
    extra = actual - required
    if missing:
        problems.append(
            f"missing required entries: {sorted(missing, key=str)!r}"
        )
    if extra:
        problems.append(
            f"unexpected entries: {sorted(extra, key=str)!r}"
        )
    return problems


# --- Pre-deploy semantic delta (R3 finding 3) -------------------------


def _compute_pre_deploy_delta(pre_snapshot_dir: Path,
                              worktree: Path) -> dict[str, Any]:
    """Compute the proposed post-deploy field-override state
    from the pre-snapshot + PR #42's local `firestore.indexes.json`,
    then diff proposed vs pre. Refuse if the proposed change
    would remove any pre-existing override, add anything other
    than the intended `rooms.status`, or leave the composites
    list disturbed. Returns a report dict.

    Called BEFORE `firebase deploy` — so the operator has a
    fail-closed proof that the LOCAL config, if applied, would
    take production from the current pre-snapshot to
    (pre + intended rooms.status) with nothing else changed."""
    pre_over = _load_field_overrides(pre_snapshot_dir)
    pr42_cfg = _load_pr42_indexes_json(worktree)
    # The PR #42 file's local `fieldOverrides` list IS the
    # complete post-state that `firebase deploy` would apply
    # (deleting anything not present remotely). Build the
    # "proposed post-state" as {existing pre-overrides for OTHER
    # (collectionGroup, fieldPath) pairs} + {PR42's local entries
    # for the pairs it declares}. Then compare against pre.
    #
    # Under `firebase deploy` semantics, ANY override remotely
    # present but not in the local file WOULD BE DELETED. R3 must
    # refuse deploy if such a deletion would happen.
    local_overrides = pr42_cfg.get("fieldOverrides", [])
    local_pairs = {
        (o.get("collectionGroup"), o.get("fieldPath"))
        for o in local_overrides
    }
    pre_pairs = {
        (o.get("collectionGroup"), o.get("fieldPath"))
        for o in pre_over
    }
    would_delete = pre_pairs - local_pairs
    if would_delete:
        return {
            "ok": False,
            "kind": "would_delete_remote_overrides",
            "detail": {"would_delete": sorted(str(p) for p in would_delete)},
        }
    unrelated_local = local_pairs - {("rooms", "status")}
    if unrelated_local:
        return {
            "ok": False,
            "kind": "local_declares_unrelated_overrides",
            "detail": {"unrelated": sorted(str(p) for p in unrelated_local)},
        }
    # The one intended add: rooms.status not yet in pre.
    if ("rooms", "status") in pre_pairs:
        return {
            "ok": False,
            "kind": "rooms_status_already_present",
            "detail": {"note": "pre-snapshot already contains an explicit "
                              "rooms.status override — R4 inventory said no"},
        }
    # Composites: PR #42 declares `indexes: []`. If pre has any
    # composite index, firebase deploy would delete it. Refuse.
    pre_composites = _load_composite(pre_snapshot_dir)
    local_composites = pr42_cfg.get("indexes", [])
    if pre_composites and local_composites == []:
        return {
            "ok": False,
            "kind": "would_delete_remote_composites",
            "detail": {"pre_composite_count": len(pre_composites)},
        }
    return {
        "ok": True,
        "kind": "delta_ready",
        "detail": {
            "would_add_field_overrides": [{"collectionGroup": "rooms",
                                           "fieldPath": "status"}],
            "would_add_composites": [],
            "would_remove_field_overrides": [],
            "would_remove_composites": [],
        },
    }


# --- Deploy invocation --------------------------------------------------


def _deploy(firebase: str, worktree: Path, timeout_sec: float,
            audit_dir: Path) -> int:
    """Run `firebase deploy --only firestore:indexes` from the
    detached worktree with a bounded timeout. Returns rc; captures
    stdout/stderr into audit_dir/deploy/.

    R3 finding 6 + R4: the child runs in its own session so a
    signal to the driver doesn't leave firebase (and its
    `firebase-tools` spawns) mutating production. The R4 driver
    retains the full `Popen` object as `_DEPLOY_POPEN` — the trap
    uses that to killpg AND wait() before snapshotting."""
    global _DEPLOY_POPEN
    deploy_dir = audit_dir / "deploy"
    deploy_dir.mkdir(parents=True, exist_ok=True)
    deploy_dir.chmod(0o700)
    cmd = [
        firebase, "deploy",
        f"--project={PROJECT_ID}",
        "--only=firestore:indexes",
        "--non-interactive",
        "--json",
    ]
    (deploy_dir / "cmd.txt").write_text(json.dumps(cmd) + "\n")
    so = (deploy_dir / "deploy.stdout").open("wb")
    se = (deploy_dir / "deploy.stderr").open("wb")
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(worktree),
            stdout=so, stderr=se,
            start_new_session=True,
        )
        _DEPLOY_POPEN = proc
        try:
            rc = proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            se.write(
                f"\n\n[driver] firebase deploy timed out after "
                f"{timeout_sec:.0f}s — terminating process group\n".encode()
            )
            _terminate_deploy_child()
            rc = 124  # matches GNU `timeout` exit code convention
    finally:
        so.close()
        se.close()
        # Normal exit: child is already reaped by wait(). Clear
        # the ref so the trap doesn't wait() again.
        _DEPLOY_POPEN = None
    (deploy_dir / "deploy.rc").write_text(f"{rc}\n")
    return rc


# --- Polling ------------------------------------------------------------


def _current_rs_state(gcloud: str) -> str:
    """Return the state of the rooms.status COLLECTION_GROUP
    ASCENDING entry, or 'MISSING' if absent. Reads the nested
    `Index.fields[]` shape gcloud actually returns (R3 correction
    — R2 read a non-existent flat shape and never matched any
    real production entry)."""
    raw = _run_gcloud(
        gcloud, "firestore", "indexes", "fields", "describe", "status",
        f"--project={PROJECT_ID}", f"--database={DATABASE_ID}",
        "--collection-group=rooms", "--format=json",
    )
    data = json.loads(raw)
    for e in data.get("indexConfig", {}).get("indexes", []):
        canonical = _canonicalize_index_entry(e)
        if (canonical.get("order") == "ASCENDING"
                and canonical.get("queryScope") == "COLLECTION_GROUP"):
            return canonical.get("state") or "UNKNOWN"
    return "MISSING"


def _poll_until_ready(gcloud: str, audit_dir: Path, *,
                      timeout_sec: float, interval_sec: float,
                      assert_present: bool) -> str:
    """Poll `rooms.status` until state=READY. Returns the terminal
    state observed. Raises TimeoutError on timeout, RuntimeError
    on NEEDS_REPAIR or unexpected state.

    R3 finding 7: `MISSING` is the driver's absence sentinel, not
    a documented Firestore index lifecycle state (which is one of
    CREATING / READY / NEEDS_REPAIR per the docs). Once the
    post-deploy snapshot has confirmed the CG_ASC entry exists,
    a subsequent MISSING observation must hard-stop immediately —
    it means the entry regressed. `assert_present=True` enables
    that immediate stop; the driver passes True after post-diff
    passes."""
    poll_dir = audit_dir / "poll"
    poll_dir.mkdir(parents=True, exist_ok=True)
    poll_dir.chmod(0o700)
    log_path = poll_dir / "poll.log"
    deadline = time.monotonic() + timeout_sec
    while True:
        state = _current_rs_state(gcloud)
        with log_path.open("a") as f:
            f.write(f"{_iso_now()} state={state}\n")
        if state == "READY":
            return state
        if state == "NEEDS_REPAIR":
            raise RuntimeError(f"rooms.status entered {state}")
        if state == "MISSING":
            if assert_present:
                raise RuntimeError(
                    "rooms.status COLLECTION_GROUP ASCENDING entry "
                    "was present in the post-deploy snapshot but is "
                    "MISSING from a subsequent describe response — "
                    "index regressed"
                )
            # Only permitted when we do NOT yet know the override
            # is present; the driver never passes assert_present
            # False today, so this branch is defensive.
        elif state != "CREATING":
            raise RuntimeError(
                f"unexpected state {state!r} (not one of "
                f"{sorted(FIRESTORE_INDEX_STATES)!r} or MISSING)"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"index did not reach READY within {timeout_sec:.0f}s "
                f"(last state={state})"
            )
        time.sleep(interval_sec)


# --- Main driver --------------------------------------------------------


def _die(rc: int, kind: str, reason: str) -> "None":
    """Emit exactly one JSON payload on stdout and exit."""
    _emit({
        "kind": "deploy_rooms_status_index",
        "command": "deploy_rooms_status_index.py",
        "script_version": SCRIPT_VERSION,
        "verified_at": _iso_now(),
        "rc": rc,
        "outcome": kind,
        "reason": reason,
    })
    _diag(f"STOP: {kind}: {reason}")
    sys.exit(rc)


def _positive_finite_float(kind: str, name: str):
    """Argparse type callable that rejects zero, negative, NaN,
    and +/-inf via `argparse.ArgumentTypeError` — same pattern as
    PR #41's `common.positive_float`."""
    import math

    def _check(raw):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(
                f"{name}: expected a positive finite {kind}, got {raw!r}"
            )
        if not math.isfinite(value):
            raise argparse.ArgumentTypeError(
                f"{name}: expected a finite {kind}, got {raw!r}"
            )
        if value <= 0:
            raise argparse.ArgumentTypeError(
                f"{name}: expected a positive {kind} > 0, got {raw!r}"
            )
        return value
    return _check


def _validate_positive_finite_or_die(name: str, value) -> float:
    """R4 finding 5: argparse does not run the `type=` validator
    on `default=` values, so environment-derived defaults could
    bypass `_positive_finite_float()`. This post-parse validation
    re-checks the resolved value regardless of source (CLI flag,
    env var, or hard-coded constant)."""
    import math
    try:
        v = float(value)
    except (TypeError, ValueError):
        _die(RC.USAGE, "usage",
             f"{name}: expected a positive finite float, got {value!r}")
    if not math.isfinite(v):
        _die(RC.USAGE, "usage",
             f"{name}: expected a finite float, got {value!r}")
    if v <= 0:
        _die(RC.USAGE, "usage",
             f"{name}: expected a positive float > 0, got {value!r}")
    return v


# --- Exact-shape rooms.status validators (R4 finding 3) ---------------


def _validate_pre_rooms_status(snapshot_dir: Path) -> None:
    """Pre-deploy inherited baseline: rooms.status describe must
    show `usesAncestorConfig == true` and EXACTLY three
    COLLECTION-scope entries — ASC, DESC, ARRAY_CONTAINS — all
    READY — with nested `fieldPath == "status"`. No
    COLLECTION_GROUP entry may exist."""
    rs = _load_rooms_status(snapshot_dir)
    problems: list[str] = []
    if rs.get("usesAncestorConfig") is not True:
        problems.append(
            f"pre rooms.status usesAncestorConfig={rs.get('usesAncestorConfig')!r}, "
            f"expected True"
        )
    entries = rs.get("indexes", [])
    if len(entries) != 3:
        problems.append(
            f"pre rooms.status has {len(entries)} entries, expected 3"
        )
    required = {
        ("ASCENDING", None, "COLLECTION"),
        ("DESCENDING", None, "COLLECTION"),
        (None, "CONTAINS", "COLLECTION"),
    }
    actual = {
        (e.get("order"), e.get("arrayConfig"), e.get("queryScope"))
        for e in entries
    }
    missing = required - actual
    extra = actual - required
    if missing:
        problems.append(f"pre missing entries: {sorted(missing, key=str)!r}")
    if extra:
        problems.append(f"pre unexpected entries: {sorted(extra, key=str)!r}")
    for i, e in enumerate(entries):
        if e.get("state") != "READY":
            problems.append(f"pre entries[{i}] state={e.get('state')!r} != READY")
        if e.get("fieldPath") != "status":
            problems.append(f"pre entries[{i}] fieldPath={e.get('fieldPath')!r} != status")
    if problems:
        _die(RC.PRE_SNAPSHOT, "pre_rooms_status_shape",
             "; ".join(problems))


def _validate_post_rooms_status(snapshot_dir: Path) -> None:
    """Post-deploy: rooms.status must show `usesAncestorConfig ==
    false` (explicit override took over), FOUR entries with
    nested `fieldPath == "status"`, three COLLECTION-scope
    entries READY, and one COLLECTION_GROUP ASCENDING entry in
    {CREATING, READY}."""
    rs = _load_rooms_status(snapshot_dir)
    problems: list[str] = []
    if rs.get("usesAncestorConfig") is not False:
        problems.append(
            f"post rooms.status usesAncestorConfig={rs.get('usesAncestorConfig')!r}, "
            f"expected False"
        )
    entries = rs.get("indexes", [])
    if len(entries) != 4:
        problems.append(
            f"post rooms.status has {len(entries)} entries, expected 4"
        )
    for i, e in enumerate(entries):
        if e.get("fieldPath") != "status":
            problems.append(f"post entries[{i}] fieldPath={e.get('fieldPath')!r} != status")
    col_asc = [e for e in entries
               if e.get("order") == "ASCENDING"
               and e.get("queryScope") == "COLLECTION"]
    col_desc = [e for e in entries
                if e.get("order") == "DESCENDING"
                and e.get("queryScope") == "COLLECTION"]
    col_arr = [e for e in entries
               if e.get("arrayConfig") == "CONTAINS"
               and e.get("queryScope") == "COLLECTION"]
    cg_asc = [e for e in entries
              if e.get("order") == "ASCENDING"
              and e.get("queryScope") == "COLLECTION_GROUP"]
    if len(col_asc) != 1 or col_asc[0].get("state") != "READY":
        problems.append(f"post COLLECTION ASC entries={col_asc!r}")
    if len(col_desc) != 1 or col_desc[0].get("state") != "READY":
        problems.append(f"post COLLECTION DESC entries={col_desc!r}")
    if len(col_arr) != 1 or col_arr[0].get("state") != "READY":
        problems.append(f"post COLLECTION ARRAY_CONTAINS entries={col_arr!r}")
    if len(cg_asc) != 1:
        problems.append(f"post COLLECTION_GROUP ASC count={len(cg_asc)}, expected 1")
    elif cg_asc[0].get("state") not in ("CREATING", "READY"):
        problems.append(f"post CG_ASC state={cg_asc[0].get('state')!r} not in "
                        f"{{CREATING, READY}}")
    if problems:
        _die(RC.POST_DIFF, "post_rooms_status_shape",
             "; ".join(problems))


def _validate_final_rooms_status(snapshot_dir: Path) -> None:
    """Final: same shape as post except all four entries MUST
    be READY."""
    rs = _load_rooms_status(snapshot_dir)
    problems: list[str] = []
    if rs.get("usesAncestorConfig") is not False:
        problems.append(
            f"final rooms.status usesAncestorConfig={rs.get('usesAncestorConfig')!r}, "
            f"expected False"
        )
    entries = rs.get("indexes", [])
    if len(entries) != 4:
        problems.append(
            f"final rooms.status has {len(entries)} entries, expected 4"
        )
    for i, e in enumerate(entries):
        if e.get("fieldPath") != "status":
            problems.append(f"final entries[{i}] fieldPath={e.get('fieldPath')!r} != status")
        if e.get("state") != "READY":
            problems.append(f"final entries[{i}] state={e.get('state')!r} != READY")
    required = {
        ("ASCENDING", None, "COLLECTION"),
        ("DESCENDING", None, "COLLECTION"),
        (None, "CONTAINS", "COLLECTION"),
        ("ASCENDING", None, "COLLECTION_GROUP"),
    }
    actual = {
        (e.get("order"), e.get("arrayConfig"), e.get("queryScope"))
        for e in entries
    }
    if actual != required:
        problems.append(
            f"final entry-shape mismatch: "
            f"missing={sorted(required - actual, key=str)!r} "
            f"extra={sorted(actual - required, key=str)!r}"
        )
    if problems:
        _die(RC.FINAL_DIFF, "final_rooms_status_shape",
             "; ".join(problems))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deploy_rooms_status_index.py",
        description=(
            "Deploy the rooms.status collection-group ASCENDING index "
            "override with fail-closed snapshots + semantic diffs."
        ),
    )
    p.add_argument("--audit-dir", required=True,
                   help="Owner-only audit directory (must be empty)")
    p.add_argument("--worktree", required=True,
                   help="Detached worktree pinned to PR #42 SHA")
    p.add_argument("--pr42-sha", required=True,
                   help="Exact PR #42 head commit the worktree must be at")
    p.add_argument("--reviewer-approved-sha", required=True,
                   help=(
                       "Independent copy of the reviewer-approved PR #42 SHA "
                       "the operator has verified against out-of-band review "
                       "artifacts. MUST equal --pr42-sha; refusing to accept "
                       "a newer HEAD that hasn't been reviewed."
                   ))
    p.add_argument("--script-sha256", required=True,
                   help=(
                       "sha256 hex digest of this driver file as it was "
                       "reviewed. Rechecked at runtime against the actual "
                       "file — a swap would refuse to run."
                   ))
    p.add_argument("--gcloud", default=os.environ.get("PR42_GCLOUD", "gcloud"))
    p.add_argument("--firebase", default=os.environ.get("PR42_FIREBASE", "firebase"))
    p.add_argument("--firebase-tools-version-pin", required=True,
                   help="Required output of `firebase --version` (exact match)")
    p.add_argument(
        "--deploy-timeout-sec",
        type=_positive_finite_float("float", "--deploy-timeout-sec"),
        default=float(os.environ.get(
            "PR42_DEPLOY_TIMEOUT_SEC", DEFAULT_DEPLOY_TIMEOUT_SEC)),
    )
    p.add_argument(
        "--poll-timeout-sec",
        type=_positive_finite_float("float", "--poll-timeout-sec"),
        default=float(os.environ.get(
            "PR42_POLL_TIMEOUT_SEC", DEFAULT_POLL_TIMEOUT_SEC)),
    )
    p.add_argument(
        "--poll-interval-sec",
        type=_positive_finite_float("float", "--poll-interval-sec"),
        default=float(os.environ.get(
            "PR42_POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL_SEC)),
    )
    p.add_argument("--dry-run", action="store_true",
                   help=(
                       "Preparation-only mode: run preconditions, "
                       "pre-snapshot, target confirmation (incl. gcloud "
                       "firestore databases describe), static invariants "
                       "on PR #42 config, AND the pre-deploy semantic delta "
                       "against the pre-snapshot. Do NOT invoke firebase, "
                       "post-diff, poll, or final-diff."
                   ))
    return p


def _check_preconditions(args: argparse.Namespace) -> None:
    # R3 finding 5: driver script hash equality. The operator MUST
    # pass the reviewed sha256 of this file; a swap of the running
    # script would refuse to run.
    script_path = Path(__file__).resolve()
    actual_sha = _sha256(script_path)
    if actual_sha != args.script_sha256:
        _die(RC.PRECONDITIONS, "preconditions",
             f"driver script sha256 mismatch: file={actual_sha!r}, "
             f"pin={args.script_sha256!r} — script has been modified "
             f"since review")

    # R3 finding 5: reviewer-approved SHA must equal the runtime-
    # pinned PR #42 SHA. `gh pr view --json headRefOid` alone would
    # silently accept a newer, unreviewed head — the operator's
    # duty is to bring the reviewed SHA as an independent input.
    if args.reviewer_approved_sha != args.pr42_sha:
        _die(RC.PRECONDITIONS, "preconditions",
             f"reviewer-approved SHA {args.reviewer_approved_sha!r} != "
             f"runtime --pr42-sha {args.pr42_sha!r}")

    # gcloud exists
    if shutil.which(args.gcloud) is None and not Path(args.gcloud).is_file():
        _die(RC.PRECONDITIONS, "preconditions",
             f"gcloud not found: {args.gcloud!r}")
    # firebase exists
    if shutil.which(args.firebase) is None and not Path(args.firebase).is_file():
        _die(RC.PRECONDITIONS, "preconditions",
             f"firebase not found: {args.firebase!r}")
    # R3 finding 8: firebase --version must succeed (rc == 0) AND
    # its stdout must match the pin exactly.
    try:
        ver_proc = subprocess.run(
            [args.firebase, "--version"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:
        _die(RC.PRECONDITIONS, "preconditions",
             f"firebase --version failed: {exc}")
    if ver_proc.returncode != 0:
        _die(RC.PRECONDITIONS, "preconditions",
             f"firebase --version rc={ver_proc.returncode}, "
             f"stderr={ver_proc.stderr.strip()[:200]!r}")
    ver = ver_proc.stdout.strip()
    if ver != args.firebase_tools_version_pin:
        _die(RC.PRECONDITIONS, "preconditions",
             f"firebase-tools version mismatch: have {ver!r}, "
             f"pin {args.firebase_tools_version_pin!r}")
    # Worktree present and at pinned SHA
    wt = Path(args.worktree)
    if not wt.is_dir():
        _die(RC.PRECONDITIONS, "preconditions",
             f"worktree not found: {wt}")
    try:
        head = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=15,
        ).stdout.strip()
    except Exception as exc:
        _die(RC.PRECONDITIONS, "preconditions",
             f"git rev-parse failed on worktree {wt}: {exc}")
    if head != args.pr42_sha:
        _die(RC.PRECONDITIONS, "preconditions",
             f"worktree HEAD {head!r} != pinned PR42 SHA {args.pr42_sha!r}")
    # Worktree clean
    dirty = subprocess.run(
        ["git", "-C", str(wt), "status", "--porcelain"],
        capture_output=True, text=True, check=True, timeout=15,
    ).stdout.strip()
    if dirty:
        _die(RC.PRECONDITIONS, "preconditions",
             f"worktree {wt} is dirty: {dirty!r}")
    # R3 finding 5 (continued): the driver we invoke must live
    # inside the pinned worktree — not the operator's random
    # checkout. Compare absolute paths.
    expected_script = (wt / "backend" / "scripts" / "window1_preflight"
                       / "deploy_rooms_status_index.py").resolve()
    if script_path != expected_script:
        _die(RC.PRECONDITIONS, "preconditions",
             f"driver script path {str(script_path)!r} is not the "
             f"one inside the pinned worktree "
             f"{str(expected_script)!r}")
    # Audit dir owner-only + empty
    ad = Path(args.audit_dir)
    if not ad.is_dir():
        _die(RC.PRECONDITIONS, "preconditions",
             f"audit dir not found: {ad}")
    mode = ad.stat().st_mode & 0o777
    if mode != 0o700:
        _die(RC.PRECONDITIONS, "preconditions",
             f"audit dir must be mode 700, is {oct(mode)}")
    if any(ad.iterdir()):
        _die(RC.PRECONDITIONS, "preconditions",
             f"audit dir must be empty at driver start")


def _confirm_target(args: argparse.Namespace, audit_dir: Path) -> None:
    wt = Path(args.worktree)
    fb = json.loads((wt / "firebase.json").read_text())
    fs = fb.get("firestore", {})
    if fs.get("database") != DATABASE_ID:
        _die(RC.TARGET_CONFIRM, "target_confirm",
             f"firebase.json database={fs.get('database')!r} != {DATABASE_ID!r}")
    if fs.get("location") != LOCATION_ID:
        _die(RC.TARGET_CONFIRM, "target_confirm",
             f"firebase.json location={fs.get('location')!r} != {LOCATION_ID!r}")
    resolved = (wt / fs.get("indexes", "")).resolve()
    expected = (wt / "firestore.indexes.json").resolve()
    if resolved != expected:
        _die(RC.TARGET_CONFIRM, "target_confirm",
             f"firebase.json indexes pointer resolves to {resolved}, "
             f"expected {expected}")
    # .firebaserc project alias
    rc_json = json.loads((wt / ".firebaserc").read_text())
    default = rc_json.get("projects", {}).get("default")
    if default != PROJECT_ID:
        _die(RC.TARGET_CONFIRM, "target_confirm",
             f".firebaserc default project={default!r} != {PROJECT_ID!r}")
    # R3 finding 8: cross-check the REAL remote database via
    # `gcloud firestore databases describe`. The pre-snapshot has
    # already captured this into audit_dir/pre/database.json; read
    # from there so we don't re-hit the API and so a fresh snapshot
    # is on file for the audit trail.
    db_meta = json.loads((audit_dir / "pre" / "database.json").read_text())
    expected_name = f"projects/{PROJECT_ID}/databases/{DATABASE_ID}"
    if db_meta.get("name") != expected_name:
        _die(RC.TARGET_CONFIRM, "target_confirm",
             f"remote database.name={db_meta.get('name')!r} != {expected_name!r}")
    if db_meta.get("locationId") != LOCATION_ID:
        _die(RC.TARGET_CONFIRM, "target_confirm",
             f"remote database.locationId={db_meta.get('locationId')!r} "
             f"!= {LOCATION_ID!r}")
    if db_meta.get("type") not in ("FIRESTORE_NATIVE",):
        _die(RC.TARGET_CONFIRM, "target_confirm",
             f"remote database.type={db_meta.get('type')!r} — expected "
             f"FIRESTORE_NATIVE")


def _desired_field_overrides(pre_overrides: list[dict]) -> list[dict]:
    """Compute the desired post-deploy field-override set from the
    pre-snapshot: everything in pre PLUS the new rooms.status entry.
    Uses the same canonical key shape as `_diff_field_overrides`."""
    def normalize(o: dict) -> dict:
        return {
            "collectionGroup": o.get("collectionGroup"),
            "fieldPath": o.get("fieldPath"),
            "indexes": sorted(
                [{"order": i.get("order"),
                  "arrayConfig": i.get("arrayConfig"),
                  "queryScope": i.get("queryScope")}
                 for i in o.get("indexes", [])],
                key=lambda i: (str(i.get("order")),
                               str(i.get("arrayConfig")),
                               str(i.get("queryScope"))),
            ),
        }
    desired = [normalize(o) for o in pre_overrides]
    # The one intended add: rooms.status with all four entries.
    intended = {
        "collectionGroup": "rooms",
        "fieldPath": "status",
        "indexes": sorted([
            {"order": "ASCENDING", "arrayConfig": None, "queryScope": "COLLECTION"},
            {"order": "DESCENDING", "arrayConfig": None, "queryScope": "COLLECTION"},
            {"order": None, "arrayConfig": "CONTAINS", "queryScope": "COLLECTION"},
            {"order": "ASCENDING", "arrayConfig": None, "queryScope": "COLLECTION_GROUP"},
        ], key=lambda i: (str(i.get("order")),
                          str(i.get("arrayConfig")),
                          str(i.get("queryScope")))),
    }
    # Replace any pre-existing rooms.status override (there shouldn't
    # be one per the R4 inventory, but code defensively) with the
    # intended shape.
    desired = [d for d in desired
               if not (d.get("collectionGroup") == "rooms"
                       and d.get("fieldPath") == "status")]
    desired.append(intended)
    return desired


def main(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)

    # R4 finding 5: argparse `type=` isn't re-run on `default=`
    # values, so env-var-provided defaults could contain nan/inf/
    # negative/zero and bypass `_positive_finite_float`. Re-check
    # each numeric arg post-parse regardless of source.
    args.deploy_timeout_sec = _validate_positive_finite_or_die(
        "--deploy-timeout-sec", args.deploy_timeout_sec,
    )
    args.poll_timeout_sec = _validate_positive_finite_or_die(
        "--poll-timeout-sec", args.poll_timeout_sec,
    )
    args.poll_interval_sec = _validate_positive_finite_or_die(
        "--poll-interval-sec", args.poll_interval_sec,
    )

    # 0. Preconditions
    _check_preconditions(args)

    audit_dir = Path(args.audit_dir).resolve()

    # 1. Pre-snapshot + inherited-baseline validator
    try:
        _take_snapshot(args.gcloud, audit_dir / "pre", label="pre")
    except Exception as exc:
        _die(RC.PRE_SNAPSHOT, "pre_snapshot",
             f"{type(exc).__name__}: {exc}")
    # R4 finding 3: assert pre rooms.status shape
    # (usesAncestorConfig=True, 3 COLLECTION READY entries, no CG).
    _validate_pre_rooms_status(audit_dir / "pre")

    # 2. Target confirm (also cross-checks the REAL remote via
    #    gcloud firestore databases describe from the pre-snapshot).
    try:
        _confirm_target(args, audit_dir)
    except SystemExit:
        raise
    except Exception as exc:
        _die(RC.TARGET_CONFIRM, "target_confirm",
             f"{type(exc).__name__}: {exc}")

    # 3. Static invariants on PR #42's file (strict schema).
    try:
        pr42_cfg = _load_pr42_indexes_json(Path(args.worktree))
        problems = _static_invariants(pr42_cfg)
        if problems:
            _die(RC.STATIC_INVARIANTS, "static_invariants",
                 "; ".join(problems))
    except SystemExit:
        raise
    except Exception as exc:
        _die(RC.STATIC_INVARIANTS, "static_invariants",
             f"{type(exc).__name__}: {exc}")

    # 3b. R3 finding 3: pre-deploy semantic delta. Prove the LOCAL
    #     config, if applied, would take production from the
    #     pre-snapshot to (pre + intended rooms.status) with
    #     nothing else changed. Any unrelated remote composite
    #     or override that firebase deploy would delete → refuse.
    try:
        delta = _compute_pre_deploy_delta(audit_dir / "pre",
                                          Path(args.worktree))
    except Exception as exc:
        _die(RC.STATIC_INVARIANTS, "pre_deploy_delta",
             f"{type(exc).__name__}: {exc}")
    (audit_dir / "pre-deploy-delta.json").write_text(
        json.dumps(delta, indent=2, sort_keys=True) + "\n"
    )
    if not delta.get("ok"):
        _die(RC.STATIC_INVARIANTS, "pre_deploy_delta",
             f"delta refused: kind={delta.get('kind')} "
             f"detail={delta.get('detail')!r}")

    # 4. --dry-run: preparation checkpoint. Skips deploy, post-diff,
    #    polling, final-diff. Trap is NOT installed because there is
    #    no deploy for it to guard.
    if args.dry_run:
        # Dry-run is the "preparation-only" mode: preconditions,
        # pre-snapshot, target confirmation, and static invariants
        # already ran. If we reached here, the operator's environment
        # is READY to deploy. Skip firebase deploy, post-diff,
        # polling, and final-diff — those all require an actual apply
        # to make sense. Report OK.
        _diag("[driver] dry-run: preconditions + pre-snapshot + target + "
              "static invariants passed; skipping firebase deploy and "
              "post-deploy phases")
        (audit_dir / "deploy").mkdir(exist_ok=True)
        (audit_dir / "deploy").chmod(0o700)
        (audit_dir / "deploy" / "dry-run.txt").write_text(
            "dry-run: no firebase deploy invoked; no post-diff, no "
            "polling, no final-diff\n"
        )
        _emit({
            "kind": "deploy_rooms_status_index",
            "command": "deploy_rooms_status_index.py",
            "script_version": SCRIPT_VERSION,
            "verified_at": _iso_now(),
            "rc": RC.OK,
            "outcome": "dry_run_ready",
            "project": PROJECT_ID,
            "database": DATABASE_ID,
            "pr42_sha": args.pr42_sha,
            "firebase_tools_version_pin": args.firebase_tools_version_pin,
        })
        return RC.OK

    # 5. Install post-snapshot trap BEFORE the deploy so signals
    #    (SIGINT/SIGTERM), non-zero exits, and normal completion
    #    all leave a post-snapshot in audit_dir/post/.
    _install_trap(audit_dir, args.gcloud)

    deploy_rc = _deploy(args.firebase, Path(args.worktree),
                        args.deploy_timeout_sec, audit_dir)

    # 6. Explicit post-snapshot (atexit will not fire twice).
    global _POST_SNAPSHOT_TAKEN
    if not _POST_SNAPSHOT_TAKEN:
        _POST_SNAPSHOT_TAKEN = True
        try:
            _take_snapshot(args.gcloud, audit_dir / "post", label="post")
        except Exception as exc:
            _die(RC.POST_DIFF, "post_snapshot",
                 f"{type(exc).__name__}: {exc}")

    # 7. Deploy-command outcome takes precedence over post-diff.
    #    The post-snapshot has already been captured (trap fires
    #    regardless), so the operator has a full record; but if
    #    firebase deploy exited non-zero, the strict "exactly one
    #    new override" invariant is not the salient failure — the
    #    deploy is. Report rc=6 here and stop.
    if deploy_rc != 0:
        _die(RC.DEPLOY, "deploy_nonzero",
             f"firebase deploy rc={deploy_rc}")

    # 7b. R4 finding 3: validate post rooms.status shape
    # (usesAncestorConfig=False, 4 entries, three COLLECTION READY
    # + CG_ASC ∈ {CREATING, READY}, nested fieldPath="status").
    _validate_post_rooms_status(audit_dir / "post")

    # 8. Semantic diff — post vs pre AND vs desired
    try:
        pre_comp = _load_composite(audit_dir / "pre")
        post_comp = _load_composite(audit_dir / "post")
        pre_over = _load_field_overrides(audit_dir / "pre")
        post_over = _load_field_overrides(audit_dir / "post")
        comp_diff = _diff_composites(pre_comp, post_comp)
        # Composites: nothing should have been added or removed by
        # this deploy.
        if comp_diff["added"] or comp_diff["removed"]:
            _die(RC.POST_DIFF, "post_composites_diff",
                 f"composites changed unexpectedly: {comp_diff!r}")
        # Field overrides: exactly one addition matching rooms.status
        # with the intended entry set; NOTHING removed from pre-state.
        over_diff = _diff_field_overrides(pre_over, post_over)
        if over_diff["removed"]:
            _die(RC.POST_DIFF, "post_field_overrides_removed",
                 f"field overrides removed: {over_diff['removed']!r}")
        if len(over_diff["added"]) != 1:
            _die(RC.POST_DIFF, "post_field_overrides_added",
                 f"expected exactly one new field override, got "
                 f"{len(over_diff['added'])}: {over_diff['added']!r}")
        # The one added override must be rooms.status with the four
        # expected entries. Verify by finding it in post_over and
        # checking shape.
        added_rooms_status = [
            o for o in post_over
            if o.get("collectionGroup") == "rooms" and o.get("fieldPath") == "status"
            and o not in pre_over
        ]
        if len(added_rooms_status) != 1:
            _die(RC.POST_DIFF, "post_new_override_not_rooms_status",
                 f"expected 1 new rooms.status, got {len(added_rooms_status)}")
        # Verify the entry set on the new override matches the desired
        # set (COL ASC, COL DESC, COL ARR, CG ASC).
        entries = added_rooms_status[0]["indexes"]
        by_key = {(e.get("order"), e.get("arrayConfig"), e.get("queryScope"))
                  for e in entries}
        required = {
            ("ASCENDING", None, "COLLECTION"),
            ("DESCENDING", None, "COLLECTION"),
            (None, "CONTAINS", "COLLECTION"),
            ("ASCENDING", None, "COLLECTION_GROUP"),
        }
        missing = required - by_key
        extra = by_key - required
        if missing or extra:
            _die(RC.POST_DIFF, "post_new_override_wrong_shape",
                 "rooms.status entries "
                 f"missing={sorted(missing, key=str)!r} "
                 f"extra={sorted(extra, key=str)!r}")
    except SystemExit:
        raise
    except Exception as exc:
        _die(RC.POST_DIFF, "post_diff",
             f"{type(exc).__name__}: {exc}")

    # 9. Poll until READY. Post-diff confirmed the CG_ASC entry
    #    is present, so any MISSING observation must hard-stop.
    try:
        _poll_until_ready(
            args.gcloud, audit_dir,
            timeout_sec=args.poll_timeout_sec,
            interval_sec=args.poll_interval_sec,
            assert_present=True,
        )
    except TimeoutError as exc:
        _die(RC.POLL, "poll_timeout", str(exc))
    except SystemExit:
        raise
    except Exception as exc:
        _die(RC.POLL, "poll_error", f"{type(exc).__name__}: {exc}")

    # 10. Final snapshot + FULL semantic diff (R3 finding 4). Not
    #     only rooms.status: prove every composite index and every
    #     unrelated field override is unchanged relative to pre,
    #     AND the intended CG_ASC entry is present and READY.
    try:
        _take_snapshot(args.gcloud, audit_dir / "final", label="final")
        # R4 finding 3: assert final rooms.status shape
        # (usesAncestorConfig=False, exactly the 4 required entries,
        # all READY, nested fieldPath="status").
        _validate_final_rooms_status(audit_dir / "final")
        pre_comp = _load_composite(audit_dir / "pre")
        final_comp = _load_composite(audit_dir / "final")
        pre_over = _load_field_overrides(audit_dir / "pre")
        final_over = _load_field_overrides(audit_dir / "final")

        # Composites: nothing added, nothing removed vs pre.
        final_comp_diff = _diff_composites(pre_comp, final_comp)
        if final_comp_diff["added"] or final_comp_diff["removed"]:
            _die(RC.FINAL_DIFF, "final_composites_drift",
                 f"composites changed relative to pre: {final_comp_diff!r}")

        # Field overrides: exactly one new — rooms.status; nothing removed.
        final_over_diff = _diff_field_overrides(pre_over, final_over)
        if final_over_diff["removed"]:
            _die(RC.FINAL_DIFF, "final_field_overrides_removed",
                 f"field overrides removed relative to pre: "
                 f"{final_over_diff['removed']!r}")
        if len(final_over_diff["added"]) != 1:
            _die(RC.FINAL_DIFF, "final_field_overrides_added",
                 f"expected exactly one new field override in final vs pre, "
                 f"got {len(final_over_diff['added'])}: "
                 f"{final_over_diff['added']!r}")

        final_rs = [
            o for o in final_over
            if o.get("collectionGroup") == "rooms"
            and o.get("fieldPath") == "status"
        ]
        if len(final_rs) != 1:
            _die(RC.FINAL_DIFF, "final_missing_rooms_status",
                 f"final snapshot has {len(final_rs)} rooms.status overrides")
        cg_asc = [
            e for e in final_rs[0]["indexes"]
            if e.get("order") == "ASCENDING"
            and e.get("queryScope") == "COLLECTION_GROUP"
        ]
        if not cg_asc:
            _die(RC.FINAL_DIFF, "final_cg_asc_missing",
                 "final rooms.status has no COLLECTION_GROUP ASCENDING entry")
        if cg_asc[0].get("state") != "READY":
            _die(RC.FINAL_DIFF, "final_cg_asc_not_ready",
                 f"final CG_ASC state={cg_asc[0].get('state')!r}, expected READY")
    except SystemExit:
        raise
    except Exception as exc:
        _die(RC.FINAL_DIFF, "final_diff", f"{type(exc).__name__}: {exc}")

    # SUCCESS
    _emit({
        "kind": "deploy_rooms_status_index",
        "command": "deploy_rooms_status_index.py",
        "script_version": SCRIPT_VERSION,
        "verified_at": _iso_now(),
        "rc": RC.OK,
        "outcome": "verified",
        "project": PROJECT_ID,
        "database": DATABASE_ID,
        "pr42_sha": args.pr42_sha,
        "firebase_tools_version_pin": args.firebase_tools_version_pin,
    })
    return RC.OK


if __name__ == "__main__":  # pragma: no cover
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover
        _die(RC.INTERNAL, "internal", f"{type(exc).__name__}: {exc}")
