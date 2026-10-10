"""F2 v6: strict RFC 6455 §4.2.2 browser-rule assertions against the
REAL FastAPI routes (not the toy ASGI handler in
``test_ws_handshake_browser_rules.py``).

Operator correction (point 4a): the v4 toy-handler coverage was not
sufficient. v6 adds route-level assertions:

- When the client offers at least one subprotocol, a successful
  handshake MUST produce a response subprotocol that is one of the
  offered values. A response of None while the client offered values
  is a browser-rejected handshake (operator's strict reading).
- The server MUST NEVER echo a carrier value (``bearer.<token>`` or
  ``host-token.<token>``). Only the literal ``"bearer"`` or
  ``"host-token"`` may be echoed.
- The response subprotocol is observable via ``ws.accepted_subprotocol``
  on ``fastapi.testclient.TestClient``'s WebSocket context.

External services mocked (same pattern as the other v6 route tests).
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
    "VALID_HOST": AuthenticatedUser(uid="host-uid", email="h@test", displayName="H", isSuper=False),
    "VALID_LISTENER": AuthenticatedUser(uid="listener-uid", email="l@test", displayName="L", isSuper=False),
}


def _verify_id_token_value(token: str):
    user = _USERS.get(token)
    if user is None:
        raise RuntimeError("invalid token")
    return user


@pytest.fixture
def route_app(monkeypatch):
    monkeypatch.setattr(ws_auth, "verify_id_token_value", _verify_id_token_value)
    monkeypatch.setattr(ws_auth._emitter, "_last", {})

    def _can_host(org_id, host_uid=None, host_token=None):
        if host_token:
            return True
        return host_uid == "host-uid"
    monkeypatch.setattr(app_main, "_can_host", _can_host)

    def _resolve_room_context(*, org_id, room_id=None, service_key=None, church_slug=None, **_):
        return org_id, room_id or "room_test"
    monkeypatch.setattr(app_main, "_resolve_room_context", _resolve_room_context)

    monkeypatch.setattr(app_main.multichurch_store, "is_room_live", lambda *a, **kw: True, raising=False)
    monkeypatch.setattr(app_main.multichurch_store, "get_room", lambda *a, **kw: {"status": "live"}, raising=False)
    monkeypatch.setattr(app_main.multichurch_store, "record_host_connect", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(app_main.multichurch_store, "record_host_disconnect", lambda *a, **kw: None, raising=False)
    try:
        monkeypatch.setattr(
            app_main.multichurch_store, "resolve_room_from_service",
            lambda *a, **kw: "room_test", raising=False,
        )
    except Exception:
        pass

    async def _noop_broadcast(*_a, **_kw):
        return None
    monkeypatch.setattr(app_main.manager, "broadcast_room", _noop_broadcast)

    yield fastapi_app


def _assert_strict_browser_rule(ws, offered: list[str]) -> None:
    """Shared strict assertion.

    - If offered is empty, echoed subprotocol MAY be None.
    - If offered is non-empty:
        - If echoed is None, this is a browser-rejected handshake under
          the operator's strict reading. Fail unless the handler
          deliberately returns None for a bad offer (carrier-only etc).
        - If echoed is non-None, it MUST be in `offered` AND MUST NOT
          contain a `.` (no carrier echo).
    """
    echoed = ws.accepted_subprotocol
    if not offered:
        assert echoed is None, f"server echoed {echoed!r} for empty offer"
        return
    if echoed is None:
        # Explicit allowed-None reason: carrier-only, no literal paired.
        # In that case, the handler returns None by design (see
        # select_ws_subprotocol). All other None echoes with a complete
        # literal+carrier offer are browser-rejected.
        has_bearer_literal = "bearer" in offered
        has_bearer_carrier = any(p.startswith("bearer.") for p in offered)
        has_ht_literal = "host-token" in offered
        has_ht_carrier = any(p.startswith("host-token.") for p in offered)
        complete_bearer_pair = has_bearer_literal and has_bearer_carrier
        complete_ht_pair = has_ht_literal and has_ht_carrier
        assert not (complete_bearer_pair or complete_ht_pair), (
            f"Browser rule violation: client offered a COMPLETE literal+carrier pair "
            f"{offered!r} but server echoed None. Browser would reject this handshake."
        )
        return
    assert echoed in offered, (
        f"Browser rule violation: server echoed {echoed!r} which is NOT in offered list {offered!r}. "
        f"Browsers would reject this handshake."
    )
    assert "." not in echoed, (
        f"Server echoed carrier value {echoed!r} — token leaked in subprotocol handshake response."
    )


def test_translate_echoes_bearer_when_literal_offered(route_app):
    """/ws/translate with host-role offer: literal+carrier → echoes 'bearer'."""
    client = TestClient(route_app)
    url = "/ws/translate?orgId=test-org&roomId=room_test&churchSlug=test&serviceKey=sun&role=host"
    offered = ["bearer", "bearer.VALID_HOST"]
    body_entered = False
    with client.websocket_connect(url, subprotocols=offered) as ws:
        body_entered = True
        _assert_strict_browser_rule(ws, offered)
    assert body_entered


def test_translate_carrier_only_response_is_none(route_app):
    """Carrier-only offer (NO literal) → server returns None (correct per
    the back-compat path). This is NOT a browser-compatible configuration
    — it exists only for CLI/test clients that do not implement the
    literal+carrier pattern. Documented as such in both reports.
    """
    client = TestClient(route_app)
    url = "/ws/translate?orgId=test-org&roomId=room_test&churchSlug=test&serviceKey=sun&role=host"
    offered = ["bearer.VALID_HOST"]
    body_entered = False
    with client.websocket_connect(url, subprotocols=offered) as ws:
        body_entered = True
        # Server MUST NOT echo "bearer" here because the client did not
        # offer the literal. This triggers the back-compat / "malformed
        # subprotocol offer" branch which accepts without an echo.
        echoed = ws.accepted_subprotocol
        assert echoed is None, (
            f"server echoed {echoed!r} for carrier-only offer — would violate RFC 6455"
        )
    assert body_entered


def test_translate_mixed_offers_echo_valid_literal(route_app):
    """Both bearer and host-token literal+carrier pairs offered → echoes
    a literal that is in the offered list."""
    client = TestClient(route_app)
    url = "/ws/translate?orgId=test-org&roomId=room_test&churchSlug=test&serviceKey=sun&role=host"
    offered = ["bearer", "bearer.VALID_HOST", "host-token", "host-token.SHARE"]
    body_entered = False
    with client.websocket_connect(url, subprotocols=offered) as ws:
        body_entered = True
        _assert_strict_browser_rule(ws, offered)
    assert body_entered


def test_stt_deepgram_echoes_bearer_with_strict_rule(route_app, monkeypatch):
    """Deepgram STT host handshake obeys the strict browser rule."""
    # Patch upstream so handler exits cleanly after accept.
    async def _fake_connect(*_a, **_kw):
        class _Stub:
            async def send(self, _m): raise RuntimeError("stub")
            async def recv(self): raise RuntimeError("stub")
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration
            async def close(self): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
        return _Stub()
    import websockets as _wsmod
    monkeypatch.setattr(_wsmod, "connect", _fake_connect, raising=True)

    client = TestClient(route_app)
    url = "/ws/stt/deepgram?orgId=test-org&roomId=room_test&churchSlug=test&serviceKey=sun"
    offered = ["bearer", "bearer.VALID_HOST"]
    body_entered = False
    try:
        with client.websocket_connect(url, subprotocols=offered) as ws:
            body_entered = True
            _assert_strict_browser_rule(ws, offered)
    except WebSocketDisconnect:
        # Handler may close after reaching the stub exit; the strict
        # browser-rule assertion ran inside the body before close.
        pass
    assert body_entered


def test_stt_deepgram_rejects_invalid_token_with_disconnect(route_app):
    """Invalid token → 4401 close BEFORE accept. Must NOT enter body."""
    client = TestClient(route_app)
    url = "/ws/stt/deepgram?orgId=test-org&roomId=room_test&churchSlug=test&serviceKey=sun"
    body_entered = False
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            url, subprotocols=["bearer", "bearer.INVALID"]
        ) as ws:
            # If body enters at all, the server accepted a bad token.
            body_entered = True
            _ = ws.accepted_subprotocol  # pragma: no cover - must not reach
    assert not body_entered, "reject-before-accept must prevent body from running"
    assert exc.value.code in {4401, 1008}, f"unexpected close code: {exc.value.code}"
