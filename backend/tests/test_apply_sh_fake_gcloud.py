"""End-to-end test of ops/monitoring/reconciler/apply.sh using a fake
`gcloud` executable on PATH.

This is the reviewer's preferred check for the "helpers are actually
called and produce the right JSON" property. Regex-based checks over
apply.sh source can miss the case where a helper is defined and
apparently called but its output is not used, or where the fetched
etag / existing condition names are silently discarded.

The harness:
  1. Writes a small Python script to a tempdir and names it `gcloud`.
  2. Puts that tempdir first on PATH.
  3. Runs `apply.sh` twice.
  4. On each `gcloud logging|monitoring|alpha` invocation the fake
     records the command args and any `--config-from-file` /
     `--policy-from-file` payload.
  5. First-run `describe` calls return "not found" so the script
     goes through the create path. Second-run `describe` calls
     return canned existing resources (with etag + condition
     names) so the script goes through the update path.
  6. The test then reads the recorded payloads and asserts:
       - Dashboard update body carries `etag` and `name`.
       - Alert-policy update body's conditions have `name` fields
         merged in from the fake describe output.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
APPLY_SH = REPO_ROOT / "ops" / "monitoring" / "reconciler" / "apply.sh"


FAKE_GCLOUD_TEMPLATE = r'''#!__PYTHON__
"""Fake gcloud used by test_apply_sh_fake_gcloud.py.

