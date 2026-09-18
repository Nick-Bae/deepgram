"""F-9 — real SIGTERM shutdown, no application helper.

Audit reference: `docs/03-analysis/resource-cleanup-audit.md` F-9.

The reviewer's requirement was blunt: "Test the real SIGTERM path — not
an application helper." Every observation here comes from Uvicorn's
own behavior under `os.kill(pid, SIGTERM)`. Nothing in the app is
patched, and no test-only endpoints exist to force behavior.

What this test locks in:

  1. **Uvicorn 0.34 closes active WebSockets with code 1012** (Service
     Restart) on SIGTERM. If Uvicorn's behavior ever changes to
     something else, this test fails and we know to revisit the client-
     side classifier (both hooks would need to add whatever new code
     Uvicorn emits to their transient-close set).

  2. **No `room_ended` frame is emitted during shutdown.** A room_ended
     event here would tombstone listener pages that should transparently
     reconnect to a surviving instance. `_on_shutdown` is separately
     locked down structurally (test_on_shutdown_structural.py), but
     this suite proves it end-to-end: even a race with an in-flight
     broadcast can't leak a terminal frame.

  3. **Firestore room stays status=live.** Instance restart is not a
     room end. The reconciler on a surviving instance owns terminal
     cleanup; the dying instance must not touch state on its way out.

  4. **The instance A process actually exits.** Cloud Run gives roughly
     10 s between SIGTERM and SIGKILL. If the shutdown handler hangs,
     we get SIGKILL and lose the pubsub `stop()` cleanup. A short exit
     time here is the smoke test for a fast, clean shutdown.

  5. **Redis pubsub subscriber count drops from two to one.** A listener
     remains connected directly to B throughout both observations, so
     the exact decrement proves A's connection went away.

  6. **A fresh listener connects to B and receives translations.** The
     "reconnect after 1012" side of the contract: transient closes are
     recoverable and delivery resumes on a surviving instance.

Baseline before SIGTERM: a marker flows through the A pipeline and
reaches its listener. Every failure after SIGTERM is unambiguously
attributable to the shutdown path.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
import uuid

import redis as redis_sync

from .conftest import (
    FIRESTORE_EMULATOR_HOST,
    GCP_PROJECT,
    REDIS_HOST,
    REDIS_PORT,
)
from .harness.backend_process import BackendConfig, BackendProcess
from .harness.clients import HostClient, ListenerClient
from .harness.deepgram_stub import DeepgramStub
from .harness.firestore_seed import read_room, seed_org_and_service, start_room
from .harness.openai_stub import OpenAIStub


HOST_TOKEN = "harness-host-token"
REDIS_CHANNEL_PREFIX = "worshiptranslate"


def _config(
    instance_id: str,
    deepgram_endpoint: str,
    openai_base_url: str,
) -> BackendConfig:
    return BackendConfig(
        instance_id=instance_id,
        redis_host=REDIS_HOST,
        redis_port=REDIS_PORT,
        firestore_emulator_host=FIRESTORE_EMULATOR_HOST,
        gcp_project=GCP_PROJECT,
        deepgram_endpoint=deepgram_endpoint,
        openai_base_url=openai_base_url,
        host_api_token=HOST_TOKEN,
    )


def _room_channel(org_id: str, room_id: str) -> str:
    return f"{REDIS_CHANNEL_PREFIX}:org:{org_id}:room:{room_id}"


def _numsub(channel: str) -> int:
    """Query PUBSUB NUMSUB for one channel. Returns 0 if the channel
    has no subscribers (redis returns [channel, 0] in that case)."""
    r = redis_sync.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    try:
        result = r.pubsub_numsub(channel)
        for name, count in result:
            if name == channel:
                return int(count)
        return 0
    finally:
        try:
            r.close()
        except Exception:
            pass


def _wait_for(predicate, *, timeout: float = 10.0, interval: float = 0.1) -> bool:
    """Poll a sync predicate until True or timeout. Returns True on
    success, False on timeout. Used for cross-process observations
    where the state change happens outside our event loop."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_f9_sigterm_closes_websockets_with_1012_and_preserves_room(admin_store):
    """Sync wrapper — mirrors F-15's structure, avoids pytest-asyncio."""
    asyncio.run(_run_f9(admin_store))


