"""F-15 — the two-process acceptance test.

Audit reference: `docs/03-analysis/resource-cleanup-audit.md` F-15.

Scenario: instance A is actively broadcasting; instance B starts. A's
room must stay `status=live`, A's connections stay open, and
translations continue flowing across Redis to a listener directly
attached to B (cross-process delivery proof).

Sequence (matters — earlier drafts got this wrong):

  1. Seed org + service (before any backend). Safe: startup cleanup
     never touches services or orgs.
  2. Start Deepgram stub + OpenAI stub. These are what the backends
     will point their provider clients at.
  3. Start instance A. Wait for readiness.
  4. Create the room via a direct Firestore write. Important: this
     happens AFTER A is ready. If we seeded the room before A, the
     acceptance-gate test (restore old startup cleanup) would end
     the room during A's own startup, before B is in the picture.
  5. Connect a listener and a host to A.
  6. Baseline: push a "baseline" transcript through the Deepgram
     stub and confirm listener_a receives the translated marker.
     This proves the full A-side pipeline (STT → translation →
     broadcast → listener) works before B starts, so a later
     failure is unambiguously attributable to B's startup.
  7. Start instance B. THIS is the F-15 trigger.
  8. Assert:
     a. A's listener sees no `STATUS(ended)` frame within 5s.
     b. A's host WS is still open.
     c. Firestore room stays `status=live`.
  9. Cross-process: connect a listener to B, push a "cross-process"
     transcript, both listeners see the same marker in a translation
     frame — proves A → Redis → B delivery.
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


def test_f15_starting_instance_b_does_not_disrupt_instance_a(admin_store):
    """Sync wrapper — avoids the pytest-asyncio plugin dependency."""
    asyncio.run(_run_f15(admin_store))


async def _run_f15(admin_store):
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
    listener_b = None

    async def teardown():
        """Robust teardown — every step runs even if others raise, and
        both backends' logs are captured to the pytest output for
        post-mortem regardless of which step failed."""
        # Close clients first (they hold WS connections into the
        # backends); order matters for clean shutdown.
        for target in (listener_b, listener_a, host_client):
            if target is not None:
                try:
                    await target.close()
                except BaseException:
                    pass
        # Stop backends before stubs so their subprocess exits before
        # the stubs' event loops close.
        for proc in (backend_b, backend_a):
            if proc is not None:
                try:
                    proc.stop()
                except BaseException:
                    pass
        # Stop stubs.
        for stub in (deepgram_stub, openai_stub):
            if stub is not None:
                try:
                    await stub.stop()
                except BaseException:
                    pass
        # ALWAYS dump both backends' logs to stdout so pytest's
        # captured-output section shows them on any failure.
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
        # (1) Seed org + service before either backend starts.
        seed_org_and_service(
            admin_store,
            org_id=org_id,
            slug=slug,
            service_key=service_key,
            host_token=HOST_TOKEN,
        )

        # (2) Start Deepgram + OpenAI stubs. Backends point at these.
        await deepgram_stub.start()
        await openai_stub.start()

        # (3) Start instance A. Wait until it accepts HTTP.
        backend_a = BackendProcess(
            _config("inst-a", deepgram_stub.endpoint, openai_stub.base_url)
        )
        backend_a.start()
        backend_a.wait_ready(timeout=45.0)

        # (4) NOW create the room. If we seeded it before A, the
        # acceptance-gate variant (old startup cleanup restored)
        # would end the room during A's startup and mask B's effect.
        start_room(
            admin_store,
            org_id=org_id,
            service_key=service_key,
            room_id=room_id,
        )

        # (5) Clients on A.
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

        host_client = HostClient(
            backend_a.ws_url,
            org_id=org_id,
            room_id=room_id,
            service_key=service_key,
            church_slug=slug,
            host_token=HOST_TOKEN,
        )
        await host_client.connect()
        await deepgram_stub.wait_for_client(timeout=15.0)

        # (6) Baseline: A's full pipeline works before B starts.
        # Every future failure is unambiguously attributable to B.
        baseline_marker = f"baseline-{uuid.uuid4().hex[:6]}"
        delivered = await deepgram_stub.send_transcript(
            f"안녕하세요 {baseline_marker}", is_final=True
        )
        assert delivered >= 1, "Deepgram stub had no client attached"

        def _has_marker(marker):
            def _pred(msg):
                expected = f"[stub-translated] {marker}"
                # Require the OpenAI stub's translated prefix. Matching the
                # Korean source marker would let a translation-bypass bug pass
                # this integration gate.
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
        assert openai_stub.request_count >= 1, (
            "baseline marker arrived without an OpenAI-stub request"
        )

        # (7) Start instance B — the F-15 trigger.
        backend_b = BackendProcess(
            _config("inst-b", deepgram_stub.endpoint, openai_stub.base_url)
        )
        backend_b.start()
        backend_b.wait_ready(timeout=45.0)

        # Give B's Redis subscriber time to establish; also gives the
        # buggy old startup path time to have fired if it were going to.
        await asyncio.sleep(2.0)

        # (8a) No terminal STATUS on A's listener.
        await listener_a.assert_no_status_ended(within=5.0)

        # (8b) A's host WS still open.
        assert await host_client.is_open(), (
            "host WS on instance A closed unexpectedly after B started — "
            "F-15 regression"
        )

        # (8c) Firestore room still live.
        room_state = read_room(admin_store, org_id=org_id, room_id=room_id)
        assert room_state is not None, "room disappeared from Firestore"
        assert room_state.get("status") == "live", (
            f"room status flipped to {room_state.get('status')!r} after "
            f"instance B started — F-15 regression"
        )

        # (9) Cross-process delivery through Redis.
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
            timeout=15.0,
        )

        cross_marker = f"cross-process-{uuid.uuid4().hex[:6]}"
        delivered = await deepgram_stub.send_transcript(
            f"안녕하세요 {cross_marker}", is_final=True
        )
        assert delivered >= 1, "Deepgram stub had no client attached"

        # Listener on A receives it (same-instance).
        await listener_a.wait_for_frame(_has_marker(cross_marker), timeout=20.0)
        # Listener on B receives it via Redis fanout.
        await listener_b.wait_for_frame(_has_marker(cross_marker), timeout=20.0)
        assert openai_stub.request_count >= 2, (
            "cross-process marker arrived without a second OpenAI-stub request"
        )

    finally:
        await teardown()
