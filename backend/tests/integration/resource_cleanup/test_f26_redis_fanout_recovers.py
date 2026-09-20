"""F-26 — same host-A → handover-to-B scenario as F-25 but with Redis
enabled: cross-instance delivery works AND real End Service releases
resources on every instance that touched the room.

Reference: defect issue #29, Track 1 Gate 2 failure record on issue #26.

F-25 tests the leading hypothesis by reproducing the observed
isolation with `REDIS_ENABLED=0`. F-26 flips the switch (Redis enabled
on both instances) and additionally exercises the terminal-broadcast
path end-to-end.

Two things F-26 does differently from an earlier draft the reviewer
rejected:

  1. **The room reconciler is explicitly DISABLED on both instances**
     (`ROOM_RECONCILER_ENABLED=0` — matches the production default).
     The reconciler is Track 1's fallback safety net; enabling it in
     this test would let it mask a missing HTTP or Redis terminal
     broadcast. F-26 tests the primary path exclusively.

  2. **End Service is invoked through the real HTTP endpoint on
     backend B**, using authentication substitution isolated to the
     harness. See
     `backend/tests/integration/resource_cleanup/harness/e2e_uvicorn_bootstrap.py` —
     that module launches uvicorn against the real `app.main:app`
     and monkey-patches only `firebase_auth.verify_id_token_value`
     in-process before the first request. Production auth code is
     untouched.

Assertions after End Service:

  - Listener on B receives a terminal `STATUS(ended)` frame AND its
    WebSocket closes.
  - Host WebSocket on A closes (the terminal broadcast from B must
    have reached A via Redis fanout — the only path available with
    the reconciler disabled).
  - Deepgram provider stub's `client_count` returns to 0 (the host's
    STT session released).
  - Redis room-channel subscriber count returns to 0 (both backends
    unsubscribed from the room after cleanup).
  - Both backend processes remain alive throughout — F-26 tests
    cleanup, not shutdown.

The room's Firestore document reaches `status=ended` with the
whitelisted `endReason=host_end`.

BEFORE the authorised End Service, F-26 also verifies that
misconfigured auth is rejected without touching room state:

  - Missing Authorization header → 401.
  - Wrong bearer token → 401.
  - Right bearer whose stub uid has no `members/<uid>` document → 403.

None of those calls may flip Firestore to `status=ended`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import uuid

import httpx
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
# Two bearer tokens, each mapped to a different stub uid. HOST_BEARER
# maps to a uid whose org membership will be seeded (role=host).
# NON_MEMBER_BEARER maps to a uid that never gets a membership record.
HOST_BEARER = "harness-e2e-host-bearer"
NON_MEMBER_BEARER = "harness-e2e-outsider-bearer"
HOST_UID = "e2e-host-uid"
NON_MEMBER_UID = "e2e-outsider-uid"
STUB_MAPPING = {HOST_BEARER: HOST_UID, NON_MEMBER_BEARER: NON_MEMBER_UID}

REDIS_CHANNEL_PREFIX = "worshiptranslate"
# Bounded socket timeouts so the polling loop's outer deadline is
# actually enforceable — reviewer's second remaining blocker.
REDIS_SOCKET_TIMEOUT_SEC = 2.0
FIRESTORE_READ_TIMEOUT_SEC = 3.0


def _config(
    instance_id: str,
    deepgram_endpoint: str,
    openai_base_url: str,
) -> BackendConfig:
    """REDIS_ENABLED=1 is the harness default. Reconciler is
    explicitly disabled — the reviewer requires the terminal
    broadcast path to stand on its own, without the reconciler's
    fallback cleanup masking a missing broadcast."""
    return BackendConfig(
        instance_id=instance_id,
        redis_host=REDIS_HOST,
        redis_port=REDIS_PORT,
        firestore_emulator_host=FIRESTORE_EMULATOR_HOST,
        gcp_project=GCP_PROJECT,
        deepgram_endpoint=deepgram_endpoint,
        openai_base_url=openai_base_url,
        host_api_token=HOST_TOKEN,
        extra_env={"ROOM_RECONCILER_ENABLED": "0"},
    )


def _room_channel(org_id: str, room_id: str) -> str:
    return f"{REDIS_CHANNEL_PREFIX}:org:{org_id}:room:{room_id}"


def _numsub_blocking(channel: str) -> int:
    """Synchronous NUMSUB call — must run inside asyncio.to_thread
    so the async polling loop's deadline check is not blocked by a
    stalled Redis socket."""
    r = redis_sync.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SEC,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SEC,
    )
    try:
        result = r.pubsub_numsub(channel)
        for name, count in result:
            if name == channel:
                return int(count)
        return 0
    finally:
        with contextlib.suppress(Exception):
            r.close()


async def _numsub(channel: str) -> int:
    return await asyncio.to_thread(_numsub_blocking, channel)


async def _read_room_bounded(admin_store, *, org_id, room_id):
    """Read the room off the event loop with an explicit per-RPC
    timeout AND a bounded outer await.

    The per-RPC `timeout` is what actually bounds the gRPC call
    (`timeout` is a Google Cloud Firestore kwarg, forwarded to the
    underlying transport). The outer `asyncio.wait_for` is a
    belt-and-braces guard for the exceptionally rare case where
    the worker thread hangs INSIDE the client library on something
    other than the RPC socket — in that case we still cancel the
    await so the polling loop's deadline can fire.
    """
    return await asyncio.wait_for(
        asyncio.to_thread(
            read_room,
            admin_store,
            org_id=org_id,
            room_id=room_id,
            timeout=FIRESTORE_READ_TIMEOUT_SEC,
        ),
        # slightly larger than the RPC timeout so the RPC is what
        # bounds latency in the normal case.
        timeout=FIRESTORE_READ_TIMEOUT_SEC + 1.0,
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


def _has_terminal_status(msg) -> bool:
    if not isinstance(msg, dict):
        return False
    if msg.get("type") == "STATUS" and msg.get("roomStatus") == "ended":
        return True
    if msg.get("reason") == "room_ended":
        return True
    if msg.get("code") == 4001:
        return True
    return False


def _received_terminal_status(listener: ListenerClient) -> bool:
    for msg in listener.received:
        if _has_terminal_status(msg):
            return True
    return False


def test_f26_redis_fanout_recovers_cross_instance_delivery(admin_store, monkeypatch):
    asyncio.run(_run_f26(admin_store, monkeypatch))


async def _run_f26(admin_store, monkeypatch):
    # Provide the mapping to backend subprocesses via env. The
    # bootstrap module reads E2E_STUB_AUTH_MAPPING and installs the
    # monkey-patch inside the child process. Neither the mapping nor
    # this env var is recognised by production code.
    monkeypatch.setenv("E2E_STUB_AUTH_MAPPING", json.dumps(STUB_MAPPING))

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
        # Seed the org + service AND a `members/{uid}=host` document
        # for HOST_UID only. NON_MEMBER_UID is intentionally NOT
        # given a membership record.
        seed_org_and_service(
            admin_store,
            org_id=org_id, slug=slug,
            service_key=service_key, host_token=HOST_TOKEN,
            e2e_host_uid=HOST_UID,
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

        # BASELINE
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

        # HANDOVER
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

        # CROSS-INSTANCE DELIVERY
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

        # PRE-CLEANUP INVARIANTS
        assert backend_a.proc is not None and backend_a.proc.poll() is None
        assert backend_b.proc is not None and backend_b.proc.poll() is None
        channel = _room_channel(org_id, room_id)

        async def _wait_for_numsub(expected: int, timeout: float = 10.0) -> int:
            deadline = asyncio.get_event_loop().time() + timeout
            observed = await _numsub(channel)
            while asyncio.get_event_loop().time() < deadline:
                observed = await _numsub(channel)
                if observed == expected:
                    return observed
                await asyncio.sleep(0.2)
            return observed

        pre_sub = await _wait_for_numsub(2, timeout=10.0)
        assert pre_sub == 2, (
            f"expected NUMSUB=2 on {channel!r} before End Service; got {pre_sub}"
        )
        assert await deepgram_stub.client_count() == 1, (
            f"expected deepgram_stub client_count=1 pre-End Service; "
            f"got {await deepgram_stub.client_count()}"
        )

        # ------- NEGATIVE AUTH TESTS — must NOT end the room. -------
        end_url = f"{backend_b.base_url}/api/org/{org_id}/room/{room_id}/end"
        end_body = {"reason": "host_end"}

        async with httpx.AsyncClient(timeout=15.0) as http:
            resp_missing = await http.post(end_url, json=end_body)
            resp_wrong = await http.post(
                end_url,
                headers={"Authorization": "Bearer this-token-is-not-in-the-mapping"},
                json=end_body,
            )
            resp_nonmember = await http.post(
                end_url,
                headers={"Authorization": f"Bearer {NON_MEMBER_BEARER}"},
                json=end_body,
            )
        assert resp_missing.status_code == 401, (
            f"missing Authorization must return 401; got {resp_missing.status_code}"
        )
        assert resp_wrong.status_code == 401, (
            f"unknown bearer token must return 401; got {resp_wrong.status_code}"
        )
        assert resp_nonmember.status_code == 403, (
            f"authenticated but non-member uid must return 403; "
            f"got {resp_nonmember.status_code} body={resp_nonmember.text[:200]}"
        )
        # None of the rejected calls may have ended the room.
        state_after_rejections = await _read_room_bounded(
            admin_store, org_id=org_id, room_id=room_id,
        )
        assert (
            state_after_rejections is not None
            and state_after_rejections.get("status") == "live"
        ), (
            f"rejected auth calls somehow ended the room; got "
            f"status={state_after_rejections and state_after_rejections.get('status')!r}"
        )

        # ------- AUTHORISED END SERVICE — must succeed + cleanup. -------
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(
                end_url,
                headers={"Authorization": f"Bearer {HOST_BEARER}"},
                json=end_body,
            )
        assert resp.status_code == 200, (
            f"End Service HTTP returned {resp.status_code}: {resp.text[:200]}"
        )

        # WAIT + POLL — bounded Redis + Firestore reads. The outer
        # deadline is enforced regardless of individual read latency.
        deadline = asyncio.get_event_loop().time() + 30.0
        firestore_ended = listener_got_terminal = listener_closed = False
        host_closed = provider_zero = redis_zero = False
        while asyncio.get_event_loop().time() < deadline:
            try:
                room_state = await _read_room_bounded(
                    admin_store, org_id=org_id, room_id=room_id,
                )
            except asyncio.TimeoutError:
                room_state = None
            firestore_ended = (
                room_state is not None
                and room_state.get("status") == "ended"
            )
            listener_got_terminal = _received_terminal_status(listener_b_post_handover)
            listener_closed = not await listener_b_post_handover.is_open()
            host_closed = not await host_client.is_open()
            provider_zero = await deepgram_stub.client_count() == 0
            try:
                redis_zero = (await _numsub(channel)) == 0
            except asyncio.TimeoutError:
                redis_zero = False
            if (
                firestore_ended
                and listener_got_terminal
                and listener_closed
                and host_closed
                and provider_zero
                and redis_zero
            ):
                break
            await asyncio.sleep(0.25)

        room_state = await _read_room_bounded(
            admin_store, org_id=org_id, room_id=room_id,
        )
        assert room_state is not None, "room disappeared from Firestore"
        assert room_state.get("status") == "ended", (
            f"Firestore status did not become 'ended'; got {room_state.get('status')!r}"
        )
        assert room_state.get("endReason") == "host_end", (
            f"endReason must be 'host_end' (End Service default); got {room_state.get('endReason')!r}"
        )
        assert _received_terminal_status(listener_b_post_handover), (
            "listener_b never received a terminal STATUS/room_ended/4001 "
            "frame after End Service — the terminal broadcast did not "
            "reach B"
        )
        assert not await listener_b_post_handover.is_open(), (
            "listener_b received a terminal frame but its WebSocket did "
            "not close — cleanup incomplete"
        )
        assert not await host_client.is_open(), (
            "host WebSocket on A did not close after End Service — "
            "terminal broadcast did not cross the Redis boundary"
        )
        final_provider = await deepgram_stub.client_count()
        assert final_provider == 0, (
            f"Deepgram provider stub still reports client_count="
            f"{final_provider}"
        )
        final_sub = await _numsub(channel)
        assert final_sub == 0, (
            f"Redis PUBSUB NUMSUB for {channel!r} is still {final_sub}"
        )
        assert backend_a.proc is not None and backend_a.proc.poll() is None, (
            "backend A exited during End Service cleanup"
        )
        assert backend_b.proc is not None and backend_b.proc.poll() is None, (
            "backend B exited during End Service cleanup"
        )

    finally:
        await teardown()
