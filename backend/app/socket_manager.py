# backend/app/socket_manager.py
from __future__ import annotations

import time
from typing import Dict, Set, Tuple

from fastapi import WebSocket


RoomKey = Tuple[str, str]


class ConnectionManager:
    def __init__(self):
        self.active: Set[WebSocket] = set()
        self.connections_by_room: Dict[RoomKey, Set[WebSocket]] = {}
        self.room_by_ws: Dict[WebSocket, RoomKey] = {}
        self.role_by_ws: Dict[WebSocket, str] = {}
        self.hostless_since_by_room: Dict[RoomKey, float] = {}
        self.host_presence_by_ws: Dict[WebSocket, RoomKey] = {}
        self.host_presence_counts_by_room: Dict[RoomKey, int] = {}

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def join_room(self, ws: WebSocket, org_id: str, room_id: str, role: str = "listener") -> int:
        key: RoomKey = ((org_id or "").strip(), (room_id or "").strip())
        if not key[0] or not key[1]:
            return 0

        prev_key = self.room_by_ws.get(ws)
        if prev_key:
            bucket = self.connections_by_room.get(prev_key)
            if bucket:
                bucket.discard(ws)
                if not bucket:
                    self.connections_by_room.pop(prev_key, None)

        bucket = self.connections_by_room.setdefault(key, set())
        bucket.add(ws)
        self.room_by_ws[ws] = key
        assigned_role = (role or "listener").strip().lower() or "listener"
        self.role_by_ws[ws] = assigned_role
        if assigned_role == "host":
            self.hostless_since_by_room.pop(key, None)
        elif self.room_host_count(key[0], key[1]) == 0:
            self.hostless_since_by_room.setdefault(key, time.monotonic())
        return self.room_viewer_count(key[0], key[1])

    def get_room(self, ws: WebSocket) -> RoomKey | None:
        return self.room_by_ws.get(ws)

    def get_role(self, ws: WebSocket) -> str:
        return self.role_by_ws.get(ws, "listener")

    def note_host_connected(self, ws: WebSocket, org_id: str, room_id: str) -> None:
        key: RoomKey = ((org_id or "").strip(), (room_id or "").strip())
        if not key[0] or not key[1]:
            return
        prev_key = self.host_presence_by_ws.get(ws)
        if prev_key == key:
            self.hostless_since_by_room.pop(key, None)
            return
        if prev_key:
            self._decrement_host_presence(prev_key)
        self.host_presence_by_ws[ws] = key
        self.host_presence_counts_by_room[key] = self.host_presence_counts_by_room.get(key, 0) + 1
        self.hostless_since_by_room.pop(key, None)

    def note_host_disconnected(self, ws: WebSocket) -> None:
        key = self.host_presence_by_ws.pop(ws, None)
        if not key:
            return
        self._decrement_host_presence(key)
        if self.room_host_count(key[0], key[1]) == 0:
            self.hostless_since_by_room.setdefault(key, time.monotonic())

    def _decrement_host_presence(self, key: RoomKey) -> None:
        count = self.host_presence_counts_by_room.get(key, 0) - 1
        if count > 0:
            self.host_presence_counts_by_room[key] = count
        else:
            self.host_presence_counts_by_room.pop(key, None)

    def disconnect(self, ws: WebSocket):
        self.note_host_disconnected(ws)
        self.active.discard(ws)
        key = self.room_by_ws.pop(ws, None)
        self.role_by_ws.pop(ws, None)
        if key:
            bucket = self.connections_by_room.get(key)
            if bucket:
                bucket.discard(ws)
                if not bucket:
                    self.connections_by_room.pop(key, None)
            if self.room_host_count(key[0], key[1]) == 0:
                self.hostless_since_by_room.setdefault(key, time.monotonic())
            else:
                self.hostless_since_by_room.pop(key, None)

    def room_viewer_count(self, org_id: str, room_id: str) -> int:
        key: RoomKey = ((org_id or "").strip(), (room_id or "").strip())
        bucket = self.connections_by_room.get(key) or set()
        count = 0
        for ws in bucket:
            role = self.role_by_ws.get(ws, "listener")
            if role != "host":
                count += 1
        return count

    def room_host_count(self, org_id: str, room_id: str) -> int:
        key: RoomKey = ((org_id or "").strip(), (room_id or "").strip())
        bucket = self.connections_by_room.get(key) or set()
        count = self.host_presence_counts_by_room.get(key, 0)
        for ws in bucket:
            role = self.role_by_ws.get(ws, "listener")
            if role == "host":
                count += 1
        return count

    def note_room_host_absence(self, org_id: str, room_id: str) -> float:
        key: RoomKey = ((org_id or "").strip(), (room_id or "").strip())
        if not key[0] or not key[1]:
            return 0.0
        if self.room_host_count(key[0], key[1]) > 0:
            self.hostless_since_by_room.pop(key, None)
            return 0.0
        started_at = self.hostless_since_by_room.setdefault(key, time.monotonic())
        return max(0.0, time.monotonic() - started_at)

    def forget_room(self, org_id: str, room_id: str) -> None:
        key: RoomKey = ((org_id or "").strip(), (room_id or "").strip())
        if not key[0] or not key[1]:
            return
        self.hostless_since_by_room.pop(key, None)
        self.host_presence_counts_by_room.pop(key, None)
        for ws, ws_key in list(self.host_presence_by_ws.items()):
            if ws_key == key:
                self.host_presence_by_ws.pop(ws, None)

    async def broadcast(self, message):
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_room(self, org_id: str, room_id: str, message):
        key: RoomKey = ((org_id or "").strip(), (room_id or "").strip())
        if not key[0] or not key[1]:
            return
        dead = []
        for ws in list(self.connections_by_room.get(key) or set()):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()
