"""Failing-before / passing-after tests for the sweeper non-blocking fix.

Scope
=====

Prove that the two synchronous Firestore store calls currently invoked
directly inside the asyncio coroutine ``_room_sweeper_loop`` —
``multichurch_store.stale_live_rooms`` and
``multichurch_store.enforce_live_usage_caps`` — must execute off the
event-loop thread, so the loop remains responsive to the observability
heartbeat (and any other coroutine) during the Firestore stream page
fetch that each call performs.

Diagnostic evidence motivating these tests
------------------------------------------

Scope 1 observability on production revision
``worshiptranslate-backend-00170-9jr`` (Gate B deploy, 2026-10-06)
captured three stack_capture events at the sweeper's cadence (~70 s).
MainThread frames in all three captures descend from
``_room_sweeper_loop`` → ``multichurch_store.stale_live_rooms`` or
``multichurch_store.enforce_live_usage_caps`` →
``firestore_v1/stream_generator.__next__`` → ``grpc._channel._next`` →
``threading.wait``. Each stall was 5.5–5.8 s. See
``~/track1-redis-rollout-2026-10-06/GATE-B-DIAGNOSTIC-FOLLOWUP.md``.

Pattern already present
-----------------------

The same loop already wraps the third store call ``live_rooms`` in
``asyncio.get_running_loop().run_in_executor(None, …)`` (``main.py``
line 1145 at the Gate B merge SHA ``e2bc57d9``). This fix extends the
same pattern to the first two calls.

Invariants proved by this file
------------------------------

Before fix
~~~~~~~~~~
- ``stale_live_rooms`` call is lexically a direct expression inside the
  async coroutine (no ``run_in_executor`` ancestor) → structural test
  fails.
- ``enforce_live_usage_caps`` same → structural test fails.
- A blocking stub for either function stops an independent async
  heartbeat ticker from advancing during the call → behavioural test
  fails.

After fix
~~~~~~~~~
- Both calls are wrapped in an executor-dispatched callable → structural
  tests pass.
- The heartbeat ticker continues to advance while the stub blocks on a
  background executor thread → behavioural tests pass.

Preserved either way (regression guards)
----------------------------------------
- Call order: ``stale_live_rooms`` runs before
  ``enforce_live_usage_caps``.
- Exception containment: an exception from either store call is caught
  by the sweeper's outer ``try/except`` and the loop remains alive.
- Cancellation: cancelling the sweeper task cleans up any tracked
  asyncio tasks.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
import threading
import time
import unittest
from typing import List
from unittest.mock import AsyncMock, patch

from app import main as app_main


# ---------------------------------------------------------------------------
# AST helpers (lifted from test_sweeper_atomic.py's _extract_called_name
# pattern so this file does not import from a sibling test module).
# ---------------------------------------------------------------------------

def _extract_called_name(func_node) -> str | None:
    """Return the callable's bare name at an ``ast.Call`` node, or None."""
    if isinstance(func_node, ast.Name):
        return func_node.id
    if isinstance(func_node, ast.Attribute):
        return func_node.attr
    return None


def _build_parent_map(tree: ast.AST) -> dict:
    """Return ``{id(child): parent}`` for every node in ``tree``."""
    parent_of = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_of[id(child)] = parent
    return parent_of


def _is_inside_run_in_executor(call_node: ast.Call, parent_of: dict) -> bool:
    """True iff ``call_node`` has an ancestor that is an
    ``ast.Call`` whose func is named ``run_in_executor``.

    Covers both direct passing (``run_in_executor(None, store.method)``)
    and partial/lambda wrappers (``run_in_executor(None,
    functools.partial(store.method, …))`` or ``…lambda: store.method(…)``).
    """
    parent = parent_of.get(id(call_node))
    while parent is not None:
        if isinstance(parent, ast.Call):
            if _extract_called_name(parent.func) == "run_in_executor":
                return True
        parent = parent_of.get(id(parent))
    return False


def _sweeper_ast() -> ast.AST:
    source = textwrap.dedent(inspect.getsource(app_main._room_sweeper_loop))
    return ast.parse(source)


# ---------------------------------------------------------------------------
# Structural tests (AST) — fast, deterministic, no async harness required.
# ---------------------------------------------------------------------------

