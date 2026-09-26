"""Window 1 preflight — tests for `roster_cross_check.py`.

Fixture-driven — no live Cloud Logging or Cloud Monitoring
call. The two fetch functions (`fetch_tick_events` and
`fetch_metric_samples`) are monkey-patched at the module level
so the rest of the pipeline (roster classification, cross-check,
exit-code roll-up) runs against controlled inputs.

Four suites:

  - `RosterCrossCheckAllowlistUnitTests` — argv-driven allowlist
    + FIRESTORE_EMULATOR_HOST env checks. Same shape as the
    count_live_rooms allowlist suite. No fetch is called at all
    for these; the check runs BEFORE any SDK import.

  - `RosterCrossCheckClassificationUnitTests` — direct tests
    against `build_tick_roster` and `cross_check`. Cover the
    per-instance status branches (clean / insufficient_ticks /
    stale_tick / non_zero_rooms) and the per-revision union
    status branches (match / mismatch / missing_metric /
    missing_ticks).

  - `RosterCrossCheckExitCodeSubprocessTests` — REAL subprocess
    launches of the CLI with the two fetch functions monkey-
    patched via a small shim entry script written to a temp
    dir. Covers every documented rc: 0, 2, 3, 4, 5, 6, 7, 8, 9.
    Ensures the runbook's decision table matches the CLI's
    actual exit codes under operator-shell conditions.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO / "backend" / "scripts" / "window1_preflight"
sys.path.insert(0, str(_SCRIPTS_DIR))

import common  # noqa: E402
import roster_cross_check as rcc  # noqa: E402


PRODUCTION_PROJECT = "sturdy-dogfish-472313-k6"
PRODUCTION_DATABASE = "worship-translation"
EMULATOR_PROJECT = "cleanup-track1-emulator"
EMULATOR_DATABASE = "(default)"


class _EnvContext:
    def __init__(self, **overrides):
        self._overrides = overrides
        self._original: dict[str, str | None] = {}

    def __enter__(self):
        for k, v in self._overrides.items():
            self._original[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_exc):
        for k, prev in self._original.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


def _run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = rcc._run(list(argv))
        except SystemExit as exc:
            rc = int(exc.code) if exc.code is not None else 0
    return rc, out.getvalue(), err.getvalue()


class RosterCrossCheckAllowlistUnitTests(unittest.TestCase):

    def test_wrong_project_returns_rc2(self):
        rc, out, _err = _run_cli(
            "--project", "some-other-project",
            "--database", PRODUCTION_DATABASE,
        )
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "roster_cross_check")
        self.assertIn("allowlist", payload["reason"])
        self.assertFalse(payload["all_instances_clean"])

    def test_wrong_database_returns_rc2(self):
        rc, out, _err = _run_cli(
            "--project", PRODUCTION_PROJECT,
            "--database", "wrong-db",
        )
        self.assertEqual(rc, 2)
        self.assertIn("allowlist", json.loads(out)["reason"])

    def test_production_target_refuses_when_emulator_env_set(self):
        with _EnvContext(FIRESTORE_EMULATOR_HOST="127.0.0.1:8085"):
            rc, out, _err = _run_cli(
                "--project", PRODUCTION_PROJECT,
                "--database", PRODUCTION_DATABASE,
            )
        self.assertEqual(rc, 2)
        self.assertIn("FIRESTORE_EMULATOR_HOST", json.loads(out)["reason"])

    def test_output_schema_present_on_refusal(self):
        rc, out, _err = _run_cli(
            "--project", "bogus", "--database", "bogus",
        )
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        for key in (
            "kind", "command", "verified_at", "project", "service_name",
            "region", "tick_window_seconds",
            "metric_freshness_max_age_seconds",
            "tick_roster", "cloud_run_metric", "roster_union",
            "all_instances_clean", "all_revisions_match",
            "metric_freshness_ok", "elapsed_seconds", "rc", "reason",
        ):
            self.assertIn(key, payload, f"missing schema field {key!r}")

    def test_bad_region_returns_rc2(self):
        rc, out, _err = _run_cli(
            "--project", PRODUCTION_PROJECT,
            "--database", PRODUCTION_DATABASE,
            "--region", "us-east4",
        )
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertIn("region", payload["reason"])


class RosterCrossCheckClassificationUnitTests(unittest.TestCase):

    def _mk_event(self, rev, inst, offset_sec, owned=0, now=None):
        now = now if now is not None else time.time()
        ts = now - offset_sec
        return {
            "revision_name": rev, "instance_id": inst,
            "timestamp_iso": f"<t-{int(offset_sec)}s>",
            "timestamp_epoch": ts, "owned_rooms": owned,
        }

    def test_clean_two_zero_ticks_span_ok_recent_youngest(self):
        now = 1000.0
        events = [
            self._mk_event("rev-a", "i-1", 10, 0, now=now),
            self._mk_event("rev-a", "i-1", 45, 0, now=now),
        ]
        roster = rcc.build_tick_roster(
            events, min_ticks=2, min_span=30, max_age=60, now_epoch=now,
        )
        self.assertEqual(len(roster), 1)
        self.assertEqual(roster[0]["status"], "clean")

    def test_insufficient_ticks_only_one_tick(self):
        now = 1000.0
        events = [self._mk_event("rev-a", "i-1", 5, 0, now=now)]
        roster = rcc.build_tick_roster(
            events, min_ticks=2, min_span=30, max_age=60, now_epoch=now,
        )
        self.assertEqual(roster[0]["status"], "insufficient_ticks")

    def test_insufficient_ticks_span_too_short(self):
        now = 1000.0
        events = [
            self._mk_event("rev-a", "i-1", 5, 0, now=now),
            self._mk_event("rev-a", "i-1", 15, 0, now=now),  # span = 10s < 30
        ]
        roster = rcc.build_tick_roster(
            events, min_ticks=2, min_span=30, max_age=60, now_epoch=now,
        )
        self.assertEqual(roster[0]["status"], "insufficient_ticks")

    def test_stale_tick_youngest_too_old(self):
        now = 1000.0
        events = [
            self._mk_event("rev-a", "i-1", 120, 0, now=now),  # 120s > 60
            self._mk_event("rev-a", "i-1", 160, 0, now=now),
        ]
        roster = rcc.build_tick_roster(
            events, min_ticks=2, min_span=30, max_age=60, now_epoch=now,
        )
        self.assertEqual(roster[0]["status"], "stale_tick")

    def test_non_zero_rooms_last_tick(self):
        now = 1000.0
        events = [
            self._mk_event("rev-a", "i-1", 5, owned=3, now=now),
            self._mk_event("rev-a", "i-1", 45, owned=0, now=now),
        ]
        roster = rcc.build_tick_roster(
            events, min_ticks=2, min_span=30, max_age=60, now_epoch=now,
        )
        self.assertEqual(roster[0]["status"], "non_zero_rooms")

    def test_non_zero_rooms_second_tick(self):
        now = 1000.0
        events = [
            self._mk_event("rev-a", "i-1", 5, owned=0, now=now),
            self._mk_event("rev-a", "i-1", 45, owned=2, now=now),
        ]
        roster = rcc.build_tick_roster(
            events, min_ticks=2, min_span=30, max_age=60, now_epoch=now,
        )
        self.assertEqual(roster[0]["status"], "non_zero_rooms")

    def _clean_ticks_for(self, rev, insts):
        return [
            {"revision_name": rev, "instance_id": inst,
             "tick_timestamps": [], "youngest_tick_age_seconds": 0,
             "span_seconds": 0, "owned_rooms_last_tick": 0,
             "status": "clean"}
            for inst in insts
        ]

    def _metric_entry(self, rev, count, sample_age_seconds=10):
        return {
            "revision_name": rev,
            "active_value": 0, "idle_value": count,
            "active_plus_idle": count,
            "aligned_sample_timestamp_iso": "",
            "aligned_sample_timestamp_epoch": 0,
            "sample_age_seconds": sample_age_seconds,
        }

    def test_cross_check_match(self):
        tick_roster = self._clean_ticks_for("rev-a", ["i-1", "i-2"])
        metric = [self._metric_entry("rev-a", 2)]
        union, all_clean, all_match, freshness = rcc.cross_check(
            tick_roster, metric, freshness_max_age=180.0,
        )
        self.assertTrue(all_clean)
        self.assertTrue(all_match)
        self.assertTrue(freshness)
        self.assertEqual(union[0]["status"], "match")

    def test_cross_check_mismatch(self):
        tick_roster = self._clean_ticks_for("rev-a", ["i-1"])
        metric = [self._metric_entry("rev-a", 3)]
        union, all_clean, all_match, freshness = rcc.cross_check(
            tick_roster, metric, freshness_max_age=180.0,
        )
        self.assertFalse(all_match)
        self.assertEqual(union[0]["status"], "mismatch")

    def test_cross_check_missing_metric(self):
        tick_roster = self._clean_ticks_for("rev-a", ["i-1"])
        union, all_clean, all_match, freshness = rcc.cross_check(
            tick_roster, [], freshness_max_age=180.0,
        )
        self.assertFalse(all_match)
        self.assertEqual(union[0]["status"], "missing_metric")

    def test_cross_check_missing_ticks(self):
        metric = [self._metric_entry("rev-a", 1)]
        union, all_clean, all_match, freshness = rcc.cross_check(
            [], metric, freshness_max_age=180.0,
        )
        self.assertFalse(all_match)
        self.assertEqual(union[0]["status"], "missing_ticks")

    def test_cross_check_stale_metric_flips_freshness(self):
        tick_roster = self._clean_ticks_for("rev-a", ["i-1"])
        metric = [self._metric_entry("rev-a", 1, sample_age_seconds=240)]
        union, all_clean, all_match, freshness = rcc.cross_check(
            tick_roster, metric, freshness_max_age=180.0,
        )
        self.assertFalse(freshness)

    def test_empty_roster_and_empty_metric_is_unresolved(self):
        """Reviewer's PR #41 round-2 blocker #1: empty/empty
        must NOT be treated as verified clean. Both a truly
        scaled-to-zero service AND a broken query returning
        nothing look identical from here — the helper cannot
        distinguish them alone. Fail closed."""
        union, all_clean, all_match, freshness = rcc.cross_check(
            [], [], freshness_max_age=180.0,
        )
        self.assertEqual(len(union), 1)
        self.assertEqual(union[0]["status"], "no_evidence")
        self.assertFalse(all_clean, "empty telemetry must not be clean")
        self.assertFalse(all_match, "empty telemetry must not match")


class RosterCrossCheckExitCodeSubprocessTests(unittest.TestCase):
    """Subprocess-level CLI tests for every exit code.

    Each test writes a tiny shim entry script to a temp dir. The
    shim imports `roster_cross_check`, monkey-patches the two
    fetch functions with a fixture the test controls, then calls
    `roster_cross_check._run(sys.argv[1:])`. This exercises the
    real CLI code (`_run` + `emit` + `die`) end-to-end and
    proves the exit code reaches the process boundary.

    Emulator env is set to the allowlist value so allowlist +
    env-mismatch checks pass on the emulator target."""

    def _run_shim(self, tick_events: list, metric_samples: list, *extra_args: str) -> tuple[int, dict, str]:
        tmpdir = Path(tempfile.mkdtemp(prefix="rcc-shim-"))
        try:
            fixture_path = tmpdir / "fixture.json"
            fixture_path.write_text(json.dumps({
                "tick_events": tick_events,
                "metric_samples": metric_samples,
            }))
            shim_path = tmpdir / "shim.py"
            shim_path.write_text(
                "import json, os, sys\n"
                f"sys.path.insert(0, {str(_SCRIPTS_DIR)!r})\n"
                "import roster_cross_check as rcc\n"
                f"fixture = json.loads({str(fixture_path)!r} and open({str(fixture_path)!r}).read())\n"
                "rcc.fetch_tick_events = lambda *a, **k: fixture['tick_events']\n"
                "rcc.fetch_metric_samples = lambda *a, **k: fixture['metric_samples']\n"
                "sys.exit(rcc._run(sys.argv[1:]))\n"
            )
            env = os.environ.copy()
            env["FIRESTORE_EMULATOR_HOST"] = env.get("FIRESTORE_EMULATOR_HOST", "127.0.0.1:0")
            cmd = [
                sys.executable, "-u", str(shim_path),
                "--project", EMULATOR_PROJECT,
                "--database", EMULATOR_DATABASE,
                *extra_args,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
            try:
                payload = json.loads(proc.stdout)
            except Exception:
                payload = {"stdout_raw": proc.stdout}
            return proc.returncode, payload, proc.stderr
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _clean_tick(self, rev, inst, now=None):
        now = now if now is not None else time.time()
        return [
            {"revision_name": rev, "instance_id": inst,
             "timestamp_iso": "t0", "timestamp_epoch": now - 10, "owned_rooms": 0},
            {"revision_name": rev, "instance_id": inst,
             "timestamp_iso": "t1", "timestamp_epoch": now - 45, "owned_rooms": 0},
        ]

    def _fresh_metric(self, rev, count):
        now = time.time()
        # Split active/idle arbitrarily; the aggregated total is what
        # cross_check consumes.
        active = count if count <= 1 else count // 2
        idle = count - active
        return {
            "revision_name": rev,
            "active_value": active, "idle_value": idle,
            "active_plus_idle": count,
            "aligned_sample_timestamp_iso": "s",
            "aligned_sample_timestamp_epoch": now - 30,
            "sample_age_seconds": 30,
        }

    _ERROR_RAISERS = {
        "malformed": (
            "def _raise(*a, **k):\n"
            "    raise ValueError('bogus payload: revision_name missing')\n"
        ),
        "permission": (
            "def _raise(*a, **k):\n"
            "    from google.api_core import exceptions as gax\n"
            "    raise gax.PermissionDenied('caller lacks logging.entries.list')\n"
        ),
        "unauthenticated": (
            "def _raise(*a, **k):\n"
            "    from google.api_core import exceptions as gax\n"
            "    raise gax.Unauthenticated('ADC not configured')\n"
        ),
        "default_credentials": (
            "def _raise(*a, **k):\n"
            "    from google.auth import exceptions as gauth\n"
            "    raise gauth.DefaultCredentialsError('test: ADC not found')\n"
        ),
        "timeout": (
            "def _raise(*a, **k):\n"
            "    raise TimeoutError('deadline expired during fetch')\n"
        ),
        "generic": (
            "def _raise(*a, **k):\n"
            "    raise RuntimeError('unexpected upstream failure')\n"
        ),
    }

    def _run_shim_error(
        self, kind: str, which: str, *extra_args: str,
    ) -> tuple[int, dict, str]:
        """Run the CLI in a subprocess where the named fetch
        (`which` = 'ticks' or 'metric') raises the named error
        kind. The other fetch returns a benign empty list so
        control reaches the target path."""
        raiser = self._ERROR_RAISERS[kind]
        tmpdir = Path(tempfile.mkdtemp(prefix="rcc-shim-err-"))
        try:
            shim_path = tmpdir / "shim.py"
            if which == "ticks":
                patches = (
                    "rcc.fetch_tick_events = _raise\n"
                    "rcc.fetch_metric_samples = lambda *a, **k: []\n"
                )
            elif which == "metric":
                patches = (
                    "rcc.fetch_tick_events = lambda *a, **k: []\n"
                    "rcc.fetch_metric_samples = _raise\n"
                )
            else:  # pragma: no cover
                raise AssertionError(f"unknown which={which!r}")
            shim_path.write_text(
                "import sys\n"
                f"sys.path.insert(0, {str(_SCRIPTS_DIR)!r})\n"
                "import roster_cross_check as rcc\n"
                + raiser
                + patches
                + "sys.exit(rcc._run(sys.argv[1:]))\n"
            )
            env = os.environ.copy()
            env["FIRESTORE_EMULATOR_HOST"] = env.get(
                "FIRESTORE_EMULATOR_HOST", "127.0.0.1:0",
            )
            cmd = [
                sys.executable, "-u", str(shim_path),
                "--project", EMULATOR_PROJECT,
                "--database", EMULATOR_DATABASE,
                *extra_args,
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60, env=env,
            )
            try:
                payload = json.loads(proc.stdout)
            except Exception:
                payload = {"stdout_raw": proc.stdout}
            return proc.returncode, payload, proc.stderr
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    # --- rc=0 verified -------------------------------------------------

    def test_rc0_verified_when_ticks_and_metric_agree(self):
        events = self._clean_tick("rev-a", "i-1")
        metric = [self._fresh_metric("rev-a", 1)]
        rc, payload, err = self._run_shim(events, metric)
        self.assertEqual(rc, 0, f"payload={payload!r} stderr={err!r}")
        self.assertTrue(payload["all_instances_clean"])
        self.assertTrue(payload["all_revisions_match"])
        self.assertTrue(payload["metric_freshness_ok"])

    def test_rc8_unresolved_when_no_instances_at_all(self):
        """Reviewer's PR #41 round-2 blocker #1: empty ticks +
        empty metric can mean (a) truly scaled to zero, OR (b)
        the query is broken — the helper cannot distinguish them.
        Fail closed with `no_evidence` / rc=8 so the operator
        confirms out-of-band before declaring the window ready."""
        rc, payload, err = self._run_shim([], [])
        self.assertEqual(rc, 8, f"payload={payload!r} stderr={err!r}")
        self.assertFalse(payload["all_instances_clean"])
        self.assertFalse(payload["all_revisions_match"])
        self.assertEqual(len(payload["roster_union"]), 1)
        self.assertEqual(payload["roster_union"][0]["status"], "no_evidence")

    # --- rc=4 incomplete ----------------------------------------------

    def test_rc4_incomplete_when_instance_has_only_one_tick(self):
        now = time.time()
        events = [{
            "revision_name": "rev-a", "instance_id": "i-1",
            "timestamp_iso": "t0", "timestamp_epoch": now - 10,
            "owned_rooms": 0,
        }]
        metric = [self._fresh_metric("rev-a", 1)]
        rc, payload, _err = self._run_shim(events, metric)
        self.assertEqual(rc, 4)
        self.assertIn("not clean", payload["reason"])

    def test_rc4_incomplete_when_owned_rooms_nonzero(self):
        now = time.time()
        events = [
            {"revision_name": "rev-a", "instance_id": "i-1",
             "timestamp_iso": "t0", "timestamp_epoch": now - 10,
             "owned_rooms": 2},
            {"revision_name": "rev-a", "instance_id": "i-1",
             "timestamp_iso": "t1", "timestamp_epoch": now - 45,
             "owned_rooms": 0},
        ]
        metric = [self._fresh_metric("rev-a", 1)]
        rc, payload, _err = self._run_shim(events, metric)
        self.assertEqual(rc, 4)

    # --- rc=5 stale ---------------------------------------------------

    def test_rc5_stale_when_metric_sample_too_old(self):
        events = self._clean_tick("rev-a", "i-1")
        # sample_age > freshness_max (default 180 s). Match the
        # refactored schema — no container_name label, aligned
        # timestamp fields — so cross_check accepts it and reads
        # sample_age_seconds directly.
        metric = [{
            "revision_name": "rev-a",
            "active_value": 0, "idle_value": 1,
            "active_plus_idle": 1,
            "aligned_sample_timestamp_iso": "old",
            "aligned_sample_timestamp_epoch": time.time() - 500,
            "sample_age_seconds": 500,
        }]
        rc, payload, _err = self._run_shim(events, metric)
        self.assertEqual(rc, 5)
        self.assertIn("older than", payload["reason"])

    # --- rc=8 unresolved mismatch -------------------------------------

    def test_rc8_unresolved_when_tick_count_neq_metric(self):
        events = self._clean_tick("rev-a", "i-1")
        metric = [self._fresh_metric("rev-a", 3)]  # metric says 3, ticks say 1
        rc, payload, _err = self._run_shim(events, metric)
        self.assertEqual(rc, 8)
        self.assertIn("tick count does not equal", payload["reason"])

    def test_rc8_unresolved_when_metric_missing_but_ticks_present(self):
        events = self._clean_tick("rev-a", "i-1")
        rc, payload, _err = self._run_shim(events, [])
        self.assertEqual(rc, 8)

    # --- rc=3 malformed upstream data --------------------------------

    def test_rc3_malformed_from_ticks(self):
        rc, payload, _err = self._run_shim_error("malformed", "ticks")
        self.assertEqual(rc, 3)
        self.assertIn("malformed", payload["reason"].lower())

    def test_rc3_malformed_from_metric(self):
        rc, payload, _err = self._run_shim_error("malformed", "metric")
        self.assertEqual(rc, 3)
        self.assertIn("malformed", payload["reason"].lower())

    # --- rc=6 permission / authentication ----------------------------

    def test_rc6_permission_denied_from_ticks(self):
        rc, payload, _err = self._run_shim_error("permission", "ticks")
        self.assertEqual(rc, 6)
        self.assertIn("permission denied", payload["reason"].lower())

    def test_rc6_permission_denied_from_metric(self):
        rc, payload, _err = self._run_shim_error("permission", "metric")
        self.assertEqual(rc, 6)
        self.assertIn("permission denied", payload["reason"].lower())

    def test_rc6_unauthenticated_from_ticks(self):
        """Reviewer's PR #41 blocker: rc=6 must cover
        Unauthenticated alongside PermissionDenied. ADC absent
        surfaces as Unauthenticated, not PermissionDenied — the
        operator sees the same 'permission-family' failure and
        the runbook branches identically."""
        rc, payload, _err = self._run_shim_error("unauthenticated", "ticks")
        self.assertEqual(rc, 6)

    def test_rc6_unauthenticated_from_metric(self):
        rc, payload, _err = self._run_shim_error("unauthenticated", "metric")
        self.assertEqual(rc, 6)

    def test_rc6_default_credentials_from_ticks(self):
        """Reviewer's PR #41 R3 blocker: DefaultCredentialsError
        (ADC missing at client construction) must classify as
        rc=6, not fall through to the generic Exception handler
        that would emit rc=9. Runbook branches on the family."""
        rc, payload, _err = self._run_shim_error("default_credentials", "ticks")
        self.assertEqual(rc, 6, f"payload={payload!r}")
        self.assertIn("credentials", payload["reason"].lower())

    def test_rc6_default_credentials_from_metric(self):
        rc, payload, _err = self._run_shim_error("default_credentials", "metric")
        self.assertEqual(rc, 6, f"payload={payload!r}")
        self.assertIn("credentials", payload["reason"].lower())

    # --- rc=7 timeout / deadline exceeded ----------------------------

    def test_rc7_timeout_from_ticks(self):
        rc, payload, _err = self._run_shim_error("timeout", "ticks")
        self.assertEqual(rc, 7)
        self.assertIn("deadline expired", payload["reason"].lower())

    def test_rc7_timeout_from_metric(self):
        rc, payload, _err = self._run_shim_error("timeout", "metric")
        self.assertEqual(rc, 7)

    # --- rc=9 upstream API failure (generic) -------------------------

    def test_rc9_generic_upstream_from_ticks(self):
        rc, payload, _err = self._run_shim_error("generic", "ticks")
        self.assertEqual(rc, 9)
        self.assertIn("RuntimeError", payload["reason"])

    def test_rc9_generic_upstream_from_metric(self):
        rc, payload, _err = self._run_shim_error("generic", "metric")
        self.assertEqual(rc, 9)

    # --- Exactly one JSON on stdout for every non-usage rc ------------

    def _assert_exactly_one_json_line(self, stdout: str) -> None:
        lines = [line for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"stdout should be one JSON line, got: {stdout!r}")
        json.loads(lines[0])  # must parse

    def test_stdout_exactly_one_json_line_on_every_non_usage_rc(self):
        """Reviewer's PR #41 blocker: 'exactly-one-JSON
        assertions on every non-usage result'. Argparse-driven
        rc=1 is exempt (argparse writes usage to stderr and
        exits before emit() runs). Every other rc — including
        the shim-injected error paths — must land exactly one
        JSON object on stdout so the operator's `jq` pipeline
        never trips."""
        # rc=0 (verified)
        events = self._clean_tick("rev-a", "i-1")
        metric = [self._fresh_metric("rev-a", 1)]
        self._assert_exactly_one_json_line(
            self._captured_stdout_for_shim(events, metric),
        )
        # rc=8 (empty/empty unresolved)
        self._assert_exactly_one_json_line(
            self._captured_stdout_for_shim([], []),
        )
        # rc=3, 6, 7, 9 — capture stdout from each error shim
        # and prove it is exactly one parseable JSON line, not
        # just that the process exited with the right code.
        # (The individual rc tests validate the exit code +
        # payload contents; this test proves the stdout contract.)
        for kind, expected_rc in (
            ("malformed", 3),
            ("permission", 6),
            ("unauthenticated", 6),
            ("default_credentials", 6),
            ("timeout", 7),
            ("generic", 9),
        ):
            for which in ("ticks", "metric"):
                rc, payload, _err = self._run_shim_error(kind, which)
                self.assertEqual(
                    rc, expected_rc,
                    f"kind={kind!r} which={which!r} payload={payload!r}",
                )
                # `_run_shim_error` sets payload to {"stdout_raw":
                # ...} when stdout was not parseable JSON. That
                # would violate the contract.
                self.assertNotIn(
                    "stdout_raw", payload,
                    f"kind={kind!r} which={which!r} did not emit valid JSON: "
                    f"{payload!r}",
                )

    def _captured_stdout_for_shim(self, events, metric):
        tmpdir = Path(tempfile.mkdtemp(prefix="rcc-shim-cap-"))
        try:
            fixture_path = tmpdir / "fixture.json"
            fixture_path.write_text(json.dumps({
                "tick_events": events, "metric_samples": metric,
            }))
            shim_path = tmpdir / "shim.py"
            shim_path.write_text(
                "import json, sys\n"
                f"sys.path.insert(0, {str(_SCRIPTS_DIR)!r})\n"
                "import roster_cross_check as rcc\n"
                f"fixture = json.loads(open({str(fixture_path)!r}).read())\n"
                "rcc.fetch_tick_events = lambda *a, **k: fixture['tick_events']\n"
                "rcc.fetch_metric_samples = lambda *a, **k: fixture['metric_samples']\n"
                "sys.exit(rcc._run(sys.argv[1:]))\n"
            )
            env = os.environ.copy()
            env["FIRESTORE_EMULATOR_HOST"] = "127.0.0.1:0"
            proc = subprocess.run(
                [sys.executable, "-u", str(shim_path),
                 "--project", EMULATOR_PROJECT,
                 "--database", EMULATOR_DATABASE],
                capture_output=True, text=True, timeout=30, env=env,
            )
            return proc.stdout
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    # --- Argparse positive-finite validators (rc=2 from argparse) ----

    def test_zero_deadline_rejected_by_argparse(self):
        """--deadline-sec 0 must be rejected. Argparse exits 2
        via its own error path (usage on stderr) — that is
        distinct from our ALLOWLIST_REFUSAL rc=2 because
        argparse writes nothing to stdout."""
        proc = subprocess.run(
            [sys.executable, "-u",
             str(_SCRIPTS_DIR / "roster_cross_check.py"),
             "--project", PRODUCTION_PROJECT,
             "--database", PRODUCTION_DATABASE,
             "--deadline-sec", "0"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "", "argparse must not emit a JSON payload")
        self.assertIn("positive", proc.stderr.lower())

    def test_negative_deadline_rejected_by_argparse(self):
        proc = subprocess.run(
            [sys.executable, "-u",
             str(_SCRIPTS_DIR / "roster_cross_check.py"),
             "--project", PRODUCTION_PROJECT,
             "--database", PRODUCTION_DATABASE,
             "--deadline-sec", "-1"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_nan_deadline_rejected_by_argparse(self):
        proc = subprocess.run(
            [sys.executable, "-u",
             str(_SCRIPTS_DIR / "roster_cross_check.py"),
             "--project", PRODUCTION_PROJECT,
             "--database", PRODUCTION_DATABASE,
             "--deadline-sec", "nan"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("finite", proc.stderr.lower())

    def test_inf_rpc_timeout_rejected_by_argparse(self):
        proc = subprocess.run(
            [sys.executable, "-u",
             str(_SCRIPTS_DIR / "roster_cross_check.py"),
             "--project", PRODUCTION_PROJECT,
             "--database", PRODUCTION_DATABASE,
             "--rpc-timeout-sec", "inf"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    # --- Schema always present ---------------------------------------

    def test_stdout_has_exactly_one_json_line_on_ok(self):
        events = self._clean_tick("rev-a", "i-1")
        metric = [self._fresh_metric("rev-a", 1)]
        tmpdir = Path(tempfile.mkdtemp(prefix="rcc-shim-"))
        try:
            fixture_path = tmpdir / "fixture.json"
            fixture_path.write_text(json.dumps({
                "tick_events": events, "metric_samples": metric,
            }))
            shim_path = tmpdir / "shim.py"
            shim_path.write_text(
                "import json, os, sys\n"
                f"sys.path.insert(0, {str(_SCRIPTS_DIR)!r})\n"
                "import roster_cross_check as rcc\n"
                f"fixture = json.loads(open({str(fixture_path)!r}).read())\n"
                "rcc.fetch_tick_events = lambda *a, **k: fixture['tick_events']\n"
                "rcc.fetch_metric_samples = lambda *a, **k: fixture['metric_samples']\n"
                "sys.exit(rcc._run(sys.argv[1:]))\n"
            )
            env = os.environ.copy()
            env["FIRESTORE_EMULATOR_HOST"] = "127.0.0.1:0"
            proc = subprocess.run(
                [sys.executable, "-u", str(shim_path),
                 "--project", EMULATOR_PROJECT,
                 "--database", EMULATOR_DATABASE],
                capture_output=True, text=True, timeout=30, env=env,
            )
            lines = [l for l in proc.stdout.splitlines() if l.strip()]
            self.assertEqual(len(lines), 1, f"stdout: {proc.stdout!r}")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class RosterCrossCheckMetricAlignmentTests(unittest.TestCase):
    """Direct tests for the alignment + filter logic inside
    `fetch_metric_samples`.

    Reviewer's PR #41 R3 blocker #2: round-2 tests monkey-
    patched the whole `fetch_metric_samples` function or fed
    already-aggregated dicts to `cross_check`, so the
    production-critical alignment code (1-s bucketing, newest
    common bucket, single-state malformed detection, region-
    pinned filter without container_name) had zero coverage.

    These tests execute the real helpers extracted for
    testability: `_aggregate_metric_series` (pure alignment
    logic over an iterable of fake TimeSeries) and
    `_build_metric_filter` (the query string).
    """

    def _pt(self, epoch: float, value: int):
        """R6-safe fixture: build a REAL `monitoring_v3.Point`.

        R5's SimpleNamespace fixture set both `int64_value` and
        `double_value` on the fake TypedValue. R6's strict
        `_extract_point_value` inspects the proto oneof via
        `WhichOneof('value')`, which is not available on
        SimpleNamespace. Real protos preserve the oneof correctly
        for `int64_value=0` (proto-plus emits the field into the
        oneof even at the default value)."""
        from google.cloud import monitoring_v3
        from google.protobuf.timestamp_pb2 import Timestamp
        end = Timestamp()
        end.FromDatetime(datetime.fromtimestamp(epoch, timezone.utc))
        return monitoring_v3.Point({
            "interval": {"end_time": end},
            "value": {"int64_value": int(value)},
        })

    def _ts(self, rev: str, state: str, points: list[tuple[float, int]]):
        from types import SimpleNamespace
        return SimpleNamespace(
            resource=SimpleNamespace(labels={"revision_name": rev}),
            metric=SimpleNamespace(labels={"state": state}),
            points=[self._pt(e, v) for e, v in points],
        )

    def _deadline(self):
        # Generous budget — these tests iterate a handful of
        # fake series in-memory; only clock skew would ever fire it.
        return common.Deadline(300.0)

    # --- Alignment logic --------------------------------------------

    def test_active_and_idle_at_same_timestamp_aligned(self):
        now = time.time()
        pages = [
            self._ts("rev-a", "active", [(now - 30, 2)]),
            self._ts("rev-a", "idle", [(now - 30, 1)]),
        ]
        out = rcc._aggregate_metric_series(
            pages, now_epoch=now, deadline=self._deadline(),
        )
        self.assertEqual(len(out), 1)
        entry = out[0]
        self.assertEqual(entry["revision_name"], "rev-a")
        self.assertEqual(entry["active_value"], 2)
        self.assertEqual(entry["idle_value"], 1)
        self.assertEqual(entry["active_plus_idle"], 3)
        self.assertAlmostEqual(entry["sample_age_seconds"], 30, delta=1.5)

    def test_newer_unmatched_point_is_ignored_in_favor_of_common(self):
        """A newer active point without a matching idle point at
        the same bucket must NOT be selected — code falls back to
        the newest bucket where BOTH states report. Otherwise
        the returned total would count active-only and drop the
        idle contribution entirely."""
        now = time.time()
        pages = [
            self._ts("rev-a", "active", [(now - 10, 5), (now - 60, 2)]),
            # Idle only reports at the older bucket.
            self._ts("rev-a", "idle", [(now - 60, 1)]),
        ]
        out = rcc._aggregate_metric_series(
            pages, now_epoch=now, deadline=self._deadline(),
        )
        self.assertEqual(len(out), 1)
        entry = out[0]
        # OLD active picked because it is the newest active bucket
        # that also has an idle partner.
        self.assertEqual(entry["active_value"], 2)
        self.assertEqual(entry["idle_value"], 1)
        self.assertEqual(entry["active_plus_idle"], 3)
        self.assertAlmostEqual(entry["sample_age_seconds"], 60, delta=1.5)

    def test_newest_of_multiple_common_buckets_is_selected(self):
        now = time.time()
        pages = [
            self._ts("rev-a", "active", [(now - 30, 4), (now - 60, 2)]),
            self._ts("rev-a", "idle", [(now - 30, 1), (now - 60, 3)]),
        ]
        out = rcc._aggregate_metric_series(
            pages, now_epoch=now, deadline=self._deadline(),
        )
        entry = out[0]
        self.assertEqual(entry["active_value"], 4)
        self.assertEqual(entry["idle_value"], 1)
        self.assertEqual(entry["active_plus_idle"], 5)
        self.assertAlmostEqual(entry["sample_age_seconds"], 30, delta=1.5)

    def test_single_state_present_raises_valueerror_for_rc3(self):
        """A revision that reports only one of the two states
        (`active` OR `idle`, not both) is malformed — the
        cross-check cannot compute a trustworthy total from a
        one-sided series. Raises ValueError, which the CLI
        classifies as rc=3 MALFORMED via its `except ValueError`
        handler on the fetch path."""
        now = time.time()
        pages = [
            self._ts("rev-a", "active", [(now - 30, 2)]),
            # No idle series at all.
        ]
        with self.assertRaises(ValueError) as ctx:
            rcc._aggregate_metric_series(
                pages, now_epoch=now, deadline=self._deadline(),
            )
        self.assertIn("cannot align", str(ctx.exception))
        self.assertIn("rev-a", str(ctx.exception))

    def test_freshness_computed_from_aligned_timestamp_not_newest_point(self):
        """When a revision has a very-recent active-only point
        and an older common bucket, sample_age must reflect the
        common bucket's age — otherwise a mixed count would ship
        with a misleadingly fresh stamp and the freshness gate
        would silently pass on stale data."""
        now = time.time()
        pages = [
            self._ts("rev-a", "active", [(now - 5, 9), (now - 200, 2)]),
            self._ts("rev-a", "idle", [(now - 200, 3)]),
        ]
        out = rcc._aggregate_metric_series(
            pages, now_epoch=now, deadline=self._deadline(),
        )
        entry = out[0]
        # Aligned bucket is the older common one.
        self.assertEqual(entry["active_value"], 2)
        self.assertEqual(entry["idle_value"], 3)
        # Age from that bucket, NOT from the fresher unmatched
        # 5-s-old active point.
        self.assertAlmostEqual(entry["sample_age_seconds"], 200, delta=1.5)

    def test_missing_revision_name_label_raises_valueerror(self):
        from types import SimpleNamespace
        from datetime import datetime, timezone
        bad = SimpleNamespace(
            resource=SimpleNamespace(labels={}),  # no revision_name
            metric=SimpleNamespace(labels={"state": "active"}),
            points=[SimpleNamespace(
                interval=SimpleNamespace(
                    end_time=datetime.now(timezone.utc),
                ),
                value=SimpleNamespace(int64_value=1, double_value=1.0),
            )],
        )
        with self.assertRaises(ValueError) as ctx:
            rcc._aggregate_metric_series(
                [bad], now_epoch=time.time(), deadline=self._deadline(),
            )
        self.assertIn("revision_name", str(ctx.exception))

    def test_unexpected_state_label_raises_valueerror(self):
        now = time.time()
        pages = [self._ts("rev-a", "unknown_state", [(now - 30, 1)])]
        with self.assertRaises(ValueError) as ctx:
            rcc._aggregate_metric_series(
                pages, now_epoch=now, deadline=self._deadline(),
            )
        self.assertIn("state", str(ctx.exception))

    def test_aggregation_covers_multiple_revisions_independently(self):
        now = time.time()
        pages = [
            self._ts("rev-a", "active", [(now - 30, 2)]),
            self._ts("rev-a", "idle", [(now - 30, 1)]),
            self._ts("rev-b", "active", [(now - 45, 5)]),
            self._ts("rev-b", "idle", [(now - 45, 0)]),
        ]
        out = rcc._aggregate_metric_series(
            pages, now_epoch=now, deadline=self._deadline(),
        )
        by_rev = {e["revision_name"]: e for e in out}
        self.assertEqual(by_rev["rev-a"]["active_plus_idle"], 3)
        self.assertEqual(by_rev["rev-b"]["active_plus_idle"], 5)

    # --- Filter string ----------------------------------------------

    def test_filter_has_required_labels_and_no_container_name(self):
        """The metric documents only `state`; a container_name
        filter returned zero series in production. Verify the
        query includes metric-type, resource-type, service,
        region, both state values — and does NOT include the
        removed container_name label anywhere."""
        f = rcc._build_metric_filter("worshiptranslate-backend", "us-central1")
        self.assertIn(
            'metric.type="run.googleapis.com/container/instance_count"', f,
        )
        self.assertIn('resource.type="cloud_run_revision"', f)
        self.assertIn(
            'resource.labels.service_name="worshiptranslate-backend"', f,
        )
        self.assertIn('resource.labels.location="us-central1"', f)
        self.assertIn('metric.labels.state="active"', f)
        self.assertIn('metric.labels.state="idle"', f)
        self.assertNotIn("container_name", f)

    def test_filter_pins_arbitrary_region(self):
        f = rcc._build_metric_filter("svc", "europe-west4")
        self.assertIn('resource.labels.location="europe-west4"', f)
        self.assertNotIn("us-central1", f)


class LoggingClientRealSignatureTests(unittest.TestCase):
    """Round-4 tests: exercise the actual installed
    `google-cloud-logging` and `google-cloud-monitoring` client
    signatures instead of monkey-patching all of
    `fetch_tick_events`. Also proves pagination retains the
    round-3 bounded timeout and `retry=None` on every page.

    A round-3 read-only production rehearsal caught that the
    old high-level `logging_v2.Client.list_entries(...)` on the
    installed v3.x package raised `TypeError` for the same
    `timeout=` / `retry=` kwargs the tests had proven safe when
    the whole function was mocked. Signature tests here would
    have flagged that shape mismatch pre-merge."""

    def test_list_log_entries_signature_accepts_our_kwargs(self):
        """The generated `LoggingServiceV2Client.list_log_entries`
        MUST accept `request=`, `timeout=`, `retry=`, and
        `metadata=`. Round-3 defect: the OLD wrapper client did
        not. Signature-drift on the installed version now trips
        this test instead of only the operator's live run."""
        import inspect
        from google.cloud.logging_v2.services.logging_service_v2 import (
            LoggingServiceV2Client,
        )
        sig = inspect.signature(LoggingServiceV2Client.list_log_entries)
        params = sig.parameters
        for kw in ("request", "timeout", "retry", "metadata"):
            self.assertIn(
                kw, params,
                f"installed google-cloud-logging {LoggingServiceV2Client.__module__} "
                f"list_log_entries missing kwarg {kw!r} — package upgrade broke "
                f"our call shape",
            )

    def test_list_time_series_signature_accepts_our_kwargs(self):
        """Sibling compat test for the Monitoring pager. The
        round-4 direction says do NOT change
        `fetch_metric_samples` unless a real test demonstrates a
        defect. This test guards the assumption that the current
        `list_time_series(request, timeout=, retry=)` shape stays
        valid on the installed version."""
        import inspect
        from google.cloud.monitoring_v3 import MetricServiceClient
        sig = inspect.signature(MetricServiceClient.list_time_series)
        params = sig.parameters
        for kw in ("request", "timeout", "retry", "metadata"):
            self.assertIn(
                kw, params,
                f"installed google-cloud-monitoring MetricServiceClient "
                f"list_time_series missing kwarg {kw!r} — package upgrade "
                f"broke our call shape",
            )

    def test_fetch_tick_events_pagination_retains_bounded_timeout_and_retry_none(self):
        """Gapic `ListLogEntriesPager` re-invokes the underlying
        `method` for each next-page fetch, passing back the same
        `retry`, `timeout`, and `metadata` it was constructed
        with. Prove that behavior with a real pager wrapped
        around a method that records EVERY call's kwargs — not a
        single-call assertion — so page 2, 3, … stay bounded.

        Round-3 tests could not have caught this: they replaced
        `fetch_tick_events` entirely, so no gapic pager was ever
        exercised."""
        from google.cloud.logging_v2.services.logging_service_v2 import (
            LoggingServiceV2Client,
        )
        from google.cloud.logging_v2.services.logging_service_v2.pagers import (
            ListLogEntriesPager,
        )
        from google.cloud.logging_v2.types import (
            ListLogEntriesRequest,
            ListLogEntriesResponse,
            LogEntry,
        )
        from google.protobuf import struct_pb2
        from google.protobuf.timestamp_pb2 import Timestamp

        # Build 3 in-memory pages, 2 entries each = 6 valid ticks.
        now_dt = datetime.now(timezone.utc)

        def _mk_entry(rev, inst, offset_sec):
            payload = struct_pb2.Struct()
            payload.update({
                "event": "reconciler_tick",
                "instance_id": inst,
                "owned_rooms": 0,
            })
            entry = LogEntry(
                resource={
                    "type": "cloud_run_revision",
                    "labels": {
                        "service_name": "worshiptranslate-backend",
                        "location": "us-central1",
                        "revision_name": rev,
                    },
                },
                json_payload=payload,
            )
            ts = Timestamp()
            ts.FromDatetime(now_dt - timedelta(seconds=offset_sec))
            entry.timestamp = ts
            return entry

        page_1 = ListLogEntriesResponse(
            entries=[_mk_entry("rev-a", "i-1", 10),
                     _mk_entry("rev-a", "i-1", 45)],
            next_page_token="tok1",
        )
        page_2 = ListLogEntriesResponse(
            entries=[_mk_entry("rev-a", "i-2", 12),
                     _mk_entry("rev-a", "i-2", 46)],
            next_page_token="tok2",
        )
        page_3 = ListLogEntriesResponse(
            entries=[_mk_entry("rev-b", "i-3", 8),
                     _mk_entry("rev-b", "i-3", 44)],
            next_page_token="",
        )
        pages_by_token = {"": page_1, "tok1": page_2, "tok2": page_3}

        # `method` is what the pager invokes for every page. Record
        # each call's kwargs so we can assert on ALL of them.
        calls: list[dict] = []

        def method(request, *, retry=None, timeout=None, metadata=()):
            calls.append({
                "page_token": getattr(request, "page_token", ""),
                "retry": retry,
                "timeout": timeout,
            })
            return pages_by_token[getattr(request, "page_token", "") or ""]

        # Patch the LoggingServiceV2Client constructor so
        # `fetch_tick_events` receives a client whose
        # `list_log_entries` builds a real pager around `method`.
        deadline_ceiling = 15.0

        def fake_list_log_entries(request, *, retry=None, timeout=None, metadata=()):
            # Record the initial call, then hand back a real pager
            # that reuses the same retry+timeout for every subsequent
            # page fetch.
            initial = method(request, retry=retry, timeout=timeout, metadata=metadata)
            return ListLogEntriesPager(
                method=method,
                request=request,
                response=initial,
                retry=retry,
                timeout=timeout,
                metadata=metadata,
            )

        class _FakeLoggingClient:
            def __init__(self):
                pass
            def list_log_entries(self, request, *, retry=None, timeout=None, metadata=()):
                return fake_list_log_entries(
                    request, retry=retry, timeout=timeout, metadata=metadata,
                )

        with patch.object(
            LoggingServiceV2Client, "__new__",
            lambda cls, *a, **k: _FakeLoggingClient(),
        ):
            events = rcc.fetch_tick_events(
                project=PRODUCTION_PROJECT,
                service_name="worshiptranslate-backend",
                region="us-central1",
                tick_window_sec=300,
                deadline=common.Deadline(60.0),
                rpc_timeout=deadline_ceiling,
            )

        self.assertEqual(len(events), 6, f"consumed all pages: {events!r}")
        # Same shape as production output — used later by cross_check.
        self.assertEqual(events[0]["revision_name"], "rev-a")
        self.assertEqual(events[0]["instance_id"], "i-1")
        self.assertEqual(events[0]["owned_rooms"], 0)

        # Round-4 assertion: EVERY page call (initial + subsequent)
        # carried retry=None AND a bounded timeout <= our ceiling.
        # The pager's construction records the initial call once
        # via fake_list_log_entries, then makes 2 additional calls
        # for pages 2 and 3.
        self.assertGreaterEqual(len(calls), 3,
                                f"expected ≥3 recorded page calls, got {len(calls)}")
        for i, call in enumerate(calls):
            self.assertIsNone(
                call["retry"],
                f"page {i} (token={call['page_token']!r}) lost retry=None: "
                f"{call['retry']!r}",
            )
            self.assertIsInstance(
                call["timeout"], (int, float),
                f"page {i} lost bounded timeout: {call['timeout']!r}",
            )
            self.assertGreater(call["timeout"], 0)
            self.assertLessEqual(
                call["timeout"], deadline_ceiling,
                f"page {i} exceeded rpc-timeout ceiling {deadline_ceiling}: "
                f"{call['timeout']!r}",
            )

    def test_fetch_tick_events_passes_expected_request_shape(self):
        """Verify the ListLogEntriesRequest built inside
        fetch_tick_events pins service, region, event filter, and
        uses the pinned page_size + resource_names."""
        from google.cloud.logging_v2.services.logging_service_v2 import (
            LoggingServiceV2Client,
        )
        from google.cloud.logging_v2.services.logging_service_v2.pagers import (
            ListLogEntriesPager,
        )
        from google.cloud.logging_v2.types import ListLogEntriesResponse

        captured: dict = {}
        empty_response = ListLogEntriesResponse(entries=[], next_page_token="")

        def _method(request, *, retry=None, timeout=None, metadata=()):
            return empty_response

        class _CapturingClient:
            def list_log_entries(self, request, *, retry=None, timeout=None, metadata=()):
                captured["request"] = request
                captured["retry"] = retry
                captured["timeout"] = timeout
                # Return a real (empty) pager so `for entry in pager`
                # in fetch_tick_events works without special-casing.
                return ListLogEntriesPager(
                    method=_method,
                    request=request,
                    response=empty_response,
                    retry=retry,
                    timeout=timeout,
                    metadata=metadata,
                )

        with patch.object(
            LoggingServiceV2Client, "__new__",
            lambda cls, *a, **k: _CapturingClient(),
        ):
            events = rcc.fetch_tick_events(
                project=PRODUCTION_PROJECT,
                service_name="worshiptranslate-backend",
                region="us-central1",
                tick_window_sec=300,
                deadline=common.Deadline(60.0),
                rpc_timeout=15.0,
            )
        # Empty pages -> empty tick roster; that is not an error
        # for fetch_tick_events itself.
        self.assertEqual(events, [])
        req = captured["request"]
        self.assertEqual(
            list(req.resource_names),
            [f"projects/{PRODUCTION_PROJECT}"],
        )
        self.assertIn("service_name=\"worshiptranslate-backend\"", req.filter)
        self.assertIn("location=\"us-central1\"", req.filter)
        self.assertIn("jsonPayload.event=\"reconciler_tick\"", req.filter)
        self.assertEqual(req.order_by, "timestamp desc")
        self.assertEqual(req.page_size, 1000)
        self.assertIsNone(captured["retry"])
        self.assertLessEqual(captured["timeout"], 15.0)
        self.assertGreater(captured["timeout"], 0)


