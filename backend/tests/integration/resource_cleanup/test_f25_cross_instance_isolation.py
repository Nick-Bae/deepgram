"""F-25 — test the leading hypothesis for the Track 1 Gate 2 failure.

Reference: defect issue #29, Track 1 Gate 2 failure record on issue #26.

The leading (not proven) hypothesis behind the 2026-09-20 Gate 2
failure is cross-instance resource separation when `REDIS_ENABLED=0`:
during a Cloud Run revision transition the producer socket may remain
on one instance while the listener's reconnect lands on another, and
without Redis fanout the cross-instance broadcast path is not
available. F-25 models that end state directly in the harness — no
SIGTERM, no revision transition — by:

  1. Starting the host + a listener on backend A.
  2. Verifying a baseline marker reaches the listener on A.
  3. Handing the listener over to backend B (disconnect, reconnect
     to B), while the original host and A continue running.
  4. Sending a fresh marker while both backends are up with
     `REDIS_ENABLED=0`.
  5. Requiring that the fresh marker still reaches an A-side
     listener (proves the A pipeline is still armed).
  6. Requiring that the fresh marker does NOT reach the handover
     listener on B within a bounded timeout.
  7. Confirming during the absence window that backend B is alive,
     the listener's socket + reader are healthy, and no terminal
     frame arrived.

If the hypothesis is correct, this test passes; if the same
configuration somehow delivers cross-instance without Redis, this
test fails and the hypothesis is falsified.

Companion F-26 flips `REDIS_ENABLED=1` and requires the fresh marker
to reach the handover listener.

Note on the production incident:
This test does NOT assert that Uvicorn emitted 1012 or that Cloud
Run drained a specific socket during the Gate 2 failure — those were
not directly established. F-25 tests only the end-state isolation.
"""
from __future__ import annotations

import asyncio
import uuid

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


def _config(
    instance_id: str,
    deepgram_endpoint: str,
    openai_base_url: str,
    *,
    redis_enabled: str,
) -> BackendConfig:
    """extra_env runs last in BackendProcess.env(), so this override
    actually takes effect regardless of the harness default."""
    return BackendConfig(
        instance_id=instance_id,
        redis_host=REDIS_HOST,
        redis_port=REDIS_PORT,
        firestore_emulator_host=FIRESTORE_EMULATOR_HOST,
        gcp_project=GCP_PROJECT,
        deepgram_endpoint=deepgram_endpoint,
        openai_base_url=openai_base_url,
        host_api_token=HOST_TOKEN,
        extra_env={"REDIS_ENABLED": redis_enabled},
    )


def _has_marker_predicate(marker: str):
    expected = f"[stub-translated] {marker}"
    def _pred(msg):
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


def _received_any_terminal_frame(listener: ListenerClient) -> bool:
    """Detect any terminal signal in the received frames — used during
    the isolation window to distinguish "quiet because isolated" from
    "quiet because the server sent an ended signal we shouldn't have
    trusted." A production Gate 2 pass requires zero terminal signals
    on the listener."""
    for msg in listener.received:
        if not isinstance(msg, dict):
            continue
        if msg.get("type") == "STATUS" and msg.get("roomStatus") == "ended":
            return True
        if msg.get("reason") == "room_ended":
            return True
        if msg.get("code") == 4001:
            return True
    return False


def test_f25_cross_instance_isolation_with_redis_disabled(admin_store):
    asyncio.run(_run_f25(admin_store))


