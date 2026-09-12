# backend/app/socket_manager.py
from __future__ import annotations

import asyncio
import time
from typing import Dict, Set, Tuple

from fastapi import WebSocket

from app.services.redis_pubsub import pubsub


RoomKey = Tuple[str, str]

_WARN_INTERVAL_SEC = 60.0
_last_warn_at: Dict[str, float] = {}


def _throttled_warn(key: str, msg: str) -> None:
    """Log a warning at most once per _WARN_INTERVAL_SEC per key."""
    now = time.monotonic()
    if now - _last_warn_at.get(key, 0.0) < _WARN_INTERVAL_SEC:
        return
    _last_warn_at[key] = now
    print(f"[REDIS_PUBSUB][warn] {msg}")


def _msg_type(message) -> str:
    if isinstance(message, dict):
        return str(message.get("type") or message.get("mode") or "?")
    return "?"


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
        prev_became_empty = False
        if prev_key:
            bucket = self.connections_by_room.get(prev_key)
            if bucket:
                bucket.discard(ws)
                if not bucket:
                    self.connections_by_room.pop(prev_key, None)
                    prev_became_empty = True

        bucket = self.connections_by_room.setdefault(key, set())
        new_room_first_ws = not bucket
        bucket.add(ws)
        self.room_by_ws[ws] = key
        assigned_role = (role or "listener").strip().lower() or "listener"
        self.role_by_ws[ws] = assigned_role
        if assigned_role == "host":
            self.hostless_since_by_room.pop(key, None)
        elif self.room_host_count(key[0], key[1]) == 0:
            self.hostless_since_by_room.setdefault(key, time.monotonic())

        # Pub/Sub refcount hooks — fire and forget; pubsub uses an internal lock.
        if pubsub.enabled:
            if new_room_first_ws:
                _schedule(pubsub.ensure_subscription(key[0], key[1]))
            if prev_became_empty and prev_key:
                _schedule(pubsub.release_subscription(prev_key[0], prev_key[1]))
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
        became_empty = False
        if key:
            bucket = self.connections_by_room.get(key)
            if bucket:
                bucket.discard(ws)
                if not bucket:
                    self.connections_by_room.pop(key, None)
                    became_empty = True
            if self.room_host_count(key[0], key[1]) == 0:
                self.hostless_since_by_room.setdefault(key, time.monotonic())
            else:
                self.hostless_since_by_room.pop(key, None)
            if pubsub.enabled and became_empty:
                _schedule(pubsub.release_subscription(key[0], key[1]))

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

    def note_room_host_activity(self, org_id: str, room_id: str) -> None:
        key: RoomKey = ((org_id or "").strip(), (room_id or "").strip())
        if not key[0] or not key[1]:
            return
        self.hostless_since_by_room.pop(key, None)

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
        # Legacy (null org/room) path — local instance only. See design §2.
        if pubsub.enabled:
            _throttled_warn(
                "legacy_broadcast",
                f"manager.broadcast() called under REDIS_ENABLED=1 (msg_type={_msg_type(message)}) — "
                "stays on this instance only; will not reach listeners on other instances",
            )
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_room(self, org_id: str, room_id: str, message):
        """Fan-out to every listener in (org, room) across all Cloud Run instances.

        Delivery is always local-first. When Redis Pub/Sub is connected, we
        additionally publish so subscribers on OTHER instances deliver to their
        own sockets. The subscriber loop on THIS instance filters out
        messages it published (envelope.publisher == INSTANCE_ID), so the
        publisher never double-delivers to its own listeners.

        This design eliminates the subscription-race that plagued the initial
        Redis rollout (see docs/02-design/features/redis-pubsub-fanout.design.md
        §8a). Local delivery does not depend on subscription state or Redis
        round-trip timing.

        When Redis is disabled or disconnected, delivery is local-only —
        identical behavior to pre-Redis single-instance.
        """
        key: RoomKey = ((org_id or "").strip(), (room_id or "").strip())
        if not key[0] or not key[1]:
            if pubsub.enabled:
                _throttled_warn(
                    "missing_room",
                    f"broadcast_room called without org/room (org={org_id!r} room={room_id!r} "
                    f"msg_type={_msg_type(message)}) — message dropped, not fanned out",
                )
            return
        if pubsub.enabled and pubsub.connected:
            # Deliver locally + publish for other instances, concurrently.
            # publish_room works on a copy of `message`, so the local delivery
            # can't race with `_rseq` stamping.
            await asyncio.gather(
                self._broadcast_local_room(key[0], key[1], message),
                pubsub.publish_room(key[0], key[1], message),
            )
            return
        await self._broadcast_local_room(key[0], key[1], message)

    async def _broadcast_local_room(self, org_id: str, room_id: str, message: dict) -> None:
        key: RoomKey = (org_id, room_id)
        dead = []
        for ws in list(self.connections_by_room.get(key) or set()):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


def _schedule(coro) -> None:
    """Schedule an async coroutine from a sync method without awaiting.

    Safe to call from sync ConnectionManager methods that are invoked inside
    an already-running event loop (all WS handlers). If no loop is running
    (rare — e.g. shutdown), just close the coroutine to avoid RuntimeWarning.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        coro.close()


manager = ConnectionManager()

# Wire the pubsub subscriber back to local delivery.
pubsub.set_delivery_callback(manager._broadcast_local_room)
