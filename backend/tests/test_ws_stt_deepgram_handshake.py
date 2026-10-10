"""Handshake-level tests for `/ws/stt/deepgram` with external services mocked.

F2 follow-up v2. Verifies RFC-6455 subprotocol negotiation and host
authorization enforcement BEFORE any socket accept. External services
(Firebase, multichurch_store, Deepgram upstream) are stubbed.

The test file self-skips if backend's heavy dependency graph is not
importable in the current venv.
"""
from __future__ import annotations

import os
import pytest

pytestmark = pytest.mark.filterwarnings("ignore")


def _ensure_app_env() -> None:
    os.environ.setdefault("CORS_ALLOW_ORIGINS", "http://localhost")
    os.environ.setdefault("LATENCY_PROBE_ENABLED", "0")
    os.environ.setdefault("REDIS_ENABLED", "0")
    os.environ.setdefault("DISABLE_WS_TRANSLATION_LIMITS", "1")
    os.environ.setdefault("DEEPGRAM_API_KEY", "test-dg")
    os.environ.setdefault("OPENAI_API_KEY", "test-oai")


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
    "VALID_HOST":     AuthenticatedUser(uid="host-uid", email="h@test", displayName="H", isSuper=False),
    "VALID_LISTENER": AuthenticatedUser(uid="listener-uid", email="l@test", displayName="L", isSuper=False),
}


def _verify_id_token_value(token: str):
    user = _USERS.get(token)
    if user is None:
        raise RuntimeError("invalid token")
    return user


class _SentinelDeepgramExit(RuntimeError):
    """Sentinel raised by the stubbed Deepgram connector so the handler ends
    its downstream session cleanly after the handshake we want to measure."""


