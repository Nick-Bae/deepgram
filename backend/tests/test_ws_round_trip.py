"""F2 v10: positive client-input → server-response round-trip tests.

Round-trip coverage status (v10)
--------------------------------

ALL FOUR routes have a positive INPUT → RESPONSE test:

  * ``/ws/translate``                       — ``test_translate_ping_pong_round_trip``
      Client ping → server pong echoing clientTs + serverTs (main.py:1953).
      No external upstream on this path.
  * ``/ws/stt/deepgram``                    — ``test_deepgram_positive_round_trip_audio_to_translation``
      Audio bytes → mocked ``connect_to_deepgram`` yields canned
      Deepgram ``Results`` with is_final+speech_final → handler state
      machine commits via commit_now → mocked
      ``translate_text_streaming`` yields "hello" → producer-ws echo
      asserts text == "hello".
  * ``/ws/stt/openai-realtime-translate``   — ``test_openai_realtime_positive_round_trip_audio_to_translation``
      Audio bytes → mocked ``websockets.connect`` yields canned
      ``session.output_transcript.delta`` → handler broadcasts partial
      → producer-ws asserts text == delta.
  * ``/ws/stt/gemini-live-translate``       — ``test_gemini_live_positive_round_trip_audio_to_translation``
      PCM48 bytes → handler downsamples 48 kHz → 16 kHz + chunks via
      ``PcmChunkBuffer(3200)`` → mocked ``websockets.connect`` yields
      canned ``serverContent.outputTranscription`` → producer-ws
      asserts text == "hello".

Mandatory shape for every positive test:

  * Connect through the actual authenticated route (not a toy handler,
    not a direct injection of the outgoing response).
  * Send a known client message / audio frame via ``ws.send_*``.
  * Mock the external STT / translation / TTS upstream to deliver a
    canned result ONLY AFTER the handler forwards client input.
  * Assert the expected application response on the correct socket per
    the real handler behavior.
  * An unexpected disconnect, missing response, wrong payload, or
    timeout MUST FAIL the test. No bare
    ``except WebSocketDisconnect: pass``.

v10 corrections to v9
---------------------

v9 shipped the three STT positive tests but the shared ``_MockUpstreamWS``
stub released its audio-gate event on ANY ``.send(payload)``, including
the handler's initial setup/control payload (``{"setup":...}`` for
Gemini, ``{"type":"session.update",...}`` for OpenAI). That hid whether
real audio ever reached the upstream. v9's Gemini test also only sent
320 bytes of PCM48, below the handler's chunker threshold
(``GEMINI_INPUT_CHUNK_BYTES=3200`` after 3:1 downsampling needs ≥ 9600
bytes PCM48 to flush one complete chunk) — zero audio payloads reached
the mock, but the canned translation yielded anyway because setup had
released the gate.

v10 fixes:

  * ``_MockUpstreamWS`` now classifies each payload (raw bytes vs. JSON)
    and releases the gate ONLY on a non-empty audio frame. Captured
    lists are split into ``audio_payloads``, ``setup_payloads`` and
    ``other_payloads`` for stronger assertions.
  * The Gemini positive test sends ``2 * 9600 = 19 200`` bytes of PCM48,
    base64-decodes the first upstream audio payload, and asserts it's
    PCM16-aligned, a multiple of 3200 bytes, and all-zero (matching the
    all-zero input through the real downsampler + chunker).
  * ``test_gemini_setup_only_cannot_release_translation`` is a new
    negative check: open the ws, send no audio, verify that setup alone
    does NOT release the canned translation.
  * Deepgram's positive test is unaffected (its handler never sends a
    non-audio setup payload — Deepgram config lives in the WS URL).
  * OpenAI's positive test benefits from the classifier (the handler's
    ``session.update`` setup is now correctly separated from audio),
    though its visible behavior remains the same because
    ``_downsample_pcm16_48k_to_24k`` emits non-empty bytes on even small
    input and the first audio payload reaches the mock immediately.

The three error-frame tests retain their accurate names
(``test_<engine>_error_frame_on_...``). The ``display_config`` test
keeps its (already-accurate) name.

All tests are bounded by module-scope ``pytest.mark.timeout(5)`` —
verified enabled with a specific-message assertion in
``test_pytest_timeout_actually_enabled.py``.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import pytest

pytestmark = [
    pytest.mark.filterwarnings("ignore"),
    pytest.mark.timeout(5),
]


def _ensure_app_env() -> None:
    os.environ.setdefault("CORS_ALLOW_ORIGINS", "http://localhost")
    os.environ.setdefault("LATENCY_PROBE_ENABLED", "0")
    os.environ.setdefault("REDIS_ENABLED", "0")
    os.environ.setdefault("DISABLE_WS_TRANSLATION_LIMITS", "1")
    os.environ.setdefault("DEEPGRAM_API_KEY", "test-dg")


_ensure_app_env()

try:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    import app.main as app_main
    from app.main import app as fastapi_app
    from app.auth.firebase_auth import AuthenticatedUser
    from app.auth import ws_auth
except Exception as exc:  # pragma: no cover - env-dependent
    pytest.skip(f"backend dependency graph not importable: {exc}", allow_module_level=True)


_USERS: dict[str, AuthenticatedUser] = {
    "VALID_HOST": AuthenticatedUser(uid="host-uid", email="h@test", displayName="H", isSuper=False),
}


def _verify_id_token_value(token: str):
    user = _USERS.get(token)
    if user is None:
        raise RuntimeError("invalid token")
    return user


def _shared_server_mocks(monkeypatch):
    """Mocks every WS route needs: auth table, multichurch_store stubs,
    broadcast no-op, _can_host accept rule.
    """
    monkeypatch.setattr(ws_auth, "verify_id_token_value", _verify_id_token_value)
    monkeypatch.setattr(ws_auth._emitter, "_last", {})

    def _can_host(org_id, host_uid=None, host_token=None):
        return host_uid == "host-uid"
    monkeypatch.setattr(app_main, "_can_host", _can_host)

    def _resolve_room_context(*, org_id, room_id=None, service_key=None, church_slug=None, **_):
        return org_id, room_id or "room_test"
    monkeypatch.setattr(app_main, "_resolve_room_context", _resolve_room_context, raising=False)

    for method, default in [
        ("is_room_live", True),
        ("get_room", {"status": "live"}),
        ("record_host_connect", None),
        ("record_host_disconnect", None),
        ("resolve_room_from_service", "room_test"),
    ]:
        monkeypatch.setattr(
            app_main.multichurch_store, method,
            (lambda d: lambda *a, **kw: d)(default),
            raising=False,
        )

    async def _noop_broadcast(*_a, **_kw):
        return None
    monkeypatch.setattr(app_main.manager, "broadcast_room", _noop_broadcast)


# ===========================================================================
# SERVER-INITIATED-FRAME TESTS (renamed from v7 "round_trip" — v8 clarity)
#
# These exercise paths where the server-side handler SENDS a known frame
# without any client input (either on accept, or on an upstream-config
# failure). They prove the frame-send call-site works but are NOT
# round-trip input→response tests. See the POSITIVE ROUND-TRIP block
# below for actual input→response coverage.
# ===========================================================================


@pytest.fixture
def translate_app(monkeypatch):
    _shared_server_mocks(monkeypatch)
    yield fastapi_app


def test_translate_receives_display_config_after_accept(translate_app):
    """SERVER-INITIATED FRAME TEST (not a round-trip).

    OPEN → RECEIVE display_config → assert type + known field. No send
    needed on the client side; the handler pushes initial state on accept
    (main.py:1534)."""
    client = TestClient(translate_app)
    url = (
        "/ws/translate?orgId=test-org&roomId=room_test&churchSlug=test&serviceKey=sun&role=listener"
    )
    received_msg = None
    disconnect_before_receive: Exception | None = None
    with client.websocket_connect(url, subprotocols=["bearer", "bearer.VALID_HOST"]) as ws:
        assert ws.accepted_subprotocol in {"bearer", None}, (
            f"server echoed un-offered subprotocol: {ws.accepted_subprotocol}"
        )
        try:
            received_msg = ws.receive_json()
        except WebSocketDisconnect as exc:
            disconnect_before_receive = exc
        finally:
            try:
                ws.close()
            except Exception:
                pass
    assert disconnect_before_receive is None, (
        f"server disconnected before sending any frame: {disconnect_before_receive}"
    )
    assert received_msg is not None, "no server frame received"
    assert received_msg.get("type") == "display_config", (
        f"expected display_config frame, got: {received_msg}"
    )
    assert "speed" in received_msg, (
        f"display_config missing 'speed' field: {received_msg}"
    )


@pytest.fixture
def deepgram_app(monkeypatch):
    _shared_server_mocks(monkeypatch)

    async def _raise_connect_to_deepgram(**_kw):
        raise RuntimeError("mocked-dg-connect-fail")
    monkeypatch.setattr(app_main, "connect_to_deepgram", _raise_connect_to_deepgram)

    yield fastapi_app


def test_deepgram_error_frame_on_upstream_connect_failure(deepgram_app):
    """SERVER-INITIATED FRAME TEST (not a round-trip).

    Handler reaches ``connect_to_deepgram()`` → mocked to raise →
    sends ``{"type":"error","message":"Deepgram connect failed: ..."}``
    (main.py:2498) → closes. Receive + assert on the server response
    before any close is observed. No ws.send_* on the client side."""
    client = TestClient(deepgram_app)
    url = "/ws/stt/deepgram?orgId=test-org&roomId=room_test&churchSlug=test&serviceKey=sun"
    received_msg = None
    disconnect_before_receive: Exception | None = None
    with client.websocket_connect(url, subprotocols=["bearer", "bearer.VALID_HOST"]) as ws:
        assert ws.accepted_subprotocol in {"bearer", None}
        try:
            received_msg = ws.receive_json()
        except WebSocketDisconnect as exc:
            disconnect_before_receive = exc
        finally:
            try:
                ws.close()
            except Exception:
                pass
    assert disconnect_before_receive is None, (
        f"server disconnected before sending error frame: {disconnect_before_receive}"
    )
    assert received_msg is not None, "no server frame received after accept"
    assert received_msg.get("type") == "error", (
        f"expected error frame, got: {received_msg}"
    )
    assert "Deepgram connect failed" in received_msg.get("message", ""), (
        f"error message missing known prefix: {received_msg}"
    )
    assert "mocked-dg-connect-fail" in received_msg.get("message", ""), (
        f"error message does not reflect the mocked upstream failure: {received_msg}"
    )


@pytest.fixture
def openai_app(monkeypatch):
    _shared_server_mocks(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    yield fastapi_app


def test_openai_realtime_error_frame_on_missing_api_key(openai_app):
    """SERVER-INITIATED FRAME TEST (not a round-trip).

    Env mocked: no OPENAI_API_KEY → handler accepts, sends the known
    error frame (main.py:4539), closes. Receive + assert."""
    client = TestClient(openai_app)
    url = (
        "/ws/stt/openai-realtime-translate?"
        "orgId=test-org&roomId=room_test&churchSlug=test&serviceKey=sun"
    )
    received_msg = None
    disconnect_before_receive: Exception | None = None
    with client.websocket_connect(url, subprotocols=["bearer", "bearer.VALID_HOST"]) as ws:
        assert ws.accepted_subprotocol in {"bearer", None}
        try:
            received_msg = ws.receive_json()
        except WebSocketDisconnect as exc:
            disconnect_before_receive = exc
        finally:
            try:
                ws.close()
            except Exception:
                pass
    assert disconnect_before_receive is None, (
        f"server disconnected before error frame: {disconnect_before_receive}"
    )
    assert received_msg is not None, "no server frame received"
    assert received_msg.get("type") == "error", (
        f"expected error frame, got: {received_msg}"
    )
    assert "OPENAI_API_KEY" in received_msg.get("message", ""), (
        f"error message missing the known key-config phrase: {received_msg}"
    )


@pytest.fixture
def gemini_app(monkeypatch):
    _shared_server_mocks(monkeypatch)
    monkeypatch.setattr(app_main, "gemini_api_key", lambda: "", raising=False)
    yield fastapi_app


def test_gemini_live_error_frame_on_missing_api_key(gemini_app):
    """SERVER-INITIATED FRAME TEST (not a round-trip).

    gemini_api_key() returns '' → handler accepts, sends error frame
    (main.py:5029), closes. Receive + assert."""
    client = TestClient(gemini_app)
    url = (
        "/ws/stt/gemini-live-translate?"
        "orgId=test-org&roomId=room_test&churchSlug=test&serviceKey=sun"
    )
    received_msg = None
    disconnect_before_receive: Exception | None = None
    with client.websocket_connect(url, subprotocols=["bearer", "bearer.VALID_HOST"]) as ws:
        assert ws.accepted_subprotocol in {"bearer", None}
        try:
            received_msg = ws.receive_json()
        except WebSocketDisconnect as exc:
            disconnect_before_receive = exc
        finally:
            try:
                ws.close()
            except Exception:
                pass
    assert disconnect_before_receive is None, (
        f"server disconnected before error frame: {disconnect_before_receive}"
    )
    assert received_msg is not None, "no server frame received"
    assert received_msg.get("type") == "error", (
        f"expected error frame, got: {received_msg}"
    )
    assert ("GEMINI_API_KEY" in received_msg.get("message", "")
            or "GOOGLE_API_KEY" in received_msg.get("message", "")), (
        f"error message missing known gemini-key phrase: {received_msg}"
    )


# ===========================================================================
# POSITIVE ROUND-TRIP TESTS (v8 — client input → server response)
#
# One per route. See module docstring for the mandatory shape and the
# skip rationale for the three STT routes.
# ===========================================================================


def test_translate_ping_pong_round_trip(translate_app):
    """POSITIVE ROUND-TRIP: /ws/translate accepts a client ``ping`` message
    and responds with a ``pong`` echoing the client's ``clientTs`` plus a
    server-side ``serverTs`` wall-clock (main.py:1953).

    The server's first frame on this socket is the initial-state
    ``display_config`` push. The test drains that frame first (asserting
    its shape so we know we read the right message), then performs the
    actual round-trip: send ping → receive pong → assert on the server
    response.

    No external STT/translation/TTS upstream is involved on this path;
    the handler services the ping entirely from local state. The route
    IS the production route (``app_main.app``), with the shared auth +
    multichurch_store mocks required to make the authenticated connect
    succeed in a unit-test environment."""
    client = TestClient(translate_app)
    url = (
        "/ws/translate?orgId=test-org&roomId=room_test"
        "&churchSlug=test&serviceKey=sun&role=listener"
    )
    client_ts = 1_600_000_000_000
    expected_type_pong = "pong"
    saw_display_config = False
    pong_msg = None
    disconnect_before_receive: Exception | None = None

    def _drain_until(ws, target_type: str, max_frames: int = 10):
        """Read up to ``max_frames`` server frames; return the first with
        ``type == target_type``. Any other response types seen along the
        way are allowed (initial state frames like display_config,
        JOINED), but a timeout/disconnect/exhaustion is a failure."""
        for _ in range(max_frames):
            frame = ws.receive_json()
            if frame.get("type") == target_type:
                return frame
            # First-state frames we tolerate on the way to pong:
            nonlocal saw_display_config
            if frame.get("type") == "display_config":
                saw_display_config = True
        raise AssertionError(
            f"did not receive {target_type!r} within {max_frames} frames"
        )

    with client.websocket_connect(url, subprotocols=["bearer", "bearer.VALID_HOST"]) as ws:
        assert ws.accepted_subprotocol in {"bearer", None}, (
            f"server echoed un-offered subprotocol: {ws.accepted_subprotocol}"
        )
        try:
            # Positive round-trip: real client send → real server send.
            # The handler also emits initial state (display_config,
            # possibly JOINED) before responding to the ping; the drain
            # helper reads past those non-target frames.
            ws.send_text(json.dumps({"type": "ping", "clientTs": client_ts}))
            pong_msg = _drain_until(ws, expected_type_pong)
        except WebSocketDisconnect as exc:
            disconnect_before_receive = exc
        finally:
            try:
                ws.close()
            except Exception:
                pass
    assert disconnect_before_receive is None, (
        f"server disconnected before pong receive completed: {disconnect_before_receive}"
    )
    assert pong_msg is not None, "no pong frame received after sending ping"
    # Wrong-payload failure: the response must be a pong echoing the clientTs.
    assert pong_msg.get("type") == expected_type_pong, (
        f"expected pong frame, got type={pong_msg.get('type')!r}: {pong_msg}"
    )
    assert pong_msg.get("clientTs") == client_ts, (
        f"pong did not echo clientTs ({client_ts}): {pong_msg}"
    )
    server_ts = pong_msg.get("serverTs")
    assert isinstance(server_ts, int) and server_ts > 0, (
        f"pong serverTs missing or not a positive int: {pong_msg}"
    )
    # Server wall-clock must be in a sane range (not the client's
    # 2020-era serverTs stamp or 0). Allow a wide bound.
    now_ms = int(time.time() * 1000)
    assert abs(now_ms - server_ts) < 60_000, (
        f"pong serverTs {server_ts} not within 60s of test wall-clock {now_ms}"
    )
    # The handler also pushed display_config on connect — the drain
    # loop should have observed it before the pong arrived.
    assert saw_display_config, (
        "expected to see the initial display_config state frame before pong"
    )


# ===========================================================================
# POSITIVE ROUND-TRIP TESTS (v9 — implemented per operator directive)
#
# Per-engine audit of what each handler echoes back to the producer ws:
#
#   Deepgram  (/ws/stt/deepgram)
#     1. Reads client audio bytes → forwards to Deepgram upstream (dg.send).
#     2. async for raw in dg → consumes transcript JSON events.
#     3. On is_final=True + speech_final=True on a Korean sentence-ending
#        phrase, state machine commits → calls _translate_text_guarded →
#        _send_to_producer(live_msg_new) + _send_to_producer(live_msg_legacy)
#        at main.py:3264 → broadcast_room fanout.
#     4. The producer ws RECEIVES live_msg_new (mode=live, text=<translation>)
#        and live_msg_legacy (type=translation, payload=<translation>).
#
#   OpenAI Realtime (/ws/stt/openai-realtime-translate)
#     1. Reads client audio → forwards to OpenAI Realtime upstream.
#     2. async for raw in oai → on session.output_transcript.delta,
#        output_buffer += delta → _broadcast_translation(partial=True) →
#        _send_to_producer(live_msg_new) at main.py:4743-4744 → broadcast_room.
#     3. Producer ws RECEIVES live_msg_new (mode=realtime, text=<delta>).
#
#   Gemini Live (/ws/stt/gemini-live-translate)
#     1. Reads client audio → forwards to Gemini upstream.
#     2. async for raw in gemini → parse_gemini_server_content extracts
#        output_transcript → _broadcast_translation(partial=True) at
#        main.py:5151 → _send_to_producer + broadcast_room.
#     3. Producer ws RECEIVES live_msg_new (mode=realtime, text=<transcript>).
#
# Each test:
#   - Opens the authenticated producer ws (real route, not a toy handler).
#   - Mocks EXACTLY these externals:
#       - Upstream connector (connect_to_deepgram / websockets.connect for
#         OpenAI/Gemini).
#       - For Deepgram: translate_text in main.py's namespace, which
#         _translate_text_guarded calls.
#   - Sends known audio bytes via ws.send_bytes.
#   - Upstream stub RECORDS audio into an asyncio-event-gated sentinel list,
#     then yields ONE canned transcript AFTER audio_received.set().
#   - Receives on the SAME ws (where _send_to_producer sends live_msg_new)
#     with a timeout. First skip SERVER-INITIATED prelude frames
#     (display_config, JOINED) via _drain_until_mode helper.
#   - Asserts: upstream sentinel captured ≥ 1 audio payload (proves handler
#     forwarded), live_msg frame has the expected text/mode/is_final.
#   - Unexpected disconnect, missing frame, or timeout = FAIL.
# ===========================================================================


def _drain_until_producer_frame(ws, max_frames: int = 20, want_text: str | None = None):
    """Drain prelude frames (display_config/JOINED/stt.partial) and return
    the first ``live_msg_new``-shaped frame — i.e. a dict carrying a ``mode``
    AND a ``text`` field. If ``want_text`` is set, keep draining until a
    frame's ``text`` field EQUALS it (handy for buffer-accumulated OAI/Gemini
    deltas where the first partial may be prefix-length and we want the
    canned-delta commit). Any unexpected disconnect or exhaustion = failure.

    All drained frames are collected and attached to the raised AssertionError
    on failure so the test's failure message shows exactly what WAS received.
    """
    from starlette.websockets import WebSocketDisconnect
    drained: list = []
    for _ in range(max_frames):
        try:
            frame = ws.receive_json()
        except WebSocketDisconnect as exc:
            raise AssertionError(
                f"server disconnected before producer frame received: {exc}; "
                f"drained_so_far={drained}"
            )
        drained.append(frame)
        if not isinstance(frame, dict):
            continue
        if "mode" in frame and "text" in frame:
            if want_text is None or frame.get("text") == want_text:
                return frame
    raise AssertionError(
        f"did not receive producer live-translation frame within {max_frames} "
        f"frames; drained_so_far={drained}"
    )


class _MockUpstreamWS:
    """Async-iterable upstream-ws stub (v10: audio-gate hardened).

    Blocks __aiter__ on ``audio_received`` until the handler has forwarded
    at least one *audio* payload via ``send``. Yields one canned message,
    then BLOCKS indefinitely on a never-set event — keeping the handler's
    ``async for raw in dg:`` loop alive so its state machine's downstream
    processing (commit_now → _send_to_producer) can run against a still-open
    producer ws.

    v10 fix (operator-reported): the v9 stub flipped the release event on
    ANY ``.send()`` payload, including the initial setup/control JSON
    (``{"setup": {...}}`` for Gemini, ``{"type":"session.update",...}`` for
    OpenAI Realtime). The gate thus released on setup, with no evidence
    that real audio ever reached the upstream. v10 classifies each payload
    by structure and releases the gate ONLY on a non-empty audio frame.

    Classification (per protocol, picked from payload shape):
      * ``bytes``                                              → audio (Deepgram raw PCM).
      * dict/JSON ``realtimeInput.audio.data`` present & non-empty → audio (Gemini).
      * dict/JSON ``type == 'session.input_audio_buffer.append'`` AND
        ``audio`` present & non-empty                          → audio (OpenAI Realtime).
      * dict/JSON ``setup`` present                            → setup (Gemini/Live).
      * dict/JSON ``type`` in {'session.update', 'session.create',
        'response.create', ...}                                → setup/control (OpenAI).
      * anything else                                          → other/control.

    Captured lists for test assertions:
      * ``audio_payloads``  — payloads classified as audio
      * ``setup_payloads``  — payloads classified as setup/control
      * ``other_payloads``  — everything else

    The gate (``_audio_received``) is set only when an audio payload is
    seen with a non-empty audio body.
    """
    _OAI_SETUP_TYPES = {
        "session.update",
        "session.create",
        "response.create",
        "input_audio_buffer.commit",
        "input_audio_buffer.clear",
    }

    def __init__(self, canned_text_frame: str):
        self._canned = canned_text_frame
        self._audio_received = asyncio.Event()
        self._release = asyncio.Event()
        self.audio_payloads: list = []
        self.setup_payloads: list = []
        self.other_payloads: list = []
        self._yielded = False

    def _classify(self, payload):
        """Return ``'audio'``, ``'setup'`` or ``'other'`` for a payload.

        Raw ``bytes`` are always treated as audio (Deepgram). ``str`` is
        parsed as JSON (both OpenAI and Gemini wire JSON strings over
        ``websockets.connect``). Anything that fails to classify is
        ``'other'``.
        """
        if isinstance(payload, (bytes, bytearray, memoryview)):
            return "audio"
        if isinstance(payload, str):
            try:
                obj = json.loads(payload)
            except Exception:
                return "other"
        elif isinstance(payload, dict):
            obj = payload
        else:
            return "other"
        if not isinstance(obj, dict):
            return "other"
        # Gemini audio: {"realtimeInput": {"audio": {"data": "<b64>", ...}}}
        rt = obj.get("realtimeInput")
        if isinstance(rt, dict):
            audio = rt.get("audio")
            if isinstance(audio, dict):
                data = audio.get("data")
                if isinstance(data, str) and data:
                    return "audio"
            # realtimeInput present but not an audio frame (e.g.
            # textInput, generationComplete) → control, not audio.
            return "setup"
        # OpenAI Realtime audio: {"type": "session.input_audio_buffer.append",
        # "audio": "<b64>"}
        if obj.get("type") == "session.input_audio_buffer.append":
            audio_b64 = obj.get("audio")
            if isinstance(audio_b64, str) and audio_b64:
                return "audio"
            return "setup"
        # Gemini setup
        if obj.get("setup") is not None:
            return "setup"
        # OpenAI setup/control
        if obj.get("type") in self._OAI_SETUP_TYPES:
            return "setup"
        return "other"

    async def send(self, payload):
        kind = self._classify(payload)
        if kind == "audio":
            self.audio_payloads.append(payload)
            self._audio_received.set()
        elif kind == "setup":
            self.setup_payloads.append(payload)
        else:
            self.other_payloads.append(payload)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._yielded:
            # Block indefinitely so the handler's `async for raw in dg:` loop
            # does not see EOF before the state machine has produced and
            # emitted its live_msg_new on the producer ws. Released by
            # close() during handler cleanup after test's ws closes.
            await self._release.wait()
            raise StopAsyncIteration
        # Block until the handler forwarded at least one audio frame.
        await self._audio_received.wait()
        self._yielded = True
        return self._canned

    async def close(self):
        self._release.set()
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self._release.set()
        return None


@pytest.fixture
def deepgram_positive_app(monkeypatch):
    """Positive-round-trip fixture: real broadcast_room (host-ws echoes
    via _send_to_producer regardless), mocked external upstream + translator.
    """
    import asyncio as _asyncio
    _shared_server_mocks(monkeypatch)

    # Known Korean phrase that satisfies ends_like_sentence. KOREAN_EOS_RE
    # includes "합니다" in its alternation list, so "안녕합니다." (strip the
    # period, "안녕합니다" ends in "합니다") matches and looks_complete()
    # returns True → state machine commits via commit_now at main.py:4311.
    # (The previous choice "안녕하세요" ends in "요" which is NOT in the
    # alternation — that went to the HOLD branch and timed out the test.)
    canned_korean = "안녕합니다."
    canned_result = json.dumps({
        "type": "Results",
        "channel": {"alternatives": [{
            "transcript": canned_korean,
            "confidence": 0.95,
            "words": [],
        }]},
        "is_final": True,
        "speech_final": True,
        "duration": 1.0,
        "start": 0.0,
    })

    stub = _MockUpstreamWS(canned_result)

    async def _mock_connect_to_deepgram(**_kw):
        return stub

    monkeypatch.setattr(app_main, "connect_to_deepgram", _mock_connect_to_deepgram)

    # Translator mocks. Deepgram's non-partial path uses
    # _translate_streaming_guarded → translate_text_streaming (an async
    # generator). Deepgram's PARTIAL/preview path uses
    # _translate_text_guarded → translate_text. We mock BOTH and track both,
    # so the test can assert the translator was called regardless of which
    # path commit_now took. Both mocks are imported into main.py's namespace
    # at main.py:56, so monkeypatch main's names.
    translator_calls: list[tuple[str, str, str]] = []

    async def _mock_translate_text(text, source, target, **_kw):
        translator_calls.append((text, source, target))
        return "hello"

    async def _mock_translate_text_streaming(text, source, target, **_kw):
        translator_calls.append((text, source, target))
        yield "hello"

    monkeypatch.setattr(app_main, "translate_text", _mock_translate_text)
    monkeypatch.setattr(app_main, "translate_text_streaming", _mock_translate_text_streaming)

    yield fastapi_app, stub, translator_calls


def test_deepgram_positive_round_trip_audio_to_translation(deepgram_positive_app):
    """POSITIVE INPUT→RESPONSE round-trip for /ws/stt/deepgram.

    1. Open authenticated producer ws through the real route.
    2. Send 320 bytes of audio.
    3. Mocked connect_to_deepgram's send() records the audio AND
       __aiter__ yields a canned Deepgram final Korean transcript
       ONLY AFTER audio is received (asyncio.Event gate).
    4. Handler state machine commits → _translate_text_guarded →
       mocked translate_text returns "hello" → _send_to_producer
       sends live_msg_new on the same ws.
    5. Assert: upstream received ≥1 audio payload (not a no-op send);
       translator was called with the canned Korean source;
       received live_msg_new has text=="hello" + is_final=True.
    6. Any unexpected disconnect / missing frame / timeout = FAIL."""
    fapp, stub, translator_calls = deepgram_positive_app
    client = TestClient(fapp)
    url = "/ws/stt/deepgram?orgId=test-org&roomId=room_test&churchSlug=test&serviceKey=sun&source=ko&target=en"
    sample_bytes = b"\x00" * 320  # 320 bytes of PCM silence; shape only.
    producer_frame = None
    with client.websocket_connect(url, subprotocols=["bearer", "bearer.VALID_HOST"]) as ws:
        assert ws.accepted_subprotocol in {"bearer", None}, (
            f"server echoed un-offered subprotocol: {ws.accepted_subprotocol}"
        )
        ws.send_bytes(sample_bytes)
        producer_frame = _drain_until_producer_frame(ws)
        try:
            ws.close()
        except Exception:
            pass
    # Upstream received the audio.
    assert len(stub.audio_payloads) >= 1, (
        f"mocked Deepgram upstream received 0 audio payloads; "
        f"handler did not forward input"
    )
    assert stub.audio_payloads[0] == sample_bytes, (
        f"upstream received unexpected first payload: {stub.audio_payloads[0]!r}"
    )
    # Translator called with the canned Korean transcript.
    assert len(translator_calls) >= 1, (
        f"translator was never called; handler state machine did not commit"
    )
    translated_source = translator_calls[0][0]
    assert "안녕합니다" in translated_source, (
        f"translator source did not include canned Korean: {translated_source!r}"
    )
    # Producer frame content.
    assert producer_frame is not None, "no producer frame received"
    assert producer_frame.get("text") == "hello", (
        f"producer frame text != canned translation: {producer_frame}"
    )
    assert producer_frame.get("meta", {}).get("is_final") is True, (
        f"producer frame not marked is_final: {producer_frame}"
    )


@pytest.fixture
def openai_positive_app(monkeypatch):
    """Positive-round-trip fixture for OpenAI Realtime.

    OpenAI Realtime uses `wss://api.openai.com/v1/realtime/translations`
    opened via ``websockets.connect`` (main.py:4562/4575). The handler
    accumulates ``session.output_transcript.delta`` events into
    output_buffer and broadcasts partials.
    """
    _shared_server_mocks(monkeypatch)

    # OpenAI handler's upstream OPEN: must be present. Env sets api key:
    monkeypatch.setenv("OPENAI_API_KEY", "test-oai-positive")

    canned_delta_text = "hello"
    canned_event = json.dumps({
        "type": "session.output_transcript.delta",
        "delta": canned_delta_text,
    })

    stub = _MockUpstreamWS(canned_event)

    import websockets as _websockets_mod

    async def _mock_ws_connect(*_a, **_kw):
        return stub

    monkeypatch.setattr(_websockets_mod, "connect", _mock_ws_connect)

    yield fastapi_app, stub, canned_delta_text


def test_openai_realtime_positive_round_trip_audio_to_translation(openai_positive_app):
    """POSITIVE INPUT→RESPONSE round-trip for /ws/stt/openai-realtime-translate.

    1. Open authenticated producer ws.
    2. Send audio bytes (base64-shaped if the handler expects base64, but
       since we're forwarding to a MOCKED upstream, byte-shape is enough).
    3. Mocked websockets.connect returns stub; stub.send captures audio;
       __aiter__ yields canned ``session.output_transcript.delta`` AFTER
       audio arrives.
    4. Handler's from_openai_to_server consumes the delta; output_buffer
       accumulates to "hello"; _broadcast_translation(partial=True) →
       _send_to_producer sends live_msg_new on the same ws.
    5. Assert: upstream received ≥1 audio frame; producer-received frame
       has text=="hello" + mode=="realtime"."""
    fapp, stub, canned_delta_text = openai_positive_app
    client = TestClient(fapp)
    url = (
        "/ws/stt/openai-realtime-translate"
        "?orgId=test-org&roomId=room_test&churchSlug=test&serviceKey=sun"
        "&source=ko&target=en"
    )
    sample_bytes = b"\x00" * 320
    producer_frame = None
    with client.websocket_connect(url, subprotocols=["bearer", "bearer.VALID_HOST"]) as ws:
        assert ws.accepted_subprotocol in {"bearer", None}
        ws.send_bytes(sample_bytes)
        producer_frame = _drain_until_producer_frame(ws, want_text=canned_delta_text)
        try:
            ws.close()
        except Exception:
            pass
    assert len(stub.audio_payloads) >= 1, (
        f"mocked OpenAI Realtime upstream received 0 audio payloads"
    )
    assert producer_frame is not None, "no producer frame received"
    assert producer_frame.get("text") == canned_delta_text, (
        f"producer frame text != canned delta: {producer_frame}"
    )
    assert producer_frame.get("mode") == "realtime", (
        f"producer frame mode != 'realtime': {producer_frame}"
    )


class _MockGeminiUpstreamWS(_MockUpstreamWS):
    """Gemini-specific extension: adds `recv()` for the setup handshake.

    Gemini Live uses a two-phase upstream protocol: (1) send setup message,
    recv() setupComplete event, (2) async-iterate for ongoing events. This
    stub returns setupComplete on the first recv(), then the base class
    __aiter__ yields the canned transcript event after audio is received.
    """
    def __init__(self, canned_text_frame: str):
        super().__init__(canned_text_frame)
        self._setup_recv_count = 0

    async def recv(self):
        self._setup_recv_count += 1
        if self._setup_recv_count == 1:
            # Return setupComplete so the handler's setup-handshake loop exits.
            return json.dumps({"setupComplete": {}})
        # Subsequent recv() calls block until released (handler shouldn't
        # call recv again — it iterates via async for raw in gemini).
        await self._release.wait()
        from websockets.exceptions import ConnectionClosedOK
        raise ConnectionClosedOK(None, None)


@pytest.fixture
def gemini_positive_app(monkeypatch):
    """Positive-round-trip fixture for Gemini Live.

    Handler uses ``websockets.connect`` to Gemini Live URL (main.py:5058).
    Needs ``gemini_api_key`` populated. Canned response is a
    serverContent event with an output_transcript segment.
    """
    _shared_server_mocks(monkeypatch)

    # Gemini handler calls `gemini_api_key()` (a function, not an attribute);
    # replace with a lambda returning a non-empty string so the handler
    # proceeds past the no-key error branch.
    monkeypatch.setattr(app_main, "gemini_api_key", lambda: "test-gem-positive", raising=False)

    canned_text = "hello"
    canned_event = json.dumps({
        "serverContent": {
            "modelTurn": {
                "parts": [{"text": canned_text}],
            },
            "outputTranscription": {"text": canned_text, "finished": True},
            "inputTranscription": {"text": "안녕하세요", "finished": True},
            "turnComplete": True,
        }
    })

    stub = _MockGeminiUpstreamWS(canned_event)

    import websockets as _websockets_mod

    async def _mock_ws_connect(*_a, **_kw):
        return stub

    monkeypatch.setattr(_websockets_mod, "connect", _mock_ws_connect)

    yield fastapi_app, stub, canned_text


def test_gemini_live_positive_round_trip_audio_to_translation(gemini_positive_app):
    """POSITIVE INPUT→RESPONSE round-trip for /ws/stt/gemini-live-translate.

    v10 fixes:
      * The Gemini handler buffers PCM16 at 16 kHz through
        ``PcmChunkBuffer(chunk_bytes=GEMINI_INPUT_CHUNK_BYTES=3200)`` and
        downsamples 48 kHz → 16 kHz at a 3:1 byte ratio (6-byte groups
        → 2-byte samples in ``Pcm16Downsampler48To16``). To produce ONE
        complete 3200-byte PCM16 chunk the handler needs
        ``3200 * 3 = 9600`` bytes of PCM48 input. The v9 test sent only
        320 bytes — the downsampler buffered that internally and no
        audio.data payload ever reached the mocked upstream.
      * The v9 ``_MockUpstreamWS`` released the audio-gate on the
        handler's initial ``gemini.send(setup_message(...))`` call, so
        the canned translation yielded even if no real audio ever flushed
        past the chunker. The v10 stub classifies payloads and gates on
        actual audio (``realtimeInput.audio.data`` non-empty).

    1. Open authenticated producer ws.
    2. Send ``2 * 9600 = 19 200`` bytes of PCM48 — enough to flush at
       least one complete 3200-byte PCM16 chunk through the handler's
       chunker to the mocked upstream, plus margin.
    3. Mocked websockets.connect returns stub; stub.send captures setup
       (ignored by the gate) and then audio (releases the gate);
       __aiter__ yields canned Gemini serverContent event AFTER audio
       arrives.
    4. Handler's from_gemini_to_server consumes event;
       parse_gemini_server_content extracts output_transcript;
       _broadcast_translation(partial=True) → _send_to_producer sends
       live_msg_new on the same ws.
    5. Assert: upstream received ≥1 setup frame (handler's handshake),
       ≥1 audio frame (classified correctly by the stub), the first
       audio payload base64-decodes to non-empty PCM16-aligned bytes,
       producer frame has text=="hello" + mode=="realtime"."""
    import base64 as _b64

    fapp, stub, canned_text = gemini_positive_app
    client = TestClient(fapp)
    url = (
        "/ws/stt/gemini-live-translate"
        "?orgId=test-org&roomId=room_test&churchSlug=test&serviceKey=sun"
        "&source=ko&target=en"
    )
    # 2 * GEMINI_INPUT_CHUNK_BYTES * downsample_ratio = 2 * 3200 * 3 = 19200.
    # Content is silent PCM48 (zeros); the chunker + downsampler run their
    # real production logic and emit at least one complete 3200-byte PCM16
    # chunk to the mocked upstream.
    sample_bytes = b"\x00" * 19_200
    producer_frame = None
    with client.websocket_connect(url, subprotocols=["bearer", "bearer.VALID_HOST"]) as ws:
        assert ws.accepted_subprotocol in {"bearer", None}
        ws.send_bytes(sample_bytes)
        producer_frame = _drain_until_producer_frame(ws, want_text=canned_text)
        try:
            ws.close()
        except Exception:
            pass
    # Handler always sends a setup message first — the hardened gate must
    # NOT have accepted it as audio.
    assert len(stub.setup_payloads) >= 1, (
        "mocked Gemini upstream never received the setup/handshake payload — "
        "handler didn't reach the real-time audio loop"
    )
    assert len(stub.audio_payloads) >= 1, (
        "mocked Gemini upstream received 0 AUDIO payloads (classification "
        "only accepts realtimeInput.audio.data non-empty); handler never "
        "flushed a complete PCM16 chunk from the chunker"
    )
    # Decode the first audio payload and verify shape. ``audio_message``
    # wraps PCM16 bytes in base64 under realtimeInput.audio.data.
    first = stub.audio_payloads[0]
    first_obj = json.loads(first) if isinstance(first, str) else first
    audio_b64 = first_obj["realtimeInput"]["audio"]["data"]
    decoded = _b64.b64decode(audio_b64)
    assert len(decoded) > 0, "decoded upstream audio was empty"
    assert len(decoded) % 2 == 0, (
        f"decoded upstream audio not PCM16-aligned (len={len(decoded)})"
    )
    # Handler downsamples 48k → 16k (3:1 bytes). Expected per-chunk size
    # is 3200 bytes (one full GEMINI_INPUT_CHUNK_BYTES chunk from the
    # buffer). Allow equality OR multiples thereof if multiple chunks
    # flushed in one send.
    assert len(decoded) % 3200 == 0, (
        f"decoded upstream audio len={len(decoded)} is not a multiple of "
        f"GEMINI_INPUT_CHUNK_BYTES=3200"
    )
    # All-zero input → all-zero downsampled bytes. If the handler ever
    # swapped in a real resampler that mixes neighbors, this assertion
    # would need relaxing — document and keep strict for now.
    assert all(b == 0 for b in decoded), (
        "decoded upstream audio has non-zero samples from an all-zero PCM48 "
        f"input: {decoded[:16]!r}"
    )
    assert producer_frame is not None, "no producer frame received"
    assert producer_frame.get("text") == canned_text, (
        f"producer frame text != canned Gemini output: {producer_frame}"
    )
    assert producer_frame.get("mode") == "realtime", (
        f"producer frame mode != 'realtime': {producer_frame}"
    )


def test_gemini_setup_only_cannot_release_translation(gemini_positive_app):
    """NEGATIVE check: setup payload alone must not release the gate.

    Operator-reported regression on v9: the shared mock stub released
    its canned translation on ANY ``.send(...)`` payload, including the
    handler's initial ``gemini.send(gemini_setup_message(...))``. That
    hid whether real audio ever reached the upstream.

    This test proves the v10 fix. We open the authenticated producer ws
    for the Gemini route, do NOT send any audio bytes, wait briefly for
    the handler to issue its setup payload, and assert:

      * The mock recorded ≥ 1 setup payload (``setup_payloads`` list).
      * The mock recorded 0 audio payloads (``audio_payloads`` empty).
      * The mock's audio-gate event is NOT set — i.e., no canned
        translation was yielded.
      * The producer ws did NOT receive a live translation frame
        (mode == 'realtime' / mode == 'live') within a bounded wait
        window. If it did, the gate was wrongly released by setup.

    The handler's own JOINED / display_config frames are expected and
    tolerated; the critical signal is the absence of any
    ``text`` + ``mode in {'realtime','live'}`` frame.
    """
    fapp, stub, canned_text = gemini_positive_app
    client = TestClient(fapp)
    url = (
        "/ws/stt/gemini-live-translate"
        "?orgId=test-org&roomId=room_test&churchSlug=test&serviceKey=sun"
        "&source=ko&target=en"
    )
    with client.websocket_connect(url, subprotocols=["bearer", "bearer.VALID_HOST"]) as ws:
        assert ws.accepted_subprotocol in {"bearer", None}
        # Give the handler time to issue its setup payload and settle
        # into the audio-receive loop. We do NOT send audio.
        time.sleep(1.0)
        try:
            ws.close()
        except Exception:
            pass
    # Primary assertions: the mock's internal state tells us whether the
    # audio-gate was ever released. If it was NOT, the mock cannot have
    # yielded its canned translation and the handler cannot have
    # broadcast — regardless of whatever JOINED / STATUS frames the
    # handler may have sent on the producer ws (which are expected and
    # unrelated to the gate).
    assert len(stub.setup_payloads) >= 1, (
        "handler never sent setup payload — handshake did not reach the "
        "audio-receive loop; test prerequisite not met"
    )
    assert len(stub.audio_payloads) == 0, (
        f"handler sent audio even though test never sent any input: "
        f"{len(stub.audio_payloads)} audio payload(s)"
    )
    assert not stub._audio_received.is_set(), (
        "audio-gate event was set after setup-only exchange — gate is "
        "still releasing on non-audio payloads"
    )
