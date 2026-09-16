# Room reconciler — monitoring stack (PR-T1-E)

Cloud Logging log-based metrics, Cloud Monitoring dashboard, and alert
policies for the Track 1 room reconciler.

## Layout

```
ops/monitoring/reconciler/
├── metrics/          # log-based metric definitions (YAML)
├── dashboards/       # Cloud Monitoring dashboards (YAML)
├── alerts/           # AlertPolicy YAML
├── apply.sh          # idempotent apply (create-or-update-safe)
├── validate.py       # structural validator, wired into CI
└── README.md         # this file
```

## Event schema (contract with the reconciler)

Every emission from `app/services/room_reconciler.py` is a single-line
JSON object with these required fields:

```
event            reconciler_tick | reconciler_action | reconciler_diagnostic
schema_version   "1"
severity         DEBUG | INFO | NOTICE | WARNING | ERROR
message          short human summary
component        "room_reconciler"
instance_id      the process's INSTANCE_ID (log body only, NOT a metric label)
```

`reconciler_tick` — one per pass, carries the snapshot (`outcome`,
`overdue`, aggregate counters). Drives the freshness heartbeat, the
overdue alert, and the tick-error alert.

`reconciler_action` — one per cleanup attempt, `reason` is a bounded
enum (`ended_room_local_cleanup` or `cleanup_error`). Drives the
recovery-frequency chart. Room and org IDs are in the log body for
debugging — NOT metric labels (cardinality).

`reconciler_diagnostic` — DEBUG-severity chatter. Not driven off any
metric; visible in Cloud Logging for debugging.

## Metrics (5)

Log-based metrics can be **counters** or **distributions**. Cloud
Logging does not support true gauges — that constraint drove the
shape of `reconciler_overdue_ticks` (a counter of "ticks reporting an
overdue" rather than a gauge of "how many overdue right now").

| Metric | Type | Purpose |
|---|---|---|
| `reconciler_success_ticks` | Counter | Freshness heartbeat (A1) |
| `reconciler_tick_outcomes` | Counter, labeled by `outcome` | A3 tick-errors alert + dashboard |
| `reconciler_actions` | Counter, labeled by `reason` | Dashboard only for now (see A4) |
| `reconciler_overdue_ticks` | Counter | A2 overdue alert |
| `reconciler_oldest_overdue_seconds` | Distribution | Dashboard trend |

The dashboard shows the raw `cleanup_inflight` snapshot via a **logs
panel** (recent tick JSON bodies) rather than a derived metric. The
snapshot is mostly 0 or 1 during normal operation, so a p95/p99
distribution over ticks would be misleading — the raw log body is
more useful for operators debugging a stuck-cleanup incident.

### Filter scoping

Every metric filter includes:

```
resource.type="cloud_run_revision"
AND resource.labels.service_name="__CLOUD_RUN_SERVICE__"
AND jsonPayload.component="room_reconciler"
```

The `__CLOUD_RUN_SERVICE__` placeholder is substituted at apply time
from the `CLOUD_RUN_SERVICE` env var. Without this scope, staging,
tests, or unrelated services in the project could contribute to
production metrics.

## Alerts (4)

| Alert | Enabled | Threshold |
|---|---|---|
| A1 freshness absence | ✅ | `reconciler_success_ticks` absent for 15 minutes, aggregated across all revisions of the service |
| A2 overdue cleanup | ✅ | `reconciler_overdue_ticks` — four observations over a 10-minute rolling window (service-level aggregation) |
| A3 tick errors | ✅ | Error-outcome `reconciler_tick_outcomes` — four errors over a 10-minute rolling window (service-level) |
| A4 recovery actions rate | ❌ | Impossible-threshold placeholder — enable only after production baseline |

### Cross-revision aggregation

Cloud Run log-based metrics produce one time series per (service,
revision). Every alert here aggregates across revisions via
`crossSeriesReducer: REDUCE_SUM` + `groupByFields:
[resource.label.service_name]`. Without this, a rolling deploy
would cause the retired revision's series to appear "absent" (for
A1) or drop off (for A2 / A3) and could page ops on healthy
rollouts. The validator refuses any alert that does not aggregate
across revisions.

### A2 / A3 window: 10 minutes with `duration: 0s`

Both thresholds use a **10-minute** rolling window (four
observations at the default 30s tick interval). Described as "four
overdue observations" — NOT "90 seconds of continuous failure."
Log-based metrics carry documented ingestion latency of up to
~10 minutes, so a shorter window would page on ingestion jitter
alone. The 10-minute window matches A1's alignment for the same
reason.

The Cloud Monitoring `duration` field is set to `0s` on purpose.
`alignmentPeriod: 600s` is what aggregates the observations over
10 minutes; a non-zero `duration` would layer an additional
"threshold must stay violated for D more seconds" retest window on
top and delay firing by up to another D. The documented intent is
"four observations inside a 10-minute aligned window," which is
exactly `alignmentPeriod=600s`, `thresholdValue=3`,
`comparison=COMPARISON_GT`, `duration=0s`.

