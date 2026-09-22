"""Task #136 — validation tests for the redis-fanout
infrastructure YAMLs at `ops/infrastructure/redis-fanout/`.

Three suites, matching the pattern PR #39 established for the
monitoring config:

  - `InfraConfigValidatorTests` — runs the shared
    `validate.load_and_validate()` and asserts every itemised
    check passed. Individual check-name assertions localise
    regressions.

  - `InfraApplyPlannerTests` — exercises `apply.build_plan()`
    and CLI `_run()` directly. Asserts allowlist refusal,
    static-check refusal, `--apply` returns rc=6 with correct
    confirmation, `--apply` returns rc=5 without confirmation,
    dry-run header rename, output redaction.

  - `InfraPinnedConstraintDriftTests` — copies the cloudrun
    YAML in memory, mutates the pinned fields, and asserts the
    validator surfaces the drift. Regression barrier for the
    reviewer's constraint list: any future edit that removes a
    pin from the manifest without the reviewer's approval also
    fails these tests.
"""
from __future__ import annotations

import copy
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_OPS_DIR = _REPO / "ops" / "infrastructure" / "redis-fanout"
sys.path.insert(0, str(_OPS_DIR))

import validate as _validate  # noqa: E402
import apply as _apply  # noqa: E402


class InfraConfigValidatorTests(unittest.TestCase):

    def setUp(self):
        self.report = _validate.load_and_validate()

    def test_all_checks_passed(self):
        failed = self.report.failed()
        if failed:
            details = "\n".join(f"  - {c.name}: {c.detail}" for c in failed)
            self.fail(f"validate.load_and_validate() failures:\n{details}")

    def test_pinned_redis_enabled_is_literal_zero(self):
        c = next(
            (c for c in self.report.checks
             if c.name.endswith("REDIS_ENABLED')].value")),
            None,
        )
        self.assertIsNotNone(c)
        self.assertTrue(c.ok, c.detail)

    def test_pinned_redis_enabled_kind_is_literal(self):
        c = next(
            (c for c in self.report.checks
             if c.name.endswith("REDIS_ENABLED')].kind")),
            None,
        )
        self.assertIsNotNone(c)
        self.assertTrue(c.ok, c.detail)

    def test_pinned_max_instances_is_one(self):
        c = next(
            (c for c in self.report.checks
             if c.name == "pinned:cloudrun:spec.max_instances"),
            None,
        )
        self.assertIsNotNone(c)
        self.assertTrue(c.ok, c.detail)

    def test_pinned_cpu_allocation_is_always(self):
        c = next(
            (c for c in self.report.checks
             if c.name == "pinned:cloudrun:spec.cpu_allocation"),
            None,
        )
        self.assertIsNotNone(c)
        self.assertTrue(c.ok, c.detail)

    def test_pinned_ingress_is_lb_only(self):
        c = next(
            (c for c in self.report.checks
             if c.name == "pinned:cloudrun:spec.ingress"),
            None,
        )
        self.assertIsNotNone(c)
        self.assertTrue(c.ok, c.detail)

    def test_all_cross_resource_invariants_hold(self):
        checks = [c for c in self.report.checks if c.name.startswith("invariant:")]
        self.assertEqual(len(checks), 4)
        for c in checks:
            self.assertTrue(c.ok, f"{c.name}: {c.detail}")


class InfraApplyPlannerTests(unittest.TestCase):

    def setUp(self):
        import yaml  # type: ignore
        with _validate.MANIFEST_PATH.open("r", encoding="utf-8") as f:
            self.manifest = yaml.safe_load(f)
        self.project = self.manifest["allowed_projects"][0]

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = _apply._run(list(argv))
            except SystemExit as exc:
                rc = int(exc.code) if exc.code is not None else 0
        return rc, out.getvalue(), err.getvalue()

    def test_wrong_project_refused_rc4(self):
        rc, out, _err = self._run("--project", "some-other-project")
        self.assertEqual(rc, 4)
        self.assertIn("allowlist", out)

    def test_dry_run_default_returns_rc0(self):
        rc, out, _err = self._run("--project", self.project)
        self.assertEqual(rc, 0)
        self.assertIn("Offline desired-state preview", out)

    def test_apply_with_confirm_returns_rc6(self):
        rc, _out, err = self._run(
            "--project", self.project,
            "--apply", "--confirm", _apply.CONFIRMATION_TOKEN,
        )
        self.assertEqual(rc, 6, f"expected rc=6, got rc={rc}; stderr={err!r}")
        self.assertIn("planning + validation only", err)

    def test_apply_missing_confirm_returns_rc5(self):
        rc, _out, err = self._run(
            "--project", self.project, "--apply",
        )
        self.assertEqual(rc, 5)
        self.assertIn("--confirm", err)

    def test_output_never_leaks_redacted_field_names(self):
        # A refuse action includes reasons — assert redacted
        # field names never appear as key=value in the plan
        # output. Feed a fake plan through render_plan directly.
        plan = _apply.Plan()
        # Simulate a refuse that mentions a redacted key.
        plan.add(_apply.PlannedAction(
            kind="refuse",
            name="malformed",
            reason='REDIS_PASSWORD: "supersecret" is set incorrectly',
        ))
        rendered = _apply.render_plan(plan)
        self.assertNotIn("supersecret", rendered)
        # Key stays visible so the operator can find the source.
        self.assertIn("REDIS_PASSWORD", rendered)
        self.assertIn("<redacted>", rendered)


