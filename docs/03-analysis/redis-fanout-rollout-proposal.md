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

The rollout outline previously named `redis_pubsub_reconnects` as if
that metric already existed. It does not — the adapter emits **log
lines**, not a named metric. This section pins the metric definitions
to the exact log strings.

### Adapter log strings (verbatim, from `redis_pubsub.py` on PR #32)

| Log string | Severity | Meaning |
|------------|----------|---------|
| `redis pubsub started host=%s:%s prefix=%s instance=%s` | INFO | One-time success at process start |
| `redis pubsub initial connect failed; reader loop will retry: %s` | WARNING | Fixed adapter — initial ping raised (connection-refused shape); reader loop is scheduled and will retry |
| `redis pubsub initial connect failed: PING timed out after %.2fs (TCP accepted but no reply); reader loop will retry` | WARNING | Fixed adapter — initial ping bounded by `asyncio.wait_for` fired the timeout branch (TCP-accepted-but-hung shape); reader loop is scheduled and will retry |
| `redis connect failed; falling back to local-only broadcast: %s` | ERROR | **Pre-fix adapter tell.** Should never appear in production once PR #32 is deployed. Detected during Gate 2 rollback drills to prove a rolled-back version reproduces the defect. |
| `redis pubsub reconnecting in %.1fs (attempt %d)` | INFO | Reader loop is attempting to reconnect |
| `redis pubsub reconnected; %d rooms resubscribed …` / `redis pubsub reconnected; 0 rooms to resubscribe` | INFO | Reconnect succeeded (possibly with 0 rooms) |
| `redis reconnect failed: %s` | WARNING | Reader-loop reconnect attempt failed |
| `pubsub reader error: %s` | WARNING | Reader-loop caught an exception; will re-enter reconnect on next iteration |

### Proposed log-based metrics (all counters, no gauges)

Metric names match the `ops/monitoring/reconciler/` convention
(underscored, service-scoped filter):

| Metric | Filter (jsonPayload-agnostic textPayload contains-match) | Purpose |
|--------|----------------------------------------------------------|---------|
| `redis_pubsub_startup_failed` | `textPayload:"redis pubsub initial connect failed" OR textPayload:"redis connect failed"` | Rise indicates the initial ping raised on some instance. Both the fixed-adapter and pre-fix wording are matched so the metric survives rollout of PR #32 without a gap. Non-zero is not zero-tolerance any more (the fixed adapter's reader loop repairs it) — it becomes the SIGNAL that A6 should also fire, and pages via A6 if reconnects do not follow. |
| `redis_pubsub_reconnect_attempts` | `textPayload:"redis pubsub reconnecting"` | Baseline for reconnect noise. Occasional attempts are normal during Memorystore maintenance. |
| `redis_pubsub_reconnect_successes` | `textPayload:"redis pubsub reconnected"` | The recovery signal. If attempts rise but successes don't, the connection is broken. |
| `redis_pubsub_reconnect_failures` | `textPayload:"redis reconnect failed"` | Direct failure counter. |
| `redis_pubsub_reader_errors` | `textPayload:"pubsub reader error"` | Reader-loop crashes (transient socket drops). Occasional is fine; sustained is not. |
| `redis_pubsub_active_probe_failure` | `textPayload:"redis_probe_failed"` | Emitted by the in-process active probe (see below). Per-instance failure signal that exercises the adapter's real pub + sub path. |
| `redis_pubsub_active_probe_success` | `textPayload:"redis_probe_ok"` | Positive-heartbeat counter. Absence pages via A8 (metric-absence alarm) when the roster expects a probe from an instance and none has arrived in the last 2 min. |

All six metrics filter on
`resource.type="cloud_run_revision" AND resource.labels.service_name="worshiptranslate-backend"`.

### Alerts (proposed)

All alerts evaluated **per instance** (Cloud Monitoring: group by
`resource.labels.revision_name` AND
`resource.labels.instance_id`). A per-service aggregate hides the
single-instance failure mode Gate 2 was actually about — one
revision/instance silently degrading to local-only while the other
looks healthy.

