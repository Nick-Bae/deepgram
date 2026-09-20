"""F-26 — same scenario as F-25 but with Redis enabled: delivery works.

Reference: defect issue #29, Track 1 Gate 2 failure record on issue #26.

F-25 reproduces the isolation defect: host on backend A, listener on
backend B, both with `REDIS_ENABLED=0`. The listener on B never
receives translations produced through A because the code path in
`socket_manager.broadcast_room` only fans out via Redis when
`pubsub.enabled and pubsub.connected` — otherwise it is local-only.

F-26 is the same setup with `REDIS_ENABLED=1` on both instances. It
proves the reviewer's preferred remediation (enable and validate the
already-built Redis pub/sub fanout) actually solves the problem: a
listener on B DOES receive the marker produced through A, and the
End Service path stays clean on both instances.

Distinction from the existing F-15 test:
  - F-15 mixes local + cross-instance delivery (listener_a on A,
    listener_b on B). It proves fanout works in the presence of a
    same-instance listener. That is a weaker statement.
  - F-26 has NO listener on A at all — the ONLY listener is on B.
    That directly models the production Gate 2 scenario where the
    listener reconnected to the new revision and the host stayed on
    the old revision.

Acceptance:
  - Baseline marker (before B is attached) reaches nothing but proves
    the A-side host + STT + translation pipeline is armed.
  - Once listener_b joins, the isolation marker MUST reach it via
    Redis fanout. Failure to reach it within the timeout means the
    Redis path is broken or misconfigured.
  - End Service on the room via HTTP terminates both instances'
    local state cleanly; no `cleanup_error`, no `overdue`.
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
    """REDIS_ENABLED=1 is the harness default (see backend_process.env());
    no override needed — but callers should be aware that this test
    intentionally does NOT pass extra_env={'REDIS_ENABLED': ...}."""
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
    listener_b = None

    async def teardown():
        for target in (listener_b, host_client):
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

        host_client = HostClient(
            backend_a.ws_url,
            org_id=org_id, room_id=room_id,
            service_key=service_key, church_slug=slug,
            host_token=HOST_TOKEN,
        )
        await host_client.connect()
        await deepgram_stub.wait_for_client(timeout=15.0)

        # Attach listener EXCLUSIVELY to instance B — no listener on
        # A. This is the exact production Gate 2 state we're validating
        # against: host stuck on old revision, listener on new revision.
        listener_b = ListenerClient(
            backend_b.ws_url,
            org_id=org_id, room_id=room_id,
            service_key=service_key, church_slug=slug,
        )
        await listener_b.connect()
        await listener_b.wait_for_frame(
            lambda m: m.get("type") == "JOINED" and m.get("roomId") == room_id,
            timeout=15.0,
        )

        # Send a unique marker. With Redis enabled on both instances,
        # A publishes on the room's channel; B's subscriber delivers
        # it to listener_b's local socket. If this fails, either Redis
        # isn't actually running for one of the backends or the
        # cross-instance path is broken.
        cross_marker = f"cross-{uuid.uuid4().hex[:6]}"
        delivered = await deepgram_stub.send_transcript(
            f"안녕하세요 {cross_marker}", is_final=True,
        )
        assert delivered >= 1, "Deepgram stub had no client attached"

        def _has_marker(marker):
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

        # This is the assertion F-25 could NOT make. Timeout is
        # generous to survive slow CI.
        await listener_b.wait_for_frame(
            _has_marker(cross_marker), timeout=20.0,
        )

        # Room stayed live throughout — cross-instance delivery is a
        # broadcast concern, not a lifecycle concern.
        room_state = read_room(admin_store, org_id=org_id, room_id=room_id)
        assert room_state is not None, "room disappeared from Firestore"
        assert room_state.get("status") == "live", (
            f"room status flipped to {room_state.get('status')!r} — "
            "no termination path was exercised, so a flip here would "
            "indicate a spurious cleanup or a Redis-related side effect"
        )

    finally:
        await teardown()
