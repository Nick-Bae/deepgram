"""F-25 — reproduce Track 1 Gate 2's cross-revision isolation defect.

Reference: defect issue #29, Track 1 Gate 2 failure record on issue #26.

The observed failure pattern from the 2026-09-20 Gate 2 attempt:

  1. The deploy replaced the serving revision, but the OLD revision
     stayed alive because Cloud Run keeps instances up while WebSocket
     requests are still in flight.
  2. The host's producer WebSocket therefore continued to talk to the
     OLD revision.
  3. The listener's transient-close reconnect (after Uvicorn's 1012 on
     the old revision) landed on the NEW revision — that revision
     served 100% of traffic once the deploy completed.
  4. Because `REDIS_ENABLED=0`, `ConnectionManager.broadcast_room` on
     the OLD revision delivered only to its own local sockets. The
     translation never crossed to the NEW revision, so the listener
     stayed black.

F-25 models this end-state directly WITHOUT any SIGTERM: two backend
instances are both healthy, the host is attached to A, the listener is
attached to B, both instances run with `REDIS_ENABLED=0`. A transcript
pushed through A must fail to reach B — because it *cannot* reach B in
that configuration. This is a "prove the bug is reproducible in the
harness" test, not a regression test for a fix; a companion F-26 will
prove the same setup succeeds when `REDIS_ENABLED=1`.

Acceptance:
  - No SIGTERM, no revision transition — this test intentionally isolates
    the isolation problem from all shutdown-race concerns.
  - Baseline via listener-on-A confirms the A pipeline itself works
    (rules out any confounding pipeline failure).
  - Listener-on-B does NOT receive the same marker within a bounded
    timeout. Timeout is deliberately generous (12 s) so a slow CI does
    not mask a real recovery.
  - Room stays live in Firestore throughout; the isolation is a delivery
    problem, not a termination problem.
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
    """Build a BackendConfig with an explicit REDIS_ENABLED override
    (the harness defaults to '1'; `extra_env` runs last in
    BackendProcess.env(), so this actually takes effect)."""
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


def test_f25_cross_instance_isolation_with_redis_disabled(admin_store):
    """Sync wrapper — mirrors the F-15 / F-9 structure."""
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
    listener_a = None
    listener_b = None

    async def teardown():
        for target in (listener_b, listener_a, host_client):
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
            org_id=org_id,
            slug=slug,
            service_key=service_key,
            host_token=HOST_TOKEN,
        )

        await deepgram_stub.start()
        await openai_stub.start()

        # Both backends run with REDIS_ENABLED=0 — this is the
        # production Track 1 configuration on 2026-09-20.
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

        # Room seeded after both backends are ready. Startup safety
        # (PR-T1-A) is not the subject of this test — F-15 covers it.
        start_room(
            admin_store,
            org_id=org_id,
            service_key=service_key,
            room_id=room_id,
        )

        host_client = HostClient(
            backend_a.ws_url,
            org_id=org_id, room_id=room_id,
            service_key=service_key, church_slug=slug,
            host_token=HOST_TOKEN,
        )
        await host_client.connect()
        await deepgram_stub.wait_for_client(timeout=15.0)

        # BASELINE — a listener on the SAME instance as the host must
        # receive translations. If this fails, the A pipeline itself is
        # broken and the assertion below would falsely "pass" for the
        # wrong reason. Fail-loudly guard.
        listener_a = ListenerClient(
            backend_a.ws_url,
            org_id=org_id, room_id=room_id,
            service_key=service_key, church_slug=slug,
        )
        await listener_a.connect()
        await listener_a.wait_for_frame(
            lambda m: m.get("type") == "JOINED" and m.get("roomId") == room_id,
            timeout=15.0,
        )

        baseline_marker = f"baseline-{uuid.uuid4().hex[:6]}"
        delivered = await deepgram_stub.send_transcript(
            f"안녕하세요 {baseline_marker}", is_final=True,
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

        await listener_a.wait_for_frame(
            _has_marker(baseline_marker), timeout=20.0,
        )

        # Now attach a listener to backend B — NOT A. This is the
        # exact production configuration during the Gate 2 failure:
        # producer stuck on the old revision, listener reconnected to
        # the new revision.
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

        # Send a fresh marker. With REDIS_ENABLED=0, the A instance
        # only delivers locally — listener_a will receive it, but
        # listener_b MUST NOT.
        isolation_marker = f"isolation-{uuid.uuid4().hex[:6]}"
        delivered = await deepgram_stub.send_transcript(
            f"안녕하세요 {isolation_marker}", is_final=True,
        )
        assert delivered >= 1, "Deepgram stub had no client attached"

        # A must still receive (proves the pipeline is otherwise fine).
        await listener_a.wait_for_frame(
            _has_marker(isolation_marker), timeout=20.0,
        )

        # B must NOT receive the same marker within a generous window.
        # If this assertion trips a TimeoutError, the isolation defect
        # is reproduced and F-25 passes.
        try:
            await listener_b.wait_for_frame(
                _has_marker(isolation_marker), timeout=12.0,
            )
            raise AssertionError(
                "listener on instance B received the marker despite "
                "REDIS_ENABLED=0 on both instances — cross-instance "
                "fanout should be impossible in this configuration. "
                "This means either (a) Redis was actually running "
                "somewhere the tests didn't expect, (b) the code path "
                "in socket_manager changed, or (c) the harness leaked "
                "a channel between processes. Investigate before "
                "trusting F-26."
            )
        except asyncio.TimeoutError:
            pass  # This is the expected defect signature.

        # Room stays live throughout — this is an isolation defect,
        # not a termination defect.
        room_state = read_room(admin_store, org_id=org_id, room_id=room_id)
        assert room_state is not None, "room disappeared from Firestore"
        assert room_state.get("status") == "live", (
            f"room status flipped to {room_state.get('status')!r} — "
            "F-25 does not exercise any termination path, so a flip "
            "here indicates a spurious cleanup or an unrelated bug"
        )

    finally:
        await teardown()
