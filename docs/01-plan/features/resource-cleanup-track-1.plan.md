# Plan: Resource Cleanup — Track 1

## Executive Summary

| Perspective | Description |
|---|---|
| **Problem** | Production has three known resource-cleanup gaps that do not depend on lease-based abandonment: (a) `_cleanup_live_rooms_on_startup` would end other instances' live rooms when multi-instance rollout happens, (b) a missed Redis terminal broadcast leaves listener sockets, provider tasks, and subscription refcounts held indefinitely on the affected instance, (c) SIGTERM drops client sockets with code 1006 rather than a reconnect-hint close, and (d) there is no metric that flags stuck cleanup for a Firestore-terminal room. All are addressable without introducing host-lease fields. |
| **Solution** | Land four surgical backend changes behind config flags, with observability, integration tests including a real-Redis outage/recovery scenario, and a documented staged validation before production enablement. Explicitly out of scope: the host-lease design (Track 2), any change to `STT_NO_SPEECH_TIMEOUT_SEC`, provisioning Redis in production, and raising `--max-instances`. |
| **Function / UX Effect** | No user-visible UX change during normal operation. Under SIGTERM, viewers reconnect against `/resolve` instead of tombstoning the room. Under a stuck-cleanup fault, operators receive an alert; today they receive nothing. |
| **Core Value** | Ships the Redis-cleanup guarantee the resource-cleanup audit exists to provide — without waiting on the still-open host-lease questions. Redis leaks and cross-instance recovery of missed terminal broadcasts are closed; production remains single-instance and lease-free. |

**Source commit at planning time:** `e63c5d4b` (main, "fix(viewer): resume listener when a new room starts (#18)"). Line-number citations must be re-verified against HEAD before each PR.

**Implementation status (2026-09-17):** PR-T1-A through PR-T1-E are merged. The
production revision containing PR-T1-D is deployed with
`ROOM_RECONCILER_ENABLED=0`, `REDIS_ENABLED=0`, and `--max-instances=1`.
The PR-T1-E monitoring stack was applied on 2026-09-17 with notifications
routed to a disabled rollout-observation channel. The staged validation in
§4 remains in progress; Track 1 is not yet complete.

**Companion audit:** [`docs/03-analysis/resource-cleanup-audit.md`](../../03-analysis/resource-cleanup-audit.md) v5. This plan is scoped to the audit's Track 1 (§9a). Track 2 items — host lease, abandonment detector, watchdog behavior changes, real-Redis provisioning, multi-instance scaling — are deliberately outside this plan and remain in the audit's §9a Track 2 list.

---

## Corrections incorporated from the plan-review pass

Four corrections from the review of the initial Track 1 scoping:

1. **Lease-specific F-17 stays in Track 2.** Track 1's atomic-conditional-transition tests use `lastAudioAt` (the existing field) — **F-23a** (emulator, activity-committed-before-transaction), **F-23b** (controlled retry via mock), and **F-23c** (AST). F-17 (lease variant) remains gated behind Track 2. Emulator validation itself is pending until CI's `firestore-emulator-tests` job actually runs successfully with no unexpected skips.
2. **Redis reconnection is an explicit acceptance test.** **F-24** in the audit exercises the outage/recovery path end-to-end: end a room while Redis is unavailable, verify local cleanup completes, restore Redis, verify the ended room's channel is not resubscribed while a still-live room's channel does recover. Zeroing local owner maps is not sufficient on its own.
3. **Transaction-retry assertion tightened.** The test asserts the callback executes **at least twice** (proving a retry actually happened), and that within the single controlled successful execution the external cleanup effects run **once for the committing branch**. This is not an exactly-once-across-crashes promise; cleanup remains idempotent so a mid-cleanup process death is safely recovered by the reconciler on any instance.
4. **§9 rollout consistency.** No watchdog change proposed in Track 1 or Track 2 by this plan; `STT_NO_SPEECH_TIMEOUT_SEC` remains 120s.

---

## 1. Scope

### In scope

