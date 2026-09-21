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

**Fix plan (separate PR, not #30):**

- Do not set `_started = True` until the reader task has actually
  been scheduled.
- On ping failure, still start the reader task with `_connected =
  False`; the reader loop's existing `_reconnect` path will bring
  the subscriber up when Redis becomes reachable.
- Preserve current stop() semantics: stop() runs only when the reader
  task exists.

**Test plan for the fix (`F-27`):**

New integration scenario in
`backend/tests/integration/resource_cleanup/`:

1. Configure both backends with `REDIS_ENABLED=1` but block Redis
   at startup via Toxiproxy (proxy DOWN).
2. Start backend A and backend B. Ping fails; the fixed code path
   schedules the reader loop anyway.
3. Restore Redis (proxy UP).
4. Attach host on A, listener on B, both with the harness's
   handover shape (as in F-25/F-26).
5. Send a marker through A.
6. Assert the listener on B receives the marker WITHOUT restarting
   either backend.
7. Failure signature under the current (buggy) code: F-27 hangs
   because the reader loop never runs on either instance. Pin the
   timeout small (~15 s) so a broken fix is loud.

F-27 ships in the SAME PR as the `_started` fix, per the reviewer's
direction. PR #30 remains a tests-only PR and its approval stands.

## 3. Monitoring — from actual emitted signals

The rollout outline previously named `redis_pubsub_reconnects` as if
that metric already existed. It does not — the adapter emits **log
lines**, not a named metric. This section pins the metric definitions
to the exact log strings.

### Adapter log strings (verbatim, from `redis_pubsub.py`)

| Line | Log string | Severity | Meaning |
|------|------------|----------|---------|
| 116 | `redis pubsub started host=%s:%s prefix=%s instance=%s` | INFO | One-time success at process start |
| 121 | `redis connect failed; falling back to local-only broadcast: %s` | ERROR | **Startup-recovery bug tell** — this line indicates the buggy path fired; subsequent reconnects are impossible until the fix in §2 lands |
| 359 | `redis pubsub reconnecting in %.1fs (attempt %d)` | INFO | Reader loop is attempting to reconnect |
| 389 or 420 | `redis pubsub reconnected; %d rooms resubscribed …` | INFO | Reconnect succeeded (possibly with 0 rooms) |
| 424 | `redis reconnect failed: %s` | WARNING | Reader-loop reconnect attempt failed |
| 346 | `pubsub reader error: %s` | WARNING | Reader-loop caught an exception; will re-enter reconnect on next iteration |

### Proposed log-based metrics (all counters, no gauges)

Metric names match the `ops/monitoring/reconciler/` convention
(underscored, service-scoped filter):

| Metric | Filter (jsonPayload-agnostic textPayload contains-match) | Purpose |
|--------|----------------------------------------------------------|---------|
| `redis_pubsub_startup_failed` | `textPayload:"redis connect failed"` | **Should always be 0.** A rise indicates the startup-recovery bug fired on some instance. |
| `redis_pubsub_reconnect_attempts` | `textPayload:"redis pubsub reconnecting"` | Baseline for reconnect noise. Occasional attempts are normal during Memorystore maintenance. |
| `redis_pubsub_reconnect_successes` | `textPayload:"redis pubsub reconnected"` | The recovery signal. If attempts rise but successes don't, the connection is broken. |
| `redis_pubsub_reconnect_failures` | `textPayload:"redis reconnect failed"` | Direct failure counter. |
| `redis_pubsub_reader_errors` | `textPayload:"pubsub reader error"` | Reader-loop crashes (transient socket drops). Occasional is fine; sustained is not. |

All five metrics filter on
`resource.type="cloud_run_revision" AND resource.labels.service_name="worshiptranslate-backend"`.

### Alerts (proposed)

- `A5 startup_failed > 0 over 5 min` — pages. Zero-tolerance for the
  §2 bug tell in production.
- `A6 reconnect_attempts > 3 in 5 min without corresponding successes`
  — pages. Indicates Memorystore or VPC path is unhealthy for this
  instance.
- The existing A1 reconciler-freshness alert continues to page on
  its own — a Redis outage does not affect the reconciler.

### Pre-enablement observability check

Before flipping `REDIS_ENABLED=1`:

1. Deploy the five log-based metrics via the same pattern used in
   `ops/monitoring/reconciler/` (adapter yaml + `apply.sh`; separate
   PR from the code fix).
2. Verify each metric name is present in Cloud Logging and returns
   0 samples (the adapter emits nothing while `REDIS_ENABLED=0`).
3. On a canary revision (traffic 0%), flip `REDIS_ENABLED=1`
   temporarily; verify the `redis pubsub started …` line appears
   in Cloud Logging and `redis_pubsub_startup_failed` stays at 0.

## 4. Enablement gates and rollback (no-live-room-required)

`--max-instances=1` alone is not a sufficient guarantee (Track 1
Gate 2 established that Cloud Run keeps drained revisions alive on
active WebSockets). The enablement and rollback windows must be
strictly no-live-room.

### 4a. No-live-room detection (authoritative)

A no-live-room window requires BOTH:

- **Firestore** — no room with `status="live"` across every
  `organizations/*/rooms/*` document. Query executed via the same
  admin store the reconciler uses; results counted in one pass.
- **Reconciler ticks** — every serving revision has emitted at least
  two consecutive `reconciler_tick` events with `owned_rooms=0` in
  the last minute. This catches the case where a revision that
  Cloud Run is draining still holds a local socket for a room that
  Firestore has already marked ended.

Both checks must hold at the same tick moment. A helper script
similar to `~/.gate2-helpers-v5/gate2_preflight.sh` will formalise
this check and emit `no_live_rooms_verified_at=<UTC>` for the
operator.

### 4b. Prevent new sessions during the window

- Set a temporary Cloud Run request filter (or short-lived nginx
  layer / feature flag) that rejects new `/api/org/*/service/*/start`
  requests with a 503 during the enablement window.
- Do not touch existing WebSocket connections — they are already
  drained by the no-live-room check.

### 4c. Enablement steps

1. Enable `vpcaccess.googleapis.com`.
2. Create a Serverless VPC Access Connector in `us-central1`
   attached to the default network. Verify state=READY.
3. Provision a new Cloud Memorystore Redis instance (Basic tier is
   sufficient for Track 1's single-instance-then-multi rollout;
   Standard tier if a maintenance-window guarantee is required).
   Note the internal IP.
4. Update the `redis-password` secret in Secret Manager to match
   the new instance's password (if the new instance requires auth).
5. Update Cloud Run's `REDIS_HOST` env to the new IP; leave the
   secret ref for `REDIS_PASSWORD` in place.
6. Attach the VPC connector to Cloud Run
   (`--vpc-connector=<name>`); confirm `vpc-access-egress` is
   `private-ranges-only`.
7. Deploy the startup-recovery fix + F-27 (once its PR is merged).
8. Deploy the log-based metrics + alerts.
9. In a no-live-room window, flip `REDIS_ENABLED=1` via the deploy
   workflow.
10. Wait 10 minutes; confirm zero `redis_pubsub_startup_failed`,
    zero `redis_pubsub_reconnect_failures`, and at least one
    `redis pubsub started` log line per instance.
11. Track 1 Gate 2 rerun with the v6 helpers.

Keep unchanged throughout: `ROOM_RECONCILER_ENABLED=1`,
`--max-instances=1`. These stay pinned until Track 2.

### 4d. Rollback (no-live-room required)

Rolling back `REDIS_ENABLED=1 → 0` recreates the exact isolation
Gate 2 failed on. A live-session rollback is unsafe and must not be
attempted. The rollback procedure:

1. Detect a no-live-room window as in §4a. If no window is
   available, terminate live rooms via the admin End Service path
   first.
2. Reject new sessions as in §4b.
3. Flip `REDIS_ENABLED=0` via the deploy workflow.
4. Wait for the reconciler ticks to confirm `owned_rooms=0` on the
   new revision.
5. Reopen new sessions.

If the reason for rollback is Memorystore itself is unhealthy, the
reconciler continues to work independently (PR-T1-C is
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

1. Memorystore tier: Basic vs Standard for Track 1? Standard buys a
   maintenance-window guarantee at ~2× cost.
2. Alert threshold on `redis_pubsub_reconnect_attempts` — 3/5 min
   is a first-cut number. A tighter or looser threshold?
3. Should the "reject new sessions during enablement" mechanism be
   a Cloud Run traffic tag (route new traffic to a returning-503
   revision) or an in-app feature flag? Both work.
4. F-27's exact Toxiproxy timing — is it acceptable for the test
   to leave Redis DOWN for 5 seconds after backend startup and
   then restore, or does the reviewer want a longer window?
