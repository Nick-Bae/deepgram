"""Fixture-backed tests for `deploy_rooms_status_index.py`.

Each test builds an isolated worktree-and-audit environment,
seeds the fake `gcloud` + `firebase` shims with a scenario-
specific state file, invokes the driver as a subprocess, and
asserts on:

  - the driver's exit code (RC scheme documented in the driver);
  - the single JSON stdout line's `outcome` and `rc`;
  - the presence of snapshot files (proves the EXIT-trap fired
    on non-zero and signal-interrupted paths);
  - that no unrelated production-side effect could occur —
    the fake `firebase` shim NEVER touches real Firestore.

Scenarios covered (one test each unless noted):
  - success                     — clean deploy, CG_ASC READY
  - dry_run_success             — same but skips the deploy call
  - deploy_command_failure      — firebase exits non-zero;
                                  post-snapshot still fires
  - unexpected_deletion         — post-snapshot shows an unrelated
                                  composite missing
  - unexpected_addition         — post-snapshot shows an unrelated
                                  composite added
  - needs_repair                — poll observes NEEDS_REPAIR
  - missing                     — CG_ASC never appears; polling
                                  hits the (tiny) timeout
  - poll_timeout                — CG_ASC stays CREATING; polling
                                  hits the (tiny) timeout
  - preconditions_bad_version   — firebase --version mismatch
  - preconditions_dirty_worktree — worktree has uncommitted change
  - preconditions_wrong_sha     — worktree HEAD != pinned SHA
  - target_wrong_project        — .firebaserc default mismatch
  - static_invariants_fail      — PR42 firestore.indexes.json shape
                                  is wrong (missing CG_ASC entry)
  - sigint_mid_deploy           — Ctrl-C during deploy; trap fires
                                  and a post-snapshot is written

Fixtures use plain files on disk; no network, no real Firestore
touched. Subprocess-only so the driver runs exactly as an
operator would run it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import mkdtemp


_REPO = Path(__file__).resolve().parents[2]
_DRIVER = _REPO / "backend" / "scripts" / "window1_preflight" / "deploy_rooms_status_index.py"
_FIXTURES = _REPO / "backend" / "tests" / "deploy_index_fixtures"
_FAKE_GCLOUD = _FIXTURES / "fake_gcloud.py"
_FAKE_FIREBASE = _FIXTURES / "fake_firebase.py"


DEFAULT_FIREBASE_VERSION = "13.19.0"
PR42_HELPER_SHA_PLACEHOLDER = "0" * 40  # replaced per test with the real head


# --- Helpers to build a valid PR #42 worktree layout -------------------


def _write_valid_pr42_indexes(path: Path) -> None:
    """Write the exact PR #42 firestore.indexes.json shape the
    driver's static invariants require."""
    path.write_text(json.dumps({
        "indexes": [],
        "fieldOverrides": [
            {
                "collectionGroup": "rooms",
                "fieldPath": "status",
                "indexes": [
                    {"order": "ASCENDING",  "queryScope": "COLLECTION"},
                    {"order": "DESCENDING", "queryScope": "COLLECTION"},
                    {"arrayConfig": "CONTAINS", "queryScope": "COLLECTION"},
                    {"order": "ASCENDING",  "queryScope": "COLLECTION_GROUP"},
                ],
            }
        ],
    }, indent=2) + "\n")


def _write_firebase_json(path: Path) -> None:
    path.write_text(json.dumps({
        "firestore": {
            "database": "worship-translation",
            "location": "us-central1",
            "rules":   "firestore.rules",
            "indexes": "firestore.indexes.json",
        }
    }, indent=2) + "\n")


def _write_firebaserc(path: Path, project: str = "sturdy-dogfish-472313-k6") -> None:
    path.write_text(json.dumps({
        "projects": {"default": project},
    }, indent=2) + "\n")


def _init_git(worktree: Path, dirty: bool = False, extra_commit_after: bool = False):
    """Initialize a mini git repo at `worktree` and return its HEAD sha.
    If `dirty=True`, leave an uncommitted change. If
    `extra_commit_after=True`, make one more commit to force a
    HEAD mismatch."""
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"],
                   cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "t"],
                   cwd=worktree, check=True)
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "test seed"],
                   cwd=worktree, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if extra_commit_after:
        (worktree / "extra.txt").write_text("second commit\n")
        subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "second"],
                       cwd=worktree, check=True)
    if dirty:
        (worktree / "dirty.txt").write_text("uncommitted\n")
    return sha


def _default_composites() -> list[dict]:
    """Baseline composites for the fake gcloud — the R4 inventory
    showed production has none, so keep this list empty for the
    happy path. Tests that need an unrelated composite override
    this."""
    return []


def _default_ancestor_entry() -> dict:
    """The `__default__` ancestor sentinel entry that appears in
    every `firestore indexes fields list` response (with or without
    `--collection-group`)."""
    return {
        "name": (
            "projects/sturdy-dogfish-472313-k6/databases/worship-translation/"
            "collectionGroups/__default__/fields/*"
        ),
        "indexConfig": {
            "indexes": [
                {"fields": [{"fieldPath": "*", "order": "ASCENDING"}],
                 "queryScope": "COLLECTION", "state": "READY"},
                {"fields": [{"fieldPath": "*", "order": "DESCENDING"}],
                 "queryScope": "COLLECTION", "state": "READY"},
                {"fields": [{"fieldPath": "*", "arrayConfig": "CONTAINS"}],
                 "queryScope": "COLLECTION", "state": "READY"},
            ],
        },
    }


def _default_field_overrides() -> list:
    """R4 inventory showed production has zero explicit field
    overrides (only the __default__ ancestor). Tests may override
    this to inject unrelated overrides that would trigger a
    pre-deploy delta refusal, or the intended rooms.status entry
    if the scenario needs it pre-existing."""
    return []


def _default_database() -> dict:
    return {
        "name": "projects/sturdy-dogfish-472313-k6/databases/worship-translation",
        "type": "FIRESTORE_NATIVE",
        "locationId": "us-central1",
        "uid": "test-uid",
    }


def _make_scenario(env_dir: Path, *,
                   composites=None,
                   field_overrides=None,
                   ancestor_default_entry=None,
                   database=None,
                   deploy_rc=0,
                   deploy_side_effect="commit_success",
                   firebase_version=DEFAULT_FIREBASE_VERSION,
                   poll_state_sequence=None,
                   die=None,
                   extra=None) -> Path:
    """Write a state file for the fake CLIs (R3 shape). `extra`
    merges additional keys into the state dict (used by R4 fixture
    tests for `sabotage_mutation_delay` and similar)."""
    state = {
        "composites": composites if composites is not None else _default_composites(),
        "field_overrides": (
            field_overrides if field_overrides is not None
            else _default_field_overrides()
        ),
        "ancestor_default_entry": (
            ancestor_default_entry if ancestor_default_entry is not None
            else _default_ancestor_entry()
        ),
        "database": database if database is not None else _default_database(),
        "deploy_rc": deploy_rc,
        "deploy_side_effect": deploy_side_effect,
        "firebase_version": firebase_version,
        "poll_state_cursor": 0,
        "die": die,
    }
    if poll_state_sequence is not None:
        state["poll_state_sequence"] = poll_state_sequence
    if extra:
        state.update(extra)
    state_path = env_dir / "state.json"
    state_path.write_text(json.dumps(state, indent=2))
    return state_path


class _Sandbox:
    """One test's isolated environment: worktree + audit dir + state file.

    The reviewed driver is copied INTO the sandbox worktree at its
    real repo-relative path BEFORE the initial commit, so the
    driver's R3 finding-5 precondition (invoked-from-pinned-worktree
    + sha256 hash-check) is naturally satisfied. The
    `dirty` / `extra_commit` flags then operate on top of a
    committed state that already contains the driver."""

    def __init__(self, *, valid_worktree=True,
                 dirty=False, extra_commit=False,
                 firebaserc_project="sturdy-dogfish-472313-k6",
                 broken_pr42_config=False):
        self.root = Path(mkdtemp(prefix="pr42-test-"))
        self.worktree = self.root / "worktree"
        self.audit = self.root / "audit"
        self.audit.mkdir()
        os.chmod(self.audit, 0o700)
        self.worktree.mkdir()
        if valid_worktree:
            if broken_pr42_config:
                (self.worktree / "firestore.indexes.json").write_text(
                    json.dumps({
                        "indexes": [],
                        "fieldOverrides": [{
                            "collectionGroup": "rooms",
                            "fieldPath": "status",
                            "indexes": [
                                {"order": "DESCENDING", "queryScope": "COLLECTION_GROUP"},
                            ],
                        }],
                    }) + "\n"
                )
            else:
                _write_valid_pr42_indexes(self.worktree / "firestore.indexes.json")
            _write_firebase_json(self.worktree / "firebase.json")
            _write_firebaserc(self.worktree / ".firebaserc",
                              project=firebaserc_project)
            # R3 finding 5: driver must live inside the pinned
            # worktree. Copy it in BEFORE the initial commit so
            # HEAD contains it and the worktree is clean afterward.
            self.driver_in_worktree = (
                self.worktree / "backend" / "scripts" / "window1_preflight"
                / "deploy_rooms_status_index.py"
            )
            self.driver_in_worktree.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_DRIVER, self.driver_in_worktree)
        self.sha = _init_git(
            self.worktree, dirty=dirty, extra_commit_after=extra_commit,
        )
        self.pinned_sha = self.sha
        if extra_commit:
            # Pin the driver's expected SHA to the FIRST commit so
            # the current HEAD (which is now the second commit)
            # mismatches — proves R2/R3 precondition fires.
            first = subprocess.run(
                ["git", "rev-parse", "HEAD^"],
                cwd=self.worktree, capture_output=True, text=True, check=True,
            ).stdout.strip()
            self.pinned_sha = first

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_driver(self, state_path: Path, *,
                   deploy_timeout_sec=30, poll_timeout_sec=2,
                   poll_interval_sec=0.05,
                   firebase_version_pin=DEFAULT_FIREBASE_VERSION,
                   dry_run=False,
                   extra_env=None,
                   override_pr42_sha=None,
                   override_reviewer_approved_sha=None,
                   override_script_sha256=None,
                   omit_flags: set[str] | None = None):
        """Invoke the driver from inside the sandbox worktree.
        The driver was already copied in by `_Sandbox.__init__`."""
        env = os.environ.copy()
        env["PR42_FAKE_STATE"] = str(state_path)
        env["PR42_GCLOUD"] = str(_FAKE_GCLOUD)
        env["PR42_FIREBASE"] = str(_FAKE_FIREBASE)
        if extra_env:
            env.update(extra_env)

        driver_path = getattr(self, "driver_in_worktree", None) or _DRIVER

        # sha256 of the driver file the test will invoke.
        import hashlib
        h = hashlib.sha256()
        with open(driver_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        real_script_sha = h.hexdigest()

        omit = omit_flags or set()
        cmd = [
            sys.executable, str(driver_path),
            "--audit-dir", str(self.audit),
            "--worktree", str(self.worktree),
            "--pr42-sha", (override_pr42_sha or self.pinned_sha),
            "--reviewer-approved-sha",
            (override_reviewer_approved_sha or self.pinned_sha),
            "--script-sha256", (override_script_sha256 or real_script_sha),
            "--gcloud", str(_FAKE_GCLOUD),
            "--firebase", str(_FAKE_FIREBASE),
            "--firebase-tools-version-pin", firebase_version_pin,
        ]
        if "--deploy-timeout-sec" not in omit:
            cmd += ["--deploy-timeout-sec", str(deploy_timeout_sec)]
        if "--poll-timeout-sec" not in omit:
            cmd += ["--poll-timeout-sec", str(poll_timeout_sec)]
        if "--poll-interval-sec" not in omit:
            cmd += ["--poll-interval-sec", str(poll_interval_sec)]
        if dry_run:
            cmd.append("--dry-run")
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, env=env,
        )
        return proc


