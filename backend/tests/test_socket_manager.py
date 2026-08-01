from __future__ import annotations

import unittest
from unittest.mock import patch

from app.socket_manager import ConnectionManager


class ConnectionManagerTests(unittest.TestCase):
    def test_host_absence_timer_starts_when_last_host_disconnects(self) -> None:
        manager = ConnectionManager()
        ws_host = object()

        manager.join_room(ws_host, "org-a", "room-a", "host")
        self.assertEqual(manager.room_host_count("org-a", "room-a"), 1)
        self.assertEqual(manager.note_room_host_absence("org-a", "room-a"), 0.0)

        with patch("app.socket_manager.time.monotonic", side_effect=[100.0, 130.0, 130.0]):
            manager.disconnect(ws_host)
            elapsed = manager.note_room_host_absence("org-a", "room-a")

        self.assertEqual(manager.room_host_count("org-a", "room-a"), 0)
        self.assertGreaterEqual(elapsed, 30.0)

    def test_forget_room_clears_host_absence_tracking(self) -> None:
        manager = ConnectionManager()
        ws_listener = object()

        with patch("app.socket_manager.time.monotonic", return_value=42.0):
            manager.join_room(ws_listener, "org-b", "room-b", "listener")

        self.assertIn(("org-b", "room-b"), manager.hostless_since_by_room)
        manager.forget_room("org-b", "room-b")
        self.assertNotIn(("org-b", "room-b"), manager.hostless_since_by_room)

    def test_producer_host_presence_counts_without_joining_broadcast_room(self) -> None:
        manager = ConnectionManager()
        ws_producer = object()

        manager.note_host_connected(ws_producer, "org-c", "room-c")

        self.assertEqual(manager.room_host_count("org-c", "room-c"), 1)
        self.assertEqual(manager.note_room_host_absence("org-c", "room-c"), 0.0)
        self.assertNotIn(("org-c", "room-c"), manager.connections_by_room)

        with patch("app.socket_manager.time.monotonic", return_value=200.0):
            manager.note_host_disconnected(ws_producer)

        self.assertEqual(manager.room_host_count("org-c", "room-c"), 0)
        self.assertIn(("org-c", "room-c"), manager.hostless_since_by_room)

    def test_host_activity_clears_host_absence_tracking(self) -> None:
        manager = ConnectionManager()
        ws_listener = object()

        with patch("app.socket_manager.time.monotonic", return_value=300.0):
            manager.join_room(ws_listener, "org-d", "room-d", "listener")

        self.assertIn(("org-d", "room-d"), manager.hostless_since_by_room)
        manager.note_room_host_activity("org-d", "room-d")
        self.assertNotIn(("org-d", "room-d"), manager.hostless_since_by_room)


if __name__ == "__main__":
    unittest.main()
