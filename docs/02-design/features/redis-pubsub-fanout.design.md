# Redis Pub/Sub Fanout — Design

**Feature:** `redis-pubsub-fanout`
**Goal:** Make Cloud Run backend safe to run at `--max-instances > 1` so we can host multiple churches concurrently without listeners dropping audio when they land on a different instance than the host.

**Branch:** `feature/redis-pubsub-fanout`

---

## 1. Problem

`app/socket_manager.py` holds `connections_by_room` in-process. `broadcast_room(org_id, room_id, msg)` iterates only local sockets. On Cloud Run with `--max-instances > 1`, load-balanced listeners on a different instance from the host receive nothing.

Cloud Run session affinity is *best-effort*, not guaranteed. External sync is required. Google recommends Memorystore for Redis + Redis Pub/Sub for this pattern.

## 2. Non-goals (this design)

- Persistence / replay of missed messages during listener reconnect — a Redis list + `lastSeq` catch-up will be a follow-up.
- Frontend seq-based dedup — added after the backend is proven stable in prod.
- Splitting into translator-service and listener-service microservices.
- Migrating global `manager.broadcast()` (null org/room) calls — these are legacy fallbacks, kept as local-only.

## 3. Chosen approach — adapter over ConnectionManager

The ConnectionManager is *already* room-keyed by `(org_id, room_id)`. Instead of rewriting every callsite in `main.py` (~15 `broadcast_room` calls), we change the internals:

```
Before:
  broadcast_room(org, room, msg)  ─→  local send to sockets in room

After (REDIS_ENABLED=0):
  broadcast_room(org, room, msg)  ─→  local send to sockets in room  (unchanged)

After (REDIS_ENABLED=1):
  broadcast_room(org, room, msg)  ─→  publish envelope to Redis channel
                                        (this instance is subscribed too)
                                        ↓
  redis subscriber callback       ─→  local send to sockets in room
```

Publish and local-send are **decoupled**. Under Redis mode, `broadcast_room` no longer sends locally itself — the subscriber does. No duplicates.

## 4. Redis message envelope

Channel name: `worshiptranslate:org:{orgId}:room:{roomId}`
(Prefix from `REDIS_CHANNEL_PREFIX`, default `worshiptranslate`.)

Payload (JSON):

```json
{
  "v": 1,
  "seq": 152,
  "publisher": "instance-<uuid>",
  "ts": "2026-09-08T20:21:00.123Z",
  "message": { ...original broadcast payload... }
}
```

- `seq`: monotonic per (org, room), from `INCR worshiptranslate:seq:{org}:{room}` (TTL 24h).
- `publisher`: this instance's UUID — for debugging; not used to filter (subscribers receive their own publishes and deliver locally).
- `message`: the exact object that used to be passed to `broadcast_room`, with an added `_rseq` field carrying the envelope's seq. We use `_rseq` (not `seq`) to avoid stomping on Shape 3's application-level `message.seq` (per-host-session counter used for React effect ordering in `useTranslationSocket`). The frontend uses `_rseq` for cross-instance dedup; app-level `seq` semantics are unchanged.

## 5. Subscription lifecycle (refcounted)

`ConnectionManager.join_room()` and `disconnect()` already know when a room's local socket set opens/closes. Hook into those:

- `join_room(org, room, role)` on an empty room → `await redis_pubsub.ensure_subscription(org, room)`
- `disconnect(ws)` when the room's local set becomes empty → `await redis_pubsub.release_subscription(org, room)`

State kept in `redis_pubsub`:

- `_ref_counts: dict[(org, room), int]`
- `_subscriber_tasks: dict[(org, room), asyncio.Task]`

Subscriber task loop: `psubscribe` on the channel; on each message decode JSON, look up local sockets, deliver. On disconnect from Redis, back off + reconnect + resubscribe to every known room key.

## 6. Deviations from the ChatGPT plan

| ChatGPT plan step | Our take |
|---|---|
| Rename all callsites `broadcast_room` → `publish_room` | Skip. Keep `broadcast_room` as the public API — change its internals. ~15 callsites unchanged. |
| Add seq stamping at every callsite | Skip. Stamp in the publish path once, as `_rseq` (see envelope). |
| Manual local ref-counter in ConnectionManager | Move to `redis_pubsub` module (single source of truth). |
| Frontend seq dedup as part of this feature | Included. `useTranslationSocket` drops any `_rseq <= last` on the current connection. Cheap under `max-instances=1`; protects future bump. |
| Replay buffer for reconnect | Deferred to follow-up. Note in code where the hook goes. |
| Split translator-service / listener-service | Not now. |

## 7. Env config

```
REDIS_ENABLED=0            # off by default; set to 1 in dev/staging, then prod
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=            # empty for local dev
REDIS_CHANNEL_PREFIX=worshiptranslate
REDIS_SEQ_TTL_SEC=86400    # seq counter TTL
REDIS_CONNECT_TIMEOUT_SEC=5
INSTANCE_ID=               # auto-generated UUID if empty
```

## 8. Failure modes

| Failure | Behavior |
|---|---|
| Redis unreachable at startup | Log error, fall back to local-only (as if `REDIS_ENABLED=0`). Retry connection in background. |
| Redis drops mid-run | Subscriber task catches, backs off 1s/2s/5s/10s, resubscribes to every known room. Publishes during outage are dropped (logged); we intentionally do NOT queue them locally — they'd fan out to a stale set of instances after reconnect. |
| Publish fails | Log + emit metric. Do not raise into the WS handler. |
| Subscriber decode fails | Log + skip; do not tear down the loop. |