# R10: anchored regex that matches EXACTLY the driver's
# contradictory-state fail-closed marker line. The driver writes:
#   "<ISO> quiescence_not_established: pgid=<N> group_exists=<b>
#    scan_complete=<b> non_zombie_members=<repr>
#    zombie_members=<repr> scan_error_notes=<repr>\n"
# For the contradictory-state race (killpg says exists, /proc walk
# saw nothing at all), every list is empty and both booleans are
# True. The pattern below is fullmatch-anchored so ANY extra text
# on the line — or ANY extra line in the file — fails the
# assertion.
_CONTRADICTORY_STATE_MARKER_RE = re.compile(
    r"\A"
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z"
    r" quiescence_not_established:"
    r" pgid=\d+"
    r" group_exists=True"
    r" scan_complete=True"
    r" non_zombie_members=\[\]"
    r" zombie_members=\[\]"
    r" scan_error_notes=\[\]"
    r"\Z"
)


def _assert_trap_failure_is_only_contradictory_state(tc, tf_path):
    """R10: assert `trap-failure.txt` contains EXACTLY one non-
    empty line AND that line matches the anchored contradictory-
    state regex. Any additional line — e.g., a
    `take_snapshot(post)` failure appended after the drain —
    fails the assertion, as does any extra text on the same
    line. Callers use this on paths that must ONLY show the
    intended fail-closed record from the killpg/proc race."""
    body = tf_path.read_text()
    lines = [ln for ln in body.splitlines() if ln.strip()]
    tc.assertEqual(
        len(lines), 1,
        f"trap-failure.txt must contain exactly one non-empty "
        f"line; found {len(lines)}. content={body!r}",
    )
    line = lines[0]
    tc.assertRegex(
        line, _CONTRADICTORY_STATE_MARKER_RE,
        f"trap-failure.txt line must fullmatch the contradictory-"
        f"state marker EXACTLY (no extra text, no extra "
        f"failures). got line={line!r}",
    )


class DeployDriverFixtureTests(unittest.TestCase):
    """Fixture-backed scenarios listed in the module docstring."""

    def setUp(self):
        os.chmod(_FAKE_GCLOUD, 0o755)
        os.chmod(_FAKE_FIREBASE, 0o755)

    # ---------- happy paths ----------

    def test_success_full_pipeline(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(state)
            self.assertEqual(
                proc.returncode, 0,
                f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}",
            )
            lines = [l for l in proc.stdout.splitlines() if l.strip()]
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["rc"], 0)
            self.assertEqual(payload["outcome"], "verified")
            # Snapshots + poll log exist.
            for p in ("pre", "post", "final", "poll"):
                self.assertTrue((sb.audit / p).is_dir(), p)
            self.assertTrue((sb.audit / "deploy" / "deploy.rc").exists())
        finally:
            sb.cleanup()

    def test_dry_run_is_preparation_checkpoint(self):
        """Dry-run runs preconditions + pre-snapshot + target
        confirmation + PR #42 static invariants, then STOPS. It
        does NOT invoke firebase, take a post-snapshot, poll, or
        take a final snapshot — those phases only make sense when
        an apply actually happened. Reports outcome=dry_run_ready."""
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root, deploy_side_effect="commit_success")
            proc = sb.run_driver(state, dry_run=True)
            self.assertEqual(proc.returncode, 0,
                             f"stdout={proc.stdout}\nstderr={proc.stderr}")
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "dry_run_ready")
            self.assertTrue((sb.audit / "deploy" / "dry-run.txt").exists())
            self.assertTrue((sb.audit / "pre").is_dir())
            # NOT expected in dry-run:
            self.assertFalse((sb.audit / "post").is_dir(),
                             "dry-run must not take a post-snapshot")
            self.assertFalse((sb.audit / "poll").is_dir(),
                             "dry-run must not poll")
            self.assertFalse((sb.audit / "final").is_dir(),
                             "dry-run must not take a final snapshot")
        finally:
            sb.cleanup()

    # ---------- deploy-command failures ----------

    def test_deploy_command_failure_still_snapshots(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root, deploy_rc=1, deploy_side_effect="commit_no_op",
            )
            proc = sb.run_driver(state)
            self.assertEqual(proc.returncode, 6, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "deploy_nonzero")
            # Post-snapshot MUST exist even though deploy failed.
            self.assertTrue((sb.audit / "post").is_dir())
            self.assertTrue((sb.audit / "post" / "composite-indexes.json").exists())
            self.assertTrue((sb.audit / "post" / "manifest.json").exists())
        finally:
            sb.cleanup()

    # ---------- unexpected server-side change ----------

    def test_unexpected_deletion_of_unrelated_composite(self):
        """R3 upgrade: the pre-deploy delta refuses at rc=5 BEFORE
        firebase runs, because a pre-existing composite would be
        deleted by the deploy (PR #42's local file has
        `indexes: []`). This surfaces the risk earlier than R2's
        post-diff catch."""
        pre_composites = [{
            "collectionGroup": "services",
            "fields": [{"fieldPath": "activeRoomId", "order": "ASCENDING"},
                       {"fieldPath": "updatedAt", "order": "DESCENDING"}],
            "queryScope": "COLLECTION",
            "state": "READY",
        }]
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root,
                composites=pre_composites,
                deploy_side_effect="commit_partial_delete_unrelated",
            )
            proc = sb.run_driver(state)
            self.assertEqual(proc.returncode, 5, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "pre_deploy_delta")
            self.assertIn("composites", payload["reason"])
        finally:
            sb.cleanup()

    def test_unexpected_addition_of_unrelated_composite(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root, deploy_side_effect="commit_extra_addition",
            )
            proc = sb.run_driver(state)
            self.assertEqual(proc.returncode, 7, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "post_composites_diff")
        finally:
            sb.cleanup()

    # ---------- polling outcomes ----------

    def test_needs_repair_during_polling(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root, deploy_side_effect="commit_needs_repair",
            )
            proc = sb.run_driver(state, poll_timeout_sec=2)
            self.assertEqual(proc.returncode, 8, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "poll_error")
            self.assertIn("NEEDS_REPAIR", payload["reason"])
        finally:
            sb.cleanup()

    def test_missing_never_appears_caught_by_r4_post_shape_validator(self):
        """R4 finding 3 upgrade: `commit_leaves_missing` (override
        registered in fields list but rooms.status describe never
        shows the CG_ASC entry) is now caught at the POST snapshot
        by the exact-shape validator BEFORE polling begins.
        Reports rc=7 outcome=post_rooms_status_shape.
        `test_missing_after_ready_immediately_hard_stops` (R3
        R3ExtendedFixtureTests) covers the polling-time
        regression path (READY then MISSING → rc=8)."""
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root, deploy_side_effect="commit_leaves_missing",
            )
            proc = sb.run_driver(state, poll_timeout_sec=5,
                                 poll_interval_sec=0.01)
            self.assertEqual(proc.returncode, 7, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "post_rooms_status_shape")
            # R11 message wording: the unified validator reports
            # entry count + missing-set instead of R10's per-branch
            # "COLLECTION_GROUP ASC count=0" phrasing. Both fire on
            # the same hard-stop condition (CG_ASC entry absent).
            self.assertIn("missing entries", payload["reason"])
            self.assertIn("COLLECTION_GROUP", payload["reason"])
        finally:
            sb.cleanup()

    def test_polling_timeout_when_stuck_in_creating(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root, deploy_side_effect="commit_never_ready",
            )
            proc = sb.run_driver(state, poll_timeout_sec=1)
            self.assertEqual(proc.returncode, 8, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "poll_timeout")
            self.assertIn("CREATING", payload["reason"])
        finally:
            sb.cleanup()

    # ---------- preconditions ----------

    def test_bad_firebase_version_pin(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root, firebase_version="14.0.0")
            proc = sb.run_driver(state,
                                 firebase_version_pin="13.19.0")
            self.assertEqual(proc.returncode, 2, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "preconditions")
            self.assertIn("version mismatch", payload["reason"])
        finally:
            sb.cleanup()

    def test_dirty_worktree_rejected(self):
        sb = _Sandbox(dirty=True)
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(state)
            self.assertEqual(proc.returncode, 2, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "preconditions")
            self.assertIn("dirty", payload["reason"])
        finally:
            sb.cleanup()

    def test_worktree_head_mismatch_with_pinned_sha(self):
        sb = _Sandbox(extra_commit=True)
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(state)
            self.assertEqual(proc.returncode, 2, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "preconditions")
            self.assertIn("worktree HEAD", payload["reason"])
        finally:
            sb.cleanup()

    def test_audit_dir_not_empty_rejected(self):
        sb = _Sandbox()
        try:
            # Pollute the audit dir before invoking the driver.
            (sb.audit / "stray.txt").write_text("stray\n")
            state = _make_scenario(sb.root)
            proc = sb.run_driver(state)
            self.assertEqual(proc.returncode, 2, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "preconditions")
        finally:
            sb.cleanup()

    # ---------- target confirmation ----------

    def test_target_wrong_default_project(self):
        sb = _Sandbox(firebaserc_project="some-other-project")
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(state)
            self.assertEqual(proc.returncode, 4, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "target_confirm")
        finally:
            sb.cleanup()

    # ---------- static invariants ----------

    def test_static_invariants_reject_bad_pr42_config(self):
        sb = _Sandbox(broken_pr42_config=True)
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(state)
            self.assertEqual(proc.returncode, 5, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "static_invariants")
        finally:
            sb.cleanup()

    # ---------- signal-interruption / trap behavior ----------

    def test_sigint_mid_deploy_terminates_process_group_and_prevents_late_mutation(self):
        """R4 finding 1: the sabotage child stays in firebase's
        process group (no setsid) so a correct trap that kills
        the whole group reaps the child BEFORE its
        mutation-delay elapses. The test waits past the mutation
        window and asserts (a) the driver's post-snapshot fired,
        AND (b) `late_child_mutation` never appeared in state —
        proving the child was killed rather than surviving the
        driver's exit."""
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root, deploy_side_effect="sigint_mid_deploy",
                # Any positive value — the fake uses this as the
                # child's sleep before it would mutate.
                extra={"sabotage_mutation_delay": 2.0},
            )
            proc = sb.run_driver(state, poll_timeout_sec=1)
            # Driver exited non-zero (re-raised SIGINT).
            self.assertNotEqual(proc.returncode, 0,
                                f"stdout={proc.stdout}\nstderr={proc.stderr}")
            # Post-snapshot fired.
            self.assertTrue((sb.audit / "post").is_dir(),
                            "trap must still write post/ snapshot")
            self.assertTrue((sb.audit / "post" / "manifest.json").exists())
            # WAIT past the child's mutation window (2 s) plus a
            # buffer — if the trap failed to kill the child, the
            # child would write the marker during this wait.
            time.sleep(4.0)
            # Assert the marker is still absent: the trap killed
            # the child before it could mutate.
            final_state = json.loads(state.read_text())
            self.assertFalse(
                final_state.get("late_child_mutation", False),
                "sabotage child was NOT killed by the trap — the "
                "trap must SIGTERM firebase's process group AND "
                "wait() before snapshotting",
            )
            # R9 accepted the contradictory-state marker as the
            # intended fail-closed record from the killpg-vs-/proc
            # race. R10 tightens the assertion so a marker that
            # contains the expected substrings PLUS any extra
            # failure line (e.g., a take_snapshot(post) exception
            # recorded after the drain) is rejected. The exactness
            # check requires:
            #   - trap-failure.txt contains exactly ONE non-empty
            #     line;
            #   - that line fullmatches the anchored regex for
            #     the contradictory-state shape (ISO timestamp,
            #     quiescence_not_established, numeric pgid, all
            #     empty member/note lists);
            #   - any additional line or extra text on the same
            #     line makes the test fail.
            tf = sb.audit / "trap-failure.txt"
            if tf.exists():
                _assert_trap_failure_is_only_contradictory_state(
                    self, tf,
                )
        finally:
            sb.cleanup()


