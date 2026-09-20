"""F-26 — same host-A → handover-to-B scenario as F-25 but with Redis
enabled: cross-instance delivery works, and End Service cleans up on
every instance that touched the room.

Reference: defect issue #29, Track 1 Gate 2 failure record on issue #26.

F-25 tests the leading hypothesis by reproducing the observed
isolation with `REDIS_ENABLED=0`. F-26 flips the switch: same
handover shape (listener starts on A, then moves to B, host stays on
A) but with `REDIS_ENABLED=1` on both instances.

What F-26 asserts:
  1. Baseline delivery on A works.
  2. After the handover, a fresh marker sent via A reaches the
     listener on B via the existing Redis fanout in
     `socket_manager.broadcast_room` / `redis_pubsub`.
  3. End Service on the room (functional equivalent of the
     production authenticated HTTP endpoint — implemented here as
     an admin-store write; see note below) drives a clean
     termination and cleanup on every instance that touched the
     room:
       - the listener on B either closes or receives a terminal
         frame,
       - the host's producer WebSocket on A closes,
       - the Deepgram provider stub's client count returns to zero,
       - Firestore reflects `status=ended` and `endReason=host_end`.

Note on End Service in the harness:
The production End Service HTTP endpoint requires a Firebase ID
token; the harness has no Firebase. `admin_store.end_room()` writes
the same Firestore state (`status=ended`, `endedAt`, `endReason`)
that the production endpoint writes, and the reconciler on each
instance observes the ended state and performs local cleanup — the
same safety net that Track 1's PR-T1-C was designed to provide.

Distinct from F-15:
F-15 has listeners on BOTH A and B simultaneously. F-26 explicitly
removes the A-side listener before the cross-instance assertion so
the delivery MUST have crossed the instance boundary.
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
) -> BackendConfig:
    """Harness default is REDIS_ENABLED=1; no override needed. F-26
    intentionally does NOT touch extra_env so its Redis state is
    identical to what F-15 exercises."""
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


def _received_terminal_status(listener: ListenerClient) -> bool:
    for msg in listener.received:
        if isinstance(msg, dict) and (
            (msg.get("type") == "STATUS" and msg.get("roomStatus") == "ended")
            or msg.get("reason") == "room_ended"
            or msg.get("code") == 4001
        ):
            return True
    return False


def test_f26_redis_fanout_recovers_cross_instance_delivery(admin_store):
    asyncio.run(_run_f26(admin_store))


async def _run_f26(admin_store):
    slug = f"church-{uuid.uuid4().hex[:8]}"
    org_id = f"org-{uuid.uuid4().hex[:8]}"
    service_key = "sunday"
    room_id = f"room-{uuid.uuid4().hex[:8]}"

    deepgram_stub = DeepgramStub()
    openai_stub = OpenAIStub()
    backend_a = None
    backend_b = None
    host_client = None
    listener_on_a = None
    listener_b_post_handover = None

    async def teardown():
        for target in (
            listener_b_post_handover,
            listener_on_a,
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
        )

        await deepgram_stub.start()
        await openai_stub.start()

        backend_a = BackendProcess(_config(
            "inst-a", deepgram_stub.endpoint, openai_stub.base_url,
        ))
        backend_a.start()
        backend_a.wait_ready(timeout=45.0)

        backend_b = BackendProcess(_config(
            "inst-b", deepgram_stub.endpoint, openai_stub.base_url,
        ))
        backend_b.start()
        backend_b.wait_ready(timeout=45.0)

        start_room(
            admin_store,
            org_id=org_id, service_key=service_key, room_id=room_id,
        )

        # Host + listener initially on A.
        host_client = HostClient(
            backend_a.ws_url,
            org_id=org_id, room_id=room_id,
            service_key=service_key, church_slug=slug,
            host_token=HOST_TOKEN,
        )
        await host_client.connect()
        await deepgram_stub.wait_for_client_count(1, timeout=15.0)

        listener_on_a = ListenerClient(
            backend_a.ws_url,
            org_id=org_id, room_id=room_id,
            service_key=service_key, church_slug=slug,
        )
        await listener_on_a.connect()
        await listener_on_a.wait_for_frame(
            lambda m: m.get("type") == "JOINED" and m.get("roomId") == room_id,
            timeout=15.0,
        )

        # BASELINE — listener on A receives the marker.
        baseline_marker = f"baseline-{uuid.uuid4().hex[:6]}"
        pre_baseline_translations = openai_stub.request_count
        delivered = await deepgram_stub.send_transcript(
            f"안녕하세요 {baseline_marker}", is_final=True,
        )
        assert delivered >= 1, "Deepgram stub had no client attached"
        await listener_on_a.wait_for_frame(
            _has_marker_predicate(baseline_marker), timeout=20.0,
        )
        assert openai_stub.request_count > pre_baseline_translations, (
            "baseline translation did not hit the OpenAI stub"
        )

        # HANDOVER — the A-side listener disconnects and a fresh
        # listener attaches to B. No listener remains on A.
        await listener_on_a.close()
        listener_on_a = None

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

        # CROSS-INSTANCE DELIVERY ASSERTION — the fresh marker MUST
        # reach the listener on B via Redis fanout. If this fails,
        # either Redis isn't up on one of the backends or the fanout
        # path is broken.
        cross_marker = f"cross-instance-{uuid.uuid4().hex[:6]}"
        pre_cross_translations = openai_stub.request_count
        delivered = await deepgram_stub.send_transcript(
            f"안녕하세요 {cross_marker}", is_final=True,
        )
        assert delivered >= 1, "Deepgram stub had no client attached"

        await listener_b_post_handover.wait_for_frame(
            _has_marker_predicate(cross_marker), timeout=20.0,
        )
        assert openai_stub.request_count > pre_cross_translations, (
            "cross-instance marker did not trigger a translation call"
        )

        # END SERVICE — functional equivalent of the production
        # authenticated HTTP endpoint. admin_store.end_room writes the
        # same Firestore state (status=ended, endedAt, endReason);
        # the reconciler on each instance observes it and performs
        # local cleanup (Track 1 PR-T1-C's Redis-independent safety
        # net).
        end_result = admin_store.end_room(org_id, room_id, reason="host_end")
        assert end_result is not None, "end_room returned no result"

        # WAIT FOR CLEANUP — the reconciler runs on a short interval;
        # give it enough time to observe the ended state and clean
        # up local resources on both instances.
        async def _all_cleaned():
            room_state = read_room(admin_store, org_id=org_id, room_id=room_id)
            firestore_ended = (
                room_state is not None
                and room_state.get("status") == "ended"
            )
            listener_gone = (
                not await listener_b_post_handover.is_open()
                or _received_terminal_status(listener_b_post_handover)
            )
            host_gone = not await host_client.is_open()
            provider_gone = await deepgram_stub.client_count() == 0
            return (firestore_ended, listener_gone, host_gone, provider_gone)

        deadline = asyncio.get_event_loop().time() + 45.0
        while asyncio.get_event_loop().time() < deadline:
            firestore_ended, listener_gone, host_gone, provider_gone = await _all_cleaned()
            if firestore_ended and listener_gone and host_gone and provider_gone:
                break
            await asyncio.sleep(0.5)

        # Read final state for the assertion messages.
        room_state = read_room(admin_store, org_id=org_id, room_id=room_id)
        assert room_state is not None, "room disappeared from Firestore"
        assert room_state.get("status") == "ended", (
            f"Firestore did not flip to ended; got {room_state.get('status')!r}"
        )
        end_reason = room_state.get("endReason")
        assert end_reason == "host_end", (
            f"endReason must be host_end (End Service default); got {end_reason!r}"
        )
        assert not await host_client.is_open(), (
            "host WebSocket on A did not close after End Service"
        )
        listener_terminal_or_closed = (
            not await listener_b_post_handover.is_open()
            or _received_terminal_status(listener_b_post_handover)
        )
        assert listener_terminal_or_closed, (
            "listener on B did not close and did not receive a "
            "terminal frame after End Service"
        )
        assert await deepgram_stub.client_count() == 0, (
            f"Deepgram provider stub still has "
            f"{await deepgram_stub.client_count()} client(s) after "
            "End Service — the host STT session did not release"
        )

    finally:
        await teardown()
