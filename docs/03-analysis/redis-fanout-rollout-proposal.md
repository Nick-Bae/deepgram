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

**Fix — landed in PR #32 (`fix/redis-pubsub-startup-recovery`),
stacked on PR #30:**

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
F-25, F-26, **F-27**) all green.

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
| `redis_pubsub_active_probe_failure` | `textPayload:"redis_probe_failed"` | Emitted by the active probe endpoint (see below). Per-instance failure signal that does NOT depend on adapter internals. |

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
  Defined below.
- The existing A1 reconciler-freshness alert continues to page on
  its own — a Redis outage does not affect the reconciler.

### Active pub/sub probe (`A7` source signal)

Log-based counters only tell us what the adapter itself emitted.
They cannot tell us that a message we publish is actually received
by another instance's subscriber. For that we need an active probe
that publishes AND subscribes from inside the Cloud Run network.

Design:

- A lightweight endpoint `/internal/redis_probe` (auth via
  workload identity, private-network-only) that, when called,
  publishes a randomly-marked heartbeat message on a dedicated
  probe channel and asserts that its OWN subscriber receives it
  within a small deadline.
- A Cloud Scheduler job (interval: 60 s) invokes this endpoint on
  each serving revision via the internal service URL, iterating
  through revisions (public traffic and 0%-traffic drained
  revisions both).
- The endpoint logs `redis_probe_ok instance=<id>` on success and
  `redis_probe_failed instance=<id> reason=<...>` on failure. A6
  metric group adds a `redis_pubsub_active_probe_failure` counter
  filtering on `textPayload:"redis_probe_failed"`.
- The probe does NOT test cross-instance fanout on its own (that
  requires two instances). It tests that this instance's publish
  path AND subscribe path are healthy — the two loop halves the
  startup-recovery fix depends on.
- Cross-instance fanout is verified during the enablement window
  by the F-27-style handover check the operator runs
  (`gate2_handover.sh` in the v6 helpers).

### Pre-enablement observability check

Before flipping `REDIS_ENABLED=1`:

1. Deploy the five log-based metrics + the probe endpoint + the
   Cloud Scheduler probe via the same pattern used in
   `ops/monitoring/reconciler/` (adapter yaml + `apply.sh`;
   separate PR from the code fix).
2. Verify each metric name is present in Cloud Logging and returns
   0 samples (the adapter emits nothing while `REDIS_ENABLED=0`;
   the probe endpoint returns 503 while disabled).
3. On a canary revision (traffic 0%), flip `REDIS_ENABLED=1`
   temporarily; verify the `redis pubsub started …` line appears
   in Cloud Logging, `redis_pubsub_startup_failed` stays at 0,
   and the probe emits `redis_probe_ok` for that instance within
   two consecutive scheduler ticks.

## 4. Enablement gates and rollback (no-live-room-required)

`--max-instances=1` alone is not a sufficient guarantee (Track 1
Gate 2 established that Cloud Run keeps drained revisions alive on
active WebSockets). The enablement and rollback windows must be
strictly no-live-room, AND every deploy in the window must follow
the ordered sequence in §4d.

### 4a. No-live-room detection (authoritative)

A no-live-room window requires ALL of:

- **Firestore** — no room with `status="live"` across every
  `organizations/*/rooms/*` document. Query executed via the same
  admin store the reconciler uses; results counted in one pass.
- **Per-instance reconciler ticks** — for EVERY Cloud Run instance
  that emitted a `reconciler_tick` in the last 90 s (whether the
  instance's revision is at 100 % traffic OR 0 % traffic while
  draining), the last two consecutive ticks show
  `owned_rooms=0`. Enumerate instances by pulling the last 90 s of
  `reconciler_tick` logs, grouping by
  `resource.labels.revision_name` × `resource.labels.instance_id`;
  every distinct pair must have two zero-tick observations, and
  the youngest pair must have ticked within the last 60 s (silence
  is not an OK signal — see §4a rationale below).

  Rationale: this catches the case where a revision at 0 % public
  traffic still holds a Cloud Run instance with an open WebSocket
  for a room that Firestore has already marked ended. Aggregating
  ticks by revision only would miss the older revision's instances
  entirely if the newer revision has more ticks — that is exactly
  the isolation shape Gate 2 observed.

- **Cross-instance active probe** — the `redis_probe_ok` line
  fired on every enumerated (revision, instance) pair inside the
  same 90 s window. Absence indicates the probe endpoint has not
  been polled for that instance; a `redis_probe_failed` line on
  ANY enumerated instance disqualifies the window.

All three checks must hold at the same tick moment. A helper
script similar to `~/.gate2-helpers-v5/gate2_preflight.sh` will
formalise this check and emit
`no_live_rooms_verified_at=<UTC>` (with the enumerated instance
list and the tick / probe timestamps) for the operator.

### 4b. Prevent new sessions during the window

Both service-start endpoints reject NEW sessions during the
window. Missing either one lets a new host attach and create a
live room between the no-live-room verification and the deploy.

- `POST /api/org/{orgId}/service/{serviceKey}/start` — the
  authenticated org-scoped path (routes/multichurch.py:295).
- `POST /api/c/{slug}/service/{service_key}/start` — the
  public church-slug path (routes/multichurch.py:311).

Mechanism: a session-blocking flag in `organizations/{orgId}`
(or a service-level flag when only one service is affected) that
both endpoints check before creating a room; on hit, return 503
with `Retry-After: 60`. Setting the flag is a single Firestore
write; the response short-circuits before any Redis or Deepgram
call. The flag is cleared at §4d step 6 (reopen).

Do not touch existing WebSocket connections — they are already
drained by the no-live-room check in §4a. The block covers new
starts only.

