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

    async def test_send_timeout_still_closes_socket_with_terminal_reason(self) -> None:
        # A listener whose send_json hangs longer than _send_one's timeout
        # must still receive a ws.close() call — otherwise the untracked
        # socket lingers, which was the bug ChatGPT reproduced.
        manager = ConnectionManager()
        # Patch the _send_one timeout to a short value so the test runs fast.
        original_send_one = manager._send_one

        async def _fast_send(ws, message):  # type: ignore[no-untyped-def]
            try:
                await asyncio.wait_for(ws.send_json(message), timeout=0.05)
                return True
            except Exception:
                return False

        manager._send_one = _fast_send  # type: ignore[assignment]

        slow_ws = MockWebSocket(send_delay=1.0)  # exceeds 0.05s timeout
        manager.join_room(slow_ws, "org", "room", "listener")

        # Deliver the terminal message. _broadcast_local_room should close
        # the timed-out socket via _close_one, not just self.disconnect(ws).
        await manager._broadcast_local_room(
            "org", "room", {"type": "STATUS", "roomStatus": "ended"}
        )

        # 1000/room_ended (terminal branch) — NOT 1011/send_failed.
        self.assertEqual(len(slow_ws.close_calls), 1)
        self.assertEqual(slow_ws.close_calls[0], (1000, "room_ended"))
        # The ws should also be removed from tracking.
        self.assertNotIn(slow_ws, manager.connections_by_room.get(("org", "room"), set()))
        # Restore original for hygiene (not strictly needed since fresh instance).
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
        manager = ConnectionManager()
        ps = _fresh_pubsub()
        await ps.start()
        await asyncio.sleep(0.02)

        # Force ensure_subscription to raise.
        original_ensure = ps.ensure_subscription

        async def failing_ensure(org, room):  # type: ignore[no-untyped-def]
            # Simulate the rollback ensure_subscription does on exception.
            async with ps._lock:
                # We simulate the ref++ and rollback.
                pass
            raise RuntimeError("simulated redis fail")

        ps.ensure_subscription = failing_ensure  # type: ignore[assignment]
        with patch("app.socket_manager.pubsub", ps):
            ws = MockWebSocket()
            with self.assertRaises(Exception):
                await manager.register_host(ws, "org", "room")
            # No ownership because ensure raised before we recorded it.
            self.assertNotIn(ws, manager.host_subscription_owned_by_ws)
            # No presence either (rolled back by register_host's except).
            self.assertNotIn(ws, manager.host_presence_by_ws)
            # No refcount leak.
            self.assertNotIn(("org", "room"), ps._ref_counts)
        ps.ensure_subscription = original_ensure  # type: ignore[assignment]
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


if __name__ == "__main__":
    unittest.main()