class SweeperStoreCallsNotOnEventLoopStructural(unittest.TestCase):
    """Prove that the two synchronous store calls are not lexically
    invoked as plain expressions inside ``_room_sweeper_loop``."""

    def test_stale_live_rooms_wrapped_in_executor(self):
        tree = _sweeper_ast()
        parent_of = _build_parent_map(tree)

        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _extract_called_name(node.func) == "stale_live_rooms"
        ]
        self.assertGreaterEqual(
            len(calls), 1,
            "sweeper must invoke multichurch_store.stale_live_rooms; "
            "if the call site was removed, this test needs updating.",
        )
        for node in calls:
            self.assertTrue(
                _is_inside_run_in_executor(node, parent_of),
                "multichurch_store.stale_live_rooms is called directly "
                "inside _room_sweeper_loop (an async coroutine) — this "
                "blocks the asyncio event-loop thread for the duration "
                "of the Firestore stream iteration. The fix is to wrap "
                "the call in `asyncio.get_running_loop().run_in_executor"
                "(None, …)` the same way `live_rooms` is wrapped a few "
                "lines later. See GATE-B-DIAGNOSTIC-FOLLOWUP.md.",
            )

    def test_enforce_live_usage_caps_wrapped_in_executor(self):
        tree = _sweeper_ast()
        parent_of = _build_parent_map(tree)

        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _extract_called_name(node.func) == "enforce_live_usage_caps"
        ]
        self.assertGreaterEqual(
            len(calls), 1,
            "sweeper must invoke multichurch_store.enforce_live_usage_caps; "
            "if the call site was removed, this test needs updating.",
        )
        for node in calls:
            self.assertTrue(
                _is_inside_run_in_executor(node, parent_of),
                "multichurch_store.enforce_live_usage_caps is called "
                "directly inside _room_sweeper_loop (an async coroutine) — "
                "this blocks the asyncio event-loop thread. Wrap it in "
                "`run_in_executor(None, …)` matching `live_rooms`.",
            )


# ---------------------------------------------------------------------------
# Behavioural helpers.
# ---------------------------------------------------------------------------