@pytest.fixture
def mocked_app(monkeypatch):
    monkeypatch.setattr(ws_auth, "verify_id_token_value", _verify_id_token_value)
    monkeypatch.setattr(ws_auth._emitter, "_last", {})

    # Host authorization: host-uid is authorized, listener-uid is not.
    def _can_host(org_id, *, host_uid=None, host_token=None):
        if host_token:  # legacy host-token path authorizes any caller
            return True
        return host_uid == "host-uid"
    monkeypatch.setattr(app_main, "_can_host", _can_host)

    # Pretend any requested room is live and belongs to the requested org.
    def _resolve_room_context(*, org_id, room_id=None, service_key=None, church_slug=None, **_):
        return org_id, room_id or "room_test"
    monkeypatch.setattr(app_main, "_resolve_room_context", _resolve_room_context)

    monkeypatch.setattr(app_main.multichurch_store, "is_room_live", lambda *a, **kw: True, raising=False)
    monkeypatch.setattr(app_main.multichurch_store, "get_room", lambda *a, **kw: {"status": "live"}, raising=False)
    monkeypatch.setattr(app_main.multichurch_store, "record_host_connect", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(app_main.multichurch_store, "record_host_disconnect", lambda *a, **kw: None, raising=False)

    async def _noop_broadcast(*_a, **_kw):
        return None
    monkeypatch.setattr(app_main.manager, "broadcast_room", _noop_broadcast)

    # Stub the Deepgram upstream connector so the handler exits cleanly after accept.
    async def _fake_connect_to_deepgram(*_a, **_kw):
        raise _SentinelDeepgramExit("stub")
    monkeypatch.setattr(app_main, "connect_to_deepgram", _fake_connect_to_deepgram, raising=False)

    yield fastapi_app


# ---------- rejection BEFORE accept (close code visible to client) ----------


def test_stt_no_bearer_no_legacy_closes_before_accept(mocked_app):
    """No auth at all → handler closes with 1008 before accept; TestClient surfaces WebSocketDisconnect."""
    client = TestClient(mocked_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
            "/ws/stt/deepgram?orgId=org1&roomId=room_test&churchSlug=c1&serviceKey=s1"
        ) as _ws:
            pass
    # 1008 is the structural-error close code used by the handler for
    # anonymous access.
    assert ei.value.code in (1008, 4401)


def test_stt_invalid_bearer_offered_closes_with_4401(mocked_app):
    """Bearer carrier offered with invalid token → close code 4401 BEFORE accept."""
    client = TestClient(mocked_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
            "/ws/stt/deepgram?orgId=org1&roomId=room_test&churchSlug=c1&serviceKey=s1",
            subprotocols=["bearer", "bearer.NOT_A_TOKEN"],
        ):
            pass
    assert ei.value.code == 4401


def test_stt_valid_bearer_but_not_host_closes_with_4403(mocked_app):
    """Valid listener-only user → _can_host rejects → close 4403 BEFORE accept."""
    client = TestClient(mocked_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
            "/ws/stt/deepgram?orgId=org1&roomId=room_test&churchSlug=c1&serviceKey=s1",
            subprotocols=["bearer", "bearer.VALID_LISTENER"],
        ):
            pass
    assert ei.value.code == 4403


# ---------- host accept — RFC 6455 echo rule --------------------------------


def test_stt_valid_host_echoes_literal_bearer(mocked_app):
    """Valid host → accept, echo `subprotocol="bearer"`. The downstream
    Deepgram-connector stub raises right after accept so the test ends.

    v5 strengthening: flip a flag INSIDE the context body so a
    reject-before-accept (which would skip the body and still be caught by
    `except WebSocketDisconnect`) cannot pass silently."""
    client = TestClient(mocked_app)
    entered = False
    try:
        with client.websocket_connect(
            "/ws/stt/deepgram?orgId=org1&roomId=room_test&churchSlug=c1&serviceKey=s1",
            subprotocols=["bearer", "bearer.VALID_HOST"],
        ) as ws:
            entered = True
            # Server MUST have echoed back the literal 'bearer', not the carrier.
            assert ws.accepted_subprotocol == "bearer"
    except WebSocketDisconnect:
        # The stub Deepgram connector raises after accept; a late disconnect is fine.
        pass
    assert entered, "handshake closed before accept — this test would have silently passed in v4"


def test_stt_legacy_idtoken_query_accepts(mocked_app, caplog):
    """Legacy `idToken=VALID_HOST` on URL still authenticates; deprecation event emitted; no 4401/4403."""
    import logging
    caplog.set_level(logging.WARNING, logger="app.auth.ws_auth")
    client = TestClient(mocked_app)
    entered = False
    try:
        with client.websocket_connect(
            "/ws/stt/deepgram?orgId=org1&roomId=room_test&churchSlug=c1&serviceKey=s1&idToken=VALID_HOST"
        ) as ws:
            entered = True
            # No subprotocol offered → server MUST NOT echo any subprotocol.
            assert ws.accepted_subprotocol in (None, "")
    except WebSocketDisconnect:
        pass
    assert entered, "handshake closed before accept — v5 sentinel"
    emits = [r for r in caplog.records if "legacy_ws_query_auth_used" in r.getMessage()]
    assert len(emits) >= 1


def test_stt_carrier_without_literal_does_not_echo_subprotocol(mocked_app):
    """Carrier-only offer with a valid token → accept, but echo nothing."""
    client = TestClient(mocked_app)
    entered = False
    try:
        with client.websocket_connect(
            "/ws/stt/deepgram?orgId=org1&roomId=room_test&churchSlug=c1&serviceKey=s1",
            subprotocols=["bearer.VALID_HOST"],  # no literal "bearer"
        ) as ws:
            entered = True
            assert ws.accepted_subprotocol in (None, "")
    except WebSocketDisconnect:
        pass
    assert entered, "handshake closed before accept — v5 sentinel"


# ---------- browser-visible close semantics ---------------------------------


def test_stt_reject_before_accept_surfaces_close_code_1008_or_4xxx(mocked_app):
    """
    Browser-visible close semantics note (for the reader of this test):

    When Starlette's `websocket.close(code=N)` fires BEFORE `websocket.accept()`,
    ASGI serves it to the TCP peer as an HTTP 403 denial of the upgrade. The
    WebSocket spec hides the HTTP response from JavaScript, which only sees
    a `close` event with `code=1006` and `wasClean=false` (per RFC 6455 and
    the WHATWG WebSocket spec). TestClient-based tests see the ASGI-level
    close code directly; browser clients do NOT.

    This means a bad-token reject-before-accept at `/ws/stt/deepgram`
    reaches the frontend as code=1006, which the reconnect loop in
    `frontend/lib/useDeepgramProducer.ts` classifies as transient. The
    existing `reconnectAttemptRef` bound and the terminal-close classifier
    together prevent unbounded spin, but a UX improvement — surfacing
    "please sign in again" after N consecutive 1006 opens without any
    bytes received — belongs on a follow-up PR.
    """
    client = TestClient(mocked_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
            "/ws/stt/deepgram?orgId=org1&roomId=room_test&churchSlug=c1&serviceKey=s1",
            subprotocols=["bearer", "bearer.NOT_A_TOKEN"],
        ):
            pass
    assert ei.value.code in (1008, 4401, 4403)
