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

# Renamed from `validate`/`apply` to unique module names because the
# monitoring test suite (`test_redis_monitoring_config.py`) imports
# its own modules under the SAME bare names from
# `ops/monitoring/redis-fanout/`. Whichever test file ran first
# would win the `sys.modules` cache and break the other suite in
# CI — the bug caught between round-1 push and round-2 fix here.
import infra_validate as _validate  # noqa: E402
import infra_apply as _apply  # noqa: E402


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
        # 6 invariants after PR #40 round-3: the four original
        # cross-resource pairings plus
        # `memorystore_declaration_is_fail_closed` (round-2) plus
        # `cloudrun_redis_password_binding_is_valid` (round-3).
        checks = [c for c in self.report.checks if c.name.startswith("invariant:")]
        self.assertEqual(len(checks), 6)
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


class InfraVpcDeepValidationTests(unittest.TestCase):
    """Reviewer's PR #40 round-2 blocker #1: the earlier VPC
    checker only checked that Direct JSON was nonempty. Now it
    parses the JSON, cross-checks network/subnet, requires
    Private Google Access, and rejects mixed / partial-mode
    configurations. Each test mutates one field to break one
    invariant."""

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

    def _run_against(self, manifest: dict, loaded: dict[str, dict]):
        import yaml  # type: ignore
        from unittest.mock import patch as _patch
        tmp = Path(tempfile.mkdtemp(prefix="infra-vpc-"))
        for name, meta in manifest["resources"].items():
            fname = Path(meta["file"]).name
            (tmp / fname).write_text(
                yaml.safe_dump(loaded[name]), encoding="utf-8",
            )
            meta["file"] = fname
        (tmp / "manifest.yaml").write_text(
            yaml.safe_dump(manifest), encoding="utf-8",
        )
        with _patch.object(_validate, "MANIFEST_PATH", tmp / "manifest.yaml"), \
             _patch.object(_validate, "HERE", tmp):
            return _validate.load_and_validate()

    def _mutate_and_expect_fail(self, mutator, expected_substr: str):
        manifest, loaded = self._load_all()
        mutator(loaded)
        report = self._run_against(manifest, loaded)
        vpc_check = next(
            (c for c in report.checks
             if c.name == "invariant:vpc_mode_matches_cloudrun_annotations"),
            None,
        )
        self.assertIsNotNone(vpc_check, "VPC invariant check did not run")
        self.assertFalse(vpc_check.ok, "expected VPC invariant to fail")
        self.assertIn(expected_substr, vpc_check.detail)

    def test_malformed_json_in_direct_egress_fails(self):
        self._mutate_and_expect_fail(
            lambda L: L["vpc"]["spec"]["direct_egress"].__setitem__(
                "network_interfaces_json", "{not json"),
            "not valid JSON",
        )

    def test_direct_egress_network_mismatch_fails(self):
        self._mutate_and_expect_fail(
            lambda L: L["vpc"]["spec"].__setitem__("network", "other-network"),
            "network",
        )

    def test_direct_egress_subnet_mismatch_fails(self):
        self._mutate_and_expect_fail(
            lambda L: L["vpc"]["spec"].__setitem__("subnet", "other-subnet"),
            "subnetwork",
        )

    def test_direct_egress_without_private_google_access_fails(self):
        self._mutate_and_expect_fail(
            lambda L: L["vpc"]["spec"]["direct_egress"].__setitem__(
                "private_google_access", False),
            "private_google_access",
        )

    def test_mixed_mode_fails_when_both_blocks_populated(self):
        def _mutate(L):
            L["vpc"]["spec"]["serverless_connector"]["name"] = "some-real-connector"
        self._mutate_and_expect_fail(_mutate, "pick one mode")

    def test_serverless_mode_requires_connector_annotation(self):
        """When active_mode=serverless_connector, cloudrun MUST
        carry `run.googleapis.com/vpc-access-connector`
        annotation. The current cloudrun.yaml has no such
        annotation (it's authored for direct mode), so switching
        vpc.active_mode alone MUST fail — the operator has to
        update BOTH files together."""
        def _mutate(L):
            L["vpc"]["metadata"]["active_mode"] = "serverless_connector"
            L["vpc"]["spec"]["serverless_connector"]["name"] = "connector-1"
            L["vpc"]["spec"]["direct_egress"]["network_interfaces_json"] = ""
        self._mutate_and_expect_fail(
            _mutate, "vpc-access-connector",
        )

    def test_direct_mode_rejects_connector_annotation(self):
        def _mutate(L):
            annots = (
                L["cloudrun"]["spec"]["template"]["metadata"]
                ["required_template_annotations"]
            )
            annots.append({
                "key": "run.googleapis.com/vpc-access-connector",
                "value": "stale-connector",
            })
        self._mutate_and_expect_fail(_mutate, "vpc-access-connector")


