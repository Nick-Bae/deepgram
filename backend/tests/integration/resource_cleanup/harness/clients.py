"""WebSocket + HTTP client helpers, pinned to a specific backend
process's port. Every network hop goes through the real production
surface — no test-only routes.

The primary client here is `ListenerClient`, which speaks the real
`/ws/translate` protocol. `HostClient` speaks the real
`/ws/stt/deepgram` protocol and streams synthetic PCM to the backend
so the STT WS actually opens and lives long enough for the F-15
assertions to hold.

WebSocket API note: the `websockets` library removed the `.closed`
attribute in the asyncio rewrite. Modern code either checks
`ws.state` against `websockets.protocol.State.OPEN` or, more robustly,
handles `ConnectionClosed` on send/recv. We take the second approach
— attempts to use the socket either succeed or raise
`ConnectionClosed`, and the harness treats a raise as "socket
closed."
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any, List, Optional

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("websockets is required for the harness") from exc


def _ws_looks_open(ws) -> bool:
    """Best-effort `is the socket still healthy?` probe that survives
    both the legacy websockets API (`ws.closed`) and the modern one
    (`ws.state`). Callers that need certainty should try to send and
    catch ConnectionClosed."""
    if ws is None:
        return False
    # Modern API: websockets 15+ exposes `state` as an enum.
    state = getattr(ws, "state", None)
    if state is not None:
        # State.OPEN == 1 by value; compare by name to be resilient.
        return getattr(state, "name", "") == "OPEN"
    # Legacy fallback for older websockets versions.
    closed = getattr(ws, "closed", None)
    if closed is not None:
        return not closed
    # If we can't tell, assume open (the caller's send will error out
    # if it isn't; that's the safer default for our assertions).
    return True


async def _cancel_task(task: Optional[asyncio.Task]) -> None:
    """Cancel a task and await its termination without leaking
    `CancelledError` out of the harness teardown path. Suppresses
    every exception a cancelled task can raise."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


