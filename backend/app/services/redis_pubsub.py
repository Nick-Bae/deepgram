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
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from app.env import ENV

log = logging.getLogger("redis_pubsub")

RoomKey = Tuple[str, str]
DeliveryCallback = Callable[[str, str, dict], Awaitable[None]]

_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0)
_ENVELOPE_VERSION = 1


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

    def set_delivery_callback(self, cb: DeliveryCallback) -> None:
        """Called by ConnectionManager to receive incoming subscribed messages."""
        self._callback = cb

    async def start(self) -> None:
        if not self._enabled or self._started:
            return
        self._started = True
        try:
            import redis.asyncio as aioredis  # type: ignore
        except Exception as exc:
            log.error("redis package not installed; disabling pubsub: %s", exc)
            self._enabled = False
            return

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
            await self._pub.ping()
            self._pubsub = self._sub.pubsub(ignore_subscribe_messages=True)
            self._connected = True
            self._reader_task = asyncio.create_task(self._reader_loop(), name="redis-pubsub-reader")
            log.info(
                "redis pubsub started host=%s:%s prefix=%s instance=%s",
                ENV.REDIS_HOST, ENV.REDIS_PORT, ENV.REDIS_CHANNEL_PREFIX, ENV.INSTANCE_ID,
            )
        except Exception as exc:
            log.error("redis connect failed; falling back to local-only broadcast: %s", exc)
            self._connected = False
            # Keep _enabled True so a future start() retry could work; publish/subscribe
            # will short-circuit on _connected until then.

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._connected = False
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
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
            await self._pubsub.subscribe(_channel_name(*key))
        except Exception as exc:
            log.warning("redis SUBSCRIBE failed key=%s: %s", key, exc)
            raise  # caller (ensure_subscription) rolls back refcount
        self._subscribed.add(key)

    async def _unsubscribe_channel(self, key: RoomKey) -> None:
        self._subscribed.discard(key)
        if not self._connected or self._pubsub is None:
            return
        try:
            await self._pubsub.unsubscribe(_channel_name(*key))
        except Exception as exc:
            log.warning("redis UNSUBSCRIBE failed key=%s: %s", key, exc)

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
                log.warning("pubsub reader error: %s", exc)
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
        log.info("redis pubsub reconnecting in %.1fs (attempt %d)", delay, attempt + 1)
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
            await self._sub.ping()
            self._pubsub = self._sub.pubsub(ignore_subscribe_messages=True)
            # Rebuild _subscribed from actual SUBSCRIBE results. Any room that
            # someone still wants (refcount > 0) gets resubscribed here; only
            # rooms where SUBSCRIBE actually succeeds land in _subscribed.
            # Prevents ensure_subscription from later returning True for a
            # room whose partial-reconnect subscribe failed silently.
            self._subscribed.clear()
            desired = [k for k, count in self._ref_counts.items() if count > 0]
            for key in desired:
                try:
                    await self._pubsub.subscribe(_channel_name(*key))
                    self._subscribed.add(key)
                except Exception as exc:
                    log.warning("resubscribe failed key=%s: %s", key, exc)
            self._connected = True
            log.info(
                "redis pubsub reconnected; %d/%d rooms resubscribed",
                len(self._subscribed),
                len(desired),
            )
        except Exception as exc:
            log.warning("redis reconnect failed: %s", exc)
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
