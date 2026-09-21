# Redis fanout enablement — rollout proposal (draft)

Draft — not yet approved. Blocks Track 1 Gate 2 rerun and Gate 3.

## Purpose

Enable the already-built Redis Pub/Sub cross-instance fanout
(`backend/app/services/redis_pubsub.py`) in production so a listener
that reconnects to a Cloud Run revision different from the one holding
the producer WebSocket still receives translations. Absent this, a
single deploy through a live session can silently break listener
delivery — the failure recorded in issue #26 and modelled by F-25 /
F-26 in the resource-cleanup harness.

Related:
- Defect: #29 (cross-revision room isolation with Redis disabled)
- Gate 2 fail record: #26
- Test evidence: PR #30 (F-25 + F-26 land as tests only; no
  production enablement)
- Adapter design: `docs/02-design/features/redis-pubsub-fanout.design.md`
- Local smoke test: `docs/03-analysis/redis-pubsub-smoke.md`

## 1. Current infrastructure inventory (observed 2026-09-20)

Read-only inventory queried against production. Sanitized — the
Redis IP still present in Cloud Run env is not printed here.

| Item | State |
|------|-------|
| `redis.googleapis.com` (Memorystore API) | ENABLED |
| `vpcaccess.googleapis.com` (VPC Access API) | NOT ENABLED |
| Cloud Memorystore Redis instances | **0** (none exist) |
| Cloud Run `vpc-access-connector` | none attached |
| Cloud Run `vpc-access-egress` annotation | `private-ranges-only` (paired with a connector that no longer exists) |
| `REDIS_ENABLED` env | `0` (Track 1 constraint holding) |
| `REDIS_HOST` env | set to a private IP that no longer resolves to a Memorystore instance (stale) |
| `REDIS_PORT` env | `6379` |
| `REDIS_PASSWORD` env | Secret Manager reference `redis-password/latest` present |
| `REDIS_CHANNEL_PREFIX` env | `worshiptranslate` |
| `INSTANCE_ID` env | unset (auto-generated per process — correct) |

**Reading:** a prior Memorystore instance existed and was destroyed;
the Cloud Run env, VPC annotation, and Secret Manager entry remain
from that provisioning. The rollout treats infrastructure as absent
and re-provisions rather than assuming any of the residue is
authoritative.

The deploy workflow's inline comment describing Memorystore as absent
matches this observation. Prior planning notes that assume
provisioning are stale.

## 2. Pre-enablement blocker — startup-recovery bug in `redis_pubsub`

`backend/app/services/redis_pubsub.py` sets `_started = True` at
line 89, before `_pub.ping()` at line 112. When the ping raises
(Redis unavailable at start), the exception handler at lines
120–124 logs "redis connect failed" and sets `_connected = False`,
but:

- `_started` is left True.
- The reader task is never created because `create_task(_reader_loop)`
  at line 115 sits inside the try body that raised.
- The reader loop is what runs `_reconnect` (line 327), so the
  reconnect path is unreachable.
- A subsequent `start()` short-circuits at line 87 because
  `self._started` is True — nothing repairs the state.

**Consequence for the rollout:** if the Cloud Run instance starts up
while Memorystore is briefly unavailable (a Memorystore maintenance
event, a transient network hiccup during instance startup, or a
misconfigured VPC path), the instance never subscribes to Redis
even after Redis comes back — it silently degrades to local-only
broadcast. That is exactly the Gate 2 failure mode we are trying to
prevent, but from a different cause.

**Fix — implemented in PR #32 (`fix/redis-pubsub-startup-recovery`,
CI-verified, unmerged), stacked on PR #30:**

- Reader task is always scheduled by `start()` regardless of
  whether the initial ping succeeded.
- Initial ping is bounded by `asyncio.wait_for(REDIS_COMMAND_TIMEOUT_SEC)`.
  `socket_connect_timeout` covers only the TCP handshake; a server
  that accepts TCP but never replies would otherwise hang `start()`
  indefinitely and defeat the recovery path.
- On ping failure the reader loop's existing `_reconnect` path
  brings the subscriber up when Redis becomes reachable.
- `stop()` handles the partial-client state a failed `start()`
  can leave behind.
- Distinct WARNING log wording for TCP-accepted-but-hung vs
  connection-refused, both under the recognisable
  `redis pubsub initial connect failed` prefix.
- Scope note: the bug can produce a cross-instance isolation
  outcome similar to Gate 2's, but Gate 2 causation has not
  been established.

**Test coverage in PR #32:**

- Unit-level: four new `RedisPubSubRealStartTests` in
  `backend/tests/test_redis_pubsub.py` exercise the real
  `start()`/`stop()` path (not the `_fake_start` shortcut the
  existing nine tests use). Covers ping-ok, ping-refused
  (reader-task assertion), ping-hang (`wait_for` bound), and
  `stop()` after ping failure (partial-client cleanup).
- Integration: F-27 in
  `backend/tests/integration/resource_cleanup/test_f27_redis_startup_recovery.py`.
  Event-driven — Redis is restored only after both backends log
  the initial-failure line (either the fixed or the pre-fix
  wording; both are recognised so a bisection run can reach the
  recovery assertion). Hard acceptance is step 6 (cross-instance
  marker delivery through the recovered subscriber on B);
  reconnect log line is a soft observability signal.
- Bisection preserved: pre-fix `redis_pubsub.py` with these
  tests reproducibly fails at reader-task, ping-hang, and
  partial-cleanup assertions.

CI on commit `25b0cf3d`: backend-tests, firestore-emulator-tests,
frontend-checks, and integration-tests (F-8, F-9, F-15, F-24,
F-25, F-26, **F-27**) all green. **PR #32 is not yet merged.**
"Implemented and CI-verified" is accurate; "shipped" is not.
This rollout proposal cannot advance past its own approval gate
until PR #32 (and PR #30) merge in a confirmed no-live-room
window.

PR #30 remains a tests-only PR and its approval stands.

## 3. Monitoring — from actual emitted signals

