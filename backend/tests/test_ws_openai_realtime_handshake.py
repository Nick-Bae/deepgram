"""Handshake-level tests for `/ws/stt/openai-realtime-translate` — v5 real-route.

F2 follow-up v5. Mirrors the Deepgram handshake-test pattern against the
OpenAI Realtime handler. External OpenAI connector + multichurch_store
are stubbed; auth path is the production path (subprotocol-based bearer +
host-token with legacy query fallback).

Success paths assert BOTH:
  (a) the context manager entered — i.e. accept was observed, no
      reject-before-accept — captured via an in-body sentinel flag; and
  (b) the echoed subprotocol matches the production rule.

Reject-before-accept paths use `pytest.raises(WebSocketDisconnect)` with
an explicit close-code assertion.
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


class _SentinelUpstreamExit(RuntimeError):
    """Stubbed upstream OpenAI connector raises this; the handler catches it
    and ends after we have already observed the handshake outcome."""


@pytest.fixture
def mocked_app(monkeypatch):
    monkeypatch.setattr(ws_auth, "verify_id_token_value", _verify_id_token_value)
    monkeypatch.setattr(ws_auth._emitter, "_last", {})

    def _can_host(org_id, *, host_uid=None, host_token=None):
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

    async def _noop_broadcast(*_a, **_kw):
        return None
    monkeypatch.setattr(app_main.manager, "broadcast_room", _noop_broadcast)

    # Stub the OpenAI upstream connector so the handler exits after accept.
    # `websockets.connect` is referenced inside the OpenAI handler branch.
    import app.main as _m

    class _StubUpstream:
        async def send(self, _msg): raise _SentinelUpstreamExit("stub-upstream")
        async def recv(self):       raise _SentinelUpstreamExit("stub-upstream")
        async def close(self):      pass

    async def _fake_connect(*_a, **_kw):
        raise _SentinelUpstreamExit("stub-connect")

    # The handler calls websockets.connect(...). Patch that module reference.
    try:
        import websockets as _websockets_mod
        monkeypatch.setattr(_websockets_mod, "connect", _fake_connect, raising=False)
    except Exception:
        pass

    yield fastapi_app


# ---------- reject-before-accept ----------


def test_oai_invalid_bearer_closes_with_4401(mocked_app):
    client = TestClient(mocked_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
            "/ws/stt/openai-realtime-translate?orgId=org1&roomId=room_test"
            "&churchSlug=c1&serviceKey=s1",
            subprotocols=["bearer", "bearer.NOT_A_TOKEN"],
        ):
            pass  # pragma: no cover — body must not run on reject-before-accept
    assert ei.value.code == 4401


def test_oai_valid_bearer_but_not_host_closes_with_4403(mocked_app):
    client = TestClient(mocked_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
            "/ws/stt/openai-realtime-translate?orgId=org1&roomId=room_test"
            "&churchSlug=c1&serviceKey=s1",
            subprotocols=["bearer", "bearer.VALID_LISTENER"],
        ):
            pass  # pragma: no cover
    assert ei.value.code == 4403


def test_oai_no_auth_at_all_closes_before_accept(mocked_app):
    client = TestClient(mocked_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
            "/ws/stt/openai-realtime-translate?orgId=org1&roomId=room_test"
            "&churchSlug=c1&serviceKey=s1"
        ):
            pass  # pragma: no cover
    # Anonymous (no bearer carrier, no legacy token) → missing_org/host_auth_failed → 1008
    assert ei.value.code in (1008, 4401)


# ---------- accept + echo rule, no swallowed disconnect ----------


def test_oai_valid_host_echoes_literal_bearer_and_body_actually_ran(mocked_app):
    """
    v5 strengthened: this test proves the context-manager body ACTUALLY RAN
    (accept observed). If the handler closed before accept, `entered=True`
    would never flip and the final assertion would fire.
    """
    client = TestClient(mocked_app)
    entered = False
    try:
        with client.websocket_connect(
            "/ws/stt/openai-realtime-translate?orgId=org1&roomId=room_test"
            "&churchSlug=c1&serviceKey=s1",
            subprotocols=["bearer", "bearer.VALID_HOST"],
        ) as ws:
            entered = True
            assert ws.accepted_subprotocol == "bearer"
    except WebSocketDisconnect:
        # Late upstream-stub exit is fine; we already asserted inside the body.
        pass
    assert entered, (
        "handshake was closed before accept — the swallowed-WebSocketDisconnect "
        "pattern previously allowed this to pass silently; v5 flips a flag "
        "inside the body to catch it."
    )


def test_oai_carrier_without_literal_does_not_echo_subprotocol(mocked_app):
    client = TestClient(mocked_app)
    entered = False
    try:
        with client.websocket_connect(
            "/ws/stt/openai-realtime-translate?orgId=org1&roomId=room_test"
            "&churchSlug=c1&serviceKey=s1",
            subprotocols=["bearer.VALID_HOST"],  # no literal "bearer"
        ) as ws:
            entered = True
            assert ws.accepted_subprotocol in (None, "")
    except WebSocketDisconnect:
        pass
    assert entered


def test_oai_legacy_idtoken_query_accepts_with_deprecation(mocked_app, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="app.auth.ws_auth")
    client = TestClient(mocked_app)
    entered = False
    try:
        with client.websocket_connect(
            "/ws/stt/openai-realtime-translate?orgId=org1&roomId=room_test"
            "&churchSlug=c1&serviceKey=s1&idToken=VALID_HOST"
        ) as ws:
            entered = True
            # No subprotocol offered → server MUST NOT echo any.
            assert ws.accepted_subprotocol in (None, "")
    except WebSocketDisconnect:
        pass
    assert entered
    emits = [r for r in caplog.records if "legacy_ws_query_auth_used" in r.getMessage()]
    assert len(emits) >= 1