def _run_sweeper_one_iteration_sync_harness(
    *,
    stale_stub,
    enforce_stub,
    live_stub=None,
    heartbeat_sampler=None,
):
    """Run ``_room_sweeper_loop`` for one iteration under patched stubs,
    collecting any heartbeat samples the test wants.

    The sweeper's ``while True`` first-line ``await asyncio.sleep(max(15,
    ROOM_SWEEPER_INTERVAL_SEC))`` is patched to return immediately, then
    on the second call to raise ``asyncio.CancelledError`` so the loop
    exits cleanly after exactly one body execution.

    ``heartbeat_sampler`` is an optional async callable
    ``async def sampler() -> None`` that will be scheduled as a
    concurrent task; when the sweeper finishes it is cancelled.
    """
    captured_sleep_calls: List[float] = []

    async def fake_sleep(delay):
        captured_sleep_calls.append(delay)
        if len(captured_sleep_calls) == 1:
            # First sleep: proceed immediately into the body.
            return None
        # Second sleep: break the loop.
        raise asyncio.CancelledError()

    patches = []
    patches.append(patch.object(app_main, "asyncio", wraps=asyncio))
    # Simpler: patch asyncio.sleep as seen from main.py's scope by
    # patching the attribute on the already-imported asyncio module.
    # We cannot easily patch "app.main.asyncio.sleep" without side
    # effects on other coroutines; instead, we replace the specific
    # asyncio.sleep function on the asyncio module for the duration of
    # the test. All other coroutines in the harness will see the fake
    # too — this is acceptable because the harness runs only the
    # sweeper + optional heartbeat sampler, both of which are
    # heartbeat-sampler aware.
    #
    # Keep the real asyncio.sleep accessible for the heartbeat sampler.

    real_sleep = asyncio.sleep

    async def runner():
        heartbeat_task = None

        async def heartbeat_wrapper():
            # Use the real sleep (keep a direct reference pre-patch).
            while True:
                await real_sleep(0.05)
                if heartbeat_sampler is not None:
                    heartbeat_sampler()

        if heartbeat_sampler is not None:
            heartbeat_task = asyncio.create_task(heartbeat_wrapper())

        with patch.object(asyncio, "sleep", side_effect=fake_sleep):
            with patch.object(
                app_main.multichurch_store, "stale_live_rooms",
                side_effect=stale_stub,
            ), patch.object(
                app_main.multichurch_store, "enforce_live_usage_caps",
                side_effect=enforce_stub,
            ), patch.object(
                app_main.multichurch_store, "live_rooms",
                side_effect=(live_stub or (lambda: [])),
            ), patch.object(
                app_main, "_check_spend_alerts", side_effect=lambda: None,
            ):
                sweeper_task = asyncio.create_task(app_main._room_sweeper_loop())
                try:
                    await asyncio.wait_for(sweeper_task, timeout=10.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                except Exception:
                    # The sweeper's own outer `except Exception` should
                    # catch and log — if an exception escapes to here,
                    # that's a defect in the containment semantics.
                    raise
                finally:
                    if heartbeat_task is not None:
                        heartbeat_task.cancel()
                        try:
                            await heartbeat_task
                        except (asyncio.CancelledError, Exception):
                            pass
                    if not sweeper_task.done():
                        sweeper_task.cancel()
                        try:
                            await sweeper_task
                        except (asyncio.CancelledError, Exception):
                            pass

    asyncio.run(runner())


class SweeperExecutesStoreCallsOffEventLoop(unittest.TestCase):
    """Behavioural proof that each store call runs off the event-loop
    thread. Each stub records ``threading.get_ident()`` at the moment
    it is invoked; after one sweep, the recorded id must differ from
    the event-loop thread's id."""

    def test_stale_live_rooms_executes_off_event_loop_thread(self):
        stale_recorded: List[int] = []

        def stale_stub(**_kw):
            stale_recorded.append(threading.get_ident())
            return []

        def enforce_stub(**_kw):
            return []

        _run_sweeper_one_iteration_sync_harness(
            stale_stub=stale_stub, enforce_stub=enforce_stub,
        )

        self.assertEqual(
            len(stale_recorded), 1,
            f"expected stale_live_rooms to be called exactly once; got "
            f"{len(stale_recorded)}",
        )
        # The main thread's identity (the event-loop thread for
        # asyncio.run()) must NOT be the thread the stub ran on.
        main_ident = threading.main_thread().ident
        self.assertNotEqual(
            stale_recorded[0], main_ident,
            "multichurch_store.stale_live_rooms executed on the main "
            "(event-loop) thread; it must run on a background executor "
            "thread so the loop stays responsive during the Firestore "
            "stream iteration.",
        )

    def test_enforce_live_usage_caps_executes_off_event_loop_thread(self):
        enforce_recorded: List[int] = []

        def stale_stub(**_kw):
            return []

        def enforce_stub(**_kw):
            enforce_recorded.append(threading.get_ident())
            return []

        _run_sweeper_one_iteration_sync_harness(
            stale_stub=stale_stub, enforce_stub=enforce_stub,
        )

        self.assertEqual(
            len(enforce_recorded), 1,
            f"expected enforce_live_usage_caps to be called exactly once; "
            f"got {len(enforce_recorded)}",
        )
        main_ident = threading.main_thread().ident
        self.assertNotEqual(
            enforce_recorded[0], main_ident,
            "multichurch_store.enforce_live_usage_caps executed on the "
            "main (event-loop) thread; it must run on a background "
            "executor thread.",
        )


class SweeperHeartbeatContinuesDuringBlockingStoreCalls(unittest.TestCase):
    """Behavioural proof that an independent async heartbeat continues
    to advance during a blocking store call. If the call runs on the
    event-loop thread, the heartbeat cannot schedule and the counter
    stays near zero. If the call runs on an executor thread, the
    heartbeat advances at its natural ~10 Hz cadence."""

    BLOCK_DURATION_S = 0.6  # long enough for ~5+ heartbeat samples at 0.05s each

    def _run(self, *, which_blocks: str):
        samples = {"count": 0}

        def heartbeat_sampler():
            samples["count"] += 1

        def stale_stub(**_kw):
            if which_blocks == "stale":
                time.sleep(self.BLOCK_DURATION_S)
            return []

        def enforce_stub(**_kw):
            if which_blocks == "enforce":
                time.sleep(self.BLOCK_DURATION_S)
            return []

        _run_sweeper_one_iteration_sync_harness(
            stale_stub=stale_stub,
            enforce_stub=enforce_stub,
            heartbeat_sampler=heartbeat_sampler,
        )
        return samples["count"]

    def test_heartbeat_ticks_while_stale_live_rooms_blocks(self):
        tick_count = self._run(which_blocks="stale")
        # With a 0.6s block and 0.05s heartbeat cadence, we expect
        # ~5-11 ticks on an executor. With the loop blocked, we would
        # see at most ~1 (the one that fit between harness startup and
        # the block). Use a conservative lower bound.
        self.assertGreaterEqual(
            tick_count, 3,
            f"heartbeat recorded only {tick_count} ticks during the "
            f"{self.BLOCK_DURATION_S}s stale_live_rooms stub — the "
            f"event loop was blocked. Expected ≥3 ticks when the stub "
            f"runs on an executor thread.",
        )

    def test_heartbeat_ticks_while_enforce_live_usage_caps_blocks(self):
        tick_count = self._run(which_blocks="enforce")
        self.assertGreaterEqual(
            tick_count, 3,
            f"heartbeat recorded only {tick_count} ticks during the "
            f"{self.BLOCK_DURATION_S}s enforce_live_usage_caps stub — "
            f"event loop was blocked. Expected ≥3 ticks when the stub "
            f"runs on an executor thread.",
        )


class SweeperPreservesCallOrderAndContract(unittest.TestCase):
    """Regression guards that must pass before AND after the fix:
    - Order stale → enforce preserved.
    - Exceptions in either store call do not escape the sweeper.
    """

    def test_sweeper_calls_stale_before_enforce(self):
        order: List[str] = []

        def stale_stub(**_kw):
            order.append("stale")
            return []

        def enforce_stub(**_kw):
            order.append("enforce")
            return []

        _run_sweeper_one_iteration_sync_harness(
            stale_stub=stale_stub, enforce_stub=enforce_stub,
        )
        self.assertEqual(
            order, ["stale", "enforce"],
            f"sweeper changed the store-call order; got {order!r}, "
            f"expected ['stale', 'enforce']. The fix must preserve "
            f"sequential ordering — do not parallelize.",
        )

    def test_exception_in_stale_live_rooms_contained(self):
        enforce_called: List[bool] = []

        def stale_stub(**_kw):
            raise RuntimeError("simulated Firestore outage in stale_live_rooms")

        def enforce_stub(**_kw):
            # If this runs, the sweeper did NOT contain the exception
            # correctly — a raised exception from stale_live_rooms
            # should break OUT of the current iteration, not continue
            # to enforce_live_usage_caps in the same try block.
            enforce_called.append(True)
            return []

        # The harness `wait_for` wraps the sweeper and will catch any
        # escape of the exception. If this raises, the sweeper's outer
        # try/except is broken.
        _run_sweeper_one_iteration_sync_harness(
            stale_stub=stale_stub, enforce_stub=enforce_stub,
        )
        # Note: in the CURRENT sweeper layout the two sync calls share
        # the SAME outer try/except; an exception from stale aborts
        # the iteration before enforce runs. The fix must preserve
        # that behaviour.
        self.assertEqual(
            enforce_called, [],
            "exception from stale_live_rooms did NOT abort the current "
            "iteration — enforce_live_usage_caps was called after "
            "stale raised. The sweeper's try/except boundary moved.",
        )

    def test_exception_in_enforce_live_usage_caps_contained(self):
        """A raised exception from enforce must not escape the sweeper
        loop."""

        def stale_stub(**_kw):
            return []

        def enforce_stub(**_kw):
            raise RuntimeError("simulated Firestore outage in enforce")

        # If the sweeper's outer try/except does not catch, the harness
        # will re-raise and this test will fail with the RuntimeError.
        _run_sweeper_one_iteration_sync_harness(
            stale_stub=stale_stub, enforce_stub=enforce_stub,
        )
        # If we got here, exception was contained. Nothing else to assert.


class SweeperCancellationLeavesNoLeftoverTasks(unittest.TestCase):
    """Behavioural guarantee: cancelling the sweeper task leaves no
    extra tracked asyncio tasks on the event loop (specifically no
    orphaned helper from the executor dispatch pattern)."""

    def test_cancellation_does_not_leak_tasks(self):
        def stale_stub(**_kw):
            return []

        def enforce_stub(**_kw):
            return []

        baseline_tasks = {"count": None}
        after_tasks = {"count": None}

        async def runner():
            # Measure task count before and after a full sweep + cancel.
            baseline_tasks["count"] = len(asyncio.all_tasks())

            captured_sleep_calls: List[float] = []

            async def fake_sleep(delay):
                captured_sleep_calls.append(delay)
                if len(captured_sleep_calls) == 1:
                    return None
                raise asyncio.CancelledError()

            with patch.object(asyncio, "sleep", side_effect=fake_sleep):
                with patch.object(
                    app_main.multichurch_store, "stale_live_rooms",
                    side_effect=stale_stub,
                ), patch.object(
                    app_main.multichurch_store, "enforce_live_usage_caps",
                    side_effect=enforce_stub,
                ), patch.object(
                    app_main.multichurch_store, "live_rooms",
                    side_effect=lambda: [],
                ), patch.object(
                    app_main, "_check_spend_alerts", side_effect=lambda: None,
                ):
                    task = asyncio.create_task(app_main._room_sweeper_loop())
                    try:
                        await asyncio.wait_for(task, timeout=5.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass

            # Give the loop one cycle to clean up any executor-dispatch
            # bookkeeping task.
            await asyncio.sleep(0)
            after_tasks["count"] = len(
                [t for t in asyncio.all_tasks() if not t.done()]
            )

        asyncio.run(runner())
        # Current-task (runner itself) counts in both measurements, so
        # we expect `after` ≤ `baseline`. The sweeper task is gone.
        self.assertLessEqual(
            after_tasks["count"], baseline_tasks["count"],
            f"cancellation leaked tasks: baseline={baseline_tasks['count']} "
            f"after={after_tasks['count']} — a tracked asyncio.Task "
            f"outlived the sweeper's cancellation. If the fix uses "
            f"an auxiliary helper task, cancellation must await it.",
        )


if __name__ == "__main__":
    unittest.main()
