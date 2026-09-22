"""Window 1 preflight — tests for `count_live_rooms.py`.

Three suites:

  - `CountLiveRoomsAllowlistUnitTests` — no Firestore call at
    all. Drives the CLI against argv variants to exercise the
    allowlist + env-mismatch branch. Verifies the exact JSON
    shape + rc contract without needing google-cloud-firestore
    installed.
  - `CountLiveRoomsEmulatorSubprocessTests` — spins the actual
    CLI up as a subprocess against the Firestore emulator, seeds
    the emulator with rooms in various states, asserts JSON
    output + rc per the runbook decision table. Skips locally
    without `FIRESTORE_EMULATOR_HOST`; runs in CI's
    firestore-emulator-tests job.
  - `CountLiveRoomsDeadlineUnitTests` — targeted unit tests
    exercising the `Deadline` helper in the shared module,
    since the CLI's timeout branch is time-sensitive and hard
    to hit deterministically via subprocess.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO / "backend" / "scripts" / "window1_preflight"
sys.path.insert(0, str(_SCRIPTS_DIR))

import common  # noqa: E402
import count_live_rooms as clr  # noqa: E402


PRODUCTION_PROJECT = "sturdy-dogfish-472313-k6"
PRODUCTION_DATABASE = "worship-translation"
EMULATOR_PROJECT = "cleanup-track1-emulator"
EMULATOR_DATABASE = "(default)"


class _EnvContext:
    """Temporarily set / unset env vars for a subprocess."""

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
    """In-process CLI invocation. Returns (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = clr._run(list(argv))
        except SystemExit as exc:
            rc = int(exc.code) if exc.code is not None else 0
    return rc, out.getvalue(), err.getvalue()


class CountLiveRoomsAllowlistUnitTests(unittest.TestCase):
    """Pure argv-driven tests — no Firestore call."""

    def test_wrong_project_returns_rc2(self):
        rc, out, _err = _run_cli(
            "--project", "some-other-project",
            "--database", PRODUCTION_DATABASE,
        )
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "live_room_count")
        self.assertEqual(payload["rc"], 2)
        self.assertIsNone(payload["count"])
        self.assertFalse(payload["complete"])
        self.assertIn("allowlist", payload["reason"])

    def test_wrong_database_returns_rc2(self):
        rc, out, _err = _run_cli(
            "--project", PRODUCTION_PROJECT,
            "--database", "wrong-db",
        )
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertIn("allowlist", payload["reason"])

    def test_production_target_refuses_when_emulator_env_set(self):
        with _EnvContext(FIRESTORE_EMULATOR_HOST="127.0.0.1:8085"):
            rc, out, _err = _run_cli(
                "--project", PRODUCTION_PROJECT,
                "--database", PRODUCTION_DATABASE,
            )
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertIn("FIRESTORE_EMULATOR_HOST", payload["reason"])
        self.assertIn("production", payload["reason"])

    def test_emulator_target_refuses_when_emulator_env_unset(self):
        with _EnvContext(FIRESTORE_EMULATOR_HOST=None):
            rc, out, _err = _run_cli(
                "--project", EMULATOR_PROJECT,
                "--database", EMULATOR_DATABASE,
            )
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertIn("emulator", payload["reason"])

    def test_output_always_has_stable_schema_fields(self):
        rc, out, _err = _run_cli(
            "--project", "bogus", "--database", "bogus",
        )
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        for key in (
            "kind", "command", "verified_at", "project", "database",
            "collection_group", "filter", "count", "complete",
            "documents_scanned", "elapsed_seconds", "rc", "reason",
        ):
            self.assertIn(key, payload, f"missing schema field {key!r}")
        self.assertEqual(payload["command"], "count_live_rooms.py")
        self.assertEqual(payload["collection_group"], "rooms")

    def test_zero_deadline_rejected_by_argparse(self):
        """Reviewer's PR #41 blocker: --deadline-sec 0 must be
        rejected by the positive-finite validator, not silently
        accepted then blow up inside Deadline(). Argparse exits 2
        via its own error path (usage on stderr, no JSON on
        stdout) — distinct from ALLOWLIST_REFUSAL rc=2 which
        does emit JSON."""
        proc = subprocess.run(
            [sys.executable, "-u",
             str(_SCRIPTS_DIR / "count_live_rooms.py"),
             "--project", PRODUCTION_PROJECT,
             "--database", PRODUCTION_DATABASE,
             "--deadline-sec", "0"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "", "argparse must not emit JSON")
        self.assertIn("positive", proc.stderr.lower())

    def test_negative_rpc_timeout_rejected_by_argparse(self):
        proc = subprocess.run(
            [sys.executable, "-u",
             str(_SCRIPTS_DIR / "count_live_rooms.py"),
             "--project", PRODUCTION_PROJECT,
             "--database", PRODUCTION_DATABASE,
             "--rpc-timeout-sec", "-2"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_nan_deadline_rejected_by_argparse(self):
        proc = subprocess.run(
            [sys.executable, "-u",
             str(_SCRIPTS_DIR / "count_live_rooms.py"),
             "--project", PRODUCTION_PROJECT,
             "--database", PRODUCTION_DATABASE,
             "--deadline-sec", "nan"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("finite", proc.stderr.lower())

    def test_default_credentials_error_at_construction_returns_rc6(self):
        """Reviewer's PR #41 R3 blocker #1: ADC missing raises
        DefaultCredentialsError at firestore.Client(...)
        construction, BEFORE any RPC. Round-2 code built the
        client outside the guarded try and emitted a bare
        traceback — the JSON contract was silently broken. Now
        the client build is inside try and DefaultCredentialsError
        maps to rc=6 with a valid JSON payload."""
        import tempfile as _tempfile
        tmpdir = Path(_tempfile.mkdtemp(prefix="clr-cred-shim-"))
        try:
            shim_path = tmpdir / "shim.py"
            shim_path.write_text(
                "import sys\n"
                f"sys.path.insert(0, {str(_SCRIPTS_DIR)!r})\n"
                "# Poison firestore.Client so construction raises\n"
                "# exactly what a missing ADC would raise.\n"
                "from google.auth import exceptions as gauth\n"
                "import google.cloud.firestore as gcf\n"
                "class _RaisingClient:\n"
                "    def __init__(self, *a, **k):\n"
                "        raise gauth.DefaultCredentialsError('test: ADC not found')\n"
                "gcf.Client = _RaisingClient\n"
                "import count_live_rooms as clr\n"
                "sys.exit(clr._run(sys.argv[1:]))\n"
            )
            env = os.environ.copy()
            env["FIRESTORE_EMULATOR_HOST"] = "127.0.0.1:0"
            proc = subprocess.run(
                [sys.executable, "-u", str(shim_path),
                 "--project", EMULATOR_PROJECT,
                 "--database", EMULATOR_DATABASE],
                capture_output=True, text=True, timeout=30, env=env,
            )
            self.assertEqual(
                proc.returncode, 6,
                f"expected rc=6, got {proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}",
            )
            # The JSON contract must hold — exactly one line of JSON
            # on stdout, no bare traceback.
            lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
            self.assertEqual(len(lines), 1, f"stdout: {proc.stdout!r}")
            payload = json.loads(lines[0])
            self.assertEqual(payload["kind"], "live_room_count")
            self.assertEqual(payload["rc"], 6)
            self.assertIn("credentials", payload["reason"].lower())
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class CountLiveRoomsDeadlineUnitTests(unittest.TestCase):

    def test_deadline_bounds_rpc_timeout(self):
        # Small budget — rpc_timeout must never exceed it.
        d = common.Deadline(0.5)
        self.assertLessEqual(d.rpc_timeout(10.0), 0.5)
        self.assertGreaterEqual(d.rpc_timeout(10.0), 0.0)

    def test_deadline_expired_after_sleep(self):
        d = common.Deadline(0.05)
        time.sleep(0.06)
        self.assertTrue(d.expired())
        self.assertEqual(d.remaining(), 0.0)

    def test_deadline_rejects_zero_budget(self):
        with self.assertRaises(ValueError):
            common.Deadline(0.0)


# --- Emulator + subprocess suite ------------------------------------------


@unittest.skipUnless(
    os.getenv("FIRESTORE_EMULATOR_HOST"),
    "Firestore emulator required — set FIRESTORE_EMULATOR_HOST + "
    "GOOGLE_CLOUD_PROJECT. CI runs this in the firestore-emulator "
    "job.",
)
class CountLiveRoomsEmulatorSubprocessTests(unittest.TestCase):
    """Real subprocess launch of the CLI against the Firestore
    emulator. This is the only suite that proves the CLI's actual
    exit code + stdout/stderr split under the operator's exec
    conditions."""

    _SEEDED_ORGS = ("emu-org-a", "emu-org-b", "emu-org-c")

    def setUp(self):
        from google.cloud import firestore  # type: ignore
        self._client = firestore.Client(
            project=EMULATOR_PROJECT, database=EMULATOR_DATABASE,
        )
        self._clear()

    def tearDown(self):
        self._clear()

    def _clear(self):
        for org in self._SEEDED_ORGS:
            for room in self._client.collection(
                f"organizations/{org}/rooms",
            ).stream():
                room.reference.delete()
            try:
                self._client.document(f"organizations/{org}").delete()
            except Exception:
                pass

    def _seed(self, mapping: dict[str, dict[str, str]]) -> None:
        """`mapping` = {org: {room_id: status}}. Writes each doc
        into the emulator."""
        for org, rooms in mapping.items():
            self._client.document(f"organizations/{org}").set(
                {"seeded_by": "test"}
            )
            for room_id, status in rooms.items():
                self._client.document(
                    f"organizations/{org}/rooms/{room_id}"
                ).set({"status": status})

    def _cli(self, *extra: str) -> tuple[int, dict, str]:
        """Subprocess invocation — the operator's actual exec
        path. Returns (rc, parsed stdout JSON, stderr text)."""
        cmd = [
            sys.executable, "-u",
            str(_SCRIPTS_DIR / "count_live_rooms.py"),
            "--project", EMULATOR_PROJECT,
            "--database", EMULATOR_DATABASE,
            *extra,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        try:
            payload = json.loads(proc.stdout)
        except Exception:
            payload = {}
        return proc.returncode, payload, proc.stderr

    def test_zero_live_rooms_returns_rc0_complete_true_count_zero(self):
        # Emulator is clean (setUp cleared). Expect count=0.
        rc, payload, err = self._cli()
        self.assertEqual(rc, 0, f"stdout={payload!r} stderr={err!r}")
        self.assertEqual(payload["count"], 0)
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["rc"], 0)

    def test_seeded_live_rooms_are_counted(self):
        self._seed({
            "emu-org-a": {"r1": "live", "r2": "ended"},
            "emu-org-b": {"r3": "live", "r4": "live"},
            "emu-org-c": {"r5": "pending"},
        })
        rc, payload, err = self._cli()
        self.assertEqual(rc, 0, f"stdout={payload!r} stderr={err!r}")
        self.assertEqual(payload["count"], 3)
        self.assertTrue(payload["complete"])

    def test_json_stable_schema_fields_present(self):
        rc, payload, _err = self._cli()
        for key in (
            "kind", "command", "verified_at", "project", "database",
            "collection_group", "filter", "count", "complete",
            "documents_scanned", "elapsed_seconds", "rc",
        ):
            self.assertIn(key, payload, f"missing schema field {key!r}")

    def test_stdout_contains_exactly_one_json_line(self):
        cmd = [
            sys.executable, "-u",
            str(_SCRIPTS_DIR / "count_live_rooms.py"),
            "--project", EMULATOR_PROJECT,
            "--database", EMULATOR_DATABASE,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(
            len(lines), 1,
            f"expected exactly one JSON line on stdout, got {len(lines)}: "
            f"{proc.stdout!r}",
        )


if __name__ == "__main__":
    unittest.main()
