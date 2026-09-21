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
import json
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


class _StubRedis:
    """Test double for ``redis.asyncio.Redis`` — the object type
    ``RedisPubSub.start()`` constructs via ``aioredis.Redis(...)``.

    ``ping_mode`` selects the failure shape:
      - ``ok``      : returns True (Redis is reachable).
      - ``refused`` : raises ``ConnectionError`` (server unreachable
        — the connection-refused case).
      - ``hang``    : awaits an event that never fires (the "TCP
        accepted but server never replies" case, which is exactly
        what ``socket_connect_timeout`` does NOT bound — this is the
        scenario the ``asyncio.wait_for`` wrapper exists for).
    """

    def __init__(self, ping_mode: str = "ok", **kwargs):
        self.kwargs = kwargs
        self.ping_mode = ping_mode
        self.closed = False
        self._pubsub_obj = _StubPubSub()
        self._never = asyncio.Event()  # never set → hang forever

    async def ping(self):
        if self.ping_mode == "ok":
            return True
        if self.ping_mode == "refused":
            raise ConnectionError("Connection refused (stub)")
        if self.ping_mode == "hang":
            await self._never.wait()
            return True  # unreachable
        raise RuntimeError(f"unknown ping_mode {self.ping_mode}")

    def pubsub(self, **_kwargs):
        return self._pubsub_obj

    async def aclose(self):
        self.closed = True

    async def close(self):
        self.closed = True


class _StubPubSub:
    def __init__(self):
        self.closed = False
        self.subscribed = []

    async def subscribe(self, *channels):
        self.subscribed.extend(channels)

    async def unsubscribe(self, *channels):
        for c in channels:
            try:
                self.subscribed.remove(c)
            except ValueError:
                pass

    async def get_message(self, **_kwargs):
        await asyncio.sleep(0.05)
        return None

    async def aclose(self):
        self.closed = True


class RedisPubSubRealStartTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the REAL ``start()`` code path (not the ``_fake_start``
    shortcut used by the dispatch tests).

    Motivation: reviewer flagged that the nine dispatch tests replace
    ``start()`` wholesale, so their pass/fail state says nothing about
    the changed startup method. These tests cover:

      1. ping success → connected, started, reader task running.
      2. ping connection-refused → NOT connected, still started,
         reader task STILL scheduled (the F-27 requirement).
      3. ping hang → wait_for aborts within the command timeout,
         reader task STILL scheduled (the blocker-A requirement).
      4. ``stop()`` cleans up the partial-client state that a
         ping-failing ``start()`` leaves behind — under the old
         ``if not self._started: return`` early-out this leaked.
    """

    def setUp(self):
        # Small timeout so the hang test doesn't sit for 5s of wall
        # clock. Restored in tearDown.
        self._orig_cmd_timeout = ENV.REDIS_COMMAND_TIMEOUT_SEC
        self._orig_connect_timeout = ENV.REDIS_CONNECT_TIMEOUT_SEC
        ENV.REDIS_COMMAND_TIMEOUT_SEC = 0.3
        ENV.REDIS_CONNECT_TIMEOUT_SEC = 0.3

    def tearDown(self):
        ENV.REDIS_COMMAND_TIMEOUT_SEC = self._orig_cmd_timeout
        ENV.REDIS_CONNECT_TIMEOUT_SEC = self._orig_connect_timeout

    def _make_started(self, ping_mode: str):
        """Return a fresh RedisPubSub with _enabled=True and
        ``redis.asyncio.Redis`` patched to yield a _StubRedis with
        the requested ping_mode. Caller runs ``await ps.start()``."""
        from app.services.redis_pubsub import RedisPubSub

        ps = RedisPubSub()
        ps._enabled = True

        # Track every constructed stub so the test can assert
        # partial-cleanup behaviour after ``stop()``.
        constructed: list[_StubRedis] = []

        def _factory(**kwargs):
            stub = _StubRedis(ping_mode=ping_mode, **kwargs)
            constructed.append(stub)
            return stub

        import redis.asyncio as aioredis
        self._orig_redis_cls = aioredis.Redis
        aioredis.Redis = _factory  # type: ignore[assignment]
        self._aioredis = aioredis
        self._constructed = constructed
        return ps

    async def asyncTearDown(self):
        # Always restore aioredis.Redis even if the test raised.
        if getattr(self, "_aioredis", None) is not None:
            self._aioredis.Redis = self._orig_redis_cls  # type: ignore[assignment]

    async def test_real_start_ping_ok(self):
        ps = self._make_started("ok")
        try:
            await ps.start()
            # ping succeeded → connected, started, reader task exists.
            self.assertTrue(ps._started)
            self.assertTrue(ps._connected)
            self.assertIsNotNone(ps._reader_task)
            self.assertFalse(ps._reader_task.done())
        finally:
            await ps.stop()

    async def test_real_start_ping_refused_still_schedules_reader(self):
        """Connection-refused shape (Redis unreachable). The reader
        task MUST still be scheduled — that's the whole point of the
        startup-recovery fix, and the exact regression F-27 covers.
        Under the pre-fix code this would leave ``_started=True`` with
        no reader task and no recovery path."""
        ps = self._make_started("refused")
        try:
            await ps.start()
            self.assertTrue(ps._started, "_started must flip so re-entry is idempotent")
            self.assertFalse(ps._connected, "ping raised → not connected")
            self.assertIsNotNone(
                ps._reader_task,
                "reader task MUST be scheduled even when initial ping fails — "
                "this is the F-27 regression assertion",
            )
            self.assertFalse(ps._reader_task.done())
        finally:
            await ps.stop()

    async def test_real_start_ping_hang_bounded_by_wait_for(self):
        """The critical blocker-A shape: server accepts TCP but never
        replies to PING. ``socket_connect_timeout`` does NOT bound this
        — only the ``asyncio.wait_for`` wrapper does. Without the
        wrapper this test would hang until unittest's own timeout."""
        ps = self._make_started("hang")
        try:
            t0 = asyncio.get_event_loop().time()
            await ps.start()
            elapsed = asyncio.get_event_loop().time() - t0
            # Must complete within a small multiple of the command
            # timeout (0.3s) — anything close to 5s would mean the
            # wait_for wrapper is missing.
            self.assertLess(
                elapsed, 2.0,
                f"start() should return within a small multiple of "
                f"REDIS_COMMAND_TIMEOUT_SEC ({ENV.REDIS_COMMAND_TIMEOUT_SEC}s); "
                f"took {elapsed:.2f}s — is asyncio.wait_for wrapping ping?",
            )
            self.assertTrue(ps._started)
            self.assertFalse(ps._connected)
            self.assertIsNotNone(
                ps._reader_task,
                "reader task must still be scheduled after ping timeout — "
                "otherwise the recovery path is unreachable",
            )
        finally:
            await ps.stop()

    async def test_stop_after_failed_ping_cleans_partial_clients(self):
        """``stop()`` must release the pub/sub client objects even in
        the ping-failed shape. Under the old ``if not self._started:
        return`` guard this branch leaked whatever ``start()`` had
        constructed. The reviewer flagged partial-client cleanup
        specifically."""
        ps = self._make_started("refused")
        await ps.start()
        # start() constructed pub + sub (2 stubs); pubsub was derived
        # from sub, so also expect the pubsub object to close.
        self.assertEqual(len(self._constructed), 2)
        pubsub_stub = ps._pubsub
        await ps.stop()
        # All three client refs must be dropped.
        self.assertIsNone(ps._pub)
        self.assertIsNone(ps._sub)
        self.assertIsNone(ps._pubsub)
        # And the underlying stubs must have been closed.
        for stub in self._constructed:
            self.assertTrue(stub.closed, "pub/sub client not closed on stop()")
        self.assertTrue(pubsub_stub.closed, "pubsub not closed on stop()")


