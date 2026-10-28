from __future__ import annotations

import asyncio
import json
import os
import unittest
from typing import Any, Optional

from app.gemini_live import (
    GeminiGoAway,
    GeminiLiveSession,
    GeminiReplacementError,
    GeminiResumptionUpdate,
    parse_control_event,
    setup_message,
)


class _ClosedByServer(Exception):
    """Mock equivalent of websockets.ConnectionClosedError.

    Carries the close code so tests can assert the dispatch differentiates
    expected GoAway-triggered closes from unrelated 1008 closes AND from
    normal 1000 closes.
    """

    def __init__(self, code: int = 1008) -> None:
        super().__init__(f"closed code={code}")
        self.code = code


class MockWS:
    def __init__(self, events: list[Any], *, close_code: int = 1008) -> None:
        self._events = list(events)
        self._pos = 0
        self._close_code = close_code
        self.sent: list[str] = []
        self.closed = False
        self.send_raises: Optional[Exception] = None

    async def send(self, frame: str) -> None:
        if self.send_raises is not None:
            exc, self.send_raises = self.send_raises, None
            raise exc
        self.sent.append(frame)

    async def recv(self) -> str:
        await asyncio.sleep(0)
        if self._pos >= len(self._events):
            raise _ClosedByServer(self._close_code)
        item = self._events[self._pos]
        self._pos += 1
        if isinstance(item, Exception):
            raise item
        if isinstance(item, (bytes, bytearray)):
            return item.decode()
        if isinstance(item, dict):
            return json.dumps(item)
        return str(item)

    async def close(self) -> None:
        self.closed = True


def _setup_complete() -> dict:
    return {"setupComplete": {}}


def _resumption(handle: str, resumable: bool = True) -> dict:
    return {"sessionResumptionUpdate": {"newHandle": handle, "resumable": resumable}}


def _goaway(time_left_s: float = 60.0) -> dict:
    return {"goAway": {"timeLeft": f"{time_left_s:g}s"}}


class _Connector:
    def __init__(self, instances: list[MockWS]) -> None:
        self._instances = list(instances)
        self.connect_calls = 0

    async def __call__(self) -> MockWS:
        self.connect_calls += 1
        if not self._instances:
            raise RuntimeError("no more mock WS instances")
        return self._instances.pop(0)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class SessionSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            asyncio.get_event_loop().close()
        except Exception:
            pass
        asyncio.set_event_loop(asyncio.new_event_loop())

    def test_setup_message_includes_sessionResumption_empty_dict(self) -> None:
        payload = json.loads(setup_message("en"))
        self.assertEqual(payload["setup"]["sessionResumption"], {})

    def test_setup_message_with_handle_uses_handle(self) -> None:
        payload = json.loads(setup_message("en", resume_handle="opaque-handle-xyz"))
        self.assertEqual(
            payload["setup"]["sessionResumption"],
            {"handle": "opaque-handle-xyz"},
        )

    def test_setup_message_omits_context_compression_by_default(self) -> None:
        # Reviewer item 2: compatibility of contextWindowCompression with
        # gemini-3.5-live-translate-preview is NOT authoritatively
        # established; default off avoids breaking initial connections.
        payload = json.loads(setup_message("en"))
        self.assertNotIn("contextWindowCompression", payload["setup"])

    def test_setup_message_compression_can_be_opted_in(self) -> None:
        payload = json.loads(setup_message("en", context_compression=True))
        self.assertEqual(
            payload["setup"]["contextWindowCompression"],
            {"slidingWindow": {}},
        )