class InfraMemorystoreDeclarationTests(unittest.TestCase):
    """Reviewer's PR #40 round-2 blocker #2: prior validator did
    not enforce STANDARD_HA, strict Boolean types, or the full
    Secret Manager reference. These regression barriers rule out
    the failure modes named in the review."""

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

    def _run_against(self, manifest: dict, loaded: dict[str, dict]):
        import yaml  # type: ignore
        from unittest.mock import patch as _patch
        tmp = Path(tempfile.mkdtemp(prefix="infra-mem-"))
        for name, meta in manifest["resources"].items():
            fname = Path(meta["file"]).name
            (tmp / fname).write_text(
                yaml.safe_dump(loaded[name]), encoding="utf-8",
            )
            meta["file"] = fname
        (tmp / "manifest.yaml").write_text(
            yaml.safe_dump(manifest), encoding="utf-8",
        )
        with _patch.object(_validate, "MANIFEST_PATH", tmp / "manifest.yaml"), \
             _patch.object(_validate, "HERE", tmp):
            return _validate.load_and_validate()

    def _fails_with(self, check_name: str, substr: str, mutator):
        manifest, loaded = self._load_all()
        mutator(loaded)
        report = self._run_against(manifest, loaded)
        c = next((x for x in report.checks if x.name == check_name), None)
        self.assertIsNotNone(c, f"{check_name!r} did not run")
        self.assertFalse(c.ok, f"{check_name!r} unexpectedly passed")
        self.assertIn(substr, c.detail)

    def test_basic_tier_is_rejected(self):
        self._fails_with(
            "invariant:memorystore_declaration_is_fail_closed",
            "STANDARD_HA",
            lambda L: L["memorystore"]["spec"].__setitem__("tier", "BASIC"),
        )

    def test_auth_enabled_string_false_is_rejected(self):
        """Prior `bool(...)` coercion accepted "false" (string) as
        True. Strict-type check rejects it."""
        self._fails_with(
            "invariant:memorystore_auth_matches_secret_binding",
            "must be a YAML boolean literal",
            lambda L: L["memorystore"]["spec"].__setitem__(
                "auth_enabled", "false"),
        )

    def test_present_string_false_is_rejected(self):
        self._fails_with(
            "invariant:memorystore_auth_matches_secret_binding",
            "must be a YAML boolean literal",
            lambda L: L["secrets"]["spec"]["redis_password"].__setitem__(
                "present", "false"),
        )

    def test_missing_secret_key_ref_is_rejected(self):
        def _mutate(L):
            L["secrets"]["spec"]["redis_password"].pop("secret_key_ref", None)
        self._fails_with(
            "invariant:memorystore_auth_matches_secret_binding",
            "secret_key_ref",
            _mutate,
        )

    def test_secret_key_ref_missing_key_field(self):
        def _mutate(L):
            L["secrets"]["spec"]["redis_password"]["secret_key_ref"].pop("key")
        self._fails_with(
            "invariant:memorystore_auth_matches_secret_binding",
            "secret_key_ref.key",
            _mutate,
        )

    def test_redis_version_off_pin_fails(self):
        self._fails_with(
            "invariant:memorystore_declaration_is_fail_closed",
            "redis_version",
            lambda L: L["memorystore"]["spec"].__setitem__(
                "redis_version", "REDIS_6_X"),
        )

    def test_memory_size_gb_zero_fails(self):
        self._fails_with(
            "invariant:memorystore_declaration_is_fail_closed",
            "memory_size_gb",
            lambda L: L["memorystore"]["spec"].__setitem__(
                "memory_size_gb", 0),
        )

    def test_region_off_us_central1_fails(self):
        self._fails_with(
            "invariant:memorystore_declaration_is_fail_closed",
            "region",
            lambda L: L["memorystore"]["metadata"].__setitem__(
                "region", "us-east1"),
        )