class TickEntryExtractorTests(unittest.TestCase):
    """Direct tests for `_extract_tick_entry` — proto → dict
    conversion. Round-3 tests skipped this path entirely because
    fetch_tick_events was fully mocked."""

    def _mk_entry(self, *, rev="rev-a", inst="i-1", owned=0,
                  offset_sec=10.0, empty_json=False):
        from google.cloud.logging_v2.types import LogEntry
        from google.protobuf import struct_pb2
        from google.protobuf.timestamp_pb2 import Timestamp

        payload = struct_pb2.Struct()
        if not empty_json:
            payload.update({
                "event": "reconciler_tick",
                "instance_id": inst,
                "owned_rooms": owned,
            })
        entry = LogEntry(
            resource={
                "type": "cloud_run_revision",
                "labels": {
                    "service_name": "worshiptranslate-backend",
                    "location": "us-central1",
                    "revision_name": rev,
                },
            },
            json_payload=payload,
        )
        ts = Timestamp()
        ts.FromDatetime(datetime.now(timezone.utc) - timedelta(seconds=offset_sec))
        entry.timestamp = ts
        return entry

    def test_integral_float_owned_rooms_accepted(self):
        """`google.protobuf.Struct` stores numbers as float64. A
        legitimately zero `owned_rooms` therefore round-trips as
        0.0 after `MessageToDict`, not int 0. The extractor must
        accept integral floats."""
        entry = self._mk_entry(owned=0)
        out = rcc._extract_tick_entry(entry)
        self.assertEqual(out["owned_rooms"], 0)
        self.assertIsInstance(out["owned_rooms"], int)

    def test_non_integral_float_rejected(self):
        # Struct won't let us set a non-int owned_rooms via the
        # normal `update({...:0.5})` path, so construct manually.
        from google.cloud.logging_v2.types import LogEntry
        from google.protobuf import struct_pb2
        from google.protobuf.timestamp_pb2 import Timestamp
        payload = struct_pb2.Struct()
        payload.update({
            "event": "reconciler_tick",
            "instance_id": "i-1",
            "owned_rooms": 0.5,
        })
        entry = LogEntry(
            resource={"labels": {"revision_name": "rev-a"}},
            json_payload=payload,
        )
        ts = Timestamp()
        ts.FromDatetime(datetime.now(timezone.utc))
        entry.timestamp = ts
        with self.assertRaises(ValueError):
            rcc._extract_tick_entry(entry)

    def test_missing_json_payload_raises(self):
        entry = self._mk_entry(empty_json=True)
        with self.assertRaises(ValueError) as ctx:
            rcc._extract_tick_entry(entry)
        self.assertIn("json_payload", str(ctx.exception))

    def test_missing_revision_name_raises(self):
        from google.cloud.logging_v2.types import LogEntry
        from google.protobuf import struct_pb2
        from google.protobuf.timestamp_pb2 import Timestamp
        payload = struct_pb2.Struct()
        payload.update({"instance_id": "i-1", "owned_rooms": 0})
        entry = LogEntry(
            resource={"labels": {}},  # no revision_name
            json_payload=payload,
        )
        ts = Timestamp()
        ts.FromDatetime(datetime.now(timezone.utc))
        entry.timestamp = ts
        with self.assertRaises(ValueError) as ctx:
            rcc._extract_tick_entry(entry)
        self.assertIn("revision_name", str(ctx.exception))

    def test_missing_instance_id_raises(self):
        from google.cloud.logging_v2.types import LogEntry
        from google.protobuf import struct_pb2
        from google.protobuf.timestamp_pb2 import Timestamp
        payload = struct_pb2.Struct()
        payload.update({"owned_rooms": 0})
        entry = LogEntry(
            resource={"labels": {"revision_name": "rev-a"}},
            json_payload=payload,
        )
        ts = Timestamp()
        ts.FromDatetime(datetime.now(timezone.utc))
        entry.timestamp = ts
        with self.assertRaises(ValueError) as ctx:
            rcc._extract_tick_entry(entry)
        self.assertIn("instance_id", str(ctx.exception))

    def test_build_tick_filter_contains_service_region_event_ts(self):
        f = rcc._build_tick_filter("svc-x", "asia-northeast1", 300)
        self.assertIn('resource.type="cloud_run_revision"', f)
        self.assertIn('resource.labels.service_name="svc-x"', f)
        self.assertIn('resource.labels.location="asia-northeast1"', f)
        self.assertIn('jsonPayload.event="reconciler_tick"', f)
        self.assertIn('timestamp >=', f)


