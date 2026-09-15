"""F-15 — the two-process acceptance test.

Audit reference: `docs/03-analysis/resource-cleanup-audit.md` F-15.

Scenario: instance A is actively broadcasting; instance B starts. A's
room must stay `status=live`, A's connections stay open, and
translations continue flowing across Redis to a listener directly
attached to B (cross-process delivery proof).

Acceptance gate: this test fails against the pre-PR-T1-A backend
(where `_cleanup_live_rooms_on_startup` unconditionally ended every
live Firestore room at startup), and passes against current `main`.
The manual bisection procedure — restore
`_cleanup_live_rooms_on_startup` and its schedule call, rerun this
test, observe failure — is documented in
`docs/01-plan/features/resource-cleanup-track-1.plan.md` PR-T1-A.
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


HOST_TOKEN = "harness-host-token"


def _config(instance_id: str, deepgram_endpoint: str) -> BackendConfig:
    return BackendConfig(
        instance_id=instance_id,
        redis_host=REDIS_HOST,
        redis_port=REDIS_PORT,
        firestore_emulator_host=FIRESTORE_EMULATOR_HOST,
        gcp_project=GCP_PROJECT,
        deepgram_endpoint=deepgram_endpoint,
        host_api_token=HOST_TOKEN,
    )


def test_f15_starting_instance_b_does_not_disrupt_instance_a(admin_store):
    """Sync wrapper around the async body — avoids the pytest-asyncio
    plugin dependency."""
    asyncio.run(_run_f15(admin_store))


async def _run_f15(admin_store):
    """The full F-15 acceptance test.

    Steps:
      1. Bring up a fresh Deepgram stub.
      2. Seed a unique org + service in the Firestore emulator (fresh
         per run — no shared state between tests).
      3. Start instance A. Wait until it accepts HTTP.
      4. Start a room on A (via direct Firestore write — the
         production HTTP path requires Firebase auth we don't wire
         up here; this is why the store's authorize_host respects
         HOST_API_TOKEN so the host WS can attach).
      5. Connect a listener WS to A directly.
      6. Connect a host WS to A directly. Backend attempts a
         Deepgram-shaped upstream connection and gets our stub.
      7. Wait for the Deepgram stub to see one client — confirms A's
         STT session is live.
      8. Start instance B. Wait until it accepts HTTP.
      9. Assert (F-15 core):
         a. A's listener WS receives no STATUS(ended) frame within a
            bounded window after B is up.
         b. A's host WS is still open.
         c. Firestore room state is still `status=live`.
     10. Prove cross-process delivery through Redis:
         a. Attach a second listener directly to B for the same room.
         b. Push a scripted transcript through the Deepgram stub.
         c. Assert both listeners (on A and on B) receive a
            translation frame carrying the same transcript text —
            proves the message went from A → Redis → B → listener
            over the real broadcast_room path.
     11. Tear down: close clients, stop both backends, stop stub.
    """
    # Unique per-test IDs so runs against a re-used emulator don't
    # interfere.
    slug = f"church-{uuid.uuid4().hex[:8]}"
    org_id = f"org-{uuid.uuid4().hex[:8]}"
    service_key = "sunday"
    room_id = f"room-{uuid.uuid4().hex[:8]}"

    stub = DeepgramStub()
    await stub.start()

    backend_a = None
    backend_b = None
    host_client = None
    listener_a = None
    listener_b = None
    try:
        # Seed org + service before A boots so its startup doesn't race
        # against the write.
        seed_org_and_service(
            admin_store,
            org_id=org_id,
            slug=slug,
            service_key=service_key,
            host_token=HOST_TOKEN,
        )
        start_room(
            admin_store,
            org_id=org_id,
            service_key=service_key,
            room_id=room_id,
        )

        # (3) Start instance A.
        backend_a = BackendProcess(_config("inst-a", stub.endpoint))
        backend_a.start()
        backend_a.wait_ready()

        # (5) Listener on A.
        listener_a = ListenerClient(
            backend_a.ws_url,
            org_id=org_id,
            room_id=room_id,
            service_key=service_key,
            church_slug=slug,
        )
        await listener_a.connect()
        # Wait for the JOINED confirmation frame — proves the listener
        # actually registered against room (org_id, room_id) on A.
        await listener_a.wait_for_frame(
            lambda m: m.get("type") == "JOINED" and m.get("roomId") == room_id,
            timeout=10.0,
        )

        # (6) Host on A.
        host_client = HostClient(
            backend_a.ws_url,
            org_id=org_id,
            room_id=room_id,
            service_key=service_key,
            church_slug=slug,
            host_token=HOST_TOKEN,
        )
        await host_client.connect()
        await stub.wait_for_client(timeout=15.0)

        # (8) Start instance B. THIS is the F-15 trigger — before
        # PR-T1-A, B's `_cleanup_live_rooms_on_startup` would end
        # A's live room, closing listener_a's WS with a terminal
        # STATUS frame.
        backend_b = BackendProcess(_config("inst-b", stub.endpoint))
        backend_b.start()
        backend_b.wait_ready()

        # Give the Redis subscriber on B a moment to establish; also
        # gives the buggy old startup path time to have already fired
        # if it were going to.
        await asyncio.sleep(2.0)

        # (9a) No terminal STATUS on A's listener.
        await listener_a.assert_no_status_ended(within=5.0)

        # (9b) A's host WS still open.
        assert await host_client.is_open(), (
            "host WS on instance A closed unexpectedly after B started — "
            "F-15 regression (see backend_a.logs() and backend_b.logs())"
        )

        # (9c) Firestore room still live.
        room_state = read_room(admin_store, org_id=org_id, room_id=room_id)
        assert room_state is not None, "room disappeared from Firestore"
        assert room_state.get("status") == "live", (
            f"room status flipped to {room_state.get('status')!r} after "
            f"instance B started — F-15 regression"
        )

        # (10) Cross-process delivery through Redis.
        listener_b = ListenerClient(
            backend_b.ws_url,
            org_id=org_id,
            room_id=room_id,
            service_key=service_key,
            church_slug=slug,
        )
        await listener_b.connect()
        await listener_b.wait_for_frame(
            lambda m: m.get("type") == "JOINED" and m.get("roomId") == room_id,
            timeout=10.0,
        )

        marker = f"cross-process-{uuid.uuid4().hex[:6]}"
        # Push a scripted transcript through the stub. Backend on A
        # sees this on its Deepgram WS, runs it through the translation
        # pipeline, and broadcasts. Redis fanout carries the result
        # to backend B, which forwards to listener_b.
        delivered = await stub.send_transcript(f"안녕하세요 {marker}", is_final=True)
        assert delivered >= 1, "Deepgram stub had no client to send to"

        def _has_marker(msg):
            payload = msg.get("payload") or msg.get("text") or ""
            if isinstance(payload, str) and marker in payload:
                return True
            meta = msg.get("meta") or {}
            src = meta.get("source_text") or ""
            return marker in src

        # Listener attached to A receives the translation.
        await listener_a.wait_for_frame(_has_marker, timeout=15.0)
        # Listener attached to B receives the SAME translation via
        # Redis fanout — proves cross-process delivery.
        await listener_b.wait_for_frame(_has_marker, timeout=15.0)

    finally:
        if listener_b is not None:
            await listener_b.close()
        if listener_a is not None:
            await listener_a.close()
        if host_client is not None:
            await host_client.close()
        if backend_b is not None:
            backend_b.stop()
        if backend_a is not None:
            backend_a.stop()
        await stub.stop()

        # Attach logs to the test output on failure. Pytest captures
        # stdout; use print rather than logger so it always ends up
        # in the failure summary.
        for name, proc in [
            ("backend_a", backend_a),
            ("backend_b", backend_b),
        ]:
            if proc is not None:
                logs = proc.logs()
                if logs:
                    print(f"===== {name} ({proc.config.instance_id}) logs =====")
                    print(logs)
                    print(f"===== end {name} logs =====")
