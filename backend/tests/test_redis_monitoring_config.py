"""Task #135 — validation tests for the redis-fanout monitoring
resources at `ops/monitoring/redis-fanout/`.

Two suites:

  - `RedisMonitoringConfigValidatorTests` — runs the shared
    `validate.load_and_validate()` structural pass and asserts
    every itemised check passed. Any file-level regression
    (bad YAML, wrong extractor, missing userLabels, etc.)
    fails a specific check by name so the diagnostic is
    localised.

  - `RedisMonitoringApplyPlannerTests` — exercises the pure
    `apply.build_plan()` function directly. No cloud contact.
    Asserts: allowlist refusal, notification-channel refusal,
    managed-identity conflict refusal, create-vs-update
    branching, orphan reporting, and the ordering guarantee
    (metrics planned before the alerts that consume them).

The ops directory lives OUTSIDE backend/ but the test suite
lives inside backend/tests/ so it runs on every CI push through
the existing pytest job. `sys.path` is prepended to import
`validate` and `apply` from the ops directory without turning
that directory into an installable package.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_OPS_DIR = _REPO / "ops" / "monitoring" / "redis-fanout"
sys.path.insert(0, str(_OPS_DIR))

import validate as _validate  # noqa: E402
import apply as _apply  # noqa: E402


# --- validator suite ------------------------------------------------------


class RedisMonitoringConfigValidatorTests(unittest.TestCase):
    """The whole tree parses and satisfies every structural rule."""

    def setUp(self):
        self.report = _validate.load_and_validate()

    def test_manifest_and_files_exist(self):
        self.assertTrue(
            self.report.checks,
            "no checks produced — validator did not run",
        )

    def test_all_checks_passed(self):
        failed = self.report.failed()
        if failed:
            details = "\n".join(f"  - {c.name}: {c.detail}" for c in failed)
            self.fail(f"validate.load_and_validate() produced failures:\n{details}")

    def test_every_metric_has_a_filter_anchor(self):
        """Regression barrier: every managed metric MUST anchor its
        filter on the production service resource. Catches a
        future YAML edit that drops the anchor and would silently
        widen the metric to unrelated services."""
        anchor_checks = [
            c for c in self.report.checks
            if c.name.startswith("metric.filter_anchor:")
        ]
        self.assertGreater(len(anchor_checks), 0, "no filter-anchor checks ran")
        for c in anchor_checks:
            self.assertTrue(c.ok, f"{c.name}: {c.detail}")

    def test_every_non_legacy_metric_extracts_instance_id(self):
        checks = [
            c for c in self.report.checks
            if c.name.startswith("metric.instance_id_extractor:")
        ]
        self.assertGreater(len(checks), 0)
        for c in checks:
            self.assertTrue(c.ok, f"{c.name}: {c.detail}")

    def test_alert_notification_channels_empty(self):
        """Notification channels come in at apply time — the files
        MUST NOT hard-code IDs."""
        checks = [
            c for c in self.report.checks
            if c.name.startswith("alert.notificationChannels_empty:")
        ]
        self.assertGreater(len(checks), 0)
        for c in checks:
            self.assertTrue(c.ok, f"{c.name}: {c.detail}")

    def test_alert_managed_by_pinned(self):
        checks = [
            c for c in self.report.checks
            if c.name.startswith("alert.managed_by_pinned:")
        ]
        self.assertGreater(len(checks), 0)
        for c in checks:
            self.assertTrue(c.ok, f"{c.name}: {c.detail}")

    def test_no_orphan_files(self):
        """A YAML file on disk without a manifest entry is a
        load-bearing config nobody reviewed — fail closed."""
        m_orphan = next(
            (c for c in self.report.checks if c.name == "metrics.no_orphan_files"),
            None,
        )
        a_orphan = next(
            (c for c in self.report.checks if c.name == "alerts.no_orphan_files"),
            None,
        )
        self.assertIsNotNone(m_orphan)
        self.assertIsNotNone(a_orphan)
        self.assertTrue(m_orphan.ok, m_orphan.detail)
        self.assertTrue(a_orphan.ok, a_orphan.detail)


# --- apply.build_plan planner suite ---------------------------------------


class RedisMonitoringApplyPlannerTests(unittest.TestCase):
    """`apply.build_plan()` is pure — never touches Google Cloud.
    Tests drive it directly with hand-crafted `existing_*` inputs."""

    def setUp(self):
        # Load the real manifest so any drift between manifest and
        # planner shows up here.
        import yaml  # type: ignore
        with _validate.MANIFEST_PATH.open("r", encoding="utf-8") as f:
            self.manifest = yaml.safe_load(f)
        self.project = "sturdy-dogfish-472313-k6"

        # Populate a channel map that satisfies every managed alert
        # so refusals from missing-channels don't dominate other
        # test assertions.
        self.full_channel_map = {
            alert_id: [f"projects/{self.project}/notificationChannels/fake-{alert_id}"]
            for alert_id in self.manifest["alerts"].keys()
        }

    def test_wrong_project_is_refused(self):
        plan = _apply.build_plan(
            project="some-other-project",
            channel_map=self.full_channel_map,
            existing_metrics=set(),
            existing_alerts_by_identity={},
            manifest=self.manifest,
        )
        self.assertTrue(plan.has_refusals())
        refusal = plan.refusals()[0]
        self.assertIn("allowlist", refusal.reason)

    def test_missing_channel_map_refuses_every_alert(self):
        plan = _apply.build_plan(
            project=self.project,
            channel_map={},  # no channels for any alert
            existing_metrics=set(),
            existing_alerts_by_identity={},
            manifest=self.manifest,
        )
        refusals = plan.refusals()
        refused_names = {r.name for r in refusals}
        for alert_id in self.manifest["alerts"].keys():
            self.assertIn(
                alert_id, refused_names,
                f"expected refusal for {alert_id!r} due to missing channel",
            )
        for r in refusals:
            if r.name in self.manifest["alerts"]:
                self.assertIn("notification channel", r.reason)

    def test_managed_identity_conflict_is_refused(self):
        """Two cloud policies sharing the same managed alert_id
        MUST refuse — the operator resolves manually to avoid a
        silent overwrite of the wrong policy."""
        managed_by = self.manifest["managed_labels"]["managed_by"]
        alert_name = next(iter(self.manifest["alerts"]))
        identity_key = f"{managed_by}/{alert_name}"
        plan = _apply.build_plan(
            project=self.project,
            channel_map=self.full_channel_map,
            existing_metrics=set(),
            existing_alerts_by_identity={
                identity_key: [
                    f"projects/{self.project}/alertPolicies/dup-A",
                    f"projects/{self.project}/alertPolicies/dup-B",
                ],
            },
            manifest=self.manifest,
        )
        refusals = [r for r in plan.refusals() if r.name == alert_name]
        self.assertEqual(len(refusals), 1, plan.actions)
        self.assertIn("share managed identity", refusals[0].reason)

    def test_create_when_absent_update_when_present(self):
        # Half the metrics already exist; the other half don't.
        metric_names = list(self.manifest["metrics"].keys())
        already = set(metric_names[: len(metric_names) // 2])

        managed_by = self.manifest["managed_labels"]["managed_by"]
        alert_names = list(self.manifest["alerts"].keys())
        already_alert_id = alert_names[0]
        existing_alerts = {
            f"{managed_by}/{already_alert_id}": [
                f"projects/{self.project}/alertPolicies/existing-a",
            ],
        }

        plan = _apply.build_plan(
            project=self.project,
            channel_map=self.full_channel_map,
            existing_metrics=already,
            existing_alerts_by_identity=existing_alerts,
            manifest=self.manifest,
        )
        self.assertFalse(plan.has_refusals(), plan.actions)

        actions_by_name = {a.name: a for a in plan.actions}
        for name in already:
            self.assertEqual(actions_by_name[name].kind, "update-metric")
        for name in set(metric_names) - already:
            self.assertEqual(actions_by_name[name].kind, "create-metric")
        self.assertEqual(actions_by_name[already_alert_id].kind, "update-alert")
        for name in alert_names[1:]:
            self.assertEqual(actions_by_name[name].kind, "create-alert")

    def test_orphan_metric_reported_but_not_deleted(self):
        """A managed-name metric in cloud without a manifest entry
        MUST show up as `orphan-metric` — never as delete."""
        plan = _apply.build_plan(
            project=self.project,
            channel_map=self.full_channel_map,
            existing_metrics={"redis_pubsub_ghost_metric"},
            existing_alerts_by_identity={},
            manifest=self.manifest,
        )
        orphan_actions = [a for a in plan.actions if a.kind == "orphan-metric"]
        self.assertEqual(len(orphan_actions), 1)
        self.assertEqual(orphan_actions[0].name, "redis_pubsub_ghost_metric")
        # No "delete-metric" action kind should ever appear.
        delete_actions = [a for a in plan.actions if "delete" in a.kind]
        self.assertEqual(delete_actions, [], "planner produced a delete action")

    def test_orphan_alert_reported_but_not_deleted(self):
        managed_by = self.manifest["managed_labels"]["managed_by"]
        plan = _apply.build_plan(
            project=self.project,
            channel_map=self.full_channel_map,
            existing_metrics=set(),
            existing_alerts_by_identity={
                f"{managed_by}/ghost_alert": [
                    f"projects/{self.project}/alertPolicies/ghost",
                ],
            },
            manifest=self.manifest,
        )
        orphan = [a for a in plan.actions if a.kind == "orphan-alert"]
        self.assertEqual(len(orphan), 1)
        self.assertIn("ghost_alert", orphan[0].name)
        delete_actions = [a for a in plan.actions if "delete" in a.kind]
        self.assertEqual(delete_actions, [])

    def test_metrics_planned_before_alerts(self):
        """The apply order matters — creating an alert that
        references a not-yet-created metric would fail against
        Cloud Monitoring. Planner must return metric actions
        before alert actions."""
        plan = _apply.build_plan(
            project=self.project,
            channel_map=self.full_channel_map,
            existing_metrics=set(),
            existing_alerts_by_identity={},
            manifest=self.manifest,
        )
        last_metric_idx = max(
            (i for i, a in enumerate(plan.actions) if "metric" in a.kind),
            default=-1,
        )
        first_alert_idx = min(
            (i for i, a in enumerate(plan.actions) if "alert" in a.kind),
            default=len(plan.actions),
        )
        self.assertLess(
            last_metric_idx, first_alert_idx,
            f"alert planned before a metric; actions={plan.actions!r}",
        )

    def test_render_plan_is_sanitized(self):
        """Plan output MUST NOT leak notification-channel IDs.
        The channel map's values are opaque resource names the
        operator did not authorize to be printed."""
        # Feed a channel map with an obviously secret-looking value
        # so a leak stands out.
        secret_channels = {
            alert_id: ["projects/x/notificationChannels/SECRET-DO-NOT-PRINT"]
            for alert_id in self.manifest["alerts"].keys()
        }
        plan = _apply.build_plan(
            project=self.project,
            channel_map=secret_channels,
            existing_metrics=set(),
            existing_alerts_by_identity={},
            manifest=self.manifest,
        )
        rendered = _apply.render_plan(plan)
        self.assertNotIn(
            "SECRET-DO-NOT-PRINT", rendered,
            "render_plan leaked a notification channel ID — must sanitize",
        )


# --- structural: manifest ↔ files agree ----------------------------------


class RedisMonitoringManifestConsistencyTests(unittest.TestCase):
    """Regression barrier for manifest / file drift."""

    def setUp(self):
        import yaml  # type: ignore
        with _validate.MANIFEST_PATH.open("r", encoding="utf-8") as f:
            self.manifest = yaml.safe_load(f)

    def test_manifest_metrics_match_directory_listing(self):
        listed = {p.name for p in (_OPS_DIR / "metrics").glob("*.yaml")}
        manifest_files = {
            Path(e["file"]).name for e in self.manifest["metrics"].values()
        }
        self.assertEqual(
            listed, manifest_files,
            "metrics directory listing differs from manifest",
        )

    def test_manifest_alerts_match_directory_listing(self):
        listed = {p.name for p in (_OPS_DIR / "alerts").glob("*.yaml")}
        manifest_files = {
            Path(e["file"]).name for e in self.manifest["alerts"].values()
        }
        self.assertEqual(
            listed, manifest_files,
            "alerts directory listing differs from manifest",
        )

    def test_alert_consumed_by_reciprocal_of_metric_manifest(self):
        """`metrics.<m>.consumed_by` and `alerts.<a>.metrics_used`
        must be reciprocal — a mismatch means a metric was renamed
        without updating the alert or vice versa."""
        m_to_a: dict[str, set[str]] = {
            m: set(entry.get("consumed_by", []) or [])
            for m, entry in self.manifest["metrics"].items()
        }
        a_to_m: dict[str, set[str]] = {
            a: set(entry.get("metrics_used", []) or [])
            for a, entry in self.manifest["alerts"].items()
        }
        for m, alerts in m_to_a.items():
            for a in alerts:
                self.assertIn(
                    m, a_to_m.get(a, set()),
                    f"metric {m!r} says consumed_by {a!r}; alert does not "
                    f"list it in metrics_used",
                )
        for a, metrics in a_to_m.items():
            for m in metrics:
                self.assertIn(
                    a, m_to_a.get(m, set()),
                    f"alert {a!r} says metrics_used {m!r}; metric does not "
                    f"list it in consumed_by",
                )


if __name__ == "__main__":
    unittest.main()
