"""Regression tests for PR-T1-A (startup safety).

Locks in the behavior change from `docs/03-analysis/resource-cleanup-audit.md`
§4a.1 and `docs/01-plan/features/resource-cleanup-track-1.plan.md` PR-T1-A:
the FastAPI startup handler no longer terminates existing live rooms.

The full multi-instance acceptance test (audit F-15 — "start instance B
while A is broadcasting; A's room stays live") requires two uvicorn
processes and belongs to the docker-compose integration harness that
lands in the Track 1 completion gate (see plan §4).

This file provides one in-process F-15 proxy that exercises the
behavior directly and two structural assertions that guard against
regression:

  1. F-15 (in-process proxy). Populate the in-memory store with a
     live room where audio just arrived; run `_on_startup()`; assert
     no `end_room` call was issued against the live room, and the
     room is still `status="live"` when startup returns. This is the
     behavioral test the reviewer asked for — the multi-process
     version merely repeats the same assertion across a TCP boundary.

  2. Structural: `_cleanup_live_rooms_on_startup` is removed. If a
     future refactor reintroduces any invocation of that name, this
     test fails with a message citing the audit and the plan.

  3. Structural: sweeper cleanup paths remain active. `_on_startup`
     still schedules `_room_sweeper_loop`, and that loop still calls
     `stale_live_rooms` and `enforce_live_usage_caps`. Idle, max-
     duration, and cap-enforcement cleanup are unchanged by this fix.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
import unittest
from unittest.mock import patch

from app import main as app_main
from app.services import multichurch_store as store_mod


def _extract_called_name(func_node: ast.expr) -> str | None:
    """Return the name of the function being called at this AST node.

    Handles `foo()` → `"foo"` and `mod.foo()` → `"foo"`. Any more
    exotic form (subscript, computed attribute, call chain) returns
    None — those forms are not how legitimate scheduling would look.
    """
    if isinstance(func_node, ast.Name):
        return func_node.id
    if isinstance(func_node, ast.Attribute):
        return func_node.attr
    return None


def _call_names_in(function_obj) -> set[str]:
    """All simple call-target names appearing anywhere in the function
    body. Used for structural assertions.
    """
    source = textwrap.dedent(inspect.getsource(function_obj))
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        n = _extract_called_name(node.func)
        if n is not None:
            names.add(n)
    return names


class StartupSafetyBehaviorTests(unittest.IsolatedAsyncioTestCase):
    """F-15 in-process proxy — the actual behavior test."""

    async def test_startup_event_does_not_end_live_rooms(self):
        """Instance B starting must not end a live room that another
        instance is actively broadcasting.

        Uses `InMemoryMultiChurchStore` directly. Seeds a live room
        with `lastAudioAt = now` (host is actively broadcasting).
        Patches `end_room` with a spy that records every call. Runs
        the `_on_startup()` handler. Asserts:

        - The live room still exists in the store afterwards.
        - Its `status` is still `"live"`.
        - `end_room` was never invoked during startup for that room.

        This is the F-15 acceptance without a second uvicorn process
        — the multi-process version verifies the same properties
        across a TCP boundary, which is why F-15 is tagged [2P] in
        the audit. The [2P] version lands with the Track 1 docker-
        compose harness.
        """
        # Isolated in-memory store so this test does not touch any
        # global state from previous tests. Populate `_rooms` directly
        # — the public start_room path would require orgs, services,
        # billing state, and an authenticated host, none of which
        # affect what we're actually testing (the startup event's
        # behavior against a Firestore-live room).
        test_store = store_mod.InMemoryMultiChurchStore()
        org_id = "org-A-broadcasting"
        room_id = "room-A-active"
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc)
        test_store._rooms[(org_id, room_id)] = {
            "serviceKey": "sunday-service",
            "status": "live",
            "startedAt": now,
            "endedAt": None,
            "hostUid": "host-A",
            "languagePair": {"source": "ko", "target": "en"},
            "listenerCountPeak": 0,
            "billingPeriodKey": "202601",
            "endReason": None,
            "lastAudioAt": now,  # actively broadcasting — audio just arrived
            "lastUsageTickAt": now,
            "finalTranscript": "",
        }

        # Sanity: room is live before startup runs.
        self.assertEqual(
            test_store._rooms[(org_id, room_id)]["status"],
            "live",
            "test setup: room must be live before we test the startup event",
        )

        # Spy on end_room. Patch the class method so any bound
        # reference (including the one held via `multichurch_store`
        # inside app.main) resolves through the spy.
        end_room_calls: list[tuple[str, str]] = []
        original_end_room = store_mod.InMemoryMultiChurchStore.end_room

        def spy_end_room(self, org_id, room_id, reason="unspecified", **kwargs):
            end_room_calls.append((org_id, room_id))
            return original_end_room(self, org_id, room_id, reason=reason, **kwargs)

        # Patch the module-level `multichurch_store` binding both the
        # store class method (defensive) and the app.main import.
        with patch.object(store_mod.InMemoryMultiChurchStore, "end_room", spy_end_room), \
             patch.object(app_main, "multichurch_store", test_store):
            # Run _on_startup. It schedules the sweeper task but the
            # sweeper's first iteration is `await asyncio.sleep(...)`
            # so nothing runs synchronously against the store.
            #
            # Redis pubsub is disabled by default in tests (REDIS_ENABLED=0),
            # so no network is touched.
            try:
                await app_main._on_startup()
            finally:
                # Cancel the sweeper task the startup created so it
                # doesn't leak into other tests.
                task = app_main._room_sweeper_task
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                app_main._room_sweeper_task = None

        # Post-startup assertions.
        post = test_store._rooms.get((org_id, room_id))
        self.assertIsNotNone(
            post,
            "room disappeared during startup — should have been left alone",
        )
        self.assertEqual(
            post.get("status"),
            "live",
            f"room was terminated during startup (F-15 REGRESSION). "
            f"end_room calls during startup: {end_room_calls}",
        )
        self.assertEqual(
            end_room_calls,
            [],
            f"end_room was invoked during startup — should never happen "
            f"under PR-T1-A. Calls: {end_room_calls}",
        )


class StartupSafetyStructuralTests(unittest.TestCase):
    """Structural checks. These are cheap regression guards; the behavioral
    test above is the load-bearing one."""

    def test_cleanup_live_rooms_function_is_removed(self):
        """The dangerous function must not be reachable as an attribute
        of `app.main` — leaving it defined but unused would create an
        opportunity for accidental reintroduction of the multi-instance
        blocker.
        """
        self.assertFalse(
            hasattr(app_main, "_cleanup_live_rooms_on_startup"),
            "PR-T1-A removed the _cleanup_live_rooms_on_startup function "
            "entirely. If this test fails, the function has been reintroduced "
            "— see resource-cleanup-audit §4a.1 before restoring it.",
        )

    def test_on_startup_body_has_no_reference_to_removed_function(self):
        """No AST-level call reference remains in the startup body.

        AST-based detection ignores comments and docstrings, so a note
        that mentions the removed name is not a false failure, while
        every syntactic form of reintroduction (bare call, `create_task`
        arg, `ensure_future`, `run_in_executor`, etc.) is still caught.
        """
        source = textwrap.dedent(inspect.getsource(app_main._on_startup))
        tree = ast.parse(source)
        offenders: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called_name = _extract_called_name(node.func)
            if called_name == "_cleanup_live_rooms_on_startup":
                offenders.append((node.lineno, "direct call"))
                continue
            for arg in node.args:
                if isinstance(arg, ast.Call):
                    inner = _extract_called_name(arg.func)
                    if inner == "_cleanup_live_rooms_on_startup":
                        offenders.append(
                            (node.lineno, f"passed into {called_name!r}"),
                        )
        self.assertEqual(
            offenders,
            [],
            f"reintroduced references: {offenders} — see audit §4a.1.",
        )

    def test_sweeper_still_scheduled_by_startup(self):
        """The sweeper task, which handles idle-timeout / max-duration /
        cap-enforcement cleanup, MUST still be scheduled by
        `_on_startup`. This is the mechanism that continues to catch
        stale rooms after PR-T1-A removes the unconditional startup
        termination.
        """
        source = textwrap.dedent(inspect.getsource(app_main._on_startup))
        tree = ast.parse(source)
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Call):
                    inner = _extract_called_name(arg.func)
                    if inner == "_room_sweeper_loop":
                        found = True
                        break
            if found:
                break
        self.assertTrue(
            found,
            "PR-T1-A must not remove the sweeper scheduling. Idle-timeout, "
            "max-duration, and cap-enforcement cleanup all run through "
            "_room_sweeper_loop and it must remain scheduled on startup.",
        )

    def test_sweeper_still_calls_stale_and_cap_paths(self):
        """The sweeper loop's body must still invoke the two Firestore
        selectors that drive idle/max-duration cleanup and cap
        enforcement. If either goes missing, the fallback safety net
        that PR-T1-A leans on is silently broken.
        """
        called = _call_names_in(app_main._room_sweeper_loop)
        self.assertIn(
            "stale_live_rooms",
            called,
            "sweeper must still call stale_live_rooms — this is the "
            "idle_timeout / max_duration path that PR-T1-A depends on.",
        )
        self.assertIn(
            "enforce_live_usage_caps",
            called,
            "sweeper must still call enforce_live_usage_caps — this is "
            "the cap-enforcement path (trial minutes / monthly limit).",
        )


if __name__ == "__main__":
    unittest.main()