class InfraServerlessConnectorBindingTests(unittest.TestCase):
    """Reviewer's PR #40 round-3 blocker #1: the serverless-mode
    branch only checked that the annotation KEY existed — an
    empty or wrong-shaped `value` / `value_from` slipped through.
    Regression barrier now pins the annotation to
    `value_from: vpc.serverless_connector.name`."""

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

    def _configure_serverless_mode(self, loaded: dict[str, dict], *, annotation: dict):
        """Put the config in serverless_connector mode with a
        supplied Cloud Run annotation for the connector key."""
        loaded["vpc"]["metadata"]["active_mode"] = "serverless_connector"
        loaded["vpc"]["spec"]["serverless_connector"]["name"] = "worshiptranslate-conn"
        # Empty direct so the mixed-mode check doesn't fire first.
        loaded["vpc"]["spec"]["direct_egress"]["network_interfaces_json"] = ""
        # Replace the network-interfaces annotation with the
        # connector annotation the test wants.
        annots = (
            loaded["cloudrun"]["spec"]["template"]["metadata"]
            ["required_template_annotations"]
        )
        loaded["cloudrun"]["spec"]["template"]["metadata"][
            "required_template_annotations"] = [
            a for a in annots
            if a.get("key") != "run.googleapis.com/network-interfaces"
        ]
        loaded["cloudrun"]["spec"]["template"]["metadata"][
            "required_template_annotations"].append(annotation)

    def _run_against(self, manifest: dict, loaded: dict[str, dict]):
        import yaml  # type: ignore
        from unittest.mock import patch as _patch
        tmp = Path(tempfile.mkdtemp(prefix="infra-serverless-"))
        for name, meta in manifest["resources"].items():
            fname = Path(meta["file"]).name
            (tmp / fname).write_text(
                yaml.safe_dump(loaded[name]), encoding="utf-8",
            )
            meta["file"] = fname
        (tmp / "manifest.yaml").write_text(
            yaml.safe_dump(manifest), encoding="utf-8",
        )
        with _patch.object(_validate, "MANIFEST_PATH", tmp / "manifest.yaml"), \
             _patch.object(_validate, "HERE", tmp):
            return _validate.load_and_validate()

    def _vpc_check(self, report):
        return next(
            (c for c in report.checks
             if c.name == "invariant:vpc_mode_matches_cloudrun_annotations"),
            None,
        )

    def test_connector_annotation_bound_via_value_from_passes(self):
        manifest, loaded = self._load_all()
        self._configure_serverless_mode(loaded, annotation={
            "key": "run.googleapis.com/vpc-access-connector",
            "value_from": "vpc.serverless_connector.name",
        })
        report = self._run_against(manifest, loaded)
        c = self._vpc_check(report)
        self.assertIsNotNone(c)
        self.assertTrue(c.ok, f"correct binding rejected: {c.detail}")

    def test_connector_annotation_empty_value_from_fails(self):
        manifest, loaded = self._load_all()
        self._configure_serverless_mode(loaded, annotation={
            "key": "run.googleapis.com/vpc-access-connector",
            "value_from": "",  # empty
        })
        report = self._run_against(manifest, loaded)
        c = self._vpc_check(report)
        self.assertFalse(c.ok)
        self.assertIn("value_from", c.detail)

    def test_connector_annotation_wrong_value_from_fails(self):
        manifest, loaded = self._load_all()
        self._configure_serverless_mode(loaded, annotation={
            "key": "run.googleapis.com/vpc-access-connector",
            "value_from": "vpc.direct_egress.network_interfaces_json",
        })
        report = self._run_against(manifest, loaded)
        c = self._vpc_check(report)
        self.assertFalse(c.ok)
        self.assertIn("value_from", c.detail)

    def test_connector_annotation_literal_value_fails(self):
        """A literal `value` alongside value_from creates two
        sources of truth — refuse. Direct literal only, no
        value_from, also refused."""
        manifest, loaded = self._load_all()
        self._configure_serverless_mode(loaded, annotation={
            "key": "run.googleapis.com/vpc-access-connector",
            "value": "projects/x/locations/us-central1/connectors/foo",
        })
        report = self._run_against(manifest, loaded)
        c = self._vpc_check(report)
        self.assertFalse(c.ok)
        # Either the value_from-missing check or the literal-value
        # check surfaces — both reject this shape.
        self.assertTrue(
            "value_from" in c.detail or "literal" in c.detail,
            c.detail,
        )


