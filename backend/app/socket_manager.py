# backend/app/socket_manager.py
from __future__ import annotations

import asyncio
import time
from typing import Callable, Dict, List, Set, Tuple

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
        # STT handlers register a shutdown callback (typically `closed.set`) so
        # close_room_hosts can deterministically unblock `await closed.wait()`
        # in the handler and trigger provider cleanup, rather than waiting on
        # the uvicorn/websockets close handshake (which can take ~10s if the
        # client never ACKs the close frame).
        self.host_shutdown_cb_by_ws: Dict[WebSocket, Callable[[], None]] = {}
        # Hooks fired from _broadcast_local_room when a roomStatus="ended"
        # message is delivered. Runs on EVERY instance (publisher and Redis
        # subscribers), so per-room module-level state kept outside the manager
        # (e.g. main.py's TTS maps) gets cleaned up wherever it lives, not
        # only on the instance that originated End Service.
        self.room_end_hooks: List[Callable[[str, str], None]] = []
        # Local tombstone for rooms whose terminal broadcast has been
        # delivered on THIS instance. Prevents a late listener-disconnect
        # finally from publishing roomStatus="live" for a room that's already
        # ended — the disconnect race that would otherwise send contradictory
        # lifecycle events to sibling instances. Bounded via TTL cleanup.
        self.ended_rooms_local: Dict[RoomKey, float] = {}
        # Tracks which WS actually holds a Redis subscription refcount. Only
        # a ws in this set may schedule release on disconnect — otherwise a
        # failed/cancelled register_host that never got past the exception
        # path could release a refcount it didn't own, decrementing a
        # listener's live subscription.
        self.host_subscription_owned_by_ws: Set[WebSocket] = set()
        # Per-listener subscription ownership. Each register_listener call
        # holds one refcount slot for the room it registered against. Room
        # switches transfer ownership. disconnect releases only when
        # ownership is present. Prevents fire-and-forget ensure/release races
        # from decrementing another listener's live subscription.
        self.listener_subscription_owned_room_by_ws: Dict[WebSocket, RoomKey] = {}

    def register_host_shutdown_callback(self, ws: WebSocket, callback: Callable[[], None]) -> None:
        self.host_shutdown_cb_by_ws[ws] = callback

    def unregister_host_shutdown_callback(self, ws: WebSocket) -> None:
        self.host_shutdown_cb_by_ws.pop(ws, None)

    def register_room_end_hook(self, hook: Callable[[str, str], None]) -> None:
        # Idempotent so repeated app-lifespan startups (tests, hot reloads)
        # don't fire the same hook twice per terminal broadcast.
        if hook not in self.room_end_hooks:
            self.room_end_hooks.append(hook)

    _ENDED_TOMBSTONE_TTL_SEC = 300.0

    def _mark_room_ended_locally(self, org_id: str, room_id: str) -> None:
        key: RoomKey = ((org_id or "").strip(), (room_id or "").strip())
        if not key[0] or not key[1]:
            return
        now = time.monotonic()
        self.ended_rooms_local[key] = now + self._ENDED_TOMBSTONE_TTL_SEC
        # Sweep expired entries opportunistically — bounded map growth without
        # a dedicated cleanup task.
        if len(self.ended_rooms_local) > 512:
            for k, exp in list(self.ended_rooms_local.items()):
                if exp <= now:
                    self.ended_rooms_local.pop(k, None)

    def is_room_locally_ended(self, org_id: str, room_id: str) -> bool:
        key: RoomKey = ((org_id or "").strip(), (room_id or "").strip())
        if not key[0] or not key[1]:
            return False
        expires_at = self.ended_rooms_local.get(key)
        if expires_at is None:
            return False
        if expires_at <= time.monotonic():
            self.ended_rooms_local.pop(key, None)
            return False
        return True

    async def register_listener(
        self,
        ws: WebSocket,
        org_id: str,
        room_id: str,
        role: str = "listener",
    ) -> int:
        """Async listener registration with deterministic subscription readiness.

        Same rationale as register_host: without awaiting the Redis subscription,
        a listener that just joined and is about to enter the message loop can
        miss a terminal broadcast published from another instance during the
        subscription window. The forgotten-viewer leak would return.

        Per-ws ownership prevents a failed/cancelled join from releasing another
        listener's live refcount. Also handles the rare room-switch case
        (consumer_join with a different room) by releasing the old room's
        refcount and acquiring the new one.
        """
        new_key: RoomKey = ((org_id or "").strip(), (room_id or "").strip())
        if not new_key[0] or not new_key[1]:
            return 0
        prev_owned = self.listener_subscription_owned_room_by_ws.get(ws)
        viewer_count = self.join_room(ws, org_id, room_id, role)
        if not pubsub.enabled:
            return viewer_count
        # Room switch: release the previous room's refcount before acquiring
        # the new one. Ownership transfer, not orphan release.
        if prev_owned and prev_owned != new_key:
            self.listener_subscription_owned_room_by_ws.pop(ws, None)
            _schedule(pubsub.release_subscription(prev_owned[0], prev_owned[1]))
        # Already own this exact room — nothing to do.
        if self.listener_subscription_owned_room_by_ws.get(ws) == new_key:
            return viewer_count
        try:
            await pubsub.ensure_subscription(new_key[0], new_key[1])
        except BaseException as exc:
            # Roll back presence. Do NOT touch ownership map — we never added
            # to it on this failure path (ensure_subscription rolled back its
            # own refcount).
            self.disconnect(ws)
            if isinstance(exc, Exception):
                raise
            raise
        self.listener_subscription_owned_room_by_ws[ws] = new_key
        return viewer_count

    async def register_host(self, ws: WebSocket, org_id: str, room_id: str) -> None:
        """Async host registration with deterministic subscription readiness.

        Registers host presence, then awaits Redis subscription readiness.
        Raises RuntimeError if the subscription cannot be established — the
        STT handler must not run a session it cannot terminate reliably
        from another instance. Presence is rolled back before raising so
        the manager stays consistent.

        Ownership: this method adds `ws` to host_subscription_owned_by_ws only
        AFTER ensure_subscription returns (whether ready or not). The
        ensure_subscription call itself rolls back refcount on exception, so
        an exception path here does NOT hold a refcount — note_host_disconnected
        will see the ws is not in the ownership set and won't release. Only
        a successful ensure_subscription (which incremented refcount) yields
        ownership. That closes the "someone else's refcount was decremented"
        race under cancellation/failure.
        """
        self.note_host_connected(ws, org_id, room_id)
        if pubsub.enabled:
            try:
                ready = await pubsub.ensure_subscription(
                    (org_id or "").strip(),
                    (room_id or "").strip(),
                )
            except BaseException as exc:
                # BaseException catches CancelledError too — a cancelled
                # register_host must not leave presence hanging.
                self.note_host_disconnected(ws)
                if isinstance(exc, Exception):
                    raise RuntimeError(f"host_subscription_failed: {exc}") from exc
                raise
            # ensure_subscription incremented the refcount, so this ws now
            # owns one subscription slot regardless of whether the underlying
            # channel is actually live.
            self.host_subscription_owned_by_ws.add(ws)
            if not ready:
                # Redis is enabled but not currently connected. Reader will
                # resubscribe on reconnect, but until then we cannot receive
                # the terminal broadcast from another instance. Safer to
                # refuse the host connection than to run without terminal
                # reachability — note_host_disconnected will release the
                # refcount we just acquired via the ownership check.
                self.note_host_disconnected(ws)
                raise RuntimeError("host_subscription_not_ready")

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

        # Pub/Sub subscription is now handled per-ws by register_listener /
        # register_host — the callers await ensure_subscription and track
        # per-ws ownership. Fire-and-forget from here would race with the
        # post-join is_room_live check on the caller side (subscription may
        # not be active yet, so a terminal broadcast from another instance
        # could be missed).
        return self.room_viewer_count(key[0], key[1])

    def get_room(self, ws: WebSocket) -> RoomKey | None:
        return self.room_by_ws.get(ws)

    def get_role(self, ws: WebSocket) -> str:
        return self.role_by_ws.get(ws, "listener")

    def note_host_connected(self, ws: WebSocket, org_id: str, room_id: str) -> None:
        """Synchronous presence bookkeeping only. Callers that need the
        Redis subscription to be active must use `register_host` (async)
        so subscription readiness is awaited before returning. Keeping this
        method sync preserves compatibility with disconnect() callers that
        can't await."""
        key: RoomKey = ((org_id or "").strip(), (room_id or "").strip())
        if not key[0] or not key[1]:
            return
        prev_key = self.host_presence_by_ws.get(ws)
        if prev_key == key:
            self.hostless_since_by_room.pop(key, None)
            return
        if prev_key:
            self._decrement_host_presence(prev_key)
            # Release Redis subscription for the previous room this ws was
            # attached to (if any) — refcount so listeners on that room can
            # keep their subscription.
            if pubsub.enabled:
                _schedule(pubsub.release_subscription(prev_key[0], prev_key[1]))
        self.host_presence_by_ws[ws] = key
        self.host_presence_counts_by_room[key] = self.host_presence_counts_by_room.get(key, 0) + 1
        self.hostless_since_by_room.pop(key, None)

    def note_host_disconnected(self, ws: WebSocket) -> None:
        # Also drop any registered shutdown callback so it can't leak by ws.
        self.host_shutdown_cb_by_ws.pop(ws, None)
        # Only release a Redis subscription that THIS ws actually acquired.
        # register_host adds to this set only after ensure_subscription
        # returned (either ready or not), so a failed/cancelled registration
        # that never incremented refcount does not decrement anyone else's.
        owned_subscription = ws in self.host_subscription_owned_by_ws
        self.host_subscription_owned_by_ws.discard(ws)
        key = self.host_presence_by_ws.pop(ws, None)
        if not key:
            return
        self._decrement_host_presence(key)
        if self.room_host_count(key[0], key[1]) == 0:
            self.hostless_since_by_room.setdefault(key, time.monotonic())
        if pubsub.enabled and owned_subscription:
            _schedule(pubsub.release_subscription(key[0], key[1]))

    def _decrement_host_presence(self, key: RoomKey) -> None:
        count = self.host_presence_counts_by_room.get(key, 0) - 1
        if count > 0:
            self.host_presence_counts_by_room[key] = count
        else:
            self.host_presence_counts_by_room.pop(key, None)

    def disconnect(self, ws: WebSocket):
        self.note_host_disconnected(ws)
        self.active.discard(ws)
        # Release listener subscription only if THIS ws holds a refcount for
        # a specific room — a fire-and-forget acquire that failed never made
        # the ws an owner, and releasing on failure would decrement another
        # listener's live subscription (the race ChatGPT flagged).
        owned_listener_room = self.listener_subscription_owned_room_by_ws.pop(ws, None)
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
        if pubsub.enabled and owned_listener_room:
            _schedule(pubsub.release_subscription(owned_listener_room[0], owned_listener_room[1]))

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

    async def close_room_hosts(
        self,
        org_id: str,
        room_id: str,
        code: int = 1000,
        reason: str = "room_ended",
    ) -> int:
        """Server-close every host WS registered in (org, room).

        Host STT sockets are tracked in host_presence_by_ws but NOT in
        connections_by_room (STT handlers don't call join_room), so
        close_room_listeners misses them. Without this, End Service leaves
        the Deepgram/OpenAI/Gemini upstream session running until the STT
        handler's own idle watchdog fires — burning provider cost meanwhile.
        """
        key: RoomKey = ((org_id or "").strip(), (room_id or "").strip())
        if not key[0] or not key[1]:
            return 0
        # Snapshot before iterating — close() cascades into disconnect handlers
        # that mutate host_presence_by_ws.
        hosts = [ws for ws, k in list(self.host_presence_by_ws.items()) if k == key]
        # Fire per-host shutdown callbacks synchronously first so the handler's
        # `closed` event is set BEFORE we send the close frame. That way even
        # if the client never ACKs the close, the handler's `await closed.wait()`
        # returns immediately and provider cleanup runs. Without this, the
        # handler would block until uvicorn's close handshake timeout (~10s).
        for ws in hosts:
            cb = self.host_shutdown_cb_by_ws.pop(ws, None)
            if cb is None:
                continue
            try:
                cb()
            except Exception:
                pass
        results = await asyncio.gather(
            *(self._close_one(ws, code, reason) for ws in hosts),
            return_exceptions=True,
        )
        for ws in hosts:
            try:
                self.note_host_disconnected(ws)
            except Exception:
                pass
        return sum(1 for r in results if r is True)

    async def close_room_listeners(
        self,
        org_id: str,
        room_id: str,
        code: int = 1000,
        reason: str = "room_ended",
    ) -> int:
        """Server-close every WebSocket in (org, room) and drop tracking.

        Why: after a room ends, listeners that stay connected (backgrounded
        tabs whose /resolve poll is throttled, sleeping devices) keep a Cloud
        Run socket slot occupied until the LB idle timeout kicks in. Closing
        from the server frees those slots immediately regardless of client
        state.

        Closes run concurrently with a per-socket timeout so a single dead
        or backpressured listener can't stall the rest of the room cleanup
        (or the End Service response).
        """
        key: RoomKey = ((org_id or "").strip(), (room_id or "").strip())
        if not key[0] or not key[1]:
            return 0
        sockets = list(self.connections_by_room.get(key) or set())
        results = await asyncio.gather(
            *(self._close_one(ws, code, reason) for ws in sockets),
            return_exceptions=True,
        )
        for ws in sockets:
            try:
                self.disconnect(ws)
            except Exception:
                pass
        return sum(1 for r in results if r is True)

    async def _close_one(self, ws, code: int, reason: str) -> bool:
        try:
            await asyncio.wait_for(ws.close(code=code, reason=reason), timeout=2.0)
            return True
        except Exception:
            return False

    async def _send_one(self, ws, message: dict) -> bool:
        try:
            await asyncio.wait_for(ws.send_json(message), timeout=2.0)
            return True
        except Exception:
            return False

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
        is_terminal = isinstance(message, dict) and message.get("roomStatus") == "ended"

        # Establish irreversible local terminal state BEFORE any listener can
        # react to the terminal message. Otherwise a fast listener could
        # receive `ended`, disconnect, reach its finally, see other listeners
        # still in the bucket, and broadcast roomStatus="live" — the exact
        # race the tombstone is meant to prevent. Firing hooks up front also
        # ensures per-room module state (TTS maps in main.py) is cleared on
        # subscriber instances before any downstream code reads it.
        if is_terminal:
            self._mark_room_ended_locally(org_id, room_id)
            for hook in list(self.room_end_hooks):
                try:
                    hook(org_id, room_id)
                except Exception:
                    pass

        sockets = list(self.connections_by_room.get(key) or set())

        # Concurrent, per-socket bounded sends. Sequential sends let one
        # backpressured client (dead, sleeping, throttled) stall the terminal
        # roomStatus=ended broadcast — which the pubsub reader awaits, and
        # which the End Service HTTP handler awaits — for the full TCP
        # write/close timeout. Do not let a single slow viewer hold up
        # room shutdown or the reader task on subscriber instances.
        results = await asyncio.gather(
            *(self._send_one(ws, message) for ws in sockets),
            return_exceptions=True,
        )
        failed = [ws for ws, ok in zip(sockets, results) if ok is not True]
        # Close failed-send sockets BEFORE disconnect(). Otherwise disconnect
        # removes them from connections_by_room and the terminal
        # close_room_listeners snapshot below misses them — a timed-out
        # listener would stay untracked but open, which is exactly the leak
        # this branch is meant to prevent.
        if failed:
            # For terminal broadcasts, use the same 1000/room_ended pair
            # close_room_listeners uses. Otherwise the frontend's terminal
            # detection (code 1000 + reason room_ended) misfires and one
            # reconnection attempt happens before /ws/translate rejects it.
            fail_code, fail_reason = (1000, "room_ended") if is_terminal else (1011, "send_failed")
            await asyncio.gather(
                *(self._close_one(ws, fail_code, fail_reason) for ws in failed),
                return_exceptions=True,
            )
            for ws in failed:
                self.disconnect(ws)

        # Auto-close on room-end. Same code path handles single-instance
        # (broadcast_room → this method directly), the Redis publisher instance
        # (broadcast_room → gather(local, publish) → this method), and Redis
        # subscriber instances (reader → callback → this method). Both listener
        # and host sockets are closed here — a host STT socket may be on a
        # different Cloud Run instance than the one that received End Service,
        # and only the instance holding the socket can close it. Without host
        # cleanup here, the Deepgram/OpenAI/Gemini upstream session on a sibling
        # instance keeps running.
        if is_terminal:
            await self.close_room_listeners(org_id, room_id, reason="room_ended")
            await self.close_room_hosts(org_id, room_id, reason="room_ended")


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
