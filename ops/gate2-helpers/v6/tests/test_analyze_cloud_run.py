"""Gate 2 v6 helper — `analyze_cloud_run` fixture-driven tests.

Covers the v5→v6 diff plus PR #36 review round-2 regressions:
  1. `expected_redis` contract (must be "0" or "1"; no defaults).
  2. Literal-string REDIS_ENABLED check with STRICT typing (no
     `str()` coercion; secret-refs rejected).
  3. VPC egress mutual-exclusivity when `expected_redis="1"`,
     **read from the SERVING REVISION not the service template**,
     with structural JSON validation of the
     network-interfaces annotation (rejects `"[]"`, `"not-json"`,
     malformed entries).
  4. AUTH secret-binding vs `REDIS_INSTANCE_HAS_AUTH` — requires
     a valid `valueFrom.secretKeyRef` with non-empty `name` and
     `key`. Plaintext, empty valueFrom, name-only entries refused.
  5. `=0` and `=1` happy paths.

Preserved v5 guards are the responsibility of the v5 test suite;
the enablement PR will import them alongside these v6 tests when
it lands the `_run_v5_guards` copy.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# The helper lives next to this test dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyze_cloud_run import analyze_cloud_run  # noqa: E402


def _revision(
    env_entries: list[dict],
    *,
    connector: str = "",
    direct_subnet_annotation: str = "",  # arbitrary string — for
                                         # malformed-annotation tests
    direct_subnet_valid: str = "",       # sets a well-formed JSON
                                         # annotation for one subnet
) -> dict:
    """Minimal revision_desc. VPC egress annotations now live on
    the revision (per round-2 review), not the service template.
    """
    ann: dict = {}
    if connector:
        ann["run.googleapis.com/vpc-access-connector"] = connector
    if direct_subnet_annotation:
        ann["run.googleapis.com/network-interfaces"] = direct_subnet_annotation
    elif direct_subnet_valid:
        ann["run.googleapis.com/network-interfaces"] = json.dumps([
            {"subnetwork": direct_subnet_valid},
        ])
    return {
        "metadata": {"name": "rev-fixture-01", "annotations": ann},
        "spec": {"containers": [{"env": env_entries}]},
    }


def _service() -> dict:
    """Minimal service_desc — v6 no longer reads VPC from here."""
    return {"spec": {"template": {}}, "status": {}}


def _valid_pw_secret() -> dict:
    return {
        "name": "REDIS_PASSWORD",
        "valueFrom": {
            "secretKeyRef": {"name": "redis-password", "key": "latest"},
        },
    }


class ExpectedRedisContractTests(unittest.TestCase):
    """Contract on the `expected_redis` parameter itself."""

    def test_refuses_missing_expected_redis_via_typeerror(self) -> None:
        """`expected_redis` is keyword-only and has no default —
        omitting it is a Python TypeError, not a silent fallback."""
        with self.assertRaises(TypeError):
            analyze_cloud_run(_service(), _revision([]), stabilization_sec=120)

    def test_refuses_non_literal_expected_redis(self) -> None:
        for bad in ("true", "True", "false", "False", "", "yes", "on", "1.0"):
            with self.subTest(bad=bad):
                _, guards = analyze_cloud_run(
                    _service(), _revision([]), stabilization_sec=120,
                    expected_redis=bad,
                )
                self.assertTrue(
                    any("expected_redis must be '0' or '1'" in g for g in guards),
                    f"expected contract-refusal guard for {bad!r}; got {guards!r}",
                )


class RedisEnabledLiteralTests(unittest.TestCase):
    """v6 rejects v5's truthy fallbacks (`"false"`, `"True"`, etc.)
    on the REDIS_ENABLED env value itself, PLUS non-string types
    and secret-ref entries."""

    def test_expected_0_accepts_literal_0(self) -> None:
        _, guards = analyze_cloud_run(
            _service(), _revision([{"name": "REDIS_ENABLED", "value": "0"}]),
            stabilization_sec=120, expected_redis="0",
        )
        self.assertNotIn(
            "REDIS_ENABLED must be exactly '0'",
            " ".join(guards),
            f"literal '0' should pass; guards={guards!r}",
        )

    def test_expected_0_rejects_true_string(self) -> None:
        _, guards = analyze_cloud_run(
            _service(), _revision([{"name": "REDIS_ENABLED", "value": "true"}]),
            stabilization_sec=120, expected_redis="0",
        )
        self.assertTrue(
            any(
                "REDIS_ENABLED must be exactly '0'" in g and "'true'" in g
                for g in guards
            ),
            f"expected literal-mismatch guard; got {guards!r}",
        )

    def test_expected_0_rejects_false_string(self) -> None:
        """v5 accepted `"false"` / `"False"` as unblocked. v6 does
        not — the operator writes literal `"0"` or the check fails."""
        _, guards = analyze_cloud_run(
            _service(), _revision([{"name": "REDIS_ENABLED", "value": "false"}]),
            stabilization_sec=120, expected_redis="0",
        )
        self.assertTrue(
            any("REDIS_ENABLED must be exactly '0'" in g for g in guards),
        )

    def test_expected_1_accepts_literal_1(self) -> None:
        # `=1` needs the full happy-path fixture (VPC + AUTH).
        env = [
            {"name": "REDIS_ENABLED", "value": "1"},
            _valid_pw_secret(),
        ]
        rev = _revision(env, direct_subnet_valid="projects/x/regions/us-central1/subnetworks/redis-egress")
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), rev,
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertNotIn(
            "REDIS_ENABLED must be exactly '1'",
            " ".join(guards),
            f"literal '1' should pass; guards={guards!r}",
        )

    def test_expected_1_rejects_literal_0(self) -> None:
        env = [{"name": "REDIS_ENABLED", "value": "0"}]
        rev = _revision(env, direct_subnet_valid="projects/x/regions/us-central1/subnetworks/redis-egress")
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "0"}):
            _, guards = analyze_cloud_run(
                _service(), rev,
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any("REDIS_ENABLED must be exactly '1'" in g for g in guards),
        )

    # -- Round-2 regressions: strict typing --

    def test_rejects_numeric_zero(self) -> None:
        """Reviewer's exact fixture — a JSON integer 0 in the env
        would coerce to '0' under v5's implicit `str()` and
        silently pass. v6 refuses it."""
        env = [{"name": "REDIS_ENABLED", "value": 0}]  # int, not "0"
        _, guards = analyze_cloud_run(
            _service(), _revision(env),
            stabilization_sec=120, expected_redis="0",
        )
        self.assertTrue(
            any("REDIS_ENABLED must be a literal STRING" in g and "int" in g for g in guards),
            f"expected type-refusal guard; got {guards!r}",
        )

    def test_rejects_numeric_one(self) -> None:
        env = [{"name": "REDIS_ENABLED", "value": 1}]
        _, guards = analyze_cloud_run(
            _service(), _revision(env),
            stabilization_sec=120, expected_redis="1",
        )
        self.assertTrue(
            any("REDIS_ENABLED must be a literal STRING" in g for g in guards),
        )

    def test_rejects_bool_true(self) -> None:
        env = [{"name": "REDIS_ENABLED", "value": True}]
        _, guards = analyze_cloud_run(
            _service(), _revision(env),
            stabilization_sec=120, expected_redis="1",
        )
        self.assertTrue(
            any("REDIS_ENABLED must be a literal STRING" in g and "bool" in g for g in guards),
        )

    def test_rejects_secret_ref(self) -> None:
        """REDIS_ENABLED as a secretKeyRef entry — meaningless for
        a boolean flag; must fail with a distinct guard so ops
        knows the manifest is misconfigured."""
        env = [{
            "name": "REDIS_ENABLED",
            "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}},
        }]
        _, guards = analyze_cloud_run(
            _service(), _revision(env),
            stabilization_sec=120, expected_redis="1",
        )
        self.assertTrue(
            any("secret ref" in g.lower() for g in guards),
            f"expected secret-ref refusal; got {guards!r}",
        )

    def test_rejects_missing_env(self) -> None:
        """No REDIS_ENABLED entry at all — must not silently pass."""
        _, guards = analyze_cloud_run(
            _service(), _revision([]),
            stabilization_sec=120, expected_redis="0",
        )
        self.assertTrue(
            any("REDIS_ENABLED is missing" in g for g in guards),
        )


class VPCEgressExclusivityTests(unittest.TestCase):
    """Exactly one of connector / Direct VPC egress per
    PR #31 §4c step 2. Read from the SERVING REVISION per round-2
    review of PR #36."""

    def _envs(self):
        return [
            {"name": "REDIS_ENABLED", "value": "1"},
            _valid_pw_secret(),
        ]

    def test_direct_happy_path_no_connector(self) -> None:
        rev = _revision(self._envs(), direct_subnet_valid="projects/x/regions/us-central1/subnetworks/redis-egress")
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), rev,
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertEqual(
            [g for g in guards if "VPC" in g or "connector" in g or "egress" in g or "network-interfaces" in g],
            [],
        )

    def test_connector_happy_path_no_direct(self) -> None:
        rev = _revision(
            self._envs(),
            connector="projects/x/locations/us-central1/connectors/redis-conn",
        )
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), rev,
                stabilization_sec=120,
                expected_redis="1", vpc_mode="connector",
            )
        self.assertEqual(
            [g for g in guards if "VPC" in g or "connector" in g or "egress" in g or "network-interfaces" in g],
            [],
        )

    def test_connector_missing_when_mode_is_connector(self) -> None:
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), _revision(self._envs()),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="connector",
            )
        self.assertTrue(
            any("no vpc-access-connector attached to the serving revision" in g for g in guards),
        )

    def test_direct_missing_when_mode_is_direct(self) -> None:
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), _revision(self._envs()),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any("no valid egress subnet attached" in g for g in guards),
        )

    def test_both_attached_is_stop(self) -> None:
        rev = _revision(
            self._envs(),
            connector="projects/x/locations/us-central1/connectors/redis-conn",
            direct_subnet_valid="projects/x/regions/us-central1/subnetworks/redis-egress",
        )
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), rev,
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        # Either mode surfaces the "both attached" stop.
        self.assertTrue(
            any("both" in g and "attached" in g for g in guards),
            f"expected both-attached stop; got {guards!r}",
        )

    def test_vpc_mode_none_is_stop(self) -> None:
        rev = _revision(self._envs(), direct_subnet_valid="projects/x/regions/us-central1/subnetworks/redis-egress")
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), rev,
                stabilization_sec=120,
                expected_redis="1", vpc_mode=None,
            )
        self.assertTrue(
            any("GATE2_VPC_MODE must be" in g for g in guards),
        )

    # -- Round-2 regressions: read from serving revision + parse
    #    the network-interfaces annotation structurally --

    def test_template_only_vpc_does_not_pass(self) -> None:
        """Reviewer's exact fixture — the SERVICE template has the
        VPC annotation but the SERVING REVISION does not. v5 read
        the template and passed; v6 must fail because the revision
        that's actually serving traffic has no VPC attachment."""
        svc = {
            "spec": {"template": {"metadata": {"annotations": {
                "run.googleapis.com/network-interfaces": json.dumps([
                    {"subnetwork": "projects/x/regions/us-central1/subnetworks/only-on-template"},
                ]),
            }}}},
            "status": {},
        }
        rev = _revision(self._envs())  # NO annotations on the revision
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                svc, rev,
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any("no valid egress subnet attached" in g for g in guards),
            f"template-only VPC must NOT satisfy the serving-revision "
            f"check; got {guards!r}",
        )

    def test_network_interfaces_empty_list_is_stop(self) -> None:
        """Reviewer's fixture — `"[]"` used to satisfy the truthy
        parser. Now the empty list is a stop."""
        rev = _revision(self._envs(), direct_subnet_annotation="[]")
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), rev,
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any("empty list" in g for g in guards),
            f"empty network-interfaces list must fail; got {guards!r}",
        )

    def test_network_interfaces_not_json_is_stop(self) -> None:
        """Reviewer's fixture — a non-JSON annotation used to pass
        because the truthy check only looked at string emptiness."""
        rev = _revision(self._envs(), direct_subnet_annotation="not-json")
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), rev,
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any("not valid JSON" in g for g in guards),
        )

    def test_network_interfaces_missing_subnetwork_is_stop(self) -> None:
        rev = _revision(
            self._envs(),
            direct_subnet_annotation=json.dumps([{"network": "default"}]),
        )
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), rev,
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any(
                "subnetwork must be a non-empty string" in g
                for g in guards
            ),
        )

    def test_network_interfaces_wrong_top_level_type_is_stop(self) -> None:
        """A JSON object instead of a JSON list."""
        rev = _revision(
            self._envs(),
            direct_subnet_annotation=json.dumps({"subnetwork": "x"}),
        )
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), rev,
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any("must be a JSON list" in g for g in guards),
        )