class RealMonitoringProtoAggregationTests(unittest.TestCase):
    """R5 blocker #1: `_aggregate_metric_series` handled proto map
    labels via `isinstance(labels, dict)`, which is False for the
    real `monitoring_v3.TimeSeries.resource.labels` (a proto
    MapField / MapComposite). Round-4 tests used
    `SimpleNamespace(labels={...})` fixtures with plain dicts, so
    they never exercised the real proto shape.

    This suite builds actual `monitoring_v3.TimeSeries` protos and
    proves the aggregator extracts revision_name, state, points,
    and zero values correctly."""

    def _mk_series(self, *, rev, state, points_iso_val, project="p"):
        """Real `monitoring_v3.TimeSeries` proto — resource labels
        via proto MapField, metric labels via proto MapField, and
        points as `Point(interval=TimeInterval, value=TypedValue)`."""
        from google.cloud import monitoring_v3
        from google.protobuf.timestamp_pb2 import Timestamp

        ts_points = []
        for epoch, value in points_iso_val:
            end = Timestamp()
            end.FromDatetime(datetime.fromtimestamp(epoch, timezone.utc))
            ts_points.append(monitoring_v3.Point({
                "interval": {"end_time": end},
                "value": {"int64_value": int(value)},
            }))
        return monitoring_v3.TimeSeries({
            "resource": {
                "type": "cloud_run_revision",
                "labels": {
                    "project_id": project,
                    "service_name": "worshiptranslate-backend",
                    "location": "us-central1",
                    "revision_name": rev,
                    "configuration_name": "worshiptranslate-backend",
                },
            },
            "metric": {
                "type": "run.googleapis.com/container/instance_count",
                "labels": {"state": state},
            },
            "points": ts_points,
        })

    def test_aggregate_over_real_protos_extracts_revision_state_points(self):
        now = time.time()
        pages = [
            self._mk_series(rev="rev-a", state="active",
                            points_iso_val=[(now - 30, 2)]),
            self._mk_series(rev="rev-a", state="idle",
                            points_iso_val=[(now - 30, 1)]),
        ]
        out = rcc._aggregate_metric_series(
            pages, now_epoch=now, deadline=common.Deadline(300.0),
        )
        self.assertEqual(len(out), 1)
        entry = out[0]
        self.assertEqual(entry["revision_name"], "rev-a")
        self.assertEqual(entry["active_value"], 2)
        self.assertEqual(entry["idle_value"], 1)
        self.assertEqual(entry["active_plus_idle"], 3)

    def test_aggregate_over_real_protos_handles_zero_values(self):
        """A scaled-nearly-to-zero revision reports active=0,
        idle=N. `TypedValue.int64_value=0` is falsy — the extractor's
        first branch (`and value.int64_value`) short-circuits and
        falls through to `double_value`, which is also 0 on an
        int64-set point (unset oneof fields default to 0). The
        aggregator must still record 0 correctly."""
        now = time.time()
        pages = [
            self._mk_series(rev="rev-a", state="active",
                            points_iso_val=[(now - 30, 0)]),
            self._mk_series(rev="rev-a", state="idle",
                            points_iso_val=[(now - 30, 3)]),
        ]
        out = rcc._aggregate_metric_series(
            pages, now_epoch=now, deadline=common.Deadline(300.0),
        )
        self.assertEqual(len(out), 1)
        entry = out[0]
        self.assertEqual(entry["active_value"], 0)
        self.assertEqual(entry["idle_value"], 3)
        self.assertEqual(entry["active_plus_idle"], 3)

    def test_aggregate_over_real_protos_rejects_unexpected_state(self):
        now = time.time()
        pages = [
            self._mk_series(rev="rev-a", state="unknown_state",
                            points_iso_val=[(now - 30, 1)]),
        ]
        with self.assertRaises(ValueError) as ctx:
            rcc._aggregate_metric_series(
                pages, now_epoch=now, deadline=common.Deadline(300.0),
            )
        self.assertIn("state", str(ctx.exception))

    def test_aggregate_over_real_protos_rejects_missing_revision_name(self):
        from google.cloud import monitoring_v3
        from google.protobuf.timestamp_pb2 import Timestamp

        end = Timestamp()
        end.FromDatetime(datetime.now(timezone.utc))
        series = monitoring_v3.TimeSeries({
            "resource": {"labels": {}},  # no revision_name
            "metric": {"labels": {"state": "active"}},
            "points": [monitoring_v3.Point({
                "interval": {"end_time": end},
                "value": {"int64_value": 1},
            })],
        })
        with self.assertRaises(ValueError) as ctx:
            rcc._aggregate_metric_series(
                [series], now_epoch=time.time(),
                deadline=common.Deadline(300.0),
            )
        self.assertIn("revision_name", str(ctx.exception))


