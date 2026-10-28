"""Handler integration tests.

These tests exercise the SAME `run_recovery_dispatch` function the
production handler in main.py calls (reviewer item 5). The handler
registers its callbacks via DispatchCallbacks and does nothing else
inside the receive loop, so test coverage here IS production coverage
of the dispatch flow.

What these tests do NOT cover (and cannot, from here):
  - auth / Firestore / room binding / host registration (owned by main.py
    before the dispatch loop is entered; see the handler-wiring section
    of this patch's production diff)
  - the audio-input sender task (from_client_to_gemini); covered by
    SendSemanticsTests in test_gemini_live_session
  - the real WS driver behind `websockets.connect` (mocked here)
"""
from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any, Optional

from app.gemini_live import (
    DispatchCallbacks,
    GeminiLiveSession,
    GeminiServerContent,
    HandlerBuffers,
    build_handler_callbacks,
    parse_control_event,
    run_recovery_dispatch,
)
from tests.test_gemini_live_session import (
    MockWS,
    _ClosedByServer,
    _Connector,
    _goaway,
    _resumption,
    _setup_complete,
)


class RecorderClient:
    """Captures what the production handler would emit to the client."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.transcripts: list[str] = []
        self.translations: list[str] = []
        self.audio_chunks: list[str] = []
        self.turn_completes: int = 0
        self.interruptions: int = 0
        self.source_buffer: str = ""
        self.output_buffer: str = ""
        self.closed = asyncio.Event()

    async def send_error(self, message: str) -> None:
        self.errors.append(message)

    async def on_content(self, content: GeminiServerContent) -> None:
        if content.interrupted:
            self.interruptions += 1
            self.source_buffer = ""
            self.output_buffer = ""
        if content.input_transcript:
            self.source_buffer += content.input_transcript
            self.transcripts.append(self.source_buffer)
        if content.output_transcript:
            self.output_buffer += content.output_transcript
            self.translations.append(self.output_buffer)
        for chunk in content.audio_chunks:
            self.audio_chunks.append(chunk)
        if content.turn_complete:
            self.turn_completes += 1

    def is_closed(self) -> bool:
        return self.closed.is_set()


def _content(
    input_text: str = "",
    output_text: str = "",
    audio: Optional[list[str]] = None,
    turn_complete: bool = False,
) -> dict:
    inner: dict[str, Any] = {}
    if input_text:
        inner["inputTranscription"] = {"text": input_text}
    if output_text:
        inner["outputTranscription"] = {"text": output_text}
    if audio:
        inner["modelTurn"] = {
            "parts": [{"inlineData": {"data": a}} for a in audio]
        }
    if turn_complete:
        inner["turnComplete"] = True
    return {"serverContent": inner}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_session(instances: list[MockWS]) -> tuple[GeminiLiveSession, _Connector]:
    connector = _Connector(instances)
    return (
        GeminiLiveSession(target_language="en", connect_cb=connector),
        connector,
    )


class HandlerDispatchTests(unittest.TestCase):
    """Each test drives the SAME `run_recovery_dispatch` function the
    production handler calls. Any wiring bug would break both."""

    def setUp(self) -> None:
        try:
            asyncio.get_event_loop().close()
        except Exception:
            pass
        asyncio.set_event_loop(asyncio.new_event_loop())

    # ---- Resumption + goaway + continued audio/transcript delivery ----
    def test_resumption_then_goaway_then_continued_delivery(self) -> None:
        ws1 = MockWS([
            _setup_complete(),
            _content(input_text="안녕", output_text="Hello", audio=["AAA"]),
            _resumption("h1", resumable=True),
            _goaway(),
        ])
        ws2 = MockWS(
            [_setup_complete(), _content(input_text=" 하세요", output_text=" world", audio=["BBB"])],
            close_code=1000,
        )
        session, connector = _make_session([ws1, ws2])
        client = RecorderClient()

        async def scenario():
            await session.open()
            await run_recovery_dispatch(
                session,
                DispatchCallbacks(client.send_error, client.on_content, client.is_closed),
            )
            await session.close()

        _run(scenario())
        self.assertEqual(connector.connect_calls, 2)
        self.assertEqual(client.errors, [])
        self.assertEqual(client.source_buffer, "안녕 하세요")
        self.assertEqual(client.output_buffer, "Hello world")
        self.assertEqual(client.audio_chunks, ["AAA", "BBB"])
        self.assertEqual(session.latest_handle, "h1")
        self.assertIn('"h1"', ws2.sent[0])

    # ---- Two successive rotations with continued delivery ----
    def test_two_successive_rotations_with_delivery(self) -> None:
        ws1 = MockWS([
            _setup_complete(),
            _content(output_text="A"),
            _resumption("h1"),
            _goaway(),
        ])
        ws2 = MockWS([
            _setup_complete(),
            _content(output_text="B"),
            _resumption("h2"),
            _goaway(),
        ])
        ws3 = MockWS([_setup_complete(), _content(output_text="C")], close_code=1000)
        session, connector = _make_session([ws1, ws2, ws3])
        client = RecorderClient()

        async def scenario():
            await session.open()
            await run_recovery_dispatch(
                session,
                DispatchCallbacks(client.send_error, client.on_content, client.is_closed),
            )
            await session.close()

        _run(scenario())
        self.assertEqual(connector.connect_calls, 3)
        self.assertEqual(session.rotation_generation, 2)
        self.assertEqual(client.output_buffer, "ABC")
        self.assertEqual(client.errors, [])
        self.assertIn('"h1"', ws2.sent[0])
        self.assertIn('"h2"', ws3.sent[0])

    # ---- Repeated GoAway + stale events ----
    def test_repeated_goaway_and_stale_events_no_extra_rotation(self) -> None:
        ws1 = MockWS([
            _setup_complete(),
            _resumption("h1"),
            _goaway(),
            _content(output_text="STALE"),  # queued on old WS; may or may not surface
            _goaway(),                      # duplicate; must not spawn 3rd rotation
        ])
        ws2 = MockWS([_setup_complete(), _content(output_text="FRESH")], close_code=1000)
        session, connector = _make_session([ws1, ws2])
        client = RecorderClient()

        async def scenario():
            await session.open()
            await run_recovery_dispatch(
                session,
                DispatchCallbacks(client.send_error, client.on_content, client.is_closed),
            )
            await session.close()

        _run(scenario())
        self.assertEqual(connector.connect_calls, 2, "no extra rotation")
        self.assertEqual(client.errors, [])
        self.assertIn("FRESH", client.output_buffer)

    # ---- resumable=False around rotation → bounded refusal, meaningful error ----
    def test_resumable_false_at_goaway_refuses_with_meaningful_error(self) -> None:
        """Reviewer item 2: observable policy — dispatch surfaces a specific
        error, does NOT silently reuse an older handle."""
        ws1 = MockWS([
            _setup_complete(),
            _resumption("h-old", resumable=True),
            _resumption("h-new?", resumable=False),  # server marks state unsafe
            _goaway(),
        ])
        session, connector = _make_session([ws1])
        client = RecorderClient()

        async def scenario():
            await session.open()
            await run_recovery_dispatch(
                session,
                DispatchCallbacks(client.send_error, client.on_content, client.is_closed),
            )
            await session.close()

        _run(scenario())
        self.assertEqual(connector.connect_calls, 1, "no replacement opened")
        self.assertEqual(len(client.errors), 1)
        self.assertIn("not resumable", client.errors[0])

    # ---- resumable=False then True re-arms before deadline ----
    def test_resumable_false_then_true_before_goaway_recovers(self) -> None:
        ws1 = MockWS([
            _setup_complete(),
            _resumption("h-old", resumable=True),
            _resumption("h-mid", resumable=False),
            _resumption("h-new", resumable=True),  # re-arm BEFORE goaway
            _goaway(),
        ])
        ws2 = MockWS([_setup_complete(), _content(output_text="OK")], close_code=1000)
        session, connector = _make_session([ws1, ws2])
        client = RecorderClient()

        async def scenario():
            await session.open()
            await run_recovery_dispatch(
                session,
                DispatchCallbacks(client.send_error, client.on_content, client.is_closed),
            )
            await session.close()

        _run(scenario())
        self.assertEqual(connector.connect_calls, 2)
        self.assertEqual(client.errors, [])
        self.assertIn('"h-new"', ws2.sent[0])
        self.assertEqual(client.output_buffer, "OK")

    # ---- Replacement setup failure → meaningful recovery error ----
    def test_replacement_setup_failure_reports_meaningful_error(self) -> None:
        ws1 = MockWS([_setup_complete(), _resumption("h1"), _goaway()])
        ws2 = MockWS([{"error": {"message": "handle expired on server"}}])
        session = GeminiLiveSession(
            target_language="en",
            connect_cb=_Connector([ws1, ws2]).__call__,
            setup_timeout_s=1.0,
            replacement_total_timeout_s=1.5,
        )
        client = RecorderClient()
        captured: list[Any] = []

        async def scenario():
            await session.open()
            await run_recovery_dispatch(
                session,
                DispatchCallbacks(client.send_error, client.on_content, client.is_closed),
            )
            captured.append(session.current_ws)
            await session.close()

        _run(scenario())
        self.assertEqual(len(client.errors), 1)
        self.assertIn("recovery failed", client.errors[0])
        # Old WS still current when dispatch returned (swap never happened).
        self.assertIs(captured[0], ws1)

    # ---- Dispatcher cancellation while connector is blocked ----
    def test_dispatcher_cancel_during_connect_cleans_up_in_close(self) -> None:
        """Reviewer defect (shielded-task cleanup): if the dispatcher is
        cancelled WHILE the replacement connector is blocked (ws not yet
        returned), rotate()'s shield absorbs the cancel and the task
        keeps running. The CancelledError handler must NOT clear tracking
        refs — session.close() needs them to drive cleanup. After
        close(), no replacement task is still running, and releasing the
        connector afterward MUST NOT produce an orphan session."""
        connector_gate = asyncio.Event()
        orphan_ws = MockWS([_setup_complete()])

        async def blocked_connect() -> MockWS:
            # The connector is blocked. If cancellation reaches here,
            # CancelledError propagates up and the orphan WS is never
            # returned (and never has a setup frame sent to it).
            await connector_gate.wait()
            return orphan_ws

        ws1 = MockWS([_setup_complete(), _resumption("h1"), _goaway()])

        async def connect_cb():
            # First call returns ws1; next call blocks on the gate.
            if connect_cb.calls == 0:
                connect_cb.calls = 1
                return ws1
            return await blocked_connect()
        connect_cb.calls = 0

        session = GeminiLiveSession(
            target_language="en",
            connect_cb=connect_cb,
            setup_timeout_s=5.0,
            replacement_total_timeout_s=5.0,
        )
        client = RecorderClient()

        async def scenario():
            await session.open()
            dispatcher = asyncio.create_task(
                run_recovery_dispatch(
                    session,
                    DispatchCallbacks(
                        client.send_error, client.on_content, client.is_closed
                    ),
                )
            )
            # Wait until replacement task has been created (rotation entered)
            for _ in range(50):
                await asyncio.sleep(0.01)
                if session._replacement_task is not None:
                    break
            self.assertIsNotNone(
                session._replacement_task,
                "dispatch did not reach rotate()/create replacement task",
            )
            # Reviewer's shutdown sequence:
            # 3. Mark client closed.
            client.closed.set()
            # 4. Cancel and await the dispatcher.
            dispatcher.cancel()
            try:
                await dispatcher
            except BaseException:
                pass
            self.assertTrue(dispatcher.done())
            # Reference MUST survive outer cancellation so close() can clean up.
            self.assertIsNotNone(
                session._replacement_task,
                "outer cancellation prematurely cleared replacement task ref",
            )
            # 5. Await session.close(). This is where cleanup must happen.
            await session.close()
            # After close(): no running task, no tracking refs.
            self.assertIsNone(
                session._replacement_task,
                "replacement task not cleared by close()",
            )
            self.assertIsNone(
                session._pending_replacement_ws,
                "pending replacement WS ref not cleared by close()",
            )
            # 6. Release the blocked connector LATER — must not spawn
            # an orphan session.
            connector_gate.set()
            # Give any lingering task a chance to misbehave.
            await asyncio.sleep(0.05)
            # orphan_ws was NEVER handed to _run_replacement (connector was
            # cancelled before it returned), so no setup frame was sent to it.
            self.assertEqual(
                orphan_ws.sent, [], "setup frame reached the orphan WS"
            )
            self.assertTrue(ws1.closed, "old WS not closed by session.close()")

        _run(scenario())

    # ---- Cancellation AFTER child completion but BEFORE swap ----
    def test_cancel_after_child_completion_before_swap_closes_both(self) -> None:
        """Reviewer defect (ownership race): child replacement task
        completes and returns its WebSocket, but outer cancellation
        arrives BEFORE rotate() resumes and commits the swap. rotate()
        re-raises CancelledError; the task is `done()`, but its WS was
        NOT transferred to the active session. `task.done()`-based
        clearing would lose the WS reference and leave ws2 open.
        Fix uses an explicit `swap_committed` flag so tracking refs
        survive until close() disposes of ws2 via `.result()`."""
        ws1 = MockWS([_setup_complete(), _resumption("h1"), _goaway()])

        class CancelOnRecvWS(MockWS):
            """Right before returning setupComplete, schedule the
            dispatcher's cancel on the event loop via call_soon. This
            races the cancel with wait_for's resumption so rotate()
            resumes AFTER the child completed but WITH a pending cancel.
            """

            def __init__(self, events, *, close_code=1008):
                super().__init__(events, close_code=close_code)
                self.dispatcher_task: Optional[asyncio.Task[Any]] = None

            async def recv(self) -> str:
                # Schedule the cancel to run on the next loop iteration;
                # setupComplete will return first, then cancel propagates.
                if self.dispatcher_task is not None:
                    loop = asyncio.get_event_loop()
                    loop.call_soon(self.dispatcher_task.cancel)
                return await super().recv()

        ws2 = CancelOnRecvWS([_setup_complete()])
        session = GeminiLiveSession(
            target_language="en",
            connect_cb=_Connector([ws1, ws2]).__call__,
            setup_timeout_s=5.0,
            replacement_total_timeout_s=5.0,
        )
        client = RecorderClient()

        async def scenario():
            await session.open()
            dispatcher = asyncio.create_task(
                run_recovery_dispatch(
                    session,
                    DispatchCallbacks(
                        client.send_error, client.on_content, client.is_closed
                    ),
                )
            )
            # Hand ws2 the dispatcher so recv() can schedule the cancel
            # at the race window.
            ws2.dispatcher_task = dispatcher
            # Let the dispatch loop reach rotate() and start the
            # replacement. We can't deterministically know whether
            # cancellation hits BEFORE or AFTER rotate() swapped; the
            # test asserts both-sockets-closed in either outcome.
            try:
                await asyncio.wait_for(dispatcher, timeout=2.0)
            except BaseException:
                pass
            self.assertTrue(dispatcher.done())
            # If the swap landed before cancel: swap_committed → refs cleared.
            # If cancel landed first: refs preserved for close() to find task.
            # Either way, close() must complete cleanup for both sockets.
            await session.close()
            self.assertIsNone(
                session._replacement_task,
                "replacement task ref not cleared after close()",
            )
            self.assertIsNone(
                session._pending_replacement_ws,
                "pending WS ref not cleared after close()",
            )
            self.assertTrue(ws1.closed, "ws1 not closed after shutdown")
            self.assertTrue(
                ws2.closed,
                "ws2 not closed after shutdown — ownership race leaked it",
            )

        _run(scenario())

    # ---- Replacement-timeout cleanup path ----
    def test_replacement_timeout_cleans_up_shielded_task(self) -> None:
        """The timeout branch of rotate() must cancel AND await the
        shielded replacement task, and close any WS it acquired, before
        raising GeminiReplacementError."""
        ws1 = MockWS([_setup_complete(), _resumption("h1"), _goaway()])

        setup_gate = asyncio.Event()  # never released

        class StalledWS(MockWS):
            async def recv(self) -> str:
                await setup_gate.wait()
                return await super().recv()

        ws2 = StalledWS([_setup_complete()])
        session = GeminiLiveSession(
            target_language="en",
            connect_cb=_Connector([ws1, ws2]).__call__,
            setup_timeout_s=5.0,
            replacement_total_timeout_s=0.2,  # trigger timeout quickly
            close_grace_s=0.5,
        )

        async def scenario():
            await session.open()
            # Drain ws1 to goaway.
            for _ in range(2):
                raw = await session.recv()
                ev = parse_control_event(json.loads(raw))
                if ev.kind == "resumption":
                    session.on_resumption_update(ev.payload)
                if ev.kind == "goaway":
                    session.on_goaway_observed()
                    with self.assertRaises(Exception):
                        await session.rotate()
                    break
            # Timeout path must have cleaned up: no running task; ws2 closed.
            self.assertIsNone(session._replacement_task)
            self.assertIsNone(session._pending_replacement_ws)
            self.assertTrue(ws2.closed, "replacement WS not closed on timeout cleanup")
            await session.close()

        _run(scenario())

    # ---- Client disconnect during recovery; no leaked tasks/sockets ----
    def test_client_disconnect_during_recovery_closes_cleanly(self) -> None:
        """Reviewer defect 3: session.close() must cancel+await the
        pending replacement task AND close the pending replacement WS
        WITHOUT the test releasing the gate. Earlier version released
        the gate after close() which masked this gap."""
        ws1 = MockWS([_setup_complete(), _resumption("h1"), _goaway()])
        gate = asyncio.Event()  # intentionally never set externally

        class SlowWS(MockWS):
            """Setup-blocked WS whose recv waits on `gate` or on close()."""

            def __init__(self, events, *, close_code=1008):
                super().__init__(events, close_code=close_code)
                self._close_signal = asyncio.Event()

            async def close(self) -> None:
                await super().close()
                self._close_signal.set()

            async def recv(self) -> str:
                gate_task = asyncio.create_task(gate.wait())
                close_task = asyncio.create_task(self._close_signal.wait())
                try:
                    done, pending = await asyncio.wait(
                        {gate_task, close_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for p in pending:
                        p.cancel()
                    if close_task in done:
                        raise _ClosedByServer(1000)
                finally:
                    for t in (gate_task, close_task):
                        if not t.done():
                            t.cancel()
                return await super().recv()

        ws2 = SlowWS([_setup_complete()])
        session = GeminiLiveSession(
            target_language="en",
            connect_cb=_Connector([ws1, ws2]).__call__,
            setup_timeout_s=5.0,
            replacement_total_timeout_s=5.0,
        )
        client = RecorderClient()

        async def scenario():
            await session.open()
            dispatch_task = asyncio.create_task(
                run_recovery_dispatch(
                    session,
                    DispatchCallbacks(client.send_error, client.on_content, client.is_closed),
                )
            )
            # Wait until replacement WS received its setup frame.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if ws2.sent:
                    break
            self.assertTrue(ws2.sent, "dispatch did not reach rotate()")
            client.closed.set()
            # close() must clean up WITHOUT external gate release.
            await session.close()
            # Both WSs must be closed by the time close() returns.
            self.assertTrue(ws1.closed, "old WS not closed on session.close()")
            self.assertTrue(ws2.closed, "replacement WS not closed by session.close()")
            # The dispatch task must finish (its rotate() has been
            # cancelled/returned-False; the loop then exits).
            try:
                await asyncio.wait_for(dispatch_task, timeout=2.0)
            except BaseException:
                pass
            self.assertTrue(dispatch_task.done(), "dispatch task leaked")

        _run(scenario())

    # ---- Unrelated 1008 reaches client as a real error ----
    def test_unrelated_1008_reaches_client_error(self) -> None:
        ws1 = MockWS([_setup_complete()])
        session = GeminiLiveSession(
            target_language="en",
            connect_cb=_Connector([ws1]).__call__,
        )
        client = RecorderClient()

        async def scenario():
            await session.open()
            await run_recovery_dispatch(
                session,
                DispatchCallbacks(client.send_error, client.on_content, client.is_closed),
            )
            await session.close()

        _run(scenario())
        self.assertEqual(len(client.errors), 1)
        self.assertIn("disconnected", client.errors[0])
        self.assertFalse(session.goaway_observed)

    # ---- Normal 1000 close is silent ----
    def test_normal_1000_close_does_not_emit_error(self) -> None:
        ws1 = MockWS(
            [_setup_complete(), _content(output_text="hi")],
            close_code=1000,
        )
        session = GeminiLiveSession(
            target_language="en",
            connect_cb=_Connector([ws1]).__call__,
        )
        client = RecorderClient()

        async def scenario():
            await session.open()
            await run_recovery_dispatch(
                session,
                DispatchCallbacks(client.send_error, client.on_content, client.is_closed),
            )
            await session.close()

        _run(scenario())
        self.assertEqual(client.errors, [])
        self.assertEqual(client.output_buffer, "hi")

    # ---- Server error event propagates verbatim ----
    def test_server_error_event_propagates(self) -> None:
        ws1 = MockWS([
            _setup_complete(),
            {"error": {"message": "quota exhausted"}},
        ])
        session = GeminiLiveSession(
            target_language="en",
            connect_cb=_Connector([ws1]).__call__,
        )
        client = RecorderClient()

        async def scenario():
            await session.open()
            await run_recovery_dispatch(
                session,
                DispatchCallbacks(client.send_error, client.on_content, client.is_closed),
            )
            await session.close()

        _run(scenario())
        self.assertEqual(client.errors, ["quota exhausted"])


class ProductionWiringTests(unittest.TestCase):
    """Reviewer item 4: verify `build_handler_callbacks` — the SAME function
    main.py calls — produces callbacks that:
      - merge transcripts the same way the production handler does
      - emit stt.partial frames when input_transcript arrives
      - call broadcast_translation(partial=True, source) on output_transcript
      - broadcast each audio part
      - commit on turn_complete
      - clear buffers AND fire on_interrupted hook on interrupted

    The production handler in main.py imports and uses this exact function,
    so a test over it IS a test over the production callback wiring.
    """

    def setUp(self) -> None:
        try:
            asyncio.get_event_loop().close()
        except Exception:
            pass
        asyncio.set_event_loop(asyncio.new_event_loop())

    def _build(self) -> tuple[HandlerBuffers, DispatchCallbacks, dict]:
        buffers = HandlerBuffers()
        rec: dict[str, Any] = {
            "producer": [],
            "translations": [],
            "audio": [],
            "commits": [],
            "interrupted_calls": 0,
            "idle_flush_calls": 0,
            "closed": False,
        }

        async def send_to_producer(m: dict) -> None:
            rec["producer"].append(m)

        async def broadcast_translation(text: str, partial: bool, src: str) -> None:
            rec["translations"].append((text, partial, src))

        async def broadcast_audio(data: str) -> None:
            rec["audio"].append(data)

        async def commit_output(reason: str) -> None:
            rec["commits"].append((reason, buffers.output, buffers.source))
            buffers.output = ""
            buffers.source = ""

        def on_interrupted() -> None:
            rec["interrupted_calls"] += 1

        def schedule_idle_flush() -> None:
            rec["idle_flush_calls"] += 1

        callbacks = build_handler_callbacks(
            buffers,
            send_to_producer=send_to_producer,
            broadcast_translation=broadcast_translation,
            broadcast_audio=broadcast_audio,
            commit_output=commit_output,
            on_interrupted=on_interrupted,
            is_closed=lambda: rec["closed"],
            schedule_idle_flush=schedule_idle_flush,
        )
        return buffers, callbacks, rec

    def test_input_transcript_updates_buffer_and_emits_partial(self) -> None:
        buffers, cb, rec = self._build()

        async def scenario():
            await cb.on_content(GeminiServerContent("안녕", "", (), False, False))
            await cb.on_content(GeminiServerContent(" 하세요", "", (), False, False))

        _run(scenario())
        self.assertEqual(buffers.source, "안녕 하세요")
        self.assertEqual(rec["producer"], [
            {"type": "stt.partial", "text": "안녕"},
            {"type": "stt.partial", "text": "안녕 하세요"},
        ])

    def test_output_transcript_broadcasts_translation_and_schedules_flush(self) -> None:
        buffers, cb, rec = self._build()

        async def scenario():
            await cb.on_content(GeminiServerContent("", "Hello", (), False, False))
            await cb.on_content(GeminiServerContent("", " world", (), False, False))

        _run(scenario())
        self.assertEqual(buffers.output, "Hello world")
        self.assertEqual(len(rec["translations"]), 2)
        text, partial, _src = rec["translations"][-1]
        self.assertEqual(text, "Hello world")
        self.assertTrue(partial)
        self.assertEqual(rec["idle_flush_calls"], 2)

    def test_turn_complete_commits_output(self) -> None:
        buffers, cb, rec = self._build()

        async def scenario():
            await cb.on_content(GeminiServerContent("src", "out", (), False, False))
            await cb.on_content(GeminiServerContent("", "", (), True, False))

        _run(scenario())
        self.assertEqual(len(rec["commits"]), 1)
        reason, out, src = rec["commits"][0]
        self.assertEqual(reason, "turn_complete")
        self.assertEqual(out, "out")
        self.assertEqual(src, "src")
        # Commit cleared buffers.
        self.assertEqual(buffers.output, "")
        self.assertEqual(buffers.source, "")

    def test_interrupted_clears_buffers_and_fires_hook(self) -> None:
        buffers, cb, rec = self._build()

        async def scenario():
            await cb.on_content(GeminiServerContent("src", "out", (), False, False))
            await cb.on_content(GeminiServerContent("", "", (), False, True))

        _run(scenario())
        self.assertEqual(buffers.source, "")
        self.assertEqual(buffers.output, "")
        self.assertEqual(rec["interrupted_calls"], 1)

    def test_audio_chunks_are_broadcast(self) -> None:
        _buffers, cb, rec = self._build()

        async def scenario():
            await cb.on_content(
                GeminiServerContent("", "", ("A", "B", "C"), False, False)
            )

        _run(scenario())
        self.assertEqual(rec["audio"], ["A", "B", "C"])

    def test_send_error_wraps_in_type_error_message(self) -> None:
        _buffers, cb, rec = self._build()

        async def scenario():
            await cb.send_error("boom")

        _run(scenario())
        self.assertEqual(rec["producer"], [{"type": "error", "message": "boom"}])

    def test_full_dispatch_through_production_callbacks(self) -> None:
        """End-to-end: feed real serverContent events through the SAME
        callbacks builder main.py uses, driven by run_recovery_dispatch,
        across a successful rotation. This is the production-route
        integration test the reviewer asked for."""
        _buffers, cb, rec = self._build()

        ws1 = MockWS([
            _setup_complete(),
            _content(input_text="src1", output_text="out1", audio=["aud1"]),
            _resumption("h1"),
            _goaway(),
        ])
        ws2 = MockWS(
            [
                _setup_complete(),
                _content(input_text=" src2", output_text=" out2", audio=["aud2"]),
                _content(turn_complete=True),
            ],
            close_code=1000,
        )
        session, _ = _make_session([ws1, ws2])

        async def scenario():
            await session.open()
            await run_recovery_dispatch(session, cb)
            await session.close()

        _run(scenario())
        # Error-free: no "type":"error" producer frame.
        self.assertEqual(
            [m for m in rec["producer"] if m.get("type") == "error"], []
        )
        # Transcripts delivered across rotation boundary.
        partials = [m["text"] for m in rec["producer"] if m.get("type") == "stt.partial"]
        self.assertEqual(partials[-1], "src1 src2")
        self.assertEqual(rec["translations"][-1][0], "out1 out2")
        self.assertEqual(rec["audio"], ["aud1", "aud2"])
        # turn_complete committed.
        self.assertEqual(len(rec["commits"]), 1)
        self.assertEqual(rec["commits"][0][0], "turn_complete")


if __name__ == "__main__":
    unittest.main()