### A1's absence-condition caveat

An absence policy in Cloud Monitoring **cannot fire until the metric
has received at least one data point**. If the reconciler is
`ROOM_RECONCILER_ENABLED=0` at deploy time, this policy stays silent
forever until the flag is flipped and a first successful tick lands
(then the freshness clock starts).

### Freshness threshold rationale

The reconciler ticks every 30 seconds at default settings. A naive
threshold of 3 × 30s = 90s would be wrong: Cloud Logging log-based
metrics can be delayed by up to ~10 minutes, and Google recommends a
rolling window of at least 10 minutes for absence alerts. The 15-
minute window (documented in A1) accepts that latency and adds
operational headroom.

### A4 stays disabled

A `reconciler_actions{reason="ended_room_local_cleanup"}` firing in
production means a primary path (End Service → Redis fanout) silently
failed. That's exactly the recovery this reconciler exists to
provide, so it can happen legitimately during Redis instability. We
have no production baseline yet: shipping A4 enabled with a guessed
threshold would either alert on healthy behavior or hide real
regressions. The placeholder policy uploads with `enabled: false`
and an intentionally-impossible threshold, so an accidental
`enabled: true` flip without a real baseline still cannot page.

## Applying

```
GCP_PROJECT=worshiptranslate \
CLOUD_RUN_SERVICE=worshiptranslate-backend \
NOTIFICATION_CHANNEL_IDS=projects/…/notificationChannels/abc,projects/…/notificationChannels/xyz \
    ops/monitoring/reconciler/apply.sh
```

The script is idempotent:
- First run: creates every metric / dashboard / policy.
- Second run: updates in place; no drift.

**How idempotence works.**

- **Log-based metrics** are identified by their client-assigned
  `name` alone. The Cloud Logging `LogMetric` resource does not
  support user labels — the API rejects them. The validator refuses
  any metric YAML that carries `userLabels` or `labels`.
- **Alert policies and dashboards** carry the user labels:
  ```
  managed_by: reconciler-monitoring
  resource_id: <stable id per file>
  ```
  The apply script looks these up by label filter — NOT by
  displayName, which is user-visible text and can drift. If the
  lookup returns:

  - **zero matches**: the resource is created.
  - **one match**: the resource is updated in place.
  - **two or more matches**: the script **refuses** to update. That
    means somebody manually duplicated a managed resource; bring the
    project back to a single instance before re-running.

Every enabled alert is asserted to have at least one notification
channel BEFORE any Cloud Monitoring call is made — a deploy with
`NOTIFICATION_CHANNEL_IDS=` (empty) fails fast instead of creating
a silent alert.

Placeholder substitution goes through a small inline Python helper,
not sed — multi-line YAML values would corrupt under sed.

**PyYAML preflight.** The apply script requires PyYAML in the
operator's shell. `backend/requirements-dev.txt` guarantees it in
CI, but a fresh terminal on an operator's laptop may not have it —
the script checks `python3 -c 'import yaml'` on startup and prints
`pip install PyYAML>=6.0` (or the homebrew equivalent for macOS
system Python) if the import fails.

Every resource is `describe`-emitted immediately after apply, so
copy-and-paste from the output is the deployment evidence.

## Notification channels

`NOTIFICATION_CHANNEL_IDS` is a comma-separated list of full channel
names:

```
projects/PROJECT/notificationChannels/CHANNEL_ID
```

The YAML in `alerts/*.yaml` uses `__NOTIFICATION_CHANNELS__` as a
placeholder. The apply script substitutes real channel IDs at deploy
time — real IDs are never committed to the repo.

## Validation in CI

`ops/monitoring/reconciler/validate.py` runs in the `backend-tests`
job via `tests/test_monitoring_config.py`. It verifies:

- Every YAML parses.
- Metric names are unique.
- Every metric filter is scoped to `component="room_reconciler"`
  AND to the Cloud Run service (`resource.labels.service_name=
  "__CLOUD_RUN_SERVICE__"`).
- No metric label is a high-cardinality field (`org_id`, `room_id`,
  `instance_id`).
- Enum labels' descriptions mention every value in
  `app.services.room_reconciler._TICK_OUTCOMES` /
  `_ACTION_REASONS` — makes schema drift visible.
- Every alert-policy `metric.type` refers to a metric that exists.
- Every alert-policy `notificationChannels` is the substitution
  placeholder.
- Every alert-policy has `userLabels.managed_by =
  "reconciler-monitoring"` and a unique `resource_id`.
- Every alert-policy condition aggregates across revision-scoped
  series (`crossSeriesReducer` + `groupByFields:
  ["resource.label.service_name"]`).
- Every dashboard has the same `managed_by` + `resource_id`
  labels.
- Exactly one alert policy is `enabled: false` (A4).
- `bash -n apply.sh` succeeds (catches shell syntax errors before
  deploy).