class ParseControlEventTests(unittest.TestCase):
    """Reviewer item 4: documented `goAway.timeLeft` is a protobuf Duration
    (seconds-with-s-suffix string). Numeric values are NOT part of the
    documented contract and must not be guessed as milliseconds."""

    def test_whole_seconds(self) -> None:
        ev = parse_control_event({"goAway": {"timeLeft": "60s"}})
        self.assertEqual(ev.kind, "goaway")
        self.assertIsInstance(ev.payload, GeminiGoAway)
        self.assertEqual(ev.payload.time_left_ms, 60000)

    def test_fractional_seconds(self) -> None:
        ev = parse_control_event({"goAway": {"timeLeft": "0.250s"}})
        self.assertEqual(ev.payload.time_left_ms, 250)

    def test_zero_seconds(self) -> None:
        ev = parse_control_event({"goAway": {"timeLeft": "0s"}})
        self.assertEqual(ev.payload.time_left_ms, 0)

    def test_numeric_value_is_rejected(self) -> None:
        # Documented contract is a string; numeric values are not part of
        # the spec and must NOT be guessed as ms or seconds.
        for raw in [60, 60.0, 60000]:
            ev = parse_control_event({"goAway": {"timeLeft": raw}})
            self.assertIsNone(ev.payload.time_left_ms, f"raw={raw!r}")

    def test_ms_suffix_is_rejected(self) -> None:
        # "100ms" is NOT the documented format.
        ev = parse_control_event({"goAway": {"timeLeft": "100ms"}})
        self.assertIsNone(ev.payload.time_left_ms)

    def test_malformed_strings_yield_none(self) -> None:
        for raw in ["", "abc", "100", "s", "nans"]:
            ev = parse_control_event({"goAway": {"timeLeft": raw}})
            self.assertIsNone(ev.payload.time_left_ms, f"raw={raw!r}")

    def test_missing_timeleft_is_none(self) -> None:
        ev = parse_control_event({"goAway": {}})
        self.assertIsNone(ev.payload.time_left_ms)

    def test_goaway_is_actionable_regardless_of_timeleft(self) -> None:
        # Policy: goAway itself is the actionable signal; timeLeft is advisory.
        ev = parse_control_event({"goAway": {"timeLeft": "garbage"}})
        self.assertEqual(ev.kind, "goaway")

    def test_other_kinds_still_parse(self) -> None:
        self.assertEqual(parse_control_event({"setupComplete": {}}).kind, "setup_complete")
        self.assertEqual(parse_control_event({"serverContent": {}}).kind, "content")
        res = parse_control_event(
            {"sessionResumptionUpdate": {"newHandle": "h1", "resumable": True}}
        )
        self.assertEqual(res.kind, "resumption")
        self.assertEqual(res.payload, GeminiResumptionUpdate("h1", True))
        self.assertEqual(parse_control_event({"error": {"message": "x"}}).kind, "error")


class SessionRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            asyncio.get_event_loop().close()
        except Exception:
            pass
        asyncio.set_event_loop(asyncio.new_event_loop())

    def _drive_until_goaway(self, session: GeminiLiveSession) -> None:
        async def drain():
            while True:
                raw = await session.recv()
                ev = parse_control_event(json.loads(raw))
                if ev.kind == "resumption":
                    session.on_resumption_update(ev.payload)
                if ev.kind == "goaway":
                    session.on_goaway_observed()
                    return

    def test_single_rotation_succeeds(self) -> None:
        ws1 = MockWS([_setup_complete(), _resumption("h1"), _goaway(60)])
        ws2 = MockWS([_setup_complete()])
        connector = _Connector([ws1, ws2])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        async def scenario():
            await session.open()
            for _ in range(2):
                raw = await session.recv()
                ev = parse_control_event(json.loads(raw))
                if ev.kind == "resumption":
                    session.on_resumption_update(ev.payload)
                if ev.kind == "goaway":
                    session.on_goaway_observed()
                    self.assertTrue(await session.rotate())
            await session.close()

        _run(scenario())
        self.assertEqual(connector.connect_calls, 2)
        self.assertIn('"h1"', ws2.sent[0])
        self.assertTrue(ws1.closed)
        self.assertEqual(session.rotation_generation, 1)
        self.assertEqual(session.latest_handle, "h1")

    def test_two_successive_rotations(self) -> None:
        ws1 = MockWS([_setup_complete(), _resumption("h1"), _goaway()])
        ws2 = MockWS([_setup_complete(), _resumption("h2"), _goaway()])
        ws3 = MockWS([_setup_complete()])
        connector = _Connector([ws1, ws2, ws3])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        async def scenario():
            await session.open()
            for _ in range(4):
                raw = await session.recv()
                ev = parse_control_event(json.loads(raw))
                if ev.kind == "resumption":
                    session.on_resumption_update(ev.payload)
                if ev.kind == "goaway":
                    session.on_goaway_observed()
                    self.assertTrue(await session.rotate())
            await session.close()

        _run(scenario())
        self.assertEqual(connector.connect_calls, 3)
        self.assertEqual(session.rotation_generation, 2)
        self.assertEqual(session.latest_handle, "h2")
        self.assertIn('"h1"', ws2.sent[0])
        self.assertIn('"h2"', ws3.sent[0])

    def test_repeated_goaway_messages_single_rotation(self) -> None:
        ws1 = MockWS([_setup_complete(), _resumption("h1"), _goaway(), _goaway()])
        ws2 = MockWS([_setup_complete()])
        connector = _Connector([ws1, ws2])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        async def scenario():
            await session.open()
            try:
                for _ in range(4):
                    raw = await session.recv()
                    ev = parse_control_event(json.loads(raw))
                    if ev.kind == "resumption":
                        session.on_resumption_update(ev.payload)
                    if ev.kind == "goaway":
                        session.on_goaway_observed()
                        await session.rotate()
            except _ClosedByServer:
                pass
            await session.close()

        _run(scenario())
        self.assertEqual(connector.connect_calls, 2)

    def test_resumable_false_refuses_rotation(self) -> None:
        """Reviewer item 2: a resumable=False update marks server state as
        NON-recoverable. rotate() must refuse (return False) even if an
        older handle is cached; silent reuse of an older checkpoint across
        a non-resumable gap can lose data per API docs."""
        ws1 = MockWS([
            _setup_complete(),
            _resumption("h-old", resumable=True),    # stored, state=True
            _resumption("h-new?", resumable=False),  # state=False (unsafe)
            _goaway(),
        ])
        connector = _Connector([ws1])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        async def scenario():
            await session.open()
            for _ in range(3):
                raw = await session.recv()
                ev = parse_control_event(json.loads(raw))
                if ev.kind == "resumption":
                    session.on_resumption_update(ev.payload)
                if ev.kind == "goaway":
                    session.on_goaway_observed()
                    return await session.rotate()
            return None

        rotated = _run(scenario())
        _run(session.close())
        # Rotation refused — server state is not resumable.
        self.assertFalse(rotated)
        self.assertEqual(connector.connect_calls, 1)
        # Old handle NOT invented or substituted; it stays stored for audit
        # but was NOT reused (connect_calls=1 proves no replacement attempted).
        self.assertEqual(session.latest_handle, "h-old")

    def test_resumable_false_then_true_before_goaway_re_arms(self) -> None:
        """A fresh resumable=True arriving BEFORE the GoAway deadline must
        re-arm recovery. This is the documented recovery pattern."""
        ws1 = MockWS([
            _setup_complete(),
            _resumption("h-old", resumable=True),
            _resumption("h-mid", resumable=False),   # temporarily unsafe
            _resumption("h-new", resumable=True),    # re-armed BEFORE goaway
            _goaway(),
        ])
        ws2 = MockWS([_setup_complete()])
        connector = _Connector([ws1, ws2])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        async def scenario():
            await session.open()
            for _ in range(4):
                raw = await session.recv()
                ev = parse_control_event(json.loads(raw))
                if ev.kind == "resumption":
                    session.on_resumption_update(ev.payload)
                if ev.kind == "goaway":
                    session.on_goaway_observed()
                    self.assertTrue(await session.rotate())
            await session.close()

        _run(scenario())
        self.assertEqual(connector.connect_calls, 2)
        self.assertEqual(session.latest_handle, "h-new")
        self.assertIn('"h-new"', ws2.sent[0])

    def test_no_resumption_update_before_goaway_refuses(self) -> None:
        """If GoAway arrives before any resumptionUpdate (state=None),
        rotation must refuse rather than open a connection with no handle."""
        ws1 = MockWS([_setup_complete(), _goaway()])
        connector = _Connector([ws1])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        async def scenario():
            await session.open()
            raw = await session.recv()
            ev = parse_control_event(json.loads(raw))
            self.assertEqual(ev.kind, "goaway")
            session.on_goaway_observed()
            result = await session.rotate()
            await session.close()
            return result

        self.assertFalse(_run(scenario()))
        self.assertEqual(connector.connect_calls, 1)

    def test_replacement_resumable_false_during_setup_applied_only_after_swap(self) -> None:
        """Reviewer defect 2 (outcome A): replacement setup receives a
        resumable=False update, then setupComplete. If applied directly
        this would leave stale state (`_resumable_state=True`) from ws1
        and the next rotation would incorrectly proceed. The fix stages
        updates and applies them AFTER the swap, so the next rotation
        correctly refuses."""
        ws1 = MockWS([
            _setup_complete(),
            _resumption("h1", resumable=True),
            _goaway(),
        ])
        # ws2's setup emits resumable=False BEFORE setupComplete.
        ws2 = MockWS([
            _resumption("h2?", resumable=False),
            _setup_complete(),
            _goaway(),  # trigger another rotation attempt
        ])
        connector = _Connector([ws1, ws2])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        async def scenario():
            await session.open()
            # Drain to first goaway → rotate.
            for _ in range(2):
                raw = await session.recv()
                ev = parse_control_event(json.loads(raw))
                if ev.kind == "resumption":
                    session.on_resumption_update(ev.payload)
                if ev.kind == "goaway":
                    session.on_goaway_observed()
                    self.assertTrue(await session.rotate())
            # Now session is on ws2 with resumable_state=False (applied
            # post-swap). Next GoAway → rotate must refuse.
            raw = await session.recv()
            ev = parse_control_event(json.loads(raw))
            self.assertEqual(ev.kind, "goaway")
            session.on_goaway_observed()
            rotated = await session.rotate()
            await session.close()
            return rotated

        rotated = _run(scenario())
        self.assertFalse(rotated, "second rotation must refuse — ws2 is non-resumable")

    def test_replacement_failure_rolls_back_latest_handle(self) -> None:
        """Reviewer defect 2 (outcome B): replacement setup emits a new
        handle, then errors. Rotation fails and ws1 remains current, but
        `latest_handle` must NOT already point to the failed replacement's
        handle — otherwise subsequent rotations would try to resume with
        a dead handle."""
        ws1 = MockWS([_setup_complete(), _resumption("h1", resumable=True), _goaway()])
        # ws2 emits a NEW handle during setup, then errors.
        ws2 = MockWS([
            _resumption("h-replacement-doomed", resumable=True),
            {"error": {"message": "handle expired"}},
        ])
        connector = _Connector([ws1, ws2])
        session = GeminiLiveSession(
            target_language="en",
            connect_cb=connector,
            setup_timeout_s=1.0,
            replacement_total_timeout_s=1.5,
        )

        async def scenario():
            await session.open()
            while True:
                raw = await session.recv()
                ev = parse_control_event(json.loads(raw))
                if ev.kind == "resumption":
                    session.on_resumption_update(ev.payload)
                if ev.kind == "goaway":
                    session.on_goaway_observed()
                    break
            with self.assertRaises(GeminiReplacementError):
                await session.rotate()
            # Rollback: latest_handle must still be ws1's h1 — NOT the
            # failed replacement's handle.
            self.assertEqual(session.latest_handle, "h1")
            # resumable_state must also be preserved as ws1's state.
            self.assertTrue(session._resumable_state)
            # Old WS still current.
            self.assertIs(session.current_ws, ws1)
            await session.close()

        _run(scenario())

    def test_initial_setup_applies_supported_resumption_updates(self) -> None:
        """Initial setup should process resumption updates consistently
        with mid-session on_resumption_update."""
        ws1 = MockWS([
            _resumption("h-initial", resumable=True),
            _setup_complete(),
        ])
        connector = _Connector([ws1])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        async def scenario():
            await session.open()
            self.assertEqual(session.latest_handle, "h-initial")
            self.assertTrue(session._resumable_state)
            await session.close()

        _run(scenario())

    def test_replacement_setup_failure_no_handoff(self) -> None:
        ws1 = MockWS([_setup_complete(), _resumption("h1"), _goaway()])
        ws2 = MockWS([{"error": {"message": "handle expired"}}])
        connector = _Connector([ws1, ws2])
        session = GeminiLiveSession(
            target_language="en",
            connect_cb=connector,
            setup_timeout_s=1.0,
            replacement_total_timeout_s=1.5,
        )

        async def scenario():
            await session.open()
            while True:
                raw = await session.recv()
                ev = parse_control_event(json.loads(raw))
                if ev.kind == "resumption":
                    session.on_resumption_update(ev.payload)
                if ev.kind == "goaway":
                    session.on_goaway_observed()
                    break
            with self.assertRaises(GeminiReplacementError):
                await session.rotate()
            self.assertIs(session.current_ws, ws1)
            await session.close()

        _run(scenario())
        self.assertTrue(ws2.closed)


