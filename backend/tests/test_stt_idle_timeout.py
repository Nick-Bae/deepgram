from __future__ import annotations

import asyncio
import sys
import time
import types
import unittest

sys.modules.setdefault("app.services.google_tts", types.SimpleNamespace(synthesize_async=None))
sys.modules.setdefault("app.services.gemini_flash_tts", types.SimpleNamespace(synthesize_async=None))

from app import main as main_module


class _DummyDeepgram:
    def __init__(self) -> None:
        self.closed = False

    async def send(self, _payload: bytes) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> "_DummyDeepgram":
        return self

    async def __anext__(self) -> str:
        while not self.closed:
            await asyncio.sleep(0.05)
        raise StopAsyncIteration


class _FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.closed = False
        self.close_code: int | None = None

    async def send_json(self, payload: dict[str, str]) -> None:
        self.messages.append(payload)

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        self.close_code = code


class SttIdleTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_watchdog_stops_socket_and_deepgram(self) -> None:
        closed = asyncio.Event()
        deepgram = _DummyDeepgram()
        websocket = _FakeWebSocket()
        last_speech_ts = time.monotonic() - 2.0

        await main_module._stt_idle_watchdog(
            closed=closed,
            last_speech_activity=lambda: last_speech_ts,
            timeout_seconds=1,
            websocket=websocket,
            deepgram=deepgram,
            org_id="test-org",
            room_id="test-room",
        )

        self.assertTrue(closed.is_set())
        self.assertTrue(websocket.closed)
        self.assertEqual(websocket.close_code, 1000)
        self.assertTrue(deepgram.closed)
        self.assertEqual(len(websocket.messages), 1)
        self.assertEqual(websocket.messages[0].get("type"), "error")
        self.assertEqual(websocket.messages[0].get("reason"), "speech_idle_timeout")
        self.assertIn("No speech detected for 1 second", str(websocket.messages[0].get("message") or ""))


if __name__ == "__main__":
    unittest.main()
