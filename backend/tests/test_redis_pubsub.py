"""Redis Pub/Sub cross-instance fanout tests.

Uses fakeredis to simulate Redis without a running server. Verifies:
- refcounted subscribe/unsubscribe lifecycle
- seq monotonic per (org, room), isolated between rooms
- publish→dispatch skips messages from own instance (no double-delivery)
- cross-instance simulation: subscriber DOES fire when publisher is a different instance
- publish while disabled/disconnected returns None and does not raise
- broadcast_room delivers LOCALLY regardless of subscription state (race fix)
- `_rseq` stamped on published envelope but the caller's original dict is not mutated
"""
from __future__ import annotations

import asyncio
import unittest

# fakeredis provides an in-memory redis.asyncio.Redis workalike.
import fakeredis.aioredis as fake_aio

from app.env import ENV


def _make_pubsub(delivered):
    """Build a RedisPubSub instance patched to use fakeredis for _pub and _sub."""
    from app.services.redis_pubsub import RedisPubSub

    ps = RedisPubSub()
    ps._enabled = True

    async def _fake_start():
        ps._started = True
        server = fake_aio.FakeServer()
        ps._pub = fake_aio.FakeRedis(server=server, decode_responses=True)
        ps._sub = fake_aio.FakeRedis(server=server, decode_responses=True)
        await ps._pub.ping()
        ps._pubsub = ps._sub.pubsub(ignore_subscribe_messages=True)
        ps._connected = True
        ps._reader_task = asyncio.create_task(ps._reader_loop())

    ps.start = _fake_start  # type: ignore[assignment]

    async def _cb(org, room, msg):
        delivered.append((org, room, msg))

    ps.set_delivery_callback(_cb)
    return ps


class RedisPubSubDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.delivered: list = []
        self.ps = _make_pubsub(self.delivered)
        await self.ps.start()
        await asyncio.sleep(0.05)

    async def asyncTearDown(self):
        await self.ps.stop()

    async def _wait(self, n: int, timeout: float = 1.0):
        elapsed = 0.0
        while len(self.delivered) < n and elapsed < timeout:
            await asyncio.sleep(0.02)
            elapsed += 0.02

    async def test_publisher_skips_own_publish(self):
        """A message published by this instance MUST NOT be re-delivered by our
        own subscriber — the caller (broadcast_room) already delivered locally."""
        await self.ps.ensure_subscription("ark", "s1")
        await asyncio.sleep(0.05)
        await self.ps.publish_room("ark", "s1", {"type": "translation", "payload": "hi"})
        # Give the subscriber loop time to receive + filter.
        await asyncio.sleep(0.2)
        self.assertEqual(self.delivered, [])

    async def test_cross_instance_dispatch_fires(self):
        """A message from a DIFFERENT publisher instance MUST reach the callback."""
        prev_instance = ENV.INSTANCE_ID
        try:
            await self.ps.ensure_subscription("ark", "s1")
            await asyncio.sleep(0.05)
            # Simulate a peer instance by changing our INSTANCE_ID before publish
            # (so publish stamps envelope.publisher != current INSTANCE_ID at dispatch).
            ENV.INSTANCE_ID = "inst-peer"
            await self.ps.publish_room("ark", "s1", {"type": "translation", "payload": "hi"})
            ENV.INSTANCE_ID = prev_instance
            await self._wait(1)
            self.assertEqual(len(self.delivered), 1)
            org, room, msg = self.delivered[0]
            self.assertEqual((org, room), ("ark", "s1"))
            self.assertEqual(msg["payload"], "hi")
            self.assertEqual(msg["_rseq"], 1)
        finally:
            ENV.INSTANCE_ID = prev_instance

    async def test_publish_does_not_mutate_caller_dict(self):
        """publish_room must work on a copy — the local-delivery path in
        broadcast_room sends the caller's dict concurrently and would race
        with any mutation here."""
        original = {"type": "translation", "payload": "hi"}
        await self.ps.publish_room("ark", "s1", original)
        self.assertNotIn("_rseq", original)

    async def test_seq_monotonic_per_room(self):
        prev_instance = ENV.INSTANCE_ID
        try:
            await self.ps.ensure_subscription("ark", "s1")
            await asyncio.sleep(0.05)
            ENV.INSTANCE_ID = "inst-peer"
            seqs = []
            for i in range(5):
                seqs.append(await self.ps.publish_room("ark", "s1", {"i": i}))
            self.assertEqual(seqs, [1, 2, 3, 4, 5])
        finally:
            ENV.INSTANCE_ID = prev_instance

    async def test_seq_isolated_per_room(self):
        await self.ps.ensure_subscription("ark", "s1")
        await self.ps.ensure_subscription("ark", "s2")
        await asyncio.sleep(0.05)
        a1 = await self.ps.publish_room("ark", "s1", {"m": "a"})
        b1 = await self.ps.publish_room("ark", "s2", {"m": "b"})
        a2 = await self.ps.publish_room("ark", "s1", {"m": "c"})
        self.assertEqual((a1, b1, a2), (1, 1, 2))

    async def test_refcount_subscribe(self):
        await self.ps.ensure_subscription("ark", "s1")
        await self.ps.ensure_subscription("ark", "s1")  # second listener
        self.assertEqual(self.ps._ref_counts[("ark", "s1")], 2)
        await self.ps.release_subscription("ark", "s1")
        self.assertEqual(self.ps._ref_counts[("ark", "s1")], 1)
        self.assertIn(("ark", "s1"), self.ps._subscribed)
        await self.ps.release_subscription("ark", "s1")
        self.assertNotIn(("ark", "s1"), self.ps._ref_counts)
        self.assertNotIn(("ark", "s1"), self.ps._subscribed)

    async def test_disabled_pubsub_is_noop(self):
        from app.services.redis_pubsub import RedisPubSub
        ps = RedisPubSub()
        ps._enabled = False
        self.assertIsNone(await ps.publish_room("ark", "s1", {"x": 1}))
        await ps.ensure_subscription("ark", "s1")
        self.assertEqual(ps._ref_counts, {})


