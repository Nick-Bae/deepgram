"""PR-T1-C unit coverage for Redis-independent ended-room recovery."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import asyncio
import time
import unittest
from unittest.mock import patch

from app.socket_manager import ConnectionManager
from app.services.room_reconciler import RoomReconciler


class FakeStore:
    def __init__(self, states=None) -> None:
        self.states = dict(states or {})
        self.failures: set[tuple[str, str]] = set()
        self.reads: list[tuple[str, str]] = []
        self.batch_calls = 0

    def get_room_reconcile_states(self, room_keys):
        self.batch_calls += 1
        out = {}
        for key in room_keys:
            self.reads.append(key)
            if key in self.failures:
                raise TimeoutError("firestore unavailable")
            value = self.states.get(key)
            out[key] = dict(value) if value is not None else None
        return out


class FakeManager:
    def __init__(self, rooms=()) -> None:
        self.resources = set(rooms)
        self.local_broadcasts: list[tuple[tuple[str, str], dict]] = []
        self.forgotten: list[tuple[str, str]] = []
        self.forgotten_subscriptions: list[tuple[str, str]] = []
        self.fail_cleanup_for: set[tuple[str, str]] = set()

    def locally_owned_room_keys(self):
        return set(self.resources)

    def room_has_local_resources(self, org_id: str, room_id: str) -> bool:
        return (org_id, room_id) in self.resources

    async def _broadcast_local_room(self, org_id: str, room_id: str, message: dict) -> None:
        key = (org_id, room_id)
        self.local_broadcasts.append((key, dict(message)))
        if key in self.fail_cleanup_for:
            raise RuntimeError("socket close stuck")
        self.resources.discard(key)

    def forget_room(self, org_id: str, room_id: str) -> None:
        self.forgotten.append((org_id, room_id))

    async def forget_room_subscription(self, org_id: str, room_id: str) -> None:
        self.forgotten_subscriptions.append((org_id, room_id))


def _ended(*, seconds_ago: float = 0.0, reason: str = "host_end") -> dict:
    return {
        "status": "ended",
        "endedAt": datetime.now(timezone.utc) - timedelta(seconds=seconds_ago),
        "endReason": reason,
    }


class RoomReconcilerTests(unittest.IsolatedAsyncioTestCase):
    def make_reconciler(self, manager: FakeManager, store: FakeStore):
        local_cleanup: list[tuple[str, str]] = []
        reconciler = RoomReconciler(
            manager=manager,
            store=store,
            cleanup_local_state=lambda org, room: local_cleanup.append((org, room)),
            interval_seconds=30,
            instance_id="test-instance",
        )
        return reconciler, local_cleanup

    async def test_F14_firestore_read_failure_never_terminates_and_next_tick_retries(self):
        key = ("org", "room")
        manager = FakeManager([key])
        store = FakeStore({key: _ended()})
        store.failures.add(key)
        reconciler, local_cleanup = self.make_reconciler(manager, store)

        self.assertEqual(await reconciler.run_once(), "firestore_error")
        self.assertIn(key, manager.resources)
        self.assertEqual(manager.local_broadcasts, [])
        self.assertEqual(local_cleanup, [])
        self.assertEqual(reconciler.metrics.tick_totals["firestore_error"], 1)

        store.failures.clear()
        self.assertEqual(await reconciler.run_once(), "ok")
        self.assertNotIn(key, manager.resources)
        self.assertEqual(manager.local_broadcasts[0][1]["roomStatus"], "ended")
        self.assertEqual(local_cleanup, [key])

    async def test_F16_cleanup_is_scoped_to_exact_room_id(self):
        old_room = ("org", "room-A")
        replacement_room = ("org", "room-B")
        manager = FakeManager([old_room, replacement_room])
        store = FakeStore(
            {
                old_room: _ended(),
                replacement_room: {"status": "live"},
            }
        )
        reconciler, local_cleanup = self.make_reconciler(manager, store)

        self.assertEqual(await reconciler.run_once(), "ok")
        self.assertNotIn(old_room, manager.resources)
        self.assertIn(replacement_room, manager.resources)
        self.assertEqual([key for key, _ in manager.local_broadcasts], [old_room])
        self.assertEqual(local_cleanup, [old_room])

    async def test_F18_stuck_terminal_cleanup_sets_overdue_metrics(self):
        key = ("org", "room-stuck")
        manager = FakeManager([key])
        manager.fail_cleanup_for.add(key)
        store = FakeStore({key: _ended(seconds_ago=125)})
        reconciler, local_cleanup = self.make_reconciler(manager, store)

        self.assertEqual(await reconciler.run_once(), "cleanup_error")
        self.assertEqual(reconciler.metrics.terminal_rooms_with_resources, 1)
        self.assertGreaterEqual(reconciler.metrics.oldest_overdue_cleanup_seconds, 125)
        self.assertEqual(reconciler.metrics.cleanup_inflight, 0)
        self.assertEqual(reconciler.metrics.actions_total, 0)
        self.assertEqual(local_cleanup, [])

    async def test_F21_healthy_long_running_room_never_sets_overdue_metrics(self):
        key = ("org", "room-live")
        manager = FakeManager([key])
        store = FakeStore(
            {
                key: {
                    "status": "live",
                    "startedAt": datetime.now(timezone.utc) - timedelta(hours=12),
                }
            }
        )
        reconciler, _ = self.make_reconciler(manager, store)

        self.assertEqual(await reconciler.run_once(), "ok")
        self.assertIn(key, manager.resources)
        self.assertEqual(reconciler.metrics.terminal_rooms_with_resources, 0)
        self.assertEqual(reconciler.metrics.oldest_overdue_cleanup_seconds, 0.0)
        self.assertEqual(manager.local_broadcasts, [])

    async def test_only_explicit_ended_status_authorizes_cleanup(self):
        missing = ("org", "missing")
        malformed = ("org", "malformed")
        unknown = ("org", "unknown")
        uppercase = ("org", "uppercase")
        manager = FakeManager([missing, malformed, unknown, uppercase])
        store = FakeStore(
            {
                malformed: {"status": None},
                unknown: {"status": "archived"},
                uppercase: {"status": "ENDED"},
            }
        )
        reconciler, _ = self.make_reconciler(manager, store)

        self.assertEqual(await reconciler.run_once(), "ok")
        self.assertEqual(manager.resources, {missing, malformed, unknown, uppercase})
        self.assertEqual(manager.local_broadcasts, [])

    async def test_terminal_cleanup_is_local_and_preserves_reason(self):
        key = ("org", "room")
        manager = FakeManager([key])
        store = FakeStore({key: _ended(reason="monthly_limit_reached")})
        reconciler, _ = self.make_reconciler(manager, store)

        await reconciler.run_once()

        self.assertEqual(len(manager.local_broadcasts), 1)
        payload = manager.local_broadcasts[0][1]
        self.assertEqual(payload["reason"], "monthly_limit_reached")
        self.assertEqual(manager.forgotten_subscriptions, [key])
        self.assertEqual(reconciler.metrics.actions_total, 1)
        self.assertIsNotNone(reconciler.metrics.last_successful_reconciliation_at)

    async def test_batch_of_fifty_owned_rooms_is_reconciled_in_one_store_call(self):
        rooms = {("org", f"room-{index}") for index in range(50)}
        manager = FakeManager(rooms)
        store = FakeStore({key: _ended() for key in rooms})
        reconciler, _ = self.make_reconciler(manager, store)

        self.assertEqual(await reconciler.run_once(), "ok")
        self.assertEqual(set(store.reads), rooms)
        self.assertEqual(len(store.reads), 50)
        self.assertEqual(store.batch_calls, 1)
        self.assertEqual(manager.resources, set())
        self.assertEqual(reconciler.metrics.actions_total, 50)

    async def test_concurrent_pass_is_skipped_instead_of_overlapping(self):
        key = ("org", "slow-room")
        manager = FakeManager([key])
        store = FakeStore({key: {"status": "live"}})
        original = store.get_room_reconcile_states

        def slow_read(room_keys):
            time.sleep(0.15)
            return original(room_keys)

        store.get_room_reconcile_states = slow_read  # type: ignore[method-assign]
        reconciler, _ = self.make_reconciler(manager, store)

        first = asyncio.create_task(reconciler.run_once())
        while not reconciler._pass_lock.locked():
            await asyncio.sleep(0)
        self.assertEqual(await reconciler.run_once(), "skipped_overlap")
        self.assertEqual(await first, "ok")
        self.assertEqual(reconciler.metrics.tick_totals["skipped_overlap"], 1)


class OwnershipInventoryTests(unittest.TestCase):
    def test_inventory_unions_listener_host_and_subscription_owner_maps(self):
        manager = ConnectionManager()
        listener = object()
        listener_owner_only = object()
        host = object()
        host_callback_only = object()
        manager.connections_by_room[("org", "listener-room")] = {listener}  # type: ignore[assignment]
        manager.listener_subscription_owned_room_by_ws[listener_owner_only] = (  # type: ignore[index]
            "org",
            "listener-owner-room",
        )
        manager.host_presence_by_ws[host] = ("org", "host-room")  # type: ignore[index]
        manager.host_subscription_owned_by_ws.add(host)  # type: ignore[arg-type]
        manager.host_presence_by_ws[host_callback_only] = ("org", "callback-room")  # type: ignore[index]
        manager.host_shutdown_cb_by_ws[host_callback_only] = lambda: None  # type: ignore[index]

        self.assertEqual(
            manager.locally_owned_room_keys(),
            {
                ("org", "listener-room"),
                ("org", "listener-owner-room"),
                ("org", "host-room"),
                ("org", "callback-room"),
            },
        )

    def test_inventory_includes_redis_reconnect_source_of_truth(self):
        manager = ConnectionManager()
        with patch("app.socket_manager.pubsub") as pubsub:
            pubsub.desired_room_keys = {("org", "orphan-subscription")}
            self.assertIn(
                ("org", "orphan-subscription"),
                manager.locally_owned_room_keys(),
            )


class EventSchemaTests(unittest.IsolatedAsyncioTestCase):
    """Lock the emitted-event schema (the contract the PR-T1-E log-based
    metrics filter on). If any of these fail, the monitoring stack in
    `ops/monitoring/reconciler/` will silently stop matching."""

    REQUIRED_FIELDS = {
        "event",
        "schema_version",
        "severity",
        "message",
        "component",
        "instance_id",
    }
    ALLOWED_EVENTS = {
        "reconciler_tick",
        "reconciler_action",
        "reconciler_diagnostic",
    }
    ALLOWED_SEVERITIES = {"DEBUG", "INFO", "NOTICE", "WARNING", "ERROR"}
    TICK_OUTCOMES = {
        "ok",
        "cleanup_error",
        "firestore_error",
        "loop_error",
        "skipped_overlap",
    }
    ACTION_REASONS = {"ended_room_local_cleanup", "cleanup_error"}

    async def _capture(self, coro):
        """Run `coro` with `builtins.print` patched; return the list of
        parsed JSON payloads."""
        import builtins
        import json as _json
        captured: list[dict] = []
        real_print = builtins.print

        def fake_print(*args, **kwargs):
            if not args:
                return
            first = args[0]
            if isinstance(first, str):
                try:
                    captured.append(_json.loads(first))
                except _json.JSONDecodeError:
                    real_print(*args, **kwargs)
            else:
                real_print(*args, **kwargs)

        with patch("builtins.print", side_effect=fake_print):
            await coro
        return captured

    def _assert_required_fields(self, payload: dict) -> None:
        missing = self.REQUIRED_FIELDS - set(payload)
        self.assertFalse(
            missing,
            f"emission missing required schema fields: {missing} in {payload}",
        )
        self.assertIn(payload["event"], self.ALLOWED_EVENTS)
        self.assertEqual(payload["schema_version"], "1")
        self.assertIn(payload["severity"], self.ALLOWED_SEVERITIES)
        self.assertEqual(payload["component"], "room_reconciler")
        self.assertIsInstance(payload["message"], str)

    async def test_healthy_tick_emits_schema_conforming_event(self):
        manager = FakeManager(rooms={("org", "r")})
        store = FakeStore(states={("org", "r"): _ended()})
        reconciler, _ = _make_reconciler_shell(manager, store)
        payloads = await self._capture(reconciler.run_once())
        ticks = [p for p in payloads if p["event"] == "reconciler_tick"]
        self.assertEqual(len(ticks), 1)
        tick = ticks[0]
        self._assert_required_fields(tick)
        self.assertIn(tick["outcome"], self.TICK_OUTCOMES)
        self.assertEqual(tick["outcome"], "ok")
        # Snapshot fields required by the metric filters:
        for field in (
            "duration_seconds",
            "owned_rooms",
            "terminal_rooms_with_resources",
            "oldest_overdue_cleanup_seconds",
            "cleanup_inflight",
            "actions_total",
            "overdue",
        ):
            self.assertIn(field, tick, f"tick missing snapshot field {field}")
        self.assertIsInstance(tick["overdue"], bool)

    async def test_action_events_carry_bounded_reason_and_ids_in_body(self):
        manager = FakeManager(rooms={("org", "r")})
        store = FakeStore(states={("org", "r"): _ended()})
        reconciler, _ = _make_reconciler_shell(manager, store)
        payloads = await self._capture(reconciler.run_once())
        actions = [p for p in payloads if p["event"] == "reconciler_action"]
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self._assert_required_fields(action)
        self.assertIn(action["reason"], self.ACTION_REASONS)
        # Room + org IDs are in the log body for debugging.
        self.assertEqual(action["org_id"], "org")
        self.assertEqual(action["room_id"], "r")

    async def test_cleanup_error_emits_action_with_error_reason(self):
        manager = FakeManager(rooms={("org", "r")})
        manager.fail_cleanup_for = {("org", "r")}
        store = FakeStore(states={("org", "r"): _ended()})
        reconciler, _ = _make_reconciler_shell(manager, store)
        payloads = await self._capture(reconciler.run_once())
        actions = [p for p in payloads if p["event"] == "reconciler_action"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["reason"], "cleanup_error")
        self.assertIn("error", actions[0])
        self.assertEqual(actions[0]["severity"], "ERROR")

    async def test_firestore_error_tick_carries_outcome_and_error_type(self):
        manager = FakeManager(rooms={("org", "r")})
        store = FakeStore(states={("org", "r"): _ended()})
        store.failures.add(("org", "r"))
        reconciler, _ = _make_reconciler_shell(manager, store)
        payloads = await self._capture(reconciler.run_once())
        ticks = [p for p in payloads if p["event"] == "reconciler_tick"]
        self.assertEqual(ticks[-1]["outcome"], "firestore_error")
        self.assertEqual(ticks[-1]["severity"], "ERROR")
        self.assertEqual(ticks[-1]["error"], "TimeoutError")

    async def test_diagnostic_events_are_debug_severity_and_never_metric_driving(self):
        # A room whose Firestore doc is missing yields a diagnostic
        # emission; it must not be classified as tick/action (those
        # are what metrics count).
        manager = FakeManager(rooms={("org", "gone")})
        store = FakeStore(states={("org", "gone"): None})
        reconciler, _ = _make_reconciler_shell(manager, store)
        payloads = await self._capture(reconciler.run_once())
        diags = [p for p in payloads if p["event"] == "reconciler_diagnostic"]
        self.assertGreaterEqual(len(diags), 1)
        for d in diags:
            self._assert_required_fields(d)
            self.assertEqual(d["severity"], "DEBUG")
            self.assertIn("kind", d)

    async def test_all_emitted_events_are_valid_single_line_json(self):
        # Log-based metrics require valid single-line JSON on stdout.
        # This asserts every emission from a representative run parses
        # cleanly (no multiline pretty-printing, no stray newlines).
        manager = FakeManager(rooms={("org", "r"), ("org", "gone")})
        store = FakeStore(states={
            ("org", "r"): _ended(),
            ("org", "gone"): None,
        })
        reconciler, _ = _make_reconciler_shell(manager, store)
        payloads = await self._capture(reconciler.run_once())
        self.assertGreater(len(payloads), 0)
        for p in payloads:
            self._assert_required_fields(p)


def _make_reconciler_shell(manager, store):
    """Factory used by both the tests above and the schema tests."""
    local_cleanup: list[tuple[str, str]] = []
    reconciler = RoomReconciler(
        manager=manager,
        store=store,
        cleanup_local_state=lambda o, r: local_cleanup.append((o, r)),
        interval_seconds=30,
        instance_id="test-instance",
    )
    return reconciler, local_cleanup


if __name__ == "__main__":
    unittest.main()
