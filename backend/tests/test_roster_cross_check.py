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
        from types import SimpleNamespace
        from datetime import datetime, timezone
        return SimpleNamespace(
            interval=SimpleNamespace(
                end_time=datetime.fromtimestamp(epoch, timezone.utc),
            ),
            # Provide both fields — the extractor prefers int64_value
            # when truthy, else falls through to double_value.
            value=SimpleNamespace(
                int64_value=value, double_value=float(value),
            ),
        )

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


if __name__ == "__main__":
    unittest.main()
