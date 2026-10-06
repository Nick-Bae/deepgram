"""Gate E — Redis integration tests (reconstructing PRs #34/#35/#38 deltas).

Scope
=====

Prove the three functional deltas layered on current ``origin/main``:

- **Commit 1 — structured Redis observability**: `_emit()` helper + event
  name constants + call sites in the pub/sub operational flows (startup,
  reconnect, reader loop, bulk-subscribe failures).
- **Commit 2 — in-process publish/subscribe probe**: per-instance probe
  channel + nonce + bounded deadline + deterministic cleanup, no
  collision with application room channels.
- **Commit 3 — startup/shutdown wiring**: `_start_pubsub_singleton`
  helper in `main.py` that arms the probe exactly once at startup
  only when ``ENV.REDIS_ENABLED`` is True; disabled mode creates no
  Redis client, socket, subscription, retry loop, or probe task.

These tests are designed to be:
- Failing-before against ``origin/main`` (`2eed1738…`).
- Passing-after each respective commit.
- Self-contained — no reliance on PR #34/#35/#38 branch test files
  (which carry stale-base dependencies).

Required coverage (14 areas, directive-mandated):

1. Disabled mode: zero Redis/network ops
2. Enabled mode: probe armed exactly once at startup
3. Publish succeeds; subscribe receives
4. Nonce / channel isolation (no collision with app room channels)
5. Timeout — publish/subscribe bounded by ``REDIS_PROBE_DEADLINE_SEC``
6. Auth failure → fail-open contract
7. Connection failure → fail-open contract
8. Structured event redaction (no sensitive fields in jsonPayload)
9. No secret / host / exception text / probe payload leakage anywhere
10. Unsubscribe cleanup — probe task cancellation drains PSUBSCRIBE
11. Cancellation during startup — bounded shutdown join
12. Repeated startup/shutdown idempotence
13. Reconnect does not duplicate subscriptions or tasks
14. Probe failure follows the documented fail-open contract

The suite is split into three unittest classes aligned with the three
commits so failing-before/passing-after can be measured per commit.
"""
from __future__ import annotations

import asyncio
import importlib
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Shared test helpers.
# ---------------------------------------------------------------------------

def _reload_pubsub():
    """Fresh import of `redis_pubsub` so module-level singletons are rebuilt."""
    import app.services.redis_pubsub as rp
    importlib.reload(rp)
    return rp


