"""Gate 2 v6 helper — `analyze_cloud_run` fixture-driven tests.

Covers ONLY the v5→v6 diff (per the spec at
`docs/03-analysis/gate2-helpers-v6-spec.md`):
  1. `expected_redis` contract (must be "0" or "1"; no defaults).
  2. Literal-string REDIS_ENABLED check (no truthy fallbacks).
  3. VPC egress mutual-exclusivity when `expected_redis="1"`.
  4. AUTH secret-binding vs `REDIS_INSTANCE_HAS_AUTH`.
  5. `=0` and `=1` happy paths.

Preserved v5 guards (traffic split, latestReady == latestCreated,
Ready + seconds-Ready, ROOM_RECONCILER_ENABLED, max-instances)
are the responsibility of the v5 test suite; the enablement PR
will import them alongside these v6 tests when it lands the
`_run_v5_guards` copy.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# The helper lives next to this test dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyze_cloud_run import analyze_cloud_run  # noqa: E402


def _revision(env_entries: list[dict]) -> dict:
    """Minimal revision_desc — v6 only inspects `spec.containers[0].env`."""
    return {
        "metadata": {"name": "rev-fixture-01"},
        "spec": {"containers": [{"env": env_entries}]},
    }


def _service(
    *,
    connector: str = "",
    direct_subnet: str = "",
) -> dict:
    """Minimal service_desc — v6 only inspects the VPC annotations
    and (via `_run_v5_guards` stub) nothing else in this branch."""
    ann: dict = {}
    if connector:
        ann["run.googleapis.com/vpc-access-connector"] = connector
    if direct_subnet:
        ann["run.googleapis.com/network-interfaces"] = direct_subnet
    return {
        "spec": {"template": {"metadata": {"annotations": ann}}},
        "status": {},
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
    on the REDIS_ENABLED env value itself."""

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
            {"name": "REDIS_PASSWORD",
             "valueFrom": {"secretKeyRef": {"name": "redis-password", "key": "latest"}}},
        ]
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(direct_subnet="projects/x/regions/us-central1/subnetworks/redis-egress"),
                _revision(env),
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
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "0"}):
            _, guards = analyze_cloud_run(
                _service(direct_subnet="projects/x/regions/us-central1/subnetworks/redis-egress"),
                _revision(env),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any("REDIS_ENABLED must be exactly '1'" in g for g in guards),
        )


class VPCEgressExclusivityTests(unittest.TestCase):
    """Exactly one of connector / Direct VPC egress per
    PR #31 §4c step 2."""

    def _envs(self):
        return [
            {"name": "REDIS_ENABLED", "value": "1"},
            {"name": "REDIS_PASSWORD",
             "valueFrom": {"secretKeyRef": {"name": "redis-password", "key": "latest"}}},
        ]

    def test_direct_happy_path_no_connector(self) -> None:
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(direct_subnet="projects/x/regions/us-central1/subnetworks/redis-egress"),
                _revision(self._envs()),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertEqual(
            [g for g in guards if "VPC" in g or "connector" in g or "egress" in g],
            [],
        )

    def test_connector_happy_path_no_direct(self) -> None:
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(connector="projects/x/locations/us-central1/connectors/redis-conn"),
                _revision(self._envs()),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="connector",
            )
        self.assertEqual(
            [g for g in guards if "VPC" in g or "connector" in g or "egress" in g],
            [],
        )

    def test_connector_missing_when_mode_is_connector(self) -> None:
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(),  # neither attached
                _revision(self._envs()),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="connector",
            )
        self.assertTrue(
            any("no vpc-access-connector attached" in g for g in guards),
        )

    def test_direct_missing_when_mode_is_direct(self) -> None:
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(),
                _revision(self._envs()),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        self.assertTrue(
            any("no egress subnet attached" in g for g in guards),
        )

    def test_both_attached_is_stop(self) -> None:
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(
                    connector="projects/x/locations/us-central1/connectors/redis-conn",
                    direct_subnet="projects/x/regions/us-central1/subnetworks/redis-egress",
                ),
                _revision(self._envs()),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="direct",
            )
        # Either mode surfaces the "both attached" stop.
        self.assertTrue(
            any("both" in g and "attached" in g for g in guards),
            f"expected both-attached stop; got {guards!r}",
        )

    def test_vpc_mode_none_is_stop(self) -> None:
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(direct_subnet="projects/x/regions/us-central1/subnetworks/redis-egress"),
                _revision(self._envs()),
                stabilization_sec=120,
                expected_redis="1", vpc_mode=None,
            )
        self.assertTrue(
            any("GATE2_VPC_MODE must be" in g for g in guards),
        )


class AuthSecretBindingTests(unittest.TestCase):
    """AUTH state is operator-supplied via `REDIS_INSTANCE_HAS_AUTH`."""

    def test_missing_env_var_is_stop(self) -> None:
        env = [
            {"name": "REDIS_ENABLED", "value": "1"},
            {"name": "REDIS_PASSWORD",
             "valueFrom": {"secretKeyRef": {"name": "redis-password", "key": "latest"}}},
        ]
        # Explicit empty patch — inherit whatever surrounding env
        # exists but pop REDIS_INSTANCE_HAS_AUTH.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_INSTANCE_HAS_AUTH", None)
            _, guards = analyze_cloud_run(
                _service(direct_subnet="projects/x/regions/us-central1/subnetworks/redis-egress"),
                _revision(env),
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
                _service(direct_subnet="projects/x/regions/us-central1/subnetworks/redis-egress"),
                _revision(env),
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
            {"name": "REDIS_PASSWORD",
             "valueFrom": {"secretKeyRef": {"name": "redis-password", "key": "latest"}}},
        ]
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "0"}):
            _, guards = analyze_cloud_run(
                _service(direct_subnet="projects/x/regions/us-central1/subnetworks/redis-egress"),
                _revision(env),
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
        # v5-preserved guards are stubbed in this branch, so on the
        # =0 path the only remaining guard family is v6's own —
        # which should also be empty.
        self.assertEqual(
            guards, [],
            f"expected no guards on =0 happy path; got {guards!r}",
        )

    def test_happy_path_expected_1_direct(self) -> None:
        env = [
            {"name": "REDIS_ENABLED", "value": "1"},
            {"name": "REDIS_PASSWORD",
             "valueFrom": {"secretKeyRef": {"name": "redis-password", "key": "latest"}}},
        ]
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(direct_subnet="projects/x/regions/us-central1/subnetworks/redis-egress"),
                _revision(env),
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
            {"name": "REDIS_PASSWORD",
             "valueFrom": {"secretKeyRef": {"name": "redis-password", "key": "latest"}}},
        ]
        with patch.dict(os.environ, {"REDIS_INSTANCE_HAS_AUTH": "1"}):
            _, guards = analyze_cloud_run(
                _service(connector="projects/x/locations/us-central1/connectors/redis-conn"),
                _revision(env),
                stabilization_sec=120,
                expected_redis="1", vpc_mode="connector",
            )
        self.assertEqual(guards, [])


if __name__ == "__main__":
    unittest.main()