class R3ExtendedFixtureTests(unittest.TestCase):
    """R3-specific coverage — findings 1, 3, 5, 6, 7, 8, 9."""

    def setUp(self):
        os.chmod(_FAKE_GCLOUD, 0o755)
        os.chmod(_FAKE_FIREBASE, 0o755)

    # Finding 1 + 9: unknown collection-group override caught by
    # the DB-wide fields list.
    def test_pre_deploy_refuses_when_unknown_cg_has_explicit_override(self):
        """An override in an unknown/unmodeled collection group in
        pre-state MUST cause the pre-deploy delta to refuse: PR
        #42's local `fieldOverrides` doesn't declare it, so
        firebase deploy would DELETE it. The R2 hardcoded 7-group
        allowlist would have silently missed this."""
        unknown_override = {
            "name": (
                "projects/sturdy-dogfish-472313-k6/databases/worship-translation/"
                "collectionGroups/unknown_group/fields/some_field"
            ),
            "indexConfig": {"indexes": [
                {"fields": [{"fieldPath": "some_field", "order": "ASCENDING"}],
                 "queryScope": "COLLECTION_GROUP", "state": "READY"},
            ]},
        }
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root, field_overrides=[unknown_override])
            proc = sb.run_driver(state)
            self.assertEqual(proc.returncode, 5, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "pre_deploy_delta")
            self.assertIn("would_delete", payload["reason"])
        finally:
            sb.cleanup()

    # Finding 3: pre-deploy delta refuses when an unrelated override exists.
    def test_pre_deploy_refuses_when_unrelated_override_would_be_deleted(self):
        unrelated = {
            "name": (
                "projects/sturdy-dogfish-472313-k6/databases/worship-translation/"
                "collectionGroups/services/fields/activeRoomId"
            ),
            "indexConfig": {"indexes": [
                {"fields": [{"fieldPath": "activeRoomId", "order": "ASCENDING"}],
                 "queryScope": "COLLECTION_GROUP", "state": "READY"},
            ]},
        }
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root, field_overrides=[unrelated])
            proc = sb.run_driver(state)
            self.assertEqual(proc.returncode, 5, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "pre_deploy_delta")
        finally:
            sb.cleanup()

    # Finding 3: pre-deploy delta refuses when there's a pre-existing composite.
    def test_pre_deploy_refuses_when_remote_composite_would_be_deleted(self):
        composite = {
            "name": "projects/foo/databases/bar/collectionGroups/x/indexes/y",
            "collectionGroup": "x",
            "fields": [{"fieldPath": "a", "order": "ASCENDING"}],
            "queryScope": "COLLECTION",
            "state": "READY",
        }
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root, composites=[composite])
            proc = sb.run_driver(state)
            self.assertEqual(proc.returncode, 5, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "pre_deploy_delta")
            self.assertIn("composites", payload["reason"])
        finally:
            sb.cleanup()

    # Finding 3: dry-run runs the pre-deploy delta too.
    def test_dry_run_runs_pre_deploy_delta_and_refuses_bad_pre_state(self):
        unrelated = {
            "name": (
                "projects/sturdy-dogfish-472313-k6/databases/worship-translation/"
                "collectionGroups/x/fields/y"
            ),
            "indexConfig": {"indexes": []},
        }
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root, field_overrides=[unrelated])
            proc = sb.run_driver(state, dry_run=True)
            self.assertEqual(proc.returncode, 5, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "pre_deploy_delta")
        finally:
            sb.cleanup()

    # Finding 5: reviewer-approved-sha must equal --pr42-sha.
    def test_reviewer_approved_sha_mismatch_rejected(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(state,
                                 override_reviewer_approved_sha="0" * 40)
            self.assertEqual(proc.returncode, 2, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "preconditions")
            self.assertIn("reviewer-approved SHA", payload["reason"])
        finally:
            sb.cleanup()

    # Finding 5: script-sha256 mismatch rejected.
    def test_script_sha256_mismatch_rejected(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(state,
                                 override_script_sha256="deadbeef" * 8)
            self.assertEqual(proc.returncode, 2, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "preconditions")
            self.assertIn("script sha256 mismatch", payload["reason"])
        finally:
            sb.cleanup()

    # Finding 7: MISSING observed AFTER post-deploy = hard stop.
    def test_missing_after_ready_immediately_hard_stops(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root, deploy_side_effect="commit_regresses_after_ready",
            )
            proc = sb.run_driver(state, poll_timeout_sec=5,
                                 poll_interval_sec=0.01)
            self.assertEqual(proc.returncode, 8, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "poll_error")
            # R11 wording: the poll's unified shape validator
            # reports "missing entries" when the CG_ASC entry
            # disappears mid-poll (previously the poll said the
            # index "regressed"). Same behavior — hard stop rc=8
            # with poll_error outcome — different phrasing.
            self.assertIn("shape violation during poll", payload["reason"])
            self.assertIn("missing entries", payload["reason"])
        finally:
            sb.cleanup()

    # Finding 8: remote database mismatch (locationId wrong).
    def test_remote_database_wrong_location_rejected(self):
        wrong_db = _default_database()
        wrong_db["locationId"] = "europe-west4"
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root, database=wrong_db)
            proc = sb.run_driver(state)
            self.assertEqual(proc.returncode, 4, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "target_confirm")
            self.assertIn("locationId", payload["reason"])
        finally:
            sb.cleanup()

    # Finding 8: remote database mismatch (name wrong — wrong project).
    def test_remote_database_wrong_name_rejected(self):
        wrong_db = _default_database()
        wrong_db["name"] = "projects/other-project/databases/worship-translation"
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root, database=wrong_db)
            proc = sb.run_driver(state)
            self.assertEqual(proc.returncode, 4, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "target_confirm")
            self.assertIn("name", payload["reason"])
        finally:
            sb.cleanup()

    # Finding 8: positive-finite argparse validators.
    def test_zero_poll_timeout_rejected_by_argparse(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(state, poll_timeout_sec=0)
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "",
                             "argparse must not emit a JSON payload")
        finally:
            sb.cleanup()

    def test_nan_deploy_timeout_rejected_by_argparse(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(state, deploy_timeout_sec="nan")
            self.assertNotEqual(proc.returncode, 0)
        finally:
            sb.cleanup()

    # Finding 8: strict schema — unknown top-level key.
    def test_static_invariants_reject_unknown_top_level_key(self):
        sb = _Sandbox()
        try:
            (sb.worktree / "firestore.indexes.json").write_text(
                json.dumps({
                    "indexes": [],
                    "fieldOverrides": [{
                        "collectionGroup": "rooms",
                        "fieldPath": "status",
                        "indexes": [
                            {"order": "ASCENDING",  "queryScope": "COLLECTION"},
                            {"order": "DESCENDING", "queryScope": "COLLECTION"},
                            {"arrayConfig": "CONTAINS", "queryScope": "COLLECTION"},
                            {"order": "ASCENDING",  "queryScope": "COLLECTION_GROUP"},
                        ],
                    }],
                    "surprise": {"unexpected": "key"},
                }) + "\n"
            )
            subprocess.run(["git", "add", "-A"], cwd=sb.worktree, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "add stray key"],
                           cwd=sb.worktree, check=True)
            sb.pinned_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=sb.worktree,
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            state = _make_scenario(sb.root)
            proc = sb.run_driver(state)
            self.assertEqual(proc.returncode, 5, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "static_invariants")
            self.assertIn("unknown top-level", payload["reason"])
        finally:
            sb.cleanup()

    # Finding 8: strict schema — duplicate entries.
    def test_static_invariants_reject_duplicate_entries(self):
        sb = _Sandbox()
        try:
            (sb.worktree / "firestore.indexes.json").write_text(
                json.dumps({
                    "indexes": [],
                    "fieldOverrides": [{
                        "collectionGroup": "rooms",
                        "fieldPath": "status",
                        "indexes": [
                            {"order": "ASCENDING",  "queryScope": "COLLECTION"},
                            {"order": "ASCENDING",  "queryScope": "COLLECTION"},  # dupe
                            {"order": "DESCENDING", "queryScope": "COLLECTION"},
                            {"arrayConfig": "CONTAINS", "queryScope": "COLLECTION"},
                            {"order": "ASCENDING",  "queryScope": "COLLECTION_GROUP"},
                        ],
                    }],
                }) + "\n"
            )
            subprocess.run(["git", "add", "-A"], cwd=sb.worktree, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "add dupe"],
                           cwd=sb.worktree, check=True)
            sb.pinned_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=sb.worktree,
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            state = _make_scenario(sb.root)
            proc = sb.run_driver(state)
            self.assertEqual(proc.returncode, 5, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "static_invariants")
            self.assertIn("duplicate", payload["reason"])
        finally:
            sb.cleanup()


