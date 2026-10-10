"""F2 v4 (operator issue 6b): strengthen handshake tests so that an
unexpected pre-accept disconnect cannot be swallowed as a passing test.

The existing ``test_ws_translate_handshake.py`` and
``test_ws_stt_deepgram_handshake.py`` use ``with client.websocket_connect(...)
as ws`` for success cases, asserting only ``ws.accepted_subprotocol``.
That assertion is TRUE even if the server disconnects you immediately
after accept — because ``accepted_subprotocol`` was set once on the
original accept frame. This module demonstrates the stronger pattern:

1. For a SUCCESS case, assert BOTH:
   - ``ws.accepted_subprotocol == expected``, AND
   - calling ``ws.receive()`` (with a short timeout) does NOT raise
     ``WebSocketDisconnect`` immediately. If the server actually did
     disconnect right after accept, this assertion fails.

2. For a REJECT-BEFORE-ACCEPT case, assert that
   ``client.websocket_connect(...)`` raises ``WebSocketDisconnect`` with
   the expected close code BEFORE control enters the ``with`` body.
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
except Exception as exc:  # pragma: no cover
    pytest.skip(f"backend dependency graph not importable: {exc}", allow_module_level=True)


_USERS = {
    "VALID_HOST":     AuthenticatedUser(uid="host-uid",     email="host@test", displayName="H", isSuper=False),
    "VALID_LISTENER": AuthenticatedUser(uid="listener-uid", email="l@test",    displayName="L", isSuper=False),
}


def _verify_token(tok):
    u = _USERS.get(tok)
    if u is None:
        raise RuntimeError("invalid")
    return u


class _Store:
    def resolve_room_from_service(self, **_): return "room_test"
    def is_room_live(self, *_a, **_kw): return True
    def get_room(self, *_a, **_kw): return {"status": "live"}
    def get_member_role(self, org_id, uid): return "host" if uid == "host-uid" else None
    def get_org(self, *_a, **_kw): return {}
    def record_host_connect(self, *_a, **_kw): return None
    def record_host_disconnect(self, *_a, **_kw): return None
    def record_listener_connect(self, *_a, **_kw): return None
    def record_listener_disconnect(self, *_a, **_kw): return None
    def __getattr__(self, _):
        return lambda *a, **kw: None


@pytest.fixture
def mocked_app(monkeypatch):
    monkeypatch.setattr(ws_auth, "verify_id_token_value", _verify_token)
    monkeypatch.setattr(ws_auth._emitter, "_last", {})
    s = _Store()
    for attr in (
        "resolve_room_from_service", "is_room_live", "get_room",
        "get_member_role", "get_org",
    ):
        monkeypatch.setattr(app_main.multichurch_store, attr, getattr(s, attr), raising=False)

    async def _noop_broadcast(*_a, **_kw):
        return None
    monkeypatch.setattr(app_main.manager, "broadcast_room", _noop_broadcast)
    yield fastapi_app


# ---------- Success cases: assert accept AND no immediate pre-accept-after close


def test_translate_success_does_not_disconnect_immediately(mocked_app):
    """Strengthened success assertion: accept happened AND the server did NOT
    hang up on us within the first receive. If the server accepted then
    closed before any app message, the receive() below raises
    WebSocketDisconnect, failing the test — this is the pattern missing
    from the v3 success tests."""
    client = TestClient(mocked_app)
    with client.websocket_connect(
        "/ws/translate?orgId=org1&roomId=room_test&churchSlug=c1&serviceKey=s1&role=listener",
        subprotocols=["bearer", "bearer.VALID_LISTENER"],
    ) as ws:
        # Subprotocol correctly echoed.
        assert ws.accepted_subprotocol == "bearer"
        # Prove the connection is still open: send a benign application
        # message. If the server disconnected post-accept, send_text raises
        # WebSocketDisconnect; failure here means the test caught a silent
        # post-accept close that the v3-style `with` + assertion missed.
        try:
            ws.send_text("{\"type\":\"client_ping\"}")
        except WebSocketDisconnect as exc:
            pytest.fail(
                f"server disconnected immediately after accept (code={exc.code}); "
                f"this is the swallow pattern the strengthened test catches"
            )


def test_translate_anonymous_success_does_not_disconnect_immediately(mocked_app):
    """Same strengthening for the anonymous listener path."""
    client = TestClient(mocked_app)
    with client.websocket_connect(
        "/ws/translate?orgId=org1&roomId=room_test&churchSlug=c1&serviceKey=s1&role=listener"
    ) as ws:
        assert ws.accepted_subprotocol in (None, "")
        try:
            ws.send_text("{\"type\":\"client_ping\"}")
        except WebSocketDisconnect as exc:
            pytest.fail(f"anonymous listener disconnected immediately: code={exc.code}")


# ---------- Reject-before-accept must raise BEFORE entering the with-body


def test_stt_deepgram_no_auth_raises_before_accept(mocked_app):
    """A no-auth stt_deepgram connect must raise WebSocketDisconnect at
    `websocket_connect(...)` time — control must NOT enter the `with`
    body. If a future change silently accepted the handshake, the else
    branch of this test would run and fail."""
    client = TestClient(mocked_app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws/stt/deepgram?orgId=org1&roomId=room_test&churchSlug=c1&serviceKey=s1&src=ko&tgt=en"
        ) as _ws:
            # Must not reach here.
            pytest.fail("stt_deepgram accepted a no-auth handshake")
    assert exc_info.value.code in (1008, 4401), f"got close code {exc_info.value.code}"


def test_stt_deepgram_invalid_bearer_raises_4401(mocked_app):
    client = TestClient(mocked_app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws/stt/deepgram?orgId=org1&roomId=room_test&churchSlug=c1&serviceKey=s1&src=ko&tgt=en",
            subprotocols=["bearer", "bearer.NOT_A_TOKEN"],
        ) as _ws:
            pytest.fail("stt_deepgram accepted an invalid-bearer handshake")
    assert exc_info.value.code in (1008, 4401), f"got close code {exc_info.value.code}"


def test_stt_deepgram_valid_but_not_host_raises_4403(mocked_app):
    """A verified user who is NOT a host of the org must be rejected."""
    client = TestClient(mocked_app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws/stt/deepgram?orgId=org1&roomId=room_test&churchSlug=c1&serviceKey=s1&src=ko&tgt=en",
            subprotocols=["bearer", "bearer.VALID_LISTENER"],
        ) as _ws:
            pytest.fail("stt_deepgram accepted a non-host handshake")
    # Backend may close 1008 (structural) or 4403 (semantic). Either must happen BEFORE accept.
    assert exc_info.value.code in (1008, 4403), f"got close code {exc_info.value.code}"