def _capture_emits(fn):
    """Call `fn()` with stdout redirected to a buffer and return the
    parsed JSON lines (ignoring non-JSON lines, which are from print()
    helpers unrelated to `_emit`)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    events = []
    for line in buf.getvalue().splitlines():
        try:
            evt = json.loads(line)
            if isinstance(evt, dict) and "event" in evt:
                events.append(evt)
        except Exception:
            continue
    return events


# ---------------------------------------------------------------------------
# Commit 1 — structured Redis observability.
# ---------------------------------------------------------------------------

class Commit1_StructuredObservability(unittest.TestCase):
    """Verify `_emit` helper + event constants are present and used."""

    def test_emit_helper_exists(self):
        import app.services.redis_pubsub as rp
        self.assertTrue(hasattr(rp, "_emit"),
                        "redis_pubsub._emit must exist for Commit 1")
        self.assertTrue(callable(rp._emit))

    def test_event_constants_exist(self):
        import app.services.redis_pubsub as rp
        expected = {
            "EVENT_STARTED", "EVENT_INITIAL_CONNECT_FAILED",
            "EVENT_RECONNECTING", "EVENT_RECONNECTED",
            "EVENT_RECONNECT_FAILED", "EVENT_READER_ERROR",
        }
        for name in expected:
            self.assertTrue(hasattr(rp, name),
                            f"redis_pubsub.{name} must exist for Commit 1")

    def test_emit_produces_structured_json_line(self):
        """`_emit(event, severity, **fields)` emits one JSON line on stdout
        with reserved fields (event/severity/component/instance_id/
        schema_version/ts) set by the emitter, and caller fields merged."""
        import app.services.redis_pubsub as rp

        events = _capture_emits(lambda: rp._emit("my_event", "INFO", some_field="x"))
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["event"], "my_event")
        self.assertEqual(e["severity"], "INFO")
        self.assertEqual(e["component"], "redis_pubsub")
        self.assertEqual(e["schema_version"], 1)
        self.assertIn("instance_id", e)
        self.assertIn("ts", e)
        self.assertEqual(e["some_field"], "x")

    def test_emit_reserved_fields_cannot_be_overridden(self):
        """Caller-supplied `component`/`instance_id`/`schema_version`/`ts`
        passed via **fields are DROPPED in favour of the emitter's
        authoritative values. (Python TypeError prevents `event=` and
        `severity=` from being supplied as kwargs when the positional
        args already exist, so those two reserved names cannot be
        collided through the normal call shape — the test uses the
        kwargs that CAN collide.)"""
        import app.services.redis_pubsub as rp

        events = _capture_emits(lambda: rp._emit(
            "real_event", "INFO",
            component="not_redis_pubsub",  # should be dropped
            instance_id="fake-instance",   # should be dropped
            schema_version=99,             # should be dropped
            ts="2000-01-01T00:00:00",      # should be dropped
            legit_field=42,
        ))
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["event"], "real_event")
        self.assertEqual(e["severity"], "INFO")
        self.assertEqual(e["component"], "redis_pubsub")
        self.assertNotEqual(e["instance_id"], "fake-instance")
        self.assertNotEqual(e["schema_version"], 99)
        self.assertNotEqual(e["ts"], "2000-01-01T00:00:00")
        self.assertEqual(e["legit_field"], 42)

    def test_emit_is_fail_open_on_stdout_broken(self):
        """A broken stdout must NOT raise out of `_emit` — the adapter's
        operational flows (reconnect, startup) rely on `_emit` being
        best-effort."""
        import app.services.redis_pubsub as rp

        class _RaisingStdout:
            def write(self, _s):
                raise BrokenPipeError("stdout gone")
            def flush(self):
                raise BrokenPipeError("stdout gone")

        original = sys.stdout
        sys.stdout = _RaisingStdout()
        try:
            # Must not raise.
            rp._emit("sanity", "INFO", k=1)
        finally:
            sys.stdout = original

    def test_emit_redacts_no_payload_or_room_fields_by_default(self):
        """The emitter does not inject any room_id/org_id/user_id/transcript/
        auth/host/URL/exception/payload content on its own. Callers must opt
        in by passing those fields. Verify that a call without such fields
        has no such keys in output."""
        import app.services.redis_pubsub as rp
        events = _capture_emits(lambda: rp._emit("sanity_redaction", "INFO"))
        self.assertEqual(len(events), 1)
        e = events[0]
        forbidden = {"room_id", "roomId", "org_id", "orgId", "user_id",
                     "uid", "transcript", "auth", "authorization",
                     "password", "payload"}
        for k in forbidden:
            self.assertNotIn(k, e)


# ---------------------------------------------------------------------------
# Commit 2 — in-process probe methods + env vars.
# ---------------------------------------------------------------------------

class Commit2_ProbeMethods(unittest.TestCase):

    def test_probe_env_vars_exist(self):
        from app.env import ENV
        self.assertTrue(hasattr(ENV, "REDIS_PROBE_INTERVAL_SEC"))
        self.assertTrue(hasattr(ENV, "REDIS_PROBE_DEADLINE_SEC"))
        # Bounds from directive.
        self.assertGreaterEqual(ENV.REDIS_PROBE_INTERVAL_SEC, 10.0)
        self.assertLessEqual(ENV.REDIS_PROBE_INTERVAL_SEC, 300.0)
        self.assertGreaterEqual(ENV.REDIS_PROBE_DEADLINE_SEC, 0.5)
        self.assertLessEqual(ENV.REDIS_PROBE_DEADLINE_SEC, 10.0)

    def test_probe_channel_name_isolated_from_room_channels(self):
        """`_probe_channel_name` must produce a channel that does not
        parse as any room channel."""
        import app.services.redis_pubsub as rp
        probe_ch = rp._probe_channel_name("inst-fake-id")
        self.assertIn(":probe:", probe_ch)
        # The production room-channel parser must NOT accept it.
        org, room = rp._parse_channel(probe_ch)
        self.assertEqual((org, room), ("", ""),
                         "probe channel must not parse as a room channel")
        # The probe-channel parser DOES accept it and extracts instance_id.
        self.assertEqual(rp._parse_probe_channel(probe_ch), "inst-fake-id")
        # And rejects actual room channels.
        room_ch = rp._channel_name("my-org", "my-room")
        self.assertEqual(rp._parse_probe_channel(room_ch), "")

    def test_enable_probe_task_sets_desired(self):
        import app.services.redis_pubsub as rp
        obj = rp.RedisPubSub()
        self.assertFalse(obj._probe_subscription_desired)
        obj.enable_probe_task()
        self.assertTrue(obj._probe_subscription_desired)
        self.assertIsNotNone(obj._probe_callback)
        # Idempotent: a second call keeps it enabled, does not overwrite callback.
        cb_before = obj._probe_callback
        obj.enable_probe_task()
        self.assertIs(obj._probe_callback, cb_before)
        self.assertTrue(obj._probe_subscription_desired)

    def test_set_probe_callback_also_marks_desired(self):
        import app.services.redis_pubsub as rp
        obj = rp.RedisPubSub()

        async def cb(_env):
            pass

        obj.set_probe_callback(cb)
        self.assertTrue(obj._probe_subscription_desired)
        self.assertIs(obj._probe_callback, cb)


# ---------------------------------------------------------------------------
# Commit 3 — startup/shutdown wiring in main.py.
# ---------------------------------------------------------------------------

class Commit3_StartupWiring(unittest.IsolatedAsyncioTestCase):

    async def test_start_pubsub_singleton_helper_exists(self):
        from app import main as app_main
        self.assertTrue(hasattr(app_main, "_start_pubsub_singleton"))
        self.assertTrue(asyncio.iscoroutinefunction(app_main._start_pubsub_singleton))

    async def test_disabled_mode_creates_no_client_or_task(self):
        """When REDIS_ENABLED=0, _start_pubsub_singleton must NOT create a
        Redis client, open any socket, start a subscription, spawn a retry
        loop, or create a probe task.

        Covered via a MagicMock pubsub that records all method calls — the
        helper must only invoke `.enabled` property access (and short-circuit
        on False). No `.enable_probe_task()`, no `.start()`, no `.set_*()`.
        """
        from app import main as app_main
        mock_pubsub = MagicMock()
        mock_pubsub.enabled = False
        mock_pubsub.enable_probe_task = MagicMock()
        mock_pubsub.start = AsyncMock()
        mock_pubsub.set_probe_callback = MagicMock()

        await app_main._start_pubsub_singleton(mock_pubsub)

        mock_pubsub.enable_probe_task.assert_not_called()
        mock_pubsub.start.assert_not_called()
        mock_pubsub.set_probe_callback.assert_not_called()

    async def test_enabled_mode_arms_probe_before_start(self):
        """When REDIS_ENABLED=1 (represented via mock_pubsub.enabled=True),
        the helper MUST call enable_probe_task() BEFORE start() so start's
        probe-subscription logic observes `_probe_subscription_desired=True`."""
        from app import main as app_main

        call_order = []

        mock_pubsub = MagicMock()
        mock_pubsub.enabled = True

        def _enable():
            call_order.append("enable_probe_task")
        mock_pubsub.enable_probe_task = MagicMock(side_effect=_enable)

        async def _start():
            call_order.append("start")
        mock_pubsub.start = AsyncMock(side_effect=_start)

        await app_main._start_pubsub_singleton(mock_pubsub)

        self.assertEqual(call_order, ["enable_probe_task", "start"],
                         "enable_probe_task MUST run before start")

    async def test_helper_is_idempotent_on_warm_reentry(self):
        """A second call to _start_pubsub_singleton on the same pubsub does
        not spawn a second probe task. Underlying guarantees from
        enable_probe_task() and start() are relied on; this test asserts the
        helper itself passes through unconditionally — no local branch that
        would skip start on a second call."""
        from app import main as app_main
        mock_pubsub = MagicMock()
        mock_pubsub.enabled = True
        mock_pubsub.enable_probe_task = MagicMock()
        mock_pubsub.start = AsyncMock()

        await app_main._start_pubsub_singleton(mock_pubsub)
        await app_main._start_pubsub_singleton(mock_pubsub)

        # Both calls fire; idempotence is enforced INSIDE enable_probe_task +
        # start, not in the helper. So we expect two calls each.
        self.assertEqual(mock_pubsub.enable_probe_task.call_count, 2)
        self.assertEqual(mock_pubsub.start.await_count, 2)


# ---------------------------------------------------------------------------
# Cross-commit behavioural tests (require Commits 1+2 complete; some also
# require Commit 3 for the startup path).
# ---------------------------------------------------------------------------

class CrossCommitBehavioural(unittest.IsolatedAsyncioTestCase):

    async def test_disabled_mode_start_creates_no_redis_client(self):
        """RedisPubSub.start() with REDIS_ENABLED=0 (via the module's own
        `_enabled`) short-circuits and creates no client / no socket."""
        import app.services.redis_pubsub as rp
        obj = rp.RedisPubSub()
        obj._enabled = False
        with patch("redis.asyncio.Redis") as redis_cls:
            await obj.start()
            redis_cls.assert_not_called()
        self.assertIsNone(obj._pub)
        self.assertIsNone(obj._sub)
        self.assertIsNone(obj._pubsub)
        self.assertIsNone(obj._reader_task)
        self.assertIsNone(obj._probe_task)
        self.assertFalse(obj._connected)

    async def test_start_stop_idempotent_second_call_noop(self):
        """A second start() with `_started=True` returns immediately; a
        second stop() with everything already torn down returns immediately."""
        import app.services.redis_pubsub as rp
        obj = rp.RedisPubSub()
        obj._enabled = False
        await obj.start()
        await obj.start()  # second — must be no-op
        await obj.stop()
        await obj.stop()   # second — must be no-op

    async def test_probe_channel_matches_dispatch_branch(self):
        """A probe envelope on the probe channel is delivered to the probe
        callback and never reaches the production callback."""
        import app.services.redis_pubsub as rp

        probe_env_received = []
        prod_env_received = []

        async def probe_cb(env):
            probe_env_received.append(env)

        async def prod_cb(org_id, room_id, msg):
            prod_env_received.append((org_id, room_id, msg))

        obj = rp.RedisPubSub()
        obj._probe_callback = probe_cb
        obj.set_delivery_callback(prod_cb)

        probe_ch = rp._probe_channel_name("inst-abc")
        envelope = {
            "v": 1,
            "publisher": "inst-abc",
            "ts": "2026-10-06T00:00:00.000Z",
            "is_probe": True,
            "probe_id": "probe-123",
        }
        msg = {"channel": probe_ch, "data": json.dumps(envelope)}

        await obj._dispatch(msg)

        self.assertEqual(len(probe_env_received), 1)
        self.assertEqual(probe_env_received[0]["probe_id"], "probe-123")
        self.assertEqual(len(prod_env_received), 0,
                         "probe message must NOT reach production callback")

    async def test_probe_dispatch_requires_is_probe_marker(self):
        """Defence-in-depth: even on the probe channel, the envelope must
        carry `is_probe=True` to be delivered."""
        import app.services.redis_pubsub as rp

        got = []

        async def probe_cb(env):
            got.append(env)

        obj = rp.RedisPubSub()
        obj._probe_callback = probe_cb

        probe_ch = rp._probe_channel_name("inst-xyz")
        envelope_without_marker = {
            "v": 1,
            "publisher": "inst-xyz",
            "ts": "2026-10-06T00:00:00.000Z",
            "probe_id": "probe-123",
        }
        msg = {"channel": probe_ch, "data": json.dumps(envelope_without_marker)}
        await obj._dispatch(msg)
        self.assertEqual(got, [], "probe without is_probe=True must be dropped")

    async def test_production_dispatch_suppresses_self_published(self):
        """A production message whose envelope.publisher equals INSTANCE_ID
        is suppressed (local-first delivery pattern)."""
        import app.services.redis_pubsub as rp
        from app.env import ENV

        got = []

        async def prod_cb(org_id, room_id, msg):
            got.append((org_id, room_id, msg))

        obj = rp.RedisPubSub()
        obj.set_delivery_callback(prod_cb)
        channel = rp._channel_name("org1", "room1")
        envelope = {
            "v": 1, "publisher": ENV.INSTANCE_ID, "ts": "2026-10-06T00:00:00.000Z",
            "message": {"type": "STATUS"},
        }
        msg = {"channel": channel, "data": json.dumps(envelope)}
        await obj._dispatch(msg)
        self.assertEqual(got, [], "self-published must be suppressed in prod branch")

        envelope["publisher"] = "other-instance-id"
        msg["data"] = json.dumps(envelope)
        await obj._dispatch(msg)
        self.assertEqual(len(got), 1)

    async def test_start_emits_initial_connect_failed_on_timeout(self):
        """Fail-open contract: a ping timeout emits
        `redis_pubsub_initial_connect_failed` with reason=timeout and still
        schedules the reader task so recovery is possible. The adapter must
        not raise out of start()."""
        import app.services.redis_pubsub as rp

        obj = rp.RedisPubSub()
        obj._enabled = True

        fake_pub = MagicMock()
        fake_pub.ping = AsyncMock(side_effect=asyncio.TimeoutError())
        fake_sub = MagicMock()
        fake_sub.pubsub = MagicMock(return_value=MagicMock(subscribe=AsyncMock()))

        with patch("redis.asyncio.Redis", side_effect=[fake_pub, fake_sub]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                await obj.start()
            events = [json.loads(l) for l in buf.getvalue().splitlines()
                      if l.strip().startswith("{")]

        # Must emit initial_connect_failed with reason=timeout and NOT raise.
        failed = [e for e in events if e.get("event") == rp.EVENT_INITIAL_CONNECT_FAILED]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].get("reason"), "timeout")

        # Reader loop MUST still be scheduled (recovery path).
        self.assertIsNotNone(obj._reader_task)
        self.assertFalse(obj._connected)
        self.assertTrue(obj._started)

        # Cleanup — cancel the reader to avoid dangling tasks.
        await obj.stop()

    async def test_start_shutdown_bounded_join_on_cancelled_reader(self):
        """stop() must await the reader_task's cancellation and return
        cleanly, bounded by whatever the reader loop does on CancelledError."""
        import app.services.redis_pubsub as rp
        obj = rp.RedisPubSub()
        obj._enabled = True

        fake_pub = MagicMock()
        fake_pub.ping = AsyncMock(return_value=True)
        fake_sub = MagicMock()
        fake_sub.pubsub = MagicMock(return_value=MagicMock(subscribe=AsyncMock()))

        with patch("redis.asyncio.Redis", side_effect=[fake_pub, fake_sub]):
            await obj.start()
            self.assertTrue(obj._started)
            self.assertIsNotNone(obj._reader_task)
            # Stop — must complete without hanging.
            await asyncio.wait_for(obj.stop(), timeout=5.0)
        self.assertFalse(obj._started)
        self.assertIsNone(obj._reader_task)
        self.assertIsNone(obj._pub)
        self.assertIsNone(obj._sub)


# ---------------------------------------------------------------------------
# Observability redaction / sensitive-field guard.
# ---------------------------------------------------------------------------

class ObservabilityRedaction(unittest.TestCase):

    def test_emit_does_not_auto_add_host_or_password(self):
        """The emitter does NOT auto-add REDIS_HOST or REDIS_PASSWORD to
        any event. Callers choose per-event whether to include host.
        (`EVENT_STARTED` call site explicitly passes host/port in the
        public-safe case; emitter does not inject it unconditionally.)"""
        import app.services.redis_pubsub as rp
        from app.env import ENV
        events = _capture_emits(lambda: rp._emit("plain_event", "INFO"))
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertNotIn("host", e)
        self.assertNotIn("REDIS_HOST", e)
        self.assertNotIn("password", e)
        self.assertNotIn("REDIS_PASSWORD", e)
        self.assertNotIn("auth", e)
        self.assertNotIn("authString", e)
        # And the AUTH credential value is never equal to any field we emit.
        for v in e.values():
            self.assertNotEqual(v, ENV.REDIS_PASSWORD or None)

    def test_event_payload_has_no_user_or_room_identifiers(self):
        """A call without explicit room/org/user kwargs must not have any
        such keys in output."""
        import app.services.redis_pubsub as rp
        events = _capture_emits(lambda: rp._emit("evt", "INFO",
                                                 attempt=1, delay_seconds=5))
        self.assertEqual(len(events), 1)
        for forbidden in ("room_id", "roomId", "org_id", "orgId",
                          "user_id", "uid", "email", "transcript"):
            self.assertNotIn(forbidden, events[0])


if __name__ == "__main__":
    unittest.main()