class AuthSecretBindingTests(unittest.TestCase):
    """AUTH state is operator-supplied via `REDIS_INSTANCE_HAS_AUTH`.
    Binding structure is validated strictly per round-2 review."""

    def _direct_rev(self, env_entries):
        return _revision(
            env_entries,
            direct_subnet_valid="projects/x/regions/us-central1/subnetworks/redis-egress",
        )

    def test_missing_env_var_is_stop(self) -> None:
        env = [
            {"name": "REDIS_ENABLED", "value": "1"},
            _valid_pw_secret(),
        ]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_INSTANCE_HAS_AUTH", None)
            _, guards = analyze_cloud_run(
                _service(), self._direct_rev(env),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any("REDIS_INSTANCE_HAS_AUTH must be" in g for g in guards),
        )

    def test_auth_on_but_no_password_binding(self) -> None:
        env = [{"name": "REDIS_ENABLED", "value": "1"}]  # no REDIS_PASSWORD
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), self._direct_rev(env),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any(
                "REDIS_INSTANCE_HAS_AUTH=1" in g
                and "not bound" in g
                for g in guards
            ),
        )

    def test_auth_off_but_stale_password_binding(self) -> None:
        env = [
            {"name": "REDIS_ENABLED", "value": "1"},
            _valid_pw_secret(),
        ]
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "0"}):
            _, guards = analyze_cloud_run(
                _service(), self._direct_rev(env),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any(
                "REDIS_INSTANCE_HAS_AUTH=0" in g
                and "stale" in g
                for g in guards
            ),
        )

    # -- Round-2 regressions: strict secret-ref validation --

    def test_plaintext_password_refused_when_auth_on(self) -> None:
        """Reviewer's fixture — `{"name": "REDIS_PASSWORD",
        "value": "..."}` used to pass. Now must fail."""
        env = [
            {"name": "REDIS_ENABLED", "value": "1"},
            {"name": "REDIS_PASSWORD", "value": ""},  # plaintext, even empty
        ]
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), self._direct_rev(env),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any("plaintext" in g for g in guards),
            f"expected plaintext refusal; got {guards!r}",
        )

    def test_plaintext_nonempty_password_refused_when_auth_on(self) -> None:
        env = [
            {"name": "REDIS_ENABLED", "value": "1"},
            {"name": "REDIS_PASSWORD", "value": "hunter2"},
        ]
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), self._direct_rev(env),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(any("plaintext" in g for g in guards))

    def test_name_only_password_entry_refused_when_auth_on(self) -> None:
        """Reviewer's fixture — a name-only entry (no value, no
        valueFrom) used to pass. Now must fail."""
        env = [
            {"name": "REDIS_ENABLED", "value": "1"},
            {"name": "REDIS_PASSWORD"},  # name-only
        ]
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), self._direct_rev(env),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any("no valueFrom section" in g for g in guards),
            f"expected valueFrom-missing refusal; got {guards!r}",
        )

    def test_secret_ref_empty_name_refused(self) -> None:
        env = [
            {"name": "REDIS_ENABLED", "value": "1"},
            {
                "name": "REDIS_PASSWORD",
                "valueFrom": {"secretKeyRef": {"name": "", "key": "k"}},
            },
        ]
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), self._direct_rev(env),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any("secretKeyRef.name must be a non-empty string" in g for g in guards),
        )

    def test_secret_ref_empty_key_refused(self) -> None:
        env = [
            {"name": "REDIS_ENABLED", "value": "1"},
            {
                "name": "REDIS_PASSWORD",
                "valueFrom": {"secretKeyRef": {"name": "s", "key": ""}},
            },
        ]
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), self._direct_rev(env),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any("secretKeyRef.key must be a non-empty string" in g for g in guards),
        )

    def test_secret_ref_missing_secretKeyRef_refused(self) -> None:
        env = [
            {"name": "REDIS_ENABLED", "value": "1"},
            {"name": "REDIS_PASSWORD", "valueFrom": {}},  # no secretKeyRef
        ]
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), self._direct_rev(env),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any("no secretKeyRef object" in g for g in guards),
        )


