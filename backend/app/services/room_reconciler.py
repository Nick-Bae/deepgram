"""Redis-independent repair of local resources for Firestore-ended rooms.

Redis Pub/Sub remains the fast path for terminal delivery.  This module is a
periodic safety net for the case where a process misses that one message.  It
reads Firestore as the source of truth and performs only process-local cleanup;
it never writes Firestore and never publishes to Redis.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import time
from typing import Any, Callable, Dict, Optional, Protocol, Set, Tuple


RoomKey = Tuple[str, str]


class RoomStateStore(Protocol):
    def get_room_reconcile_states(
        self,
        room_keys: list[RoomKey],
    ) -> Dict[RoomKey, Optional[Dict[str, Any]]]:
        ...


class LocalRoomManager(Protocol):
    def locally_owned_room_keys(self) -> Set[RoomKey]:
        ...

    def room_has_local_resources(self, org_id: str, room_id: str) -> bool:
        ...

    async def _broadcast_local_room(self, org_id: str, room_id: str, message: dict) -> None:
        ...

    async def forget_room_subscription(self, org_id: str, room_id: str) -> None:
        ...

    def forget_room(self, org_id: str, room_id: str) -> None:
        ...


@dataclass
class ReconcilerMetrics:
    terminal_rooms_with_resources: int = 0
    oldest_overdue_cleanup_seconds: float = 0.0
    cleanup_inflight: int = 0
    tick_totals: Dict[str, int] = field(default_factory=dict)
    actions_total: int = 0
    last_successful_reconciliation_at: Optional[str] = None

    def increment_tick(self, outcome: str) -> None:
        self.tick_totals[outcome] = self.tick_totals.get(outcome, 0) + 1


class RoomReconciler:
    """Reconcile Firestore terminal state with this process's owned rooms."""

    def __init__(
        self,
        *,
        manager: LocalRoomManager,
        store: RoomStateStore,
        cleanup_local_state: Callable[[str, str], None],
        interval_seconds: float = 30.0,
        instance_id: str = "unknown",
    ) -> None:
        self.manager = manager
        self.store = store
        self.cleanup_local_state = cleanup_local_state
        self.interval_seconds = max(0.05, float(interval_seconds))
        self.instance_id = instance_id or "unknown"
        self.metrics = ReconcilerMetrics()
        self._pass_lock = asyncio.Lock()

    def _emit(self, event: str, **fields: Any) -> None:
        payload = {
            "component": "room_reconciler",
            "event": event,
            "instanceId": self.instance_id,
            **fields,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))

    @staticmethod
    def _ended_age_seconds(state: Dict[str, Any]) -> float:
        ended_at = state.get("endedAt")
        if isinstance(ended_at, str):
            try:
                ended_at = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
            except ValueError:
                return 0.0
        if not isinstance(ended_at, datetime):
            return 0.0
        if ended_at.tzinfo is None:
            now = datetime.utcnow()
        else:
            now = datetime.now(tz=ended_at.tzinfo)
        return max(0.0, (now - ended_at).total_seconds())

    async def _read_states(
        self,
        keys: list[RoomKey],
    ) -> Dict[RoomKey, Optional[Dict[str, Any]]]:
        # Firestore's sync client must not block the event loop that owns the
        # websocket connections we may need to close.
        return await asyncio.to_thread(
            self.store.get_room_reconcile_states,
            keys,
        )

    async def _cleanup_ended_room(self, key: RoomKey, state: Dict[str, Any]) -> None:
        org_id, room_id = key
        reason = str(state.get("endReason") or "room_ended")
        # Local delivery is intentional.  Calling broadcast_room here would
        # make recovery depend on Redis and could fan a duplicate terminal
        # event back to healthy sibling instances.
        await self.manager._broadcast_local_room(
            org_id,
            room_id,
            {
                "type": "STATUS",
                "orgId": org_id,
                "roomId": room_id,
                "roomStatus": "ended",
                "viewerCount": 0,
                "reason": reason,
            },
        )
        # Websocket disconnects release their own individual refcounts, but a
        # previously failed release may have left an orphan desired key.  The
        # room is confirmed ended, so remove that reconnect source of truth
        # idempotently before Redis can recover.
        await self.manager.forget_room_subscription(org_id, room_id)
        self.manager.forget_room(org_id, room_id)
        self.cleanup_local_state(org_id, room_id)

    async def run_once(self) -> str:
        """Run one pass and return its outcome token.

        Missing, malformed, or unknown room documents fail open.  Only the
        explicit Firestore predicate ``status == "ended"`` authorizes local
        cleanup.
        """
        if self._pass_lock.locked():
            self.metrics.increment_tick("skipped_overlap")
            self._emit("tick", outcome="skipped_overlap")
            return "skipped_overlap"

        async with self._pass_lock:
            started = time.monotonic()
            owned = sorted(self.manager.locally_owned_room_keys())
            terminal: Dict[RoomKey, Dict[str, Any]] = {}

            try:
                states = await self._read_states(owned)
            except Exception as exc:
                # A failed batch is entirely unknown. Preserve the existing
                # overdue gauges, close nothing, and retry on the next tick.
                self.metrics.increment_tick("firestore_error")
                self._emit(
                    "tick",
                    outcome="firestore_error",
                    durationSeconds=round(time.monotonic() - started, 6),
                    ownedRooms=len(owned),
                    error=type(exc).__name__,
                )
                return "firestore_error"

            for key in owned:
                state = states.get(key)

                if state is None:
                    self._emit(
                        "room_read_skipped",
                        orgId=key[0],
                        roomId=key[1],
                        reason="missing",
                    )
                    continue
                raw_status = state.get("status")
                status = raw_status if isinstance(raw_status, str) else ""
                if status == "live":
                    continue
                if status != "ended":
                    self._emit(
                        "room_read_skipped",
                        orgId=key[0],
                        roomId=key[1],
                        reason="malformed_or_unknown_status",
                        status=raw_status,
                    )
                    continue
                terminal[key] = state

            cleanup_errors = 0
            for key, state in terminal.items():
                self.metrics.cleanup_inflight += 1
                self._emit(
                    "cleanup_state",
                    orgId=key[0],
                    roomId=key[1],
                    cleanupInflight=self.metrics.cleanup_inflight,
                )
                try:
                    await self._cleanup_ended_room(key, state)
                    self.metrics.actions_total += 1
                    self._emit(
                        "action",
                        reason="ended_room_local_cleanup",
                        orgId=key[0],
                        roomId=key[1],
                    )
                except Exception as exc:
                    cleanup_errors += 1
                    self._emit(
                        "cleanup_error",
                        orgId=key[0],
                        roomId=key[1],
                        error=type(exc).__name__,
                    )
                finally:
                    self.metrics.cleanup_inflight -= 1
                    self._emit(
                        "cleanup_state",
                        orgId=key[0],
                        roomId=key[1],
                        cleanupInflight=self.metrics.cleanup_inflight,
                    )

            remaining = {
                key: state
                for key, state in terminal.items()
                if self.manager.room_has_local_resources(key[0], key[1])
            }
            self.metrics.terminal_rooms_with_resources = len(remaining)
            self.metrics.oldest_overdue_cleanup_seconds = max(
                (self._ended_age_seconds(state) for state in remaining.values()),
                default=0.0,
            )

            if cleanup_errors or remaining:
                outcome = "cleanup_error"
            else:
                outcome = "ok"
                self.metrics.last_successful_reconciliation_at = datetime.now(
                    timezone.utc
                ).isoformat()
            self.metrics.increment_tick(outcome)
            self._emit(
                "tick",
                outcome=outcome,
                durationSeconds=round(time.monotonic() - started, 6),
                ownedRooms=len(owned),
                terminalRoomsWithResources=self.metrics.terminal_rooms_with_resources,
                oldestOverdueCleanupSeconds=round(
                    self.metrics.oldest_overdue_cleanup_seconds,
                    3,
                ),
                cleanupInflight=self.metrics.cleanup_inflight,
                reconcilerActionsTotal=self.metrics.actions_total,
                lastSuccessfulReconciliationAt=self.metrics.last_successful_reconciliation_at,
            )
            return outcome

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A programming error in one pass must be visible but must not
                # permanently disable future reconciliation attempts.
                self.metrics.increment_tick("loop_error")
                self._emit("tick", outcome="loop_error", error=type(exc).__name__)
                await asyncio.sleep(self.interval_seconds)
