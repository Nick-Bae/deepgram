"""F2 follow-up v2: WS subprotocol-based bearer auth + legacy fallback + RFC-6455 selection + hostToken."""
from __future__ import annotations

import logging

import pytest

from app.auth import ws_auth
from app.auth.firebase_auth import AuthenticatedUser


@pytest.fixture(autouse=True)
def _reset_emitter(monkeypatch):
    monkeypatch.setattr(ws_auth._emitter, "_last", {})
    yield


def _fake_user(uid: str = "uid-abc", is_super: bool = False) -> AuthenticatedUser:
    return AuthenticatedUser(uid=uid, email=f"{uid}@test", displayName="Test", isSuper=is_super)


# ---------- subprotocol selector (RFC 6455 echo) ----------------------------


def test_select_subprotocol_requires_both_literal_and_carrier():
    assert ws_auth.select_ws_bearer_subprotocol(["bearer", "bearer.jwt-xyz"]) == "bearer"
    assert ws_auth.select_ws_bearer_subprotocol(["chat", "bearer", "bearer.jwt-xyz"]) == "bearer"


def test_select_subprotocol_carrier_without_literal_returns_none():
    assert ws_auth.select_ws_bearer_subprotocol(["bearer.jwt-xyz"]) is None


def test_select_subprotocol_literal_without_carrier_returns_none():
    assert ws_auth.select_ws_bearer_subprotocol(["bearer"]) is None


def test_select_subprotocol_empty_offer_returns_none():
    assert ws_auth.select_ws_bearer_subprotocol([]) is None


def test_select_subprotocol_rejects_bare_bearer_dot():
    assert ws_auth.select_ws_bearer_subprotocol(["bearer", "bearer."]) is None


# ---------- bearer extraction + fallback ------------------------------------


def test_subprotocol_valid_token_returns_user_no_legacy(monkeypatch):
    monkeypatch.setattr(ws_auth, "verify_id_token_value", lambda t: _fake_user() if t == "good.token.sig" else None)
    user, legacy = ws_auth.extract_ws_bearer_sync(
        subprotocol_header="bearer, bearer.good.token.sig",
        legacy_query_token=None,
        path="/ws/translate",
    )
    assert user is not None and user.uid == "uid-abc" and legacy is False


def test_subprotocol_invalid_token_returns_none(monkeypatch):
    monkeypatch.setattr(ws_auth, "verify_id_token_value", lambda _t: None)
    user, legacy = ws_auth.extract_ws_bearer_sync(
        subprotocol_header="bearer, bearer.not.a.real.token",
        legacy_query_token=None,
        path="/ws/translate",
    )
    assert user is None and legacy is False


def test_subprotocol_verify_raises_returns_none(monkeypatch):
    def _raise(_t):
        raise RuntimeError("firebase down")
    monkeypatch.setattr(ws_auth, "verify_id_token_value", _raise)
    user, legacy = ws_auth.extract_ws_bearer_sync(
        subprotocol_header="bearer, bearer.whatever",
        legacy_query_token=None,
        path="/ws/translate",
    )
    assert user is None and legacy is False


def test_no_subprotocol_no_legacy_returns_none(monkeypatch):
    monkeypatch.setattr(ws_auth, "verify_id_token_value", lambda t: _fake_user() if t == "good" else None)
    user, legacy = ws_auth.extract_ws_bearer_sync(
        subprotocol_header=None,
        legacy_query_token=None,
        path="/ws/translate",
    )
    assert user is None and legacy is False


def test_legacy_query_param_fallback_valid_token_emits_deprecation(monkeypatch, caplog):
    monkeypatch.setattr(ws_auth, "verify_id_token_value", lambda t: _fake_user(uid="legacy-uid") if t == "legacy.token.sig" else None)
    caplog.set_level(logging.WARNING, logger="app.auth.ws_auth")
    user, legacy = ws_auth.extract_ws_bearer_sync(
        subprotocol_header=None,
        legacy_query_token="legacy.token.sig",
        path="/ws/translate",
    )
    assert user is not None and user.uid == "legacy-uid" and legacy is True
    recs = [r for r in caplog.records if "legacy_ws_query_auth_used" in r.getMessage()]
    assert len(recs) == 1
    assert "legacy-uid" not in recs[0].getMessage()
    assert "/ws/translate" in recs[0].getMessage() and "uid_hash=" in recs[0].getMessage()


def test_legacy_fallback_rate_limited_per_uid(monkeypatch, caplog):
    monkeypatch.setattr(ws_auth, "verify_id_token_value", lambda t: _fake_user(uid="repeat") if t == "ok" else None)
    caplog.set_level(logging.WARNING, logger="app.auth.ws_auth")
    for _ in range(5):
        ws_auth.extract_ws_bearer_sync(
            subprotocol_header=None,
            legacy_query_token="ok",
            path="/ws/stt/deepgram",
        )
    emits = [r for r in caplog.records if "legacy_ws_query_auth_used" in r.getMessage()]
    assert len(emits) == 1