async def _run_f25(admin_store):
    slug = f"church-{uuid.uuid4().hex[:8]}"
    org_id = f"org-{uuid.uuid4().hex[:8]}"
    service_key = "sunday"
    room_id = f"room-{uuid.uuid4().hex[:8]}"

    deepgram_stub = DeepgramStub()
    openai_stub = OpenAIStub()
    backend_a = None
    backend_b = None
    host_client = None
    listener_a_persistent = None
    listener_migrating_on_a = None
    listener_b_post_handover = None

    async def teardown():
        for target in (
            listener_b_post_handover,
            listener_migrating_on_a,
            listener_a_persistent,
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
        seed_org_and_service(
            admin_store,
            org_id=org_id, slug=slug,
            service_key=service_key, host_token=HOST_TOKEN,
            # Not strictly needed for F-25 (no HTTP End Service here),
            # but seeded to match F-26's baseline exactly.
            e2e_host_uid="e2e-host-uid",
        )

        await deepgram_stub.start()
        await openai_stub.start()

        # Both backends run with REDIS_ENABLED=0.
        backend_a = BackendProcess(_config(
            "inst-a", deepgram_stub.endpoint, openai_stub.base_url,
            redis_enabled="0",
        ))
        backend_a.start()
        backend_a.wait_ready(timeout=45.0)

        backend_b = BackendProcess(_config(
            "inst-b", deepgram_stub.endpoint, openai_stub.base_url,
            redis_enabled="0",
        ))
        backend_b.start()
        backend_b.wait_ready(timeout=45.0)

        start_room(
            admin_store,
            org_id=org_id, service_key=service_key, room_id=room_id,
        )

        # Host + BOTH listeners initially on A. The migrating listener
        # will move to B after the baseline. The persistent listener
        # stays on A so we can assert A-side delivery still works when
        # we later send a fresh marker.
        host_client = HostClient(
            backend_a.ws_url,
            org_id=org_id, room_id=room_id,
            service_key=service_key, church_slug=slug,
            host_token=HOST_TOKEN,
        )
        await host_client.connect()
        await deepgram_stub.wait_for_client_count(1, timeout=15.0)

        listener_a_persistent = ListenerClient(
            backend_a.ws_url,
            org_id=org_id, room_id=room_id,
            service_key=service_key, church_slug=slug,
        )
        await listener_a_persistent.connect()
        await listener_a_persistent.wait_for_frame(
            lambda m: m.get("type") == "JOINED" and m.get("roomId") == room_id,
            timeout=15.0,
        )

        listener_migrating_on_a = ListenerClient(
            backend_a.ws_url,
            org_id=org_id, room_id=room_id,
            service_key=service_key, church_slug=slug,
        )
        await listener_migrating_on_a.connect()
        await listener_migrating_on_a.wait_for_frame(
            lambda m: m.get("type") == "JOINED" and m.get("roomId") == room_id,
            timeout=15.0,
        )

        # BASELINE — both A-side listeners must receive the marker.
        # baseline- prefix is one of the openai_stub-supported names
        # AND also matches the broader regex the stub now accepts.
        baseline_marker = f"baseline-{uuid.uuid4().hex[:6]}"
        pre_baseline_translations = openai_stub.request_count
        delivered = await deepgram_stub.send_transcript(
            f"안녕하세요 {baseline_marker}", is_final=True,
        )
        assert delivered >= 1, "Deepgram stub had no client attached"

        await listener_a_persistent.wait_for_frame(
            _has_marker_predicate(baseline_marker), timeout=20.0,
        )
        await listener_migrating_on_a.wait_for_frame(
            _has_marker_predicate(baseline_marker), timeout=20.0,
        )
        assert openai_stub.request_count > pre_baseline_translations, (
            "baseline translation did not hit the OpenAI stub — a "
            "later A-side delivery check for the fresh marker would "
            "be meaningless."
        )

        # HANDOVER — migrating listener disconnects from A, a fresh
        # ListenerClient with the same room+service context attaches
        # to B. In production, this is what happens when a listener's
        # transient-close reconnect lands on a different revision
        # than the producer.
        await listener_migrating_on_a.close()
        listener_migrating_on_a = None

        listener_b_post_handover = ListenerClient(
            backend_b.ws_url,
            org_id=org_id, room_id=room_id,
            service_key=service_key, church_slug=slug,
        )
        await listener_b_post_handover.connect()
        await listener_b_post_handover.wait_for_frame(
            lambda m: m.get("type") == "JOINED" and m.get("roomId") == room_id,
            timeout=15.0,
        )

        # BEFORE the absence window: the reader task must exist and
        # be running. A dead reader would silently swallow any frame
        # the isolation window would have observed, so this check
        # anchors the "listener_b is healthy right now" claim.
        assert listener_b_post_handover.reader_alive(), (
            "listener_b's reader task was not alive before the "
            "isolation window — the absence check would be meaningless"
        )

        # FRESH MARKER while REDIS_ENABLED=0 on both instances.
        isolation_marker = f"isolation-{uuid.uuid4().hex[:6]}"
        pre_isolation_translations = openai_stub.request_count
        delivered = await deepgram_stub.send_transcript(
            f"안녕하세요 {isolation_marker}", is_final=True,
        )
        assert delivered >= 1, "Deepgram stub had no client attached"

        # A-SIDE DELIVERY CHECK — the persistent listener on A must
        # still receive the fresh marker. This rules out "quiet
        # because the whole pipeline stopped" as a false-pass
        # explanation for the isolation assertion below.
        await listener_a_persistent.wait_for_frame(
            _has_marker_predicate(isolation_marker), timeout=20.0,
        )
        assert openai_stub.request_count > pre_isolation_translations, (
            "fresh marker did not trigger a translation call — the "
            "A-side broadcast pipeline is not firing, so the isolation "
            "assertion below cannot be trusted."
        )

        # ISOLATION ASSERTION — listener on B must NOT receive the
        # fresh marker within a bounded window. Timeout is generous
        # so a slow CI does not mask a real cross-instance recovery.
        try:
            await listener_b_post_handover.wait_for_frame(
                _has_marker_predicate(isolation_marker), timeout=12.0,
            )
            raise AssertionError(
                "listener on instance B received the fresh marker "
                "despite REDIS_ENABLED=0 on both instances — cross-"
                "instance fanout should be impossible in this "
                "configuration. Investigate before trusting F-26."
            )
        except asyncio.TimeoutError:
            pass  # expected — the isolation hypothesis holds

        # DEAD-LISTENER CHECK — the isolation timeout must be caused
        # by "message never arrived across the instance boundary,"
        # NOT by "the listener's socket died and its reader stopped
        # collecting frames." Verify backend B is alive, the socket
        # is still open, and no terminal frame was received.
        assert backend_b.proc is not None and backend_b.proc.poll() is None, (
            "backend B exited during the isolation window — the timeout "
            "cannot be attributed to isolation without a live server"
        )
        assert await listener_b_post_handover.is_open(), (
            "listener B's WebSocket closed during the isolation window — "
            "the missing marker could be the server tearing the socket "
            "down rather than the isolation hypothesis"
        )
        assert not _received_any_terminal_frame(listener_b_post_handover), (
            "listener B received a terminal frame during the isolation "
            "window — the server signalled ended when it shouldn't have"
        )
        # AFTER the absence window: the reader must STILL be running.
        # A reader that finished mid-window (task cancelled, exception
        # raised) would look identical to isolation from the test's
        # perspective — no frames arrive — but the cause would be
        # completely different.
        assert listener_b_post_handover.reader_alive(), (
            "listener_b's reader task ended during the isolation "
            "window — the missing marker could be the reader dying "
            "rather than the isolation hypothesis"
        )

        # LIFECYCLE CHECK — the room is not supposed to have moved.
        room_state = read_room(admin_store, org_id=org_id, room_id=room_id)
        assert room_state is not None, "room disappeared from Firestore"
        assert room_state.get("status") == "live", (
            f"room status flipped to {room_state.get('status')!r} — "
            "F-25 does not exercise any termination path"
        )

    finally:
        await teardown()
