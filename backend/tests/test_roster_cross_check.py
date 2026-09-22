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
            "container_name_filter", "tick_window_seconds",
            "metric_freshness_max_age_seconds",
            "tick_roster", "cloud_run_metric", "roster_union",
            "all_instances_clean", "all_revisions_match",
            "metric_freshness_ok", "elapsed_seconds", "rc", "reason",
        ):
            self.assertIn(key, payload, f"missing schema field {key!r}")


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

    def test_cross_check_match(self):
        tick_roster = [
            {"revision_name": "rev-a", "instance_id": "i-1",
             "tick_timestamps": [], "youngest_tick_age_seconds": 0,
             "span_seconds": 0, "owned_rooms_last_tick": 0,
             "status": "clean"},
            {"revision_name": "rev-a", "instance_id": "i-2",
             "tick_timestamps": [], "youngest_tick_age_seconds": 0,
             "span_seconds": 0, "owned_rooms_last_tick": 0,
             "status": "clean"},
        ]
        metric = [{"revision_name": "rev-a", "container_name": "x",
                   "active_plus_idle": 2, "sample_timestamp_iso": "",
                   "sample_timestamp_epoch": 0, "sample_age_seconds": 10}]
        union, all_clean, all_match, freshness = rcc.cross_check(
            tick_roster, metric, freshness_max_age=180.0,
        )
        self.assertTrue(all_clean)
        self.assertTrue(all_match)
        self.assertTrue(freshness)
        self.assertEqual(union[0]["status"], "match")

    def test_cross_check_mismatch(self):
        tick_roster = [
            {"revision_name": "rev-a", "instance_id": "i-1",
             "tick_timestamps": [], "youngest_tick_age_seconds": 0,
             "span_seconds": 0, "owned_rooms_last_tick": 0,
             "status": "clean"},
        ]
        metric = [{"revision_name": "rev-a", "container_name": "x",
                   "active_plus_idle": 3, "sample_timestamp_iso": "",
                   "sample_timestamp_epoch": 0, "sample_age_seconds": 10}]
        union, all_clean, all_match, freshness = rcc.cross_check(
            tick_roster, metric, freshness_max_age=180.0,
        )
        self.assertFalse(all_match)
        self.assertEqual(union[0]["status"], "mismatch")

    def test_cross_check_missing_metric(self):
        tick_roster = [
            {"revision_name": "rev-a", "instance_id": "i-1",
             "tick_timestamps": [], "youngest_tick_age_seconds": 0,
             "span_seconds": 0, "owned_rooms_last_tick": 0,
             "status": "clean"},
        ]
        union, all_clean, all_match, freshness = rcc.cross_check(
            tick_roster, [], freshness_max_age=180.0,
        )
        self.assertFalse(all_match)
        self.assertEqual(union[0]["status"], "missing_metric")

    def test_cross_check_missing_ticks(self):
        metric = [{"revision_name": "rev-a", "container_name": "x",
                   "active_plus_idle": 1, "sample_timestamp_iso": "",
                   "sample_timestamp_epoch": 0, "sample_age_seconds": 10}]
        union, all_clean, all_match, freshness = rcc.cross_check(
            [], metric, freshness_max_age=180.0,
        )
        self.assertFalse(all_match)
        self.assertEqual(union[0]["status"], "missing_ticks")

    def test_cross_check_stale_metric_flips_freshness(self):
        tick_roster = [
            {"revision_name": "rev-a", "instance_id": "i-1",
             "tick_timestamps": [], "youngest_tick_age_seconds": 0,
             "span_seconds": 0, "owned_rooms_last_tick": 0,
             "status": "clean"},
        ]
        metric = [{"revision_name": "rev-a", "container_name": "x",
                   "active_plus_idle": 1, "sample_timestamp_iso": "",
                   "sample_timestamp_epoch": 0,
                   "sample_age_seconds": 240}]  # 240 > 180
        union, all_clean, all_match, freshness = rcc.cross_check(
            tick_roster, metric, freshness_max_age=180.0,
        )
        self.assertFalse(freshness)

    def test_empty_roster_and_empty_metric_is_clean(self):
        union, all_clean, all_match, freshness = rcc.cross_check(
            [], [], freshness_max_age=180.0,
        )
        self.assertEqual(union, [])
        self.assertTrue(all_clean)
        self.assertTrue(all_match)
        self.assertTrue(freshness)


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

    def _fresh_metric(self, rev, count, container="worshiptranslate-backend"):
        now = time.time()
        return {"revision_name": rev, "container_name": container,
                "active_plus_idle": count, "sample_timestamp_iso": "s",
                "sample_timestamp_epoch": now - 30, "sample_age_seconds": 30}

    # --- rc=0 verified -------------------------------------------------

    def test_rc0_verified_when_ticks_and_metric_agree(self):
        events = self._clean_tick("rev-a", "i-1")
        metric = [self._fresh_metric("rev-a", 1)]
        rc, payload, err = self._run_shim(events, metric)
        self.assertEqual(rc, 0, f"payload={payload!r} stderr={err!r}")
        self.assertTrue(payload["all_instances_clean"])
        self.assertTrue(payload["all_revisions_match"])
        self.assertTrue(payload["metric_freshness_ok"])

    def test_rc0_verified_when_no_instances_at_all(self):
        rc, payload, err = self._run_shim([], [])
        self.assertEqual(rc, 0, f"payload={payload!r} stderr={err!r}")

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
        # sample_age > freshness_max (default 180s)
        metric = [{
            "revision_name": "rev-a", "container_name": "worshiptranslate-backend",
            "active_plus_idle": 1, "sample_timestamp_iso": "old",
            "sample_timestamp_epoch": time.time() - 500,
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


if __name__ == "__main__":
    unittest.main()