| Change | Reference | Acceptance tests |
|---|---|---|
| Startup safety: remove unconditional startup-termination | Audit §4a.1 | **F-15** |
| Atomic conditional transition for the sweeper's idle-timeout path (`lastAudioAt` re-verified inside the Firestore transaction; external effects fire only after commit) | Audit §4a.2 | **F-23**, transaction-retry test |
| Ended-room reconciler, Redis-independent | Audit §5.1a | **F-8, F-14, F-16, F-18, F-21** |
| SIGTERM sends a reconnect-hint close code (not `room_ended`); client-side handling distinguishes the code from terminal | Audit §4.6 | **F-9** |
| Overdue monitoring: `terminal_rooms_with_resources`, `oldest_overdue_cleanup_seconds`, `cleanup_inflight`, `last_successful_reconciliation_at` | Audit §8 | **F-18** alert fires; **F-21** healthy long room does not |
| Real-Redis outage/recovery integration test | Audit F-24 | **F-24** |

### Explicitly out of scope

- Host lease design or implementation (Audit §5.1b — Track 2).
- Abandonment detector (Track 2).
- Any change to `STT_NO_SPEECH_TIMEOUT_SEC` or the STT idle watchdog.
- Provisioning real Redis in production (`REDIS_ENABLED` remains 0).
- Raising `--max-instances` (remains 1).
- Frontend changes beyond the SIGTERM close-code handling in `useSubtitleSocket`.

---

## 2. Change breakdown (small reviewable PRs)

Each PR has a single purpose, its own acceptance tests, and can be merged and deployed independently. Config flags default off; each PR ships dark and is enabled at the staged-validation gate in §4.

### PR-T1-A — Startup safety fix

**What changes**

- Remove the unconditional `_cleanup_live_rooms_on_startup` call from `main.py:1275`.
- The function stays defined (used by tests) but is no longer registered on `@app.on_event("startup")`.
- Add a startup-time log line stating "startup-cleanup path removed; abandonment detector or sweeper will handle stale rooms on their own schedules."

**Why this shape**

- The current call ends every live Firestore room at boot (`stale_live_rooms(idle_seconds=0, max_duration_seconds=0)` returns all live rooms). Benign at max-instances=1 in isolation; guaranteed to break at any greater instance count.
- No replacement mechanism is introduced by this PR. The existing sweeper's 15-minute idle path continues to handle rooms whose host actually left; Track 2's abandonment detector will provide the shorter deadline later.

**Acceptance test**

- **F-15** (audit Group B): start a second uvicorn process while the first is broadcasting a live room. First room's `status=live` unchanged; first's listeners see no terminal event; first's host connection undisturbed; second process has zero references to the first's room. This test is a merge-blocker for this PR.

**Risk**

- Very low. The removed path was correctly identified as "no observed justification"; the fallback is the existing 15-min sweeper which already runs.

---

### PR-T1-B — Atomic conditional transition for the sweeper's idle path

**What changes**