### 4c. Enablement — one-time infrastructure preparation

Steps 1–8 happen BEFORE the deploy window opens and are safe to
run while rooms are live. They do not change adapter behaviour
(`REDIS_ENABLED=0` remains pinned throughout).

1. Enable `vpcaccess.googleapis.com`.
2. **Prefer Direct VPC egress** over a Serverless VPC Access
   Connector. Direct VPC egress skips the connector VM,
   eliminates its cold-start cost and per-hour spend, and works
   with Cloud Run gen2. Only fall back to a connector if Direct
   VPC egress is unavailable in `us-central1` for the account
   (verify with `gcloud beta run services describe`).
   - Direct VPC path: attach an egress subnet with private
     Google access; no separate connector to provision.
   - Connector fallback: create a Serverless VPC Access
     Connector in `us-central1` attached to the default network;
     verify state=READY.
3. Provision a new Cloud Memorystore Redis instance. **Use
   Standard tier**, not Basic — Standard provides a replicated
   HA setup and a documented maintenance-window guarantee.
   Basic is cheaper but has no HA and Basic maintenance can
   drop the instance for the duration of the patch. The
   startup-recovery fix in §2 makes a dropped-then-restored
   Redis survivable, but only Standard prevents the extended
   local-only-broadcast window entirely. Note the internal IP.
4. Update the `redis-password` secret in Secret Manager to match
   the new instance's password (if the new instance requires auth).
5. Update Cloud Run's `REDIS_HOST` env to the new IP; leave the
   secret ref for `REDIS_PASSWORD` in place.
6. Attach the VPC egress path to Cloud Run (Direct VPC egress
   subnet, OR `--vpc-connector=<name>` for the fallback); confirm
   `vpc-access-egress` is `private-ranges-only`.
7. Deploy the startup-recovery fix + F-27 (PR #32).
8. Deploy the log-based metrics + alerts + active probe endpoint
   + Cloud Scheduler probe job (see §3).

Keep unchanged throughout §4c: `REDIS_ENABLED=0`,
`ROOM_RECONCILER_ENABLED=1`, `--max-instances=1`.

### 4d. Deploy window — the ordered sequence

Every deploy that flips `REDIS_ENABLED` (in either direction)
follows this sequence exactly. Skipping or reordering any step
re-creates the Gate 2 failure shape.

1. **Block new starts** — write the session-blocking flag from
   §4b. Both endpoints must now return 503.
2. **Verify the block is in effect** — issue one request to each
   of the two start endpoints from outside Cloud Run and confirm
   both return 503. A stale routing layer or forgotten preview
   URL that still accepts starts is a stop.
3. **Confirm no live rooms** — run the §4a check to completion:
   Firestore query + per-instance reconciler ticks + active
   probe on every enumerated instance. Persist
   `no_live_rooms_verified_at=<UTC>` for the audit trail.
4. **Deploy the env flip** — flip `REDIS_ENABLED=1` (or `=0` on
   rollback) via the deploy workflow. Wait for the new revision
   to reach 100 % traffic.
5. **Verify post-deploy** — for each new instance:
   `redis pubsub started …` log line present within 60 s,
   `redis_pubsub_startup_failed` at 0, `redis_probe_ok` fired at
   least twice within the last 3 min. Any per-instance
   startup_failed occurrence rolls back to step 1 with the
   opposite direction.
6. **Reopen new sessions** — clear the session-blocking flag
   from §4b. Both endpoints resume accepting starts.
7. **Track 1 Gate 2 rerun** (enablement direction only) with the
   v6 helpers.

### 4e. Rollback (no-live-room required)

Rolling back `REDIS_ENABLED=1 → 0` recreates the exact isolation
Gate 2 failed on. A live-session rollback is unsafe and must not
be attempted. The rollback procedure follows the §4d sequence
in reverse direction: block starts → verify block → confirm no
live rooms → deploy `REDIS_ENABLED=0` → verify post-deploy →
reopen. If no window is available for a critical rollback,
terminate live rooms via the admin End Service path first, then
run §4d.

If the reason for rollback is Memorystore itself is unhealthy,
the reconciler continues to work independently (PR-T1-C is
Redis-independent by design) — but the cross-instance fanout
scenario the Gate 2 rerun tests is exactly what fails. Roll back
during no-live-room windows only.

## 5. v6 Gate 2 helper acceptance criteria (for a later PR)

v5 hardcodes `analyze_cloud_run`'s guard as `redis_enabled == "0"`.
v6 must:

- Accept an expected Redis state (`redis_enabled == "0" | "1"`)
  driven by a helper env `GATE2_EXPECTED_REDIS`.
- When `GATE2_EXPECTED_REDIS=1`, additionally verify at run time:
  - Cloud Run has a `vpc-access-connector` attached.
  - A per-revision preflight log query shows a `redis pubsub started`
    line for the serving revision (the fix in §2 makes this a
    reliable signal).
  - `redis_pubsub_startup_failed` is 0 across the deploy window.
- Retain every existing v5 check that would stop the run when
  evidence is missing.
- Ship with fixture-driven tests for both the Redis-disabled
  (backward-compatible) and Redis-enabled paths.

v6 lands as a review-only branch after this rollout proposal is
approved and after the startup-recovery fix + F-27 pass in CI.

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
- F-27 Toxiproxy timing — **event-driven** (F-27 waits on log
  strings from both backends before restoring the proxy, so
  timing is not a fixed number). Documented in the F-27 module
  docstring.

Still open:

1. Alert threshold on `redis_pubsub_reconnect_attempts` — 3/5 min
   per instance is a first-cut number. Tune after two weeks of
   Redis-enabled traffic; leave at 3/5 min for the enablement
   window.

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
