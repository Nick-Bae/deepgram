"""Wraps ops/monitoring/reconciler/validate.py so CI runs it inside the
backend-tests job. Failing validation fails the build before the
monitoring stack can be applied against a real project."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import unittest


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
VALIDATOR = REPO_ROOT / "ops" / "monitoring" / "reconciler" / "validate.py"


class MonitoringConfigValidatorTests(unittest.TestCase):
    def test_ops_monitoring_reconciler_validate_passes(self):
        self.assertTrue(
            VALIDATOR.is_file(),
            f"expected validator at {VALIDATOR} — see PR-T1-E scoping",
        )
        result = subprocess.run(
            [sys.executable, str(VALIDATOR)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"validate.py exited {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}",
        )