class InfraCloudRunRedisPasswordBindingTests(unittest.TestCase):
    """Reviewer's PR #40 round-3 blocker #2: without an
    end-to-end binding check, a plaintext REDIS_PASSWORD, a
    mis-typed `value_from`, or a `kind=literal` could pass
    validation. Task #137 would deploy the wrong secret.

    Also covers the tightened secret_name↔secret_key_ref.name
    and version↔secret_key_ref.key agreement in
    _check_auth_matches_secret."""

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

    def _run_against(self, manifest: dict, loaded: dict[str, dict]):
        import yaml  # type: ignore
        from unittest.mock import patch as _patch
        tmp = Path(tempfile.mkdtemp(prefix="infra-secret-"))
        for name, meta in manifest["resources"].items():
            fname = Path(meta["file"]).name
            (tmp / fname).write_text(
                yaml.safe_dump(loaded[name]), encoding="utf-8",
            )
            meta["file"] = fname
        (tmp / "manifest.yaml").write_text(
            yaml.safe_dump(manifest), encoding="utf-8",
        )
        with _patch.object(_validate, "MANIFEST_PATH", tmp / "manifest.yaml"), \
             _patch.object(_validate, "HERE", tmp):
            return _validate.load_and_validate()

    def _env(self, loaded: dict, name: str):
        for e in loaded["cloudrun"]["spec"]["template"]["spec"]["containers"][0]["env"]:
            if e.get("name") == name:
                return e
        return None

    def _fails_with(self, expected_check: str, substr: str, mutator):
        manifest, loaded = self._load_all()
        mutator(loaded)
        report = self._run_against(manifest, loaded)
        c = next((x for x in report.checks if x.name == expected_check), None)
        self.assertIsNotNone(c, f"{expected_check!r} did not run")
        self.assertFalse(c.ok, f"{expected_check!r} unexpectedly passed")
        self.assertIn(substr, c.detail)

    def test_redis_password_kind_literal_is_rejected(self):
        def _mutate(L):
            e = self._env(L, "REDIS_PASSWORD")
            e["kind"] = "literal"
        self._fails_with(
            "invariant:cloudrun_redis_password_binding_is_valid",
            "secret_or_absent",
            _mutate,
        )

    def test_redis_password_plaintext_value_is_rejected(self):
        def _mutate(L):
            e = self._env(L, "REDIS_PASSWORD")
            e["value"] = "hunter2"
        self._fails_with(
            "invariant:cloudrun_redis_password_binding_is_valid",
            "literal `value`",
            _mutate,
        )

    def test_redis_password_wrong_value_from_is_rejected(self):
        def _mutate(L):
            e = self._env(L, "REDIS_PASSWORD")
            e["value_from"] = "secrets.some_other_secret"
        self._fails_with(
            "invariant:cloudrun_redis_password_binding_is_valid",
            "value_from",
            _mutate,
        )

    def test_secret_name_disagrees_with_secret_key_ref_name(self):
        def _mutate(L):
            L["secrets"]["spec"]["redis_password"]["secret_name"] = "wrong-name"
        self._fails_with(
            "invariant:memorystore_auth_matches_secret_binding",
            "secret_name",
            _mutate,
        )

    def test_version_disagrees_with_secret_key_ref_key(self):
        def _mutate(L):
            L["secrets"]["spec"]["redis_password"]["version"] = "5"
        self._fails_with(
            "invariant:memorystore_auth_matches_secret_binding",
            "version",
            _mutate,
        )