def test_subprotocol_wins_over_legacy_when_both_present(monkeypatch, caplog):
    def _verify(t):
        return _fake_user(uid="from-sub") if t == "sub.good" else (_fake_user(uid="from-legacy") if t == "legacy.good" else None)
    monkeypatch.setattr(ws_auth, "verify_id_token_value", _verify)
    caplog.set_level(logging.WARNING, logger="app.auth.ws_auth")
    user, legacy = ws_auth.extract_ws_bearer_sync(
        subprotocol_header="bearer, bearer.sub.good",
        legacy_query_token="legacy.good",
        path="/ws/translate",
    )
    assert user is not None and user.uid == "from-sub" and legacy is False
    assert [r for r in caplog.records if "legacy_ws_query_auth_used" in r.getMessage()] == []


def test_malformed_offer_still_verifies_but_emits_deprecation(monkeypatch, caplog):
    sentinel_uid = "SENTINEL_RAW_UID_X91Z"
    monkeypatch.setattr(ws_auth, "verify_id_token_value", lambda t: _fake_user(uid=sentinel_uid) if t == "ok" else None)
    caplog.set_level(logging.WARNING, logger="app.auth.ws_auth")
    user, legacy = ws_auth.extract_ws_bearer_sync(
        subprotocol_header="bearer.ok",
        legacy_query_token=None,
        path="/ws/translate",
    )
    assert user is not None and user.uid == sentinel_uid and legacy is False
    emits = [r for r in caplog.records if "malformed_ws_subprotocol_offer" in r.getMessage()]
    assert len(emits) == 1
    assert sentinel_uid not in emits[0].getMessage()
    assert "uid_hash=" in emits[0].getMessage()


def test_subprotocol_header_with_additional_entries_still_parses(monkeypatch):
    monkeypatch.setattr(ws_auth, "verify_id_token_value", lambda t: _fake_user() if t == "jwt-bytes" else None)
    user, legacy = ws_auth.extract_ws_bearer_sync(
        subprotocol_header="chat, bearer, bearer.jwt-bytes, foo",
        legacy_query_token=None,
        path="/ws/translate",
    )
    assert user is not None and legacy is False


def test_subprotocol_header_without_bearer_entry_returns_none(monkeypatch):
    monkeypatch.setattr(ws_auth, "verify_id_token_value", lambda t: _fake_user())
    user, legacy = ws_auth.extract_ws_bearer_sync(
        subprotocol_header="other.proto, something.else",
        legacy_query_token=None,
        path="/ws/translate",
    )
    assert user is None and legacy is False


# ---------- host-token subprotocol ------------------------------------------


def test_host_token_from_subprotocol():
    tok, legacy = ws_auth.extract_ws_host_token_sync(
        subprotocol_header="bearer, bearer.jwt-bytes, host-token, host-token.room-123-abc",
        legacy_query_token=None,
        path="/ws/translate",
    )
    assert tok == "room-123-abc"
    assert legacy is False


def test_host_token_legacy_query_fallback_emits_deprecation(caplog):
    caplog.set_level(logging.WARNING, logger="app.auth.ws_auth")
    tok, legacy = ws_auth.extract_ws_host_token_sync(
        subprotocol_header="bearer, bearer.jwt-bytes",
        legacy_query_token="HT-legacy-xyz",
        path="/ws/translate",
        uid_hash_for_log="abcd1234",
    )
    assert tok == "HT-legacy-xyz"
    assert legacy is True
    emits = [r for r in caplog.records if "legacy_ws_hosttoken_query_used" in r.getMessage()]
    assert len(emits) == 1
    assert "HT-legacy-xyz" not in emits[0].getMessage()
    assert "abcd1234" in emits[0].getMessage()


def test_host_token_absent_both_sides_returns_none():
    tok, legacy = ws_auth.extract_ws_host_token_sync(
        subprotocol_header="bearer, bearer.jwt-bytes",
        legacy_query_token=None,
        path="/ws/translate",
    )
    assert tok is None and legacy is False


# ---------- hashed uid safety -----------------------------------------------


def test_hashed_uid_is_short_hex():
    h = ws_auth._hashed_uid("some-firebase-uid")
    assert len(h) == 8
    int(h, 16)


def test_hashed_uid_handles_none_and_empty():
    assert ws_auth._hashed_uid(None) == "-"
    assert ws_auth._hashed_uid("") == "-"


def test_parse_subprotocol_header_splits_whitespace_tolerant():
    assert ws_auth.parse_subprotocol_header("bearer,bearer.x,chat") == ["bearer", "bearer.x", "chat"]
    assert ws_auth.parse_subprotocol_header("  bearer ,  bearer.x  ") == ["bearer", "bearer.x"]
    assert ws_auth.parse_subprotocol_header(None) == []
    assert ws_auth.parse_subprotocol_header("") == []
