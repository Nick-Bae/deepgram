"""Handshake-level tests for `/ws/stt/gemini-live-translate` — v5 real-route.

F2 follow-up v5. Mirrors the Deepgram/OpenAI handshake-test pattern against
the Gemini Live handler. External Gemini connector + multichurch_store are
stubbed; auth path is the production path (subprotocol-based bearer +
host-token with legacy query fallback).

Success paths assert the context manager actually entered (sentinel flag)
so a swallowed WebSocketDisconnect cannot pass silently.
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
    # Gemini key — handler short-circuits with 1011 if absent AFTER accept.
    os.environ.setdefault("GOOGLE_API_KEY", "test-gem")


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


class _SentinelGeminiExit(RuntimeError):
    """Stub raises this after accept so the test observes handshake only."""


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

    # Gemini key present but connector short-circuits so we don't hit real cloud.
    monkeypatch.setattr(app_main, "gemini_api_key", lambda: "test-gem-key", raising=False)

    # F2 v6 CORRECTION: the handler's actual upstream call is
    #   `gemini = await websockets.connect(gemini_websocket_url(...), ...)`
    # (main.py:5058). The previous v5 version of this test tried to patch
    # names like `gemini_live_connect` / `create_gemini_live_session`
    # which do NOT exist in `app.main` — `monkeypatch.setattr(..., raising=False)`
    # therefore silently did nothing and the handshake test accidentally
    # ran the real `websockets.connect` call. Patch the actual import.
    class _StubUpstream:
        async def send(self, _msg): raise _SentinelGeminiExit("stub-upstream")
        async def recv(self):       raise _SentinelGeminiExit("stub-upstream")
        async def close(self):      pass

    async def _fake_connect(*_a, **_kw):
        raise _SentinelGeminiExit("stub-connect")

    try:
        import websockets as _websockets_mod
        monkeypatch.setattr(_websockets_mod, "connect", _fake_connect, raising=True)
    except Exception as exc:
        import pytest as _pytest
        _pytest.skip(f"could not patch websockets.connect: {exc}", allow_module_level=False)

    yield fastapi_app


# ---------- reject-before-accept ----------


def test_gemini_invalid_bearer_closes_with_4401(mocked_app):
    client = TestClient(mocked_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
            "/ws/stt/gemini-live-translate?orgId=org1&roomId=room_test"
            "&churchSlug=c1&serviceKey=s1",
            subprotocols=["bearer", "bearer.NOT_A_TOKEN"],
        ):
            pass  # pragma: no cover
    assert ei.value.code == 4401


def test_gemini_valid_bearer_but_not_host_closes_with_4403(mocked_app):
    client = TestClient(mocked_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
            "/ws/stt/gemini-live-translate?orgId=org1&roomId=room_test"
            "&churchSlug=c1&serviceKey=s1",
            subprotocols=["bearer", "bearer.VALID_LISTENER"],
        ):
            pass  # pragma: no cover
    assert ei.value.code == 4403


def test_gemini_no_auth_closes_before_accept(mocked_app):
    client = TestClient(mocked_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
            "/ws/stt/gemini-live-translate?orgId=org1&roomId=room_test"
            "&churchSlug=c1&serviceKey=s1"
        ):
            pass  # pragma: no cover
    assert ei.value.code in (1008, 4401)


# ---------- accept + echo rule, strengthened against swallowed disconnect ----------


def test_gemini_valid_host_echoes_literal_bearer_and_body_actually_ran(mocked_app):
    client = TestClient(mocked_app)
    entered = False
    try:
        with client.websocket_connect(
            "/ws/stt/gemini-live-translate?orgId=org1&roomId=room_test"
            "&churchSlug=c1&serviceKey=s1",
            subprotocols=["bearer", "bearer.VALID_HOST"],
        ) as ws:
            entered = True
            assert ws.accepted_subprotocol == "bearer"
    except WebSocketDisconnect:
        pass
    assert entered, (
        "handshake was closed before accept — v5 sentinel flag catches "
        "the previously-swallowed disconnect pattern."
    )


def test_gemini_carrier_without_literal_does_not_echo_subprotocol(mocked_app):
    client = TestClient(mocked_app)
    entered = False
    try:
        with client.websocket_connect(
            "/ws/stt/gemini-live-translate?orgId=org1&roomId=room_test"
            "&churchSlug=c1&serviceKey=s1",
            subprotocols=["bearer.VALID_HOST"],
        ) as ws:
            entered = True
            assert ws.accepted_subprotocol in (None, "")
    except WebSocketDisconnect:
        pass
    assert entered


def test_gemini_legacy_idtoken_query_accepts_with_deprecation(mocked_app, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="app.auth.ws_auth")
    client = TestClient(mocked_app)
    entered = False
    try:
        with client.websocket_connect(
            "/ws/stt/gemini-live-translate?orgId=org1&roomId=room_test"
            "&churchSlug=c1&serviceKey=s1&idToken=VALID_HOST"
        ) as ws:
            entered = True
            assert ws.accepted_subprotocol in (None, "")
    except WebSocketDisconnect:
        pass
    assert entered
    emits = [r for r in caplog.records if "legacy_ws_query_auth_used" in r.getMessage()]
    assert len(emits) >= 1