class ManualPaginationShrinkingTimeoutTests(unittest.TestCase):
    """R5 blocker #2: manual per-page timeout control.

    The gapic pager freezes the initial `timeout=` across every
    next-page RPC. If the outer deadline shrinks below the frozen
    `rpc_timeout`, later pages can still spend the full frozen
    value each. Round-4 checked `deadline.expired()` BETWEEN pages
    — but only AFTER the offending page RPC completed.

    R5 recomputes `deadline.rpc_timeout(rpc_timeout)` before every
    page and refuses to start a new page RPC once the deadline has
    expired. Below, a fake clock advances between page fetches so
    the recorded per-page timeouts strictly shrink, and a would-be
    fourth page RPC is refused because expiry fires FIRST.
    """

    def _fake_clock(self):
        """Returns (getter, setter). `common.Deadline` reads
        `time.monotonic` at the module scope."""
        state = {"t": 0.0}
        return state, (lambda: state["t"])

    # --- Cloud Logging manual pagination ------------------------

    def test_fetch_tick_events_per_page_timeout_shrinks_and_refuses_after_expiry(self):
        from google.cloud.logging_v2.services.logging_service_v2 import (
            LoggingServiceV2Client,
        )
        from google.cloud.logging_v2.services.logging_service_v2.pagers import (
            ListLogEntriesPager,
        )
        from google.cloud.logging_v2.types import (
            ListLogEntriesRequest,
            ListLogEntriesResponse,
            LogEntry,
        )
        from google.protobuf import struct_pb2
        from google.protobuf.timestamp_pb2 import Timestamp

        # Fake clock the Deadline reads.
        state, fake_monotonic = self._fake_clock()

        # Fixture: three pages with entries, all with non-empty
        # next_page_tokens so the loop would attempt a fourth page.
        def _mk_entry(rev, inst):
            payload = struct_pb2.Struct()
            payload.update({
                "event": "reconciler_tick",
                "instance_id": inst,
                "owned_rooms": 0,
            })
            entry = LogEntry(
                resource={"labels": {"revision_name": rev}},
                json_payload=payload,
            )
            ts = Timestamp()
            # Timestamp does not need to advance with fake clock —
            # `_extract_tick_entry` reads it verbatim.
            ts.FromDatetime(datetime(2026, 1, 1, tzinfo=timezone.utc))
            entry.timestamp = ts
            return entry

        pages_by_token = {
            "":     ListLogEntriesResponse(entries=[_mk_entry("rev-a", "i-1")],
                                           next_page_token="tok1"),
            "tok1": ListLogEntriesResponse(entries=[_mk_entry("rev-a", "i-2")],
                                           next_page_token="tok2"),
            "tok2": ListLogEntriesResponse(entries=[_mk_entry("rev-b", "i-3")],
                                           next_page_token="tok3"),
            # If a 4th RPC is attempted we'd hit this — the test
            # proves we never do.
            "tok3": ListLogEntriesResponse(entries=[], next_page_token=""),
        }

        # Per-page clock advance: page 1 costs 20s, page 2 costs
        # 15s, page 3 costs 12s. With budget=45 and ceiling=15,
        # per-page timeouts should be [15, 15, 10]. After page 3
        # the clock is at 47s → deadline expired, would-be page 4
        # refused.
        page_costs = [20.0, 15.0, 12.0]

        calls: list[dict] = []

        def _method(request, *, retry=None, timeout=None, metadata=()):
            token = getattr(request, "page_token", "") or ""
            calls.append({
                "page_token": token,
                "timeout": timeout,
                "retry": retry,
                "monotonic_at_call": state["t"],
            })
            state["t"] += page_costs[len(calls) - 1]
            return pages_by_token[token]

        class _FakeLoggingClient:
            def list_log_entries(self, request, *, retry=None, timeout=None, metadata=()):
                initial = _method(request, retry=retry, timeout=timeout, metadata=metadata)
                return ListLogEntriesPager(
                    method=_method,
                    request=request,
                    response=initial,
                    retry=retry,
                    timeout=timeout,
                    metadata=metadata,
                )

        with patch.object(common.time, "monotonic", fake_monotonic), \
             patch.object(LoggingServiceV2Client, "__new__",
                          lambda cls, *a, **k: _FakeLoggingClient()):
            with self.assertRaises(TimeoutError) as ctx:
                rcc.fetch_tick_events(
                    project=PRODUCTION_PROJECT,
                    service_name="worshiptranslate-backend",
                    region="us-central1",
                    tick_window_sec=300,
                    deadline=common.Deadline(45.0),
                    rpc_timeout=15.0,
                )

        # Refusal message must reference the pre-RPC expiry point.
        self.assertIn("deadline expired", str(ctx.exception).lower())

        # Exactly 3 RPCs happened; the 4th was refused pre-RPC.
        self.assertEqual(len(calls), 3,
                         f"expected 3 page RPCs before refusal, got {len(calls)}: "
                         f"{[c['page_token'] for c in calls]!r}")

        # Every call carried retry=None.
        for c in calls:
            self.assertIsNone(c["retry"], f"page {c['page_token']} lost retry=None")

        # Per-page timeouts: [15, 15, 10] under the model above.
        # Strictly stated: monotonic-non-increasing and shrinking
        # by the end.
        timeouts = [c["timeout"] for c in calls]
        for t in timeouts:
            self.assertGreater(t, 0)
            self.assertLessEqual(t, 15.0)
        self.assertLess(timeouts[-1], timeouts[0],
                        f"per-page timeouts must shrink; got {timeouts!r}")

        # Sanity: pre-RPC monotonic timestamps must be monotonically
        # non-decreasing (clock only advances forward).
        for i in range(1, len(calls)):
            self.assertGreaterEqual(calls[i]["monotonic_at_call"],
                                    calls[i-1]["monotonic_at_call"])

    # --- Cloud Monitoring manual pagination --------------------

    def test_fetch_metric_samples_per_page_timeout_shrinks_and_refuses_after_expiry(self):
        from google.cloud import monitoring_v3
        from google.cloud.monitoring_v3.services.metric_service import (
            MetricServiceClient,
        )
        from google.cloud.monitoring_v3.services.metric_service.pagers import (
            ListTimeSeriesPager,
        )
        from google.protobuf.timestamp_pb2 import Timestamp

        state, fake_monotonic = self._fake_clock()

        def _mk_series(rev, s):
            end = Timestamp()
            end.FromDatetime(datetime(2026, 1, 1, tzinfo=timezone.utc))
            return monitoring_v3.TimeSeries({
                "resource": {"labels": {"revision_name": rev}},
                "metric": {"labels": {"state": s}},
                "points": [monitoring_v3.Point({
                    "interval": {"end_time": end},
                    "value": {"int64_value": 1},
                })],
            })

        pages_by_token = {
            "":     monitoring_v3.ListTimeSeriesResponse(
                       time_series=[_mk_series("rev-a", "active")],
                       next_page_token="tok1"),
            "tok1": monitoring_v3.ListTimeSeriesResponse(
                       time_series=[_mk_series("rev-a", "idle")],
                       next_page_token="tok2"),
            "tok2": monitoring_v3.ListTimeSeriesResponse(
                       time_series=[_mk_series("rev-b", "active")],
                       next_page_token="tok3"),
            "tok3": monitoring_v3.ListTimeSeriesResponse(
                       time_series=[], next_page_token=""),
        }
        page_costs = [40.0, 30.0, 22.0]

        calls: list[dict] = []

        def _method(request, *, retry=None, timeout=None, metadata=()):
            token = getattr(request, "page_token", "") or ""
            calls.append({
                "page_token": token,
                "timeout": timeout,
                "retry": retry,
                "monotonic_at_call": state["t"],
            })
            state["t"] += page_costs[len(calls) - 1]
            return pages_by_token[token]

        class _FakeMetricClient:
            def list_time_series(self, request, *, retry=None, timeout=None, metadata=()):
                initial = _method(request, retry=retry, timeout=timeout, metadata=metadata)
                return ListTimeSeriesPager(
                    method=_method,
                    request=request,
                    response=initial,
                    retry=retry,
                    timeout=timeout,
                    metadata=metadata,
                )

        with patch.object(common.time, "monotonic", fake_monotonic), \
             patch.object(MetricServiceClient, "__new__",
                          lambda cls, *a, **k: _FakeMetricClient()):
            with self.assertRaises(TimeoutError) as ctx:
                rcc.fetch_metric_samples(
                    project=PRODUCTION_PROJECT,
                    service_name="worshiptranslate-backend",
                    region="us-central1",
                    lookback_sec=240,
                    deadline=common.Deadline(90.0),
                    rpc_timeout=30.0,
                )

        self.assertIn("deadline expired", str(ctx.exception).lower())
        self.assertEqual(len(calls), 3,
                         f"expected 3 page RPCs before refusal, got {len(calls)}: "
                         f"{[c['page_token'] for c in calls]!r}")
        for c in calls:
            self.assertIsNone(c["retry"], f"page {c['page_token']} lost retry=None")

        timeouts = [c["timeout"] for c in calls]
        # Budget=90, ceiling=30; costs 40/30/22.
        # Page 1 (t=0):  min(30, 90) = 30. After: t=40.
        # Page 2 (t=40): min(30, 50) = 30. After: t=70.
        # Page 3 (t=70): min(30, 20) = 20. After: t=92 → expired.
        for t in timeouts:
            self.assertGreater(t, 0)
            self.assertLessEqual(t, 30.0)
        self.assertLess(timeouts[-1], timeouts[0],
                        f"per-page timeouts must shrink; got {timeouts!r}")