Grouping alerts by instance requires that every log line carry
an instance identifier the metric can extract. Plain-text
`log.warning(...)` calls do not — they land as `textPayload`
and Cloud Logging cannot pull a structured field out of them.
This section therefore first pins the adapter's emission
FORMAT (structured JSON on stdout, one line per event, matching
`app/services/room_reconciler.py:173`'s existing style), then
defines the metrics and alerts against those fields.

### Adapter emission format (proposed for the enablement PR)

The enablement PR (a separate follow-on, not this doc) changes
`backend/app/services/redis_pubsub.py` so every operational
event goes through a single JSON emitter:

```python
def _emit(event: str, severity: str, **fields) -> None:
    print(json.dumps({
        "event": event,           # stable machine-readable name
        "severity": severity,     # INFO | WARNING | ERROR
        "component": "redis_pubsub",
        "instance_id": ENV.INSTANCE_ID,
        "schema_version": 1,
        **fields,
    }, ensure_ascii=False, sort_keys=True, default=str))
```

Cloud Run parses each JSON line into `jsonPayload`, so the
metric filters below can extract `jsonPayload.instance_id`
directly — no textual regex, no dependence on log-level
plumbing (the emitter uses `print`, not `logging`, and always
reaches stdout regardless of the root logger level).

Human-readable existing WARNING strings from the current
adapter stay in the message body but move under
`jsonPayload.message`. The `event` field is the metric anchor.

### Adapter event catalogue

| event | severity | fields (in addition to instance_id, component, schema_version, severity) | emitted from |
|-------|----------|--------------------------------------------------------------------------|--------------|
| `redis_pubsub_started` | INFO | `host`, `port`, `prefix` | `start()` after ping succeeds |
| `redis_pubsub_initial_connect_failed` | WARNING | `reason` (`"refused"` \| `"timeout"`), `error` | `start()` — either exception branch |
| `redis_pubsub_reconnecting` | INFO | `attempt`, `delay_seconds` | `_reconnect()` before sleep |
| `redis_pubsub_reconnected` | INFO | `rooms_resubscribed` (int) | `_reconnect()` on success |
| `redis_pubsub_reconnect_failed` | WARNING | `error` | `_reconnect()` on exception |
| `redis_pubsub_reader_error` | WARNING | `error` | `_reader_loop()` outer except |
| `redis_probe_ok` | INFO | `rtt_ms`, `probe_id` | probe callback on delivery |
| `redis_probe_failed` | WARNING | `reason` (`"timeout"` \| `"exception"`), `probe_id`, optional `error` | probe timeout or exception |
| `redis_pubsub_startup_local_only_broadcast` | ERROR | `error` | **Pre-fix adapter only.** Never emitted by PR #32's code; matched by a legacy metric filter so a rollback is loud. |

The pre-fix `redis connect failed; falling back to local-only broadcast`
string (see PR #30 archives) never lands in `jsonPayload.event`
after PR #32 merges. The rollback tell for it uses a legacy
textPayload filter (see the metric table below).

### Log-based metrics (all counters, no gauges) + label extraction

Each metric declares an `EXTRACT(jsonPayload.instance_id)`
label named `instance_id`. Cloud Monitoring groups on this
label plus the resource labels Cloud Run provides
(`resource.labels.service_name`, `resource.labels.revision_name`,
`resource.labels.location`). The Cloud Run log resource does
NOT expose an `instance_id` label of its own — the extracted
label is the ONLY per-instance identity available.

| Metric | Filter | Extracted label | Purpose |
|--------|--------|-----------------|---------|
| `redis_pubsub_startup_failed` | `jsonPayload.event="redis_pubsub_initial_connect_failed"` | `instance_id=EXTRACT(jsonPayload.instance_id)`, `reason=EXTRACT(jsonPayload.reason)` | Initial ping raised. Paired with `redis_pubsub_reconnect_successes` by A5. |
| `redis_pubsub_reconnect_attempts` | `jsonPayload.event="redis_pubsub_reconnecting"` | `instance_id` | Reader-loop attempted reconnect. |
| `redis_pubsub_reconnect_successes` | `jsonPayload.event="redis_pubsub_reconnected"` | `instance_id` | Reader-loop repaired the connection. This is the recovery signal. |
| `redis_pubsub_reconnect_failures` | `jsonPayload.event="redis_pubsub_reconnect_failed"` | `instance_id` | Reconnect attempt failed. |
| `redis_pubsub_reader_errors` | `jsonPayload.event="redis_pubsub_reader_error"` | `instance_id` | Reader-loop crashes. |
| `redis_pubsub_active_probe_failure` | `jsonPayload.event="redis_probe_failed"` | `instance_id`, `reason` | In-process probe failed. Exercises adapter's real pub+sub. |
| `redis_pubsub_active_probe_success` | `jsonPayload.event="redis_probe_ok"` | `instance_id` | In-process probe succeeded. Absence detection feeds A8. |
| `redis_pubsub_legacy_startup_failed` | `textPayload:"redis connect failed; falling back to local-only broadcast"` | `revision_name` only (pre-fix code has no structured emitter) | **Rollback tell.** Non-zero indicates PR #32 was rolled back or a pre-#32 revision is somehow live. Pages via A5-legacy. |

All metrics also filter on
`resource.type="cloud_run_revision" AND resource.labels.service_name="worshiptranslate-backend"`.

### Alerts

All alerts group by `resource.labels.revision_name` AND the
EXTRACTED `instance_id` label defined above. A per-service
aggregate hides the single-instance failure mode Gate 2 was
about — one revision/instance silently degrading while the
other looks healthy.

- `A5 startup_failed on instance X AND reconnect_successes on
  same X == 0 in the following 5 min` — pages. The fixed
  adapter is expected to follow a startup failure with a
  reconnect success; pairing them separates the recovered case
  from the stuck case. If PR #32 is rolled back, the paired
  condition still fires because pre-fix code has NO reconnect
  path, so `reconnect_successes` stays 0 by construction and
  every occurrence pages.
- `A5-legacy redis_pubsub_legacy_startup_failed > 0 in 5 min`
  (per revision) — pages. Direct tell for a pre-#32 code
  running in production, independent of A5's paired condition.
- `A6 reconnect_attempts > 3 on instance X in 5 min AND
  reconnect_successes on same X == 0 in the same window` —
  pages. Indicates Memorystore or VPC path is unhealthy for
  that instance.
- `A7 active_probe_failure > 0 on instance X in 5 min` —
  pages. Exercises the adapter's real reader path so a stuck
  subscriber cannot be masked by a fresh Redis client.
- `A8` — see the missing-probe section below.
- The existing A1 reconciler-freshness alert continues to page
  on its own — a Redis outage does not affect the reconciler.

### Active pub/sub probe (`A7`/`A8` source)

Log-based counters only tell us what the adapter itself
emitted. They cannot tell us that a message we publish is
actually received by our OWN adapter's subscriber. That is the
exact failure §2's startup-recovery bug produced: the reader
task was never scheduled, so the subscribe path was dead — a
probe using its own fresh Redis client would have succeeded
and hidden the defect. The probe MUST exercise the adapter's
actual reader path.

**Cloud Run routing constraint (why the probe runs in-process).**

A request to a Cloud Run service URL selects an instance by
load balancing; session affinity is best-effort, not
guaranteed
(https://cloud.google.com/run/docs/triggering/session-affinity).
Polling a revision URL from any external caller cannot
guarantee every instance is exercised. The probe therefore
runs INSIDE each backend process; per-instance coverage is by
construction. There is no Cloud Scheduler probe job and no
`/internal/redis_probe` HTTP endpoint in this design.

**CPU allocation prerequisite (Cloud Run request-based billing
does not run a 30s task through idle periods).**

Cloud Run's default "CPU is only allocated during request
processing" mode throttles CPU when there are no in-flight
requests, so a `asyncio.sleep(30)` task cannot be promised to
fire on time on an idle instance
(https://cloud.google.com/run/docs/configuring/cpu-allocation).
The enablement PR therefore sets the service to **"CPU is
always allocated"** for the redis-enabled revisions. This is a
Cloud Run config change and lands in the enablement §4d
window; the operator confirms the current allocation mode
before selecting probe cadence.

If "CPU is always allocated" is refused for cost reasons, the
fallback is (i) piggyback the probe on a health-check endpoint
already exercised by an uptime check (removing the idle-CPU
issue by producing a request every N seconds) AND (ii) widen
A7/A8 tolerances to the health-check cadence + jitter. Do NOT
silently accept unreliable probe cadence.

**Adapter integration required by the probe.**

The current adapter (`backend/app/services/redis_pubsub.py`
on PR #32) will NOT accept the probe loopback as-is. Two
things block it, both documented so the enablement PR resolves
them explicitly:

1. `_parse_channel(channel)` at
   `backend/app/services/redis_pubsub.py:458` requires the
   channel to match `{prefix}:org:{orgId}:room:{roomId}` and
   returns `("", "")` for anything else. The probe channel
   `{prefix}:probe:{instance_id}` currently gets dropped at
   `_dispatch()` line 436.
2. `_dispatch()` at
   `backend/app/services/redis_pubsub.py:444` drops any
   envelope whose `publisher == ENV.INSTANCE_ID` — this is
   the self-broadcast suppression that protects production
   from double-delivery. The probe's loopback publish is
   self-published by design, so it would be dropped here.

The enablement PR resolves both by carving out a dedicated
probe path:

- New channel form `{prefix}:probe:{instance_id}` recognised
  by a new `_parse_probe_channel` sibling; `_dispatch`
  dispatches probe envelopes to a dedicated `_probe_callback`
  registered via `RedisPubSub.set_probe_callback(...)`.
  Probe envelopes carry an `is_probe=True` marker in the
  envelope for defence in depth (a channel-name-only
  discriminator is enough for correctness but the marker
  makes the branch obvious in incident review).
- The self-suppression check at
  `redis_pubsub.py:444` is preserved for production channels.
  Probe envelopes bypass it because the probe callback lives
  on a different code path.
- `ensure_subscription` is extended to accept probe channels
  (or a separate `ensure_probe_subscription` sibling) that
  writes into `_ref_counts` under a distinct key so
  `_reconnect`'s bulk resubscribe includes the probe channel
  on every reconnect (`redis_pubsub.py:474-476`).
- `stop()` cancels the probe task before `_teardown_clients()`
  so no probe attempt runs against a torn-down client. The
  probe task registers with the same shutdown path the
  reader task uses.

These are proposed adapter changes only — no code lands in
this doc's PR. The enablement PR implements them and lands
with a unit test verifying loopback delivery in `test_redis_pubsub.py`.

**Probe control plane and cadence.**

- Interval: 30 s under "CPU always allocated" mode
  (configurable via `REDIS_PROBE_INTERVAL_SEC`, default 30 s,
  min 10 s, max 300 s).
- Each tick, the probe publishes an envelope with a random
  `probe_id` on `{prefix}:probe:{instance_id}` via the
  adapter's `_pub` client and starts a 2 s wait for its own
  `_probe_callback` to fire with the same `probe_id`.
- On delivery: emit `redis_probe_ok` (INFO) with `rtt_ms`
  and `probe_id`.
- On timeout OR any exception in publish: emit
  `redis_probe_failed` (WARNING) with `reason` and `probe_id`.

**Missing-probe detection (`A8`).**

Cloud Monitoring's absence policy requires at least one
previous data point to arm — it cannot fire for an instance
that never emitted a first probe
(https://cloud.google.com/monitoring/alerts/concepts-indepth#absence-alerts).
It also does not know the §4a-1 roster; grouping is on
resource + extracted labels, not on an operator-supplied set.

`A8` has TWO complementary parts. Both are ONLY meaningful
when Redis is enabled — the adapter is silent when
`REDIS_ENABLED=0` and probe series do not exist. A8 is out
of scope during §4a-1 (Redis-independent room-drain) and
§4a-3 (Redis-disabled acceptance).

Cloud Monitoring's absence policy is instance-blind: it
groups by extracted labels, cannot enumerate an operator-
supplied roster, and can only fire on series that have
previously reported. Two failure modes fall OUTSIDE the
guarantees a metric-only design can offer and are handled
explicitly instead of pretending Cloud Monitoring covers
them:

  **Counter-example 1 — instance A retires while sibling B
  remains on the same revision.** Revision-level
  `container/instance_count` stays > 0 (B is still alive),
  so a revision-correlated absence policy still pages on
  A's now-silent probe series. Cloud Monitoring has no way
  to distinguish A's retirement from B's continued
  operation because the revision count aggregates them.

  **Counter-example 2 — instance C runs but its probe task
  never emits.** No prior `redis_probe_ok` series exists
  for C, so Cloud Monitoring's absence policy cannot arm
  against C at all. A7 (`active_probe_failure > 0`) only
  fires if C's probe task actually ran and raised; a task
  that never scheduled emits nothing.

Given these limits, A8 is defined as follows.

- **A8a — Steady-state absence, per-revision only**
  (continuous, once Redis is enabled). Cloud Monitoring
  absence policy on `redis_pubsub_active_probe_success`
  grouped by `resource.labels.revision_name` (NOT
  `instance_id`) with a Cloud-Run-side correlation on
  `run.googleapis.com/container/instance_count` on the
  same `revision_name`. Fires when NO
  `redis_pubsub_active_probe_success` series for the
  revision has reported in the last 2 min while
  Cloud Run's active+idle count for the same revision is
  positive.

  What A8a catches:
  - A revision where NO instance is emitting probes even
    though Cloud Run says instances are alive. This
    covers the whole-revision stuck state (e.g., a bad
    build silently disabling the probe task for every
    process on the revision).

  What A8a explicitly does NOT catch:
  - Counter-example 1 — A retires with B still emitting
    probes. B's probes keep the revision-level series
    alive, so A8a stays quiet by design. Per-instance
    retirement of one process while siblings continue is
    NOT observable from Cloud Monitoring alone. Handled
    by the operator helper (below) during windows, and
    accepted as a known steady-state gap otherwise.
  - Counter-example 2 — C never emits. The
    revision-level series is either still positive
    (siblings emitting) or absent from the start; either
    way the per-C absence cannot be evaluated. Handled by
    the operator helper (below) during windows.

- **A8b — Operator-window per-instance completeness**
  (windows ONLY). At §4d step 5 in the enable direction,
  the §4a-2 helper enumerates the roster from ticks + the
  Cloud Run cross-check (see §4a-1). For every roster
  member the helper waits up to `PROBE_FIRST_DEADLINE_SEC`
  (default 90 s) for at least one `redis_probe_ok` with a
  matching `jsonPayload.instance_id`. Missing =
  UNRESOLVED = window fails.

  A8b runs ONLY at §4d step 5 in the enable direction. It
  does NOT gate §4d step 3 (pre-deploy §4a-1 room-drain
  is Redis-independent) and does NOT run in the disable
  direction (§4a-3 verifies absence of Redis activity,
  not probe success).

  A8b resolves both counter-examples inside the window:
  it enumerates per-instance identity directly from the
  ticks (independent of Cloud Monitoring's grouping) and
  waits for a per-instance first probe (no reliance on a
  prior data point in Cloud Monitoring's series).

- **Explicit steady-state gap acknowledgement.** Outside
  operator windows, the per-instance completeness A8b
  provides is NOT available. The paired production
  signals are:
  - A6 — a reconnect_attempts / reconnect_successes
    mismatch on a specific `instance_id` still pages
    per-instance (because the metric is per-instance,
    not per-revision).
  - A5 — startup_failed without a follow-up
    reconnect_success on the same `instance_id` still
    pages per-instance.
  - A7 — a probe task that ran and raised still pages.
  A steady-state stuck-probe instance where reconnects are
  succeeding will not page from Cloud Monitoring; the
  operator's routine health review is the compensating
  control. This limitation is documented so ops knows the
  audit surface, not glossed as a Cloud-Monitoring
  guarantee.

### Pre-enablement observability check

Before the first §4d enable window:

1. Land the adapter emission-format changes (structured JSON
   `_emit` + probe integration) via the enablement PR.
2. Deploy the log-based metrics + alert policies (A5, A5-legacy,
   A6, A7, A8a) via the same pattern used in
   `ops/monitoring/reconciler/`. Google Cloud resources only,
   no Cloud Run change.
3. Verify each metric name is present in Cloud Logging and
   returns 0 samples (the adapter emits nothing while
   `REDIS_ENABLED=0`; the in-process probe task is inert while
   disabled).
4. **JSON emission smoke test.** On a canary revision at 0%
   traffic, flip `REDIS_ENABLED=1` briefly and confirm a
   `jsonPayload.event="redis_pubsub_started"` entry lands in
   Cloud Logging within 60 s with `jsonPayload.instance_id`
   populated. If it does not, the enablement PR's `_emit`
   integration is incomplete — fix that BEFORE any subsequent
   §4d window. Flip back to `=0` and record the smoke-test
   outcome in the audit trail.
5. On the same canary at 0% traffic (re-flipped to
   `REDIS_ENABLED=1`), verify:
   - `redis_pubsub_started` OR `redis_pubsub_reconnected`
     appeared for this instance (A5 pairs `startup_failed`
     with recovery; either origin proves the adapter is up).
   - `redis_pubsub_active_probe_success` for this instance
     ≥ 2 within the first 2 min of the probe running (A8b's
     first-probe deadline satisfied).
   - `redis_pubsub_startup_failed` on this instance is either
     0 OR followed by `redis_pubsub_reconnect_successes` on
     the same instance within 5 min (A5 paired condition).

The canary must return to `REDIS_ENABLED=0` before the operator
begins the real §4d enablement window.

## 4. Enablement gates and rollback (no-live-room-required)

`--max-instances=1` alone is not a sufficient guarantee (Track 1
Gate 2 established that Cloud Run keeps drained revisions alive on
active WebSockets). The enablement and rollback windows must be
strictly no-live-room, AND every deploy in the window must follow
the ordered sequence in §4d.

### 4a. Acceptance-check families (three separate sets)

Three check families exist. They are named separately because
they run at DIFFERENT phases of the window and depend on
DIFFERENT state. The reviewer flagged earlier drafts for mixing
Redis-health checks into rollback (where Redis is being
disabled) and into preflight (where Redis is still disabled
and the in-process probe task is inert).

Every family shares the same authoritative identity source:

- Cloud Run's `resource.labels` for a service log carries
  `service_name`, `revision_name`, `location` — but **no**
  `instance_id`. The Cloud Run instance id field name shifts
  across product surfaces and is not reliable for service logs.
- The APPLICATION identity we use is
  `jsonPayload.instance_id` (present on every `reconciler_tick`
  and on the probe log lines — the value of `ENV.INSTANCE_ID`
  auto-generated per process, see
  `backend/app/services/room_reconciler.py:170`,
  `backend/app/main.py:1319`).
- Cloud Logging exposes this via an explicit extraction —
  Log-based metrics and MQL queries reference it as
  `EXTRACT(jsonPayload.instance_id)` and Cloud Monitoring
  groups by that extracted label.
- Any check below that says "per instance" means per
  `(jsonPayload.instance_id, resource.labels.revision_name)`
  pair, using the extraction.

#### 4a-1. Room-drain checks — Redis-independent

These are the ONLY checks used inside §4d step 3 (confirm no
live rooms before deploying either an enable or a disable flip)
and inside §4e (rollback). They intentionally do NOT depend on
Redis health, so they work even when Redis is off or being
turned off.

**Roster source — application ticks are authoritative; Cloud
Run counts are a coverage cross-check.**

Only the application knows its own process identity. Cloud
Run's `run.googleapis.com/container/instance_count` reports
per-revision counts, not per-application-process IDs, so it
cannot serve as an exhaustive roster on its own. Its role is
to cross-check that the tick log has not missed a live
process — a live-but-silent instance would appear as a Cloud
Run count without a matching tick and is UNRESOLVED.

The roster at window entry is:

- Every `(instance_id, revision_name)` pair that emitted a
  `reconciler_tick` JSON event
  (`jsonPayload.event="reconciler_tick"`, see
  `app/services/room_reconciler.py:57`) in the last 30 min.
  This supplies the per-process identity.

Cross-checked against:

- The Cloud Monitoring metric
  `run.googleapis.com/container/instance_count`
  (https://cloud.google.com/monitoring/api/metrics_gcp#gcp-run)
  filtered on
  `resource.labels.service_name="worshiptranslate-backend"`,
  summed over BOTH `state="active"` AND `state="idle"`,
  grouped by `resource.labels.revision_name`. Cross-check
  uses STRICT EQUALITY: for each revision in EITHER the
  tick roster OR the metric (union), the metric's most
  recent settled sample must equal the tick-derived
  distinct-`instance_id` count for that revision at the
  same moment.
  - `metric > ticks` — Cloud Run reports more processes
    than the tick log identifies. A hidden or stuck
    process exists; UNRESOLVED.
  - `metric < ticks` — the tick log identifies more
    processes than Cloud Run reports alive. Almost always
    an old-tick residue, but sample-ingestion lag can also
    cause it transiently; UNRESOLVED and rechecked on the
    next sample.
  - `metric == ticks == 0` for a revision in the metric
    union — the revision has drained.
  - A revision that appears ONLY in the metric union (no
    matching tick roster) is UNRESOLVED — Cloud Run says
    processes exist for it but the application has never
    identified them.
  - A revision that appears ONLY in the tick roster
    (metric reports zero for it while ticks were recent)
    is UNRESOLVED until either the metric catches up or
    the tick residue ages out beyond the 30 min lookback.

Sampling caveats for the cross-check metric
(https://cloud.google.com/monitoring/api/metrics_gcp — container/instance_count):

- Sampled every 60 s.
- Visibility can be delayed up to 120 s from sample time.
- N consecutive samples are collected (N-1)×60 s apart
  and span exactly (N-1)×60 s of sampled evidence — two
  consecutive samples span 60 s, three span 120 s. The
  ingestion delay does NOT extend the SPAN of evidence;
  it only shifts when the operator can OBSERVE the
  samples. Any window that needs to observe a state
  persist for N seconds must count ceil(N/60)+1 samples,
  and the SAMPLES themselves must all report the state.

The cross-check requires the metric to be alive (a fresh
sample within the last 180 s — one full sampling interval
plus the documented delay). If no sample has landed in the
last 180 s the metric itself is UNRESOLVED; the operator
waits for the next sample rather than declaring coverage.

**Per-roster-member check.** The window can proceed only when
BOTH hold at the same moment:

1. **Firestore** — no `organizations/*/rooms/*` document has
   `status="live"`. One pass via the admin store the reconciler
   uses.
2. **Instance roster is clean** — every roster member has EITHER:
   - emitted two consecutive `reconciler_tick` events with
     `jsonPayload.owned_rooms=0` where the two ticks span at
     least the reconciler's own interval (see
     `ROOM_RECONCILER_INTERVAL_SEC`, currently 5 s) AND its
     youngest tick is within 60 s (silence during the window
     disqualifies), OR
   - been proven RETIRED. Retirement is proven ONLY from a
     window of samples that are ALL zero, including the
     newest sample. Rules:
     - Define the "retirement candidate window" as at least
       four consecutive samples on the instance's
       `revision_name`: three consecutive settled samples
       (samples whose collection timestamp is at least
       120 s in the past — past the documented visibility
       ceiling) plus the newest sample. Three settled
       samples span 120 s of sampled evidence; the newest
       sample proves the current state.
     - EVERY sample in that window must report
       `active+idle=0`. A single non-zero sample anywhere
       in the window — including a nonzero newest sample
       after older zeros — INVALIDATES retirement. In
       particular, a `0, 0, 1` sequence does NOT retire
       the revision because the newest sample is 1.
     - The window as a whole covers 120 s of sampled zero
       evidence + a fresh confirmation. This is
       intentionally conservative: two 60-s-apart zero
       samples on their own only measure emptiness at two
       moments and cannot prove continuous emptiness
       across the interval; requiring three consecutive
       settled zeros makes the observed window three
       samples wide, and requiring the newest sample also
       be zero rejects the "empty then refilled" case.
     - Any missing/stale sample in the window is
       UNRESOLVED, NOT retirement.
     - Additionally, the retirement candidate must NOT be
       contradicted by a fresh `reconciler_tick` from any
       `instance_id` on the same `revision_name` within
       the same window — a tick during the retirement
       window means an application process is still
       running, and retirement is invalidated regardless
       of what the Cloud Run metric shows.
     - Once retired, the instance is removed from the
       active roster for the rest of the window; the
       audit trail records
       `(revision, instance_id, retired_at,
       retirement_confirmed_by="instance_count_all_zero_window_and_no_recent_tick")`.
     - Because revision-level counts cannot identify
       individual processes, retirement of a single
       `instance_id` while another instance on the SAME
       `revision_name` remains alive is proven ONLY when the
       revision count reaches zero across the settled window.
       An operator retiring one of several sibling instances
       must wait for the revision as a whole to drain.

A helper script (successor to `~/.gate2-helpers-v5/gate2_preflight.sh`)
formalises this and emits, for the audit trail:

- `no_live_rooms_verified_at=<UTC>`
- `roster_source_cloud_run=[revision, active+idle_last_sample, sample_at, …]`
- `roster_source_tick_log=[(revision, instance_id, last_tick_at, last_owned_rooms), …]`
- `roster_union=[(revision, instance_id, status="present"|"retired"|"unresolved")]`
- `firestore_live_room_count=0`
- `retired_this_window=[(revision, instance_id, retired_at), …]`
- `unresolved_this_window=[(revision, instance_id, reason)]` (empty in the pass case)

#### 4a-2. Redis-enabled acceptance — post-`REDIS_ENABLED=1` deploy

Runs at §4d step 5 ONLY after a flip to `REDIS_ENABLED=1`. The
roster used here is the post-deploy roster (a fresh §4a-1
roster enumeration against the new revision). For every roster
member:

- **Adapter is up** — verified by EITHER
  `jsonPayload.event="redis_pubsub_started"` OR
  `jsonPayload.event="redis_pubsub_reconnected"` for this
  `instance_id` in the last 5 min. The successful-recovery
  case (initial ping failed → reader loop repaired) emits
  ONLY `redis_pubsub_reconnected`, not `redis_pubsub_started`
  — accepting only `started` would spuriously fail a
  legitimately-healthy instance that started while Memorystore
  was briefly unreachable.
- **Fresh probe success after adapter is up** — at least one
  `jsonPayload.event="redis_probe_ok"` for this `instance_id`
  landed AFTER the timestamp of the `started` OR `reconnected`
  event above (the probe attempt uses the adapter's real
  `_pub`/`_pubsub` — see §3 probe integration). A probe from
  BEFORE the adapter came up does not count.
- **Startup/reconnect pairing per A5** —
  `redis_pubsub_startup_failed` for this instance is either 0
  OR followed by a `redis_pubsub_reconnect_successes` event on
  the same instance within 5 min. A lone startup_failed is
  expected under Standard-tier ~15 s failover; a lone one
  without a follow-up recovery fails the window.
- **First-probe deadline per A8b** — `redis_probe_ok` fired
  within `PROBE_FIRST_DEADLINE_SEC` (default 90 s) of the
  instance's first `reconciler_tick`. Missing first probe is
  UNRESOLVED and fails the window even if steady-state A8a
  cannot fire.

#### 4a-3. Redis-disabled acceptance — post-`REDIS_ENABLED=0` deploy (rollback)

Runs at §4d step 5 when the flip direction is toward disabled.
The adapter emits nothing while disabled; verifying "off" is a
matter of proving both the deployed CONFIGURATION and the
absence of Redis activity.

- **Explicit configuration check — read the serving
  revision, not the template, using validated structured
  output.** `spec.template...env` on a service describes
  the LATEST configured template, which may differ from
  the revision actually receiving traffic (e.g., a newer
  canary at 0 %, or a stalled rollout). The earlier draft
  used a shell `for` loop, which silently succeeds when the
  serving list is empty; the check MUST fail on empty or
  malformed output and MUST select positive-traffic
  revisions structurally. Reference implementation
  (executable as-is in the helper script):

  ```bash
  set -euo pipefail

  svc_json=$(gcloud run services describe worshiptranslate-backend \
      --region us-central1 --format=json) \
      || { echo "STOP: gcloud describe failed"; exit 1; }

  # Parse in Python so we fail loudly on unexpected shape.
  python3 - "$svc_json" <<'PY'
  import json, sys, subprocess
  svc = json.loads(sys.argv[1])
  traffic = svc.get("status", {}).get("traffic") or []
  # Serving revisions = entries with numeric percent > 0.
  serving = [
      t["revisionName"]
      for t in traffic
      if isinstance(t.get("percent"), int) and t["percent"] > 0
      and t.get("revisionName")
  ]
  if not serving:
      sys.exit("STOP: status.traffic reports no revisions with percent > 0")

  bad = []
  for rev in serving:
      rev_json = json.loads(subprocess.check_output([
          "gcloud", "run", "revisions", "describe", rev,
          "--region", "us-central1", "--format=json",
      ]))
      containers = (
          rev_json.get("spec", {})
                  .get("containers") or []
      )
      if not containers:
          bad.append((rev, "no containers in spec"))
          continue
      env = containers[0].get("env") or []
      # env is a list of {"name": ..., "value": ...} or
      # {"name": ..., "valueFrom": {...}}. Match on
      # literal value only; a secret ref for REDIS_ENABLED
      # is refused.
      found = [e for e in env if e.get("name") == "REDIS_ENABLED"]
      if not found:
          bad.append((rev, "REDIS_ENABLED missing"))
      elif "valueFrom" in found[0]:
          bad.append((rev, "REDIS_ENABLED is a secret ref, expected literal 0"))
      elif found[0].get("value") != "0":
          bad.append((rev, f"REDIS_ENABLED={found[0].get('value')!r}"))

  if bad:
      for rev, reason in bad:
          print(f"STOP: {rev}: {reason}", file=sys.stderr)
      sys.exit(1)
  print(f"OK: {len(serving)} serving revision(s) all report REDIS_ENABLED=0")
  PY
  ```

  All serving revisions must show `REDIS_ENABLED=0` as a
  literal value (not a secret ref); a split between two
  revisions where only one carries the flip is a stop
  (rollback is not complete). An empty serving set is
  ALSO a stop — the service is not routing traffic and
  the deploy is unresolved. Absence of Redis log activity
  alone cannot prove the flag was actually flipped — a
  code deploy that failed to include the env change would
  look identical from the log side. This step is
  MANDATORY.
- **Log activity absence** — for every post-deploy roster
  member (fresh §4a-1 enumeration), NO
  `jsonPayload.event` starting with `redis_pubsub_` and NO
  `jsonPayload.event="redis_probe_ok"` /
  `"redis_probe_failed"` events in the last 5 min. The
  adapter's `start()` early-outs on `_enabled=False` so a
  correctly-disabled adapter is silent by construction.
- **Room-drain unchanged** — the §4a-1 checks continue to
  pass throughout the rollback window.

Do NOT run any Redis-health probe during §4a-3; the probe
requires an enabled adapter and is silent when disabled by
design.

### 4b. Prevent new sessions during the window — global maintenance gate

The block must (a) cover BOTH start endpoints, (b) apply to
every organization including newly-created ones, (c) prevent a
race between "flag checked" and "room created" for a request
already in flight, and (d) itself be safely deployable in the
first place. A per-org flag cannot satisfy (b), and a
non-transactional check cannot satisfy (c).

**Endpoints covered.** Both entry paths for a live room:

- `POST /api/org/{orgId}/service/{serviceKey}/start` — the
  authenticated org-scoped path (`routes/multichurch.py:295`).
- `POST /api/c/{slug}/service/{service_key}/start` — the
  public church-slug path (`routes/multichurch.py:311`).

Missing either lets a new host create a live room between the
no-live-room verification and the deploy.

**Mechanism — single global maintenance gate.**

- Location: a single Firestore document at `system/deploy_gate`
  (a service-level singleton outside `organizations/*`, so
  organization creation cannot bypass it).
- Fields:
  - `blocked` (bool)
  - `blocked_at` (timestamp)
  - `reason` (string; short human-readable audit note)
  - `blocked_by` (string; operator identity)
  - `revision` (int; increments on every set/clear, used as
    the transaction precondition token)
- Both start endpoints, and any FUTURE start endpoint, MUST
  consult `system/deploy_gate` before creating a room.

**Preventing the check/create race — transactional room creation.**

The block is enforced inside the SAME Firestore transaction
that creates the room, not by an early return in the handler:

```
with client.transaction() as tx:
    gate = tx.get(system_deploy_gate_ref)      # read inside tx
    if gate.exists and gate.get("blocked"):
        raise HTTPException(503, "maintenance", Retry-After=60)
    tx.set(rooms/{room_id}, {..., status="live", ...})
```

The transaction guarantees the gate cannot flip to `blocked`
between the read and the create. A request already in flight
when the operator sets the gate either (a) commits the room
before the gate change and appears on the roster (§4a-1 will
require it to drain before proceeding) OR (b) sees the gate on
retry and returns 503. There is no third case.

**Coverage of new organizations.** The gate is a single global
document. New organizations do NOT need any per-org gate write
— every new-org start request reads the SAME
`system/deploy_gate` in its transaction.

**Bootstrap — deploying the block-aware code safely.**

The endpoints don't consult the gate until the code that reads
it ships. That first deploy is itself a Cloud Run revision, so
it must run through §4d — but §4d itself relies on the gate
that BOOT-1 is installing. Chicken-and-egg: BOOT-1 cannot use
the transactional gate to protect its own deploy.

Neither §4a-1 room drain alone nor the maintenance-window
communication alone is sufficient. A start request that arrives
between "drain confirmed" and "new revision at 100 % traffic"
can still create a live room on the OLD (gate-less) code,
which the drain check will not see because that check has
already completed.

BOOT-1 needs an EXTERNAL block that (i) prevents new starts,
(ii) preserves routing for existing sessions on the same
underlying Cloud Run revision that was already serving them,
and (iii) does not itself introduce a second revision — a
second revision is the exact isolation shape §4d's transactional
gate later prevents, and reintroducing it here would recreate
the Gate 2 failure surface on BOOT-1's own window.

The earlier draft's `boot1-shim` revision fails (ii) and (iii):
a shim is a separate Cloud Run revision, so a host WebSocket
on the current revision plus a listener reconnect landing on
the shim is exactly the split-revision isolation Gate 2 hit.
Every request-handling process on Cloud Run is bound to its
own revision, and reconnection is not guaranteed to land on
the same revision as the original connection.

Two options remain. Neither uses a second revision.

- **Option A — External load-balancer path rule that blocks
  the two `start` paths only** (preferred if the account has
  Cloud Run behind Google Cloud Load Balancing).
  A Cloud Load Balancer URL map rule (or an equivalent Cloud
  Armor rule) matches the exact paths `/api/org/*/service/*/start`
  and `/api/c/*/service/*/start` and returns 503 with
  `Retry-After: 60` at the load balancer layer, BEFORE routing
  to Cloud Run. Every other route (End Service, WebSocket
  upgrades and reconnects, health checks, metrics) continues
  to route unchanged to the current serving revision.
  - Existing sessions keep talking to the same processes on
    the same revision because the URL map does not touch
    non-`start` paths.
  - New starts get 503 at the LB layer with no involvement of
    a second Cloud Run revision.
  - Prerequisite: the account currently fronts Cloud Run with
    Google Cloud LB. Verify with
    `gcloud compute url-maps list --filter='defaultService~
    worshiptranslate'` before selecting Option A. If the
    service is exposed via its direct `run.app` URL only,
    Option A is not available and Option B applies.

  Steps during BOOT-1's window:
  1. Add the URL-map path rule that returns 503 for the two
     `start` paths. Verify from OUTSIDE Cloud Run that each
     start path now returns 503 and that a non-`start` path
     (e.g., health check) still returns 200.
  2. Wait for §4a-1 to confirm no live rooms and drained
     roster (the LB is forwarding End Service and reconnects
     unchanged throughout — same underlying revision, same
     processes).
  3. Deploy BOOT-1's revision with 100 % traffic on the
     existing service. Because the LB rule and the URL map
     are separate resources from the Cloud Run revision, the
     rule remains in effect across the revision cut.
  4. Verify BOOT-1 is serving (see BOOT-2 below).
  5. Remove the URL-map path rule; both start endpoints
     resume serving via BOOT-1's gate-aware handlers.

- **Option B — Documented operator-controlled maintenance
  window** (fallback when Option A is not available).
  Ops publishes a maintenance notice to the church operators
  at least 48 h in advance, targeting a genuinely quiet
  window (e.g., Tuesday 02:00 CDT). During the window:
  (1) confirm Firestore has zero live rooms AND zero rooms
  started in the last 5 min (indicating the notice is being
  observed); (2) deploy BOOT-1 with 100 % traffic on the
  existing service (no traffic-split, no second revision);
  (3) verify BOOT-1 is serving.
  - Explicit residual-risk decision — NOT "self-limiting".
    Option B does NOT prevent a live room from being created
    during the window. If one is created after step 1's
    drain check completes and before BOOT-1 is serving, the
    result is the ORIGINAL Gate 2 failure shape: a room
    running on the gate-less code that could still see the
    cross-instance isolation observed at Gate 2 if the
    subsequent §4d enablement deploy proceeds while it is
    live. Option B is a documented residual risk the
    operator accepts to ship BOOT-1 without Option A's
    LB-level block; it is NOT a safety guarantee.
  - Option B is acceptable ONLY when the operator has
    confirmed (via church-operator communication) that the
    quiet window will not include a live service and is
    prepared to hold the subsequent §4d enablement deploy
    until any such room ends. It is NOT acceptable for
    subsequent §4d windows where the transactional gate is
    available and where a mid-deploy room can leave the
    service in an inconsistent Redis/local-only state.

Once BOOT-1 is serving, §4d can use the transactional gate
for every subsequent window.

1. **BOOT-1** — Land the gate-reading code in a separate,
   earlier PR. Behaviour: reads `system/deploy_gate`; when
   the document is absent it treats the gate as unblocked. So
   BOOT-1 is a no-op change until the operator writes the
   document. Deploy BOOT-1 using Option A or Option B above.
2. **BOOT-2** — Verify BOOT-1 is on 100 % traffic and behaves
   as no-op (`system/deploy_gate` still absent; both start
   endpoints succeed and both consult the gate — verify via
   log inspection or a synthetic request whose transaction
   ID appears in Firestore audit logs reading the gate
   document).
3. From this point on, every §4d deploy sets `blocked=True`
   in step 1 and clears it in step 6, using the transactional
   gate — Option A and Option B are no longer used.

**Behaviour observed by clients.**

- Blocked state: both endpoints return 503 with
  `Retry-After: 60` before any Redis or Deepgram call. Existing
  WebSocket connections are NOT touched — §4a-1 drains them
  before the deploy. The block covers new starts only.
- Cleared state: normal service.

**Audit trail.** The `revision` field on `system/deploy_gate`
gives every set/clear a monotonic sequence number that the
window helper records in
`no_live_rooms_verified_at=<UTC>` alongside the gate revision
seen at check time. A post-hoc audit can prove the gate was set
before the drain check and cleared after the post-deploy check.

### 4c. Enablement — one-time infrastructure preparation

§4c covers work that does NOT change the running Cloud Run
service. Anything that creates a new Cloud Run revision (env
change, VPC path attachment, code deploy) is a service-affecting
deployment and moves out of §4c into the ordered windows in §4d
— Cloud Run configuration changes create new revisions
(https://cloud.google.com/run/docs/managing/revisions).

`REDIS_ENABLED=0`, `ROOM_RECONCILER_ENABLED=1`,
`--max-instances=1` remain pinned throughout §4c.

**Truly out-of-band pre-work — runs anytime, no window needed:**

1. Enable `vpcaccess.googleapis.com` (idempotent API enablement).
2. Provision the VPC egress path (does not attach yet):
   - **Direct VPC egress** preferred — reserve an egress subnet
     with private Google access. No connector to provision, no
     Cloud Run change until §4d attaches it.
   - **Serverless VPC Access Connector** fallback — create the
     connector in `us-central1` attached to the default network;
     verify state=READY. Does not affect Cloud Run until §4d
     attaches it.
   - Whichever mode is chosen becomes the SINGLE networking
     mode for the rollout; see §5 acceptance criteria.
3. Provision a new Cloud Memorystore Redis instance. **Standard
   tier**, not Basic — Standard provides a replicated HA setup
   and a documented maintenance policy. **Standard does NOT
   eliminate interruptions**: Google documents planned failover
   at ~15 s with client-side reconnection required
   (https://cloud.google.com/memorystore/docs/redis/failover-and-patching).
   The startup-recovery fix in §2 (PR #32, CI-verified) is what
   makes that 15 s survivable end-to-end. Note the internal IP.
4. If the new Memorystore instance uses AUTH: update the
   `redis-password` secret in Secret Manager to the new
   instance's password. If the new instance has AUTH disabled:
   remove the stale `REDIS_PASSWORD` secret binding from Cloud
   Run entirely in the §4d-1 window (Cloud Run env change);
   do not leave a dangling reference to a secret whose value no
   longer maps to a live credential.
5. Provision the log-based metrics + Cloud Monitoring alert
   policies (A5, A5-legacy, A6, A7, A8a) — Google Cloud
   resources only, no Cloud Run change. The active probe is
   in-process on each backend (see §3) — there is no Cloud
   Scheduler job and no external probe endpoint to provision.

Steps 1-5 above do not create new Cloud Run revisions and can be
staged in advance. The window-required deploys live in §4d.

### 4d. Deploy window — the ordered sequence

Every window-required deploy (env flip, VPC egress attachment,
password-binding removal, code deploy affecting the running
service) follows this sequence exactly. Skipping or reordering
any step re-creates the Gate 2 failure shape.

Two sub-flavours share the same sequence but pick different
post-deploy check families in step 5:

- **Enable direction** — deploys that leave the service with
  `REDIS_ENABLED=1` (either flipping from `0→1`, or a code
  deploy while Redis is enabled). Step 5 runs §4a-2 (Redis-
  enabled acceptance).
- **Disable direction** — deploys that leave the service with
  `REDIS_ENABLED=0` (either flipping from `1→0`, or any code
  deploy while Redis is disabled). Step 5 runs §4a-3 (Redis-
  disabled acceptance).

The room-drain check in step 3 (§4a-1) is Redis-independent and
is the SAME in both directions — it must complete before the
deploy regardless of which side of the flip we are on.

1. **Block new starts** — write `system/deploy_gate` per §4b.
   Both endpoints will return 503 for any request whose Firestore
   transaction reads the gate after this write commits.
2. **Verify the block is in effect** — issue one request to each
   of the two start endpoints from outside Cloud Run and confirm
   both return 503. Also confirm the gate document's `revision`
   matches the value the operator just wrote (rules out a stale
   Firestore replica). A stale routing layer or forgotten
   preview URL that still accepts starts is a stop.
3. **Confirm no live rooms — §4a-1 room-drain checks only**.
   Firestore live-room count zero + every rostered
   `(revision, jsonPayload.instance_id)` pair either shows two
   consecutive `owned_rooms=0` ticks with a fresh youngest tick
   OR is proven retired via Cloud Run revision status. Persist
   `no_live_rooms_verified_at=<UTC>`, the full roster, and the
   gate revision to the audit trail. Do NOT run §4a-2 or §4a-3
   here — they are post-deploy checks.
4. **Deploy the change** — apply the env flip / VPC attachment /
   password-binding change / code deploy via the deploy
   workflow. Wait for the new revision to reach 100 % traffic
   AND for §4a-1 to re-verify the post-deploy roster (a new
   revision brings new instances that must be included).
5. **Verify post-deploy — check family selected by direction:**
   - Enable direction → run §4a-2 in full. Adapter-up is
     satisfied by EITHER `redis_pubsub_started` OR
     `redis_pubsub_reconnected` per §4a-2. A lone per-instance
     `startup_failed` is expected under Memorystore Standard
     ~15 s failover and is NOT a stop on its own; the paired
     condition (§3 A5) — `startup_failed` with no follow-up
     `reconnect_successes` on the same instance within 5 min —
     IS a stop and rolls back to step 1 with the opposite
     direction.
   - Disable direction → run §4a-3 in full. Explicit
     `REDIS_ENABLED=0` env verification on the serving
     revision is MANDATORY (absence of Redis log activity
     alone cannot prove configuration). Any post-deploy
     instance emitting `redis_pubsub_started` or
     `redis_pubsub_reconnected` AFTER the flip is a stop and
     rolls back.
6. **Reopen new sessions** — clear `system/deploy_gate` per
   §4b. Both endpoints resume accepting starts on the next
   transaction that reads the cleared document.
7. **Track 1 Gate 2 rerun** (enablement direction only) with
   the v6 helpers.

### 4e. Rollback (no-live-room required)

Rolling back `REDIS_ENABLED=1 → 0` recreates the exact isolation
Gate 2 failed on. A live-session rollback is unsafe and must not
be attempted. The rollback procedure follows §4d disable-
direction end-to-end: block starts → verify block → run §4a-1
(room-drain, Redis-independent) → deploy `REDIS_ENABLED=0` →
run §4a-3 (Redis-disabled acceptance) → reopen. If no window is
available for a critical rollback, terminate live rooms via the
admin End Service path first, then run §4d.

Redis-health checks are NOT part of the rollback critical path.
The adapter's in-process probe task is inert while
`REDIS_ENABLED=0` and its metrics stop incrementing; §4a-3
checks the ABSENCE of Redis activity, which is the correct
signal in this direction.

If the reason for rollback is Memorystore itself is unhealthy,
the reconciler continues to work independently (PR-T1-C is
Redis-independent by design) — but the cross-instance fanout
scenario the Gate 2 rerun tests is exactly what fails. Roll
back during no-live-room windows only.

## 5. v6 Gate 2 helper acceptance criteria (for a later PR)

v5 hardcodes `analyze_cloud_run`'s guard as `redis_enabled == "0"`.
v6 must:

- Accept an expected Redis state (`redis_enabled == "0" | "1"`)
  driven by a helper env `GATE2_EXPECTED_REDIS`.
- When `GATE2_EXPECTED_REDIS=1`, additionally verify at run time:
  - Cloud Run has the operator's chosen VPC egress path attached
    — either a `vpc-access-connector` OR a Direct VPC egress
    subnet. `GATE2_VPC_MODE={connector|direct}` selects which
    field to require; the check passes when the matching field
    is present and non-empty. Do NOT require both; the two are
    mutually exclusive per §4c step 2.
  - AUTH secret binding matches the Memorystore instance:
    - If the instance has AUTH enabled, `REDIS_PASSWORD` must
      be a Cloud Run secret ref that resolves in the current
      revision.
    - If the instance has AUTH disabled, `REDIS_PASSWORD` must
      be absent from the revision's env — a stale binding to a
      no-longer-existent secret ref is a stop.
  - A per-revision preflight log query shows EITHER
    `jsonPayload.event="redis_pubsub_started"` OR
    `jsonPayload.event="redis_pubsub_reconnected"` for the
    serving revision (see §3 event catalogue). Requiring
    ONLY `redis_pubsub_started` would spuriously fail an
    instance whose initial ping raised — its recovery event
    is `redis_pubsub_reconnected`, and that is what §4a-2
    also accepts.
  - `redis_pubsub_startup_failed` counted paired with
    `redis_pubsub_reconnect_successes` across the deploy
    window per A5's paired-condition definition — a lone
    `startup_failed` is expected under Standard-tier ~15 s
    failover and does NOT fail the window, but a
    `startup_failed` without a follow-up reconnect success on
    the same `instance_id` within 5 min DOES.
  - `redis_probe_ok` for the serving revision's instances
    satisfies A8b's first-probe deadline (default 90 s from
    each instance's first `reconciler_tick`).
- When `GATE2_EXPECTED_REDIS=0`, additionally verify at run
  time:
  - The serving revision's env carries `REDIS_ENABLED=0`
    (queried from Cloud Run, not inferred from log absence —
    see §4a-3).
  - No `jsonPayload.event` starting with `redis_pubsub_` and
    no `redis_probe_*` events in the last 5 min.
- Retain every existing v5 check that would stop the run when
  evidence is missing.
- Ship with fixture-driven tests for both the Redis-disabled
  (backward-compatible) and Redis-enabled paths.

v6 lands as a review-only branch after this rollout proposal is
approved and after PR #32 is merged.

## 6. Non-goals

- No production enablement is proposed by this document. Approval
  gates each subsequent step.
- No change to `--max-instances`. Multi-instance is Track 2.
- No change to the reconciler. PR-T1-C's Redis-independent cleanup
  path remains the primary safety net during and after enablement.

## 7. Open items for the reviewer

Resolved in this revision (formerly items 1, 3, 4):

- Memorystore tier — **decided: Standard** (§4c step 3). Basic's
  lack of HA and its uncontrolled maintenance drops make the
  startup-recovery fix a workaround rather than an eliminator of
  the failure mode.
- Reject-new-sessions mechanism — **decided: in-app session-block
  flag in Firestore** (§4b). A traffic-tag approach fires a 503
  from the wrong revision and does not compose with the
  per-service scoping the flag supports.
- F-27 Toxiproxy timing — **event-driven** (F-27 waits for both
  backends to log an initial-failure line — accepting both the
  fixed and the pre-fix wording — before restoring the proxy,
  and its hard acceptance is cross-instance marker delivery at
  step 6, not any specific log string). Documented in the F-27
  module docstring on PR #32.

Still open:

1. Alert threshold on `redis_pubsub_reconnect_attempts` — 3/5 min
   per instance is a first-cut number. Tune after two weeks of
   Redis-enabled traffic; leave at 3/5 min for the enablement
   window.
2. Roster retirement RTT — §4a-1 requires three consecutive
   settled zero samples on `run.googleapis.com/container/instance_count`
   PLUS a zero on the newest sample PLUS no fresh
   `reconciler_tick` from any instance on the revision
   during the same window. Minimum wall-clock cost is the
   settled portion (120 s of sampled evidence) + the 120 s
   ingestion visibility ceiling = 240 s, plus operator
   observation time. Confirmable only under real Cloud Run
   drain behaviour; leave as first-cut for the initial
   window and retune.
3. Cloud Run CPU allocation mode for the enablement revision —
   §3 assumes "CPU is always allocated" so the 30 s in-process
   probe can fire reliably during idle periods. Cost impact
   over "CPU only during requests": Cloud Run bills CPU-seconds
   at the always-allocated rate. Confirm operator acceptance of
   the higher steady-state cost before the enablement PR ships
   with the always-allocated setting; otherwise select the
   health-check-piggyback fallback described in §3.
4. `PROBE_FIRST_DEADLINE_SEC` default (90 s) — §3 A8b's first-
   probe deadline is 3× the 30 s probe interval. Tighter than
   the reconciler's own tick cadence but generous enough for
   cold-start pubsub subscribe. Retune if enablement-window
   observations show consistent margin.
5. BOOT-1 Option A prerequisite — the LB-level start-block
   requires that the Cloud Run service currently sits behind
   Google Cloud Load Balancing. If the service is exposed
   through its `run.app` URL directly, Option A is not
   available and BOOT-1 must use Option B (documented
   residual-risk maintenance window). Confirm the LB fronting
   before scheduling BOOT-1.
6. Steady-state A8 gap OUTSIDE operator windows — §3 A8
   explicitly acknowledges Cloud Monitoring cannot enforce
   per-instance probe completeness in steady state (a
   retired instance while a sibling on the same revision
   remains alive; a probe task that never emits at all).
   Compensating controls: A5 (per-instance), A6
   (per-instance), A7 (probe raise/timeout). Ops's routine
   review of `redis_probe_ok` distinct-`instance_id` counts
   vs Cloud Run count is the current fill-in. Consider
   whether a periodic audit query is worth automating; the
   answer depends on how often the specific stuck-probe-
   sibling shape actually materialises in production.

## 8. Audit — §9a scope amendment

Formally: Redis enablement performed at `--max-instances=1` does
NOT extend Track 1's scope.

- Track 1 exit criteria (unchanged): reconciler enabled + Gate 1
  passed + Gate 2 controlled-deploy reconnect passes + Gate 3
  7-day soak passes.
- Redis fanout is a **Track 2 dependency surfaced early** because
  Gate 2 cannot be re-run safely without it (issue #29). It is
  not a Track 1 deliverable; the enablement PR is a scope
  precondition, not a scope expansion.
- After Redis is enabled at `--max-instances=1`, the Gate 2 rerun
  and Gate 3 soak are still the same Track 1 gates against the
  same acceptance criteria. Multi-instance operation (raising
  `--max-instances`) remains a Track 2 change and stays out of
  scope until Track 1 exits.
- Documentation impact: the Track 1 rollout plan
  (`docs/02-design/…/track1-rollout.plan.md`) gains a one-line
  reference to this §8 amendment. Existing Track 1 audit trail
  (PR #28 evidence, Gate 1 memo) is not restated.

The rationale for surfacing this amendment: without it, the
enablement PR looks like a Track 1 scope creep and can be
mis-approved as such. Naming Redis as a Track 2 dependency being
resolved early keeps the Track 1 audit trail crisp.
