"""F-8 and F-24 real-Redis acceptance tests for PR-T1-C."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import time
import uuid

import redis

from .conftest import FIRESTORE_EMULATOR_HOST, GCP_PROJECT, REDIS_HOST, REDIS_PORT
from .harness.backend_process import BackendConfig, BackendProcess
from .harness.clients import ListenerClient
from .harness.firestore_seed import seed_org_and_service, start_room


HOST_TOKEN = "harness-host-token"
CHANNEL_PREFIX = "worshiptranslate"


def _backend_config(
    instance_id: str,
    redis_port: int,
    *,
    interval: int = 5,
    reconciler_enabled: bool = True,
) -> BackendConfig:
    return BackendConfig(
        instance_id=instance_id,
        redis_host="127.0.0.1",
        redis_port=redis_port,
        firestore_emulator_host=FIRESTORE_EMULATOR_HOST,
        gcp_project=GCP_PROJECT,
        host_api_token=HOST_TOKEN,
        extra_env={
            "ROOM_RECONCILER_ENABLED": "1" if reconciler_enabled else "0",
            "ROOM_RECONCILER_INTERVAL_SEC": str(interval),
            "REDIS_COMMAND_TIMEOUT_SEC": "1",
            "REDIS_CONNECT_TIMEOUT_SEC": "1",
        },
    )


def _channel(org_id: str, room_id: str) -> str:
    return f"{CHANNEL_PREFIX}:org:{org_id}:room:{room_id}"


def _redis_client():
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def _publish_terminal(org_id: str, room_id: str, reason: str = "host_end") -> int:
    envelope = {
        "v": 1,
        "seq": 1,
        "publisher": "harness-terminal-publisher",
        "ts": datetime.now(timezone.utc).isoformat(),
        "message": {
            "type": "STATUS",
            "orgId": org_id,
            "roomId": room_id,
            "roomStatus": "ended",
            "viewerCount": 0,
            "reason": reason,
        },
    }
    return int(_redis_client().publish(_channel(org_id, room_id), json.dumps(envelope)))


def _numsub(*channels: str) -> dict[str, int]:
    rows = _redis_client().pubsub_numsub(*channels)
    return {str(channel): int(count) for channel, count in rows}


async def _wait_until(predicate, *, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await asyncio.to_thread(predicate):
            return
        await asyncio.sleep(0.2)
    raise AssertionError(message)


async def _close_all(*targets) -> None:
    for target in targets:
        if target is None:
            continue
        try:
            await target.close()
        except BaseException:
            pass


def _stop_and_dump(name: str, proc: BackendProcess | None) -> None:
    if proc is None:
        return
    try:
        proc.stop()
    finally:
        logs = proc.logs()
        if logs:
            print(f"===== {name} ({proc.config.instance_id}) logs =====")
            print(logs)
            print(f"===== end {name} logs =====")


def test_f8_missed_terminal_broadcast_is_repaired(admin_store, redis_outage_proxies):
    asyncio.run(_run_f8(admin_store, redis_outage_proxies))


async def _run_f8(admin_store, redis_outage_proxies):
    _, proxy_b = redis_outage_proxies
    org_id = f"org-f8-{uuid.uuid4().hex[:8]}"
    slug = f"church-f8-{uuid.uuid4().hex[:8]}"
    service_key = "sunday"
    room_id = f"room-f8-{uuid.uuid4().hex[:8]}"
    backend_a = backend_b = None
    listener_a = listener_b = None
    try:
        seed_org_and_service(
            admin_store,
            org_id=org_id,
            slug=slug,
            service_key=service_key,
            host_token=HOST_TOKEN,
        )
        backend_a = BackendProcess(
            _backend_config(
                "f8-a",
                REDIS_PORT,
                interval=8,
                reconciler_enabled=False,
            )
        )
        backend_a.start()
        backend_a.wait_ready(timeout=45)
        start_room(
            admin_store,
            org_id=org_id,
            service_key=service_key,
            room_id=room_id,
        )
        backend_b = BackendProcess(
            _backend_config("f8-b", proxy_b.redis_port, interval=8)
        )
        backend_b.start()
        backend_b.wait_ready(timeout=45)

        listener_a = ListenerClient(
            backend_a.ws_url,
            org_id=org_id,
            room_id=room_id,
            service_key=service_key,
            church_slug=slug,
        )
        listener_b = ListenerClient(
            backend_b.ws_url,
            org_id=org_id,
            room_id=room_id,
            service_key=service_key,
            church_slug=slug,
        )
        await listener_a.connect()
        await listener_b.connect()
        for listener in (listener_a, listener_b):
            await listener.wait_for_frame(
                lambda m: m.get("type") == "JOINED",
                timeout=15,
            )

        # Drop only B's Redis transport. A still receives the terminal fast
        # path, while B can recover only by observing Firestore on a later
        # reconciler tick.
        await asyncio.to_thread(proxy_b.set_enabled, False)
        await asyncio.sleep(1.5)
        await asyncio.to_thread(
            admin_store.end_room,
            org_id,
            room_id,
            reason="host_end",
        )
        subscribers = await asyncio.to_thread(_publish_terminal, org_id, room_id)
        assert subscribers >= 1, "terminal event had no Redis subscriber on instance A"

        await listener_a.wait_for_status_ended(timeout=4)
        await listener_b.assert_no_status_ended(within=2)
        await listener_b.wait_for_status_ended(timeout=12)
        await listener_b.wait_closed(timeout=4)
    finally:
        try:
            await asyncio.to_thread(proxy_b.set_enabled, True)
        except Exception:
            pass
        await _close_all(listener_b, listener_a)
        _stop_and_dump("backend_b", backend_b)
        _stop_and_dump("backend_a", backend_a)


def test_f24_redis_outage_cleanup_and_selective_resubscribe(
    admin_store,
    redis_outage_proxies,
):
    asyncio.run(_run_f24(admin_store, redis_outage_proxies))


async def _run_f24(admin_store, redis_outage_proxies):
    proxy, _ = redis_outage_proxies
    org_id = f"org-f24-{uuid.uuid4().hex[:8]}"
    slug = f"church-f24-{uuid.uuid4().hex[:8]}"
    ended_service = "ended-service"
    live_service = "live-service"
    ended_room = f"room-ended-{uuid.uuid4().hex[:8]}"
    live_room = f"room-live-{uuid.uuid4().hex[:8]}"
    backend = None
    ended_listener = live_listener = None
    try:
        seed_org_and_service(
            admin_store,
            org_id=org_id,
            slug=slug,
            service_key=ended_service,
            host_token=HOST_TOKEN,
        )
        seed_org_and_service(
            admin_store,
            org_id=org_id,
            slug=slug,
            service_key=live_service,
            host_token=HOST_TOKEN,
        )
        start_room(
            admin_store,
            org_id=org_id,
            service_key=ended_service,
            room_id=ended_room,
        )
        start_room(
            admin_store,
            org_id=org_id,
            service_key=live_service,
            room_id=live_room,
        )

        backend = BackendProcess(_backend_config("f24", proxy.redis_port, interval=5))
        backend.start()
        backend.wait_ready(timeout=45)
        ended_listener = ListenerClient(
            backend.ws_url,
            org_id=org_id,
            room_id=ended_room,
            service_key=ended_service,
            church_slug=slug,
        )
        live_listener = ListenerClient(
            backend.ws_url,
            org_id=org_id,
            room_id=live_room,
            service_key=live_service,
            church_slug=slug,
        )
        await ended_listener.connect()
        await live_listener.connect()
        for listener in (ended_listener, live_listener):
            await listener.wait_for_frame(
                lambda m: m.get("type") == "JOINED",
                timeout=15,
            )

        ended_channel = _channel(org_id, ended_room)
        live_channel = _channel(org_id, live_room)
        await _wait_until(
            lambda: _numsub(ended_channel, live_channel).get(ended_channel) == 1
            and _numsub(ended_channel, live_channel).get(live_channel) == 1,
            timeout=8,
            message="backend did not subscribe both rooms before the outage",
        )

        await asyncio.to_thread(proxy.set_enabled, False)
        await asyncio.sleep(1.5)
        await asyncio.to_thread(
            admin_store.end_room,
            org_id,
            ended_room,
            reason="host_end",
        )

        # Redis is unavailable, so this terminal frame and socket close can
        # only have come from the Firestore reconciler's local cleanup path.
        await ended_listener.wait_for_status_ended(timeout=12)
        await ended_listener.wait_closed(timeout=4)
        assert await live_listener.is_open(), "unrelated live-room listener was closed"

        # Restore Redis. The desired-subscription refcount for the ended room
        # must already be gone; only the unrelated live room is resubscribed.
        await asyncio.to_thread(proxy.set_enabled, True)
        await _wait_until(
            lambda: _numsub(ended_channel, live_channel).get(ended_channel) == 0
            and _numsub(ended_channel, live_channel).get(live_channel) == 1,
            timeout=20,
            message=(
                "Redis reconnect did not selectively restore subscriptions: "
                "ended room must stay unsubscribed and live room must recover"
            ),
        )
        assert await live_listener.is_open(), "live listener did not survive Redis recovery"
    finally:
        try:
            await asyncio.to_thread(proxy.set_enabled, True)
        except Exception:
            pass
        await _close_all(live_listener, ended_listener)
        _stop_and_dump("backend", backend)