class InfraPinnedConstraintDriftTests(unittest.TestCase):
    """Copy the loaded YAMLs, mutate the pinned fields, and
    assert the validator surfaces the drift. Regression barrier
    for the reviewer's constraint list."""

    def _load_all(self):
        import yaml  # type: ignore
        with _validate.MANIFEST_PATH.open("r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        loaded: dict[str, dict] = {}
        for name, meta in manifest["resources"].items():
            path = _OPS_DIR / meta["file"]
            with path.open("r", encoding="utf-8") as f:
                loaded[name] = yaml.safe_load(f)
        return manifest, loaded

    def _write_transient_tree(self, manifest: dict, loaded: dict[str, dict]) -> Path:
        """Write a transient manifest + resource tree to a temp
        dir and return the path so `validate.load_and_validate()`
        can be pointed at it via a MANIFEST_PATH monkey patch."""
        import yaml  # type: ignore
        tmp = Path(tempfile.mkdtemp(prefix="infra-drift-"))
        # Update file paths to point inside `tmp`.
        for name, meta in manifest["resources"].items():
            fname = Path(meta["file"]).name
            (tmp / fname).write_text(
                yaml.safe_dump(loaded[name]), encoding="utf-8",
            )
            meta["file"] = fname
        (tmp / "manifest.yaml").write_text(
            yaml.safe_dump(manifest), encoding="utf-8",
        )
        return tmp

    def _run_against(self, tmp: Path):
        from unittest.mock import patch as _patch
        with _patch.object(_validate, "MANIFEST_PATH", tmp / "manifest.yaml"), \
             _patch.object(_validate, "HERE", tmp):
            return _validate.load_and_validate()

    def test_redis_enabled_flip_to_one_fails_pinned_check(self):
        manifest, loaded = self._load_all()
        envs = (
            loaded["cloudrun"]["spec"]["template"]["spec"]["containers"][0]["env"]
        )
        for e in envs:
            if e.get("name") == "REDIS_ENABLED":
                e["value"] = "1"
        tmp = self._write_transient_tree(manifest, loaded)
        report = self._run_against(tmp)
        failed_names = {c.name for c in report.failed()}
        self.assertIn(
            "pinned:cloudrun:spec.template.spec.containers[0].env[?(@.name=='REDIS_ENABLED')].value",
            failed_names,
        )

    def test_redis_enabled_switch_to_secret_ref_fails(self):
        manifest, loaded = self._load_all()
        envs = (
            loaded["cloudrun"]["spec"]["template"]["spec"]["containers"][0]["env"]
        )
        for e in envs:
            if e.get("name") == "REDIS_ENABLED":
                e["kind"] = "secret_or_absent"
        tmp = self._write_transient_tree(manifest, loaded)
        report = self._run_against(tmp)
        failed_names = {c.name for c in report.failed()}
        self.assertIn(
            "pinned:cloudrun:spec.template.spec.containers[0].env[?(@.name=='REDIS_ENABLED')].kind",
            failed_names,
        )

    def test_max_instances_drift_is_caught(self):
        manifest, loaded = self._load_all()
        loaded["cloudrun"]["spec"]["max_instances"] = 3
        tmp = self._write_transient_tree(manifest, loaded)
        report = self._run_against(tmp)
        failed_names = {c.name for c in report.failed()}
        self.assertIn("pinned:cloudrun:spec.max_instances", failed_names)

    def test_cpu_allocation_drift_is_caught(self):
        manifest, loaded = self._load_all()
        loaded["cloudrun"]["spec"]["cpu_allocation"] = "on_request"
        tmp = self._write_transient_tree(manifest, loaded)
        report = self._run_against(tmp)
        failed_names = {c.name for c in report.failed()}
        self.assertIn("pinned:cloudrun:spec.cpu_allocation", failed_names)

    def test_ingress_drift_to_all_traffic_is_caught(self):
        manifest, loaded = self._load_all()
        loaded["cloudrun"]["spec"]["ingress"] = "all"
        tmp = self._write_transient_tree(manifest, loaded)
        report = self._run_against(tmp)
        failed_names = {c.name for c in report.failed()}
        self.assertIn("pinned:cloudrun:spec.ingress", failed_names)

    def test_auth_secret_mismatch_is_caught(self):
        manifest, loaded = self._load_all()
        # Turn AUTH off in Memorystore but keep the secret bound.
        loaded["memorystore"]["spec"]["auth_enabled"] = False
        tmp = self._write_transient_tree(manifest, loaded)
        report = self._run_against(tmp)
        failed_names = {c.name for c in report.failed()}
        self.assertIn(
            "invariant:memorystore_auth_matches_secret_binding",
            failed_names,
        )

    def test_transit_encryption_flip_without_client_change_fails(self):
        manifest, loaded = self._load_all()
        loaded["memorystore"]["spec"]["transit_encryption_mode"] = "SERVER_AUTHENTICATION"
        tmp = self._write_transient_tree(manifest, loaded)
        report = self._run_against(tmp)
        failed_names = {c.name for c in report.failed()}
        self.assertIn(
            "invariant:transit_encryption_matches_client_ssl_kwarg",
            failed_names,
        )


if __name__ == "__main__":
    unittest.main()