Records every invocation to $FAKE_GCLOUD_LOG (JSON lines). Reads
"pass mode" from $FAKE_GCLOUD_PASS (values: `create` or `update`)
to decide whether `describe`/`list` return not-found (first run) or
canned existing resources (second run).
"""
import json, os, sys, uuid

log_path = os.environ["FAKE_GCLOUD_LOG"]
mode = os.environ.get("FAKE_GCLOUD_PASS", "create")

argv = list(sys.argv[1:])

# Extract --project and --format for accurate response shaping.
project = ""
fmt = ""
config_file = None
policy_file = None
pruned = []
i = 0
while i < len(argv):
    a = argv[i]
    if a == "--project":
        project = argv[i + 1]; i += 2; continue
    if a.startswith("--project="):
        project = a.split("=", 1)[1]; i += 1; continue
    if a == "--format":
        fmt = argv[i + 1]; i += 2; continue
    if a.startswith("--format="):
        fmt = a.split("=", 1)[1]; i += 1; continue
    if a == "--config-from-file":
        config_file = argv[i + 1]; i += 2; continue
    if a.startswith("--config-from-file="):
        config_file = a.split("=", 1)[1]; i += 1; continue
    if a == "--policy-from-file":
        policy_file = argv[i + 1]; i += 2; continue
    if a.startswith("--policy-from-file="):
        policy_file = a.split("=", 1)[1]; i += 1; continue
    if a == "--filter" or a == "--quiet":
        # accept but skip its value if present
        if a == "--filter":
            i += 2
        else:
            i += 1
        continue
    if a.startswith("--filter=") or a.startswith("--quiet"):
        i += 1; continue
    pruned.append(a); i += 1

# pruned is now the positional command chain, e.g. ["logging", "metrics", "describe", "reconciler_success_ticks"]
subcmd = tuple(pruned)

# Log the invocation, including a snapshot of the submitted payload
# for update/create calls.
submitted = None
if config_file and os.path.exists(config_file):
    with open(config_file, "r") as fh:
        submitted = json.load(fh) if config_file.endswith(".json") else fh.read()
if policy_file and os.path.exists(policy_file):
    with open(policy_file, "r") as fh:
        submitted = json.load(fh) if policy_file.endswith(".json") else fh.read()

with open(log_path, "a") as fh:
    fh.write(json.dumps({
        "argv": pruned,
        "project": project,
        "format": fmt,
        "submitted": submitted,
    }) + "\n")

# Dispatch table.
def _reply_json(obj):
    sys.stdout.write(json.dumps(obj))

# metrics
if subcmd[:3] == ("logging", "metrics", "describe"):
    # apply.sh calls describe twice per metric:
    #   1. existence check, with `--format=json` and output
    #      redirected to /dev/null. Returning non-zero here in
    #      create mode is what steers apply_metric into the
    #      `create` branch.
    #   2. post-apply evidence emit, no `--format` argument, output
    #      not redirected. This one must ALWAYS succeed — it's
    #      what the reviewer copies out of the run log.
    if fmt == "json" and mode == "create":
        sys.stderr.write("NOT_FOUND\n")
        sys.exit(1)
    _reply_json({"name": "projects/test/metrics/" + subcmd[3]})
    sys.exit(0)
if subcmd[:3] == ("logging", "metrics", "create"):
    sys.exit(0)
if subcmd[:3] == ("logging", "metrics", "update"):
    sys.exit(0)

# dashboards
if subcmd[:3] == ("monitoring", "dashboards", "list"):
    if mode == "create":
        # no existing dashboards
        sys.stdout.write("")
    else:
        # one canned existing dashboard matching the labels
        sys.stdout.write("projects/test/dashboards/existing-dash")
    sys.exit(0)
if subcmd[:3] == ("monitoring", "dashboards", "describe"):
    _reply_json({
        "name": subcmd[3],
        "displayName": "Room Reconciler — Track 1",
        "etag": "existing-etag-abc123",
        "labels": {"managed_by": "reconciler-monitoring",
                   "resource_id": "reconciler_dashboard"},
    })
    sys.exit(0)
if subcmd[:3] == ("monitoring", "dashboards", "create"):
    sys.stdout.write("projects/test/dashboards/new-dash-" + uuid.uuid4().hex[:6])
    sys.exit(0)
if subcmd[:3] == ("monitoring", "dashboards", "update"):
    sys.exit(0)

# alert policies (alpha)
if subcmd[:4] == ("alpha", "monitoring", "policies", "list"):
    if mode == "create":
        sys.stdout.write("")
    else:
        sys.stdout.write("projects/test/alertPolicies/existing-policy")
    sys.exit(0)
if subcmd[:4] == ("alpha", "monitoring", "policies", "describe"):
    # Return a canned existing policy with named conditions.
    _reply_json({
        "name": subcmd[4],
        "displayName": "Existing Policy",
        "conditions": [
            {
                "name": subcmd[4] + "/conditions/existing-cond-1",
                "displayName": "reconciler_success_ticks absent for 15m (service-level)",
            },
            {
                "name": subcmd[4] + "/conditions/existing-cond-2",
                "displayName": "reconciler_overdue_ticks — four observations over 10m (service-level)",
            },
            {
                "name": subcmd[4] + "/conditions/existing-cond-3",
                "displayName": "reconciler_tick_outcomes error rate — four errors over 10m (service-level)",
            },
            {
                "name": subcmd[4] + "/conditions/existing-cond-4",
                "displayName": "reconciler_actions rate — placeholder (disabled)",
            },
        ],
    })
    sys.exit(0)
if subcmd[:4] == ("alpha", "monitoring", "policies", "create"):
    sys.stdout.write("projects/test/alertPolicies/new-" + uuid.uuid4().hex[:6])
    sys.exit(0)
if subcmd[:4] == ("alpha", "monitoring", "policies", "update"):
    sys.exit(0)

sys.stderr.write(f"fake-gcloud: unhandled subcommand {subcmd}\n")
sys.exit(2)
'''


class FakeGcloudApplyTests(unittest.TestCase):
    """Runs apply.sh against a fake gcloud and verifies the update
    payloads carry the fields Cloud Monitoring requires.

    Skipped when apply.sh is not present (defensive against the test
    running in an environment where ops/ was not shipped).
    """

    def setUp(self):
        if not APPLY_SH.exists():
            self.skipTest(f"apply.sh not present at {APPLY_SH}")

    def _run_apply(self, pass_mode: str, workdir: Path) -> list[dict]:
        fake_gcloud = workdir / "gcloud"
        fake_gcloud.write_text(
            FAKE_GCLOUD_TEMPLATE.replace("__PYTHON__", sys.executable)
        )
        fake_gcloud.chmod(fake_gcloud.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

        log_path = workdir / f"gcloud-{pass_mode}.log"
        env = os.environ.copy()
        # apply.sh calls `python3` from PATH. Make sure that resolves to
        # a python that can import PyYAML — the current test runner's
        # interpreter is guaranteed to have it (requirements-dev.txt).
        # Also expose our fake gcloud on PATH by putting workdir first.
        python_bin_dir = str(Path(sys.executable).resolve().parent)
        env["PATH"] = (
            f"{workdir}{os.pathsep}{python_bin_dir}{os.pathsep}"
            f"{env.get('PATH', '')}"
        )
        env["FAKE_GCLOUD_LOG"] = str(log_path)
        env["FAKE_GCLOUD_PASS"] = pass_mode
        env["GCP_PROJECT"] = "test-project"
        env["CLOUD_RUN_SERVICE"] = "worshiptranslate-backend"
        env["NOTIFICATION_CHANNEL_IDS"] = "projects/test/notificationChannels/abc"

        result = subprocess.run(
            ["bash", str(APPLY_SH)],
            env=env,
            cwd=workdir,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"apply.sh failed in {pass_mode} mode\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}",
        )

        entries = []
        with log_path.open("r") as fh:
            for line in fh:
                if line.strip():
                    entries.append(json.loads(line))
        return entries

    def test_second_apply_preserves_dashboard_etag_and_alert_condition_names(self):
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            _first = self._run_apply("create", workdir)
            second = self._run_apply("update", workdir)

        # Find the dashboard update call.
        dash_updates = [
            e for e in second
            if tuple(e["argv"][:3]) == ("monitoring", "dashboards", "update")
        ]
        self.assertEqual(
            len(dash_updates),
            1,
            f"expected exactly one dashboard update on the second "
            f"apply; got {len(dash_updates)}",
        )
        submitted = dash_updates[0]["submitted"]
        self.assertIsInstance(submitted, dict, "dashboard update body")
        self.assertEqual(
            submitted.get("etag"),
            "existing-etag-abc123",
            "dashboard update body must carry the current server etag "
            f"fetched via describe; got submitted={submitted!r}",
        )
        self.assertTrue(
            submitted.get("name", "").startswith("projects/test/dashboards/"),
            f"dashboard update body must carry the resource name; got "
            f"{submitted.get('name')!r}",
        )

        # Find every alert-policy update call. There are four alert
        # YAMLs, so we expect four updates on the second apply.
        alert_updates = [
            e for e in second
            if tuple(e["argv"][:4]) == ("alpha", "monitoring", "policies", "update")
        ]
        self.assertEqual(
            len(alert_updates),
            4,
            f"expected four alert-policy updates on the second apply; "
            f"got {len(alert_updates)}",
        )
        for entry in alert_updates:
            body = entry["submitted"]
            self.assertIsInstance(body, dict, "alert update body")
            self.assertTrue(
                body.get("name", "").startswith("projects/test/alertPolicies/"),
                f"alert update body must carry the policy name; got "
                f"name={body.get('name')!r}",
            )
            conditions = body.get("conditions") or []
            self.assertTrue(
                conditions,
                f"alert update body has no conditions; body keys: "
                f"{list(body.keys())}",
            )
            named = [c for c in conditions if c.get("name")]
            self.assertEqual(
                len(named),
                len(conditions),
                f"every condition on the update body must carry the "
                f"server-assigned name merged from describe. Missing on: "
                f"{[c.get('displayName') for c in conditions if not c.get('name')]}",
            )
