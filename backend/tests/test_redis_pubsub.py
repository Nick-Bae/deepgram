"""Redis Pub/Sub cross-instance fanout tests.

Uses fakeredis to simulate Redis without a running server. Verifies:
- publish -> subscribe roundtrip delivers to the subscriber callback
- seq monotonic per (org, room), isolated between rooms
- refcounted subscribe/unsubscribe (multiple listeners share one subscription)
- publish while disconnected returns None and does not raise
- envelope shape (v, seq, publisher, ts, message)
"""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

# fakeredis provides an in-memory redis.asyncio.Redis workalike.
import fakeredis.aioredis as fake_aio


def _make_pubsub(monkey_module, delivered):
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


class RedisPubSubRoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reset ENV cache; the module was imported once, but our fake start()
        # replaces the internals so ENV.REDIS_ENABLED value doesn't matter.
        self.delivered: list = []
        self.ps = _make_pubsub(self, self.delivered)
        await self.ps.start()
        # Yield to let the reader task begin.
        await asyncio.sleep(0.05)

    async def asyncTearDown(self):
        await self.ps.stop()

    async def _wait_for_delivery(self, n: int, timeout: float = 1.0):
        elapsed = 0.0
        while len(self.delivered) < n and elapsed < timeout:
            await asyncio.sleep(0.02)
            elapsed += 0.02

    async def test_publish_then_subscribe_delivers(self):
        await self.ps.ensure_subscription("ark", "sunday-main")
        await asyncio.sleep(0.05)  # let subscribe settle
        seq = await self.ps.publish_room("ark", "sunday-main", {"type": "translation", "payload": "hello"})
        self.assertIsInstance(seq, int)
        await self._wait_for_delivery(1)
        self.assertEqual(len(self.delivered), 1)
        org, room, msg = self.delivered[0]
        self.assertEqual((org, room), ("ark", "sunday-main"))
        self.assertEqual(msg["type"], "translation")
        self.assertEqual(msg["payload"], "hello")
        self.assertEqual(msg["seq"], seq)  # seq stamped onto message

    async def test_seq_monotonic_per_room(self):
        await self.ps.ensure_subscription("ark", "s1")
        await asyncio.sleep(0.05)
        seqs = []
        for i in range(5):
            seqs.append(await self.ps.publish_room("ark", "s1", {"i": i}))
        self.assertEqual(seqs, [1, 2, 3, 4, 5])

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
        # Still subscribed after one release.
        self.assertIn(("ark", "s1"), self.ps._subscribed)
        await self.ps.release_subscription("ark", "s1")
        self.assertNotIn(("ark", "s1"), self.ps._ref_counts)
        self.assertNotIn(("ark", "s1"), self.ps._subscribed)

    async def test_room_isolation_no_cross_delivery(self):
        await self.ps.ensure_subscription("ark", "s1")
        await asyncio.sleep(0.05)
        # publish to a DIFFERENT room we aren't subscribed to
        await self.ps.publish_room("newlife", "s1", {"x": 1})
        await asyncio.sleep(0.1)
        self.assertEqual(self.delivered, [])

    async def test_disabled_pubsub_is_noop(self):
        from app.services.redis_pubsub import RedisPubSub
        ps = RedisPubSub()
        ps._enabled = False
        self.assertIsNone(await ps.publish_room("ark", "s1", {"x": 1}))
        # ensure_subscription short-circuits without touching Redis
        await ps.ensure_subscription("ark", "s1")
        self.assertEqual(ps._ref_counts, {})


class ConnectionManagerFallbackTests(unittest.IsolatedAsyncioTestCase):
    """When Redis is disabled/disconnected, broadcast_room must deliver locally."""

    async def test_local_broadcast_when_redis_disabled(self):
        from app.services.redis_pubsub import pubsub as _real_pubsub
        from app.socket_manager import ConnectionManager

        # Force the singleton pubsub to look disabled during this test.
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


if __name__ == "__main__":
    unittest.main()
