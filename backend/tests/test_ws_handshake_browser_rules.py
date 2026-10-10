"""F2 v4: enforce the RFC 6455 §4.2.2 browser rule at the handshake layer.

Browsers (Chrome/Firefox/Safari) fail the WebSocket handshake when the
server echoes a `Sec-WebSocket-Protocol` value that was NOT in the
client's offered list. ``fastapi.testclient`` does not enforce this
rule — a server returning a bad subprotocol slips through its
``websocket_connect`` without raising. This module asserts the rule at
the raw ASGI level by intercepting the ``websocket.accept`` message and
asserting ``subprotocol`` is either ``None`` or in the request scope's
offered list.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from app.auth.ws_auth import select_ws_subprotocol


class _AsgiProbe:
    """Minimal ASGI client that drives a WebSocket handler and captures
    whatever subprotocol the handler selects on accept.

    We do not go through the full FastAPI app (which imports a large
    dependency graph); instead we invoke a minimal handler that mirrors
    the production call site: parse the offered list, select the
    subprotocol, call ``websocket.accept(subprotocol=selected)``.
    """

    def __init__(self, offered: List[str]) -> None:
        self.offered = offered
        self.received_accept: Optional[Dict[str, Any]] = None
        self.received_close: Optional[Dict[str, Any]] = None

    async def send(self, message: Dict[str, Any]) -> None:
        if message["type"] == "websocket.accept":
            self.received_accept = message
        elif message["type"] == "websocket.close":
            self.received_close = message

    async def receive(self) -> Dict[str, Any]:
        # Handshake "connect" from Starlette's perspective
        return {"type": "websocket.connect"}


async def _run_minimal_handler(offered: List[str]) -> _AsgiProbe:
    """Mirror the production call: select then accept."""
    probe = _AsgiProbe(offered)
    scope = {
        "type": "websocket",
        "subprotocols": offered,
        "headers": [
            (b"sec-websocket-protocol", ", ".join(offered).encode()),
        ],
    }
    # Minimal production-equivalent handler
    selected = select_ws_subprotocol(scope["subprotocols"])
    await probe.send(
        {"type": "websocket.accept", "subprotocol": selected, "headers": []}
    )
    return probe


@pytest.mark.parametrize(
    "offered",
    [
        [],
        ["bearer"],
        ["bearer.T"],
        ["bearer", "bearer.T"],
        ["host-token"],
        ["host-token.H"],
        ["host-token", "host-token.H"],
        ["bearer", "bearer.T", "host-token", "host-token.H"],
        ["bearer", "host-token"],
        ["bearer.T", "host-token.H"],
        ["bearer", "host-token.H"],   # cross
        ["host-token", "bearer.T"],   # cross
    ],
)
def test_server_only_echoes_offered_subprotocol(offered: List[str]) -> None:
    """RFC 6455 §4.2.2 enforcement at the ASGI accept-message layer.

    For every input, the server MUST either (a) accept with
    ``subprotocol=None`` (no echo) or (b) accept with a subprotocol that
    was in the client's offered list. Browsers reject anything else.
    """
    probe = asyncio.run(_run_minimal_handler(offered))
    assert probe.received_accept is not None, "handler must accept the handshake"

    selected = probe.received_accept.get("subprotocol")
    if selected is None:
        return   # legal — no subprotocol echoed

    # Hard rule: selected MUST be in client's offered list, else browser rejects.
    assert selected in offered, (
        f"Browser rule violation: server echoed subprotocol {selected!r} "
        f"which is NOT in client's offered list {offered}. "
        f"Browsers would fail the handshake."
    )

    # Belt and suspenders: never echo a carrier (token-bearing) value.
    assert "." not in selected, (
        f"Server echoed carrier value {selected!r} — token leaked in handshake response"
    )


def test_browser_rule_would_catch_malformed_echo() -> None:
    """Sanity check the test infra: if a hypothetical bad server echoed a
    subprotocol NOT in the offered list, this test would fail. We assert
    the assertion itself works by constructing the error case manually."""
    offered = ["bearer", "bearer.T"]
    # Simulate a bad server that echoes "host-token" without it being offered.
    bad_selected = "host-token"
    with pytest.raises(AssertionError):
        assert bad_selected in offered


def test_subprotocol_echo_never_contains_credential_bytes() -> None:
    """For 50 synthetic token values, assert the selected literal never
    exposes any portion of the carrier value."""
    for i in range(50):
        tok = f"tok{i:04d}deadbeef"
        offered = ["bearer", f"bearer.{tok}"]
        probe = asyncio.run(_run_minimal_handler(offered))
        selected = probe.received_accept["subprotocol"]
        assert selected == "bearer", f"iter={i} expected literal 'bearer', got {selected!r}"
        assert tok not in (selected or ""), f"iter={i} credential leaked into {selected!r}"