class R4RealGcloudFixtureParserTests(unittest.TestCase):
    """R4 finding 2: verify the driver's parsers correctly consume
    REAL, sanitized `gcloud 581.0.0` responses captured against
    production. Fixtures live at
    `backend/tests/deploy_index_fixtures/real_gcloud_samples/`."""

    @classmethod
    def setUpClass(cls):
        cls.samples = _FIXTURES / "real_gcloud_samples"

    def test_load_rooms_status_matches_real_shape(self):
        # Copy the real describe response into a snapshot-dir
        # layout and load through the driver's parser.
        import importlib.util as _iu
        spec = _iu.spec_from_file_location("_drv_r4", _DRIVER)
        drv = _iu.module_from_spec(spec)
        spec.loader.exec_module(drv)  # type: ignore[union-attr]

        tmp = Path(mkdtemp(prefix="r4-parser-"))
        try:
            (tmp / "rooms-status.json").write_text(
                (self.samples / "fields_describe_rooms_status.json").read_text()
            )
            rs = drv._load_rooms_status(tmp)
            self.assertIs(rs["usesAncestorConfig"], True)
            self.assertEqual(len(rs["indexes"]), 3,
                             f"real pre-state has three entries: {rs['indexes']!r}")
            fps = {e.get("fieldPath") for e in rs["indexes"]}
            self.assertEqual(fps, {"status"})
            scopes = {e.get("queryScope") for e in rs["indexes"]}
            self.assertEqual(scopes, {"COLLECTION"})
            states = {e.get("state") for e in rs["indexes"]}
            self.assertEqual(states, {"READY"})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_load_field_overrides_filters_ancestor_default(self):
        import importlib.util as _iu
        spec = _iu.spec_from_file_location("_drv_r4b", _DRIVER)
        drv = _iu.module_from_spec(spec)
        spec.loader.exec_module(drv)  # type: ignore[union-attr]

        tmp = Path(mkdtemp(prefix="r4-parser-b-"))
        try:
            (tmp / "fields-list.json").write_text(
                (self.samples / "fields_list_dbwide.json").read_text()
            )
            overrides = drv._load_field_overrides(tmp)
            # Real production: db-wide fields list returns only the
            # __default__ ancestor sentinel; no explicit overrides.
            # Our loader must filter the ancestor out.
            self.assertEqual(overrides, [],
                             f"expected zero explicit overrides in real "
                             f"production baseline, got {overrides!r}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_databases_describe_fixture_has_required_fields(self):
        # Not parsed by driver directly, but this ensures the
        # sanitized fixture is valid JSON with the fields the
        # driver's `_confirm_target` reads.
        d = json.loads(
            (self.samples / "databases_describe.json").read_text()
        )
        self.assertEqual(d["name"],
                         "projects/sturdy-dogfish-472313-k6/"
                         "databases/worship-translation")
        self.assertEqual(d["type"], "FIRESTORE_NATIVE")
        self.assertEqual(d["locationId"], "us-central1")


class R4ExactShapeValidatorTests(unittest.TestCase):
    """R4 finding 3: pre / post / final rooms.status shape."""

    def test_pre_snapshot_rejects_pre_state_with_extra_entry(self):
        """Simulate a pre-state where rooms.status has FOUR
        entries already — the driver's pre-baseline check must
        refuse because inherited baseline has exactly three."""
        # Build a fake ancestor entry with an unexpected extra
        # entry so the pre-snapshot passes db-wide checks but the
        # rooms.status describe would show 4 entries.
        # Simpler: craft a poll sequence whose FIRST call (pre
        # snapshot rooms.status describe) returns a 4-entry
        # inherited state via fake_gcloud override. Instead of
        # subverting fake_gcloud further, we test the validator
        # directly.
        import importlib.util as _iu
        spec = _iu.spec_from_file_location("_drv_r4c", _DRIVER)
        drv = _iu.module_from_spec(spec)
        spec.loader.exec_module(drv)  # type: ignore[union-attr]

        # Craft a snapshot dir with a rooms-status.json that has
        # ONE COLLECTION entry in CREATING (not the required
        # READY).
        tmp = Path(mkdtemp(prefix="r4-pre-shape-"))
        try:
            (tmp / "rooms-status.json").write_text(json.dumps({
                "indexConfig": {
                    "usesAncestorConfig": True,
                    "indexes": [
                        {"fields": [{"fieldPath": "status",
                                     "order": "ASCENDING"}],
                         "queryScope": "COLLECTION",
                         "state": "CREATING"},
                        {"fields": [{"fieldPath": "status",
                                     "order": "DESCENDING"}],
                         "queryScope": "COLLECTION",
                         "state": "READY"},
                        {"fields": [{"fieldPath": "status",
                                     "arrayConfig": "CONTAINS"}],
                         "queryScope": "COLLECTION",
                         "state": "READY"},
                    ],
                },
                "name": "…/rooms/fields/status",
            }) + "\n")
            with self.assertRaises(SystemExit) as ctx:
                drv._validate_pre_rooms_status(tmp)
            self.assertEqual(ctx.exception.code, drv.RC.PRE_SNAPSHOT)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_post_snapshot_rejects_uses_ancestor_true(self):
        import importlib.util as _iu
        spec = _iu.spec_from_file_location("_drv_r4d", _DRIVER)
        drv = _iu.module_from_spec(spec)
        spec.loader.exec_module(drv)  # type: ignore[union-attr]

        tmp = Path(mkdtemp(prefix="r4-post-shape-"))
        try:
            (tmp / "rooms-status.json").write_text(json.dumps({
                "indexConfig": {
                    # Explicit override should REPLACE the ancestor;
                    # if this is still True post-deploy, something
                    # is wrong.
                    "usesAncestorConfig": True,
                    "indexes": [
                        {"fields": [{"fieldPath": "status",
                                     "order": "ASCENDING"}],
                         "queryScope": "COLLECTION", "state": "READY"},
                        {"fields": [{"fieldPath": "status",
                                     "order": "DESCENDING"}],
                         "queryScope": "COLLECTION", "state": "READY"},
                        {"fields": [{"fieldPath": "status",
                                     "arrayConfig": "CONTAINS"}],
                         "queryScope": "COLLECTION", "state": "READY"},
                        {"fields": [{"fieldPath": "status",
                                     "order": "ASCENDING"}],
                         "queryScope": "COLLECTION_GROUP", "state": "CREATING"},
                    ],
                },
                "name": "…/rooms/fields/status",
            }) + "\n")
            with self.assertRaises(SystemExit) as ctx:
                drv._validate_post_rooms_status(tmp)
            self.assertEqual(ctx.exception.code, drv.RC.POST_DIFF)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_final_snapshot_rejects_creating_entry(self):
        import importlib.util as _iu
        spec = _iu.spec_from_file_location("_drv_r4e", _DRIVER)
        drv = _iu.module_from_spec(spec)
        spec.loader.exec_module(drv)  # type: ignore[union-attr]

        tmp = Path(mkdtemp(prefix="r4-final-shape-"))
        try:
            (tmp / "rooms-status.json").write_text(json.dumps({
                "indexConfig": {
                    "usesAncestorConfig": False,
                    "indexes": [
                        {"fields": [{"fieldPath": "status",
                                     "order": "ASCENDING"}],
                         "queryScope": "COLLECTION", "state": "READY"},
                        {"fields": [{"fieldPath": "status",
                                     "order": "DESCENDING"}],
                         "queryScope": "COLLECTION", "state": "READY"},
                        {"fields": [{"fieldPath": "status",
                                     "arrayConfig": "CONTAINS"}],
                         "queryScope": "COLLECTION", "state": "READY"},
                        {"fields": [{"fieldPath": "status",
                                     "order": "ASCENDING"}],
                         "queryScope": "COLLECTION_GROUP", "state": "CREATING"},
                    ],
                },
                "name": "…/rooms/fields/status",
            }) + "\n")
            with self.assertRaises(SystemExit) as ctx:
                drv._validate_final_rooms_status(tmp)
            self.assertEqual(ctx.exception.code, drv.RC.FINAL_DIFF)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class R4FinalDriftRegressionTests(unittest.TestCase):
    """R4 finding 4: unrelated drift introduced AFTER polling
    reaches READY (i.e., between post-diff and final snapshot)
    must be caught by the final semantic diff (rc=9)."""

    def setUp(self):
        os.chmod(_FAKE_GCLOUD, 0o755)
        os.chmod(_FAKE_FIREBASE, 0o755)

    def test_final_diff_catches_unrelated_composite_added_post_ready(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root,
                deploy_side_effect="commit_success_then_composite_add",
            )
            proc = sb.run_driver(state, poll_timeout_sec=5,
                                 poll_interval_sec=0.01)
            self.assertEqual(proc.returncode, 9, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "final_composites_drift")
        finally:
            sb.cleanup()

    def test_final_diff_catches_unrelated_override_added_post_ready(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root,
                deploy_side_effect="commit_success_then_unrelated_override_added",
            )
            proc = sb.run_driver(state, poll_timeout_sec=5,
                                 poll_interval_sec=0.01)
            self.assertEqual(proc.returncode, 9, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "final_field_overrides_added")
        finally:
            sb.cleanup()

    def test_final_diff_catches_target_entry_duplicated_post_ready(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root,
                deploy_side_effect="commit_success_then_target_duplicated",
            )
            proc = sb.run_driver(state, poll_timeout_sec=5,
                                 poll_interval_sec=0.01)
            self.assertEqual(proc.returncode, 9, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            # Either the final-diff catches an extra field-override
            # entry (because the duplication changes the override's
            # canonical shape) OR the final rooms.status shape
            # validator catches "post rooms.status has 5 entries".
            self.assertIn(payload["outcome"], (
                "final_field_overrides_added",
                "final_rooms_status_shape",
            ), f"outcome={payload['outcome']!r} reason={payload.get('reason')!r}")
        finally:
            sb.cleanup()


class R4EnvDefaultValidationTests(unittest.TestCase):
    """R4 finding 5: env-derived defaults must be validated too."""

    def setUp(self):
        os.chmod(_FAKE_GCLOUD, 0o755)
        os.chmod(_FAKE_FIREBASE, 0o755)

    def _run_with_env(self, env_var, env_value):
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            return sb.run_driver(
                state,
                extra_env={env_var: env_value},
            ), sb
        finally:
            pass  # cleanup deferred to caller

    def test_env_var_nan_rejected_by_post_parse_validation(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(
                state,
                extra_env={"PR42_POLL_TIMEOUT_SEC": "nan"},
                omit_flags={"--poll-timeout-sec"},
            )
            self.assertNotEqual(proc.returncode, 0)
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "usage")
            self.assertIn("--poll-timeout-sec", payload["reason"])
        finally:
            sb.cleanup()

    def test_env_var_zero_rejected_by_post_parse_validation(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(
                state,
                extra_env={"PR42_DEPLOY_TIMEOUT_SEC": "0"},
                omit_flags={"--deploy-timeout-sec"},
            )
            self.assertNotEqual(proc.returncode, 0)
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "usage")
        finally:
            sb.cleanup()

    def test_env_var_negative_rejected_by_post_parse_validation(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(
                state,
                extra_env={"PR42_POLL_INTERVAL_SEC": "-1"},
                omit_flags={"--poll-interval-sec"},
            )
            self.assertNotEqual(proc.returncode, 0)
        finally:
            sb.cleanup()

    def test_env_var_inf_rejected_by_post_parse_validation(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(
                state,
                extra_env={"PR42_DEPLOY_TIMEOUT_SEC": "inf"},
                omit_flags={"--deploy-timeout-sec"},
            )
            self.assertNotEqual(proc.returncode, 0)
        finally:
            sb.cleanup()


class R5EnvDefaultValidationTests(unittest.TestCase):
    """R5 finding 2: env-var resolution happens post-parse and
    routes every invalid value through rc=1 (usage). Prior R4
    code applied `float(os.environ.get(...))` at parser
    construction, which raised `ValueError` before argparse ran
    and let a non-numeric value bubble out as rc=99 (internal)."""

    def setUp(self):
        os.chmod(_FAKE_GCLOUD, 0o755)
        os.chmod(_FAKE_FIREBASE, 0o755)

    def test_env_var_non_numeric_deploy_timeout_is_usage_not_internal(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(
                state,
                extra_env={"PR42_DEPLOY_TIMEOUT_SEC": "abc"},
                omit_flags={"--deploy-timeout-sec"},
            )
            self.assertEqual(proc.returncode, 1,
                             f"expected rc=1 (usage), got {proc.returncode}. "
                             f"stdout={proc.stdout} stderr={proc.stderr}")
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["rc"], 1)
            self.assertEqual(payload["outcome"], "usage")
            # Reason must name the offending env var so the
            # operator knows exactly what to fix.
            self.assertIn("PR42_DEPLOY_TIMEOUT_SEC", payload["reason"])
            self.assertIn("abc", payload["reason"])
        finally:
            sb.cleanup()

    def test_env_var_non_numeric_poll_timeout_is_usage_not_internal(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(
                state,
                extra_env={"PR42_POLL_TIMEOUT_SEC": "not-a-number"},
                omit_flags={"--poll-timeout-sec"},
            )
            self.assertEqual(proc.returncode, 1)
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "usage")
            self.assertIn("PR42_POLL_TIMEOUT_SEC", payload["reason"])
        finally:
            sb.cleanup()

    def test_env_var_non_numeric_poll_interval_is_usage_not_internal(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(
                state,
                extra_env={"PR42_POLL_INTERVAL_SEC": "xyz"},
                omit_flags={"--poll-interval-sec"},
            )
            self.assertEqual(proc.returncode, 1)
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "usage")
            self.assertIn("PR42_POLL_INTERVAL_SEC", payload["reason"])
        finally:
            sb.cleanup()

    def test_env_var_empty_string_is_usage_not_internal(self):
        # An empty string is set but non-numeric; the driver must
        # not treat it as "unset" (which would silently use the
        # hard default), and must not raise ValueError.
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(
                state,
                extra_env={"PR42_POLL_TIMEOUT_SEC": ""},
                omit_flags={"--poll-timeout-sec"},
            )
            self.assertEqual(proc.returncode, 1)
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "usage")
        finally:
            sb.cleanup()


class R5ProcessGroupDrainTests(unittest.TestCase):
    """R5 finding 1: the deploy trap must SIGTERM the whole
    process group, wait for every member (not just the direct
    firebase child) to drain, and SIGKILL survivors. A grandchild
    that installs SIG_IGN for SIGTERM must not survive to mutate
    production after `Popen.wait()` on the direct child returns."""

    def setUp(self):
        os.chmod(_FAKE_GCLOUD, 0o755)
        os.chmod(_FAKE_FIREBASE, 0o755)

    def test_grandchild_ignoring_sigterm_is_killed_before_it_can_mutate(self):
        """The fixture forks a grandchild that:
          - stays in firebase's process group (no setsid),
          - installs SIG_IGN for SIGTERM,
          - would perform a state mutation after
            `sabotage_mutation_delay` seconds unless SIGKILLed.

        If the driver only waits on the direct Popen child, the
        grandchild survives SIGTERM and completes the mutation.
        The R5 fix walks the process group, escalates to SIGKILL
        on survivors, and only then takes the post-snapshot."""
        sb = _Sandbox()
        try:
            # Mutation delay > driver's SIGTERM grace (5 s) +
            # SIGKILL grace (2 s) so the grandchild can only die
            # by SIGKILL escalation, never by natural wake.
            state = _make_scenario(
                sb.root,
                deploy_side_effect="sigint_mid_deploy_grandchild_ignores_sigterm",
                extra={"sabotage_mutation_delay": 12.0},
            )
            proc = sb.run_driver(state, poll_timeout_sec=1)
            self.assertNotEqual(proc.returncode, 0,
                                f"stdout={proc.stdout}\nstderr={proc.stderr}")
            # Post-snapshot fired.
            self.assertTrue((sb.audit / "post").is_dir(),
                            "trap must still write post/ snapshot")
            # Wait past the grandchild's mutation window PLUS the
            # driver's SIGTERM grace: if the driver did not
            # escalate to SIGKILL on the whole PG, the grandchild
            # would wake and mutate state during this wait.
            time.sleep(15.0)
            final_state = json.loads(state.read_text())
            self.assertFalse(
                final_state.get("late_child_mutation", False),
                "R5 regression: grandchild that ignored SIGTERM "
                "was NOT killed by the trap. The driver must "
                "SIGTERM the process group, wait for every member "
                "to drain, and SIGKILL survivors before "
                "snapshotting — waiting on the direct Popen child "
                "alone is insufficient.",
            )
        finally:
            sb.cleanup()

    # R9: `test_pgid_alive_members_reads_proc_correctly` was
    # removed because it required the test-runner's PID to be
    # visible in `/proc` — the same environment dependency R8's
    # mocked-killpg test removed from `_pgid_group_exists`. The
    # portable coverage is now provided by
    # `R7ProcScanFailClosedTests.test_pgid_group_exists_mocked_
    # killpg_semantics` (mocked `os.killpg` behaviour) plus the
    # R8 deterministic-tuple assertions on
    # `_pgscan_is_quiescent()`. Together those exercise every
    # `/proc`-parse outcome and every quiescence-classification
    # branch without depending on the executor's process
    # namespace.


class R6DeployGroupQuiescenceTests(unittest.TestCase):
    """R6 finding 1: the deploy trap must retain the PGID
    captured at Popen (with `start_new_session=True`, PGID==PID
    so the value is known synchronously) and must run the PG
    drain on EVERY `_deploy` exit path — including normal rc=0.

    Two races that R5 did not cover:

      (a) The direct firebase child exits before the trap
          resolves the PGID via `os.getpgid(popen.pid)`. In R5,
          `getpgid` on a reaped PID raised `ProcessLookupError`
          and the driver returned quietly, leaving any same-PG
          descendant free to mutate production.

      (b) firebase returns rc=0 normally, but a grandchild in
          the same PG lingers. R5 only drained on the abnormal
          (signal / timeout) paths, so the rc=0 path never
          checked the group.

    Both races produce a corrupted final snapshot unless the
    driver drains on every exit path AND refuses to return
    rc=0 when quiescence cannot be established."""

    def setUp(self):
        os.chmod(_FAKE_GCLOUD, 0o755)
        os.chmod(_FAKE_FIREBASE, 0o755)

    def test_leader_exits_early_grandchild_ignoring_sigterm_prevented(self):
        """R6 finding 1a (tightened R7): fake_firebase forks a
        same-PG grandchild that installs SIG_IGN for SIGTERM and
        would mutate after ~12 s, then exits rc=0 immediately.

        The R6 driver captures the PGID at Popen and drains on
        the rc=0 exit path. SIGKILL clears the grandchild before
        its mutation fires. Because fake_firebase does NOT
        publish rooms.status in this scenario, the driver's
        post-shape validator sees the pre-state after the
        successful deploy and fails with rc=7 outcome=
        `post_rooms_status_shape`.

        Assertions (per reviewer R7 request):
          - grandchild-ready marker exists (proves the fixture
            actually spawned the SIG_IGN grandchild);
          - exact rc=7 with outcome=`post_rooms_status_shape`;
          - no late mutation in state."""
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root,
                deploy_side_effect="leader_exits_early_grandchild_ignores_sigterm",
                extra={"sabotage_mutation_delay": 12.0},
            )
            proc = sb.run_driver(state, poll_timeout_sec=1)
            # Grandchild readiness handshake: the fixture writes
            # this marker before beginning its sleep. Its
            # presence proves the SIG_IGN grandchild was really
            # spawned and running when the driver started
            # draining.
            ready = state.parent / "sabotage_gc_ready_leader_exit.txt"
            self.assertTrue(
                ready.exists(),
                "grandchild ready marker missing — fixture did "
                "not spawn the SIG_IGN grandchild, so this test "
                "isn't exercising the leader-exits-early race.",
            )
            self.assertEqual(
                proc.returncode, 7,
                f"driver must exit rc=7 (post-shape validator "
                f"catches unpublished rooms.status). "
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["rc"], 7)
            self.assertEqual(payload["outcome"],
                             "post_rooms_status_shape")
            # Wait past the mutation window; assert the SIG_IGN
            # grandchild never fired.
            time.sleep(15.0)
            final_state = json.loads(state.read_text())
            self.assertFalse(
                final_state.get("late_child_mutation", False),
                "R6/R7 regression: SIG_IGN grandchild survived "
                "after leader exited early. The PGID must be "
                "captured at Popen and the drain must SIGKILL "
                "the group on the rc=0 path.",
            )
        finally:
            sb.cleanup()

    def test_normal_zero_exit_grandchild_delayed_mutation_prevented(self):
        """R6 finding 1b (tightened R7): fake_firebase publishes
        rooms.status normally AND spawns a same-PG grandchild
        with SIG_IGN for SIGTERM that would mutate after ~12 s,
        then exits rc=0.

        The R6 rc=0-path drain SIGKILLs the grandchild before
        its timer fires. Because rooms.status IS published, the
        post-shape validator passes; because poll_state_sequence
        is `[READY]`, polling and final-diff both pass. The
        driver returns rc=0 outcome=`verified`.

        Assertions (per reviewer R7 request):
          - grandchild-ready marker exists (proves fixture ran);
          - exact rc=0 with outcome=`verified`;
          - `trap-failure.txt` MUST NOT exist (drain succeeded
            cleanly);
          - no late mutation in state."""
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root,
                deploy_side_effect="commit_success_grandchild_delayed_mutation",
                extra={"sabotage_mutation_delay": 12.0},
            )
            proc = sb.run_driver(state, poll_timeout_sec=1)
            ready = state.parent / "sabotage_gc_ready_success.txt"
            self.assertTrue(
                ready.exists(),
                "grandchild ready marker missing — fixture did "
                "not spawn the SIG_IGN grandchild.",
            )
            self.assertEqual(
                proc.returncode, 0,
                f"normal-success path with SIGKILL-cleared "
                f"grandchild must exit rc=0. "
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["rc"], 0)
            self.assertEqual(payload["outcome"], "verified")
            tf = sb.audit / "trap-failure.txt"
            self.assertFalse(
                tf.exists(),
                "trap-failure.txt must NOT exist on the clean "
                "rc=0 path — the drain SIGKILLed the grandchild "
                "and the /proc scan proved quiescence.",
            )
            time.sleep(15.0)
            final_state = json.loads(state.read_text())
            self.assertFalse(
                final_state.get("late_child_mutation", False),
                "R6/R7 regression: SIG_IGN grandchild survived "
                "a normal rc=0 deploy — the drain must SIGKILL "
                "the group before returning success.",
            )
        finally:
            sb.cleanup()

    def test_quiescence_failure_dies_rc6_with_trap_failure_marker(self):
        """R6 finding 1: when the drain CANNOT establish
        quiescence (a real SIGKILL-immune process is impossible
        under POSIX, so we simulate via env-var hook), the
        driver must (a) write `trap-failure.txt` naming the
        survivor and (b) exit rc=6.

        The `PR42_TESTING_FORCE_DRAIN_SURVIVOR=1` env var forces
        `_pgid_scan` to return a scan with a synthetic
        non-zombie survivor + `scan_complete=True`, so the
        quiescence check reports live members on every call."""
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(
                state,
                extra_env={
                    "PR42_TESTING_FORCE_DRAIN_SURVIVOR": "1",
                },
            )
            self.assertEqual(
                proc.returncode, 6,
                f"driver must exit rc=6 when quiescence cannot be "
                f"established. stdout={proc.stdout}\n"
                f"stderr={proc.stderr}"
            )
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["rc"], 6)
            self.assertEqual(payload["outcome"],
                             "deploy_group_quiescence_failed")
            tf = sb.audit / "trap-failure.txt"
            self.assertTrue(
                tf.exists(),
                "trap-failure.txt must be written on quiescence "
                "failure so the operator sees the survivor list.",
            )
            self.assertIn("quiescence_not_established", tf.read_text())
        finally:
            sb.cleanup()


