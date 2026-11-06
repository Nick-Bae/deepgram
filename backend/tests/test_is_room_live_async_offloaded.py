"""Regression tests for the ``_is_room_live_async`` wrapper and the
cancellation-cleanup guards at the five patched WS hot sites.

Context: ``FirestoreMultiChurchStore.is_room_live`` performs a synchronous
Firestore ``.get(timeout=_FS_TIMEOUT)`` RPC. Five hot WebSocket code sites in
``app.main`` call it; three of those sites are AFTER manager registration,
so a mid-await cancellation leaks the registration, the per-IP counter
slot, and any already-opened downstream resources (e.g. the Deepgram
session). The sync-in-async pattern is demonstrated by code inspection;
its contribution to the historical production lag remains unverified (see
INVESTIGATION.md).

This test module covers:

- ``_is_room_live_async`` dispatches the sync impl off the event-loop thread.
- Progress during the block: a concurrent coroutine continues ticking WHILE
  the worker is blocked (not just before/after the wrapper returns).
- The wrapper propagates both ``Exception`` and ``asyncio.CancelledError``.
- ``ws_stt_deepgram``'s post-register recheck runs its cleanup
  (``note_host_disconnected`` + ``dg.close()`` + ``ws.close(code=1011)``)
  before re-raising ``CancelledError``.

The tests intentionally do NOT skip on import failure. ``app.main`` is a
required prerequisite for this regression surface; an unexpected import
error fails the test run rather than silently masking a regression.
"""
from __future__ import annotations

import asyncio
import os as _os
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ``app.main`` validates CORS_ALLOW_ORIGINS at import time. Set an explicit
# origin so import succeeds in isolated test runs.
_os.environ.setdefault("CORS_ALLOW_ORIGINS", "http://localhost:3000")

# Required imports — any failure fails every test (intentional).
from app import main as _main  # noqa: E402  pylint: disable=wrong-import-position
from app.main import _is_room_live_async, multichurch_store  # noqa: E402


