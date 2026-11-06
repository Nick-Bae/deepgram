"""Regression tests for the ``_is_room_live_async`` wrapper and the two
production helpers that each patched WebSocket handler actually invokes.

Context: ``FirestoreMultiChurchStore.is_room_live`` performs a synchronous
Firestore ``.get(timeout=_FS_TIMEOUT)`` RPC. Called inline from an async
handler it blocks the asyncio event loop for the full RPC round-trip. The
sync-in-async pattern is demonstrated by code inspection; its contribution
to the historical production lag remains unverified (see INVESTIGATION.md).

This module tests:

- ``_is_room_live_async``: dispatches off the event-loop thread, remains
  responsive while a concurrent coroutine runs, propagates both
  ``Exception`` and ``asyncio.CancelledError``.
- ``_ws_translate_post_register_recheck_or_cleanup``: the EXACT helper
  the ``/ws/translate`` handler invokes after ``manager.register_listener``.
  Three regression tests cover is-live PASS, not-live cleanup, and
  CancelledError cleanup (which includes an auxiliary check: removing the
  helper's ``except asyncio.CancelledError`` guard makes the regression
  test fail).
- ``_ws_stt_deepgram_post_register_recheck_or_cleanup``: the EXACT helper
  the ``/ws/stt/deepgram`` handler invokes after ``manager.register_host``.
  Same three-case coverage with the same auxiliary "remove the guard → test
  fails" demonstration.

``app.main`` is a prerequisite for this regression surface; unexpected
import failure fails the run rather than silently skipping.
"""
from __future__ import annotations

import asyncio
import os as _os
import threading
import time
from unittest.mock import patch

import pytest


# ``app.main`` validates CORS_ALLOW_ORIGINS at import time. Set an explicit
# origin so import succeeds in isolated test runs.
_os.environ.setdefault("CORS_ALLOW_ORIGINS", "http://localhost:3000")

# Required imports — any failure fails every test (intentional, not skipped).
from app import main as _main  # noqa: E402  pylint: disable=wrong-import-position
from app.main import (  # noqa: E402
    _is_room_live_async,
    _ws_stt_deepgram_post_register_recheck_or_cleanup,
    _ws_translate_post_register_recheck_or_cleanup,
    multichurch_store,
)


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
    assert recorded["tid"] != loop_tid


@pytest.mark.asyncio
async def test_is_room_live_async_concurrent_ticker_progresses_during_block() -> None:
    """Assert the ticker makes progress WHILE the slow worker is blocked.

    Snapshots the ticker count immediately before invoking the slow wrapper
    and immediately after it returns; a blocking inline implementation
    would produce 0-1 ticks during the 100-ms wrapper call and fail the
    assertion. The offloaded implementation yields and the ticker runs.
    """
    tick_count = {"n": 0}

    async def ticker() -> None:
        for _ in range(500):
            tick_count["n"] += 1
            await asyncio.sleep(0.001)

    def slow_impl(org: str, room: str) -> bool:
        time.sleep(0.1)
        return True

    ticker_task = asyncio.create_task(ticker())
    await asyncio.sleep(0.01)  # warm-up
    tick_before = tick_count["n"]

    with patch.object(multichurch_store, "is_room_live", side_effect=slow_impl):
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

    assert ticks_during_block >= 30, (
        f"ticker made only {ticks_during_block} ticks during the "
        f"{block_elapsed*1000:.1f}-ms wrapper call. With offloading, "
        "expected ≥ 30. The event loop was likely blocked."
    )


@pytest.mark.asyncio
async def test_is_room_live_async_propagates_exceptions() -> None:
    class _FakeFirestoreError(RuntimeError):
        pass

    def raising_impl(org: str, room: str) -> bool:
        raise _FakeFirestoreError("firestore unavailable")

    with patch.object(multichurch_store, "is_room_live", side_effect=raising_impl):
        with pytest.raises(_FakeFirestoreError):
            await _is_room_live_async("org-x", "room-y")


@pytest.mark.asyncio
async def test_is_room_live_async_propagates_cancellation() -> None:
    """CancelledError must propagate so the handler's cleanup can run."""
    def slow_impl(org: str, room: str) -> bool:
        time.sleep(0.5)
        return True

    with patch.object(multichurch_store, "is_room_live", side_effect=slow_impl):
        task = asyncio.create_task(_is_room_live_async("org-x", "room-y"))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Production-helper tests: /ws/translate post-register
# ---------------------------------------------------------------------------


class _FakeWS:
    """Minimal async-WebSocket stand-in that records close calls."""
    def __init__(self) -> None:
        self.close_calls: list[dict] = []
        self.closed = False

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_calls.append({"code": code, "reason": reason})
        self.closed = True


class _FakeManager:
    def __init__(self) -> None:
        self.disconnect_calls: list[object] = []
        self.note_host_disconnected_calls: list[object] = []

    def disconnect(self, ws: object) -> None:
        self.disconnect_calls.append(ws)

    def note_host_disconnected(self, ws: object) -> None:
        self.note_host_disconnected_calls.append(ws)