class InfraMemorySizeBoolIsIntTests(unittest.TestCase):
    """Reviewer's PR #40 round-3 small-hardening item:
    `isinstance(True, int)` returns True in Python. Explicit
    bool rejection prevents a stray `memory_size_gb: true`
    from passing."""

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

    def _run_against(self, manifest: dict, loaded: dict[str, dict]):
        import yaml  # type: ignore
        from unittest.mock import patch as _patch
        tmp = Path(tempfile.mkdtemp(prefix="infra-int-"))
        for name, meta in manifest["resources"].items():
            fname = Path(meta["file"]).name
            (tmp / fname).write_text(
                yaml.safe_dump(loaded[name]), encoding="utf-8",
            )
            meta["file"] = fname
        (tmp / "manifest.yaml").write_text(
            yaml.safe_dump(manifest), encoding="utf-8",
        )
        with _patch.object(_validate, "MANIFEST_PATH", tmp / "manifest.yaml"), \
             _patch.object(_validate, "HERE", tmp):
            return _validate.load_and_validate()

    def test_memory_size_gb_true_is_rejected(self):
        manifest, loaded = self._load_all()
        loaded["memorystore"]["spec"]["memory_size_gb"] = True
        report = self._run_against(manifest, loaded)
        c = next(
            (x for x in report.checks
             if x.name == "invariant:memorystore_declaration_is_fail_closed"),
            None,
        )
        self.assertIsNotNone(c)
        self.assertFalse(c.ok, "bool `True` passed the int check")
        self.assertIn("bool excluded", c.detail)


class InfraApplyControlFlowTests(unittest.TestCase):
    """Reviewer's PR #40 round-2 blocker #3: `--apply` returned
    rc=6 BEFORE build_plan, so `--apply --project unauthorized`
    bypassed the allowlist entirely. Task #137 would inherit
    that unsafe control flow. These tests lock in the corrected
    ordering:
      rc=5 — --apply w/o correct --confirm
      rc=4 — allowlist / static-check refusal
      rc=6 — clean config, --apply intentionally deferred
      rc=0 — clean plan without --apply
    """

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = _apply._run(list(argv))
            except SystemExit as exc:
                rc = int(exc.code) if exc.code is not None else 0
        return rc, out.getvalue(), err.getvalue()

    def test_apply_with_unauthorized_project_returns_rc4_not_rc6(self):
        """The regression: pre-fix, `--apply --project bad`
        returned rc=6 because the SystemExit fired above
        build_plan. Now allowlist rejection wins."""
        rc, out, _err = self._run(
            "--project", "some-other-project",
            "--apply", "--confirm", _apply.CONFIRMATION_TOKEN,
        )
        self.assertEqual(rc, 4, f"expected rc=4, got rc={rc}")
        self.assertIn("allowlist", out)

    def test_apply_with_missing_confirm_takes_precedence_over_allowlist(self):
        """--confirm is an INPUT error and returns rc=5 even
        when the project is bad — the operator's typo is the
        first thing to fix."""
        rc, _out, err = self._run(
            "--project", "some-other-project", "--apply",
        )
        self.assertEqual(rc, 5)
        self.assertIn("--confirm", err)


if __name__ == "__main__":
    unittest.main()