class ListenerClient:
    """Connect to `<ws_url>/ws/translate` and collect every incoming
    JSON frame. Matches what the real listener page does."""

    def __init__(self, ws_base_url: str, *, org_id: str, room_id: str, service_key: str, church_slug: str):
        self.ws_base_url = ws_base_url.rstrip("/")
        self.org_id = org_id
        self.room_id = room_id
        self.service_key = service_key
        self.church_slug = church_slug
        self.ws = None
        self.received: List[dict] = []
        self._reader_task: Optional[asyncio.Task] = None
        self._url = (
            f"{self.ws_base_url}/ws/translate"
            f"?orgId={org_id}&roomId={room_id}"
            f"&serviceKey={service_key}&churchSlug={church_slug}"
            f"&role=viewer"
        )

    async def connect(self, *, timeout: float = 15.0) -> None:
        self.ws = await asyncio.wait_for(
            websockets.connect(self._url),
            timeout=timeout,
        )
        # Send the consumer_join payload the real listener page sends.
        await self.ws.send(json.dumps({
            "type": "consumer_join",
            "orgId": self.org_id,
            "roomId": self.room_id,
            "serviceKey": self.service_key,
            "churchSlug": self.church_slug,
            "role": "listener",
        }))
        self._reader_task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        try:
            assert self.ws is not None
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                self.received.append(msg)
        except Exception:
            pass

    async def is_open(self) -> bool:
        return _ws_looks_open(self.ws)

    async def wait_closed(self, *, timeout: float = 10.0) -> None:
        if self.ws is None:
            return
        await asyncio.wait_for(self.ws.wait_closed(), timeout=timeout)

    def close_code(self) -> Optional[int]:
        """Close code observed after `wait_closed()`. `None` if the
        socket is still open (or never opened)."""
        return getattr(self.ws, "close_code", None) if self.ws is not None else None

    def close_reason(self) -> str:
        """Close reason observed after `wait_closed()`. Empty string
        if there is none."""
        return getattr(self.ws, "close_reason", "") or "" if self.ws is not None else ""

    async def wait_for_frame(self, predicate, *, timeout: float = 10.0) -> dict:
        """Poll `self.received` for the first frame matching `predicate`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for msg in self.received:
                if predicate(msg):
                    return msg
            await asyncio.sleep(0.05)
        raise TimeoutError(
            f"no frame matching predicate within {timeout:.1f}s "
            f"(got {len(self.received)} frames)"
        )

    async def wait_for_status_ended(self, *, timeout: float = 10.0) -> None:
        """Assertion helper — waits for a terminal STATUS frame. The
        F-15 test asserts this DOES NOT arrive within the timeout,
        via `assert_no_status_ended`.
        """
        await self.wait_for_frame(
            lambda m: m.get("type") == "STATUS" and m.get("roomStatus") == "ended",
            timeout=timeout,
        )

    async def assert_no_status_ended(self, *, within: float = 5.0) -> None:
        """Assert no terminal STATUS frame arrives within `within`
        seconds. Called from F-15 after instance B starts."""
        try:
            await self.wait_for_status_ended(timeout=within)
        except TimeoutError:
            return
        raise AssertionError(
            f"listener received terminal STATUS(ended) within {within:.1f}s — "
            f"this is the exact F-15 regression PR-T1-A is meant to prevent"
        )

    async def close(self) -> None:
        # Cancellation-safe teardown. Each step is isolated so a
        # failure in one doesn't prevent later ones from running.
        await _cancel_task(self._reader_task)
        self._reader_task = None
        if self.ws is not None:
            try:
                await self.ws.close()
            except (asyncio.CancelledError, Exception):
                pass
            self.ws = None


class HostClient:
    """Connect to `<ws_url>/ws/stt/deepgram` as an authenticated host.
    Streams a small amount of synthetic PCM audio so the backend's
    STT session is actually running when instance B starts.

    Uses the `HOST_API_TOKEN` shared secret rather than a Firebase
    ID token — see `BackendProcess.env()`."""

    def __init__(
        self,
        ws_base_url: str,
        *,
        org_id: str,
        room_id: str,
        service_key: str,
        church_slug: str,
        host_token: str,
    ):
        self.ws_base_url = ws_base_url.rstrip("/")
        self.org_id = org_id
        self.room_id = room_id
        self.service_key = service_key
        self.church_slug = church_slug
        self.host_token = host_token
        self.ws = None
        self.received: List[Any] = []
        self._reader_task: Optional[asyncio.Task] = None
        self._audio_task: Optional[asyncio.Task] = None
        self._url = (
            f"{self.ws_base_url}/ws/stt/deepgram"
            f"?orgId={org_id}&roomId={room_id}"
            f"&serviceKey={service_key}&churchSlug={church_slug}"
            f"&hostToken={host_token}"
            f"&source=ko&target=en"
        )

    async def connect(self, *, timeout: float = 15.0) -> None:
        self.ws = await asyncio.wait_for(
            websockets.connect(self._url),
            timeout=timeout,
        )
        self._reader_task = asyncio.create_task(self._reader())
        self._audio_task = asyncio.create_task(self._audio_pump())

    async def _reader(self) -> None:
        try:
            assert self.ws is not None
            async for raw in self.ws:
                self.received.append(raw)
        except Exception:
            pass

    async def _audio_pump(self) -> None:
        """Send silent PCM frames every 100 ms so the backend's STT
        forward loop stays active. Real audio content isn't necessary
        — the Deepgram stub returns scripted transcripts on demand.

        The `ConnectionClosed` handling matters: on modern websockets
        the socket has no `.closed` attribute, so a naive check would
        loop forever. `ws.send(...)` raises `ConnectionClosed` after
        the peer disconnects, which is our exit signal.
        """
        # 100 ms of 16-bit 16 kHz mono silence.
        silence = b"\x00\x00" * 1600
        try:
            while self.ws is not None and _ws_looks_open(self.ws):
                try:
                    await self.ws.send(silence)
                except ConnectionClosed:
                    return
                except Exception:
                    return
                await asyncio.sleep(0.1)
        except (asyncio.CancelledError, Exception):
            return

    async def is_open(self) -> bool:
        return _ws_looks_open(self.ws)

    async def wait_closed(self, *, timeout: float = 10.0) -> None:
        if self.ws is None:
            return
        await asyncio.wait_for(self.ws.wait_closed(), timeout=timeout)

    def close_code(self) -> Optional[int]:
        return getattr(self.ws, "close_code", None) if self.ws is not None else None

    def close_reason(self) -> str:
        return getattr(self.ws, "close_reason", "") or "" if self.ws is not None else ""

    async def close(self) -> None:
        # Cancellation-safe teardown. Each step isolated.
        await _cancel_task(self._audio_task)
        self._audio_task = None
        await _cancel_task(self._reader_task)
        self._reader_task = None
        if self.ws is not None:
            try:
                await self.ws.close()
            except (asyncio.CancelledError, Exception):
                pass
            self.ws = None