class HappyPathTests(unittest.TestCase):
    """The full-fixture pass cases — no guards at all when
    every input is aligned."""

    def test_happy_path_expected_0(self) -> None:
        _, guards = analyze_cloud_run(
            _service(),
            _revision([{"name": "REDIS_ENABLED", "value": "0"}]),
            stabilization_sec=120,
            expected_redis="0",
        )
        self.assertEqual(
            guards, [],
            f"expected no guards on =0 happy path; got {guards!r}",
        )

    def test_happy_path_expected_1_direct(self) -> None:
        env = [
            {"name": "REDIS_ENABLED", "value": "1"},
            _valid_pw_secret(),
        ]
        rev = _revision(env, direct_subnet_valid="projects/x/regions/us-central1/subnetworks/redis-egress")
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), rev,
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertEqual(
            guards, [],
            f"expected no guards on =1 direct-VPC happy path; got {guards!r}",
        )

    def test_happy_path_expected_1_connector(self) -> None:
        env = [
            {"name": "REDIS_ENABLED", "value": "1"},
            _valid_pw_secret(),
        ]
        rev = _revision(
            env,
            connector="projects/x/locations/us-central1/connectors/redis-conn",
        )
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(), rev,
                stabilization_sec=120,
                expected_redis="1", vpc_mode="connector",
            )
        self.assertEqual(guards, [])


if __name__ == "__main__":
    unittest.main()