class _FakeDg:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


@pytest.fixture
def listener_mocks():
    """Provide a complete mock surface for the listener helper.

    Patches app.main's module-level ``manager``, ``_ws_viewer_ip_conns``, and
    ``_ws_viewer_ip_lock`` with test doubles so the helper exercises its
    real code path. Tests can patch ``_is_room_live_async`` on top of this.
    """
    fake_ws = _FakeWS()
    fake_manager = _FakeManager()
    viewer_ip = "ip-1"
    # Start with the viewer already counted (as the real handler does
    # before this recheck runs).
    ip_counter: dict[str, int] = {viewer_ip: 1}

    with patch.object(_main, "manager", fake_manager), \
         patch.object(_main, "_ws_viewer_ip_conns", ip_counter), \
         patch.object(_main, "_ws_viewer_ip_lock", threading.Lock()):
        yield {
            "ws": fake_ws,
            "manager": fake_manager,
            "viewer_ip": viewer_ip,
            "ip_counter": ip_counter,
        }


@pytest.mark.asyncio
async def test_ws_translate_helper_returns_true_when_live(listener_mocks) -> None:
    """When the room is live, the helper returns True and does NOT clean up."""
    m = listener_mocks
    with patch.object(_main, "_is_room_live_async", return_value=True):
        result = await _ws_translate_post_register_recheck_or_cleanup(
            ws=m["ws"], joined_org_id="org-x", joined_room_id="room-y",
            viewer_ip=m["viewer_ip"],
        )

    assert result is True
    assert m["manager"].disconnect_calls == []
    assert m["ip_counter"].get(m["viewer_ip"]) == 1  # unchanged
    assert m["ws"].close_calls == []


@pytest.mark.asyncio
async def test_ws_translate_helper_cleans_up_when_not_live(listener_mocks) -> None:
    """When the room is not live, the helper cleans up and returns False."""
    m = listener_mocks
    with patch.object(_main, "_is_room_live_async", return_value=False):
        result = await _ws_translate_post_register_recheck_or_cleanup(
            ws=m["ws"], joined_org_id="org-x", joined_room_id="room-y",
            viewer_ip=m["viewer_ip"],
        )

    assert result is False
    assert m["manager"].disconnect_calls == [m["ws"]]
    assert m["viewer_ip"] not in m["ip_counter"]
    assert m["ws"].close_calls == [{"code": 1000, "reason": "room_ended"}]


@pytest.mark.asyncio
async def test_ws_translate_helper_cleans_up_on_cancellation(listener_mocks) -> None:
    """On CancelledError during the recheck, the helper cleans up BEFORE
    re-raising. This is the regression: without the ``except asyncio.CancelledError``
    guard, CancelledError bypasses ``except Exception`` and leaks the
    listener registration + IP-counter slot."""
    m = listener_mocks

    async def cancelled_recheck(org: str, room: str) -> bool:
        raise asyncio.CancelledError()

    with patch.object(_main, "_is_room_live_async", side_effect=cancelled_recheck):
        with pytest.raises(asyncio.CancelledError):
            await _ws_translate_post_register_recheck_or_cleanup(
                ws=m["ws"], joined_org_id="org-x", joined_room_id="room-y",
                viewer_ip=m["viewer_ip"],
            )

    assert m["manager"].disconnect_calls == [m["ws"]]
    assert m["viewer_ip"] not in m["ip_counter"]
    assert m["ws"].close_calls == [{"code": 1011, "reason": None}]


@pytest.mark.asyncio
async def test_ws_translate_helper_unguarded_leaks_on_cancellation(listener_mocks) -> None:
    """Auxiliary discriminator: construct the exact SAME helper body but
    WITHOUT the ``except asyncio.CancelledError`` guard. Assert that in
    that configuration, cancellation propagates WITHOUT cleanup — proving
    the guard is load-bearing and the previous test is not trivially green.
    """
    m = listener_mocks

    async def unguarded_variant(*, ws, joined_org_id, joined_room_id, viewer_ip):
        # Same shape as the production helper but WITHOUT the
        # `except asyncio.CancelledError` guard. CancelledError should
        # escape here unhandled.
        try:
            is_live = await _main._is_room_live_async(joined_org_id, joined_room_id)
        except Exception:
            is_live = True
        if is_live:
            return True
        _main.manager.disconnect(ws)
        with _main._ws_viewer_ip_lock:
            _remaining = _main._ws_viewer_ip_conns.get(viewer_ip, 1) - 1
            if _remaining <= 0:
                _main._ws_viewer_ip_conns.pop(viewer_ip, None)
            else:
                _main._ws_viewer_ip_conns[viewer_ip] = _remaining
        try:
            await ws.close(code=1000, reason="room_ended")
        except Exception:
            pass
        return False

    async def cancelled_recheck(org: str, room: str) -> bool:
        raise asyncio.CancelledError()

    with patch.object(_main, "_is_room_live_async", side_effect=cancelled_recheck):
        with pytest.raises(asyncio.CancelledError):
            await unguarded_variant(
                ws=m["ws"], joined_org_id="org-x", joined_room_id="room-y",
                viewer_ip=m["viewer_ip"],
            )

    # LEAK: cleanup did NOT run because the unguarded variant has no
    # except-CancelledError clause. This is the behaviour the production
    # helper's guard prevents.
    assert m["manager"].disconnect_calls == [], (
        "Unguarded variant called disconnect; the test does not actually "
        "exercise the pre-guard behaviour."
    )
    assert m["ip_counter"].get(m["viewer_ip"]) == 1, "IP counter was cleaned up by unguarded variant"
    assert m["ws"].close_calls == [], "WebSocket was closed by unguarded variant"


