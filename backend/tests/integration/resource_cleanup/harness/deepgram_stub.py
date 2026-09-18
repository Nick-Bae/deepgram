"""Minimal Deepgram-shaped WebSocket server used by the resource-cleanup
integration harness. Accepts an inbound WS connection from the backend's
STT client (`app.deepgram_session.connect_to_deepgram`, gated behind
`DEEPGRAM_ENDPOINT`), reads any binary payloads the client sends, and
returns scripted `Results` frames whose shape matches what the real
Deepgram cloud emits — enough for the backend's transcript parser to
produce a translation and broadcast it.

Scope: exercise the real backend WebSocket + broadcast pipeline against
a controlled provider. No paid credentials, no external network.

Intentionally lax on protocol conformance: only what
`app/deepgram_session.py`'s consumer path actually reads is honored.
Fields the real Deepgram sends but the backend ignores are omitted.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Optional

try:
    import websockets
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "websockets is required for the integration harness — "
        "install via `pip install websockets`"
    ) from exc

# `websockets.serve` (top-level, the current async API) rather than
# the deprecated `websockets.server.serve`.
_ws_serve = websockets.serve


class DeepgramStub:
    """A test-owned Deepgram-shaped WS server. Bind to 127.0.0.1:0
    (any free port) and expose the URL for the backend to point
    `DEEPGRAM_ENDPOINT` at.

    Frames sent to clients (backends) are the minimal shape the STT
    consumer actually reads:

        {
          "type": "Results",
          "is_final": true,
          "speech_final": true,
          "channel": {"alternatives": [{"transcript": "<text>"}]}
        }

    Callers can push a scripted transcript at any time via
    `send_transcript`; every currently-connected backend receives it.
    """

    def __init__(self):
        self._server = None
        self._port: int = 0
        self._clients: set = set()
        self._lock = asyncio.Lock()

    @property
    def port(self) -> int:
        return self._port

    @property
    def endpoint(self) -> str:
        return f"ws://127.0.0.1:{self._port}/v1/listen"

    async def start(self) -> None:
        # `path` is ignored so we accept both /v1/listen and query-string
        # variants — the backend appends query params to DG_ENDPOINT.
        self._server = await _ws_serve(
            self._on_connect,
            host="127.0.0.1",
            port=0,
        )
        sockets = self._server.sockets or []
        if not sockets:
            raise RuntimeError("deepgram stub failed to bind a socket")
        self._port = sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()
        async with self._lock:
            for client in list(self._clients):
                with contextlib.suppress(Exception):
                    await client.close()
            self._clients.clear()

    async def _on_connect(self, ws) -> None:
        async with self._lock:
            self._clients.add(ws)
        try:
            # Drain audio bytes. Backend sends binary frames of PCM; we
            # simply consume them and stay open until the backend closes.
            async for _ in ws:
                pass
        finally:
            async with self._lock:
                self._clients.discard(ws)

    async def send_transcript(self, text: str, *, is_final: bool = True) -> int:
        """Broadcast a scripted Results frame to every currently-
        connected backend. Returns the number of clients that received
        it — the caller can assert > 0 to prove a backend is
        actually reading."""
        frame = {
            "type": "Results",
            "is_final": is_final,
            "speech_final": is_final,
            "start": 0.0,
            "duration": 1.0,
            "channel": {
                "alternatives": [
                    {"transcript": text, "confidence": 0.95, "words": []}
                ]
            },
        }
        payload = json.dumps(frame)
        delivered = 0
        async with self._lock:
            targets = list(self._clients)
        for client in targets:
            try:
                await client.send(payload)
                delivered += 1
            except Exception:
                pass
        return delivered

    async def client_count(self) -> int:
        """Return the number of currently connected provider clients."""
        async with self._lock:
            return len(self._clients)

    async def wait_for_client_count(
        self, expected: int, *, timeout: float = 10.0
    ) -> None:
        """Block until exactly ``expected`` provider clients are connected."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.client_count() == expected:
                return
            await asyncio.sleep(0.05)
        raise TimeoutError(
            f"Deepgram-stub client count did not become {expected} within "
            f"{timeout:.1f}s (observed {await self.client_count()})"
        )

    async def wait_for_client(self, *, timeout: float = 10.0) -> None:
        """Backward-compatible helper: block until at least one client connects."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.client_count() >= 1:
                return
            await asyncio.sleep(0.05)
        raise TimeoutError(
            "no Deepgram-stub client connected within "
            f"{timeout:.1f}s — did the backend STT WS open?"
        )
