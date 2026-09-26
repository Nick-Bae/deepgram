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
                   die=None) -> Path:
    """Write a state file for the fake CLIs (R3 shape)."""
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
                   override_script_sha256=None):
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
            "--deploy-timeout-sec", str(deploy_timeout_sec),
            "--poll-timeout-sec", str(poll_timeout_sec),
            "--poll-interval-sec", str(poll_interval_sec),
        ]
        if dry_run:
            cmd.append("--dry-run")
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, env=env,
        )
        return proc


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

    def test_missing_never_appears_hard_stops_on_first_poll(self):
        """R3 finding 7 upgrade: because the post-deploy snapshot
        confirmed the CG_ASC entry exists (fake_firebase publishes
        the override before setting the poll sequence), a MISSING
        observation from polling is now an immediate hard-stop —
        NOT a wait-until-timeout. Rc=8 with outcome=poll_error."""
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root, deploy_side_effect="commit_leaves_missing",
            )
            proc = sb.run_driver(state, poll_timeout_sec=5,
                                 poll_interval_sec=0.01)
            self.assertEqual(proc.returncode, 8, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "poll_error")
            self.assertIn("regressed", payload["reason"])
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

    def test_sigint_mid_deploy_still_writes_post_snapshot(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root, deploy_side_effect="sigint_mid_deploy",
            )
            proc = sb.run_driver(state, poll_timeout_sec=1)
            # SIGINT causes the driver to re-raise SIGINT after the trap,
            # so the shell exit code is 128 + 2 = 130. Some CI runners
            # coerce it differently — accept any non-zero AND require
            # the post-snapshot to exist.
            self.assertNotEqual(proc.returncode, 0)
            self.assertTrue((sb.audit / "post").is_dir(),
                            "SIGINT trap must still write post/ snapshot")
            self.assertTrue((sb.audit / "post" / "manifest.json").exists())
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
            self.assertIn("regressed", payload["reason"])
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


if __name__ == "__main__":
    unittest.main()