class RedisPubSubStructuredEmissionTests(unittest.IsolatedAsyncioTestCase):
    """PR #31 §3 W1 — structured JSON event emission on stdout.

    Every adapter operational event goes through the module-level
    `_emit()` helper, which writes ONE JSON line per event via
    `print()` so:
      1. Cloud Run's log parser reliably produces one `jsonPayload`
         entry with `event`, `severity`, `instance_id`,
         `schema_version`, and `component` labels for the metric
         adapter to extract.
      2. The event bypasses `logging` entirely — the exact pitfall
         PR #32's F-27 hit under uvicorn's `logging.config.dictConfig`.
      3. The `message` field preserves the previous
         `log.warning("redis pubsub …")` substrings so F-27's
         log-file grep keeps working during the transition.

    These tests capture stdout during the exact code paths PR #31
    §3's event catalogue names and assert the JSON schema shape.
    """

    def setUp(self):
        self._orig_cmd_timeout = ENV.REDIS_COMMAND_TIMEOUT_SEC
        self._orig_connect_timeout = ENV.REDIS_CONNECT_TIMEOUT_SEC
        ENV.REDIS_COMMAND_TIMEOUT_SEC = 0.3
        ENV.REDIS_CONNECT_TIMEOUT_SEC = 0.3

    def tearDown(self):
        ENV.REDIS_COMMAND_TIMEOUT_SEC = self._orig_cmd_timeout
        ENV.REDIS_CONNECT_TIMEOUT_SEC = self._orig_connect_timeout
        if getattr(self, "_aioredis", None) is not None:
            self._aioredis.Redis = self._orig_redis_cls  # type: ignore[assignment]

    def _install_fake_redis(self, ping_mode: str):
        """Same infra as `RedisPubSubRealStartTests` — patch
        `redis.asyncio.Redis` with a `_StubRedis` shim so `start()`
        can run without a real Redis instance."""
        from app.services.redis_pubsub import RedisPubSub

        ps = RedisPubSub()
        ps._enabled = True

        def _factory(**kwargs):
            return _StubRedis(ping_mode=ping_mode, **kwargs)

        import redis.asyncio as aioredis
        self._orig_redis_cls = aioredis.Redis
        aioredis.Redis = _factory  # type: ignore[assignment]
        self._aioredis = aioredis
        return ps

    def _capture_stdout_json_lines(self):
        """Context manager that captures stdout and returns the list
        of parsed JSON lines emitted during the block. Non-JSON lines
        are silently ignored — the harness prints its own diagnostics
        alongside the adapter's events."""
        import contextlib
        import io
        buf = io.StringIO()

        class _Capture:
            def __init__(self, buf):
                self.buf = buf
                self._entered = False

            def __enter__(self):
                self._cm = contextlib.redirect_stdout(self.buf)
                self._cm.__enter__()
                return self

            def __exit__(self, *args):
                return self._cm.__exit__(*args)

            @property
            def events(self):
                out = []
                for line in self.buf.getvalue().splitlines():
                    line = line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
                return out

        return _Capture(buf)

    def _assert_common_shape(self, event: dict, *, event_name: str, severity: str):
        """Every event MUST carry the six schema-required fields.
        Missing any of these breaks the metric-label extraction and
        the log-based-metric filters — this shape is contract."""
        self.assertEqual(event.get("event"), event_name)
        self.assertEqual(event.get("severity"), severity)
        self.assertEqual(event.get("component"), "redis_pubsub")
        self.assertEqual(event.get("schema_version"), 1)
        self.assertIn("instance_id", event)
        self.assertTrue(event.get("instance_id"))
        self.assertIn("ts", event)
        self.assertTrue(event.get("ts"))

    async def test_started_event_on_successful_ping(self):
        ps = self._install_fake_redis("ok")
        try:
            with self._capture_stdout_json_lines() as cap:
                await ps.start()
            events = cap.events
        finally:
            await ps.stop()

        started = [e for e in events if e.get("event") == "redis_pubsub_started"]
        self.assertEqual(len(started), 1, f"expected 1 started event, got {len(started)}: {events!r}")
        e = started[0]
        self._assert_common_shape(e, event_name="redis_pubsub_started", severity="INFO")
        # Field-level contract from PR #31 §3 W1.
        self.assertEqual(e.get("host"), ENV.REDIS_HOST)
        self.assertEqual(e.get("port"), ENV.REDIS_PORT)
        self.assertEqual(e.get("prefix"), ENV.REDIS_CHANNEL_PREFIX)
        # `message` preserves the F-27 substring.
        self.assertIn("redis pubsub started", e.get("message", ""))

    async def test_initial_connect_failed_refused_event(self):
        """`reason=refused` shape — the ping raised. Reader task
        still scheduled (asserted separately in
        RedisPubSubRealStartTests); here we only check the event."""
        ps = self._install_fake_redis("refused")
        try:
            with self._capture_stdout_json_lines() as cap:
                await ps.start()
            events = cap.events
        finally:
            await ps.stop()

        fails = [e for e in events if e.get("event") == "redis_pubsub_initial_connect_failed"]
        self.assertEqual(len(fails), 1, f"expected 1 initial-failure event, got {len(fails)}")
        e = fails[0]
        self._assert_common_shape(
            e, event_name="redis_pubsub_initial_connect_failed", severity="WARNING",
        )
        self.assertEqual(e.get("reason"), "refused")
        self.assertIn("error", e)
        self.assertIn("redis pubsub initial connect failed", e.get("message", ""))

    async def test_initial_connect_failed_timeout_event(self):
        """`reason=timeout` shape — asyncio.wait_for aborted the
        ping. Distinct reason label so A5 can page separately on
        this operational mode."""
        ps = self._install_fake_redis("hang")
        try:
            with self._capture_stdout_json_lines() as cap:
                await ps.start()
            events = cap.events
        finally:
            await ps.stop()

        fails = [e for e in events if e.get("event") == "redis_pubsub_initial_connect_failed"]
        self.assertEqual(len(fails), 1)
        e = fails[0]
        self._assert_common_shape(
            e, event_name="redis_pubsub_initial_connect_failed", severity="WARNING",
        )
        self.assertEqual(e.get("reason"), "timeout")
        self.assertIn("timeout_seconds", e)
        self.assertIn("PING timed out", e.get("message", ""))

    def test_emit_helper_shape_and_field_precedence(self):
        """Direct coverage of the `_emit` helper — captures a single
        emission and confirms every required field is present AND
        that caller-provided fields propagate."""
        from app.services.redis_pubsub import _emit
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _emit(
                "redis_pubsub_started",
                "INFO",
                host="127.0.0.1",
                port=6379,
                prefix="worshiptranslate",
                message="human-readable",
                extra_field="carried through",
            )
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip().startswith("{")]
        self.assertEqual(len(lines), 1)
        e = json.loads(lines[0])
        self._assert_common_shape(
            e, event_name="redis_pubsub_started", severity="INFO",
        )
        self.assertEqual(e["host"], "127.0.0.1")
        self.assertEqual(e["port"], 6379)
        self.assertEqual(e["prefix"], "worshiptranslate")
        self.assertEqual(e["message"], "human-readable")
        self.assertEqual(e["extra_field"], "carried through")

    def test_reserved_schema_fields_survive_kwarg_collisions(self):
        """Reviewer finding — a caller kwarg with the same name as
        a reserved schema field (`component`, `instance_id`,
        `schema_version`, `ts`) MUST NOT override the emitter's
        authoritative value. The metric filters key on these fields;
        letting a misconfigured caller overwrite them would silently
        poison the metric labels.

        `_emit` guarantees this by merging caller-provided `**fields`
        FIRST and the reserved fields LAST, so dict-merge order
        drops the collision. This test attacks all four kwarg-
        reachable reserved names simultaneously.

        `event` and `severity` are positional parameters of `_emit`
        itself — Python's own calling convention raises `TypeError`
        for a `_emit(..., event=..., ...)` collision before the
        function body ever runs. That is covered by the sibling
        test `test_reserved_positional_params_python_rejects_kwarg`."""
        from app.services.redis_pubsub import _emit
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _emit(
                "redis_pubsub_started",
                "INFO",
                # Four kwarg-reachable reserved names, all attacked.
                component="OVERRIDDEN_COMPONENT",
                instance_id="OVERRIDDEN_INSTANCE",
                schema_version=9999,
                ts="OVERRIDDEN_TS",
                # A legitimate field alongside, to prove the
                # emitter still passes through non-reserved kwargs.
                host="127.0.0.1",
            )
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip().startswith("{")]
        self.assertEqual(len(lines), 1)
        e = json.loads(lines[0])
        # Reserved fields keep their authoritative values.
        self.assertEqual(e["event"], "redis_pubsub_started")
        self.assertEqual(e["severity"], "INFO")
        self.assertEqual(
            e["component"], "redis_pubsub",
            f"caller-supplied component={e['component']!r} bypassed reserved-field guard",
        )
        self.assertEqual(
            e["schema_version"], 1,
            f"caller-supplied schema_version={e['schema_version']!r} bypassed reserved-field guard",
        )
        # instance_id + ts are dynamic — assert their overrides did NOT land.
        self.assertNotEqual(
            e["instance_id"], "OVERRIDDEN_INSTANCE",
            "caller-supplied instance_id bypassed reserved-field guard",
        )
        self.assertNotEqual(
            e["ts"], "OVERRIDDEN_TS",
            "caller-supplied ts bypassed reserved-field guard",
        )
        # Non-reserved kwargs still pass through.
        self.assertEqual(e["host"], "127.0.0.1")

    def test_reserved_positional_params_python_rejects_kwarg(self):
        """`event` and `severity` are positional parameters of
        `_emit`, so Python raises `TypeError` when a caller passes
        them as kwargs alongside their positional counterpart. This
        is a stronger guarantee than dict-merge-order for those two
        specific names — the collision is impossible at the calling
        convention level, not just corrected inside the function."""
        from app.services.redis_pubsub import _emit
        with self.assertRaises(TypeError):
            _emit("redis_pubsub_started", "INFO", event="OVERRIDE")
        with self.assertRaises(TypeError):
            _emit("redis_pubsub_started", "INFO", severity="OVERRIDE")

    def test_emit_swallows_stdout_write_failures(self):
        """Reviewer finding — output failures MUST NOT propagate.
        A logging failure interrupting `start()` or the reader-error
        handler would leave the reader task unscheduled or block a
        connection-state transition. The emitter is best-effort by
        contract; losing one telemetry line to a broken stdout is
        strictly better than aborting recovery."""
        from unittest.mock import patch
        from app.services.redis_pubsub import _emit

        # Replace sys.stdout with a stream whose `write` raises.
        class _BrokenStream:
            def write(self, *_a, **_kw):
                raise BrokenPipeError("simulated broken stdout")
            def flush(self, *_a, **_kw):
                raise BrokenPipeError("simulated broken stdout")

        with patch("app.services.redis_pubsub.sys.stdout", _BrokenStream()):
            # Must return normally, not raise.
            try:
                _emit(
                    "redis_pubsub_started",
                    "INFO",
                    host="127.0.0.1",
                    port=6379,
                )
            except Exception as exc:
                self.fail(
                    f"_emit propagated stdout failure ({type(exc).__name__}: {exc}); "
                    f"telemetry must be best-effort per PR #34 review round 2"
                )

    def test_emit_swallows_serialisation_failures(self):
        """A caller passing a non-serialisable value (e.g. an object
        with a circular reference that `default=str` can't reduce)
        also MUST NOT propagate. Same reason as write-failure — we
        never let a logging bug interrupt recovery."""
        from app.services.redis_pubsub import _emit

        class _Unserialisable:
            def __repr__(self):
                raise RuntimeError("cannot repr")
            def __str__(self):
                raise RuntimeError("cannot str")

        try:
            _emit(
                "redis_pubsub_started",
                "INFO",
                bad=_Unserialisable(),
            )
        except Exception as exc:
            self.fail(
                f"_emit propagated serialisation failure ({type(exc).__name__}: {exc}); "
                f"telemetry must be best-effort per PR #34 review round 2"
            )

    async def test_start_succeeds_when_stdout_write_fails(self):
        """Reviewer's exact reproduction — `start()` under a
        BrokenPipeError-raising stdout used to raise and NOT schedule
        the reader task. Fix: emit is best-effort. This regression
        test drives the actual `start()` implementation with a
        successful ping AND a stdout that raises, and asserts the
        reader task IS scheduled and start() returns cleanly."""
        from unittest.mock import patch

        ps = self._install_fake_redis("ok")

        class _BrokenStream:
            def write(self, *_a, **_kw):
                raise BrokenPipeError("simulated broken stdout")
            def flush(self, *_a, **_kw):
                raise BrokenPipeError("simulated broken stdout")

        try:
            with patch("app.services.redis_pubsub.sys.stdout", _BrokenStream()):
                # Must complete normally.
                await ps.start()
            self.assertTrue(ps._started, "_started must flip")
            self.assertTrue(ps._connected, "ping succeeded → _connected")
            self.assertIsNotNone(
                ps._reader_task,
                "reader task MUST be scheduled even when telemetry stdout is broken; "
                "PR #34 review round 2 regression assertion",
            )
            self.assertFalse(ps._reader_task.done())
        finally:
            await ps.stop()

    async def test_reader_error_handler_survives_broken_stdout(self):
        """The reader-loop's exception handler calls `_emit` before
        flipping `_connected = False`. If the emit raised, the
        adapter would never mark itself disconnected and the reader
        would keep hitting the broken client. Fix: emit is
        best-effort; the state transition happens unconditionally.

        This test doesn't reproduce the full reader loop (that
        requires a real pubsub stream); it exercises the emit path
        directly with a broken stream to prove the emitter itself
        does not propagate."""
        from unittest.mock import patch
        from app.services.redis_pubsub import _emit, EVENT_READER_ERROR

        class _BrokenStream:
            def write(self, *_a, **_kw):
                raise BrokenPipeError("simulated broken stdout")
            def flush(self, *_a, **_kw):
                raise BrokenPipeError("simulated broken stdout")

        with patch("app.services.redis_pubsub.sys.stdout", _BrokenStream()):
            # Mimics the reader-loop's exact call.
            _emit(
                EVENT_READER_ERROR,
                "WARNING",
                error="RuntimeError('simulated pubsub reader failure')",
                message="pubsub reader error: simulated pubsub reader failure",
            )
        # Reached this line → no propagation.

    def test_event_name_constants_match_pr31_catalogue(self):
        """The event NAMES are the metric anchor per PR #31 §3. If
        anyone renames one, this test catches it, and the metric
        adapter in `ops/monitoring/reconciler/` must be updated in
        the same PR."""
        from app.services import redis_pubsub as rp
        self.assertEqual(rp.EVENT_STARTED, "redis_pubsub_started")
        self.assertEqual(
            rp.EVENT_INITIAL_CONNECT_FAILED,
            "redis_pubsub_initial_connect_failed",
        )
        self.assertEqual(rp.EVENT_RECONNECTING, "redis_pubsub_reconnecting")
        self.assertEqual(rp.EVENT_RECONNECTED, "redis_pubsub_reconnected")
        self.assertEqual(rp.EVENT_RECONNECT_FAILED, "redis_pubsub_reconnect_failed")
        self.assertEqual(rp.EVENT_READER_ERROR, "redis_pubsub_reader_error")
        # Probe events are declared for the follow-on PR (task #127)
        # but not emitted yet.
        self.assertEqual(rp.EVENT_PROBE_OK, "redis_probe_ok")
        self.assertEqual(rp.EVENT_PROBE_FAILED, "redis_probe_failed")


if __name__ == "__main__":
    unittest.main()