- Introduce a Firestore transaction wrapper in `multichurch_store.py` used **only from the sweeper's idle-timeout callsite**. Inside the callback: re-read the room doc; stage the End Service write only if `status == "live"` AND `lastAudioAt` age still exceeds `ROOM_IDLE_TIMEOUT_SEC` at write time.
- **NOT re-gated by this wrapper** — explicit End Service (`POST /api/org/{orgId}/rooms/{roomId}/end`), `ROOM_MAX_DURATION_SEC` termination, and cap enforcement (`enforce_live_usage_caps`). Each of those has an authoritative signal that must not be second-guessed by a `lastAudioAt` recheck: the host clicked End, the room reached its hard duration cap, or Firestore/Stripe flipped the org into a cap state. Adding an idle-based recheck to these paths would silently drop legitimate terminations.
- Callback body is pure Firestore. External effects (`close_room_listeners`, `close_room_hosts`, `forget_room`, `_cleanup_room_local_state`, `broadcast_room(STATUS ended)`) fire only after the transaction returns success.
- All external effects remain idempotent (they already are in current code — this PR asserts that property with tests, doesn't add new invariants).

**Why this shape**

- Fixes the stale-read termination race called out in audit §4a.2 for the existing sweeper's idle-based cleanup — a race the current idempotent `end_room` alone does not prevent.
- Establishes the transaction-callback purity pattern that Track 2 will extend to the lease.
- Scopes the recheck to the ONE termination reason where the recheck is meaningful. Other reasons keep their existing (already-atomic-enough) codepaths untouched.

**Acceptance tests**

- **F-23a (emulator)** — narrowly scoped: activity committed to Firestore BEFORE the termination transaction opens is respected. `touch_audio` writes the fresh `lastAudioAt`; end_room's transaction reads the fresh value and returns `skipped=no_longer_idle`; room stays `status=live`. **This does NOT prove Firestore's production optimistic-concurrency retry** — Google's emulator uses simplified locking and does not reproduce all production concurrency modes. That's what F-23b covers.
- **F-23b (controlled retry, no emulator)** — deterministic proof of callback safety under retry. A patched transactional decorator forces the callback body to run twice with different reads. Assertions: callback invocation count ≥ 2, second invocation's decision reflects the second read (eligibility rechecked), no external side effects observed in either invocation. AST inspection separately verifies the transaction callback body contains no calls to `close_room_*`, `broadcast_room`, `forget_room`, `disconnect`, or `_cleanup_room_local_state`.
- **F-23c (sweeper flow)** — AST inspection of `_room_sweeper_loop`: the `if result.get("skipped"):` branch must `continue` before any external effect fires. Locks in that a skipped termination triggers zero external cleanup effects.
- **Idempotence test** — running local cleanup back-to-back on the same room produces no duplicate log lines, no errors, no double-decrement of Redis refcounts.
- **Skipped-mutates-nothing test** — a skipped `end_room` result leaves every room field byte-identical.
- **Transaction-retry test**: simulate a concurrent write to the room document during the transaction so Firestore reruns the callback. Assert:
  - Callback executes **at least twice** (proves retry happened).
  - External cleanup effects fire **once** for the committing branch during this test run.
- **Cleanup-idempotence test**: run `close_room_listeners` and `_cleanup_room_local_state` twice back-to-back on the same room. Second invocation is a no-op with no errors, no duplicate log lines, and no double-decrement of Redis subscription refcounts.

**Risk**

- Medium. Touching the sweeper's write path is delicate. The transaction wrapper is a well-known Firestore pattern; the test suite is the load-bearing verification.

**What PR-T1-B does NOT close.** The Redis missed-terminal-broadcast recovery (G-5) and the Redis outage/reconnect scenario (F-24) remain upcoming Track 1 work in PR-T1-C. PR-T1-B tightens the sweeper's idle-based termination against stale reads on a single instance; it does not add cross-instance reconciliation.

---

### PR-T1-C — Ended-room reconciler (Redis-independent), with the metrics it needs

**What changes**

- New module `backend/app/services/room_reconciler.py`.
- Background task started from `_on_startup` behind a config flag `ROOM_RECONCILER_ENABLED` (default 0). Interval configurable via `ROOM_RECONCILER_INTERVAL_SEC` (default 30).
- Each tick:
  1. Build ownership inventory by unioning owner-map keys: `connections_by_room`, `host_presence_by_ws` (mapped through), `host_subscription_owned_by_ws`, `listener_subscription_owned_room_by_ws`, rooms with active STT background tasks, and Redis Pub/Sub's desired-room refcount keys. Set union naturally deduplicates. Including Redis's reconnect source of truth lets reconciliation repair a subscription whose websocket ownership was already lost.
  2. Batch-read Firestore docs for that set.
  3. For each returned doc, apply the explicit terminal-state predicate (audit §5.1a): `status == "ended"` → confirmed terminal; `status == "live"` → skip; missing / malformed / unrecognized → skip + log at warn; read failure → skip + increment `reconciler_tick_total{outcome=firestore_error}` and never treat as terminal.
  4. For confirmed-terminal rooms, run the idempotent local cleanup path — same operations PR-T1-B's transaction wrapper triggers on commit, but here fired unconditionally because Firestore already reflects the terminal state.
- Passes do not overlap; if a pass is still running when the next interval fires, the second pass emits `reconciler_tick_total{outcome=skipped_overlap}` and returns.
- No Firestore writes from this module. Reconciler is a follower.
- **Ships with the minimum monitoring signals required to make F-18 / F-21 meaningful** — the reconciler cannot demonstrate correctness in tests or in production without them:
  - `terminal_rooms_with_resources` (gauge, per instance).
  - `oldest_overdue_cleanup_seconds` (gauge, per instance).
  - `cleanup_inflight` (gauge, per instance).
  - `reconciler_tick_total{outcome}` counter (outcome ∈ {ok, firestore_error, cleanup_error, skipped_overlap, loop_error}).
  - `reconciler_actions_total{reason=ended_room_local_cleanup}` counter.
  - `last_successful_reconciliation_at` timestamp.
  - Exposition: structured log lines (JSON) tagged for Cloud Logging (see §7 answer 1); adapter to Cloud Monitoring lives in PR-T1-E.

**Why this shape**

- Closes G-5 (missed Redis terminal broadcast). Uses Firestore as source of truth; operation is independent of Redis reachability by design.
- The union-of-owner-maps inventory means adding a new resource type in future requires adding it to this list — a comment and a test enforce that dependency.

**Acceptance tests**

- **F-8** (Group D, [R] [2P]): missed terminal broadcast during real Redis outage; affected instance's reconciler discovers Firestore=ended within interval + read latency; local cleanup completes; no false termination of unrelated rooms.
- **F-14** (Group D, [E]): Firestore read failure during a tick; reconciler does not treat outage as "room ended"; no false terminations; pass logged as `firestore_error`; next tick retries once Firestore recovers.
- **F-16** (Group D): delayed cleanup for room A must not affect replacement room B under the same service URL.
- **F-18** (Group D): stuck cleanup for a terminal room; `terminal_rooms_with_resources` becomes ≥ 1 and stays past deadline; `oldest_overdue_cleanup_seconds` grows past threshold; alert would fire.
- **F-21** (Group D): healthy long-running room does NOT trigger the overdue alert (locks in that the overdue-cleanup metric is scoped to Firestore-terminal rooms).

**Risk**

- Medium-low. The module is additive and disabled by default; production enable happens only at the staged-validation gate in §4.

---

### PR-T1-D — SIGTERM reconnect-hint close code

**What changes**

- Backend: rely on Uvicorn 0.34's real shutdown ordering. Uvicorn closes active
  WebSockets with code `1012` (Service Restart) before the application shutdown
  hook runs; `_on_shutdown` then cancels background tasks and stops pubsub. Do
  not add a second app-level socket-close loop: it races Uvicorn and cannot
  deterministically replace the already-sent close frame.
- Frontend, listener side: `useSubtitleSocket` onclose handler distinguishes:
  - `event.code === 1000 && event.reason === "room_ended"` → terminal (existing PR #18 behavior; unchanged).
  - `event.code === 4002` OR `event.code === 1012` → infrastructure reconnect; do NOT tombstone the room; **reuse the existing transient-reconnect policy** (jittered backoff, bounded retries), not a separate mechanism.
  - Other abnormal closes (1006, 1011, etc.) → existing reconnect behavior, unchanged.
- Frontend, host side: the corresponding host STT WebSocket hook (host-console component, wherever it lives) MUST also distinguish 4002 / 1012 from terminal. The host currently reconnects on close; verify that neither code is misinterpreted as End Service, and that the host STT reconnect path also reuses the existing transient policy rather than introducing a new one.
- Test coverage in both `useSubtitleSocket` and the host STT hook must include:
  - `1000 room_ended` → terminal (regression guard for PR #18).
  - `4001` → terminal (regression guard for PR #16).
  - `4002` → infrastructure reconnect (new).
  - `1006` → abnormal, existing reconnect.

**Why this shape**

- `close(1000, "room_ended")` on SIGTERM would make PR #18's client tombstone a room that is actually still live on a sibling instance. That would break rolling deploys once multi-instance is on.
- Uvicorn's `1012` is the infrastructure-restart signal observed on the real
  SIGTERM path. Client handling keys it to the existing transient reconnect
  policy. `4002` remains classified as transient for forward compatibility but
  is not emitted by the current backend shutdown path.

**Acceptance test**

- **F-9** (Group F, [2P]): send SIGTERM to instance A while it holds active
  listener and host WSs. Require process exit in under 10 seconds; both clients
  receive `1012` (never `room_ended`); Redis `NUMSUB` drops exactly 2 → 1 while
  B's sentinel stays connected; the provider-client count transitions exactly
  1 → 0 → 1; Firestore remains live; and a fresh listener and host reconnect to
  B and deliver one post-SIGTERM marker. Test the full server shutdown sequence,
  not `_on_shutdown()` in isolation.

**Risk**

- Medium. The frontend change touches the terminal-detection logic PR #18
  introduced and PR #16 depends on. Test coverage in `useSubtitleSocket` and
  the host hook includes both existing terminal cases (`room_ended`, `4001`),
  Uvicorn's `1012`, and the reserved `4002` infrastructure code.

---

### PR-T1-E — Operational monitoring: dashboards, alerts, and completion evidence

**What changes**

- **Cloud Logging → Cloud Monitoring adapter** for the bounded structured
  events emitted by PR-T1-C. Cloud Logging has counters and distributions, not
  true gauges, so the deployed contract is five log-based metrics:
  `reconciler_success_ticks`, `reconciler_tick_outcomes{outcome}`,
  `reconciler_actions{reason}`, `reconciler_overdue_ticks`, and
  `reconciler_oldest_overdue_seconds`.
- **Dashboards.** One-page operator view: successful-tick freshness, tick
  outcomes, overdue observations, overdue-age percentiles, recovery actions,
  and raw cleanup lifecycle logs. An unmatched `cleanup_started` /
  `cleanup_finished` pair is the in-flight-stall signal.
- **Alert policies:**
  - `terminal_rooms_with_resources ≥ 1` sustained past the 60s acceptance target → page.
  - `oldest_overdue_cleanup_seconds` above target → page.
  - `reconciler_success_ticks` absent for 15 minutes → page. This is an
    external Cloud Monitoring absence condition aggregated across revisions;
    the 15-minute threshold accommodates log-based metric ingestion latency.
  - `reconciler_actions_total{reason=ended_room_local_cleanup}` rate > SLO threshold during *normal* operation → page (primary paths silently failing). **NOT a rollback trigger; alert for investigation.**
- **Cleanup deadline** for `oldest_overdue_cleanup_seconds` starts from Firestore `endedAt` (fallback: from the moment a cleanup was requested if that predates the write).

**Why this shape**

- PR-T1-C ships the raw signal emission because F-18 / F-21 in that PR cannot demonstrate correctness without them. PR-T1-E ships the operator-facing consumption: dashboards operators actually watch, alert policies wired to paging, and the completion evidence required for the staged validation gates in §4.
- The external missing-heartbeat alert closes the "wedged process stops logging" failure mode that a purely log-based system otherwise misses.

**Acceptance criteria (not tests — production readiness evidence)**

- All alert policies deployed and routed to a disabled rollout channel for a
  24-hour observation period before any channel is enabled for paging. While
  `ROOM_RECONCILER_ENABLED=0`, no reconciler ticks exist and the absence alert
  cannot start its clock until its first data point; this period validates
  resource configuration and accidental-alert behavior, not runtime liveness.
- Dashboard reviewed by whoever will actually be on-call for Track 1 changes; questions from that review answered before production reconciler enable (§4 step 4).
- F-18 alert fires in staging with the seeded stuck-cleanup fixture; F-21 does not fire under a long healthy broadcast.

**Risk**

- Low. Additive; no runtime effect other than logs / metrics.

**Completion required before** production reconciler activation (§4 step 4).

---

## 3. Test mapping (each change → its acceptance tests)

| PR | Audit change | Acceptance tests | Env |
|---|---|---|---|
| PR-T1-A | §4a.1 startup safety | F-15 | [2P] |
| PR-T1-B | §4a.2 atomic conditional (sweeper idle-timeout variant only) | F-23a (emulator, activity-committed-before-transaction), F-23b (controlled retry via mock, ≥ 2 callback invocations + eligibility recheck), F-23c (AST — sweeper `continue`s on skipped), skipped-mutates-nothing, cleanup-idempotence | [E] for F-23a; doubles for the rest |
| PR-T1-C | §5.1a reconciler + the raw metric signals it needs | F-8, F-14, F-16, F-18, F-21 | mostly doubles; F-8 needs [R] [2P] |
| PR-T1-D | §4.6 SIGTERM close code + host AND listener reconnect handling | F-9 (both host and listener reconnect paths asserted) | [2P] |
| PR-T1-E | §8 dashboards, alert policies, external freshness alert, log→metrics adapter | F-18 alert fires in staging with seeded fixture; F-21 does not fire during a healthy long broadcast; alert policies dry-run for 24h before paging | metric harness + staging |
| **Track 1 completion gate** | integration | **F-24** end-room during Redis outage; verify (a) ended-room subscriptions stay removed after Redis recovery **and** (b) unrelated live-room subscriptions do recover | **[R]** |

**F-17** (lease-renewal race) is deliberately absent from this table — it moved to Track 2 alongside the lease design. F-23 replaces it for Track 1 purposes.

**F-10, F-19, F-20, F-22** are Track 2 (require abandonment detector).

**F-7** (Redis reconnect race) is Track 2 preparation (real Redis + 2-process) but is not strictly required for Track 1 to ship. Recommended to run it opportunistically once the docker-compose environment exists for F-24; failures do not gate Track 1 landing.

---

## 4. Staged validation before production enablement

Reconciler and SIGTERM close-code both ship dark. Order of enable:

1. **Local + CI.** All doubles-based tests (F-15, F-23, F-16, F-18, F-21, transaction-retry, cleanup-idempotence) pass on every PR. CI harness for metrics assertions in place.
2. **Docker-compose integration environment.** Real Redis + two uvicorn workers + Firestore emulator. Manual + scripted runs of F-8, F-9, F-14, F-24. Instance IDs recorded in log lines so tests can prove traffic actually crossed processes.
3. **Production, dark.** All PRs merged and deployed with reconciler flag
   `ROOM_RECONCILER_ENABLED=0`. Apply the PR-T1-E monitoring stack and route it
   to a disabled rollout channel for 24 hours; review the dashboard and policy
   configuration. A disabled reconciler emits no ticks, so no runtime metric
   baseline is claimed at this stage.
4. **Production, reconciler enabled.** Flip `ROOM_RECONCILER_ENABLED=1`. Success criteria over the next 24h:
   - `reconciler_actions_total{reason=ended_room_local_cleanup}` is near-zero. If it's not, the primary paths are silently failing and Track 1 has surfaced a pre-existing bug — investigate before proceeding.
   - `terminal_rooms_with_resources` stays 0 or returns to 0 within the acceptance deadline whenever it briefly rises.
   - `oldest_overdue_cleanup_seconds` p99 stays under the acceptance target (proposed 60s).
   - No false terminations (compare against Firestore audit log).
5. **SIGTERM behavior verified in production.** Trigger a controlled deploy
   while a host and listener are active. Confirm both reconnect against
   `/resolve`, the room is not tombstoned, and service continues on the new
   revision. Uvicorn `1012` is the tested close contract; current production
   monitoring does not expose a server-side close-code counter, so retain the
   CI F-9 contract as the automated close-code guard and record browser/network
   evidence for this production gate.

Track 1 exit criteria are met when steps 1–5 all show green over a 7-day soak period.

---

## 5. Rollback plan

**First response to any reconciler-related regression: flip `ROOM_RECONCILER_ENABLED=0`.** This is immediate, requires no code deploy, and stops the class of behavior the reconciler introduces. Reserve code reverts for regressions that survive the flag flip.

Per-change rollbacks:

- **Reconciler (PR-T1-C):** `ROOM_RECONCILER_ENABLED=0`. Immediate.
- **SIGTERM close handling (PR-T1-D):** revert PR-T1-D. This removes the shared
  close classifier and returns host/listener handling to their prior paths;
  validate `1012` reconnect behavior explicitly before taking that action.
- **Startup safety (PR-T1-A):** revert PR-T1-A. Restores current behavior. Only meaningful if Track 1 has also flipped multi-instance — which it does not — so revert is low-consequence.
- **Sweeper transaction wrapper (PR-T1-B):** revert PR-T1-B. Restores the current stale-read behavior. Undesirable but not a regression from today.
- **Monitoring (PR-T1-E):** revert PR-T1-E. No runtime effect other than losing observability. PR-T1-C's minimum-required signals remain since they are in a separate PR.

**"Independently revertible" — caveat.** Individually the PRs merge and revert independently. But once landed in order:

- Reverting PR-T1-B leaves PR-T1-C running against the pre-atomic sweeper. Reconciler still functions (it doesn't depend on the sweeper's transaction wrapper), but the stale-read race in the sweeper returns.
- Reverting PR-T1-C leaves PR-T1-E's dashboards showing gauges that nothing populates. Not harmful; the dashboards read empty. Alerts on `terminal_rooms_with_resources` never fire.
- Reverting PR-T1-A while PR-T1-C is enabled has no interaction — reconciler and startup path do not overlap.

Rollback dependencies to remember: **flag flip first (reconciler), reverts second, and never revert PR-T1-C without first disabling its flag** (avoids a brief window where an outdated reconciler runs during the deploy).

---

## 6. Non-goals (restated)

- Host lease design (Track 2, audit §5.1b).
- Any change to `STT_NO_SPEECH_TIMEOUT_SEC` or STT idle watchdog logic.
- Provisioning real Redis in production. `REDIS_ENABLED` remains 0 throughout Track 1.
- Raising `--max-instances`. Remains 1 throughout Track 1.
- Firestore schema additions (lease fields). Existing schema only.
- Abandonment detection.
- Multi-instance rollout.

---

## 7. Open questions — answered

1. **Metric backend.** Structured JSON log lines feeding Cloud Logging initially. PR-T1-C emits the raw signals as log lines; PR-T1-E's adapter turns them into Cloud Monitoring log-based metrics with alert policies. **The `last_successful_reconciliation_at` freshness alert MUST be an external check** (Cloud Monitoring absence condition, or a small Cloud Scheduler ping) — a wedged process that stops logging must still trigger the alert, and it can't do that via its own log stream.
2. **Reconciler interval.** Configurable, default 30s (`ROOM_RECONCILER_INTERVAL_SEC`). Acceptance target: cleanup within 60s of confirmed termination under healthy Firestore, **including during Redis outages** (reconciler is Redis-independent by design). Supported load: single-digit rooms per instance in current production; tests must confirm this under 50 concurrent rooms as a headroom check.
3. **Client reconnect backoff after infrastructure shutdown.** Reuse the
   existing transient-reconnect policy in `useSubtitleSocket` (jittered
   exponential backoff, bounded retries). Test coverage confirms that neither
   host nor listener treats Uvicorn `1012` (or reserved code `4002`) as terminal,
   and that both reconnect via `/resolve` rather than tombstoning.
4. **Integration harness location.** `backend/tests/integration/resource_cleanup/`. Docker-compose file with:
   - One Redis service.
   - Two backend uvicorn processes with distinct `INSTANCE_ID` env vars, distinct host ports.
   - Firestore emulator.
   - A pytest driver that runs test scenarios and asserts against log lines / API responses / owner-map introspection endpoints.
   - Instance IDs recorded in every log line so tests can prove traffic actually crossed processes.

---

## 8. Related documents

- Audit: [`docs/03-analysis/resource-cleanup-audit.md`](../../03-analysis/resource-cleanup-audit.md) v5.
- Redis Pub/Sub design: [`docs/02-design/features/redis-pubsub-fanout.design.md`](../../02-design/features/redis-pubsub-fanout.design.md).
- Redis smoke test procedure: [`docs/03-analysis/redis-pubsub-smoke.md`](../../03-analysis/redis-pubsub-smoke.md).
- Redis Cloud Run runbook: [`docs/03-analysis/redis-pubsub-cloudrun-runbook.md`](../../03-analysis/redis-pubsub-cloudrun-runbook.md).