- `A5 startup_failed > 0 in 5 min` (per instance) with
  `reconnect_successes == 0 in the following 5 min` — pages.
  On the fixed adapter a startup failure is expected to be
  followed by a reconnect success; pairing them separates the
  "Redis briefly unreachable, recovered" case (noise) from the
  "Redis unreachable and adapter stuck" case (page). Pre-fix
  behaviour would page on every occurrence, so the same alert
  correctly re-fires zero-tolerance if PR #32 is rolled back.
- `A6 reconnect_attempts > 3 in 5 min without corresponding
  successes on the same instance` — pages. Indicates Memorystore
  or VPC path is unhealthy for that instance.
- `A7 active_probe_failure > 0 in 5 min` (per instance) — pages.
  Defined below. Exercises the adapter's actual reader path so a
  stuck subscriber cannot be masked by a fresh Redis client.
- `A8 no active_probe_success from a rostered instance in 2 min`
  (per instance, metric-absence alarm) — pages. Catches silent
  instances that stopped emitting probe heartbeats altogether.
  Depends on the §4a-1 roster to know which instances SHOULD be
  reporting.
- The existing A1 reconciler-freshness alert continues to page on
  its own — a Redis outage does not affect the reconciler.

### Active pub/sub probe (`A7` source signal)

Log-based counters only tell us what the adapter itself emitted.
They cannot tell us that a message we publish is actually
received by our OWN adapter's subscriber. That is the exact
failure the startup-recovery bug (§2) produced: the adapter's
reader task was never scheduled, so its subscribe path was
dead — a probe using its own fresh Redis client would have
succeeded and hidden the defect. The probe MUST exercise the
adapter's actual reader path.

**Coverage — Cloud Run routing does not give per-instance access.**