class PostRpcDeadlineEnforcementTests(unittest.TestCase):
    """R6 blocker #1: enforce the outer deadline AFTER every page
    RPC, DURING Logging entry processing, and BEFORE successful
    return. R5's manual pagination checked the deadline BEFORE
    each page RPC — but a final page whose RPC itself overran the
    budget, or a large entry loop that overran during processing,
    would have slipped through to a "successful" return."""

    def _fake_clock(self):
        state = {"t": 0.0}
        return state, (lambda: state["t"])

    # --- Logging: final-page RPC overruns budget ---------------

    def test_fetch_tick_events_raises_after_final_page_rpc_overrun(self):
        """Single page with `next_page_token=""`. The RPC returns
        successfully with a real entry, but consumes 40s of a 30s
        budget. R6 post-RPC gate must raise TimeoutError instead
        of returning the buffered entry."""
        from google.cloud.logging_v2.services.logging_service_v2 import (
            LoggingServiceV2Client,
        )
        from google.cloud.logging_v2.services.logging_service_v2.pagers import (
            ListLogEntriesPager,
        )
        from google.cloud.logging_v2.types import (
            ListLogEntriesResponse, LogEntry,
        )
        from google.protobuf import struct_pb2
        from google.protobuf.timestamp_pb2 import Timestamp

        state, fake_monotonic = self._fake_clock()

        payload = struct_pb2.Struct()
        payload.update({
            "event": "reconciler_tick",
            "instance_id": "i-1",
            "owned_rooms": 0,
        })
        entry = LogEntry(
            resource={"labels": {"revision_name": "rev-a"}},
            json_payload=payload,
        )
        ts = Timestamp()
        ts.FromDatetime(datetime(2026, 1, 1, tzinfo=timezone.utc))
        entry.timestamp = ts

        final_page = ListLogEntriesResponse(
            entries=[entry], next_page_token="",
        )

        calls: list[dict] = []

        def _method(request, *, retry=None, timeout=None, metadata=()):
            calls.append({"timeout": timeout, "retry": retry})
            # Overrun the outer budget in a single RPC.
            state["t"] += 40.0
            return final_page

        class _FakeLoggingClient:
            def list_log_entries(self, request, *, retry=None, timeout=None, metadata=()):
                initial = _method(request, retry=retry, timeout=timeout, metadata=metadata)
                return ListLogEntriesPager(
                    method=_method,
                    request=request,
                    response=initial,
                    retry=retry,
                    timeout=timeout,
                    metadata=metadata,
                )

        with patch.object(common.time, "monotonic", fake_monotonic), \
             patch.object(LoggingServiceV2Client, "__new__",
                          lambda cls, *a, **k: _FakeLoggingClient()):
            with self.assertRaises(TimeoutError) as ctx:
                rcc.fetch_tick_events(
                    project=PRODUCTION_PROJECT,
                    service_name="worshiptranslate-backend",
                    region="us-central1",
                    tick_window_sec=300,
                    deadline=common.Deadline(30.0),
                    rpc_timeout=15.0,
                )

        self.assertEqual(len(calls), 1)
        self.assertIn("after Cloud Logging page RPC returned",
                      str(ctx.exception))

    # --- Logging: entry-loop overruns budget -------------------

    def test_fetch_tick_events_raises_during_entry_processing(self):
        """RPC returns quickly with N entries; entry extraction
        advances the fake clock, so somewhere mid-loop the budget
        expires. R6 entry-loop gate must raise before extracting
        the entry that would push us further past budget."""
        from google.cloud.logging_v2.services.logging_service_v2 import (
            LoggingServiceV2Client,
        )
        from google.cloud.logging_v2.services.logging_service_v2.pagers import (
            ListLogEntriesPager,
        )
        from google.cloud.logging_v2.types import (
            ListLogEntriesResponse, LogEntry,
        )
        from google.protobuf import struct_pb2
        from google.protobuf.timestamp_pb2 import Timestamp

        state, fake_monotonic = self._fake_clock()

        def _entry(inst):
            payload = struct_pb2.Struct()
            payload.update({
                "event": "reconciler_tick",
                "instance_id": inst,
                "owned_rooms": 0,
            })
            e = LogEntry(
                resource={"labels": {"revision_name": "rev-a"}},
                json_payload=payload,
            )
            ts = Timestamp()
            ts.FromDatetime(datetime(2026, 1, 1, tzinfo=timezone.utc))
            e.timestamp = ts
            return e

        page = ListLogEntriesResponse(
            entries=[_entry("i-1"), _entry("i-2"), _entry("i-3")],
            next_page_token="",
        )

        def _method(request, *, retry=None, timeout=None, metadata=()):
            # RPC itself is fast.
            state["t"] += 1.0
            return page

        class _FakeLoggingClient:
            def list_log_entries(self, request, *, retry=None, timeout=None, metadata=()):
                initial = _method(request, retry=retry, timeout=timeout, metadata=metadata)
                return ListLogEntriesPager(
                    method=_method,
                    request=request,
                    response=initial,
                    retry=retry,
                    timeout=timeout,
                    metadata=metadata,
                )

        # Advance the clock inside _extract_tick_entry so entry
        # processing consumes time. Deadline budget = 10s;
        # each extract advances 5s. Iter 1 (t=1→check ok→extract
        # →t=6). Iter 2 (t=6→check ok→extract→t=11). Iter 3 (t=11
        # →check fires; TimeoutError).
        original_extract = rcc._extract_tick_entry
        def slow_extract(entry):
            result = original_extract(entry)
            state["t"] += 5.0
            return result

        with patch.object(common.time, "monotonic", fake_monotonic), \
             patch.object(LoggingServiceV2Client, "__new__",
                          lambda cls, *a, **k: _FakeLoggingClient()), \
             patch.object(rcc, "_extract_tick_entry", slow_extract):
            with self.assertRaises(TimeoutError) as ctx:
                rcc.fetch_tick_events(
                    project=PRODUCTION_PROJECT,
                    service_name="worshiptranslate-backend",
                    region="us-central1",
                    tick_window_sec=300,
                    deadline=common.Deadline(10.0),
                    rpc_timeout=5.0,
                )
        self.assertIn("while processing Cloud Logging entries",
                      str(ctx.exception))

    # --- Monitoring: final-page RPC overruns budget ------------

    def test_fetch_metric_samples_raises_after_aggregation_overruns_deadline(self):
        """R7 blocker: `_aggregate_metric_series` checks
        `deadline.expired()` at the TOP of each series iteration,
        so processing the FINAL series can push us past budget
        without any inner check firing again — aggregation
        completes and `fetch_metric_samples` would return the
        aggregated result. R7 adds a post-aggregation gate that
        must raise TimeoutError with a distinct message before
        the successful return.

        Test setup:
          - Budget = 20s; RPC costs 5s (within budget).
          - Two real proto series (active + idle for `rev-a` at
            the same 1-s bucket, so aggregation produces exactly
            one union entry).
          - `_extract_point_value` is patched to advance the fake
            clock by 10s per call. Series 1's point advances
            5→15 (still within budget); series 2's point advances
            15→25 (past budget) — but the aggregator's pre-iter
            check has already passed for series 2, so aggregation
            completes.
          - fetch_metric_samples must raise TimeoutError instead
            of returning the aggregated `rev-a` entry."""
        from google.cloud import monitoring_v3
        from google.cloud.monitoring_v3.services.metric_service import (
            MetricServiceClient,
        )
        from google.cloud.monitoring_v3.services.metric_service.pagers import (
            ListTimeSeriesPager,
        )
        from google.protobuf.timestamp_pb2 import Timestamp

        state, fake_monotonic = self._fake_clock()

        # Fix the point timestamp so `_extract_point_timestamp`
        # is deterministic; only `_extract_point_value` advances
        # the fake clock.
        end = Timestamp()
        end.FromDatetime(datetime(2026, 1, 1, tzinfo=timezone.utc))
        active_series = monitoring_v3.TimeSeries({
            "resource": {"labels": {"revision_name": "rev-a"}},
            "metric": {"labels": {"state": "active"}},
            "points": [monitoring_v3.Point({
                "interval": {"end_time": end},
                "value": {"int64_value": 2},
            })],
        })
        idle_series = monitoring_v3.TimeSeries({
            "resource": {"labels": {"revision_name": "rev-a"}},
            "metric": {"labels": {"state": "idle"}},
            "points": [monitoring_v3.Point({
                "interval": {"end_time": end},
                "value": {"int64_value": 1},
            })],
        })
        final_page = monitoring_v3.ListTimeSeriesResponse(
            time_series=[active_series, idle_series],
            next_page_token="",
        )

        rpc_calls: list[dict] = []

        def _method(request, *, retry=None, timeout=None, metadata=()):
            rpc_calls.append({"timeout": timeout, "retry": retry})
            state["t"] += 5.0   # RPC costs 5s — within 20s budget
            return final_page

        class _FakeMetricClient:
            def list_time_series(self, request, *, retry=None, timeout=None, metadata=()):
                initial = _method(request, retry=retry, timeout=timeout, metadata=metadata)
                return ListTimeSeriesPager(
                    method=_method,
                    request=request,
                    response=initial,
                    retry=retry,
                    timeout=timeout,
                    metadata=metadata,
                )

        original_extract = rcc._extract_point_value

        def slow_extract(point):
            v = original_extract(point)
            state["t"] += 10.0   # each point extraction advances 10s
            return v

        with patch.object(common.time, "monotonic", fake_monotonic), \
             patch.object(MetricServiceClient, "__new__",
                          lambda cls, *a, **k: _FakeMetricClient()), \
             patch.object(rcc, "_extract_point_value", slow_extract):
            with self.assertRaises(TimeoutError) as ctx:
                rcc.fetch_metric_samples(
                    project=PRODUCTION_PROJECT,
                    service_name="worshiptranslate-backend",
                    region="us-central1",
                    lookback_sec=240,
                    deadline=common.Deadline(20.0),
                    rpc_timeout=15.0,
                )

        # Exactly one page RPC — no next_page_token; the loop
        # exits cleanly after page 1 and hands control to
        # `_aggregate_metric_series`.
        self.assertEqual(len(rpc_calls), 1)
        # Distinct R7 message so callers can distinguish this from
        # the R6 pre-aggregation and post-RPC gates.
        self.assertIn("before returning Cloud Monitoring results",
                      str(ctx.exception))

    def test_fetch_metric_samples_raises_after_final_page_rpc_overrun(self):
        """Same shape as the tick-side final-page test: single
        page with `next_page_token=""` whose RPC overruns the
        outer budget. R6 post-RPC gate must raise."""
        from google.cloud import monitoring_v3
        from google.cloud.monitoring_v3.services.metric_service import (
            MetricServiceClient,
        )
        from google.cloud.monitoring_v3.services.metric_service.pagers import (
            ListTimeSeriesPager,
        )
        from google.protobuf.timestamp_pb2 import Timestamp

        state, fake_monotonic = self._fake_clock()

        end = Timestamp()
        end.FromDatetime(datetime(2026, 1, 1, tzinfo=timezone.utc))
        series = monitoring_v3.TimeSeries({
            "resource": {"labels": {"revision_name": "rev-a"}},
            "metric": {"labels": {"state": "active"}},
            "points": [monitoring_v3.Point({
                "interval": {"end_time": end},
                "value": {"int64_value": 1},
            })],
        })
        final_page = monitoring_v3.ListTimeSeriesResponse(
            time_series=[series], next_page_token="",
        )

        calls: list[dict] = []

        def _method(request, *, retry=None, timeout=None, metadata=()):
            calls.append({"timeout": timeout, "retry": retry})
            state["t"] += 120.0  # overrun the outer 60s budget
            return final_page

        class _FakeMetricClient:
            def list_time_series(self, request, *, retry=None, timeout=None, metadata=()):
                initial = _method(request, retry=retry, timeout=timeout, metadata=metadata)
                return ListTimeSeriesPager(
                    method=_method,
                    request=request,
                    response=initial,
                    retry=retry,
                    timeout=timeout,
                    metadata=metadata,
                )

        with patch.object(common.time, "monotonic", fake_monotonic), \
             patch.object(MetricServiceClient, "__new__",
                          lambda cls, *a, **k: _FakeMetricClient()):
            with self.assertRaises(TimeoutError) as ctx:
                rcc.fetch_metric_samples(
                    project=PRODUCTION_PROJECT,
                    service_name="worshiptranslate-backend",
                    region="us-central1",
                    lookback_sec=240,
                    deadline=common.Deadline(60.0),
                    rpc_timeout=30.0,
                )
        self.assertEqual(len(calls), 1)
        self.assertIn("after Cloud Monitoring page RPC returned",
                      str(ctx.exception))


