# Resource Cleanup Audit — v5

> **Status:** draft v5 — scope proposal for the backend cleanup pass
> discussed after PRs #16 / #17 / #18 landed. Nothing here is
> implemented yet.
>
> **Source commit at draft time:** `e63c5d4b` (main, "fix(viewer):
> resume listener when a new room starts (#18)"). Line-number
> citations are correct at this commit and must be re-checked
> against HEAD before implementation.
>
> **v5 changes** (in response to review of v4, targeted corrections):
> - **Watchdog co-tuning removed.** v4 proposed lowering
>   `STT_NO_SPEECH_TIMEOUT_SEC` from 120s to 90s to "co-tune" with
>   a 90s lease TTL. The math doesn't work: watchdog and lease
>   measure different things (last recognized speech vs. last
>   successful renewal), so matching their nominal values does
>   not automatically produce a reconnect grace window. §5.1b now
>   keeps the 120s watchdog untouched and states the actual
>   reconnect grace formula: `TTL − (time since last renewal)`,
>   which is variable and bounded above by TTL. If the
>   application needs a supported silent-pause window longer than
>   that, option A (dedicated host heartbeat) is required — the
>   audit no longer implies option B alone can guarantee such a
>   window through timing.
> - **Firestore discovery query corrected.** v4's `hostSessionId !=
>   null` filter is unreliable in Firestore's query semantics
>   (`!=` on a missing field is undefined behavior for these
>   purposes). Grandfathering is handled by the ordering on
>   `hostLeaseExpires` instead — documents missing that field are
>   naturally excluded from range-filtered / ordered queries.
>   Detection flow rewritten with cursor-based pagination to
>   prevent early-record starvation (a malformed row at the top
>   of the sort order can't block progress on later rows). Index
>   scope stated as *collection-group* on `rooms`.
> - **In-transaction validation moved to the transaction body.**
>   session ID, generation, timestamp type, room path, and
>   current expiration are all re-verified inside the atomic
>   transition, not only checked before it. §5.1b property 4
>   rewritten.
> - **Lease-loss rule consolidated into one non-contradictory
>   set.** v4 had "retry for 30s before closing" and "stop
>   producing as soon as any renewal fails" living in different
>   paragraphs. v5 states one four-part rule: transient write
>   failures permit bounded retries while last-confirmed lease
>   is still valid; confirmed ownership replacement stops the
>   old session immediately; lease expiration or the failure
>   deadline stops production; every in-flight translation
>   result is ownership-checked before publication.
> - **F-20 extended** to cover an in-flight translation
>   completing after ownership is lost — this is the concrete
>   version of the "handler alive ≠ still owns room" rule.
> - **Transaction-callback purity vs. cleanup idempotence
>   distinguished.** The exactly-once guarantee applies to
>   external effects triggered by a *specific successful commit*
>   — that's a within-transaction property. Cleanup as a whole
>   must remain idempotent because the process may die between
>   commit and cleanup, in which case another instance's
>   reconciler must safely rerun cleanup. Both are now stated
>   explicitly and separately.
> - **§11 TTL numbers updated** to match the v4 staging proposal
>   (90s / 20s); the "no instance running" paragraph now names
>   the abandonment detector's first pass as the recovery
>   mechanism (v4 still referenced the removed startup-cleanup
>   path).
> - **F-22 rewritten** to test the actual client capture
>   behavior during silence, including a mode that sends no
>   audio. The old wording assumed a timely reconnect; the new
>   wording requires the test to demonstrate presence during a
>   supported pause or to fall back to option A.
> - **Implementation tracks split.** New §9a divides the work
>   into Track 1 (ready to start: startup safety, Redis-
>   independent reconciler, subscription/resource cleanup,
>   overdue monitoring) and Track 2 (held pending v5 design
>   items: lease-based abandonment, watchdog changes, multi-
>   instance scaling). Track 2 does not delay Track 1.
>
> **v4 changes** (in response to review of v3, targeted corrections):
> - **§5.1b lease discovery is no longer host-local.** v3 only
>   inspected rooms with a *local* STT handler. If instance A owns
>   the host and crashes, surviving instance B has only listeners
>   and would miss the room until the 15-min idle sweeper. v4
>   requires a bounded global Firestore query on
>   `status="live" AND hostLeaseExpires < now`, run by any
>   instance. Composite index required.
> - **§5.1b lease vs. STT idle watchdog reconciled explicitly.**
>   v3 said "lease renews on host WS liveness" while the current
>   idle watchdog closes the STT WS after 120s of *speech*
>   silence. v4 documents the two options and adopts option B
>   (STT-close begins a documented reconnect grace period; lease
>   TTL is sized to cover it) with option A (dedicated host
>   heartbeat) noted as a fallback if data shows too many
>   legitimate pauses exceed grace.
> - **Old session must stop producing after losing ownership.**
>   Semantics for asymmetric Firestore failure (host cannot renew,
>   detector can read + terminate) documented explicitly: this is
>   indistinguishable from a vanished host, and policy requires
>   the old session to stop producing on renewal failure.
> - **§5.1b starting TTL / renewal numbers changed** from 30s/10s
>   to **90s TTL / 20s renewal** for initial staging. Not a
>   proven optimum; measure detection and cleanup separately
>   before tightening.
> - **§4a.2 transaction callback purity.** Firestore can rerun
>   transaction callbacks. External side effects (close sockets,
>   cancel tasks, publish Redis) MUST fire only after the
>   transaction commits successfully — not inside the callback.
> - **§8 overdue-cleanup alert corrected.** v3's proposal of
>   alerting on `locally_owned_rooms_size` was wrong — a healthy
>   long broadcast keeps that gauge non-zero. Replaced with
>   `terminal_rooms_with_resources`, `oldest_overdue_cleanup_seconds`,
>   `cleanup_inflight`, `last_successful_reconciliation_at`. F-18
>   assertions rewritten accordingly, plus a new test that a
>   long-running healthy room produces no overdue alert.
> - **Rollout for pre-lease live rooms.** Existing rooms in
>   production have no lease fields. v4 grandfathers them: rooms
>   missing lease fields fall back to the existing 15-min idle
>   policy, and only rooms started after the lease deploy get the
>   short abandonment deadline.
> - **Batched lease writes must preserve per-row conditional
>   checks.** If renewal is ever batched, the batch must retain
>   the `(session_id, generation)` guard per row; otherwise
>   don't batch.
> - **§9 gate ordering:** F-15 gates the startup-safety fix AND
>   the full multi-instance rollout — not every unrelated
>   deployment. F-10 moved from Group D (reconciler) to Group E
>   (abandonment), since F-10 requires the abandonment detector
>   to flip Firestore to ended in the first place.
> - **New tests:** F-19 (host-crash listener-only discovery),
>   F-20 (asymmetric Firestore renewal failure), F-21 (long
>   healthy room produces no overdue alert), F-22 (silent worship
>   period longer than lease TTL — must not falsely terminate).
> - **v3 audio-silence phrasing softened.** The claim was "no
>   audio bytes arrive during silence." Corrected: audio arrival
>   is not *sufficient* evidence of host presence across all
>   capture modes (some pipelines transmit silent audio). The
>   rejection of B-1 still stands on that weaker claim.
>
> **v3 changes** (in response to review of v2):
> - Abandonment mechanism recommendation flipped to **B-2 (explicit
>   host lease with generation)** after verifying that `touch_audio`
>   fires only on audio bytes — a silent worship period would let
>   `lastAudioAt` grow stale despite the host being fully connected,
>   which makes B-1 unsafe for this application. Lease requirements
>   spelled out.
> - §4a.2 concurrency: idempotent `end_room()` does not prevent
>   *incorrect* termination from a stale read. Now requires atomic
>   conditional transition that re-verifies activity/lease
>   generation at write time.
> - §5.1a recovery target is now stated as independent of Redis.
>   The whole point of the reconciler is to bound recovery when
>   Redis fanout failed; making the target conditional on Redis
>   would defeat it.
> - §5.1a terminal predicate: replaced blanket `status != "live"`
>   with an explicit set of recognized terminal states. Missing
>   docs, malformed records, and transitional states get separate
>   handling. Unknown state must not silently become confirmed
>   termination.
> - §5.1a ownership inventory: dropped the "canonical set"
>   proposal in favor of a union-of-existing-owner-maps read (which
>   already deduplicates via set operations); added
>   provider/background-task ownership to that inventory.
> - §9 deployment guard corrected: the invalid combination is
>   `REDIS_ENABLED=0` with `--max-instances>1`. `REDIS_ENABLED=1`
>   with `--max-instances=1` is a valid staged-rollout state that
>   should be allowed.
> - §9 gate ordering fixed. v2 required abandonment tests to pass
>   at a step before abandonment was implemented. Tests are now
>   assigned to the step that provides their behavior, with a full
>   integration gate before rollout.
> - Two new tests added: **F-17 renewal-vs-expiration race** and
>   **F-18 cleanup-never-completes monitoring**.
> - Assertions tightened throughout: receiving a close frame is
>   not asserted where the network is broken; provider close
>   timeout logged is evidence of a bounded attempt, not proof
>   the remote session stopped.
> - Removed the "benign in single-instance production" claim from
>   §4a.1 — not established by any evidence collected here. Also
>   recommends removing the unconditional startup-termination path
>   in favor of the validated abandonment mechanism.

## 1. Purpose

Enumerate every path by which a live room can terminate, list the
resources each path is expected to release, cite the current release
mechanism, and name the gap that keeps this from being an operational
guarantee across Cloud Run instances.

Scope explicitly **excludes** viewer/UI behavior — PR #18 closed
that loop. Scope is confined to backend-owned resources whose leak
costs money (provider connections, Cloud Run slots, Firestore state
churn) or correctness (contradictory lifecycle events, orphaned
tombstones, listeners on the wrong side of a terminated room).

The definition of "finished" this audit is written against:

> Every known termination path releases owned resources within a
> bounded, measured time, including cross-instance failures, with
> regression tests and per-instance runtime metrics that show the
> bound holds in production. Behavior during dependency outage
> (Firestore, Redis) is documented and tested separately from the
> healthy-path bound. Monitoring detects overdue resources even
> when no successful cleanup event is emitted.

Passing a happy-path browser test does not meet that definition.

## 2. Two distinct problems

The most important framing point: recovery and abandonment
detection are two different problems, solved by different
mechanisms. Every proposal in §5 is scoped to one of them and does
not claim to solve the other.

- **Recovery of an ended room.** Firestore says the room is ended,
  but some instance still holds local sockets, provider tasks, or
  subscription refcounts for it. Cause: End Service ran, Redis was
  supposed to fan out the terminal broadcast, and this instance's
  subscriber missed the message (or the instance was mid-restart,
  or Firestore was momentarily unreachable when the terminal
  fanout was attempted, etc.). A Firestore-read reconciler solves
  this — the ground truth already agrees the room is over.
- **Detection of an abandoned live room.** Firestore says the room
  is live, but the host WebSocket is gone and cannot come back.
  Cause: host tab close, network drop, or crash without a graceful
  End Service. A Firestore reader cannot solve this alone, because
  Firestore reflects "still live" — the very question is whether
  the room *should* be live. Requires a positive
  presence-of-a-live-host signal, and requires the mechanism
  producing that signal to be robust to a departed host silently
  looking present.

## 3. Owned resources catalog

Resources a live room instance holds. Each row is a leak surface
if not released; some are correctness-critical rather than
cost-critical.

| Resource | Owner | Location | Leak cost |
|---|---|---|---|
| Listener WebSocket | `ConnectionManager.connections_by_room` | in-memory, per Cloud Run instance | Cloud Run request-slot, egress if broadcasts still fire |
| Host WebSocket | `manager.host_presence_by_ws` + `role_by_ws` | in-memory, per instance | request-slot, prevents graceful host_absent detection |
| STT provider connection | Deepgram / OpenAI Realtime / Gemini Live sessions inside the STT handler | in-memory, tied to host WS lifetime | per-second provider billing |
| STT background tasks | `consumer` / `producer` / `idle_watchdog` asyncio tasks inside each STT handler | asyncio scheduler, per instance | correctness (tasks writing after room ended) + tail resource use |
| Redis subscription (channel) | `pubsub._subscriptions` refcount + `host_subscription_owned_by_ws` / `listener_subscription_owned_room_by_ws` | in-memory per instance | subscription slot; correctness (missed terminal broadcast) |
| Host presence tracking | `host_presence_counts_by_room`, `hostless_since_by_room` | in-memory per instance | correctness — sweeper decisions read this |
| Room-end hooks (list) / per-room module state (maps) | `room_end_hooks` list is process-scoped; per-room state is TTS/pipeline maps cleaned by `_cleanup_room_local_state` | in-memory per instance | correctness after room ends |
| Local ended-room tombstone | `ended_rooms_local` (5 min TTL) | in-memory per instance | correctness (suppresses stale `roomStatus=live` from disconnect finally) |
| Firestore room document | `organizations/{orgId}/rooms/{roomId}` (`status`, `endedAt`, `lastAudioAt`, etc.) | Firestore, source of truth | correctness — orphaned live status blocks new rooms and misleads sweeper |
| Firestore usage counters | `organizations/{orgId}/usage/{periodKey}` | Firestore | billing accuracy |

## 4. Termination-path × resource matrix

Every code path that can end a live room today. Values in cells:
✅ released by this path, ⚠️ partially released, ❌ not released,
N/A doesn't apply.

Legend for "Current mechanism":
- `end_room()` — `multichurch_store.end_room` writes Firestore end state.
- `close_room_listeners` / `close_room_hosts` — server-side WS closes.
- `broadcast_room(STATUS ended)` — terminal message to same-instance +
  Redis subscribers.
- `manager.forget_room` — drops in-memory per-room state.
- `_cleanup_room_local_state` — module-level per-room state cleanup.
- STT `finally:` — the try/finally at the bottom of each STT handler
  (Deepgram at `main.py:4161`, OpenAI Realtime at `main.py:4243`,
  Gemini at `main.py:4675`) closes the provider connection and calls
  `note_host_disconnected`.

### 4.1 End Service (host click) — `POST /api/org/{orgId}/rooms/{roomId}/end`

| Resource | Status | Current mechanism |
|---|---|---|
| Listener WS (same instance) | ✅ | `close_room_listeners(1000, room_ended)` |
| Listener WS (other instance) | ⚠️ | via Redis fanout of STATUS ended → subscriber closes local sockets; unreliable if the subscriber missed the message (G-5) |
| Host WS + STT provider | ✅ | `close_room_hosts` fires `host_shutdown_cb` → STT handler `finally:` closes provider |
| STT background tasks | ✅ | STT handler cancels `consumer` / `producer` / `idle_watchdog` and awaits them in `finally:` |
| Redis subscription refcount | ✅ | released on each ws disconnect via ownership check |
| Firestore room doc | ✅ | `end_room()` |
| Room-end hooks / per-room state / tombstone | ✅ | `broadcast_room` delivers the terminal, hooks fire on every instance, `_mark_room_ended_locally` sets tombstone |

**Verified in production 2026-09-15 after PR #18 deploy** in the
current single-instance, `REDIS_ENABLED=0` configuration. This is
the baseline the audit measures other paths against. Not verified
in a multi-instance configuration.

### 4.2 Host closes tab or drops network (no End Service click) — **abandonment problem**

| Resource | Status | Current mechanism | Detection time | Cleanup time |
|---|---|---|---|---|
| STT provider connection | ✅ *when disconnect is detected* | STT handler `finally: dg.close()` on WS drop | tab close: seconds. Silent network loss: uvicorn/TCP keepalive-dependent, minutes | up to 3s (close attempt is bounded; a timeout is evidence of a bounded attempt, **not** proof the remote provider session has stopped) |
| STT background tasks | ✅ | cancelled + awaited in the same `finally:` | with STT close | seconds |
| Host presence tracking | ✅ | `manager.note_host_disconnected` in same `finally` | with STT close | immediate |
| Host Redis subscription refcount | ✅ | ownership check releases | with STT close | immediate |
| Deepgram usage recorded | ✅ | `record_deepgram_usage` in `finally` | with STT close | immediate |
| Listener WSs (same room) | ❌ | **no close** — listeners stay connected until an abandonment detector fires | no detector today | up to `ROOM_IDLE_TIMEOUT_SEC` (default 900s = 15 min) |
| Firestore room doc | ❌ | still `status=live` | same as above | 15 min |
| Listener Redis subscription refcounts | ❌ | still held by connected listeners | same as above | 15 min |
| Cross-instance listeners | ❌ | not notified until sweeper's `end_room` fires on some instance | same as above | 15 min |

**Sweeper path:** `_room_sweeper_loop` calls `stale_live_rooms`
which compares `lastAudioAt` age against `ROOM_IDLE_TIMEOUT_SEC=900`.
When crossed, it runs `end_room()` + `close_room_listeners` +
`close_room_hosts` locally.

**`host_absent` is currently a no-op.** `main.py:154` sets
`ROOM_HOST_PRESENCE_END_ROOMS = False` — the sweeper detects
absence via `ROOM_HOST_PRESENCE_GRACE_SEC=300` but only logs.
Effective bound is idle_timeout, not host_absent.

**Detection vs. cleanup, phrased carefully.** These are different
quantities and must not be conflated in acceptance criteria. Tab
close produces a TCP-level RST quickly; a laptop lid closed on
Wi-Fi may take minutes before OS keepalives or Cloud Run idle
timeouts fire the disconnect. Provider close-timeout logging
proves *this side* attempted close; it does not prove the
provider stopped billing. That's a limit of what any test on our
side can assert; provider-side confirmation would need provider
API metadata that we don't currently ingest.

**Gap G-1:** listener sockets and Firestore room state persist up
to 15 minutes after abandonment. Needs the mechanism in §5.1b,
not the reconciler in §5.1a. Provider bill stops when the host
disconnect is detected — that fast path is already working.

### 4.3 Trial cap / monthly cap hit mid-broadcast

| Resource | Status | Current mechanism | Bound |
|---|---|---|---|
| All resources (as End Service) | ⚠️ | `enforce_live_usage_caps` flags the room in the sweeper's next tick; sweeper then runs the End Service cleanup path | `ROOM_SWEEPER_INTERVAL_SEC` default 60s |

**Gap G-2:** up to 60 seconds of billed provider usage after the
cap crossed. Probably acceptable; should be measured, not assumed.

### 4.4 `ROOM_IDLE_TIMEOUT_SEC` / `ROOM_MAX_DURATION_SEC` (sweeper)

| Resource | Status | Current mechanism | Bound |
|---|---|---|---|
| All resources | ✅ | sweeper End Service path (§4.1 mechanism) | sweeper interval + config bound |

### 4.5 STT idle watchdog (`_stt_idle_watchdog`, `STT_NO_SPEECH_TIMEOUT_SEC=120`)

| Resource | Status | Current mechanism | Bound |
|---|---|---|---|
| STT provider connection | ✅ | `dg.close()` inside watchdog | 120s of silence |
| STT WebSocket to host | ✅ | `websocket.close(1000)` inside watchdog | 120s |
| Host presence tracking | ✅ | STT `finally:` fires after watchdog closes the WS | seconds after close |
| Room end (Firestore, listeners) | ❌ | **watchdog does NOT call `end_room`** — only closes the STT session | falls back to §4.2 / §4.4 |

**Gap G-3:** watchdog kills the provider bill fast (good) but
leaves the room live in Firestore with listeners connected.
Reconnecting host within 15 minutes resumes — a feature worth
preserving. The fix here is about listener UX and the abandonment
policy (§5.1b), not about ending the room the moment STT goes
idle.

### 4.6 Cloud Run instance shutdown (SIGTERM) — **infrastructure event, not room termination**

| Resource | Status | Current mechanism | Bound |
|---|---|---|---|
| Redis subscriber background task | ✅ | `_on_shutdown` → `pubsub.stop()` | 10s Cloud Run drain window |
| Sweeper task | ✅ | `_on_shutdown` cancels + awaits | 10s drain window |
| Live listener/host WSs | ❌ | no explicit close — uvicorn drops sockets on process exit; clients see abnormal close (1006) | immediate but semantics are wrong for reconnect |
| Firestore room state | ✅ (correctly left alone) | shutdown does not mutate Firestore | N/A |
| STT provider connections | ⚠️ | STT handler `finally:` fires on task cancellation, so provider close attempts run within the 3s timeout; a timeout logs an outcome but does not confirm remote-side stop | best-effort inside drain window |

**Semantic point:** SIGTERM is an infrastructure event. The room
is *not* ending; it's alive on Firestore and other instances may
already be serving it or about to (Cloud Run rolls instances).
Sending `close(1000, room_ended)` to clients during shutdown, as
v1 originally proposed, would make PR #18's client tombstone the
room and stop reconnecting.

**Correct SIGTERM semantics:**
- Release *local* resources: cancel background tasks, close
  provider connections (bounded attempts), unsubscribe from Redis,
  clear the in-memory maps.
- Close client WebSockets with a code that says "this instance is
  going away; please reconnect" — a distinct code like `1012`
  (Service Restart) or a custom `4002 instance_shutdown`. **Never**
  `room_ended`.
- Under the 10s Cloud Run drain, shutdown work must have a shared
  deadline and bounded concurrency. Test against the actual server
  shutdown sequence, not `_on_shutdown()` in isolation.
- Frontend clients treat that code as a transient close and
  reconnect against `/resolve`, which routes them to a healthy
  instance and (if the room is still live) picks up broadcasting
  again.

**Gap G-4:** current shutdown drops sockets as 1006 with no
reconnect hint. Fix is a deliberate close with an
infrastructure-reconnect code plus client-side handling in
`useSubtitleSocket` to distinguish this code from `room_ended`.

### 4.7 Redis subscriber missed a terminal broadcast — **recovery problem**

| Resource | Status | Current mechanism | Bound |
|---|---|---|---|
| Cross-instance listener/host cleanup | ❌ | **no recovery mechanism** — the idle sweeper only walks rooms Firestore still shows as `status=live`. A room whose Firestore state is already terminal will never be returned by `stale_live_rooms` (`multichurch_store.py:2766` — `continue` on non-live). An instance holding sockets for that room retains them until process recycle. | **no demonstrated bound** |

**Gap G-5:** cross-instance recovery of missed terminal broadcasts
is unbounded today. The reconciler in §5.1a is designed to close
this specifically.

### 4.8 Orphaned Firestore state (instance died mid-flight) — **recovery + abandonment problem**

| Resource | Status | Current mechanism | Bound |
|---|---|---|---|
| Firestore room stuck at `status=live` on the dead instance | ⚠️ | sweeper `stale_live_rooms` on any surviving instance eventually flags via idle_timeout (if `lastAudioAt` stops advancing) | up to `ROOM_IDLE_TIMEOUT_SEC` after audio last arrived — and only if the abandonment mechanism in §5.1b agrees the room is abandoned |
| In-memory state on the dead instance | N/A | (instance is gone) | — |
| In-memory state on surviving instances | ⚠️ | released once Firestore reflects ended (via sweeper *or* the abandonment detector), then §5.1a reconciler cleans up on other instances | Firestore-transition bound + reconciler interval |

Startup cleanup, previously miscategorized here, is now in §4a.

## 4a. Blockers before multi-instance rollout

Code paths that would corrupt cleanup at `--max-instances > 1`.
These must be fixed before Redis is turned on and the instance
count is raised.

### 4a.1 `_cleanup_live_rooms_on_startup` ends other instances' healthy rooms

`main.py:1235` calls
`stale_live_rooms(idle_seconds=0, max_duration_seconds=0)` at
startup. `stale_live_rooms` returns every live room where `idle >=
idle_seconds`; with `idle_seconds=0` that's every live room.

In multi-instance:
- Instance A broadcasts a live service; `lastAudioAt` advancing;
  Firestore `status=live`.
- Instance B starts (autoscale-up, deploy).
- B's startup task queries Firestore, sees A's live room, calls
  `end_room(reason="server_restart")` on it.
- A's listeners get `room_ended`. Host gets terminated. Broadcast
  destroyed by a sibling instance starting up.

**Recommended fix:** remove the unconditional startup-termination
path. Let the abandonment mechanism in §5.1b decide which rooms
are stale, based on its own lease evidence. There is no observed
justification for a distinct "server_restart" reason that operates
without abandonment evidence.

If the fix instead retains this function, it must prove
abandonment before ending (lease expiration checked atomically,
same as §5.1b requires for the abandonment detector), and the
reason string must reflect what actually happened, not
"server_restart."

**Merge-blocker test:** F-15 — start instance B while A is
actively broadcasting; A's room stays live, no client receives a
terminal event.

### 4a.2 Sweeper concurrency across instances — stale-read termination race

Multiple sweeper instances race to `end_room` the same room.
`end_room` is idempotent (returns `alreadyEnded=True` on the
loser), so *duplicate* termination effects are prevented. That is
**not** the concurrency risk this section is about.

The risk is a stale-read termination race, present in both the
sweeper and the abandonment detector in §5.1b:

- t=100: detector reads `lastAudioAt` (or host lease) — expired.
- t=101: host reconnects; `lastAudioAt` refreshed / new lease
  acquired.
- t=102: detector, acting on its t=100 read, calls `end_room`.

The idempotency of `end_room` doesn't help; the room *becomes*
ended by a decision made before it was healthy.

**Required fix:** the terminate call must be an atomic conditional
transition in Firestore. Concretely, `end_room` (or an
abandonment-specific variant) must, inside a Firestore
transaction, re-verify one of:

- for idle-based termination: `lastAudioAt` is still older than
  the threshold at write time, AND
- for lease-based termination: the lease `generation` and
  `expiresAt` observed at read time still match at write time.

If the re-check fails, the write is abandoned. This is a Firestore
transaction pattern; the store layer already supports it for other
operations.

**Transaction callback purity (critical).** Firestore may rerun a
transaction callback during concurrent updates. Any external side
effects placed inside the callback risk being executed multiple
times, or being executed before the commit succeeds. Therefore:

- The callback body is **pure Firestore**: read the doc, validate
  status / ownership / lease generation / timestamp type / path,
  and stage the write. Nothing else.
- External effects — `close_room_listeners`, `close_room_hosts`,
  cancelling STT background tasks, `broadcast_room(STATUS ended)`,
  `manager.forget_room`, `_cleanup_room_local_state` — fire only
  **after** the transaction returns success. If the transaction
  is abandoned (validation failed on rerun), none of those effects
  fire.
- The test suite for §4a.2 and §5.1b must inject a transaction
  retry (simulate a concurrent write) and assert:
  - The callback executes at **least twice** — proves a retry
    actually happened. "At least once" wouldn't distinguish a
    retry from a single successful pass.
  - Within the single controlled successful execution of the
    outer transaction, the external cleanup effects tied to
    the committing branch run **once**. This is a
    within-transaction property, not a system-wide guarantee.
    See "Distinction from cleanup idempotence" below.

**Distinction from cleanup idempotence.** "Runs once per
successful commit" is a within-transaction property. It does
**not** promise that cleanup for a given room happens at most
once across the whole system:

- The process may die after Firestore commit but before external
  cleanup begins. On next reconciler pass (either on this
  instance's restart or on any other instance), Firestore shows
  the room terminal and cleanup runs.
- Two instances' detectors may race; the losing transaction is
  abandoned, so cleanup fires only from the winning commit —
  but if that instance dies mid-cleanup, another instance's
  reconciler can pick it up.
- Therefore all cleanup operations (`close_room_listeners`,
  provider close, `forget_room`, `_cleanup_room_local_state`,
  Redis subscription release) must remain **idempotent** — safe
  to rerun. The exactly-once-per-commit rule and idempotence
  operate at different layers.

## 5. Recovery and abandonment mechanisms

### 5.1a Ended-room reconciliation — for G-5 (missed terminal broadcast) and G-8 partial (orphan on surviving instances)

**Problem this solves:** Firestore has a recognized terminal state
for the room; some instance still holds local sockets, provider
tasks, or subscription refcounts for it.

**Design:** each instance runs a lightweight background task
(interval configurable, proposed starting value 30s) that:

1. **Ownership inventory (read):** builds the set of
   `(org_id, room_id)` keys the instance owns local resources for
   by unioning the keys of `connections_by_room`,
   `host_presence_by_ws` mapped through, `host_subscription_owned_by_ws`,
   `listener_subscription_owned_room_by_ws`, and the set of rooms
   with active STT background tasks. Set union naturally
   deduplicates; no new registry is introduced. If a future change
   adds another owner map, this list has to be updated — that
   dependency is called out in code with a test that asserts every
   owner-map add-site is present here.
2. **Batch-read Firestore** for the room documents in that set.
3. **Terminal predicate (explicit):** for each returned doc,
   classify:
   - `status == "ended"` → *confirmed terminal*, proceed to
     cleanup.
   - `status == "live"` → not terminal; skip.
   - **document missing** (deleted or never existed) → *not*
     terminal by default. Log at warn level; do not clean up. A
     missing doc is likelier a bug in the write path than a
     signal to release resources.
   - **malformed** (missing `status`, unexpected type) → not
     terminal; skip; log at warn.
   - **any unrecognized value** — same treatment as malformed.
   - **Firestore read failed** → skip; increment failed-tick
     metric; retry next interval.
4. For each *confirmed terminal* room, run the local cleanup path
   (`close_room_listeners`, `close_room_hosts`, cancel any STT
   background tasks bound to the room, `forget_room`,
   `_cleanup_room_local_state`) idempotently. Cleanup targets a
   specific `(org_id, room_id)` — never global.
5. **Passes do not overlap.** If a pass is still running when the
   next interval fires, the second pass skips.

**Explicit semantics:**
- A Firestore *read failure* MUST NOT be interpreted as "room
  ended." Reconciler either skips or marks the pass failed.
  Never converts an outage into a termination event.
- Cleanup for room A never affects room B's state, even if the
  reconciler begins A's cleanup after B has re-opened under the
  same service URL. Every close operation is `(org_id, room_id)`
  scoped.
- Reconciler does **not** write Firestore. It is a follower.
- Reconciler operation is **independent of Redis state.** Redis
  disconnected does not prevent the reconciler from running; that
  independence is the whole point.

**What this closes:**
- G-5 (missed cross-instance terminal broadcast).
- G-8 partial (orphaned state on surviving instances after another
  instance died; the dead instance's Firestore transition still
  requires §5.1b to flip live→ended).

**What this does NOT close:** G-1, G-3 (both are abandonment
problems; Firestore says live). G-4 (shutdown mechanism, §4.6).

**Acceptance target (to validate, not guaranteed):**
- **Under healthy Firestore, regardless of Redis state**: local
  cleanup completes within 60 seconds of Firestore reflecting
  `status=ended`. Measured bound = interval + Firestore
  read latency + per-close time.
- **Under Firestore outage**: no false terminations. Reconciler
  skips. Recovery deadline resumes once Firestore reads succeed
  again. Monitoring must detect resource-count growth during the
  outage window (see §8).

### 5.1b Abandoned-room detection — for G-1 (host abandonment), G-3 (idle listener UX)

**Problem this solves:** Firestore says the room is live; the
host is gone and cannot come back. Reconciliation alone cannot
solve this; Firestore says exactly what the mechanism must
question.

#### Why lease, not `lastAudioAt`

`multichurch_store.touch_audio` (`multichurch_store.py:2717` and
`:5570`) is called from each STT handler; today it fires only
when audio bytes arrive (every 20s while ingesting). More
generally: audio arrival is not a *sufficient* evidence of host
presence across all capture modes. A capture pipeline in
always-on mode transmits silent audio too, so `lastAudioAt`
could theoretically advance during silence — but only if the
client is running such a mode, which today's frontend does not
guarantee. Speech-detection watchdogs on the STT side
(`last_speech_activity_ts` at `main.py:3884`) further distinguish
"bytes arrived" from "speech happened."

`lastAudioAt` accurately answers *when did audio arrive?* It does
not answer *is the host still present?*, and it does not answer
*is the host actively facilitating the service?* Using a short
`lastAudioAt` threshold as an abandonment signal would end
services during silent worship moments whenever the client
happens to be in a capture mode where silence yields no bytes.

Keep `lastAudioAt` for the existing 15-minute idle policy — a
coarse safety net whose threshold sits well past any legitimate
silent period.

#### Lease design

The host STT handler holds a **host lease** on the room. The
lease is a Firestore-persisted claim keyed by
`(host_session_id, generation)` and expiring at `hostLeaseExpires`.

Required properties:

1. **Session + generation scope.** A lease belongs to a specific
   host session — a UUID minted at STT connect — and a
   monotonically increasing generation, not just to the room.
2. **Alive-connection renewal.** Renewal writes only fire in
   response to *evidence the host WebSocket is currently
   healthy* — the STT handler's `receive` returning without error
   is the simplest such evidence. A pure server-side timer that
   renews the lease unconditionally is forbidden; it would keep
   a disconnected host looking present indefinitely.
3. **Old-lease safety.** An old host's disconnect or a delayed
   heartbeat from a previous session must not revoke or overwrite
   a replacement host's lease. Renewal writes are conditional on
   `(session_id, generation)` matching the current stored values;
   otherwise they no-op.
4. **Atomic recheck on termination.** The abandonment
   detector's `end_room` call is inside a Firestore transaction
   (see §4a.2). The transaction body re-reads the room document
   and validates all of the following before staging the write:
   - `status == "live"` (not already terminated by someone else)
   - `hostSessionId` matches what the detector saw at discovery
   - `hostLeaseGeneration` matches what the detector saw at
     discovery
   - `hostLeaseExpires` is a valid timestamp type, still
     `< now` at transaction time
   - The document path resolves to the room whose ownership
     the detector intended to terminate (guard against a
     mid-query rename in test scenarios)
   If any check fails, the transaction is abandoned — the write
   does not commit, and the external cleanup effects that would
   have fired after commit do not fire. See §4a.2 for the
   callback-purity rules that make this safe under Firestore's
   automatic transaction retry.
5. **Batched writes preserve per-row conditional checks.** If
   lease renewal is ever batched (e.g., a single write path
   handling multiple hosts on one instance), the batch must
   retain the `(session_id, generation)` guard *per row*.
   Batched writes that drop the per-row conditional are
   forbidden. If the batch layer cannot preserve per-row
   conditionals, don't batch lease writes.
6. **Failure-mode semantics, one four-part rule.** Prior drafts
   had "retry for 30s before closing" and "stop producing as
   soon as any renewal fails" in different paragraphs. Those
   were contradictory. v5 states the rule once:
   1. **Transient write failure** — a renewal write returns an
      error, but the lease last confirmed by the STT handler is
      *still valid at wall-clock time*. The handler retries
      with bounded backoff up to a documented threshold
      (proposed 30s). During this window the STT handler MAY
      continue accepting audio and producing translations,
      because ownership has not been proven lost.
   2. **Confirmed ownership replacement** — the handler
      observes (via a subsequent Firestore read succeeding, or
      via a Redis terminal broadcast) that `hostSessionId` or
      `hostLeaseGeneration` no longer matches the handler's
      lease. The handler stops production IMMEDIATELY: cancels
      in-flight translation work, closes the STT WS with the
      shutdown-reconnect code, and does not publish any pending
      results.
   3. **Lease expiration or retry threshold reached** — the
      handler's last-confirmed lease is now stale
      (`hostLeaseExpires < now` per the handler's own clock,
      applying a small margin for skew), OR the retry threshold
      from case (1) has been exceeded without a successful
      renewal. Either condition stops production the same way
      case (2) does.
   4. **In-flight translation check before publication** — a
      translation call started before ownership was lost may
      return after ownership was lost. Every publish site
      (broadcast, Firestore segment write, TTS emission) MUST
      re-check ownership against the current lease state before
      publishing. "Handler alive" is not sufficient evidence of
      ownership; a valid, unexpired, matching lease is.
   - **Firestore read failure** on the detector side → skip
     pass, never falsely terminate. Not part of the four-part
     rule; it belongs to the detector, not the handler.
   - **Asymmetric Firestore failure** — the host cannot renew,
     but the detector can read and terminate. This is
     indistinguishable from a genuinely vanished host. The
     detector may terminate; this is *correct by policy*. See
     "Loss of lease is not proof of voluntary abandonment"
     below.

#### Lease vs. STT idle watchdog — reconciliation

An open design question in v3: renewal requires "evidence the WS
is healthy," but the existing STT idle watchdog closes the WS
after `STT_NO_SPEECH_TIMEOUT_SEC=120` of *speech* silence
(verified against `main.py:3884` — `last_speech_activity_ts`
advances only on transcript arrival). A legitimate silent worship
period exceeding that threshold would close the STT WS today,
which under a naive lease design would immediately stop
renewals.

The **watchdog is not being changed by this work.** v4 previously
proposed lowering `STT_NO_SPEECH_TIMEOUT_SEC` from 120s to 90s
to "co-tune" with the lease TTL. That proposal has been
withdrawn for two reasons:

1. Lowering the watchdog is a functional behavior change with
   no justification derived from cleanup requirements. It would
   shorten the maximum legitimate speech-silence pause without
   any cleanup-side benefit.
2. The watchdog and the lease measure different quantities —
   time since the last recognized *speech transcript* vs. time
   since the last successful *lease renewal*. Matching their
   nominal values does not automatically produce a reconnect
   grace window. Worked example with a naïve 90s / 90s
   co-tune:
   - Last successful lease renewal at t=0. Lease expires at
     t=90.
   - No speech throughout. Watchdog trips at t=90 and closes
     STT WS. No more renewals possible.
   - Reconnect grace remaining after watchdog close = 0.
   - Alternatively, renewals continued through t=80 (lease
     renewal fires every 20s while STT WS is healthy). Lease
     expires at t=170. Reconnect grace after t=90 watchdog
     close = 80s. But this depends entirely on when the last
     renewal fell relative to the watchdog trip — it is not
     guaranteed by the timer values.

So neither 90/90 nor 120/150 actually guarantees a specific
reconnect grace window. Two options remain:

**Option B (adopted for initial implementation, with honest
limitations): STT-close naturally starts a variable reconnect
grace.**

- When the STT WS closes (watchdog, network hiccup, brief
  backgrounding), the lease is not immediately revoked. It
  expires at its TTL from the last successful renewal.
- Reconnect grace after STT close is `TTL − (time since last
  renewal at moment of close)`, bounded above by TTL and
  bounded below by 0 depending on timing.
- With a 90s TTL and 20s renewal cadence, the grace is
  therefore somewhere in `[TTL − renewal_interval − jitter, TTL]`
  ≈ `[68s, 90s]` under healthy conditions.
- On client reconnect, the new STT session mints a fresh
  session_id and acquires a lease conditional on the old one
  being expired or matching a client-supplied prior-session-id.
  The atomic recheck (property 4 + transaction validation
  block) handles the "old detector saw expired lease, new
  client renewed just in time" race.

Option B is sufficient for services whose supported silent
periods fit inside the grace window. It is **not** sufficient
for services that need to guarantee a long silent pause
(several minutes) without any risk of false termination. If
production data shows a non-trivial rate of legitimate pauses
triggering lease expiry, escalate to option A.

**Option A (required if option B is insufficient): dedicated
host heartbeat.**

- Client opens a lightweight control channel (WS or periodic
  POST) separate from STT. The control channel renews the
  lease regardless of STT state.
- Provider bill can still be trimmed by closing STT on speech
  silence; the room stays live because the control channel
  keeps the lease alive.
- Expands frontend scope: `useSubtitleSocket` and the host
  console both need to know about this channel.

**F-22 tests the ACTUAL client behavior.** The test simulates
a real capture-mode scenario during silence — including capture
modes that send no audio during pauses — and asserts one of:
(a) with the chosen client mode, the STT WS receives enough
non-audio activity to keep renewals firing across the silence,
demonstrating presence without option A, OR (b) the test
demonstrates that option B is insufficient for the target
pause duration and gates option A. The test outcome, not a
timing calculation, determines which option ships.

**Reconciliation with §4.5 (STT idle watchdog):** existing
behavior is preserved. The watchdog still closes the STT WS
on speech silence at 120s. The lease design accepts this and
absorbs it via the (variable) reconnect grace above. No change
to the watchdog is part of this work.

#### Loss of lease is not proof of voluntary abandonment

A design principle to document rather than a mechanism: when the
detector terminates a room on lease expiry, that action is
correct *by policy*, not because the host has been proven absent.
The mechanism cannot distinguish:

- Host tab closed without End Service.
- Host laptop asleep on Wi-Fi.
- Host on a flaky network that can't reach Firestore for a while.
- Firestore itself was briefly unavailable to the host but
  reachable from the detector.

In all four cases the lease expires and the detector ends the
room. The policy is: **loss of a valid lease requires the old
host session to stop producing.** Specifically:

- On lease renewal failure (write returns any error), the STT
  handler retries with bounded backoff, and after the documented
  threshold closes the STT WS with the shutdown-reconnect code.
- On observing (via subsequent Firestore read or Redis terminal
  broadcast) that the lease has been terminated or replaced by
  another session, the STT handler closes the STT WS
  immediately.
- Any code path that could produce a translation or broadcast a
  message must be gated on the STT handler being alive; the STT
  handler being alive requires an unfailed most-recent renewal.
- Callers of `broadcast_room` from the STT path must not
  survive past their handler's lease loss. This is enforced by
  the handler being the only caller of those broadcasts and by
  it closing the WS as soon as renewal fails.

This is the "old session must stop producing" rule the reviewer
called out. It cannot be inferred from the lease design alone;
it must be enforced by STT-handler code and covered by tests.

#### Rollout for pre-lease live rooms

At the moment the abandonment detector deploys, there may be
live rooms in Firestore that were created before the lease
fields existed. Those rooms have no `hostLeaseExpires`,
`hostSessionId`, or `hostLeaseGeneration`.

- **Grandfather rule:** a room with missing lease fields is
  treated as if it holds a lease that never expires. The
  abandonment detector's Firestore query filters these out (see
  Detection flow below). They fall back to the existing 15-min
  idle_timeout / max_duration sweeper, exactly as today.
- **Post-deploy rooms** get the short abandonment deadline.
- **Migration is not required.** The grandfathering window
  naturally closes as pre-lease rooms end via the existing
  sweeper. No backfill write.
- **STT handlers deployed with lease support** attempt to
  acquire a fresh lease on a room they connect to; if the room
  predates the deploy and has no lease fields, the STT handler
  writes the fields on first renewal. This is a one-way upgrade
  per room — safe because the acquisition is conditional on no
  prior session_id / generation.

#### Detection flow

The detector polls at a configurable interval (proposed 30s).

**Discovery must not depend on a surviving host handler.** v3
proposed limiting discovery to "rooms with a local STT handler or
sweeper-flagged rooms." That misses the exact case the short
abandonment deadline exists to catch: instance A owns the host,
A crashes, surviving instance B has only listeners for the room
and no STT handler — B's detector would never see the room.

**Instead:** any backend instance may run the discovery query.
The query is a bounded Firestore collection-group read on
`rooms`:

```
collection group: rooms
where status == "live"
where hostLeaseExpires < cutoff
order by hostLeaseExpires asc
start after cursor      # cursor-based pagination, see below
limit N
```

Firestore semantics eliminate the need for an explicit "field
exists" filter: an ORDER BY / range filter on `hostLeaseExpires`
already excludes documents missing that field. Pre-lease rooms
are grandfathered automatically. (v4's `hostSessionId != null`
filter was withdrawn — `!=` against `null` is not a reliable
"field exists" test in Firestore.)

**Required Firestore index:** collection-group scope on `rooms`,
composite `(status ASC, hostLeaseExpires ASC)`. Committed to
`backend/firestore/firestore.indexes.json`, deployed via
`firebase deploy --only firestore:indexes`, waited-until-ready
before the detector is enabled. The abandonment-detector deploy
step must include a check that queries the index once and
confirms the response before flipping the config flag.

**Cursor-based pagination, not repeated first-N.** Each pass
starts from the cursor emitted by the previous pass (or the
epoch for the first pass) and reads N rows in
`hostLeaseExpires ASC` order. This prevents a malformed or
repeatedly-failing early row from starving later expired rooms
— a bug that repeated `LIMIT N` from the top would introduce.

**Coordination across instances.** Every instance runs the
discovery. Two instances may see the same expired room and race
to terminate it. The atomic recheck (property 4 + transaction
validation block) rejects the loser's write. Duplicate reads
are wasted Firestore quota, not a correctness issue. If quota
becomes a concern, a lightweight lease on the *discovery task
itself* (leader election on a Firestore doc with a short TTL)
can gate the query to one instance at a time — later
optimization.

**What if no backend instance is running?** No detection while
the tier is down. Rooms accumulate as `status=live` with
expired leases. On next boot, the abandonment detector's first
pass runs the same query and terminates them (subject to an
instance actually running to run the pass). The startup-safety
fix in §4a.1 does *not* itself detect expired leases — v4's
grandfathering rule made pre-lease rooms invisible to the
startup path, and post-lease rooms are properly ended only by
the abandonment detector's global query.

**For each returned doc** (`hostLeaseExpires < cutoff`), the
detector attempts atomic `end_room` per property 4. If the
transaction succeeds, external cleanup effects fire (see
§4a.2). If it fails (lease was renewed or replaced between
read and transaction body), the detector moves on to the next
row.

**Grace period is baked into the lease TTL**, not layered on
top. Starting values proposed for staging: **90s TTL with
renewal every 20s** under healthy conditions. These are test
candidates, not proven optima. Tune once observability lands
and once real host-reconnect / silent-period distributions are
measured.

#### Module placement

Policy and lease operations live in
`app/services/abandonment_detector.py`. Scheduling either
piggybacks on the existing sweeper coordinator (if the sweeper's
cadence and non-blocking guarantees fit; today the sweeper
`await asyncio.sleep(max(15, ROOM_SWEEPER_INTERVAL_SEC))` — a
30s abandonment cadence would require a distinct schedule) or
runs as its own managed task. Independent-loop-vs-piggyback is
implementation-level; testability comes from separating policy,
storage, and cleanup, not from creating another background loop.

#### What this closes

- G-1 (host abandonment). Bounded by the lease TTL + one
  detector interval + one Firestore transaction round-trip.
- G-3 partial (better listener UX when STT idle-watchdog closes
  the provider). The listener sees a terminal event after lease
  expiry rather than after 15 minutes.

#### What this does NOT close

- G-5. Firestore says ended in G-5; abandonment doesn't apply.

### 5.1c Rejected alternatives

- **Per-socket last-seen heartbeat.** Requires a new heartbeat
  broadcast protocol; larger surface. §5.1a covers G-5 without
  it.
- **Redis Streams replay on subscriber reconnect.** Closes G-5
  only; nothing else. Redundant with §5.1a.
- **`lastAudioAt`-only abandonment.** Unsafe for worship
  services during silent periods (justified above).

## 6. Failure test matrix

Tests that make the "finished" definition credible. Each row is
one integration test. **Detection time and cleanup time are
recorded separately.**

- **[R]** requires real Redis (docker-compose or Memorystore).
- **[E]** requires a Firestore emulator.
- **[2P]** requires two backend processes to demonstrate
  cross-instance behavior.

Tests are grouped by the change that provides their behavior.
Rollout gate ordering is in §9.

### Group A — existing behavior, no code change required

| # | Scenario | Assertions |
|---|---|---|
| F-1 | End Service (baseline) | server-side: `connections_by_room` empty; `host_presence_*` empty; Redis refcount 0; Firestore status=ended; provider close completed OR timed out at 3s with logged outcome; STT background tasks joined; ownership map entries removed. Client-side (only where the network is intact): each ws received a close frame — this is not asserted where the network is broken |
| F-4 | Trial cap crossed | sweeper runs cap enforcement within ROOM_SWEEPER_INTERVAL_SEC + one Firestore read; room terminated via End Service path; all Group-A assertions hold |
| F-5 | Monthly cap crossed | same as F-4 with different reason |
| F-6 | Redis disconnect during broadcast | `ensure_subscription` returns not-ready on reconnect; existing owners keep refcount; new joins refuse with `listener_subscription_not_ready` until ready; refcount stays balanced |
| F-11 | Rapid start/end/start/end (100 cycles same service URL) | resource-count gauges return to baseline after each cycle; no owner-map monotonic growth over 100 cycles; provider close success rate = 100%; no orphaned tombstones past 5-min TTL |
| F-12 | End Service arriving while listener is mid-join | listener either doesn't join or immediately receives close(1000, room_ended); no dangling refcount; no `listener_subscription_owned_room_by_ws` entry left |
| F-13 | End Service while host STT is mid-provider-connect | provider close attempt logged (success or timeout); no orphan Deepgram/OpenAI/Gemini task lingering |

### Group B — provided by §4a.1 fix (startup safety)

| # | Scenario | Assertions | Env |
|---|---|---|---|
| F-15 | Instance B starts while A is actively broadcasting | A's room stays `status=live`; A's listeners see no terminal event; A's host connection is undisturbed; B has zero references to A's room | **[2P]** — merge-blocker for multi-instance |

### Group C — provided by §4a.2 fix (atomic conditional transition)

Two variants of the same race pattern, one per condition source.
F-23 exercises the mechanism against `lastAudioAt` and belongs to
Track 1 (Redis cleanup work). F-17 exercises it against the lease
and belongs to Track 2 (introduces lease fields, deferred).

| # | Scenario | Assertions | Env | Track |
|---|---|---|---|---|
| F-23 | Sweeper stale-`lastAudioAt` race — split into TWO evidence pieces per audit §4a.2 clarification. **F-23a (emulator):** activity committed BEFORE the termination transaction opens is respected — the transaction reads the fresh `lastAudioAt` and returns `skipped`. Scoped narrowly; does NOT claim to reproduce Firestore's production optimistic-concurrency retry (the emulator uses simplified locking per Google's docs). **F-23b (controlled retry, no emulator):** patched transactional driver forces the callback to run at least twice with different reads; assert eligibility rechecked on each rerun, callback body has no external side effects (AST). Together these are the reviewer's two distinguished pieces. See PR-T1-B in `docs/01-plan/features/resource-cleanup-track-1.plan.md` | **[E]** (F-23a) / doubles (F-23b) | **1** |
| F-17 | Renewal-vs-expiration race (lease variant): detector reads expired lease at t=100; host renews lease at t=101; detector attempts `end_room` at t=102 | transaction detects lease renewal, abandons the write; room stays `status=live`; host session unaffected; detector metric records the abandoned attempt | **[E]** | **2** |

### Group D — provided by §5.1a reconciler

| # | Scenario | Assertions | Env |
|---|---|---|---|
| F-8 | Missed terminal broadcast (Redis publish succeeds, one subscriber dropped) | reconciler on the affected instance discovers Firestore=ended within interval + Firestore read latency; local cleanup completes; no false termination of any other room | **[R] [2P]** |
| F-14 | Firestore read failure during reconciler tick | reconciler does not treat outage as "room ended"; no false terminations; pass logged as failed via `reconciler_tick_total{outcome=firestore_error}`; next tick retries once Firestore recovers | **[E]** |
| F-16 | Delayed cleanup for room A must not affect replacement room B (same service URL) | after A ends, room B opens under the same slug/serviceKey; A's delayed cleanup does not touch B's `connections_by_room`, `host_presence_by_ws`, or provider connection | doubles |
| F-18 | Stuck cleanup for a terminal room | after Firestore reflects `status=ended` for room X, a cleanup operation on X hangs. `terminal_rooms_with_resources` becomes ≥ 1 and stays ≥ 1 past the deadline; `oldest_overdue_cleanup_seconds` grows past its threshold; alert fires. Assertion is scoped to Firestore-terminal rooms — a healthy long room does NOT trigger this signal (see F-21) | doubles + metric harness |
| F-21 | Healthy long-running room — no false overdue alert | a room stays `status=live` for hours with active broadcasts. `terminal_rooms_with_resources` stays 0; `oldest_overdue_cleanup_seconds` stays 0. No alert fires under this signal, even though `connections_by_room_size` and `locally_owned_rooms_size` remain non-zero | doubles + metric harness |

### Group E — provided by §5.1b abandonment detector

| # | Scenario | Assertions | Env |
|---|---|---|---|
| F-2 | Host closes tab | provider close attempt logged (success or timeout — bound is on the *attempt*); detection time and cleanup time recorded separately; abandonment detector flips Firestore to ended within lease-TTL + interval + transaction round-trip; §5.1a reconciler then cleans local state on any other instance | doubles |
| F-3 | Host WS killed mid-message (no close frame) | same as F-2, plus no half-buffered Firestore writes; STT tasks cancelled cleanly | doubles |
| F-10 | Instance crash (no graceful shutdown) | surviving instance's abandonment detector runs the global expired-lease query, discovers the crashed room, atomically flips Firestore to ended; §5.1a reconciler on any surviving instance then completes local cleanup. **Detection deadline = lease TTL + one detector interval + Firestore transaction round-trip**, independent of whether the surviving instance has any local resources for that room | **[R] [2P]** |
| F-19 | Host-crash listener-only discovery: instance A owns the host STT for room X, instance B owns only listeners for room X. A crashes without graceful shutdown. Room X has no local STT handler anywhere. | B's abandonment detector (or C's, or any instance's) discovers X via the global expired-lease query, terminates within the detection deadline. Discovery is NOT limited to instances with a local STT handler. **This test would fail against a discovery flow that only inspected local STT-owning rooms — it exists to lock in the global-query requirement.** | **[R] [2P] [E]** |
| F-20 | Asymmetric Firestore failure with in-flight translation: host STT handler cannot write lease renewals; detector's reads succeed; a translation call was started at t=T0 while ownership was still valid and returns at t=T1 *after* renewal has failed past threshold. | Host STT retries with bounded backoff, hits documented threshold (30s), closes STT WS with the shutdown-reconnect code. Detector terminates the room via atomic transaction. The in-flight translation returning at T1 goes through the publication path and **is rejected at the ownership check** — no `broadcast_room` call fires for it, no Firestore segment append happens, no TTS emission. This locks in the "handler alive ≠ still owns room" rule from §5.1b property 6 case 4. Documented as correct by policy even though indistinguishable from a healthy host on a Firestore partition | **[E]** |
| F-22 | Actual client silent-pause behavior: run the test against the real frontend capture code path. Start a broadcast; enter a silent period during which no speech transcripts arrive. Observe whether the STT WS receives enough non-audio activity (client heartbeats, keepalives, etc.) to keep lease renewals firing across the silence. | Two acceptable outcomes: **(a)** the STT WS remains active enough during silence that lease renewals continue and no abandonment fires — test passes, option B is sufficient; **(b)** the STT WS goes quiet enough that lease renewals stop before the target silent-pause window is safe — test FAILS option B and gates option A (dedicated heartbeat channel) as required. The test outcome, not a timing calculation, determines which option ships. Must exercise capture modes that send no audio during pauses if the client supports any such mode | doubles + **[E]** + real client bundle |

### Group F — provided by §4.6 SIGTERM fix

| # | Scenario | Assertions | Env |
|---|---|---|---|
| F-9 | Instance SIGTERM with active rooms (full server shutdown, not `_on_shutdown()` in isolation) | drain window enforced (10s shared deadline, bounded concurrency); sweeper cancelled; pubsub stopped; provider close *attempts* run (timeout outcomes logged); client WSs receive an infrastructure-reconnect close code (**not** `room_ended`); Firestore untouched; clients reconnect against `/resolve` and land on the surviving instance which continues serving | **[2P]** |

### Group G — provided by real Redis

| # | Scenario | Assertions | Env |
|---|---|---|---|
| F-7 | Redis reconnect race | reader loop resubscribes; refcount stays balanced across the disconnect/reconnect boundary; no loss of messages published *after* resubscribe completes; instance IDs recorded so we can prove traffic actually crossed instances | **[R] [2P]** |
| F-24 | End-room during Redis outage, then recover: room X is live with a real Redis subscription; kill Redis; End Service for X fires while Redis is down; then restart Redis. During outage: local `close_room_listeners` / `close_room_hosts` / `forget_room` run to completion; local owner maps for X return to baseline. After Redis recovery: reader loop resubscribes; X's channel is **NOT** re-subscribed (the local ownership map no longer holds it); a separate still-live room Y's channel **IS** resubscribed and receives fresh messages; refcount ledger reflects only Y | **[R]** — Track 1 |

## 7. Resource-evidence language

Test assertions must be specific enough that a passing test means
something.

- ❌ "Provider `.close()` called."
- ✅ "Provider close completed successfully OR timed out at 3s
  with logged outcome; the STT handler's background tasks
  (`consumer`, `producer`, `idle_watchdog`) were cancelled and
  joined via `asyncio.gather(..., return_exceptions=True)`;
  `host_presence_by_ws` no longer contains this ws;
  `host_subscription_owned_by_ws` no longer contains this ws. A
  logged timeout does **not** assert the remote provider session
  stopped billing — that is beyond what our tests can prove
  without provider-side observation."

- ❌ "Listeners disconnected."
- ✅ "Every ws in `connections_by_room[(org,room)]` at test start
  received a WebSocket close frame **if the network was intact
  for the duration of the test**; final `connections_by_room` has
  no key for `(org,room)`; final
  `listener_subscription_owned_room_by_ws` contains no entry with
  value `(org,room)`. Where the test deliberately breaks the
  network (F-3, F-10), the close-frame assertion is dropped and
  the assertion is on server-side owner-map state alone."

- ❌ "Resources released."
- ✅ "Owner-map cardinality returned to baseline; Redis
  subscription refcount for the channel returned to zero; over
  100 repeated cycles, none of these values grew monotonically."

- ❌ "Reconciler ran cleanly."
- ✅ "Reconciler emitted a tick log within interval + jitter;
  `reconciler_actions_total{reason=ended_room_local_cleanup}`
  incremented only where a *confirmed-terminal* Firestore doc
  had local resources; no action was emitted for a room whose
  Firestore state was `live`, missing, malformed, or unreadable."

## 8. Observability signals

Regression tests prove code correctness against mocks; runtime
metrics prove the bound holds in production. Names below are
illustrative — actual exposition mechanism TBD.

| Metric | Type | What it detects |
|---|---|---|
| `connections_by_room_size` | gauge, per instance | listener leak |
| `host_presence_by_ws_size` | gauge, per instance | host tracking leak |
| `host_shutdown_cb_by_ws_size` | gauge, per instance | STT callback leak |
| `redis_subscription_refcount` | gauge, per channel | subscription leak |
| `locally_owned_rooms_size` | gauge, per instance | reconciler input size. **NOT an overdue-cleanup signal** — a healthy long broadcast keeps this non-zero (see F-21). Use only for baseline-leak alerts (monotonic growth over hours) |
| `terminal_rooms_with_resources` | gauge, per instance | count of rooms where local resources exist AND the last Firestore read shows a terminal state. This is the correctly-scoped signal for stuck cleanup; a healthy long broadcast contributes 0 here. F-18 alert fires on this metric being ≥ 1 past the deadline |
| `oldest_overdue_cleanup_seconds` | gauge, per instance | age of the oldest terminal-with-resources room, measured from Firestore `endedAt` (or from the moment a cleanup was *requested*, if that predates the Firestore write). 0 when `terminal_rooms_with_resources` is 0. Alert threshold is the acceptance deadline (proposed 60s) |
| `cleanup_inflight` | gauge, per instance | currently running cleanup operations. Useful for distinguishing "cleanup is stuck" from "cleanup was never attempted" — the former shows inflight > 0 while `oldest_overdue_cleanup_seconds` grows; the latter shows inflight = 0 while the overdue gauge grows |
| `last_successful_reconciliation_at` | gauge (timestamp), per instance | wall-clock time of the last completed reconciliation pass. Absence-of-tick alert: if this stops advancing for > 3 × interval, the reconciler is wedged. Complements per-tick outcome counters, which are silent when the reconciler stops running entirely |
| `reconciler_tick_total{outcome}` | counter | outcome ∈ {ok, firestore_error, skipped_overlap}; distinguishes healthy ticks from failure modes |
| `reconciler_actions_total{reason}` | counter | reason ∈ {ended_room_local_cleanup}. **Not a rollback trigger.** During injected-failure tests (F-8, F-10, F-19) this rising is evidence of successful recovery |
| `reconciler_lag_seconds` | histogram | wall-clock lag between Firestore `endedAt` and local cleanup completing. **Only records on completion — insufficient by itself.** Overdue detection uses `terminal_rooms_with_resources` + `oldest_overdue_cleanup_seconds`, which do not depend on a completion event ever firing |
| `abandonment_lease_renewals_total{outcome}` | counter | outcome ∈ {ok, no_change, replaced, firestore_error}; measures lease health |
| `abandonment_detector_actions_total{reason}` | counter | reason ∈ {lease_expired}; how often abandonment fired |
| `abandonment_detector_race_avoided_total` | counter | atomic recheck rejected a stale termination (F-17). A non-zero value here is normal under load; it proves the safety mechanism is doing its job |
| `stt_provider_close_total{provider,outcome}` | counter | outcome ∈ {clean, timeout, error}. Clean rate degrading is an alert; timeout does not assert remote-side stop |
| `sigterm_client_close_total{code}` | counter | sanity check that shutdown closes go out with the reconnect-hint code, not `room_ended` |

**Alert semantics:**
- `reconciler_actions_total` rising *during normal operation* →
  alert. Primary paths are silently failing. Rising during
  deliberately injected failures is *expected*.
- `reconciler_lag_seconds` p99 exceeding acceptance target →
  alert.
- `terminal_rooms_with_resources` ≥ 1 sustained past the
  deadline, or `oldest_overdue_cleanup_seconds` above the
  acceptance target → alert. This catches stuck cleanup
  independently of whether a completion event ever fires. A
  healthy long broadcast does NOT trigger this alert (F-21).
- `last_successful_reconciliation_at` not advancing for > 3 ×
  interval → alert. Reconciler wedged.
- `abandonment_detector_actions_total` rate spike outside expected
  usage → investigate.
- `abandonment_lease_renewals_total{outcome!="ok"}` rising →
  investigate before it becomes a false termination.
- `stt_provider_close_total{outcome!="clean"}` rising → alert.
- Any *_size gauge growing monotonically over hours → alert
  (baseline leak, unrelated to termination).

Rollback triggers are the alert categories above, **never** the
mere fact that a recovery mechanism ran.

## 9. Rollout plan

Ordered gates. Each gate must pass before proceeding. Tests are
assigned to the step that provides their behavior.

1. **Current audit adopted (this document, v5 or its successor).**
   Bounds / intervals / gap wording finalized. Track 1 vs.
   Track 2 split acknowledged (§9a).
2. **§4a.1 fix landed.** Unconditional startup-termination removed
   (or replaced with abandonment-evidence-required termination).
   **F-15 passes.** F-15 gates this step specifically; it does
   not gate every unrelated deployment.
3. **§4a.2 atomic conditional transition landed (sweeper variant).**
   Firestore transaction pattern with pure Firestore-only
   callbacks. Applied to the sweeper's idle-timeout path against
   `lastAudioAt`; **not** applied to explicit End Service,
   duration limits, or cap enforcement — those already have
   authoritative signals and must not be gated on a
   `lastAudioAt` recheck. **F-23a (emulator) passes** — activity
   committed before the transaction opens is respected. **F-23b
   (controlled retry, deterministic) passes** — callback rerun
   rechecks eligibility, callback body has no external side
   effects. **Emulator validation is PENDING until CI actually
   executes the emulator job successfully with no unexpected
   skips.** This step is Track 1 and does not require lease
   fields. Track 2 later extends the same pattern to the lease
   as **§4a.2 + §5.1b lease foundation** with F-17.
4. **§5.1a reconciler landed** behind a config flag, default off.
   Explicit terminal-state predicate, ownership inventory
   covering all owner maps (including STT tasks). Observability
   signals emitting, including `terminal_rooms_with_resources`,
   `oldest_overdue_cleanup_seconds`, `cleanup_inflight`,
   `last_successful_reconciliation_at`. **Group D tests (F-8,
   F-14, F-16, F-18, F-21) pass. F-24 (Redis outage + recovery)
   is the Track 1 integration gate and must pass before
   completion.**
5. **§5.1b abandonment detector landed.** Lease renewal wired
   into every STT handler with alive-connection evidence; lease
   TTL and renewal cadence configurable (starting values 90s /
   20s); detector loop uses the §4a.2 atomic transition; global
   Firestore query on the composite index; **`STT_NO_SPEECH_TIMEOUT_SEC`
   remains at 120s** (no watchdog change proposed by this work —
   see §5.1b); grandfathering path exercised;
   asymmetric-Firestore policy enforced. **Group E tests (F-2,
   F-3, F-10, F-19, F-20, F-22) pass.**
6. **§4.6 SIGTERM semantics implemented.** Distinct close code
   from `room_ended`; client-side handling in `useSubtitleSocket`.
   **F-9 passes.**
7. **Isolated multi-instance test environment.** docker-compose
   with real Redis + two uvicorn workers + Firestore emulator.
   Full failure matrix runs end-to-end with instance IDs recorded
   so tests can prove the traffic actually crossed instances.
   **Group G (F-7) passes; F-8, F-9, F-10, F-15, F-19 pass in the
   multi-instance environment.**
8. **Enable reconciler + abandonment detector on production**,
   still `REDIS_ENABLED=0`, single-instance. Confirm
   `reconciler_actions_total` is near-zero during normal ops (if
   not, primary paths are broken — debug before proceeding).
   Confirm `abandonment_detector_actions_total` matches expected
   patterns. Confirm no false terminations against real host
   sessions.
9. **Staging with real Memorystore.** Same test matrix on real
   network conditions and real Redis authentication.
10. **Redis-enabled single-instance production.** Set
    `REDIS_ENABLED=1` while keeping `--max-instances=1`. This is
    a valid, useful staged-rollout state — validates the
    reconciler and pub/sub against production traffic before
    scaling.
11. **Multi-instance production.** Raise `--max-instances`.
    Deploy YAML gains a check-run that **fails the deploy if
    `REDIS_ENABLED=0` while `--max-instances > 1`** (silent
    cross-instance broadcast drop). `REDIS_ENABLED=1` with
    `--max-instances=1` is explicitly allowed (step 10 is that
    state). **Full failure matrix, including F-15 and F-19,
    re-runs green** before this step ships. Observability
    dashboards live from day one. Rollback triggers as defined
    in §8 — reconciler activity is not one of them.

## 9a. Implementation tracks

The work in §9 is not all blocked on the same open questions.
It splits into two tracks that can run in parallel; **Track 2
does not delay Track 1**. Redis leaks and cross-instance
recovery are in Track 1 — the lease-related items that still
have open design questions are in Track 2.

### Track 1 — ready to start

Design is settled; corresponds to §9 steps 1, 2, 4, 6, 7 (in
part), 8 (in part).

- **§4a.1 startup safety.** Remove the unconditional
  startup-termination path. **F-15 must pass** before merging
  this fix.
- **§5.1a Redis-independent ended-room reconciler.** Explicit
  terminal-state predicate, ownership inventory covering all
  owner maps (including STT background tasks), pure Firestore
  reads, idempotent local cleanup. **F-8, F-14, F-16, F-18,
  F-21 must pass.** This closes G-5 (missed terminal broadcast
  recovery) — the Redis-cleanup work the audit exists to ship.
- **§4a.2 atomic conditional termination (sweeper variant) +
  transaction-callback purity + cleanup idempotence.** Applied to
  the sweeper's idle-timeout path against `lastAudioAt`; explicit
  End Service, `ROOM_MAX_DURATION_SEC`, and cap enforcement paths
  are NOT re-gated on a `lastAudioAt` recheck (those have their
  own authoritative signals). No lease fields introduced.
  **F-23 and the transaction-retry test must pass.** Same
  transaction pattern is later extended to the lease in Track 2
  (F-17).
- **§4.6 SIGTERM semantics.** Distinct close code from
  `room_ended`; client-side handling in `useSubtitleSocket`.
  **F-9 must pass.**
- **Overdue monitoring.** `terminal_rooms_with_resources`,
  `oldest_overdue_cleanup_seconds`, `cleanup_inflight`,
  `last_successful_reconciliation_at` metrics wired.
- **Redis-independent subscription/resource cleanup.** Any
  Redis-specific cleanup that follows from a confirmed room
  termination — release refcount, drop subscription, prune
  owner maps — runs the same way regardless of whether Redis
  itself is currently reachable. If Redis is down, the local
  bookkeeping still gets cleaned; re-subscribe on Redis
  recovery handles the future.

Track 1 exit criteria: the "released within a bounded time
after confirmed termination" half of the "finished" definition
holds. Abandonment detection is not yet in scope; §3.2 G-1
still uses the 15-min idle sweeper as its fallback.

### Track 2 — hold pending design corrections

- **§5.1b lease-based abandonment.** Requires:
  - F-22 result deciding option B vs. option A. Option A
    expands frontend scope; the decision must be made before
    lease implementation.
  - Composite Firestore index deployed and ready.
  - Lease TTL / renewal cadence measured against real
    silent-period and reconnect distributions from Track 1
    telemetry.
- **Any watchdog behavior change.** No change proposed at this
  time. If F-22 forces option A, the watchdog can stay at 120s.
- **Multi-instance production rollout** (§9 steps 10 and 11).
  Requires both Track 1 and Track 2 complete plus F-19 passing
  in a real multi-instance test environment.

Track 2 exit criteria: the "abandonment-detection" half of
"finished" is defined and validated with real client behavior,
and multi-instance rollout is safe.

## 10. Explicit non-goals

- Rewriting `_room_sweeper_loop`. It remains the coarse safety net
  for `ROOM_IDLE_TIMEOUT_SEC` / `ROOM_MAX_DURATION_SEC`. §5.1b
  layers a faster, evidence-based deadline on top.
- Flipping `ROOM_HOST_PRESENCE_END_ROOMS`. That knob predates the
  lease and is superseded by §5.1b.
- Changing Firestore schema beyond the lease fields (`hostLeaseExpires`,
  `hostSessionId`, `hostLeaseGeneration`) required by §5.1b.
- Frontend changes beyond the §4.6 close-code handling.
- Cost optimization of Redis subscription strategy at current
  scale.

## 11. Open questions

1. **Reconciliation and detector intervals.** 30s each is a
   starting proposal. Tune against `reconciler_lag_seconds` and
   `abandonment_detector_actions_total` distribution once
   observability lands.
2. **Lease TTL and renewal cadence.** Starting staging proposal:
   **90s TTL, renewal every 20s** under healthy conditions. These
   are test candidates, not proven optima. Justify with measured
   distributions of legitimate silent periods and host reconnect
   times before tightening. Under Firestore write latency spikes,
   the renewal cadence may need to relax. If lease renewal is
   ever batched, per-row `(session_id, generation)` conditional
   checks must be preserved (see §5.1b property 5); otherwise
   don't batch.
3. **Test doubles vs. emulator boundary.** Doubles for CI on
   every PR (fast). Emulator + docker-compose Redis for a
   nightly integration run and pre-merge on any
   reconciler/detector/sweeper change. Rows in §6 tagged **[E]**
   or **[R]** or **[2P]** require the heavier environment.
4. **Whether abandonment detector piggybacks on the sweeper's
   coordinator or runs its own task.** Piggybacking simplifies
   scheduling but risks starvation if a sweeper pass runs long.
   Independent task adds one background loop but keeps cadences
   independent. Decide during implementation, driven by the
   sweeper's measured tick time.

## Related documents

- `docs/02-design/features/redis-pubsub-fanout.design.md` — pub/sub design.
- `docs/03-analysis/redis-pubsub-cloudrun-runbook.md` — deploy-time notes.
- `docs/03-analysis/redis-pubsub-smoke.md` — smoke test procedure.

## Changelog

- **v5** — Watchdog co-tuning proposal withdrawn (existing 120s
  behavior preserved). Firestore discovery query corrected —
  removed unreliable `hostSessionId != null` filter, added
  cursor-based pagination to prevent early-record starvation,
  named the required collection-group composite index.
  In-transaction validation block now enumerates all recheck
  fields (`status`, `hostSessionId`, `hostLeaseGeneration`,
  `hostLeaseExpires` type + freshness, room path). Lease-loss
  rule consolidated into one non-contradictory four-part
  statement (transient retry / confirmed replacement / expiry
  or threshold / in-flight publish check). Transaction-callback
  purity distinguished from cleanup idempotence — exactly-once
  per commit is a within-transaction property; cleanup remains
  idempotent so a mid-cleanup process death is safely recovered
  by the reconciler. F-20 extended with the in-flight
  translation case. F-22 rewritten to test actual client
  capture behavior (option B vs. option A decided by the test
  outcome). §11 TTL numbers and the "no instance running"
  paragraph fixed to match v4 staging values and remove the
  reference to the removed startup-cleanup recovery path. New
  §9a splits the work into Track 1 (Redis leaks and cleanup,
  ready to start) and Track 2 (lease-based abandonment and
  multi-instance scaling, held pending corrections).
- **v4** — §5.1b lease discovery made independent of surviving
  host-handler presence via a bounded global Firestore query on
  a composite index. Lease vs. STT idle watchdog reconciliation
  spelled out with option B (STT-close reconnect grace) adopted
  and option A (dedicated heartbeat) noted as fallback. Old-
  session-must-stop-producing policy documented, including
  asymmetric-Firestore-failure semantics. §4a.2 clarified that
  Firestore transaction callbacks are pure — external effects
  fire only after commit. Overdue-cleanup metric corrected from
  `locally_owned_rooms_size` to `terminal_rooms_with_resources`
  and `oldest_overdue_cleanup_seconds`. TTL / renewal starting
  values raised from 30s/10s to 90s/20s. Pre-lease-rooms
  rollout via grandfathering. Batched lease writes must
  preserve per-row conditional checks. F-15 gates the
  startup-safety fix and multi-instance rollout, not
  everything. F-10 moved to Group E. New tests F-19
  (listener-only discovery), F-20 (asymmetric Firestore
  failure), F-21 (healthy-long-room no-false-alert), F-22
  (silent worship period).
- **v3** — B-2 lease design spelled out with session+generation
  scope, alive-connection renewal, old-lease safety,
  atomic-recheck-on-termination, and failure-mode semantics.
  §4a.2 rewritten around stale-read races. §5.1a made
  Redis-independent, with explicit terminal-state predicate and
  provider/task ownership. §9 deploy guard corrected; gate
  ordering fixed. Tests F-17 (renewal race) and F-18
  (cleanup-never-completes monitoring) added. Assertions
  tightened. §4a.1 recommends removing unconditional startup
  termination. Source commit `e63c5d4b` recorded.
- **v2** — corrected v1 factual errors, split §5.1 into 5.1a and
  5.1b, added §4a blockers section, rewrote SIGTERM semantics.
- **v1** — initial draft.