### 8a. Post-mortem: subscription race dropped every delivery (2026-09-11)

**What happened.** With `REDIS_ENABLED=1` enabled in prod at `--max-instances=1`, both host and listener audio broke immediately. Setting `REDIS_ENABLED=0` restored service. Unit tests still passed.

**Root cause.** In the original `broadcast_room` under Redis mode, delivery flowed only through Redis:

```
broadcast_room
  → pubsub.publish_room  (publish to Redis; return)
  → Redis pubsub delivers back to same-instance subscriber
  → subscriber calls _broadcast_local_room  → sockets
```

The subscribe path was set up fire-and-forget from `join_room`:

```python
if new_room_first_ws:
    _schedule(pubsub.ensure_subscription(key[0], key[1]))
```

`_schedule` used `asyncio.create_task` — non-blocking. When a WS joined the room and moments later a broadcast fired, `SUBSCRIBE channel` had not yet reached Redis. Redis pub/sub is **not buffered**: messages published before a subscription is active are silently dropped. Every early broadcast landed on the floor. Because the host page's `useTranslationSocket` on `/ws/translate` is what drives its TTS, the host lost audio too — not just listeners.

**Why tests missed it.** `tests/test_redis_pubsub.py` manually `await`s `ensure_subscription()` before each publish. The fire-and-forget path from `join_room` was never exercised. The 2-process Docker smoke test in `docs/03-analysis/redis-pubsub-smoke.md` would have caught it — but was not executed before shipping.

**Fix (adopted).** Deliver locally *immediately* on the publisher instance, and publish to Redis in parallel for other instances. On the subscriber side, skip messages where `envelope.publisher == ENV.INSTANCE_ID` so the publisher instance doesn't double-deliver its own message.

```
broadcast_room (when pubsub connected):
  ├─ _broadcast_local_room   (immediate; no subscription dependence)
  └─ pubsub.publish_room     (parallel; fanout to other instances)

_dispatch (subscriber callback):
  if envelope["publisher"] == ENV.INSTANCE_ID: skip
```

This eliminates the race on the publisher side (`--max-instances=1` fully covered). For `--max-instances > 1`, a receiver-side subscription race still exists in theory (subscription-late-vs-publish-early on a DIFFERENT instance), but is much smaller because subscribe latency is measured in milliseconds and the pattern is only a problem for the very first message immediately after join. If it bites in the multi-instance era, the next follow-up is:

- **Option B (deferred):** make `ensure_subscription` awaitable from `join_room` — turns `join_room` async and updates callers. Fully eliminates the receiver-side race.
- **Option C (deferred):** switch fanout to Redis Streams instead of Pub/Sub. Streams buffer publishes, so subscribers can catch up on missed messages. Bigger change, no race even under adverse timing.

**Also:** `publish_room` now works on a shallow copy of the message dict rather than mutating the caller's dict, so the local-delivery path (which runs first) never sees a partially-stamped `_rseq`.

**Verification.** Add an integration test that: (a) joins a room via `manager.join_room` without awaiting subscription, (b) immediately publishes, (c) asserts the local WS bucket received the message. See `tests/test_redis_pubsub.py::ConnectionManagerFallbackTests::test_publisher_own_broadcast_delivers_without_race`.

## 9. Rollout plan

1. **Dev:** `REDIS_ENABLED=1` + local docker `redis:7`. Two uvicorn processes on 8080/8081; verify cross-instance delivery.
2. **Staging:** `REDIS_ENABLED=1` + Memorystore. `--max-instances=1` still. Verify no regression.
3. **Prod:** `REDIS_ENABLED=1` + Memorystore. `--max-instances=1` for one week. Then bump to 2 during a low-traffic window; monitor.
4. **Prod:** `--max-instances=3` once stable. Continue watching listener counts + Redis metrics.

## 10. Test plan

- **Unit** (`tests/test_redis_pubsub.py`): fakeredis-backed round-trip: publish → subscribe callback fires with same message + seq stamped. Refcount subscribe/unsubscribe. Seq monotonicity per room + isolation across rooms.
- **Manual smoke** (documented in `docs/03-analysis/redis-pubsub-smoke.md`): docker redis + 2 uvicorns; verify cross-instance broadcast.
- **Regression:** `REDIS_ENABLED=0` path (default) behaves identically to `main` today — one existing test asserts local broadcast still fires.

## 11. Files touched

- `backend/requirements.txt` — `redis>=5.0.0`
- `backend/app/env.py` — new Redis fields
- `backend/app/services/redis_pubsub.py` — new module (~200 lines)
- `backend/app/socket_manager.py` — adapter hooks in `join_room` / `disconnect` / `broadcast_room`
- `backend/app/main.py` — startup/shutdown wiring for the Redis client
- `backend/tests/test_redis_pubsub.py` — new
- `CLAUDE.md` — new env vars documented
- `docs/03-analysis/redis-pubsub-smoke.md` — manual test recipe

## 12. Out of scope for THIS PR

- Cloud Run deploy workflow bump (max-instances, session-affinity). Ship as a follow-up once Memorystore is provisioned.
- Memorystore Terraform / gcloud commands. Follow-up runbook.
- Frontend seq dedup + replay buffer.