async def _run_f9(admin_store):
    slug = f"church-{uuid.uuid4().hex[:8]}"
    org_id = f"org-{uuid.uuid4().hex[:8]}"
    service_key = "sunday"
    room_id = f"room-{uuid.uuid4().hex[:8]}"

    deepgram_stub = DeepgramStub()
    openai_stub = OpenAIStub()
    backend_a = None
    backend_b = None
    host_client = None
    listener_a = None
    listener_b_sentinel = None
    listener_b_post = None

    async def teardown():
        for target in (
            listener_b_post,
            listener_b_sentinel,
            listener_a,
            host_client,
        ):
            if target is not None:
                try:
                    await target.close()
                except BaseException:
                    pass
        for proc in (backend_b, backend_a):
            if proc is not None:
                try:
                    proc.stop()
                except BaseException:
                    pass
        for stub in (deepgram_stub, openai_stub):
            if stub is not None:
                try:
                    await stub.stop()
                except BaseException:
                    pass
        for name, proc in [("backend_a", backend_a), ("backend_b", backend_b)]:
            if proc is None:
                continue
            logs = ""
            try:
                logs = proc.logs()
            except BaseException:
                pass
            if logs:
                print(f"===== {name} ({proc.config.instance_id}) logs =====")
                print(logs)
                print(f"===== end {name} logs =====")

    try:
        # (1) Seed org + service.
        seed_org_and_service(
            admin_store,
            org_id=org_id,
            slug=slug,
            service_key=service_key,
            host_token=HOST_TOKEN,
        )

        # (2) Stubs.
        await deepgram_stub.start()
        await openai_stub.start()

        # (3) Start BOTH backends before opening any client. Instance A
        # will die; instance B is the survivor that must remain
        # subscribed to Redis and receive the reconnected listener.
        backend_a = BackendProcess(
            _config("inst-a", deepgram_stub.endpoint, openai_stub.base_url)
        )
        backend_a.start()
        backend_a.wait_ready(timeout=45.0)
        a_pid = backend_a.proc.pid  # noqa: F841 — capture for post-mortem too

        backend_b = BackendProcess(
            _config("inst-b", deepgram_stub.endpoint, openai_stub.base_url)
        )
        backend_b.start()
        backend_b.wait_ready(timeout=45.0)

        # (4) Room created AFTER both backends are healthy. Order
        # doesn't matter for F-9 (no startup-cleanup race here), but
        # keeping the parallel with F-15 makes both tests easier to
        # reason about together.
        start_room(
            admin_store,
            org_id=org_id,
            service_key=service_key,
            room_id=room_id,
        )

        channel = _room_channel(org_id, room_id)

        # (5) Attach a listener + a host to instance A, plus a sentinel
        # listener directly to B. The sentinel stays connected through
        # both Redis assertions, making the expected counts exact.
        listener_a = ListenerClient(
            backend_a.ws_url,
            org_id=org_id,
            room_id=room_id,
            service_key=service_key,
            church_slug=slug,
        )
        await listener_a.connect()
        await listener_a.wait_for_frame(
            lambda m: m.get("type") == "JOINED" and m.get("roomId") == room_id,
            timeout=15.0,
        )

        listener_b_sentinel = ListenerClient(
            backend_b.ws_url,
            org_id=org_id,
            room_id=room_id,
            service_key=service_key,
            church_slug=slug,
        )
        await listener_b_sentinel.connect()
        await listener_b_sentinel.wait_for_frame(
            lambda m: m.get("type") == "JOINED" and m.get("roomId") == room_id,
            timeout=15.0,
        )

        host_client = HostClient(
            backend_a.ws_url,
            org_id=org_id,
            room_id=room_id,
            service_key=service_key,
            church_slug=slug,
            host_token=HOST_TOKEN,
        )
        await host_client.connect()
        await deepgram_stub.wait_for_client_count(1, timeout=15.0)

        # Give A's subscriber time to actually register on Redis (the
        # subscribe happens on first broadcast, so this poll gives it
        # a chance to appear). Failing to see A here would mean the
        # broadcast pipeline never armed — pipeline bug, not F-9.
        def _pred_both_subscribed():
            return _numsub(channel) == 2

        assert _wait_for(_pred_both_subscribed, timeout=10.0), (
            f"expected exactly two Redis subscribers for {channel!r} before "
            f"SIGTERM (A listener + B sentinel), observed {_numsub(channel)}."
        )

        # (6) Baseline: a marker delivered through A proves the whole
        # pipeline is armed before we send SIGTERM.
        baseline_marker = f"baseline-{uuid.uuid4().hex[:6]}"
        delivered = await deepgram_stub.send_transcript(
            f"안녕하세요 {baseline_marker}", is_final=True
        )
        assert delivered >= 1, "Deepgram stub had no client attached"

        def _has_marker(marker):
            def _pred(msg):
                expected = f"[stub-translated] {marker}"
                for key in ("payload", "text"):
                    val = msg.get(key)
                    if isinstance(val, str) and expected in val:
                        return True
                meta = msg.get("meta") or {}
                val = meta.get("translated")
                if isinstance(val, str) and expected in val:
                    return True
                return False
            return _pred

        await listener_a.wait_for_frame(_has_marker(baseline_marker), timeout=20.0)
        await listener_b_sentinel.wait_for_frame(
            _has_marker(baseline_marker), timeout=20.0
        )

        # Snapshot the listener frame count so we can prove NO further
        # terminal frame arrived during shutdown.
        pre_signal_frame_count = len(listener_a.received)

        # (7) Real SIGTERM. The whole point of F-9: no application
        # helper, no _on_shutdown hook invocation from Python — the
        # kernel delivers SIGTERM to the uvicorn PID exactly as Cloud
        # Run does on instance restart.
        sigterm_at = time.monotonic()
        os.kill(backend_a.proc.pid, signal.SIGTERM)

        # Bound the real process exit at the signal boundary. The wait
        # runs in a worker so the event loop remains free to service the
        # WebSocket clients and provider stub close handshake.
        await asyncio.to_thread(backend_a.proc.wait, timeout=9.5)
        exited_at = time.monotonic()
        shutdown_elapsed = exited_at - sigterm_at
        assert shutdown_elapsed < 10.0, (
            f"backend A took {shutdown_elapsed:.3f}s to exit after SIGTERM; "
            "Cloud Run may SIGKILL the process at approximately 10s"
        )

        # (8) The client-side WebSockets must close. Uvicorn 0.34
        # closes them with code 1012 (Service Restart) before its own
        # process exit. Both listener_a and host_client observe this.
        await listener_a.wait_closed(timeout=15.0)
        await host_client.wait_closed(timeout=15.0)

        listener_close_code = listener_a.close_code()
        host_close_code = host_client.close_code()
        listener_close_reason = listener_a.close_reason()
        host_close_reason = host_client.close_reason()

        # Assert: the code is 1012. If Uvicorn ever changes this, the
        # test tells us which code we now see so the fix is obvious.
        assert listener_close_code == 1012, (
            f"listener_a WebSocket closed with code={listener_close_code!r} "
            f"reason={listener_close_reason!r} — expected 1012 (Service "
            f"Restart) from Uvicorn 0.34 on SIGTERM. If this fails with "
            f"a different code, either Uvicorn changed its shutdown code "
            f"(update the client-side classifier's transient set to "
            f"include the new code) OR the app is intercepting the "
            f"close and picking a different one (must not do this — "
            f"race with client reconnect)."
        )
        assert host_close_code == 1012, (
            f"host_client WebSocket closed with code={host_close_code!r} "
            f"reason={host_close_reason!r} — same expectation as the "
            f"listener; see the listener assertion above."
        )

        # Assert: NOT a room_ended close. Belt-and-braces — code 4001
        # or reason 'room_ended' would tombstone the client. The two
        # assertions above already exclude this, but naming the failure
        # mode explicitly makes a future regression trivial to read.
        assert listener_close_code != 4001, "listener close code must not be 4001"
        assert host_close_code != 4001, "host close code must not be 4001"
        assert listener_close_reason != "room_ended", (
            "listener close reason must not be 'room_ended' on SIGTERM"
        )
        assert host_close_reason != "room_ended", (
            "host close reason must not be 'room_ended' on SIGTERM"
        )

        # Assert: no terminal STATUS(ended) frame ever appeared on the
        # listener — not before SIGTERM (we already saw a baseline
        # translation), and not during the close race. Uvicorn's frame
        # ordering means anything sent after the close request is
        # dropped, but we still check both sides.
        for msg in listener_a.received:
            assert not (
                msg.get("type") == "STATUS" and msg.get("roomStatus") == "ended"
            ), (
                f"listener_a saw a terminal STATUS(ended) frame during "
                f"SIGTERM shutdown — Uvicorn's SIGTERM path must NOT "
                f"emit terminal room events. Frame: {msg!r}"
            )
        # Sanity: at least one frame was received (the baseline), so
        # an empty-list bug can't quietly pass the loop above.
        assert pre_signal_frame_count >= 1, "no baseline frames received"

        # (9) The provider socket is a paid resource. A's connection must
        # be gone before B establishes a replacement session.
        await deepgram_stub.wait_for_client_count(0, timeout=5.0)

        # (10) Firestore room stays live. The reconciler on B, or on a
        # fresh A instance later, is the authority on cleanup. The
        # dying process must never write terminal state to Firestore.
        room_state = read_room(admin_store, org_id=org_id, room_id=room_id)
        assert room_state is not None, "room disappeared from Firestore"
        assert room_state.get("status") == "live", (
            f"room status flipped to {room_state.get('status')!r} on "
            f"SIGTERM — the shutting-down instance must NOT write "
            f"terminal state (the reconciler owns terminal cleanup). "
            f"See test_on_shutdown_structural.py for the static guard."
        )

        # (11) Redis subscriber count drops exactly from two to one.
        # B's sentinel has remained open, so one cannot be a false pass
        # caused by A leaking while B was never subscribed.
        def _pred_a_dropped():
            return _numsub(channel) == 1

        assert _wait_for(_pred_a_dropped, timeout=10.0), (
            f"Redis subscriber count for {channel!r} did not drop after "
            f"SIGTERM (observed {_numsub(channel)}, expected 1). Instance A left a "
            f"lingering subscriber; either its connection wasn't closed "
            f"or Redis hasn't cleaned it up. Cloud Run production would "
            f"leak this on every rolling restart."
        )

        # (12) Reconnect to B. This is the "1012 is transient" side of
        # the contract: a fresh listener on B receives the fanout.
        listener_b_post = ListenerClient(
            backend_b.ws_url,
            org_id=org_id,
            room_id=room_id,
            service_key=service_key,
            church_slug=slug,
        )
        await listener_b_post.connect()
        await listener_b_post.wait_for_frame(
            lambda m: m.get("type") == "JOINED" and m.get("roomId") == room_id,
            timeout=15.0,
        )

        # Now B is subscribed. Marker delivery requires a host on B —
        # in production, the host page reconnects to B via
        # useDeepgramProducer's scheduleReconnect(). Simulate that
        # here with a fresh HostClient on B (the same params). This is
        # what the reviewer meant by "reconnect to B" for markers.
        host_b = HostClient(
            backend_b.ws_url,
            org_id=org_id,
            room_id=room_id,
            service_key=service_key,
            church_slug=slug,
            host_token=HOST_TOKEN,
        )
        try:
            await host_b.connect()
            await deepgram_stub.wait_for_client_count(1, timeout=15.0)

            post_marker = f"post-sigterm-{uuid.uuid4().hex[:6]}"
            delivered = await deepgram_stub.send_transcript(
                f"안녕하세요 {post_marker}", is_final=True
            )
            assert delivered == 1, (
                f"expected exactly host B's provider connection, delivered to {delivered}"
            )
            await listener_b_post.wait_for_frame(
                _has_marker(post_marker), timeout=20.0
            )
        finally:
            try:
                await host_b.close()
            except BaseException:
                pass

    finally:
        await teardown()
