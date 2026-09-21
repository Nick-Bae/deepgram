# backend/app/services/redis_pubsub.py
"""Cross-instance broadcast fanout via Redis Pub/Sub.

Design: docs/02-design/features/redis-pubsub-fanout.design.md

Public API used by ConnectionManager:
    await pubsub.start()
    await pubsub.stop()
    await pubsub.publish_room(org_id, room_id, message)  -> stamped seq
    await pubsub.ensure_subscription(org_id, room_id, callback)
    await pubsub.release_subscription(org_id, room_id)

When ENV.REDIS_ENABLED is False, start() is a no-op and every call short-circuits
so callers can invoke unconditionally.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from app.env import ENV

log = logging.getLogger("redis_pubsub")

RoomKey = Tuple[str, str]
DeliveryCallback = Callable[[str, str, dict], Awaitable[None]]

_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0)
_ENVELOPE_VERSION = 1

# PR #31 §3 (W1) structured-event schema. Every adapter event now
# emits one JSON line on stdout via `_emit` so Cloud Run parses it
# into `jsonPayload` and log-based metrics can filter on
# `jsonPayload.event="..."` and extract `jsonPayload.instance_id` as
# a metric label — no more textPayload regex, no more dependence on
# whatever root-logger level uvicorn ends up applying. The `print`
# path bypasses `logging` entirely, which is deliberate (see PR #31
# §3 W1 and PR #32's F-27 harness log-level pitfall).

_EVENT_SCHEMA_VERSION = 1

# Canonical event names — used by metric filters and by tests that
# assert the adapter emits the right event for a given code path.
# Any change here MUST be paired with a metric-filter update in
# `ops/monitoring/reconciler/` per PR #31 §3.
EVENT_STARTED = "redis_pubsub_started"
EVENT_INITIAL_CONNECT_FAILED = "redis_pubsub_initial_connect_failed"
EVENT_RECONNECTING = "redis_pubsub_reconnecting"
EVENT_RECONNECTED = "redis_pubsub_reconnected"
EVENT_RECONNECT_FAILED = "redis_pubsub_reconnect_failed"
EVENT_READER_ERROR = "redis_pubsub_reader_error"
# Probe events (task #127) are declared here so the catalogue is
# complete but are NOT emitted by this PR — the probe task itself
# lands in the probe-integration branch.
EVENT_PROBE_OK = "redis_probe_ok"
EVENT_PROBE_FAILED = "redis_probe_failed"


def _emit(event: str, severity: str, **fields: Any) -> None:
    """Emit one structured-JSON operational event line to stdout.

    Fields always present: `event`, `severity`, `component`,
    `instance_id`, `schema_version`, `ts`. `message` is populated
    from a `message` kwarg when provided so F-27 and other consumers
    that grep for a human-readable substring keep working during the
    transition; metric filters key off `event` regardless.

    Uses `print` directly, not `logging`, so:
      1. The line reaches stdout regardless of the root logger level
         (the exact pitfall PR #32's F-27 hit under uvicorn's
         `logging.config.dictConfig`).
      2. Cloud Run's log-parser reliably sees a single JSON object
         per line, populating `jsonPayload.event` etc.

    `flush=True` because uvicorn workers can otherwise buffer stdout
    across a few seconds when a shutdown is in flight, and startup /
    reconnect events are exactly the ones an operator needs to see
    IMMEDIATELY during a window.
    """
    payload = {
        "event": event,
        "severity": severity,
        "component": "redis_pubsub",
        "instance_id": ENV.INSTANCE_ID,
        "schema_version": _EVENT_SCHEMA_VERSION,
        "ts": _iso_now(),
        **fields,
    }
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        file=sys.stdout,
        flush=True,
    )


def _channel_name(org_id: str, room_id: str) -> str:
    return f"{ENV.REDIS_CHANNEL_PREFIX}:org:{org_id}:room:{room_id}"


def _seq_key(org_id: str, room_id: str) -> str:
    return f"{ENV.REDIS_CHANNEL_PREFIX}:seq:{org_id}:{room_id}"


class RedisPubSub:
    """Refcounted async Redis Pub/Sub fanout.

    One publish client (`_pub`) is used for PUBLISH + INCR. One subscribe client
    (`_sub`) hosts a `pubsub()` object; each subscribed room runs a `psubscribe`
    style listener under a single reader task (`_reader_task`).
    """

    def __init__(self) -> None:
        self._enabled = bool(ENV.REDIS_ENABLED)
        self._started = False
        self._pub = None                       # redis.asyncio.Redis
        self._sub = None                       # redis.asyncio.Redis
        self._pubsub = None                    # PubSub object
        self._reader_task: Optional[asyncio.Task] = None
        self._ref_counts: Dict[RoomKey, int] = {}
        self._callback: Optional[DeliveryCallback] = None
        self._subscribed: set[RoomKey] = set()
        self._lock = asyncio.Lock()
        self._connected = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def connected(self) -> bool:
        return self._enabled and self._connected

    @property
    def desired_room_keys(self) -> set[RoomKey]:
        """Snapshot of room subscriptions this process intends to restore.

        The reader uses ``_ref_counts`` as its reconnect source of truth.  A
        stale entry there is therefore a real resource leak even when every
        websocket ownership map is already empty.
        """
        return {key for key, count in self._ref_counts.items() if count > 0}

    def set_delivery_callback(self, cb: DeliveryCallback) -> None:
        """Called by ConnectionManager to receive incoming subscribed messages."""
        self._callback = cb

    async def start(self) -> None:
        """Start the pub/sub adapter.

        Always schedules the reader task, even when the initial Redis
        connect fails. The reader loop's `_reconnect` path is the ONLY
        thing that can bring the subscriber up once Redis becomes
        reachable, so scheduling it unconditionally is required for
        startup-recovery.

        Before the fix, `_started = True` was set before `_pub.ping()`;
        a ping failure left `_started=True` with no reader task, and
        subsequent `start()` calls short-circuited at the enabled/started
        gate. The reconnect path became unreachable and the instance
        silently degraded to local-only broadcast even after Redis
        recovered. This pattern can produce a cross-instance isolation
        outcome similar to the one seen at Track 1 Gate 2 (issue #29),
        though the Gate 2 failure was not proven to originate here.

        The initial `_pub.ping()` is wrapped in `asyncio.wait_for` with
        `REDIS_COMMAND_TIMEOUT_SEC`. `socket_connect_timeout` bounds only
        the TCP connect; once the socket is up, a server that accepts
        connections but never replies to PING would otherwise hang start()
        indefinitely and defeat the reader-loop recovery this fix installs.
        """
        if not self._enabled or self._started:
            return
        try:
            import redis.asyncio as aioredis  # type: ignore
        except Exception as exc:
            log.error("redis package not installed; disabling pubsub: %s", exc)
            self._enabled = False
            return

        # Construct clients first. If instantiation itself fails
        # (unlikely — this is object creation, not I/O), leave
        # _started=False so a retry is possible.
        try:
            self._pub = aioredis.Redis(
                host=ENV.REDIS_HOST,
                port=ENV.REDIS_PORT,
                password=ENV.REDIS_PASSWORD or None,
                socket_connect_timeout=ENV.REDIS_CONNECT_TIMEOUT_SEC,
                decode_responses=True,
            )
            self._sub = aioredis.Redis(
                host=ENV.REDIS_HOST,
                port=ENV.REDIS_PORT,
                password=ENV.REDIS_PASSWORD or None,
                socket_connect_timeout=ENV.REDIS_CONNECT_TIMEOUT_SEC,
                decode_responses=True,
            )
            self._pubsub = self._sub.pubsub(ignore_subscribe_messages=True)
        except Exception as exc:
            log.error("redis client construction failed; disabling: %s", exc)
            # Clean up whatever partial state we managed to create.
            await self._teardown_clients()
            self._enabled = False
            return

        # Initial ping. Success → _connected=True and a "started" log
        # line for the metric. Failure → _connected=False + a specific
        # initial-failure log line the metric alerts on; the reader
        # loop will then repair via _reconnect.
        #
        # `asyncio.wait_for` is REQUIRED here. `socket_connect_timeout`
        # bounds only the TCP handshake; once the socket is up, PING
        # can hang forever if the server accepts connections but never
        # replies (a common failure mode when Redis is overloaded or
        # a proxy accepts TCP but blackholes commands). Without this
        # bound, the reader task would never be scheduled and the
        # whole recovery path this fix installs would remain unreachable.
        try:
            await asyncio.wait_for(
                self._pub.ping(),
                timeout=ENV.REDIS_COMMAND_TIMEOUT_SEC,
            )
            self._connected = True
            _emit(
                EVENT_STARTED,
                "INFO",
                host=ENV.REDIS_HOST,
                port=ENV.REDIS_PORT,
                prefix=ENV.REDIS_CHANNEL_PREFIX,
                message=(
                    f"redis pubsub started host={ENV.REDIS_HOST}:{ENV.REDIS_PORT} "
                    f"prefix={ENV.REDIS_CHANNEL_PREFIX} instance={ENV.INSTANCE_ID}"
                ),
            )
        except asyncio.TimeoutError:
            self._connected = False
            # Distinct `reason` field so the operator (and A5) can
            # tell TCP-accepted-but-hung apart from connection-refused.
            # Same `event` name so the metric/filter picks up both.
            _emit(
                EVENT_INITIAL_CONNECT_FAILED,
                "WARNING",
                reason="timeout",
                timeout_seconds=ENV.REDIS_COMMAND_TIMEOUT_SEC,
                message=(
                    f"redis pubsub initial connect failed: PING timed out after "
                    f"{ENV.REDIS_COMMAND_TIMEOUT_SEC:.2f}s "
                    f"(TCP accepted but no reply); reader loop will retry"
                ),
            )
        except Exception as exc:
            self._connected = False
            # `reason=refused` covers ConnectionError shape; anything
            # else surfaces via the message field for forensic
            # inspection (still under the same `event` name).
            _emit(
                EVENT_INITIAL_CONNECT_FAILED,
                "WARNING",
                reason="refused",
                error=repr(exc),
                message=(
                    f"redis pubsub initial connect failed; reader loop will retry: {exc}"
                ),
            )

        # ALWAYS schedule the reader loop. If _connected=False, the
        # loop's first iteration enters _reconnect and continues
        # attempting until Redis becomes reachable.
        self._started = True
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name="redis-pubsub-reader",
        )

    async def _teardown_clients(self) -> None:
        """Close and null out the pub/sub client objects. Idempotent."""
        for obj in (self._pubsub, self._sub, self._pub):
            if obj is None:
                continue
            try:
                closer = getattr(obj, "aclose", None) or getattr(obj, "close", None)
                if closer is not None:
                    res = closer()
                    if hasattr(res, "__await__"):
                        await res
            except Exception:
                pass
        self._pub = None
        self._sub = None
        self._pubsub = None

    async def stop(self) -> None:
        """Stop the adapter cleanly.

        Handles every intermediate state the fixed `start()` can leave
        behind:
          - `_started=True` with a running reader task (normal case)
          - `_started=True` with a reader task that hasn't yet observed
            the cancel (still normal)
          - `_started=False` with partially constructed clients but no
            reader task — this happens when client construction itself
            raised inside `start()`. The old `if not self._started:
            return` early-out at the top of `stop()` would have leaked
            those partial clients.
        """
        # Nothing to do only when we have neither the started flag nor
        # any lingering client objects.
        if not self._started and self._pub is None and self._sub is None and self._pubsub is None:
            return
        self._started = False
        self._connected = False
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._teardown_clients()
        self._reader_task = None
        self._subscribed.clear()
        self._ref_counts.clear()

    async def _next_seq(self, org_id: str, room_id: str) -> Optional[int]:
        if not self._connected or self._pub is None:
            return None
        try:
            key = _seq_key(org_id, room_id)
            seq = await self._pub.incr(key)
            # Refresh TTL each write so idle rooms let the counter expire.
            try:
                await self._pub.expire(key, ENV.REDIS_SEQ_TTL_SEC)
            except Exception:
                pass
            return int(seq)
        except Exception as exc:
            log.warning("redis INCR failed org=%s room=%s: %s", org_id, room_id, exc)
            return None

    async def publish_room(self, org_id: str, room_id: str, message: dict) -> Optional[int]:
        """Publish a message envelope; returns the assigned seq or None if not delivered.

        Works on a shallow copy of `message` — never mutates the caller's dict.
        This matters when the caller also delivers `message` locally (see
        ConnectionManager.broadcast_room's parallel path): a mutation here
        could race with json.dumps in the local send loop.
        """
        if not self._enabled or not self._connected or self._pub is None:
            return None
        seq = await self._next_seq(org_id, room_id)
        # Stamp the fanout-layer seq onto the payload under `_rseq` so the
        # frontend can dedup cross-instance duplicates without colliding with
        # any application-level `seq` field (Shape 3 messages already use
        # `message.seq` for per-host-session ordering — see main.py:1487).
        published_message = dict(message)
        if seq is not None:
            published_message["_rseq"] = seq
        envelope = {
            "v": _ENVELOPE_VERSION,
            "seq": seq,
            "publisher": ENV.INSTANCE_ID,
            "ts": _iso_now(),
            "message": published_message,
        }
        try:
            channel = _channel_name(org_id, room_id)
            await self._pub.publish(channel, json.dumps(envelope, ensure_ascii=False))
            return seq
        except Exception as exc:
            log.warning("redis PUBLISH failed org=%s room=%s: %s", org_id, room_id, exc)
            return None

    async def ensure_subscription(self, org_id: str, room_id: str) -> bool:
        """Refcount++ for (org, room); subscribe if this is the first local listener.

        Returns True when this instance is (or already was) actually
        subscribed to the room's channel. Returns False if the subscription
        is pending (Redis disconnected — reader will resubscribe on reconnect).
        Raises if the underlying SUBSCRIBE call fails; the refcount is rolled
        back so a caller retry is not double-counted, and _subscribed does not
        record a phantom membership.
        """
        if not self._enabled:
            return True
        key: RoomKey = (org_id, room_id)
        async with self._lock:
            previous = self._ref_counts.get(key, 0)
            self._ref_counts[key] = previous + 1
            if previous > 0:
                # Someone else already opened this subscription. Reflect the
                # actual state — False means Redis is currently disconnected;
                # the reader loop's reconnect will resubscribe.
                return self._connected and key in self._subscribed
            try:
                await self._subscribe_channel(key)
            except BaseException:
                # Roll back the refcount on ANY exception including
                # CancelledError. Without this, a task cancelled inside
                # ensure_subscription would leave refcount > 0, and the next
                # call would see previous > 0 and skip the actual SUBSCRIBE
                # forever. `except Exception` in modern Python does not catch
                # CancelledError.
                self._ref_counts.pop(key, None)
                # Also drop any phantom _subscribed entry — refuse to lie
                # about readiness on the next call.
                self._subscribed.discard(key)
                raise
            return self._connected and key in self._subscribed

    async def release_subscription(self, org_id: str, room_id: str) -> None:
        """Refcount--; unsubscribe when the room's local listener count drops to zero."""
        if not self._enabled:
            return
        key: RoomKey = (org_id, room_id)
        async with self._lock:
            count = self._ref_counts.get(key, 0) - 1
            if count > 0:
                self._ref_counts[key] = count
                return
            self._ref_counts.pop(key, None)
            await self._unsubscribe_channel(key)

    async def forget_room_subscription(self, org_id: str, room_id: str) -> None:
        """Remove every desired refcount for a confirmed-ended local room.

        Normal disconnects release one owner at a time.  The reconciler uses
        this stronger idempotent operation only after Firestore confirms the
        room is ended and local sockets have been closed.  Popping the desired
        key while Redis is down is what prevents reconnect from resurrecting
        an ended room's subscription.
        """
        key: RoomKey = (org_id, room_id)
        async with self._lock:
            self._ref_counts.pop(key, None)
            await self._unsubscribe_channel(key)

    async def _subscribe_channel(self, key: RoomKey) -> None:
        # _subscribed now strictly means "confirmed SUBSCRIBE succeeded on
        # the current live connection." Do NOT add here when disconnected —
        # that would let ensure_subscription report a false "ready" once
        # _connected flips true, even if the resubscribe later failed. The
        # reader loop's reconnect path walks _ref_counts.keys() (the set of
        # *desired* rooms) and only adds to _subscribed on actual success.
        if not self._connected or self._pubsub is None:
            return
        try:
            # Bounded wait: this call runs under _lock. Without the timeout,
            # a hung Redis connection would freeze every caller waiting on
            # _lock (listener/host registration, releases) until process
            # restart. On timeout, mark disconnected so the reader rebuilds.
            await asyncio.wait_for(
                self._pubsub.subscribe(_channel_name(*key)),
                timeout=ENV.REDIS_COMMAND_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            log.warning("redis SUBSCRIBE timeout key=%s", key)
            self._connected = False
            raise
        except Exception as exc:
            # Any subscribe failure (ConnectionError, OSError, redis error)
            # likely means the transport is broken. Mark disconnected so the
            # reader rebuilds; otherwise repeated ensure_subscription would
            # keep hitting the same dead client.
            log.warning("redis SUBSCRIBE failed key=%s: %s", key, exc)
            self._connected = False
            raise  # caller (ensure_subscription) rolls back refcount
        self._subscribed.add(key)

    async def _unsubscribe_channel(self, key: RoomKey) -> None:
        self._subscribed.discard(key)
        if not self._connected or self._pubsub is None:
            return
        try:
            await asyncio.wait_for(
                self._pubsub.unsubscribe(_channel_name(*key)),
                timeout=ENV.REDIS_COMMAND_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            log.warning("redis UNSUBSCRIBE timeout key=%s — marking disconnected", key)
            self._connected = False
        except Exception as exc:
            # Best-effort but still mark disconnected — a failed UNSUBSCRIBE
            # usually means the transport is unhealthy. Without this, the last
            # unsubscribe in an idle period could fail silently and the reader
            # would sleep forever waiting on nothing.
            log.warning("redis UNSUBSCRIBE failed key=%s: %s — marking disconnected", key, exc)
            self._connected = False

    async def _reader_loop(self) -> None:
        """Long-lived task: read subscribed messages and dispatch to callback.

        On connection drop, back off + reconnect + resubscribe every known room.
        Idle (no subscriptions) is fine — we just sleep briefly and re-check.
        """
        attempt = 0
        while self._started:
            try:
                if self._pubsub is None or not self._connected:
                    await self._reconnect(attempt)
                    attempt = min(attempt + 1, len(_BACKOFF_SECONDS) - 1)
                    continue
                # Nothing subscribed yet — don't poll get_message (redis client
                # raises when no channels are set on some versions/backends).
                if not self._subscribed:
                    await asyncio.sleep(0.05)
                    continue
                attempt = 0
                msg = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is None:
                    continue
                await self._dispatch(msg)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _emit(
                    EVENT_READER_ERROR,
                    "WARNING",
                    error=repr(exc),
                    message=f"pubsub reader error: {exc}",
                )
                # get_message() raising almost always means the subscriber's
                # transport dropped. Flip _connected so the next iteration
                # enters _reconnect and rebuilds the client; otherwise the
                # loop keeps calling the same broken _pubsub object forever
                # and this instance stops receiving terminal broadcasts —
                # provider sessions leak.
                self._connected = False
                attempt = min(attempt + 1, len(_BACKOFF_SECONDS) - 1)
                await asyncio.sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])

    async def _reconnect(self, attempt: int) -> None:
        delay = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
        _emit(
            EVENT_RECONNECTING,
            "INFO",
            attempt=attempt + 1,
            delay_seconds=delay,
            message=f"redis pubsub reconnecting in {delay:.1f}s (attempt {attempt + 1})",
        )
        await asyncio.sleep(delay)
        try:
            import redis.asyncio as aioredis  # type: ignore
            if self._sub is not None:
                try:
                    await self._sub.close()
                except Exception:
                    pass
            self._sub = aioredis.Redis(
                host=ENV.REDIS_HOST,
                port=ENV.REDIS_PORT,
                password=ENV.REDIS_PASSWORD or None,
                socket_connect_timeout=ENV.REDIS_CONNECT_TIMEOUT_SEC,
                decode_responses=True,
            )
            await asyncio.wait_for(self._sub.ping(), timeout=ENV.REDIS_COMMAND_TIMEOUT_SEC)
            # Reconcile _subscribed against _ref_counts under _lock. Without the
            # lock, a concurrent release_subscription can pop the last refcount
            # for a room after we've snapshotted `desired` — we'd then subscribe
            # a room nobody wants and record it in _subscribed with no future
            # release path (orphan Redis subscription). ensure_subscription
            # blocks briefly during reconnect, which is acceptable: reconnect
            # is rare and the alternative is data loss.
            async with self._lock:
                self._pubsub = self._sub.pubsub(ignore_subscribe_messages=True)
                self._subscribed.clear()
                desired = [k for k, count in self._ref_counts.items() if count > 0]
                if not desired:
                    self._connected = True
                    _emit(
                        EVENT_RECONNECTED,
                        "INFO",
                        rooms_resubscribed=0,
                        message="redis pubsub reconnected; 0 rooms to resubscribe",
                    )
                    return
                # Single bulk SUBSCRIBE so the whole reconciliation is bounded
                # by ONE REDIS_COMMAND_TIMEOUT_SEC, regardless of how many
                # rooms are on this instance. Per-room timeout could stall
                # _lock for N × timeout seconds on a hung connection.
                channels = [_channel_name(*key) for key in desired]
                try:
                    await asyncio.wait_for(
                        self._pubsub.subscribe(*channels),
                        timeout=ENV.REDIS_COMMAND_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    _emit(
                        EVENT_RECONNECT_FAILED,
                        "WARNING",
                        reason="bulk_subscribe_timeout",
                        rooms_desired=len(desired),
                        message=(
                            f"redis pubsub bulk resubscribe timeout "
                            f"({len(desired)} rooms) — will retry"
                        ),
                    )
                    self._connected = False
                    return
                except Exception as exc:
                    _emit(
                        EVENT_RECONNECT_FAILED,
                        "WARNING",
                        reason="bulk_subscribe_failed",
                        rooms_desired=len(desired),
                        error=repr(exc),
                        message=(
                            f"redis pubsub bulk resubscribe failed ({len(desired)} rooms): "
                            f"{exc} — will retry"
                        ),
                    )
                    self._connected = False
                    return
                # All-or-nothing on the bulk call: if wait_for returned, every
                # channel was accepted. Populate _subscribed accordingly.
                self._subscribed.update(desired)
                self._connected = True
                _emit(
                    EVENT_RECONNECTED,
                    "INFO",
                    rooms_resubscribed=len(self._subscribed),
                    message=(
                        f"redis pubsub reconnected; "
                        f"{len(self._subscribed)} rooms resubscribed (bulk)"
                    ),
                )
        except Exception as exc:
            _emit(
                EVENT_RECONNECT_FAILED,
                "WARNING",
                reason="ping_failed",
                error=repr(exc),
                message=f"redis reconnect failed: {exc}",
            )
            self._connected = False

    async def _dispatch(self, msg: dict) -> None:
        if self._callback is None:
            return
        try:
            channel = msg.get("channel") or ""
            data = msg.get("data")
            if not channel or data is None:
                return
            org_id, room_id = _parse_channel(channel)
            if not org_id or not room_id:
                return
            envelope = json.loads(data) if isinstance(data, (str, bytes)) else data
            if not isinstance(envelope, dict):
                return
            # Skip messages we published from this instance. broadcast_room
            # already delivered them locally; re-delivering here would double
            # up on the publisher instance (see design doc §8a).
            if envelope.get("publisher") == ENV.INSTANCE_ID:
                return
            payload = envelope.get("message")
            if not isinstance(payload, dict):
                return
            await self._callback(org_id, room_id, payload)
        except Exception as exc:
            log.warning("dispatch error: %s", exc)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time() % 1) * 1000):03d}Z"


def _parse_channel(channel: str) -> RoomKey:
    # `prefix:org:{orgId}:room:{roomId}` — split on ":org:" then ":room:".
    prefix = f"{ENV.REDIS_CHANNEL_PREFIX}:org:"
    if not channel.startswith(prefix):
        return ("", "")
    tail = channel[len(prefix):]
    parts = tail.split(":room:", 1)
    if len(parts) != 2:
        return ("", "")
    return (parts[0], parts[1])


# Module-level singleton, mirroring socket_manager.manager style.
pubsub = RedisPubSub()
