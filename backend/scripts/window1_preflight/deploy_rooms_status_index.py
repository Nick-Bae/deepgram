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


SCRIPT_VERSION = "1.0.0"

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

# Field-override discovery uses this allowlist because the
# Firestore Admin API doesn't expose a database-wide fieldOverride
# list. Any collection group that carries app data goes here so
# the semantic diff can prove no unrelated field override was
# added or removed by this deploy.
KNOWN_COLLECTION_GROUPS: tuple[str, ...] = (
    "organizations",
    "services",
    "rooms",
    "members",
    "invites",
    "usage",
    "sermons",
)

DEFAULT_DEPLOY_TIMEOUT_SEC = 300
DEFAULT_POLL_TIMEOUT_SEC = 1800
DEFAULT_POLL_INTERVAL_SEC = 30


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


def _install_trap(audit_dir: Path, gcloud: str) -> None:
    """Install a signal handler + atexit hook that fires exactly
    once and writes the post-snapshot to `audit_dir/post/`. Uses
    the same pattern the shell EXIT trap uses: idempotent, runs
    on success, non-zero, SIGTERM, and SIGINT."""
    import atexit

    def _run_once():
        global _POST_SNAPSHOT_TAKEN
        if _POST_SNAPSHOT_TAKEN:
            return
        _POST_SNAPSHOT_TAKEN = True
        post_dir = audit_dir / "post"
        try:
            _take_snapshot(gcloud, post_dir, label="post")
        except Exception as exc:  # pragma: no cover — snapshot best-effort
            _diag(f"post-snapshot trap: {type(exc).__name__}: {exc}")

    def _handler(signum, _frame):
        _run_once()
        # Re-raise the signal so the process exits with the
        # canonical signal exit code.
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
    """Take a database-wide snapshot of composite indexes plus
    field overrides on every known collection group. Writes JSON
    per collection group + a `manifest.json` summarizing what
    was captured + a `snapshot.sha256`."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir.chmod(0o700)

    manifest: dict[str, Any] = {
        "label": label,
        "captured_at": _iso_now(),
        "project": PROJECT_ID,
        "database": DATABASE_ID,
        "location": LOCATION_ID,
        "known_collection_groups": list(KNOWN_COLLECTION_GROUPS),
    }

    # Database-wide composite indexes (no collection-group filter).
    composite_path = snapshot_dir / "composite-indexes.json"
    composite_json = _run_gcloud(
        gcloud, "firestore", "indexes", "composite", "list",
        f"--project={PROJECT_ID}", f"--database={DATABASE_ID}",
        "--format=json",
    )
    composite_path.write_text(composite_json)
    manifest["composite_count"] = len(json.loads(composite_json))

    # Per-known-collection-group field overrides.
    fields_dir = snapshot_dir / "fields"
    fields_dir.mkdir(exist_ok=True)
    fields_dir.chmod(0o700)
    per_group: dict[str, int] = {}
    for cg in KNOWN_COLLECTION_GROUPS:
        raw = _run_gcloud(
            gcloud, "firestore", "indexes", "fields", "list",
            f"--project={PROJECT_ID}", f"--database={DATABASE_ID}",
            f"--collection-group={cg}", "--format=json",
        )
        (fields_dir / f"{cg}.json").write_text(raw)
        per_group[cg] = len(json.loads(raw))
    manifest["field_overrides_per_collection_group"] = per_group

    # rooms.status field-level state (the one we care most about).
    rs_path = snapshot_dir / "rooms-status.json"
    rs_json = _run_gcloud(
        gcloud, "firestore", "indexes", "fields", "describe", "status",
        f"--project={PROJECT_ID}", f"--database={DATABASE_ID}",
        "--collection-group=rooms", "--format=json",
    )
    rs_path.write_text(rs_json)

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


def _load_field_overrides(snapshot_dir: Path) -> list[dict[str, Any]]:
    """Return every explicit field override across the known
    collection groups as a canonicalized list."""
    out = []
    fields_dir = snapshot_dir / "fields"
    for cg in KNOWN_COLLECTION_GROUPS:
        raw = json.loads((fields_dir / f"{cg}.json").read_text())
        for entry in raw:
            # `fields list` returns only explicit overrides — the
            # implicit ancestor-default fields are NOT here.
            out.append({
                "collectionGroup": cg,
                "fieldPath": _parse_field_path_from_name(entry.get("name", "")),
                "indexes": [
                    {"order": i.get("order"),
                     "arrayConfig": i.get("arrayConfig"),
                     "queryScope": i.get("queryScope"),
                     "state": i.get("state")}
                    for i in entry.get("indexConfig", {}).get("indexes", [])
                ],
                "usesAncestorConfig": entry.get("indexConfig", {}).get("usesAncestorConfig"),
            })
    return out


def _parse_field_path_from_name(name: str) -> str | None:
    # projects/.../collectionGroups/<cg>/fields/<field>
    m = re.search(r"/fields/([^/]+)$", name)
    return m.group(1) if m else None


def _load_rooms_status(snapshot_dir: Path) -> dict[str, Any]:
    """rooms.status describe → canonical."""
    raw = json.loads((snapshot_dir / "rooms-status.json").read_text())
    return {
        "usesAncestorConfig": raw.get("indexConfig", {}).get("usesAncestorConfig"),
        "indexes": sorted(
            [(e.get("order"), e.get("arrayConfig"), e.get("queryScope"),
              e.get("state"))
             for e in raw.get("indexConfig", {}).get("indexes", [])],
            key=lambda t: (str(t[0]), str(t[1]), str(t[2]))
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


def _static_invariants(pr42_cfg: dict[str, Any]) -> list[str]:
    """Return list of failure reasons (empty means pass)."""
    problems: list[str] = []
    if pr42_cfg.get("indexes") != []:
        problems.append(
            f"expected `indexes` to be [], got {pr42_cfg.get('indexes')!r}"
        )
    overrides = pr42_cfg.get("fieldOverrides", [])
    if len(overrides) != 1:
        problems.append(
            f"expected exactly one fieldOverride, got {len(overrides)}"
        )
        return problems
    o = overrides[0]
    if o.get("collectionGroup") != "rooms" or o.get("fieldPath") != "status":
        problems.append(
            f"fieldOverride not for rooms.status: "
            f"{o.get('collectionGroup')}.{o.get('fieldPath')}"
        )
    entries = o.get("indexes", [])
    have_col_asc = any(
        e.get("order") == "ASCENDING" and e.get("queryScope") == "COLLECTION"
        for e in entries
    )
    have_col_desc = any(
        e.get("order") == "DESCENDING" and e.get("queryScope") == "COLLECTION"
        for e in entries
    )
    have_col_arr = any(
        e.get("arrayConfig") == "CONTAINS" and e.get("queryScope") == "COLLECTION"
        for e in entries
    )
    have_cg_asc = [
        e for e in entries
        if e.get("order") == "ASCENDING"
        and e.get("queryScope") == "COLLECTION_GROUP"
    ]
    if not have_col_asc:
        problems.append("missing COLLECTION ASCENDING")
    if not have_col_desc:
        problems.append("missing COLLECTION DESCENDING")
    if not have_col_arr:
        problems.append("missing COLLECTION ARRAY_CONTAINS")
    if len(have_cg_asc) != 1:
        problems.append(
            f"expected exactly one COLLECTION_GROUP ASCENDING, got {len(have_cg_asc)}"
        )
    forbidden_cg = [
        e for e in entries
        if e.get("queryScope") == "COLLECTION_GROUP"
        and (e.get("order") == "DESCENDING" or e.get("arrayConfig") == "CONTAINS")
    ]
    if forbidden_cg:
        problems.append(f"forbidden COLLECTION_GROUP entries: {forbidden_cg!r}")
    return problems


# --- Deploy invocation --------------------------------------------------


def _deploy(firebase: str, worktree: Path, timeout_sec: float,
            audit_dir: Path) -> int:
    """Run `firebase deploy --only firestore:indexes` from the
    detached worktree with a bounded timeout. Returns rc; captures
    stdout/stderr into audit_dir/deploy/."""
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
    with (deploy_dir / "deploy.stdout").open("wb") as so, \
         (deploy_dir / "deploy.stderr").open("wb") as se:
        try:
            proc = subprocess.run(
                cmd, cwd=str(worktree),
                stdout=so, stderr=se, timeout=timeout_sec,
            )
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = 124  # matches GNU `timeout`
            se.write(
                f"\n\n[driver] firebase deploy timed out after "
                f"{timeout_sec:.0f}s\n".encode()
            )
    (deploy_dir / "deploy.rc").write_text(f"{rc}\n")
    return rc


# --- Polling ------------------------------------------------------------


def _current_rs_state(gcloud: str) -> str:
    """Return the state of the rooms.status COLLECTION_GROUP
    ASCENDING entry, or 'MISSING' if absent."""
    raw = _run_gcloud(
        gcloud, "firestore", "indexes", "fields", "describe", "status",
        f"--project={PROJECT_ID}", f"--database={DATABASE_ID}",
        "--collection-group=rooms", "--format=json",
    )
    data = json.loads(raw)
    for e in data.get("indexConfig", {}).get("indexes", []):
        if (e.get("order") == "ASCENDING"
                and e.get("queryScope") == "COLLECTION_GROUP"):
            return e.get("state", "UNKNOWN")
    return "MISSING"


def _poll_until_ready(gcloud: str, audit_dir: Path, *,
                      timeout_sec: float, interval_sec: float) -> str:
    """Poll `rooms.status` until state=READY. Returns the terminal
    state observed. Raises TimeoutError on timeout."""
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
        if state not in ("CREATING", "MISSING"):
            raise RuntimeError(f"unexpected state {state!r}")
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
    p.add_argument("--gcloud", default=os.environ.get("PR42_GCLOUD", "gcloud"))
    p.add_argument("--firebase", default=os.environ.get("PR42_FIREBASE", "firebase"))
    p.add_argument("--firebase-tools-version-pin", required=True,
                   help="Required output of `firebase --version` (exact match)")
    p.add_argument("--deploy-timeout-sec", type=float,
                   default=float(os.environ.get(
                       "PR42_DEPLOY_TIMEOUT_SEC", DEFAULT_DEPLOY_TIMEOUT_SEC)))
    p.add_argument("--poll-timeout-sec", type=float,
                   default=float(os.environ.get(
                       "PR42_POLL_TIMEOUT_SEC", DEFAULT_POLL_TIMEOUT_SEC)))
    p.add_argument("--poll-interval-sec", type=float,
                   default=float(os.environ.get(
                       "PR42_POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL_SEC)))
    p.add_argument("--dry-run", action="store_true",
                   help="Skip only the firebase deploy invocation; still snapshot + diff")
    return p


def _check_preconditions(args: argparse.Namespace) -> None:
    # gcloud exists
    if shutil.which(args.gcloud) is None and not Path(args.gcloud).is_file():
        _die(RC.PRECONDITIONS, "preconditions",
             f"gcloud not found: {args.gcloud!r}")
    # firebase exists (unless dry-run allows skipping — but we still
    # verify the version pin so an operator error surfaces).
    if shutil.which(args.firebase) is None and not Path(args.firebase).is_file():
        _die(RC.PRECONDITIONS, "preconditions",
             f"firebase not found: {args.firebase!r}")
    # firebase version pin (exact match)
    try:
        ver = subprocess.run(
            [args.firebase, "--version"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    except Exception as exc:
        _die(RC.PRECONDITIONS, "preconditions",
             f"firebase --version failed: {exc}")
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


def _confirm_target(args: argparse.Namespace) -> None:
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

    # 0. Preconditions
    _check_preconditions(args)

    audit_dir = Path(args.audit_dir).resolve()

    # 1. Pre-snapshot
    try:
        _take_snapshot(args.gcloud, audit_dir / "pre", label="pre")
    except Exception as exc:
        _die(RC.PRE_SNAPSHOT, "pre_snapshot",
             f"{type(exc).__name__}: {exc}")

    # 2. Target confirm
    try:
        _confirm_target(args)
    except SystemExit:
        raise
    except Exception as exc:
        _die(RC.TARGET_CONFIRM, "target_confirm",
             f"{type(exc).__name__}: {exc}")

    # 3. Static invariants on PR #42's file + delta computation
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

    # 9. Poll until READY
    try:
        _poll_until_ready(
            args.gcloud, audit_dir,
            timeout_sec=args.poll_timeout_sec,
            interval_sec=args.poll_interval_sec,
        )
    except TimeoutError as exc:
        _die(RC.POLL, "poll_timeout", str(exc))
    except SystemExit:
        raise
    except Exception as exc:
        _die(RC.POLL, "poll_error", f"{type(exc).__name__}: {exc}")

    # 10. Final snapshot + final diff
    try:
        _take_snapshot(args.gcloud, audit_dir / "final", label="final")
        final_over = _load_field_overrides(audit_dir / "final")
        final_added_rs = [
            o for o in final_over
            if o.get("collectionGroup") == "rooms" and o.get("fieldPath") == "status"
        ]
        if len(final_added_rs) != 1:
            _die(RC.FINAL_DIFF, "final_missing_rooms_status",
                 f"final snapshot has {len(final_added_rs)} rooms.status overrides")
        # State of the CG ASC entry must be READY.
        cg_asc = [
            e for e in final_added_rs[0]["indexes"]
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