class R7ProcScanFailClosedTests(unittest.TestCase):
    """R7 finding 1: an incomplete `/proc` scan while the process
    group still exists must NEVER be accepted as quiescence.
    Prior to R7, `_pgid_alive_members()` returned an empty list
    on `os.listdir("/proc")` OSError or on per-PID stat read
    failures — silently declaring the group drained. The reviewer
    reproduced a live grandchild mutating state on a Linux
    environment where /proc enumeration returned empty while
    `killpg(pgid, 0)` confirmed the group still existed.

    R7 replaces the fail-open helper with a structured `_PgScan`
    that carries an explicit `scan_complete` flag AND uses
    `killpg(pgid, 0)` as kernel-authoritative existence. An
    incomplete scan against a still-existing group triggers the
    fail-closed rc=6 branch.

    Test hook: `PR42_TESTING_FORCE_PROC_INCOMPLETE=1` forces
    `_pgid_scan` to return `group_exists=True, scan_complete=
    False, non_zombie_members=[], zombie_members=[]`. This
    exactly simulates the reviewer's reproduction environment."""

    def setUp(self):
        os.chmod(_FAKE_GCLOUD, 0o755)
        os.chmod(_FAKE_FIREBASE, 0o755)

    def test_incomplete_proc_scan_with_live_group_fails_closed(self):
        """When /proc enumeration cannot prove exhaustiveness AND
        the kernel confirms the group still exists, the driver
        must NOT declare quiescence. It must exit rc=6, record
        `trap-failure.txt` with the scan_complete=False detail,
        and never return rc=0."""
        sb = _Sandbox()
        try:
            state = _make_scenario(sb.root)
            proc = sb.run_driver(
                state,
                extra_env={
                    "PR42_TESTING_FORCE_PROC_INCOMPLETE": "1",
                },
            )
            self.assertEqual(
                proc.returncode, 6,
                f"an incomplete /proc scan against a still-"
                f"existing group must fail closed rc=6. "
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["rc"], 6)
            self.assertEqual(payload["outcome"],
                             "deploy_group_quiescence_failed")
            tf = sb.audit / "trap-failure.txt"
            self.assertTrue(
                tf.exists(),
                "trap-failure.txt must be written when the /proc "
                "scan is incomplete — silent fail-open was the R6 "
                "defect this test regresses against.",
            )
            body = tf.read_text()
            self.assertIn("quiescence_not_established", body)
            # The scan detail must record scan_complete=False so
            # the operator can distinguish this class of failure
            # from a real SIGKILL-immune process.
            self.assertIn("scan_complete=False", body)
        finally:
            sb.cleanup()

    def test_pgid_group_exists_mocked_killpg_semantics(self):
        """R8: environment-independent version of the R7
        `_pgid_group_exists` unit test. Mocks `os.killpg` so the
        assertion isolates the mapping from signal-zero syscall
        outcomes to the returned bool — no assumption about the
        executor's pid or session namespace.

        Contract under test:
          - `killpg(pgid, 0)` returns cleanly → group exists.
          - `ProcessLookupError` (ESRCH) → group absent.
          - `PermissionError` (EPERM) → fail-closed as existing.
          - Any other `OSError` → fail-closed as existing."""
        from unittest import mock
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dep_drv_r8_mock", str(_DRIVER),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fake_pgid = 12345
        # Normal return → group exists.
        with mock.patch.object(mod.os, "killpg", return_value=None):
            self.assertTrue(mod._pgid_group_exists(fake_pgid))
        # ProcessLookupError (ESRCH) → group absent.
        with mock.patch.object(mod.os, "killpg",
                               side_effect=ProcessLookupError()):
            self.assertFalse(mod._pgid_group_exists(fake_pgid))
        # PermissionError (EPERM) → fail-closed as existing.
        with mock.patch.object(mod.os, "killpg",
                               side_effect=PermissionError()):
            self.assertTrue(mod._pgid_group_exists(fake_pgid))
        # Generic OSError → fail-closed as existing.
        with mock.patch.object(mod.os, "killpg",
                               side_effect=OSError(1, "generic")):
            self.assertTrue(mod._pgid_group_exists(fake_pgid))

    def test_pgscan_is_quiescent_deterministic_assertions(self):
        """R8 finding: the quiescence decision function must
        distinguish 'kernel says group is gone' AND 'complete
        scan proves only zombies remain' from the contradictory
        state 'group exists, scan complete, but no members
        visible'. The reviewer independently reproduced that
        contradictory state against a real live grandchild and
        R7 falsely declared quiescence.

        These assertions are the R8 contract in tuple form."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dep_drv_r8_q", str(_DRIVER),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        Scan = mod._PgScan
        q = mod._pgscan_is_quiescent

        # Reviewer-required three cases:
        self.assertFalse(
            q(Scan(True, [], [], True, [])),
            "R8: contradictory state — group exists, complete "
            "scan, no live members, NO zombies. Must fail closed.",
        )
        self.assertTrue(
            q(Scan(True, [], [123], True, [])),
            "R8: quiescent — group exists but only a zombie "
            "member (unreapable, cannot mutate).",
        )
        self.assertTrue(
            q(Scan(False, [], [], True, [])),
            "R8: quiescent — ESRCH (kernel authority).",
        )
        # Additional coverage:
        self.assertFalse(
            q(Scan(True, [42], [], True, [])),
            "not quiescent: live non-zombie member visible.",
        )
        self.assertFalse(
            q(Scan(True, [], [], False, [])),
            "not quiescent: incomplete scan against live group.",
        )
        self.assertFalse(
            q(Scan(True, [42], [123], True, [])),
            "not quiescent: live member outweighs visible zombies.",
        )
        self.assertTrue(
            q(Scan(False, [42], [], True, [])),
            "quiescent: kernel says group gone even if /proc "
            "shows a stale live entry — the kernel is authority.",
        )
        self.assertFalse(
            q(Scan(True, [], [], True, ["listdir(/proc): PermissionError"])),
            "not quiescent: scan_error_notes signal an unreliable "
            "scan; but note that the decision is dominated by "
            "the contradiction (no members + group exists + no "
            "zombies) regardless.",
        )


class R10TrapFailureMarkerExactnessTests(unittest.TestCase):
    """R10: prove the SIGINT test's exactness assertion truly
    rejects extra content in `trap-failure.txt`. Under R9, the
    check used only `assertIn` on individual substrings, so a
    marker containing the expected fields PLUS an extra failure
    line (e.g., `take_snapshot(post)`) would still pass. The R10
    helper `_assert_trap_failure_is_only_contradictory_state`
    now uses (a) an exactly-one-line count check and (b) an
    anchored fullmatch regex. These tests construct synthetic
    trap-failure files and verify both rejection paths.

    No driver invocation — this is a pure unit test of the
    assertion helper's rejection behaviour."""

    def setUp(self):
        self._tmp = Path(mkdtemp(prefix="r10-marker-"))

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_marker(self, body: str) -> Path:
        p = self._tmp / "trap-failure.txt"
        p.write_text(body)
        return p

    # A well-formed contradictory-state line the driver could
    # legitimately write.
    _GOOD_LINE = (
        "2026-09-26T14:00:00.000Z quiescence_not_established: "
        "pgid=12345 group_exists=True scan_complete=True "
        "non_zombie_members=[] zombie_members=[] "
        "scan_error_notes=[]"
    )

    def test_pattern_fullmatch_accepts_the_expected_shape(self):
        self.assertRegex(self._GOOD_LINE,
                         _CONTRADICTORY_STATE_MARKER_RE)

    def test_helper_accepts_marker_with_exactly_one_good_line(self):
        tf = self._write_marker(self._GOOD_LINE + "\n")
        _assert_trap_failure_is_only_contradictory_state(self, tf)

    def test_helper_rejects_two_lines_even_if_first_matches(self):
        """R10 reviewer's exact failure mode: a marker whose
        first line is the contradictory-state record and whose
        second line reports a separate `take_snapshot(post)`
        failure. Under R9 substring checks this would pass;
        under R10 the line-count assertion fails."""
        body = (
            self._GOOD_LINE + "\n"
            "2026-09-26T14:00:01.000Z take_snapshot(post): "
            "RuntimeError: gcloud CLI returned rc=1\n"
        )
        tf = self._write_marker(body)
        with self.assertRaises(AssertionError) as cm:
            _assert_trap_failure_is_only_contradictory_state(self, tf)
        self.assertIn("exactly one non-empty line", str(cm.exception))

    def test_helper_rejects_extra_text_appended_to_the_same_line(self):
        """R9 substring checks would pass an augmented single
        line because every `assertIn` substring is still
        present. Anchored fullmatch rejects it."""
        body = self._GOOD_LINE + " EXTRA_FAILURE_TEXT\n"
        tf = self._write_marker(body)
        with self.assertRaises(AssertionError) as cm:
            _assert_trap_failure_is_only_contradictory_state(self, tf)
        self.assertIn("fullmatch", str(cm.exception))

    def test_helper_rejects_zombie_present(self):
        """Fail-closed rule requires empty zombie_members — a
        marker with a visible zombie represents an OK state
        that the driver would have accepted as quiescent, so it
        should not appear in trap-failure.txt at all. If one
        did appear, the exactness check rejects it."""
        body = (
            "2026-09-26T14:00:00.000Z quiescence_not_established: "
            "pgid=12345 group_exists=True scan_complete=True "
            "non_zombie_members=[] zombie_members=[9999] "
            "scan_error_notes=[]\n"
        )
        tf = self._write_marker(body)
        with self.assertRaises(AssertionError):
            _assert_trap_failure_is_only_contradictory_state(self, tf)

    def test_helper_rejects_live_member_present(self):
        body = (
            "2026-09-26T14:00:00.000Z quiescence_not_established: "
            "pgid=12345 group_exists=True scan_complete=True "
            "non_zombie_members=[42] zombie_members=[] "
            "scan_error_notes=[]\n"
        )
        tf = self._write_marker(body)
        with self.assertRaises(AssertionError):
            _assert_trap_failure_is_only_contradictory_state(self, tf)

    def test_helper_rejects_scan_incomplete_marker(self):
        body = (
            "2026-09-26T14:00:00.000Z quiescence_not_established: "
            "pgid=12345 group_exists=True scan_complete=False "
            "non_zombie_members=[] zombie_members=[] "
            "scan_error_notes=[]\n"
        )
        tf = self._write_marker(body)
        with self.assertRaises(AssertionError):
            _assert_trap_failure_is_only_contradictory_state(self, tf)

    def test_helper_rejects_scan_error_note_present(self):
        body = (
            "2026-09-26T14:00:00.000Z quiescence_not_established: "
            "pgid=12345 group_exists=True scan_complete=True "
            "non_zombie_members=[] zombie_members=[] "
            "scan_error_notes=['listdir(/proc): PermissionError']\n"
        )
        tf = self._write_marker(body)
        with self.assertRaises(AssertionError):
            _assert_trap_failure_is_only_contradictory_state(self, tf)

    def test_helper_rejects_non_iso_timestamp(self):
        body = (
            "not-a-timestamp quiescence_not_established: "
            "pgid=12345 group_exists=True scan_complete=True "
            "non_zombie_members=[] zombie_members=[] "
            "scan_error_notes=[]\n"
        )
        tf = self._write_marker(body)
        with self.assertRaises(AssertionError):
            _assert_trap_failure_is_only_contradictory_state(self, tf)