A request to a Cloud Run service URL selects an instance by load
balancing; session affinity is best-effort, not guaranteed
(https://cloud.google.com/run/docs/triggering/session-affinity).
Polling a revision URL from Cloud Scheduler cannot guarantee
every instance is exercised. The coverage mechanism instead runs
INSIDE each backend process:

- A background probe task starts alongside the reader loop.
  Interval: 30 s (configurable via `REDIS_PROBE_INTERVAL_SEC`).
- Each tick, the probe publishes a marker on a dedicated per-
  instance channel `worshiptranslate:probe:<instance_id>` via
  the ADAPTER'S `_pub` client. The instance's OWN adapter is
  already subscribed to that channel via `ensure_subscription`
  at start-up.
- The probe waits for the adapter's reader-loop to deliver the
  marker back through the same callback path production
  broadcasts use. Deadline: 2 s.
- On delivery: log
  `redis_probe_ok instance=<id> rtt_ms=<n>` (INFO).
- On timeout OR any exception: log
  `redis_probe_failed instance=<id> reason=<...>` (WARNING).

Because the probe runs in-process, coverage is by construction —
every live instance emits its own probe result. Silent instance
means silent probe means alert fires (see below).

**Probe correctness — it exercises the failure surface.**

- Publish path: uses `_pub` — same client production broadcasts
  use. A stuck `_pub` fails production and fails the probe.
- Subscribe path: uses `_pubsub` via the reader loop's
  `_dispatch` — the same code path that delivers production
  translations. A reader loop that never got scheduled (the §2
  bug shape) fails the probe.
- The probe does not open a fresh Redis client for either half;
  a probe with its own client would pass even when the adapter
  is stuck, defeating the purpose.

Cross-instance fanout (marker published by A, delivered by B's
reader loop) is verified during the enablement window by the
F-27-style handover check the operator runs
(`gate2_handover.sh` in the v6 helpers). The in-process probe
covers the per-instance half; the handover check covers the
cross-instance half.

**Missing-probe detection.**

- `redis_pubsub_probe_missing` counter — a metric absence alarm
  (Cloud Monitoring "absence" condition) fires when no
  `redis_probe_ok` for a given `(revision, instance_id)` pair
  has arrived in the last 2 min while that instance is still on
  the roster (§4a-1). Absence pages via `A8`.
- The absence check needs the roster from §4a-1 to know which
  instances SHOULD be reporting. In steady state the roster is
  just "every live instance"; during a window it's the frozen
  roster the operator entered on.

**INFO-log deliverability caveat (learned from PR #32).**

Cloud Run captures stdout and stderr to Cloud Logging, but
module-level `logging.info(...)` calls only surface when the
process's root logger is at INFO. PR #32's F-27 initially
timed out because the harness backend ran with root at
WARNING; the fix was to explicitly set the `redis_pubsub`
logger to INFO in the harness bootstrap.

Production applies the same discipline:

- The backend's process entry point MUST set the root logger
  (or at minimum `redis_pubsub` + `app.services.*`) to INFO
  before `uvicorn.run(...)` so `redis pubsub started …`,
  `redis pubsub reconnected …`, and `redis_probe_ok …` INFO
  lines reach Cloud Logging.
- Pre-enablement observability check §3 includes a
  "smoke-verify INFO reaches Cloud Logging" step that flips
  `REDIS_ENABLED=1` on a canary revision at 0 % traffic and
  confirms the `redis pubsub started …` INFO line surfaces in
  Cloud Logging within 60 s.

### Pre-enablement observability check

Before flipping `REDIS_ENABLED=1`:

1. Deploy the seven log-based metrics + the alert policies (A5–
   A8 + reader errors) via the same pattern used in
   `ops/monitoring/reconciler/` (adapter yaml + `apply.sh`;
   Google Cloud resources only, no Cloud Run change).
2. Verify each metric name is present in Cloud Logging and
   returns 0 samples (the adapter emits nothing while
   `REDIS_ENABLED=0`; the in-process probe task is inert while
   disabled).
3. **INFO-log deliverability smoke test.** On a canary revision
   at 0 % traffic, flip `REDIS_ENABLED=1` briefly and confirm
   the `redis pubsub started …` INFO line reaches Cloud
   Logging within 60 s. If it does not, the production entry
   point is not configuring the root/`redis_pubsub` logger at
   INFO — fix that BEFORE any subsequent §4d window (this is
   the exact defect PR #32 hit in its harness). Flip back to
   `=0` and record the smoke-test outcome in the audit trail.
4. On the same canary at 0 % traffic (re-flipped to
   `REDIS_ENABLED=1`), verify:
   - `redis_pubsub_startup_failed` for this instance stays 0
     OR is followed by `redis_pubsub_reconnect_successes` on
     the same instance within 5 min (A5 paired condition).
   - The in-process probe emits `redis_probe_ok instance=<id>`
     at least twice within 2 min.
   - `redis_pubsub_probe_missing` for this instance stays 0.

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
disabled) and into preflight (where Redis is still disabled and
the probe endpoint returns 503).

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

At window entry, enumerate the **instance roster**:

- Every distinct `(jsonPayload.instance_id, revision_name)` pair
  that emitted a `reconciler_tick` in the last **10 minutes**.
  Ten minutes covers a full sweeper cycle plus one deploy cycle;
  90 s (the earlier draft) drops instances that go silent while
  stuck.
- Persist the roster to the audit trail. New pairs that appear
  during the window are ADDED. Existing pairs stay on the roster
  until retirement is proven (see below); disappearance is
  treated as **unresolved**, not implicit pass.

The window can proceed only when BOTH hold at the same moment:

1. **Firestore** — no `organizations/*/rooms/*` document has
   `status="live"`. One pass via the admin store the reconciler
   uses.
2. **Instance roster is clean** — every roster member has either:
   - emitted two consecutive `reconciler_tick` events with
     `owned_rooms=0` in the last 60 s AND its youngest tick is
     within 60 s (silence during the window disqualifies), OR
   - been proven RETIRED via a Cloud Run revision status
     showing the instance's `revision_name` has zero live
     instances (checked with `gcloud run revisions describe`
     against the revision's `status.conditions[Ready]` and the
     revision-level active-instance count published in
     Monitoring). Retirement removes the instance from the
     roster for the rest of the window.

A helper script (successor to `~/.gate2-helpers-v5/gate2_preflight.sh`)
formalises this and emits, for the audit trail:

- `no_live_rooms_verified_at=<UTC>`
- `roster=[(revision, instance_id, last_tick_at, last_owned_rooms), …]`
- `firestore_live_room_count=0`
- `retired_this_window=[(revision, instance_id, retired_at), …]`

#### 4a-2. Redis-enabled acceptance — post-`REDIS_ENABLED=1` deploy

Runs at §4d step 5 ONLY after a flip to `REDIS_ENABLED=1`. The
roster used here is the post-deploy roster (new revision's new
instances). For every roster member:

- `redis pubsub started …` INFO line present within 60 s of the
  instance's first `reconciler_tick`.
- `redis_pubsub_startup_failed` counter for this instance is
  either 0 for the last 5 min, OR followed by a corresponding
  `redis_pubsub_reconnect_successes` event on the same instance
  within 5 min. The pair is the true page signal (matches A5's
  definition in §3); a lone startup_failed is expected during
  transient Memorystore blips.
- `redis_probe_ok` fired at least twice within the last 3 min
  for this instance via the coverage mechanism in §3's active
  probe.

Absence of the probe line is FAILURE, not silent pass; §3
specifies the `redis_pubsub_probe_missing` counter that pages
when the probe has not landed for an instance within its
expected polling window.

#### 4a-3. Redis-disabled acceptance — post-`REDIS_ENABLED=0` deploy (rollback)

Runs at §4d step 5 when the flip direction is toward disabled.
The adapter emits nothing while disabled; verifying "off" is a
matter of proving the FROM state left cleanly:

- `redis pubsub started …` did NOT fire on any post-deploy
  instance in the last 5 min (the adapter's `start()` early-outs
  when `_enabled=False`).
- No `redis_pubsub_*` metric increments on any post-deploy
  instance in the last 5 min.
- The `4a-1` room-drain checks continue to pass (unchanged by
  the rollback).

Do NOT run any Redis-health probe during `4a-3`; the endpoint
returns 503 in this state by design and would fail every check.

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
it ships. That first deploy is itself a Cloud Run revision and
therefore must be a §4d-style deploy. Bootstrap sequence:

1. **BOOT-1** — Land the gate-reading code in a separate,
   earlier PR. It reads `system/deploy_gate`; when the
   document is absent (default state) it treats gate as
   unblocked and creates rooms normally. This is a no-op
   change on the running service until the operator writes the
   document, so it is a §4d-style deploy on its own — but
   because the gate-aware code doesn't itself need the gate
   yet, the FIRST §4d deploy can (and must) use the older
   §4a-1 checks alone (no gate to consult). Merge and deploy
   BOOT-1 during a routine no-live-room window BEFORE the
   Redis rollout begins.
2. **BOOT-2** — Verify BOOT-1 is on 100 % traffic and behaves
   as no-op (`system/deploy_gate` still absent; both start
   endpoints succeed). Now the gate mechanism exists.
3. From this point on, every §4d deploy sets `blocked=True`
   in step 1 and clears it in step 6.

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
5. Provision the log-based metrics + Cloud Monitoring alerts +
   the Cloud Scheduler probe job's Google Cloud resources
   (Scheduler job + Cloud Monitoring policies) — these do NOT
   change Cloud Run and are safe as pre-work.

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
   - Enable direction → run §4a-2 in full. A lone per-instance
     `startup_failed` is expected under Memorystore Standard
     ~15 s failover and is NOT a stop on its own; the paired
     condition (§3 A5) — `startup_failed` with no follow-up
     `reconnect_success` on the same instance within 5 min — IS
     a stop and rolls back to step 1 with the opposite
     direction.
   - Disable direction → run §4a-3 in full. Any post-deploy
     instance emitting `redis pubsub started …` after the flip
     is a stop and rolls back.
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
The adapter's probe endpoint returns nothing while disabled and
its metrics stop incrementing; §4a-3 checks the ABSENCE of
Redis activity, which is the correct signal in this direction.

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
  - A per-revision preflight log query shows a
    `redis pubsub started` line for the serving revision (the
    fix in §2 makes this a reliable signal; the INFO-log
    deliverability caveat in §3 must be satisfied by the
    production process entry point).
  - `redis_pubsub_startup_failed` counted paired with
    `redis_pubsub_reconnect_successes` across the deploy window
    per A5's paired-condition definition — a lone
    `startup_failed` is expected under Standard-tier ~15 s
    failover and does NOT fail the window, but a
    `startup_failed` without a follow-up reconnect success
    within 5 min DOES.
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
2. Roster retirement RTT — §4a-1 proves retirement via Cloud Run
   revision status. What's the longest acceptable delay between
   "last tick observed" and "retirement proven" before the
   operator manually intervenes? First-cut: 5 min. Confirmable
   only under real Cloud Run drain behaviour, so leave as first-
   cut for the initial window and retune.

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