class ConnectionManagerBroadcastTests(unittest.IsolatedAsyncioTestCase):
    """broadcast_room must deliver locally even when Redis is off, and must
    deliver locally immediately when Redis is on (race fix)."""

    async def test_local_broadcast_when_redis_disabled(self):
        from app.services.redis_pubsub import pubsub as _real_pubsub
        from app.socket_manager import ConnectionManager

        prev_enabled = _real_pubsub._enabled
        _real_pubsub._enabled = False
        try:
            m = ConnectionManager()

            class _FakeWS:
                def __init__(self):
                    self.sent = []
                async def accept(self):
                    pass
                async def send_json(self, obj):
                    self.sent.append(obj)

            ws1, ws2 = _FakeWS(), _FakeWS()
            m.join_room(ws1, "ark", "s1", "listener")
            m.join_room(ws2, "ark", "s1", "listener")
            await m.broadcast_room("ark", "s1", {"type": "translation", "payload": "hi"})
            self.assertEqual(len(ws1.sent), 1)
            self.assertEqual(len(ws2.sent), 1)
            self.assertEqual(ws1.sent[0]["payload"], "hi")
        finally:
            _real_pubsub._enabled = prev_enabled

    async def test_local_broadcast_when_redis_enabled_no_subscription_yet(self):
        """The race fix: local delivery MUST happen even when the pubsub
        subscription hasn't completed (simulates the join_room fire-and-forget)."""
        from app.services.redis_pubsub import RedisPubSub, pubsub as _singleton
        from app.socket_manager import ConnectionManager

        # Point the socket_manager singleton at a fake pubsub whose subscription
        # never completes (simulates race). We use fakeredis for _pub so publish
        # itself succeeds; the reader loop just never gets a chance to dispatch.
        prev_pub = _singleton._pub
        prev_started = _singleton._started
        prev_enabled = _singleton._enabled
        prev_connected = _singleton._connected

        server = fake_aio.FakeServer()
        _singleton._pub = fake_aio.FakeRedis(server=server, decode_responses=True)
        _singleton._enabled = True
        _singleton._connected = True
        try:
            m = ConnectionManager()

            class _FakeWS:
                def __init__(self):
                    self.sent = []
                async def accept(self):
                    pass
                async def send_json(self, obj):
                    self.sent.append(obj)

            ws1 = _FakeWS()
            m.join_room(ws1, "ark", "s1", "listener")
            # DO NOT await pubsub.ensure_subscription — this is the race.
            await m.broadcast_room("ark", "s1", {"type": "translation", "payload": "hi"})
            # Local delivery must have happened regardless of subscription state.
            self.assertEqual(len(ws1.sent), 1)
            self.assertEqual(ws1.sent[0]["payload"], "hi")
        finally:
            _singleton._pub = prev_pub
            _singleton._started = prev_started
            _singleton._enabled = prev_enabled
            _singleton._connected = prev_connected


if __name__ == "__main__":
    unittest.main()