# ---------------------------------------------------------------------------
# Production-helper tests: /ws/stt/deepgram post-register
# ---------------------------------------------------------------------------


@pytest.fixture
def deepgram_mocks():
    fake_ws = _FakeWS()
    fake_dg = _FakeDg()
    fake_manager = _FakeManager()
    with patch.object(_main, "manager", fake_manager):
        yield {
            "ws": fake_ws,
            "dg": fake_dg,
            "manager": fake_manager,
        }


@pytest.mark.asyncio
async def test_ws_stt_deepgram_helper_returns_true_when_live(deepgram_mocks) -> None:
    m = deepgram_mocks
    with patch.object(_main, "_is_room_live_async", return_value=True):
        result = await _ws_stt_deepgram_post_register_recheck_or_cleanup(
            websocket=m["ws"], dg=m["dg"], org_id="org-x", room_id="room-y",
        )

    assert result is True
    assert m["manager"].note_host_disconnected_calls == []
    assert m["dg"].close_calls == 0
    assert m["ws"].close_calls == []


@pytest.mark.asyncio
async def test_ws_stt_deepgram_helper_cleans_up_when_not_live(deepgram_mocks) -> None:
    m = deepgram_mocks
    with patch.object(_main, "_is_room_live_async", return_value=False):
        result = await _ws_stt_deepgram_post_register_recheck_or_cleanup(
            websocket=m["ws"], dg=m["dg"], org_id="org-x", room_id="room-y",
        )

    assert result is False
    assert m["manager"].note_host_disconnected_calls == [m["ws"]]
    assert m["dg"].close_calls == 1
    assert m["ws"].close_calls == [{"code": 1000, "reason": "room_ended"}]


@pytest.mark.asyncio
async def test_ws_stt_deepgram_helper_cleans_up_on_cancellation(deepgram_mocks) -> None:
    """Regression: CancelledError during the recheck must trigger cleanup
    (``note_host_disconnected`` + ``dg.close()`` + ``websocket.close(1011)``)
    before CancelledError propagates. Without the ``except asyncio.CancelledError``
    guard, cancellation would leave the host registration, shutdown
    callback, Deepgram session, and WebSocket all dangling."""
    m = deepgram_mocks

    async def cancelled_recheck(org: str, room: str) -> bool:
        raise asyncio.CancelledError()

    with patch.object(_main, "_is_room_live_async", side_effect=cancelled_recheck):
        with pytest.raises(asyncio.CancelledError):
            await _ws_stt_deepgram_post_register_recheck_or_cleanup(
                websocket=m["ws"], dg=m["dg"], org_id="org-x", room_id="room-y",
            )

    assert m["manager"].note_host_disconnected_calls == [m["ws"]]
    assert m["dg"].close_calls == 1
    assert m["ws"].close_calls == [{"code": 1011, "reason": None}]


@pytest.mark.asyncio
async def test_ws_stt_deepgram_helper_unguarded_leaks_on_cancellation(
    deepgram_mocks,
) -> None:
    """Auxiliary discriminator: unguarded variant exposes the leak the
    production helper's except-CancelledError prevents."""
    m = deepgram_mocks

    async def unguarded_variant(*, websocket, dg, org_id, room_id):
        try:
            is_live = await _main._is_room_live_async(org_id, room_id)
        except Exception:
            is_live = True
        if is_live:
            return True
        _main.manager.note_host_disconnected(websocket)
        try:
            await asyncio.wait_for(dg.close(), timeout=3.0)
        except Exception:
            pass
        try:
            await websocket.close(code=1000, reason="room_ended")
        except Exception:
            pass
        return False

    async def cancelled_recheck(org: str, room: str) -> bool:
        raise asyncio.CancelledError()

    with patch.object(_main, "_is_room_live_async", side_effect=cancelled_recheck):
        with pytest.raises(asyncio.CancelledError):
            await unguarded_variant(
                websocket=m["ws"], dg=m["dg"], org_id="org-x", room_id="room-y",
            )

    assert m["manager"].note_host_disconnected_calls == [], (
        "Unguarded variant called note_host_disconnected"
    )
    assert m["dg"].close_calls == 0, "Unguarded variant closed dg"
    assert m["ws"].close_calls == [], "Unguarded variant closed websocket"
