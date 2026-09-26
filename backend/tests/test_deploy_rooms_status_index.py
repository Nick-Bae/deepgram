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


def _default_fields_by_group() -> dict[str, list]:
    """Baseline field overrides — R4 inventory showed production
    has none (all rooms.status entries come from usesAncestorConfig).
    Tests may override this."""
    return {
        "organizations": [],
        "services": [],
        "rooms": [],
        "members": [],
        "invites": [],
        "usage": [],
        "sermons": [],
    }


def _make_scenario(env_dir: Path, *,
                   composites=None,
                   fields_by_group=None,
                   deploy_rc=0,
                   deploy_side_effect="commit_success",
                   firebase_version=DEFAULT_FIREBASE_VERSION,
                   poll_state_sequence=None,
                   die=None) -> Path:
    """Write a state file for the fake CLIs and return its path."""
    state = {
        "composites": composites if composites is not None else _default_composites(),
        "fields_by_group": (
            fields_by_group if fields_by_group is not None
            else _default_fields_by_group()
        ),
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
    """One test's isolated environment: worktree + audit dir + state file."""

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
                # Wrong shape: only COLLECTION_GROUP DESCENDING, missing
                # all COLLECTION-scope entries.
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
        self.sha = _init_git(
            self.worktree, dirty=dirty, extra_commit_after=extra_commit,
        )
        self.pinned_sha = self.sha
        if extra_commit:
            # Pin the sha the driver expects to the FIRST commit, so the
            # current HEAD (which is now the second commit) mismatches.
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
                   extra_env=None):
        env = os.environ.copy()
        env["PR42_FAKE_STATE"] = str(state_path)
        env["PR42_GCLOUD"] = str(_FAKE_GCLOUD)
        env["PR42_FIREBASE"] = str(_FAKE_FIREBASE)
        if extra_env:
            env.update(extra_env)
        cmd = [
            sys.executable, str(_DRIVER),
            "--audit-dir", str(self.audit),
            "--worktree", str(self.worktree),
            "--pr42-sha", self.pinned_sha,
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
            self.assertEqual(proc.returncode, 7, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "post_composites_diff")
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

    def test_missing_never_appears_hits_timeout(self):
        sb = _Sandbox()
        try:
            state = _make_scenario(
                sb.root, deploy_side_effect="commit_leaves_missing",
            )
            proc = sb.run_driver(state, poll_timeout_sec=1)
            self.assertEqual(proc.returncode, 8, proc.stderr[:400])
            payload = json.loads(proc.stdout.splitlines()[0])
            self.assertEqual(payload["outcome"], "poll_timeout")
            self.assertIn("MISSING", payload["reason"])
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


if __name__ == "__main__":
    unittest.main()