class SendSemanticsTests(unittest.TestCase):
    """Reviewer item 3: send() must NOT retry on an ambiguous close.

    Even one retry can duplicate an input frame the server already applied
    before closing. The resumption handle is the server's checkpoint; any
    frame NOT yet reflected in that checkpoint has an ambiguous status
    client-side. Policy: no blind replay — caller sees the failure and
    the audio input path continues with the NEXT frame on the new WS.
    """

    def setUp(self) -> None:
        try:
            asyncio.get_event_loop().close()
        except Exception:
            pass
        asyncio.set_event_loop(asyncio.new_event_loop())

    def test_send_delivers_to_current_ws_without_duplication(self) -> None:
        """A manual swap simulates a rotation that just completed. The next
        send() reads self._ws once and delivers to the CURRENT ws; nothing
        additional lands on the OLD ws."""
        ws1 = MockWS([_setup_complete()])
        ws2 = MockWS([])
        connector = _Connector([ws1, ws2])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        async def scenario():
            await session.open()
            setup_sends_before = len(ws1.sent)  # 1 setup frame
            session._ws = ws2
            session._rotation_generation += 1
            delivered = await session.send("frame-A")
            self.assertTrue(delivered)
            self.assertEqual(ws2.sent, ["frame-A"])
            # No ADDITIONAL send landed on ws1 — no duplication.
            self.assertEqual(len(ws1.sent), setup_sends_before)
            await session.close()

        _run(scenario())

    def test_send_classifies_rotation_in_flight_failure_as_benign(self) -> None:
        """If a send raises while a rotation is in flight, send() returns
        False (benign drop) instead of raising — so a planned rotation
        does not kill the audio sender task."""
        ws1 = MockWS([_setup_complete()])
        connector = _Connector([ws1])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        async def scenario():
            await session.open()
            session._rotation_in_flight = True  # simulate mid-rotation
            ws1.send_raises = RuntimeError("old-ws-closing")
            # NOT raising; returns False.
            delivered = await session.send("frame-A")
            self.assertFalse(delivered)
            await session.close()

        _run(scenario())

    def test_send_classifies_goaway_observed_failure_as_benign(self) -> None:
        """A send that fails after GoAway was observed but before rotate()
        acquires the lock is also benign (rotation imminent)."""
        ws1 = MockWS([_setup_complete()])
        connector = _Connector([ws1])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        async def scenario():
            await session.open()
            session.on_goaway_observed()
            ws1.send_raises = RuntimeError("server-closed-after-goaway")
            delivered = await session.send("frame-A")
            self.assertFalse(delivered)
            await session.close()

        _run(scenario())

    def test_send_propagates_unrelated_failure(self) -> None:
        """If a send fails with NO rotation in flight AND NO GoAway
        observed, send() propagates. This is a real connection fault
        that the caller must surface."""
        ws1 = MockWS([_setup_complete()])
        connector = _Connector([ws1])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        async def scenario():
            await session.open()
            ws1.send_raises = RuntimeError("network-glitch")
            with self.assertRaises(RuntimeError) as cm:
                await session.send("frame-A")
            self.assertIn("network-glitch", str(cm.exception))
            await session.close()

        _run(scenario())

    def test_send_after_close_returns_false(self) -> None:
        """Post-close send() returns False (no raise). This lets the
        audio sender exit cleanly rather than through an exception
        path that would double-set the close flag or spuriously log."""
        ws1 = MockWS([_setup_complete()])
        connector = _Connector([ws1])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        async def scenario():
            await session.open()
            await session.close()
            delivered = await session.send("frame-A")
            self.assertFalse(delivered)

        _run(scenario())

    def test_delayed_old_ws_send_failure_after_swap_is_benign(self) -> None:
        """Reviewer defect 1: a send that captured ws1 and blocks inside
        ws1.send() while rotation completes and old-ws close releases the
        send with an exception. send() must classify by captured (ws, gen),
        not live flags — so this returns False (benign), the sender keeps
        running, and the next frame reaches ws2."""
        ws1 = MockWS([_setup_complete()])
        ws2 = MockWS([])
        connector = _Connector([ws1, ws2])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        send_released = asyncio.Event()
        send_can_release = asyncio.Event()

        async def _ws1_send(frame: str) -> None:
            # Signal we're inside ws1.send(), then block until released.
            send_released.set()
            await send_can_release.wait()
            # Mimic websockets lib: a close during a blocked send raises.
            raise RuntimeError("websocket closed while sending")

        async def scenario():
            await session.open()
            # Patch ws1.send to the blocking shim.
            ws1.send = _ws1_send  # type: ignore[assignment]
            # Start a send that will capture ws1 and block.
            sender_task = asyncio.create_task(session.send("audio-A"))
            await send_released.wait()
            # Simulate a successful rotation: swap ws + bump generation
            # BEFORE releasing the blocked ws1.send.
            session._ws = ws2
            session._rotation_generation += 1
            # Clear flags as the real post-swap state does.
            session._goaway_observed = False
            session._rotation_in_flight = False
            # Release the blocked ws1 send — it raises.
            send_can_release.set()
            delivered = await sender_task
            # It must be a benign drop (False), NOT a propagated exception.
            self.assertFalse(delivered)
            # The sender can continue and reach ws2.
            self.assertTrue(await session.send("audio-B"))
            self.assertEqual(ws2.sent, ["audio-B"])
            await session.close()

        _run(scenario())

    def test_current_ws_genuine_failure_still_propagates(self) -> None:
        """Preserves reviewer-confirmed behavior: a genuine failure on
        the CURRENT ws (no swap under us, no close, no rotation/goaway)
        still raises so the caller can surface it."""
        ws1 = MockWS([_setup_complete()])
        connector = _Connector([ws1])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        async def scenario():
            await session.open()
            ws1.send_raises = RuntimeError("hard-fault-current-ws")
            with self.assertRaises(RuntimeError) as cm:
                await session.send("audio")
            self.assertIn("hard-fault-current-ws", str(cm.exception))
            await session.close()

        _run(scenario())

    def test_send_during_concurrent_rotation_does_not_kill_sender(self) -> None:
        """Concurrent scenario: a long-running sender attempts a send WHILE
        a rotation is swapping. The sender neither duplicates onto the old
        WS nor terminates with an unhandled exception."""
        ws1 = MockWS([_setup_complete(), _resumption("h1"), _goaway()])
        gate = asyncio.Event()

        class SlowWS(MockWS):
            async def recv(self) -> str:
                await gate.wait()
                return await super().recv()

        ws2 = SlowWS([_setup_complete()])
        session = GeminiLiveSession(
            target_language="en",
            connect_cb=_Connector([ws1, ws2]).__call__,
            setup_timeout_s=5.0,
            replacement_total_timeout_s=5.0,
        )
        sender_errors: list[Exception] = []
        sender_results: list[bool] = []

        async def sender():
            # Attempt one send BEFORE rotation (should succeed on ws1),
            # one DURING rotation (should drop benignly), one AFTER swap
            # (should succeed on ws2).
            try:
                sender_results.append(await session.send("pre"))
                # wait until rotation is in flight
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    if session._rotation_in_flight:
                        break
                ws1.send_raises = RuntimeError("mid-rotation-fault")
                sender_results.append(await session.send("mid"))
                # Let the rotation complete
                gate.set()
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    if session.rotation_generation == 1:
                        break
                sender_results.append(await session.send("post"))
            except Exception as e:
                sender_errors.append(e)

        async def driver():
            await session.open()
            # drain to goaway and trigger rotation
            while True:
                raw = await session.recv()
                ev = parse_control_event(json.loads(raw))
                if ev.kind == "resumption":
                    session.on_resumption_update(ev.payload)
                if ev.kind == "goaway":
                    session.on_goaway_observed()
                    task = asyncio.create_task(session.rotate())
                    break
            send_task = asyncio.create_task(sender())
            await asyncio.wait_for(send_task, timeout=3.0)
            await asyncio.wait_for(task, timeout=3.0)
            await session.close()

        _run(driver())
        self.assertEqual(sender_errors, [], f"sender raised: {sender_errors}")
        self.assertEqual(sender_results, [True, False, True])
        self.assertEqual(ws2.sent[-1], "post", "post-swap frame reached ws2")


class ErrorReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            asyncio.get_event_loop().close()
        except Exception:
            pass
        asyncio.set_event_loop(asyncio.new_event_loop())

    def test_unrelated_1008_surfaces_error_when_no_goaway_observed(self) -> None:
        ws1 = MockWS([_setup_complete()])
        connector = _Connector([ws1])
        session = GeminiLiveSession(target_language="en", connect_cb=connector)

        async def scenario():
            await session.open()
            with self.assertRaises(_ClosedByServer) as cm:
                await session.recv()
            self.assertEqual(cm.exception.code, 1008)
            self.assertFalse(session.goaway_observed)
            await session.close()

        _run(scenario())


if __name__ == "__main__":
    unittest.main()
