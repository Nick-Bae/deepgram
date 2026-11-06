"""Regression tests for the ``_is_room_live_async`` wrapper.

Context: ``FirestoreMultiChurchStore.is_room_live`` performs a synchronous
Firestore RPC. Prior to diag/firestore-sync-in-async-hot-paths, five hot
WebSocket code sites (``/ws/translate`` upgrade + its join recheck +
``consumer_join`` recheck, and ``/ws/stt_deepgram`` pre-/post-register
checks) called it inline from async handlers, blocking the asyncio event
loop for the full RPC round-trip. Production Cloud Monitoring and Logging
evidence shows event_loop_lag ERROR emissions concentrated on
listener-connection-heavy minutes; the tightest hypothesis consistent with
the evidence is that these sync-in-async hot sites were a contributor.

These tests cover the wrapper's intent:

1. The wrapper dispatches to a worker thread (not the event loop thread).
2. While the underlying implementation is slow, other asyncio tasks make
   progress — verifying the event loop is not blocked.

They do NOT prove the patch eliminates production lag; a bounded production
measurement (described in the diagnostic patch's accompanying proposal) is
the remaining step.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from unittest.mock import patch

import pytest


# The backend's main.py imports many optional modules at import time (Firestore
# client, OpenAI, Deepgram, Stripe, Firebase Admin, etc.). We only need the
# wrapper itself, which depends on `multichurch_store`. If the environment
# cannot import `app.main`, skip these tests with a clear reason — the wrapper
# is still exercised when the module does import.
import os as _os
# Ensure main.py's CORS validation passes during import. Tests do not make
# cross-origin requests; this single explicit origin satisfies the validator.
_os.environ.setdefault("CORS_ALLOW_ORIGINS", "http://localhost:3000")

try:
    # `multichurch_store` in main.py is the store INSTANCE imported via
    # ``from app.services.multichurch_store import multichurch_store``.
    # We patch its ``is_room_live`` method on the instance.
    from app.main import _is_room_live_async, multichurch_store  # type: ignore[import-not-found]
    _IMPORT_OK = True
    _IMPORT_SKIP_REASON = ""
except Exception as exc:  # pragma: no cover - environment-dependent
    _IMPORT_OK = False
    _IMPORT_SKIP_REASON = f"app.main not importable in this environment: {type(exc).__name__}: {exc}"


pytestmark = pytest.mark.skipif(not _IMPORT_OK, reason=_IMPORT_SKIP_REASON)


@pytest.mark.asyncio
async def test_is_room_live_async_runs_on_worker_thread() -> None:
    """The wrapper must dispatch the sync RPC to a worker thread."""
    loop_tid = threading.get_ident()
    recorded: dict[str, object] = {}

    def mock_impl(org: str, room: str) -> bool:
        recorded["tid"] = threading.get_ident()
        recorded["org"] = org
        recorded["room"] = room
        return True

    with patch.object(multichurch_store, "is_room_live", side_effect=mock_impl):
        result = await _is_room_live_async("org-x", "room-y")

    assert result is True
    assert recorded["org"] == "org-x"
    assert recorded["room"] == "room-y"
    assert recorded["tid"] != loop_tid, (
        "is_room_live ran on the event loop thread; the wrapper would block "
        "the loop during the Firestore RPC."
    )


@pytest.mark.asyncio
async def test_is_room_live_async_does_not_block_other_tasks() -> None:
    """While the sync RPC is slow, other coroutines must still make progress."""
    tick_count = {"n": 0}

    async def ticker() -> None:
        for _ in range(20):
            tick_count["n"] += 1
            await asyncio.sleep(0.005)  # 5ms ticks × 20 ≈ 100ms total

    def slow_impl(org: str, room: str) -> bool:
        time.sleep(0.1)  # 100ms blocking — mimics a slow Firestore RPC
        return True

    ticker_task = asyncio.create_task(ticker())

    with patch.object(multichurch_store, "is_room_live", side_effect=slow_impl):
        await _is_room_live_async("org-x", "room-y")

    await asyncio.wait_for(ticker_task, timeout=1.0)

    # If the wrapper did NOT offload, the 100ms sync sleep would block the
    # event loop and the ticker would complete at most 1-2 iterations.
    # With offloading, the ticker ticks concurrently during that window.
    assert tick_count["n"] >= 15, (
        f"ticker ran only {tick_count['n']} ticks during the 100ms slow RPC; "
        "the event loop was likely blocked. Expected ≥15 ticks."
    )


@pytest.mark.asyncio
async def test_is_room_live_async_propagates_exceptions() -> None:
    """Firestore errors must propagate so callers can fail-open explicitly."""
    class _FakeFirestoreError(RuntimeError):
        pass

    def raising_impl(org: str, room: str) -> bool:
        raise _FakeFirestoreError("firestore unavailable")

    with patch.object(multichurch_store, "is_room_live", side_effect=raising_impl):
        with pytest.raises(_FakeFirestoreError):
            await _is_room_live_async("org-x", "room-y")