class StrictOneofPointValueTests(unittest.TestCase):
    """R6 blocker #2: `_extract_point_value` must inspect the real
    TypedValue oneof and accept only `int64_value` (including
    zero). Every other set branch, and every unset value, must
    raise ValueError.

    Real `monitoring_v3.TypedValue` protos preserve oneof
    membership through proto-plus: `int64_value=0` still sets
    the oneof to `"int64_value"`, so a legitimate scaled-to-zero
    Cloud Run revision is accepted. `double_value=1.5`,
    `bool_value=True`, `string_value="x"`, `distribution_value=…`,
    and `TypedValue()` (no branch set) all fail closed."""

    def _pt(self, typed_value):
        from google.cloud import monitoring_v3
        from google.protobuf.timestamp_pb2 import Timestamp
        end = Timestamp()
        end.FromDatetime(datetime(2026, 1, 1, tzinfo=timezone.utc))
        return monitoring_v3.Point({
            "interval": {"end_time": end},
            "value": typed_value,
        })

    def test_int64_zero_accepted(self):
        from google.cloud import monitoring_v3
        p = self._pt(monitoring_v3.TypedValue({"int64_value": 0}))
        self.assertEqual(rcc._extract_point_value(p), 0)

    def test_int64_nonzero_accepted(self):
        from google.cloud import monitoring_v3
        p = self._pt(monitoring_v3.TypedValue({"int64_value": 7}))
        self.assertEqual(rcc._extract_point_value(p), 7)

    def test_double_value_rejected(self):
        from google.cloud import monitoring_v3
        p = self._pt(monitoring_v3.TypedValue({"double_value": 1.5}))
        with self.assertRaises(ValueError) as ctx:
            rcc._extract_point_value(p)
        self.assertIn("double_value", str(ctx.exception))

    def test_bool_value_rejected(self):
        from google.cloud import monitoring_v3
        p = self._pt(monitoring_v3.TypedValue({"bool_value": True}))
        with self.assertRaises(ValueError) as ctx:
            rcc._extract_point_value(p)
        self.assertIn("bool_value", str(ctx.exception))

    def test_string_value_rejected(self):
        from google.cloud import monitoring_v3
        p = self._pt(monitoring_v3.TypedValue({"string_value": "x"}))
        with self.assertRaises(ValueError) as ctx:
            rcc._extract_point_value(p)
        self.assertIn("string_value", str(ctx.exception))

    def test_distribution_value_rejected(self):
        from google.cloud import monitoring_v3
        # A default Distribution is enough — `WhichOneof('value')`
        # returns `distribution_value` as long as the field is set.
        p = self._pt(monitoring_v3.TypedValue({"distribution_value": {}}))
        with self.assertRaises(ValueError) as ctx:
            rcc._extract_point_value(p)
        self.assertIn("distribution_value", str(ctx.exception))

    def test_unset_typed_value_rejected(self):
        from google.cloud import monitoring_v3
        p = self._pt(monitoring_v3.TypedValue())
        with self.assertRaises(ValueError) as ctx:
            rcc._extract_point_value(p)
        self.assertIn("no oneof branch set", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
