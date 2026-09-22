# Redis fanout — monitoring resources

This directory holds the source-of-truth declarative
configuration for the Cloud Monitoring log-based metrics and
alert policies that PR #31 §3 "Monitoring" requires for the
Redis pub/sub rollout. The resources land in production only
via the enablement PR (task #137); this PR (task #135) is
config + validator + planner only. No writes to Google Cloud
happen from CI or from `python apply.py` in its default mode.

## Contents

```
ops/monitoring/redis-fanout/
├── metrics/                 One YAML per log-based counter (8)
├── alerts/                  One YAML per alert policy      (5)
├── manifest.yaml            Source-of-truth mapping
├── validate.py              Structural checks (also invoked from CI test)
├── apply.py                 Plan-only by default; --apply gated
└── README.md                This file
```

## How the resources map to the rollout doc

| File | Kind | Spec anchor |
|---|---|---|
| `metrics/redis_pubsub_startup_failed.yaml` | counter | §3 metrics table row 1 |
| `metrics/redis_pubsub_reconnect_attempts.yaml` | counter | §3 metrics row 2 |
| `metrics/redis_pubsub_reconnect_successes.yaml` | counter | §3 metrics row 3 |
| `metrics/redis_pubsub_reconnect_failures.yaml` | counter | §3 metrics row 4 |
| `metrics/redis_pubsub_reader_errors.yaml` | counter | §3 metrics row 5 |
| `metrics/redis_pubsub_active_probe_failure.yaml` | counter | §3 metrics row 6 |
| `metrics/redis_pubsub_active_probe_success.yaml` | counter | §3 metrics row 7 |
| `metrics/redis_pubsub_legacy_startup_failed.yaml` | counter | §3 metrics row 8 |
| `alerts/a5_startup_without_recovery.yaml` | policy | §3 Alerts — A5 |
| `alerts/a5_legacy_startup_failed.yaml` | policy | §3 Alerts — A5-legacy |
| `alerts/a6_reconnect_without_success.yaml` | policy | §3 Alerts — A6 |
| `alerts/a7_active_probe_failure.yaml` | policy | §3 Alerts — A7 |
| `alerts/a8a_probe_absence_per_revision.yaml` | policy | §3 Alerts — A8a |

**A8b is intentionally NOT a Cloud Monitoring policy.** Per the
spec it runs only inside the §4d step-5 operator window as an
external helper against the roster from `§4a-1` ticks. A metric
policy cannot enumerate the operator's roster.

## Managed identity

Every alert carries three `userLabels`:

- `managed_by = ops-monitoring-redis-fanout` — pins the policy
  to this repo's tooling. `apply.py` refuses to touch a policy
  without this label.
- `alert_id = <kebab-of-manifest-key>` — unique within the
  managed set. Two cloud policies sharing an `alert_id` is a
  fatal conflict; the operator resolves manually.
- `runbook = docs/03-analysis/redis-fanout-rollout-proposal.md#alerts`
  — every page carries a link to the diagnostic runbook.

`apply.py` never identifies policies by display name alone — a
display-name edit in the console would otherwise double-create.

## Notification channels

`notificationChannels: []` in every alert file. `apply.py`
injects the actual channel IDs at apply time from a separate
mapping file (`--channels <path>`, format:
`{ alert_id: [channel_resource_name, ...] }`). This keeps the
config environment-portable — the same alert files apply to
staging and production; only the channel map changes.

Refuses with `rc=4` if any managed alert has no channel
configured — we do not silently create pageable alerts with no
destination.

## Structural validator

`validate.py` runs a battery of static checks:

- Every YAML parses and matches its manifest entry.
- Every non-legacy metric's filter references the correct
  `jsonPayload.event` value and extracts
  `EXTRACT(jsonPayload.instance_id)` as `instance_id`.
- The legacy metric is the sole `textPayload` matcher.
- Every alert references only metrics declared in the manifest.
- Every alert's `groupByFields` includes at least the
  `resource.label.revision_name` and any per-instance grouping
  the manifest declares.
- Every alert carries the required managed userLabels.
- No orphan YAML on disk without a manifest entry.

Run locally:

```
python ops/monitoring/redis-fanout/validate.py
```

Runs in CI as a pytest suite (`backend/tests/test_redis_monitoring_config.py`).

## Operator use — plan only (default)

```
python ops/monitoring/redis-fanout/apply.py \
    --project sturdy-dogfish-472313-k6
```

Prints the planned actions (create / update / no-op / orphan
/ refuse) without contacting Google Cloud. `--project` must
be on `manifest.allowed_projects`; anything else is refused
with `rc=4` before any planning runs.

## Operator use — apply (gated)

```
python ops/monitoring/redis-fanout/apply.py \
    --project sturdy-dogfish-472313-k6 \
    --channels /path/to/channels.yaml \
    --apply --confirm 'I understand this affects production monitoring'
```

**Note:** `--apply` in this PR (task #135) exits with rc=6 and
a "planning only" message. The actual SDK snapshot + write paths
land in task #137's enablement PR, after review. Splitting them
keeps this PR reviewable as pure config + planner, and keeps a
Google Cloud SDK dependency out of CI.

## Return codes

| Code | Meaning |
|---|---|
| 0 | Plan printed, nothing to refuse; or (future) apply succeeded |
| 2 | Missing Google Cloud SDK when `--apply` was requested |
| 3 | Notification-channel map missing / malformed |
| 4 | Refuse — allowlist miss, static-check failure, missing channel, or identity conflict |
| 5 | `--apply` supplied without correct `--confirm` |
| 6 | `--apply` requested but this PR ships planning only |
