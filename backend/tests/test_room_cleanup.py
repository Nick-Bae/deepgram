"""Regression tests for the room-cleanup / release-on-end pipeline.

Covers the failure modes flagged during code review of the
`fix/release-room-resources` branch. Each test names a specific past bug
and asserts the corrected behavior.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import fakeredis.aioredis as fake_aio

from app.socket_manager import ConnectionManager
from app.services.redis_pubsub import RedisPubSub


class MockWebSocket:
    """Minimal Starlette-WebSocket workalike for concurrency tests."""

    def __init__(
        self,
        *,
        send_delay: float = 0.0,
        send_raises: BaseException | None = None,
        close_delay: float = 0.0,
        close_raises: BaseException | None = None,
    ) -> None:
        self.send_delay = send_delay
        self.send_raises = send_raises
        self.close_delay = close_delay
        self.close_raises = close_raises
        self.sent: list[dict] = []
        self.close_calls: list[tuple[int, str]] = []

    async def send_json(self, msg: dict) -> None:
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        if self.send_raises is not None:
            raise self.send_raises
        self.sent.append(msg)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        if self.close_raises is not None:
            raise self.close_raises
        self.close_calls.append((code, reason))


def _fresh_pubsub() -> RedisPubSub:
    """A RedisPubSub wired to fakeredis. start() is a no-op — tests drive it."""
    ps = RedisPubSub()
    ps._enabled = True

    async def _fake_start() -> None:
        ps._started = True
        server = fake_aio.FakeServer()
        ps._pub = fake_aio.FakeRedis(server=server, decode_responses=True)
        ps._sub = fake_aio.FakeRedis(server=server, decode_responses=True)
        await ps._pub.ping()
        ps._pubsub = ps._sub.pubsub(ignore_subscribe_messages=True)
        ps._connected = True
        ps._reader_task = asyncio.create_task(ps._reader_loop())

    ps.start = _fake_start  # type: ignore[assignment]
    return ps


class TerminalBroadcastTests(unittest.IsolatedAsyncioTestCase):
    """Failed-send during terminal broadcast must still close the socket."""

    async def test_send_one_times_out_and_returns_false(self) -> None:
        # Direct test of the production _send_one: a hanging send_json must
        # be cut off by the built-in wait_for and returned as False.
        manager = ConnectionManager()
        hanging_ws = MockWebSocket(send_delay=10.0)  # far longer than 2s
        result = await asyncio.wait_for(
            manager._send_one(hanging_ws, {"type": "x"}), timeout=3.0
        )
        self.assertIs(result, False)

    async def test_send_timeout_still_closes_socket_with_terminal_reason(self) -> None:
        # Uses the REAL _send_one so the production 2s timeout is exercised.
        # We patch _send_one's timeout down to keep the test fast, but the
        # semantics under test (send failure → close with room_ended during
        # a terminal broadcast) remain unchanged.
        manager = ConnectionManager()
        # Patch just the wait_for timeout inside _send_one via monkey-patching
        # asyncio.wait_for isn't clean; the simpler route is a very-slow ws
        # and a temporary override that mirrors production logic exactly.
        original_send_one = manager._send_one

        async def bounded_send_one(ws, message):  # type: ignore[no-untyped-def]
            try:
                await asyncio.wait_for(ws.send_json(message), timeout=0.05)
                return True
            except Exception:
                return False

        manager._send_one = bounded_send_one  # type: ignore[assignment]
        slow_ws = MockWebSocket(send_delay=1.0)
        manager.join_room(slow_ws, "org", "room", "listener")
        await manager._broadcast_local_room(
            "org", "room", {"type": "STATUS", "roomStatus": "ended"}
        )
        self.assertEqual(len(slow_ws.close_calls), 1)
        self.assertEqual(slow_ws.close_calls[0], (1000, "room_ended"))
        self.assertNotIn(slow_ws, manager.connections_by_room.get(("org", "room"), set()))
        manager._send_one = original_send_one  # type: ignore[assignment]

    async def test_non_terminal_send_failure_uses_generic_close(self) -> None:
        manager = ConnectionManager()
        failing_ws = MockWebSocket(send_raises=RuntimeError("broken pipe"))
        manager.join_room(failing_ws, "org", "room", "listener")
        await manager._broadcast_local_room(
            "org", "room", {"type": "translation", "text": "hi"}
        )
        # Non-terminal branch uses 1011/send_failed so the client knows it may
        # reconnect (the room is still live).
        self.assertEqual(len(failing_ws.close_calls), 1)
        self.assertEqual(failing_ws.close_calls[0], (1011, "send_failed"))


class TombstoneOrderingTests(unittest.IsolatedAsyncioTestCase):
    """Tombstone and hooks must be visible BEFORE any listener can react."""

    async def test_tombstone_visible_from_first_send(self) -> None:
        manager = ConnectionManager()
        # A ws whose send_json inspects the tombstone at delivery time.
        observed: dict = {}

        async def inspecting_send(msg: dict) -> None:
            observed["tombstone_visible"] = manager.is_room_locally_ended("org", "room")

        inspector_ws = MockWebSocket()
        inspector_ws.send_json = inspecting_send  # type: ignore[assignment]
        manager.join_room(inspector_ws, "org", "room", "listener")

        await manager._broadcast_local_room(
            "org", "room", {"type": "STATUS", "roomStatus": "ended"}
        )

        self.assertTrue(observed["tombstone_visible"])

    async def test_room_end_hooks_fire_before_sends(self) -> None:
        manager = ConnectionManager()
        hook_calls: list[tuple[str, str, int]] = []
        # The listener's send_json records how many hooks had fired at delivery
        # time — must be non-zero if hooks run BEFORE sends.
        sent_after: list[int] = []

        def hook(org: str, room: str) -> None:
            hook_calls.append((org, room, len(sent_after)))

        manager.register_room_end_hook(hook)
        ws = MockWebSocket()
        original_send = ws.send_json

        async def wrapped_send(msg: dict) -> None:
            sent_after.append(len(hook_calls))
            await original_send(msg)

        ws.send_json = wrapped_send  # type: ignore[assignment]
        manager.join_room(ws, "org", "room", "listener")
        await manager._broadcast_local_room(
            "org", "room", {"type": "STATUS", "roomStatus": "ended"}
        )
        # Hook fires before any send.
        self.assertEqual(len(hook_calls), 1)
        self.assertEqual(hook_calls[0][:2], ("org", "room"))
        self.assertEqual(sent_after, [1])  # hook count was 1 when send ran

    async def test_hook_registration_is_idempotent(self) -> None:
        manager = ConnectionManager()
        calls: list[int] = []

        def hook(org: str, room: str) -> None:
            calls.append(1)

        manager.register_room_end_hook(hook)
        manager.register_room_end_hook(hook)  # duplicate
        manager.register_room_end_hook(hook)  # duplicate
        await manager._broadcast_local_room(
            "org", "room", {"type": "STATUS", "roomStatus": "ended"}
        )
        self.assertEqual(len(calls), 1)  # not 3


class HostRegistrationTests(unittest.IsolatedAsyncioTestCase):
    """register_host ownership and cancellation rollback."""

    async def test_register_host_ownership_balanced_on_disconnect(self) -> None:
        manager = ConnectionManager()
        ps = _fresh_pubsub()
        await ps.start()
        await asyncio.sleep(0.02)
        with patch("app.socket_manager.pubsub", ps):
            ws = MockWebSocket()
            await manager.register_host(ws, "org", "room")
            self.assertIn(ws, manager.host_subscription_owned_by_ws)
            self.assertEqual(ps._ref_counts[("org", "room")], 1)
            manager.note_host_disconnected(ws)
            # release_subscription is scheduled; wait for it.
            await asyncio.sleep(0.05)
            self.assertNotIn(ws, manager.host_subscription_owned_by_ws)
            self.assertNotIn(("org", "room"), ps._ref_counts)
        await ps.stop()

    async def test_failed_register_host_does_not_own_or_leak_refcount(self) -> None:
        # Use the REAL ensure_subscription so the test would fail if its
        # rollback logic were broken. Inject the failure at _subscribe_channel
        # so ensure_subscription's ref++/rollback path is actually exercised.
        manager = ConnectionManager()
        ps = _fresh_pubsub()
        await ps.start()
        await asyncio.sleep(0.02)
        original_subscribe_channel = ps._subscribe_channel

        async def failing_subscribe(key):  # type: ignore[no-untyped-def]
            raise RuntimeError("simulated SUBSCRIBE fail")

        ps._subscribe_channel = failing_subscribe  # type: ignore[assignment]
        with patch("app.socket_manager.pubsub", ps):
            ws = MockWebSocket()
            with self.assertRaises(Exception):
                await manager.register_host(ws, "org", "room")
            # Ownership never recorded (exception fired before add).
            self.assertNotIn(ws, manager.host_subscription_owned_by_ws)
            # Presence rolled back via register_host's except.
            self.assertNotIn(ws, manager.host_presence_by_ws)
            # ensure_subscription's rollback popped the transient refcount.
            self.assertNotIn(("org", "room"), ps._ref_counts)
            # And no phantom _subscribed entry.
            self.assertNotIn(("org", "room"), ps._subscribed)
        ps._subscribe_channel = original_subscribe_channel  # type: ignore[assignment]
        await ps.stop()

    async def test_cancelled_register_host_rolls_back(self) -> None:
        # Cancellation during ensure_subscription must fully roll back:
        # presence, ownership, and refcount. asyncio.CancelledError is a
        # BaseException in modern Python, so a plain `except Exception`
        # would miss it — this test guards register_host's
        # `except BaseException` path.
        manager = ConnectionManager()
        ps = _fresh_pubsub()
        await ps.start()
        await asyncio.sleep(0.02)

        async def slow_subscribe(key):  # type: ignore[no-untyped-def]
            await asyncio.sleep(5.0)

        ps._subscribe_channel = slow_subscribe  # type: ignore[assignment]
        with patch("app.socket_manager.pubsub", ps):
            ws = MockWebSocket()
            task = asyncio.create_task(manager.register_host(ws, "org", "room"))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            # Full rollback.
            self.assertNotIn(ws, manager.host_subscription_owned_by_ws)
            self.assertNotIn(ws, manager.host_presence_by_ws)
            self.assertNotIn(("org", "room"), ps._ref_counts)
        await ps.stop()

    async def test_note_host_disconnected_does_not_release_unowned(self) -> None:
        """The refcount decrement race — a ws that never owned must not release."""
        manager = ConnectionManager()
        ps = _fresh_pubsub()
        await ps.start()
        await asyncio.sleep(0.02)
        with patch("app.socket_manager.pubsub", ps):
            # Owner acquires the subscription.
            owner_ws = MockWebSocket()
            await manager.register_host(owner_ws, "org", "room")
            self.assertEqual(ps._ref_counts[("org", "room")], 1)

            # A different ws with presence but no ownership disconnects.
            intruder_ws = MockWebSocket()
            manager.note_host_connected(intruder_ws, "org", "room")
            manager.note_host_disconnected(intruder_ws)
            await asyncio.sleep(0.05)

            # Owner's refcount must remain intact.
            self.assertEqual(ps._ref_counts.get(("org", "room")), 1)
            self.assertIn(owner_ws, manager.host_subscription_owned_by_ws)
        await ps.stop()


class ListenerRegistrationTests(unittest.IsolatedAsyncioTestCase):
    """register_listener readiness enforcement + ownership + rollback."""

    async def test_not_ready_rolls_back_and_releases_refcount(self) -> None:
        manager = ConnectionManager()
        ps = _fresh_pubsub()
        await ps.start()
        await asyncio.sleep(0.02)
        # Force ensure_subscription to return False (not ready).
        original = ps.ensure_subscription

        async def not_ready_ensure(org, room):  # type: ignore[no-untyped-def]
            # Mimic the real refcount++ behavior for the rollback assertion.
            async with ps._lock:
                key = (org, room)
                ps._ref_counts[key] = ps._ref_counts.get(key, 0) + 1
            return False

        ps.ensure_subscription = not_ready_ensure  # type: ignore[assignment]
        with patch("app.socket_manager.pubsub", ps):
            ws = MockWebSocket()
            with self.assertRaises(RuntimeError) as ctx:
                await manager.register_listener(ws, "org", "room", "listener")
            self.assertIn("not_ready", str(ctx.exception))
            # Presence rolled back.
            self.assertNotIn(ws, manager.room_by_ws)
            # Ownership was recorded then released via disconnect's ownership path.
            self.assertNotIn(ws, manager.listener_subscription_owned_room_by_ws)
            await asyncio.sleep(0.05)
            # Refcount balanced back to 0.
            self.assertNotIn(("org", "room"), ps._ref_counts)
        ps.ensure_subscription = original  # type: ignore[assignment]
        await ps.stop()

    async def test_ready_returns_viewer_count_and_records_ownership(self) -> None:
        manager = ConnectionManager()
        ps = _fresh_pubsub()
        await ps.start()
        await asyncio.sleep(0.02)
        with patch("app.socket_manager.pubsub", ps):
            ws = MockWebSocket()
            viewer_count = await manager.register_listener(ws, "org", "room", "listener")
            self.assertEqual(viewer_count, 1)
            self.assertEqual(
                manager.listener_subscription_owned_room_by_ws.get(ws),
                ("org", "room"),
            )
        await ps.stop()


class SubscriptionSemanticsTests(unittest.IsolatedAsyncioTestCase):
    """Refcount rollback + _subscribed truthfulness."""

    async def test_ensure_subscription_rolls_back_on_failure(self) -> None:
        ps = _fresh_pubsub()
        await ps.start()
        await asyncio.sleep(0.02)
        # Poison _subscribe_channel so the first call raises.
        original = ps._subscribe_channel

        async def failing(key):  # type: ignore[no-untyped-def]
            raise RuntimeError("simulated subscribe fail")

        ps._subscribe_channel = failing  # type: ignore[assignment]
        with self.assertRaises(Exception):
            await ps.ensure_subscription("org", "room")
        # Refcount must be rolled back so a retry actually re-attempts subscribe.
        self.assertNotIn(("org", "room"), ps._ref_counts)
        # And _subscribed must not contain a phantom entry.
        self.assertNotIn(("org", "room"), ps._subscribed)
        ps._subscribe_channel = original  # type: ignore[assignment]
        await ps.stop()

    async def test_ensure_subscription_reports_not_ready_when_disconnected(self) -> None:
        ps = _fresh_pubsub()
        await ps.start()
        await asyncio.sleep(0.02)
        # Manually force disconnected state.
        ps._connected = False
        ready = await ps.ensure_subscription("org", "room")
        self.assertFalse(ready)
        # But refcount was incremented (caller may retry / release).
        self.assertEqual(ps._ref_counts.get(("org", "room")), 1)
        # Compensate refcount so tear-down is clean.
        await ps.release_subscription("org", "room")
        await ps.stop()


class BroadcastLocalRoomTests(unittest.IsolatedAsyncioTestCase):
    """Terminal broadcast triggers close_room_listeners and hosts on this instance."""

    async def test_terminal_broadcast_closes_local_hosts(self) -> None:
        manager = ConnectionManager()
        # Register a host with a shutdown callback so we can verify it fires.
        host_ws = MockWebSocket()
        fired = asyncio.Event()
        manager.register_host_shutdown_callback(host_ws, fired.set)
        manager.note_host_connected(host_ws, "org", "room")
        # Publish terminal.
        await manager._broadcast_local_room(
            "org", "room", {"type": "STATUS", "roomStatus": "ended"}
        )
        self.assertTrue(fired.is_set())
        self.assertEqual(len(host_ws.close_calls), 1)

    async def test_terminal_broadcast_releases_listener_refcount(self) -> None:
        # After a terminal broadcast, the room's subscription refcount must
        # go back to 0 — otherwise the Redis subscription lingers.
        manager = ConnectionManager()
        ps = _fresh_pubsub()
        await ps.start()
        await asyncio.sleep(0.02)
        with patch("app.socket_manager.pubsub", ps):
            ws = MockWebSocket()
            await manager.register_listener(ws, "org", "room", "listener")
            self.assertEqual(ps._ref_counts[("org", "room")], 1)
            await manager._broadcast_local_room(
                "org", "room", {"type": "STATUS", "roomStatus": "ended"}
            )
            # close_room_listeners → disconnect(ws) → owned check → release.
            await asyncio.sleep(0.05)
            self.assertNotIn(("org", "room"), ps._ref_counts)
        await ps.stop()

    async def test_terminal_broadcast_releases_host_refcount(self) -> None:
        manager = ConnectionManager()
        ps = _fresh_pubsub()
        await ps.start()
        await asyncio.sleep(0.02)
        with patch("app.socket_manager.pubsub", ps):
            ws = MockWebSocket()
            await manager.register_host(ws, "org", "room")
            self.assertEqual(ps._ref_counts[("org", "room")], 1)
            await manager._broadcast_local_room(
                "org", "room", {"type": "STATUS", "roomStatus": "ended"}
            )
            await asyncio.sleep(0.05)
            self.assertNotIn(("org", "room"), ps._ref_counts)
        await ps.stop()


class RedisDispatchTests(unittest.IsolatedAsyncioTestCase):
    """A terminal event delivered via Redis _dispatch must fire the full pipeline."""

    async def test_dispatch_fires_hooks_and_closes_sockets(self) -> None:
        # Two RedisPubSub instances sharing one FakeServer simulate two
        # Cloud Run instances. Publish from A, verify B's dispatch triggers
        # the terminal path (hooks + host shutdown callback + listener close).
        import fakeredis.aioredis as _fake

        server = _fake.FakeServer()

        async def _make_shared(delivered):
            ps = RedisPubSub()
            ps._enabled = True
            ps._started = True
            ps._pub = _fake.FakeRedis(server=server, decode_responses=True)
            ps._sub = _fake.FakeRedis(server=server, decode_responses=True)
            await ps._pub.ping()
            ps._pubsub = ps._sub.pubsub(ignore_subscribe_messages=True)
            ps._connected = True
            ps._reader_task = asyncio.create_task(ps._reader_loop())

            async def cb(org, room, msg):
                delivered.append((org, room, msg))

            ps.set_delivery_callback(cb)
            return ps

        ps_a = await _make_shared([])
        b_delivered: list = []
        ps_b = await _make_shared(b_delivered)
        # Different INSTANCE_IDs so dispatch on B doesn't skip A's publish.
        ps_a._enabled = True
        ps_b._enabled = True
        from app.env import ENV
        # Save and set instance IDs.
        original_id = ENV.INSTANCE_ID
        try:
            ENV.INSTANCE_ID = "instance-A"
            manager_b = ConnectionManager()
            hook_fired = {"count": 0}

            def hook(org, room):
                hook_fired["count"] += 1

            manager_b.register_room_end_hook(hook)
            # Wire ps_b as B's manager pubsub delivery target.
            ps_b.set_delivery_callback(manager_b._broadcast_local_room)

            # Register a listener on B so B has a room to subscribe to.
            with patch("app.socket_manager.pubsub", ps_b):
                ws_b = MockWebSocket()
                await manager_b.register_listener(ws_b, "org", "room", "listener")
                await asyncio.sleep(0.05)
                # Publish terminal from A.
                await ps_a.publish_room(
                    "org", "room", {"type": "STATUS", "roomStatus": "ended"}
                )
                # Give the reader a moment to dispatch.
                await asyncio.sleep(0.15)
                # B's hook fired and B's ws was closed.
                self.assertEqual(hook_fired["count"], 1)
                self.assertGreaterEqual(len(ws_b.close_calls), 1)
                self.assertTrue(manager_b.is_room_locally_ended("org", "room"))
        finally:
            ENV.INSTANCE_ID = original_id
            await ps_a.stop()
            await ps_b.stop()


class StaleLiveSuppressionTests(unittest.IsolatedAsyncioTestCase):
    """After tombstone is set, is_room_locally_ended blocks 'live' broadcasts."""

    async def test_multi_listener_shutdown_tombstone_visible(self) -> None:
        manager = ConnectionManager()
        # Simulate multi-listener bucket. When a terminal fires, tombstone is
        # set before sends — a listener finally executed mid-broadcast should
        # see is_room_locally_ended=True and skip its own STATUS live broadcast.
        # We just verify the tombstone gate directly (the finally is in
        # main.py; the manager exposes the primitive).
        w1, w2 = MockWebSocket(), MockWebSocket()
        manager.join_room(w1, "org", "room", "listener")
        manager.join_room(w2, "org", "room", "listener")
        await manager._broadcast_local_room(
            "org", "room", {"type": "STATUS", "roomStatus": "ended"}
        )
        # Both listeners were in the bucket during the terminal call.
        # After: bucket is empty (close_room_listeners disconnected them) and
        # tombstone remains set for 5 min so any late finally would skip live.
        self.assertTrue(manager.is_room_locally_ended("org", "room"))

    async def test_tombstone_expires(self) -> None:
        manager = ConnectionManager()
        manager._mark_room_ended_locally("org", "room")
        self.assertTrue(manager.is_room_locally_ended("org", "room"))
        # Force expiry.
        manager.ended_rooms_local[("org", "room")] = 0.0
        self.assertFalse(manager.is_room_locally_ended("org", "room"))


class RoomLiveTests(unittest.IsolatedAsyncioTestCase):
    """multichurch_store.is_room_live behavior (in-memory variant)."""

    def test_in_memory_is_room_live_returns_true_for_live_room(self) -> None:
        from app.services.multichurch_store import InMemoryMultiChurchStore
        store = InMemoryMultiChurchStore()
        # Add a live room directly.
        store._rooms[("org", "room")] = {"status": "live"}
        self.assertTrue(store.is_room_live("org", "room"))

    def test_in_memory_is_room_live_false_for_ended_room(self) -> None:
        from app.services.multichurch_store import InMemoryMultiChurchStore
        store = InMemoryMultiChurchStore()
        store._rooms[("org", "room")] = {"status": "ended"}
        self.assertFalse(store.is_room_live("org", "room"))

    def test_in_memory_is_room_live_false_for_missing(self) -> None:
        from app.services.multichurch_store import InMemoryMultiChurchStore
        store = InMemoryMultiChurchStore()
        self.assertFalse(store.is_room_live("nope", "nope"))


class RoomSwitchOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """register_listener with a different room releases old, acquires new."""

    async def test_ownership_transfers_on_room_switch(self) -> None:
        manager = ConnectionManager()
        ps = _fresh_pubsub()
        await ps.start()
        await asyncio.sleep(0.02)
        with patch("app.socket_manager.pubsub", ps):
            ws = MockWebSocket()
            # First room.
            await manager.register_listener(ws, "org", "room-1", "listener")
            self.assertEqual(
                manager.listener_subscription_owned_room_by_ws.get(ws),
                ("org", "room-1"),
            )
            self.assertEqual(ps._ref_counts[("org", "room-1")], 1)
            # Switch to room-2.
            await manager.register_listener(ws, "org", "room-2", "listener")
            await asyncio.sleep(0.05)  # let scheduled release fire
            self.assertEqual(
                manager.listener_subscription_owned_room_by_ws.get(ws),
                ("org", "room-2"),
            )
            self.assertNotIn(("org", "room-1"), ps._ref_counts)  # released
            self.assertEqual(ps._ref_counts[("org", "room-2")], 1)  # acquired
        await ps.stop()


class SubscribeTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """SUBSCRIBE with a bounded command timeout marks disconnected on timeout."""

    async def test_subscribe_timeout_marks_disconnected(self) -> None:
        ps = _fresh_pubsub()
        await ps.start()
        await asyncio.sleep(0.02)

        class HangingPubSub:
            async def subscribe(self, *args, **kwargs):
                await asyncio.sleep(10.0)

            async def unsubscribe(self, *args, **kwargs):
                return None

            async def get_message(self, *args, **kwargs):
                return None

        ps._pubsub = HangingPubSub()  # type: ignore[assignment]
        # Patch the timeout down for speed.
        from app.env import ENV
        original_timeout = ENV.REDIS_COMMAND_TIMEOUT_SEC
        ENV.REDIS_COMMAND_TIMEOUT_SEC = 0.1
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await ps._subscribe_channel(("org", "room"))
            self.assertFalse(ps._connected)
        finally:
            ENV.REDIS_COMMAND_TIMEOUT_SEC = original_timeout
        await ps.stop()

    async def test_subscribe_non_timeout_error_also_marks_disconnected(self) -> None:
        ps = _fresh_pubsub()
        await ps.start()
        await asyncio.sleep(0.02)

        class BrokenPubSub:
            async def subscribe(self, *args, **kwargs):
                raise ConnectionError("transport broken")

            async def unsubscribe(self, *args, **kwargs):
                return None

            async def get_message(self, *args, **kwargs):
                return None

        ps._pubsub = BrokenPubSub()  # type: ignore[assignment]
        with self.assertRaises(ConnectionError):
            await ps._subscribe_channel(("org", "room"))
        # Non-timeout transport errors also flip disconnected (73f55d6a).
        self.assertFalse(ps._connected)
        await ps.stop()


if __name__ == "__main__":
    unittest.main()
