"""Redis-independent repair of local resources for Firestore-ended rooms.

Redis Pub/Sub remains the fast path for terminal delivery.  This module is a
periodic safety net for the case where a process misses that one message.  It
reads Firestore as the source of truth and performs only process-local cleanup;
it never writes Firestore and never publishes to Redis.

Event schema (single source of truth for the PR-T1-E monitoring stack):

  Every emission is a single-line JSON object with these required fields:
    - event: one of {"reconciler_tick", "reconciler_action",
                     "reconciler_diagnostic"}
    - schema_version: string, currently "1"
    - severity: one of {"DEBUG", "INFO", "NOTICE", "WARNING", "ERROR"}
    - message: short human-readable summary
    - component: always "room_reconciler"
    - instance_id: the process's INSTANCE_ID (logging context only —
                   NOT used as a metric label because that would be
                   high-cardinality per-instance)

  `reconciler_tick` — emitted exactly once per reconciler pass. Carries
  the tick snapshot (outcome, owned rooms count, aggregate counters).
  Includes an `overdue` boolean set true when
  `terminal_rooms_with_resources > 0`, which lets Cloud Logging count
  "stuck" ticks with a counter rather than depend on gauge semantics.
  Log-based metrics filter on this event by `outcome` label.

  `reconciler_action` — emitted per cleanup attempt on a terminal
  room. Reason is a bounded enum:
    - "ended_room_local_cleanup" (success)
    - "cleanup_error" (failure; error field carries type name)
  Metric label is `reason`. Room IDs and org IDs are included in the
  log body for debugging BUT MUST NOT be promoted to metric labels
  (cardinality).

  `reconciler_diagnostic` — everything else (per-room skips,
  cleanup_state transitions). severity=DEBUG. NOT driven off any
  log-based metric; visible in Cloud Logging for debugging only.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import time
from typing import Any, Callable, Dict, Optional, Protocol, Set, Tuple


RoomKey = Tuple[str, str]

_SCHEMA_VERSION = "1"

# Allowed values for the top-level `event` field. Anything not here is
# a bug.
_ALLOWED_EVENTS = frozenset({
    "reconciler_tick",
    "reconciler_action",
    "reconciler_diagnostic",
})

# Cloud Logging severity levels — a subset we actually use.
_ALLOWED_SEVERITIES = frozenset({
    "DEBUG",
    "INFO",
    "NOTICE",
    "WARNING",
    "ERROR",
})

# Bounded set of reason values for `reconciler_action`. Metric labels
# with unbounded values are rejected by Cloud Monitoring for
# cardinality; this enum keeps that invariant.
_ACTION_REASONS = frozenset({
    "ended_room_local_cleanup",
    "cleanup_error",
})

# The `outcome` field on `reconciler_tick`. Same cardinality concern.
_TICK_OUTCOMES = frozenset({
    "ok",
    "cleanup_error",
    "firestore_error",
    "loop_error",
    "skipped_overlap",
})


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

    def _emit(
        self,
        event: str,
        *,
        severity: str,
        message: str,
        **fields: Any,
    ) -> None:
        """Emit a single-line JSON log conforming to the schema at the
        top of this module. The Cloud Logging → log-based metric
        pipeline in `ops/monitoring/reconciler/` reads these events;
        breaking the schema breaks metrics without breaking tests
        unless the schema is asserted separately (see
        `backend/tests/test_room_reconciler.py::EventSchemaTests`).
        """
        assert event in _ALLOWED_EVENTS, f"unknown event {event!r}"
        assert severity in _ALLOWED_SEVERITIES, f"unknown severity {severity!r}"
        payload = {
            "event": event,
            "schema_version": _SCHEMA_VERSION,
            "severity": severity,
            "message": message,
            "component": "room_reconciler",
            "instance_id": self.instance_id,
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

    def _emit_tick(
        self,
        *,
        outcome: str,
        duration_seconds: float,
        owned_rooms: int,
        error: Optional[str] = None,
    ) -> None:
        assert outcome in _TICK_OUTCOMES, f"unknown tick outcome {outcome!r}"
        # `overdue` is a per-tick boolean derived from the aggregate snapshot.
        # Cloud Logging can count events matching it; that gives us an
        # "overdue-tick counter" without needing gauge semantics.
        overdue = self.metrics.terminal_rooms_with_resources > 0
        severity = "INFO"
        if outcome in ("firestore_error", "loop_error", "cleanup_error"):
            severity = "ERROR"
        elif outcome == "skipped_overlap":
            severity = "WARNING"
        # NOTE: cleanup_inflight is deliberately NOT included in tick
        # payload. The reconciler increments cleanup_inflight,
        # awaits cleanup, decrements it in finally, and only THEN
        # emits the tick — so cleanup_inflight is always 0 by the
        # time the tick is written. An in-progress cleanup that
        # never completes never produces a tick at all (A1 catches
        # that via freshness absence). To see per-cleanup state,
        # follow the `reconciler_diagnostic` events with kinds
        # `cleanup_started` / `cleanup_finished` — an unmatched
        # `cleanup_started` in Cloud Logging means the cleanup is
        # still in flight or has crashed inside the loop.
        fields: Dict[str, Any] = {
            "outcome": outcome,
            "duration_seconds": round(duration_seconds, 6),
            "owned_rooms": owned_rooms,
            "terminal_rooms_with_resources": self.metrics.terminal_rooms_with_resources,
            "oldest_overdue_cleanup_seconds": round(
                self.metrics.oldest_overdue_cleanup_seconds, 3,
            ),
            "actions_total": self.metrics.actions_total,
            "last_successful_reconciliation_at": self.metrics.last_successful_reconciliation_at,
            "overdue": overdue,
        }
        if error is not None:
            fields["error"] = error
        self._emit(
            "reconciler_tick",
            severity=severity,
            message=f"reconciler tick {outcome}",
            **fields,
        )

    def _emit_action(
        self,
        *,
        reason: str,
        org_id: str,
        room_id: str,
        error: Optional[str] = None,
    ) -> None:
        assert reason in _ACTION_REASONS, f"unknown action reason {reason!r}"
        severity = "NOTICE" if reason == "ended_room_local_cleanup" else "ERROR"
        # Room + org IDs live in the log body for debugging, NOT as
        # metric labels (Cloud Monitoring rejects high-cardinality
        # labels, and we would have unbounded values otherwise).
        fields: Dict[str, Any] = {
            "reason": reason,
            "org_id": org_id,
            "room_id": room_id,
        }
        if error is not None:
            fields["error"] = error
        self._emit(
            "reconciler_action",
            severity=severity,
            message=f"reconciler action {reason}",
            **fields,
        )

    def _emit_diagnostic(self, *, kind: str, **fields: Any) -> None:
        """Debug-severity chatter (per-room skips, cleanup transitions).
        NOT read by any log-based metric — Cloud Logging retention +
        the reviewer's dashboard link back to these for debugging."""
        self._emit(
            "reconciler_diagnostic",
            severity="DEBUG",
            message=f"reconciler diagnostic {kind}",
            kind=kind,
            **fields,
        )

    async def run_once(self) -> str:
        """Run one pass and return its outcome token.

        Missing, malformed, or unknown room documents fail open.  Only the
        explicit Firestore predicate ``status == "ended"`` authorizes local
        cleanup.
        """
        started = time.monotonic()
        if self._pass_lock.locked():
            self.metrics.increment_tick("skipped_overlap")
            self._emit_tick(
                outcome="skipped_overlap",
                duration_seconds=time.monotonic() - started,
                owned_rooms=0,
            )
            return "skipped_overlap"

        async with self._pass_lock:
            owned = sorted(self.manager.locally_owned_room_keys())
            terminal: Dict[RoomKey, Dict[str, Any]] = {}

            try:
                states = await self._read_states(owned)
            except Exception as exc:
                # A failed batch is entirely unknown. Preserve the existing
                # overdue gauges, close nothing, and retry on the next tick.
                self.metrics.increment_tick("firestore_error")
                self._emit_tick(
                    outcome="firestore_error",
                    duration_seconds=time.monotonic() - started,
                    owned_rooms=len(owned),
                    error=type(exc).__name__,
                )
                return "firestore_error"

            for key in owned:
                state = states.get(key)

                if state is None:
                    self._emit_diagnostic(
                        kind="room_read_skipped",
                        org_id=key[0],
                        room_id=key[1],
                        reason="missing",
                    )
                    continue
                raw_status = state.get("status")
                status = raw_status if isinstance(raw_status, str) else ""
                if status == "live":
                    continue
                if status != "ended":
                    self._emit_diagnostic(
                        kind="room_read_skipped",
                        org_id=key[0],
                        room_id=key[1],
                        reason="malformed_or_unknown_status",
                        status=raw_status,
                    )
                    continue
                terminal[key] = state

            cleanup_errors = 0
            for key, state in terminal.items():
                # cleanup_started emitted BEFORE the await so Cloud
                # Logging captures an in-flight cleanup that never
                # returns (no matching cleanup_finished follows).
                self._emit_diagnostic(
                    kind="cleanup_started",
                    org_id=key[0],
                    room_id=key[1],
                )
                self.metrics.cleanup_inflight += 1
                # Default to "cancelled" — asyncio.CancelledError
                # inherits from BaseException (not Exception) and
                # would slip past `except Exception` while still
                # running `finally`. Without this default the
                # diagnostic would falsely record outcome=ok on a
                # mid-flight task cancellation (Cloud Run SIGTERM,
                # test cancellation, etc.). We flip to "ok" only
                # AFTER the await successfully returns.
                outcome_for_finished = "cancelled"
                try:
                    await self._cleanup_ended_room(key, state)
                    outcome_for_finished = "ok"
                    self.metrics.actions_total += 1
                    self._emit_action(
                        reason="ended_room_local_cleanup",
                        org_id=key[0],
                        room_id=key[1],
                    )
                except asyncio.CancelledError:
                    # Preserve the default outcome=cancelled and
                    # let the cancellation propagate — the loop
                    # must not silently absorb it. `finally` will
                    # still emit the paired cleanup_finished with
                    # outcome=cancelled before the CancelledError
                    # unwinds.
                    raise
                except Exception as exc:
                    cleanup_errors += 1
                    outcome_for_finished = "error"
                    self._emit_action(
                        reason="cleanup_error",
                        org_id=key[0],
                        room_id=key[1],
                        error=type(exc).__name__,
                    )
                finally:
                    self.metrics.cleanup_inflight -= 1
                    # cleanup_finished paired with the cleanup_started
                    # above. Absence of this event in Cloud Logging
                    # for a given (org_id, room_id) is the signal
                    # that the cleanup is hung mid-flight. Outcome
                    # values: ok, error, cancelled.
                    self._emit_diagnostic(
                        kind="cleanup_finished",
                        org_id=key[0],
                        room_id=key[1],
                        outcome=outcome_for_finished,
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
            self._emit_tick(
                outcome=outcome,
                duration_seconds=time.monotonic() - started,
                owned_rooms=len(owned),
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
                self._emit_tick(
                    outcome="loop_error",
                    duration_seconds=0.0,
                    owned_rooms=0,
                    error=type(exc).__name__,
                )
                await asyncio.sleep(self.interval_seconds)
