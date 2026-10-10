"""WebSocket subprotocol-based bearer-token authentication.

F2 follow-up v2: moves the Firebase ID token AND the host-upgrade token
off the URL query string onto the WebSocket `Sec-WebSocket-Protocol`
header, which is NOT recorded by Cloud Run's in-container access-log
formatter. Verification runs BEFORE `websocket.accept()` so an
unauthorized caller never gets an accepted connection to a room.

RFC 6455 §4.2.2 compliance
--------------------------

The server MUST echo back exactly one of the subprotocols the client
offered. Browsers (Chrome, Firefox, Safari) fail the handshake if the
server echoes a protocol that was NOT offered. We therefore require the
client to offer the literal `"bearer"` token in ADDITION to the
`bearer.<token>` carrier; the server echoes only the literal `"bearer"`.
The `bearer.<token>` entry is NEVER echoed back to the client, so the
raw token bytes never appear in the handshake response.

Client-side convention
----------------------

Browser (ID token only):

    new WebSocket(url, ["bearer", `bearer.${idToken}`])

Browser (ID token + host-upgrade token):

    new WebSocket(
        url,
        ["bearer", `bearer.${idToken}`, "host-token", `host-token.${hostToken}`]
    )

Firebase ID tokens are URL-safe base64 with dots as segment separators
(per RFC 7519); dots are valid subprotocol characters per RFC 6455 §4.1,
so no additional encoding is required.

Backend contract
----------------

In a WS handler:

    offered = parse_subprotocol_header(ws.headers.get("sec-websocket-protocol"))
    selected = select_ws_subprotocol(offered)   # "bearer" or "host-token" or None
    user, legacy = extract_ws_bearer_sync(
        subprotocol_header=raw_hdr,
        legacy_query_token=qctx.get("idToken"),
        path="/ws/translate",
    )
    host_token, ht_legacy = extract_ws_host_token_sync(
        subprotocol_header=raw_hdr,
        legacy_query_token=qctx.get("hostToken"),
        path="/ws/translate",
    )
    # ... room authorization checks against user and host_token ...
    await ws.accept(subprotocol=selected)   # None → no subprotocol echoed

If a client offers a `bearer.<token>` entry WITHOUT the literal `"bearer"`
first, the server accepts the handshake WITHOUT a subprotocol echo and
emits a `malformed_ws_subprotocol_offer` deprecation event (hashed uid
only; token bytes NEVER logged).

Deprecation telemetry
---------------------

Legacy query-param clients and malformed-offer clients each emit a
rate-limited (one per hashed-uid per 60 s) structured log line so the
operator can see migration progress without having the token or uid in
the log line.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from threading import Lock
from typing import List, Optional, Tuple

from starlette.websockets import WebSocket

from app.auth.firebase_auth import AuthenticatedUser, verify_id_token_value

logger = logging.getLogger(__name__)

_SUBPROTO_BEARER_LITERAL = "bearer"
_SUBPROTO_BEARER_CARRIER = "bearer."
_SUBPROTO_HOSTTOKEN_LITERAL = "host-token"
_SUBPROTO_HOSTTOKEN_CARRIER = "host-token."
_EMIT_INTERVAL_SEC = 60


class _RateLimitedEmitter:
    """One structured line per (event, hashed_uid) per _EMIT_INTERVAL_SEC."""
    def __init__(self) -> None:
        self._last: dict[tuple, float] = {}
        self._lock = Lock()

    def should_emit(self, event: str, uid_hash: str) -> bool:
        now = time.monotonic()
        key = (event, uid_hash)
        with self._lock:
            last = self._last.get(key, 0.0)
            if now - last < _EMIT_INTERVAL_SEC:
                return False
            self._last[key] = now
            if len(self._last) > 10000:
                cutoff = now - _EMIT_INTERVAL_SEC * 2
                stale = [k for k, t in self._last.items() if t <= cutoff]
                for k in stale:
                    self._last.pop(k, None)
            return True


_emitter = _RateLimitedEmitter()


def _hashed_uid(uid: Optional[str]) -> str:
    """Short, salted hash of a uid for deprecation telemetry — never logs the uid."""
    if not uid:
        return "-"
    salt = os.getenv("WS_LEGACY_UID_SALT", "ws-legacy-salt-v1")
    h = hashlib.sha256((salt + ":" + uid).encode("utf-8")).hexdigest()
    return h[:8]


def _instance_id() -> str:
    return (os.getenv("INSTANCE_ID") or "").strip() or "-"


def parse_subprotocol_header(raw: Optional[str]) -> List[str]:
    """Split a `Sec-WebSocket-Protocol` header into stripped entries."""
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def select_ws_subprotocol(offered: List[str]) -> Optional[str]:
    """Return the subprotocol literal the server SHOULD echo on `accept()`.

    RFC 6455 §4.2.2: the selected subprotocol MUST be one of the client's
    offered values. The server echoes a LITERAL ("bearer" or "host-token"),
    NEVER a carrier value (which contains the token/credential).

    Returns:
    - ``"bearer"`` when the client offered BOTH ``"bearer"`` AND at least
      one ``"bearer.<token>"`` carrier.
    - ``"host-token"`` when the client offered BOTH ``"host-token"`` AND
      at least one ``"host-token.<token>"`` carrier, AND no complete
      bearer pair is present.
    - ``None`` otherwise.

    Preference: when BOTH complete pairs are offered, prefer ``"bearer"``.
    Rationale: a Firebase ID token identifies the user and is scoped
    broader than a per-room share token; selecting bearer keeps the
    user-identity path authoritative.
    """
    has_bearer_literal = any(p == _SUBPROTO_BEARER_LITERAL for p in offered)
    has_bearer_carrier = any(
        p.startswith(_SUBPROTO_BEARER_CARRIER) and p != _SUBPROTO_BEARER_CARRIER
        for p in offered
    )
    has_ht_literal = any(p == _SUBPROTO_HOSTTOKEN_LITERAL for p in offered)
    has_ht_carrier = any(
        p.startswith(_SUBPROTO_HOSTTOKEN_CARRIER) and p != _SUBPROTO_HOSTTOKEN_CARRIER
        for p in offered
    )
    if has_bearer_literal and has_bearer_carrier:
        return _SUBPROTO_BEARER_LITERAL
    if has_ht_literal and has_ht_carrier:
        return _SUBPROTO_HOSTTOKEN_LITERAL
    return None


# v3 name was `select_ws_bearer_subprotocol` (bearer-only). v4 renames to
# `select_ws_subprotocol` with host-token support. No external callers in
# this tree used the v3 name directly except via `main.py`, which has
# been updated. The v3 import path is retired.
select_ws_bearer_subprotocol = select_ws_subprotocol  # v3 compat for any out-of-tree importer


def _extract_carrier(offered: List[str], carrier: str) -> Optional[str]:
    for p in offered:
        if p.startswith(carrier):
            tail = p[len(carrier):].strip()
            if tail:
                return tail
    return None


def _extract_bearer_from_subprotocols(protocols: List[str]) -> Optional[str]:
    return _extract_carrier(protocols, _SUBPROTO_BEARER_CARRIER)


def _extract_hosttoken_from_subprotocols(protocols: List[str]) -> Optional[str]:
    return _extract_carrier(protocols, _SUBPROTO_HOSTTOKEN_CARRIER)


def _emit_legacy_query_event(path: str, uid_hash: str) -> None:
    if not _emitter.should_emit("legacy_ws_query_auth_used", uid_hash):
        return
    logger.warning(
        "legacy_ws_query_auth_used path=%s uid_hash=%s instance_id=%s",
        path, uid_hash, _instance_id(),
    )


def _emit_legacy_hosttoken_event(path: str, uid_hash: str) -> None:
    if not _emitter.should_emit("legacy_ws_hosttoken_query_used", uid_hash):
        return
    logger.warning(
        "legacy_ws_hosttoken_query_used path=%s uid_hash=%s instance_id=%s",
        path, uid_hash, _instance_id(),
    )


def _emit_malformed_offer(path: str, uid_hash: str) -> None:
    if not _emitter.should_emit("malformed_ws_subprotocol_offer", uid_hash):
        return
    logger.warning(
        "malformed_ws_subprotocol_offer path=%s uid_hash=%s instance_id=%s detail=carrier_without_literal",
        path, uid_hash, _instance_id(),
    )


def extract_ws_bearer_sync(
    *,
    subprotocol_header: Optional[str],
    legacy_query_token: Optional[str],
    path: str,
) -> Tuple[Optional[AuthenticatedUser], bool]:
    """Verify the ID token offered via subprotocol or legacy query param.

    Returns (user, legacy_used). `user` is None if the token is absent or
    invalid. `legacy_used` is True only when authentication succeeded via
    the LEGACY query-param path.

    Subprotocol takes precedence over the legacy query param. A
    well-formed carrier (`bearer.<token>`) WITHOUT the literal `"bearer"`
    sibling is still accepted for authentication but emits a
    `malformed_ws_subprotocol_offer` deprecation event. The server-side
    caller still must NOT echo a subprotocol on `accept()` in that case
    — use `select_ws_subprotocol()` which returns None.
    """
    offered = parse_subprotocol_header(subprotocol_header)
    sub_token = _extract_bearer_from_subprotocols(offered)
    if sub_token:
        try:
            user = verify_id_token_value(sub_token)
        except Exception:
            return (None, False)
        if user and user.uid:
            if _SUBPROTO_BEARER_LITERAL not in offered:
                _emit_malformed_offer(path, _hashed_uid(user.uid))
            return (user, False)
        return (None, False)
    raw = (legacy_query_token or "").strip()
    if not raw:
        return (None, False)
    try:
        user = verify_id_token_value(raw)
    except Exception:
        return (None, False)
    if user and user.uid:
        _emit_legacy_query_event(path, _hashed_uid(user.uid))
        return (user, True)
    return (None, False)


def extract_ws_host_token_sync(
    *,
    subprotocol_header: Optional[str],
    legacy_query_token: Optional[str],
    path: str,
    uid_hash_for_log: Optional[str] = None,
) -> Tuple[Optional[str], bool]:
    """Return the host-upgrade token offered via subprotocol or legacy query.

    `host-token.<raw>` on the subprotocol list is preferred. Legacy
    `hostToken=<raw>` on the URL still works and emits a
    `legacy_ws_hosttoken_query_used` deprecation event tagged with the
    provided `uid_hash_for_log` (if any) so the operator can correlate
    with the ID-token hash. Returns (token_or_none, legacy_used).
    """
    offered = parse_subprotocol_header(subprotocol_header)
    sub_token = _extract_hosttoken_from_subprotocols(offered)
    if sub_token:
        return (sub_token, False)
    raw = (legacy_query_token or "").strip()
    if not raw:
        return (None, False)
    _emit_legacy_hosttoken_event(path, uid_hash_for_log or "-")
    return (raw, True)


async def extract_ws_bearer(
    ws: WebSocket,
    *,
    legacy_query_token: Optional[str],
    path: str,
) -> Tuple[Optional[AuthenticatedUser], bool]:
    headers = getattr(ws, "headers", None)
    sub_header: Optional[str] = None
    if headers is not None:
        try:
            sub_header = headers.get("sec-websocket-protocol") or headers.get("Sec-WebSocket-Protocol")
        except Exception:
            sub_header = None
    return extract_ws_bearer_sync(
        subprotocol_header=sub_header,
        legacy_query_token=legacy_query_token,
        path=path,
    )


async def extract_ws_host_token(
    ws: WebSocket,
    *,
    legacy_query_token: Optional[str],
    path: str,
    uid_hash_for_log: Optional[str] = None,
) -> Tuple[Optional[str], bool]:
    headers = getattr(ws, "headers", None)
    sub_header: Optional[str] = None
    if headers is not None:
        try:
            sub_header = headers.get("sec-websocket-protocol") or headers.get("Sec-WebSocket-Protocol")
        except Exception:
            sub_header = None
    return extract_ws_host_token_sync(
        subprotocol_header=sub_header,
        legacy_query_token=legacy_query_token,
        path=path,
        uid_hash_for_log=uid_hash_for_log,
    )


async def select_ws_subprotocol_from_ws(ws: WebSocket) -> Optional[str]:
    """Async convenience wrapper: read Sec-WebSocket-Protocol from the WS
    and delegate to the sync :func:`select_ws_subprotocol`."""
    headers = getattr(ws, "headers", None)
    sub_header: Optional[str] = None
    if headers is not None:
        try:
            sub_header = headers.get("sec-websocket-protocol") or headers.get("Sec-WebSocket-Protocol")
        except Exception:
            sub_header = None
    return select_ws_subprotocol(parse_subprotocol_header(sub_header))