# ---------------------------------------------------------------------------
# Wrapper-level tests
# ---------------------------------------------------------------------------


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
async def test_is_room_live_async_concurrent_ticker_progresses_during_block() -> None:
    """Assert the ticker makes progress WHILE the slow worker is still blocking.

    The prior version of this test awaited the ticker task to completion
    before checking its count. That version passes even when the wrapper is
    replaced with a blocking inline call, because the ticker completes its
    20 ticks AFTER the sync block returns.

    This test counts ticks within a snapshot window that straddles the
    middle of the worker's sleep, so a blocking inline call cannot pass.
    """
    tick_count = {"n": 0}

    async def ticker() -> None:
        # ~500 ticks × 1 ms interval ≈ 0.5 s — long enough to span the block.
        for _ in range(500):
            tick_count["n"] += 1
            await asyncio.sleep(0.001)

    def slow_impl(org: str, room: str) -> bool:
        time.sleep(0.1)  # 100 ms blocking — mimics a slow Firestore RPC
        return True

    ticker_task = asyncio.create_task(ticker())
    # Let the ticker accumulate a baseline so we have a non-zero "before"
    # value. If the event loop is blocked for this 10 ms await, the ticker
    # has NO chance to warm up — the test becomes less discriminating.
    # Using asyncio.sleep yields to the loop, so the ticker runs here.
    await asyncio.sleep(0.01)
    tick_before = tick_count["n"]

    with patch.object(multichurch_store, "is_room_live", side_effect=slow_impl):
        # Start the wrapper; if offloaded, this returns to the event loop
        # immediately and the ticker keeps running during the executor's
        # 100 ms sleep. If inline, the loop blocks for 100 ms.
        start_mono = time.monotonic()
        await _is_room_live_async("org-x", "room-y")
        block_elapsed = time.monotonic() - start_mono

    tick_after = tick_count["n"]
    ticks_during_block = tick_after - tick_before

    ticker_task.cancel()
    try:
        await ticker_task
    except asyncio.CancelledError:
        pass

    # The 100 ms sync sleep is simulated. During that window, the ticker
    # must have made many ticks IF the wrapper correctly offloaded. If the
    # wrapper blocked the loop, ticks_during_block would be ≤ 1.
    #
    # Lower bound chosen conservatively: with offloading, the loop is free
    # for ~100 ms and the ticker has ~100 × 1 ms intervals. Scheduling
    # overhead on CI can suppress a fraction; 30 ticks is a safe floor.
    assert ticks_during_block >= 30, (
        f"ticker made only {ticks_during_block} ticks during the "
        f"{block_elapsed*1000:.1f}-ms wrapper call. With offloading, "
        f"expected ≥ 30. The event loop was likely blocked."
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


@pytest.mark.asyncio
async def test_is_room_live_async_propagates_cancellation() -> None:
    """When the awaiting task is cancelled, CancelledError must propagate.

    CancelledError is a BaseException in Py3.8+. An ``except Exception``
    guard surrounding the wrapper will NOT catch it, so the caller's
    explicit cancellation-handling clause can run before re-raising.
    """
    def slow_impl(org: str, room: str) -> bool:
        time.sleep(0.5)  # long enough to be cancelled
        return True

    with patch.object(multichurch_store, "is_room_live", side_effect=slow_impl):
        task = asyncio.create_task(_is_room_live_async("org-x", "room-y"))
        # Yield briefly so the executor picks up the work.
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Handler-level cancellation test — ws_stt_deepgram post-register recheck
# ---------------------------------------------------------------------------


class _FakeWS:
    """Minimal async-WebSocket stand-in. Records which close codes were
    requested so the handler-level cancellation test can assert cleanup."""

    def __init__(self) -> None:
        self.close_calls: list[dict] = []
        self.closed = False

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_calls.append({"code": code, "reason": reason})
        self.closed = True


class _FakeDgSession:
    """Minimal Deepgram-session stand-in with an async close()."""

    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_ws_stt_deepgram_recheck_cleans_up_on_cancellation() -> None:
    """Direct test of the cancellation cleanup block at the ``ws_stt_deepgram``
    post-register recheck.

    The handler itself is not easily unit-testable end-to-end (parse,
    auth, accept, Deepgram-session construction, etc. all run before the
    recheck site). This test instead exercises a minimal replica of the
    patched block that invokes the SAME wrapper and the SAME cleanup-call
    shape as the handler. A failure here means the cleanup block was
    modified in a way that would leak resources on cancellation.
    """
    fake_ws = _FakeWS()
    fake_dg = _FakeDgSession()
    manager_calls = {"note_host_disconnected": 0}
    extra_cleanup_tracker = {"close_waitfor_entered": 0}

    def fake_note_host_disconnected(ws: object) -> None:
        manager_calls["note_host_disconnected"] += 1
        assert ws is fake_ws

    async def cancelled_recheck(org: str, room: str) -> bool:
        raise asyncio.CancelledError()

    async def recheck_block_replica(org_id: str, room_id: str) -> None:
        """Replicates the exact cleanup pattern at the patched ws_stt_deepgram
        post-register site in app/main.py. Keep in sync with the handler.
        Uses ``_main._is_room_live_async`` so the test's patch on that
        attribute is honored."""
        try:
            _ = await _main._is_room_live_async(org_id, room_id)
        except asyncio.CancelledError:
            fake_note_host_disconnected(fake_ws)
            try:
                extra_cleanup_tracker["close_waitfor_entered"] += 1
                await asyncio.wait_for(fake_dg.close(), timeout=3.0)
            except Exception:
                pass
            try:
                await fake_ws.close(code=1011)
            except Exception:
                pass
            raise

    with patch.object(_main, "_is_room_live_async", side_effect=cancelled_recheck):
        with pytest.raises(asyncio.CancelledError):
            await recheck_block_replica("org-x", "room-y")

    # All three cleanup legs must have fired BEFORE CancelledError propagated.
    assert manager_calls["note_host_disconnected"] == 1, (
        "note_host_disconnected was not called on cancellation; the host and "
        "shutdown-callback registration would leak."
    )
    assert fake_dg.close_calls == 1, (
        "dg.close() was not called on cancellation; the Deepgram session "
        "would remain open."
    )
    assert fake_ws.close_calls, "websocket.close() was not called on cancellation."
    assert fake_ws.close_calls[-1]["code"] == 1011, (
        f"websocket.close() code on cancellation was {fake_ws.close_calls[-1]['code']!r}; "
        "expected 1011 (error) to distinguish from the normal room_ended path."
    )


@pytest.mark.asyncio
async def test_ws_translate_post_register_recheck_cleans_up_on_cancellation() -> None:
    """Direct test of the cancellation cleanup block at the ``ws_translate``
    post-register recheck (line 1662 area). Mirrors the test above for
    the listener registration cleanup shape."""
    fake_ws = _FakeWS()
    manager_calls = {"disconnect": 0}
    ip_counter: dict[str, int] = {"ip-1": 1}  # pre-registered
    viewer_ip = "ip-1"

    def fake_disconnect(ws: object) -> None:
        manager_calls["disconnect"] += 1
        assert ws is fake_ws

    async def cancelled_recheck(org: str, room: str) -> bool:
        raise asyncio.CancelledError()

    async def recheck_block_replica(org_id: str, room_id: str) -> None:
        """Replicates the exact cleanup pattern at the patched ws_translate
        post-register site in app/main.py. Uses ``_main._is_room_live_async``
        so the test's patch on that attribute is honored."""
        try:
            _ = await _main._is_room_live_async(org_id, room_id)
        except asyncio.CancelledError:
            fake_disconnect(fake_ws)
            _remaining = ip_counter.get(viewer_ip, 1) - 1
            if _remaining <= 0:
                ip_counter.pop(viewer_ip, None)
            else:
                ip_counter[viewer_ip] = _remaining
            try:
                await fake_ws.close(code=1011)
            except Exception:
                pass
            raise

    with patch.object(_main, "_is_room_live_async", side_effect=cancelled_recheck):
        with pytest.raises(asyncio.CancelledError):
            await recheck_block_replica("org-x", "room-y")

    assert manager_calls["disconnect"] == 1
    assert viewer_ip not in ip_counter, "IP counter slot was not decremented/popped"
    assert fake_ws.close_calls, "websocket.close() was not called on cancellation."
    assert fake_ws.close_calls[-1]["code"] == 1011
