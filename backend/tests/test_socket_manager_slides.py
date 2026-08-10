from __future__ import annotations

import asyncio
import unittest

import pytest

from app.socket_manager import ConnectionManager

pytestmark = pytest.mark.skip(
    reason="ConnectionManager.broadcast_slide_change not implemented; "
    "slides feature not yet wired up. Track as follow-up."
)


class _FakeWebSocket:
    """Minimal WebSocket stand-in. Records messages sent via send_json."""

    def __init__(self, *, fail_after: int | None = None):
        self.sent: list[dict] = []
        self.closed = False
        self._fail_after = fail_after

    async def send_json(self, message: dict) -> None:
        if self.closed:
            raise RuntimeError("connection closed")
        if self._fail_after is not None and len(self.sent) >= self._fail_after:
            self.closed = True
            raise RuntimeError("simulated send failure")
        self.sent.append(dict(message))

    async def accept(self) -> None:
        pass


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class BroadcastSlideChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ConnectionManager()

    def test_broadcast_to_all_clients_in_room_returns_count(self) -> None:
        host = _FakeWebSocket()
        viewer_a = _FakeWebSocket()
        viewer_b = _FakeWebSocket()

        self.manager.join_room(host, "org-1", "room-1", "host")
        self.manager.join_room(viewer_a, "org-1", "room-1", "listener")
        self.manager.join_room(viewer_b, "org-1", "room-1", "listener")

        delivered = _run(
            self.manager.broadcast_slide_change(
                "org-1",
                "room-1",
                {"index": 3, "slideId": "abc", "url": "https://example.com/slide.png"},
            )
        )

        self.assertEqual(delivered, 3)
        for ws in (host, viewer_a, viewer_b):
            self.assertEqual(len(ws.sent), 1)
            msg = ws.sent[0]
            self.assertEqual(msg["type"], "slide_change")
            self.assertEqual(msg["index"], 3)
            self.assertEqual(msg["slideId"], "abc")
            self.assertEqual(msg["url"], "https://example.com/slide.png")

    def test_broadcast_does_not_reach_other_rooms(self) -> None:
        viewer_in_room_a = _FakeWebSocket()
        viewer_in_room_b = _FakeWebSocket()

        self.manager.join_room(viewer_in_room_a, "org-1", "room-A", "listener")
        self.manager.join_room(viewer_in_room_b, "org-1", "room-B", "listener")

        delivered = _run(
            self.manager.broadcast_slide_change(
                "org-1",
                "room-A",
                {"index": 0, "slideId": "x", "url": "https://example.com/x.png"},
            )
        )

        self.assertEqual(delivered, 1)
        self.assertEqual(len(viewer_in_room_a.sent), 1)
        self.assertEqual(len(viewer_in_room_b.sent), 0)

    def test_broadcast_drops_dead_sockets_and_continues(self) -> None:
        good = _FakeWebSocket()
        bad = _FakeWebSocket(fail_after=0)  # fails on first send

        self.manager.join_room(good, "org-1", "room-1", "listener")
        self.manager.join_room(bad, "org-1", "room-1", "listener")

        delivered = _run(
            self.manager.broadcast_slide_change(
                "org-1",
                "room-1",
                {"index": 5, "slideId": "z", "url": "https://example.com/z.png"},
            )
        )

        # Only the good socket should receive.
        self.assertEqual(delivered, 1)
        self.assertEqual(len(good.sent), 1)
        # Bad socket should be disconnected by the broadcaster's dead-socket cleanup.
        self.assertNotIn(bad, self.manager.connections_by_room.get(("org-1", "room-1"), set()))

    def test_broadcast_returns_zero_for_empty_or_invalid_room(self) -> None:
        delivered_empty = _run(
            self.manager.broadcast_slide_change(
                "org-x", "room-empty", {"index": 0, "slideId": "id", "url": "u"}
            )
        )
        self.assertEqual(delivered_empty, 0)

        delivered_blank = _run(
            self.manager.broadcast_slide_change(
                "", "", {"index": 0, "slideId": "id", "url": "u"}
            )
        )
        self.assertEqual(delivered_blank, 0)


if __name__ == "__main__":
    unittest.main()