class R11ShapeValidatorTests(unittest.TestCase):
    """R11: the unified rooms.status target-shape validator is the
    single source of truth for post/poll/final acceptance. The
    2026-09-26 production deploy exposed two R10 false-negatives
    (usesAncestorConfig omitted; inherited entries transiently
    CREATING); R11 accepts both real production shapes and rejects
    every other class of drift.

    These tests exercise `_validate_rooms_status_target_shape`
    with hand-built canonical dicts — the same shape both
    `_load_rooms_status()` and `_current_rs_shape()` produce."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dep_drv_r11_shape", str(_DRIVER),
        )
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def _entry(self, order, arr, scope, state, field="status"):
        return {"fieldPath": field, "order": order,
                "arrayConfig": arr, "queryScope": scope, "state": state}

    def _all_ready(self):
        return [
            self._entry("ASCENDING", None, "COLLECTION", "READY"),
            self._entry("DESCENDING", None, "COLLECTION", "READY"),
            self._entry(None, "CONTAINS", "COLLECTION", "READY"),
            self._entry("ASCENDING", None, "COLLECTION_GROUP", "READY"),
        ]

    def _all_creating(self):
        return [{**e, "state": "CREATING"} for e in self._all_ready()]

    def _mixed(self):
        e = self._all_ready()
        e[0]["state"] = "CREATING"
        e[3]["state"] = "CREATING"
        return e

    # ---- usesAncestorConfig acceptance rules ----

    def test_uac_absent_all_ready_accepted_post_and_final(self):
        rs = {"indexes": self._all_ready()}  # no usesAncestorConfig
        self.assertEqual(
            self.mod._validate_rooms_status_target_shape(
                rs, phase="post", allow_creating=True), [])
        self.assertEqual(
            self.mod._validate_rooms_status_target_shape(
                rs, phase="final", allow_creating=False), [])

    def test_uac_false_all_ready_accepted_post_and_final(self):
        rs = {"usesAncestorConfig": False, "indexes": self._all_ready()}
        self.assertEqual(
            self.mod._validate_rooms_status_target_shape(
                rs, phase="post", allow_creating=True), [])
        self.assertEqual(
            self.mod._validate_rooms_status_target_shape(
                rs, phase="final", allow_creating=False), [])

    def test_uac_true_rejected_post(self):
        rs = {"usesAncestorConfig": True, "indexes": self._all_ready()}
        problems = self.mod._validate_rooms_status_target_shape(
            rs, phase="post", allow_creating=True)
        self.assertTrue(any("usesAncestorConfig=True" in p for p in problems))

    def test_uac_true_rejected_final(self):
        rs = {"usesAncestorConfig": True, "indexes": self._all_ready()}
        problems = self.mod._validate_rooms_status_target_shape(
            rs, phase="final", allow_creating=False)
        self.assertTrue(any("usesAncestorConfig=True" in p for p in problems))

    def test_uac_non_bool_rejected(self):
        rs = {"usesAncestorConfig": "false", "indexes": self._all_ready()}
        problems = self.mod._validate_rooms_status_target_shape(
            rs, phase="post", allow_creating=True)
        self.assertTrue(any("usesAncestorConfig=" in p for p in problems))

    # ---- state-transition acceptance rules ----

    def test_all_creating_accepted_in_post(self):
        rs = {"indexes": self._all_creating()}
        self.assertEqual(
            self.mod._validate_rooms_status_target_shape(
                rs, phase="post", allow_creating=True), [])

    def test_all_creating_rejected_in_final(self):
        rs = {"indexes": self._all_creating()}
        problems = self.mod._validate_rooms_status_target_shape(
            rs, phase="final", allow_creating=False)
        self.assertEqual(len(problems), 4)
        for p in problems:
            self.assertIn("!= READY", p)

    def test_mixed_creating_and_ready_accepted_in_post(self):
        rs = {"indexes": self._mixed()}
        self.assertEqual(
            self.mod._validate_rooms_status_target_shape(
                rs, phase="post", allow_creating=True), [])

    def test_mixed_creating_and_ready_rejected_in_final(self):
        rs = {"indexes": self._mixed()}
        problems = self.mod._validate_rooms_status_target_shape(
            rs, phase="final", allow_creating=False)
        # exactly 2 CREATING entries → 2 !=READY problems
        creating_problems = [p for p in problems if "!= READY" in p]
        self.assertEqual(len(creating_problems), 2)

    # ---- error-class rejection rules ----

    def test_needs_repair_rejected_in_post(self):
        entries = self._all_ready()
        entries[3]["state"] = "NEEDS_REPAIR"
        rs = {"indexes": entries}
        problems = self.mod._validate_rooms_status_target_shape(
            rs, phase="post", allow_creating=True)
        self.assertTrue(any("NEEDS_REPAIR" in p for p in problems))

    def test_needs_repair_rejected_in_final(self):
        entries = self._all_ready()
        entries[3]["state"] = "NEEDS_REPAIR"
        rs = {"indexes": entries}
        problems = self.mod._validate_rooms_status_target_shape(
            rs, phase="final", allow_creating=False)
        self.assertTrue(any("NEEDS_REPAIR" in p for p in problems))

    def test_missing_entry_rejected(self):
        rs = {"indexes": self._all_ready()[:3]}  # drop CG_ASC
        problems = self.mod._validate_rooms_status_target_shape(
            rs, phase="post", allow_creating=True)
        self.assertTrue(any("missing entries" in p for p in problems))
        self.assertTrue(any("3 entries, expected 4" in p for p in problems))

    def test_duplicate_entry_rejected(self):
        entries = self._all_ready()
        entries.append(self._entry("ASCENDING", None, "COLLECTION", "READY"))
        # 5 entries with one duplicate — also count!=4, but that's fine
        rs = {"indexes": entries[:4] + entries[3:4]}
        # crafted: 4th is CG_ASC and we append CG_ASC → duplicate
        rs2 = {"indexes": self._all_ready() + [
            self._entry("ASCENDING", None, "COLLECTION_GROUP", "READY")]}
        problems = self.mod._validate_rooms_status_target_shape(
            rs2, phase="post", allow_creating=True)
        self.assertTrue(
            any("duplicate entry keys" in p for p in problems),
            f"expected duplicate-detection in problems={problems!r}"
        )

    def test_extra_fifth_entry_rejected(self):
        entries = self._all_ready()
        entries.append(self._entry("DESCENDING", None, "COLLECTION_GROUP",
                                   "READY"))
        rs = {"indexes": entries}
        problems = self.mod._validate_rooms_status_target_shape(
            rs, phase="post", allow_creating=True)
        self.assertTrue(any("unexpected entries" in p for p in problems))
        self.assertTrue(any("5 entries, expected 4" in p for p in problems))

    def test_unknown_state_rejected(self):
        entries = self._all_ready()
        entries[0]["state"] = "PENDING"
        rs = {"indexes": entries}
        problems = self.mod._validate_rooms_status_target_shape(
            rs, phase="post", allow_creating=True)
        self.assertTrue(any("state='PENDING' not in" in p for p in problems))

    def test_wrong_field_path_rejected(self):
        entries = self._all_ready()
        entries[0]["fieldPath"] = "not_status"
        rs = {"indexes": entries}
        problems = self.mod._validate_rooms_status_target_shape(
            rs, phase="post", allow_creating=True)
        self.assertTrue(any("fieldPath='not_status'" in p for p in problems))

    def test_wrong_scope_rejected(self):
        entries = self._all_ready()
        # Change COLLECTION_GROUP ASC to COLLECTION ASC → duplicate +
        # missing one CG entry
        entries[3]["queryScope"] = "COLLECTION"
        rs = {"indexes": entries}
        problems = self.mod._validate_rooms_status_target_shape(
            rs, phase="post", allow_creating=True)
        self.assertTrue(any("missing entries" in p for p in problems))

    def test_wrong_mode_rejected(self):
        # Change ARRAY_CONTAINS entry to an ordered ASC entry
        entries = self._all_ready()
        entries[2] = self._entry("ASCENDING", None, "COLLECTION", "READY")
        rs = {"indexes": entries}
        problems = self.mod._validate_rooms_status_target_shape(
            rs, phase="post", allow_creating=True)
        self.assertTrue(
            any("missing entries" in p for p in problems)
            or any("duplicate" in p for p in problems)
        )


class R11ProductionFixtureTests(unittest.TestCase):
    """R11: load the sanitized production-derived fixtures captured
    2026-09-26 and assert the R11 validators accept them exactly
    as the R10 rerun on production would have — proving that the
    R10 false-negative is closed and that R11 will decide correctly
    against real Firestore output shapes.

    Fixtures at `deploy_index_fixtures/real_gcloud_samples/`:
      - `fields_describe_rooms_status_post_creating.json` — the
        rooms.status shape immediately after `firebase deploy`
        returned rc=0: usesAncestorConfig ABSENT, four entries
        all in CREATING state.
      - `fields_describe_rooms_status_final_ready.json` — the same
        shape ~7 minutes later after Firestore finished building:
        usesAncestorConfig ABSENT, four entries all READY.
      - `fields_list_dbwide_post_creating.json` — the db-wide
        fields list at post time (default ancestor + rooms.status
        explicit override, entries CREATING).
      - `fields_list_dbwide_final_ready.json` — same, entries READY."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dep_drv_r11_prod", str(_DRIVER),
        )
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        cls.SAMPLES = _FIXTURES / "real_gcloud_samples"

    def _canonicalize(self, path: Path) -> dict:
        raw = json.loads(path.read_text())
        canonical = [
            self.mod._canonicalize_index_entry(e)
            for e in raw.get("indexConfig", {}).get("indexes", [])
        ]
        return {
            "usesAncestorConfig": raw.get("indexConfig", {}).get(
                "usesAncestorConfig"),
            "indexes": sorted(
                canonical,
                key=lambda c: (str(c.get("order") or ""),
                               str(c.get("arrayConfig") or ""),
                               str(c.get("queryScope") or "")),
            ),
        }

    def test_post_creating_fixture_accepted_by_post_validator(self):
        p = self.SAMPLES / "fields_describe_rooms_status_post_creating.json"
        rs = self._canonicalize(p)
        # usesAncestorConfig should be absent
        self.assertIsNone(rs["usesAncestorConfig"])
        # 4 entries, all state=CREATING
        self.assertEqual(len(rs["indexes"]), 4)
        self.assertEqual([e["state"] for e in rs["indexes"]],
                         ["CREATING"] * 4)
        # post validator accepts (allow_creating=True)
        problems = self.mod._validate_rooms_status_target_shape(
            rs, phase="post", allow_creating=True)
        self.assertEqual(problems, [])
        # final validator REJECTS this shape (it needs all READY)
        final_problems = self.mod._validate_rooms_status_target_shape(
            rs, phase="final", allow_creating=False)
        self.assertNotEqual(final_problems, [])

    def test_final_ready_fixture_accepted_by_both_validators(self):
        p = self.SAMPLES / "fields_describe_rooms_status_final_ready.json"
        rs = self._canonicalize(p)
        self.assertIsNone(rs["usesAncestorConfig"])
        self.assertEqual(len(rs["indexes"]), 4)
        self.assertEqual([e["state"] for e in rs["indexes"]],
                         ["READY"] * 4)
        # Both validators accept
        for phase, allow in (("post", True), ("final", False)):
            problems = self.mod._validate_rooms_status_target_shape(
                rs, phase=phase, allow_creating=allow)
            self.assertEqual(problems, [], f"phase={phase} problems={problems}")

    def test_dbwide_post_creating_fixture_has_rooms_status_override(self):
        """R11: the db-wide field-list fixture during post-CREATING
        must contain exactly one explicit override (rooms.status)
        plus the __default__ ancestor sentinel."""
        p = self.SAMPLES / "fields_list_dbwide_post_creating.json"
        data = json.loads(p.read_text())
        self.assertEqual(len(data), 2)
        explicit = [e for e in data
                    if "/__default__/fields/" not in e.get("name", "")]
        default = [e for e in data
                   if "/__default__/fields/" in e.get("name", "")]
        self.assertEqual(len(explicit), 1)
        self.assertEqual(len(default), 1)
        self.assertTrue(
            explicit[0].get("name", "").endswith(
                "/collectionGroups/rooms/fields/status"),
            f"unexpected explicit override: {explicit[0].get('name')!r}",
        )
        # Entry states in the fixture (post-CREATING) = all CREATING
        idxs = explicit[0].get("indexConfig", {}).get("indexes", [])
        self.assertEqual(len(idxs), 4)
        self.assertEqual([i.get("state") for i in idxs], ["CREATING"] * 4)

    def test_dbwide_final_ready_fixture_has_rooms_status_override(self):
        p = self.SAMPLES / "fields_list_dbwide_final_ready.json"
        data = json.loads(p.read_text())
        self.assertEqual(len(data), 2)
        explicit = [e for e in data
                    if "/__default__/fields/" not in e.get("name", "")]
        self.assertEqual(len(explicit), 1)
        idxs = explicit[0].get("indexConfig", {}).get("indexes", [])
        self.assertEqual(len(idxs), 4)
        self.assertEqual([i.get("state") for i in idxs], ["READY"] * 4)

    def test_neither_dbwide_fixture_contains_uid_or_etag(self):
        """R11: guard against accidentally committing the database
        `uid` or `etag` fields (from `databases describe`) in the
        db-wide field-list fixtures. The reviewer requires the
        minimal sanitized structural fixtures only."""
        for name in ("fields_list_dbwide_post_creating.json",
                     "fields_list_dbwide_final_ready.json",
                     "fields_describe_rooms_status_post_creating.json",
                     "fields_describe_rooms_status_final_ready.json"):
            text = (self.SAMPLES / name).read_text()
            self.assertNotIn('"uid"', text, f"{name} contains uid")
            self.assertNotIn('"etag"', text, f"{name} contains etag")


if __name__ == "__main__":
    unittest.main()
