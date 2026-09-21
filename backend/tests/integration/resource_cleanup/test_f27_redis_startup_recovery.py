"""F-27 — startup-recovery regression for redis_pubsub.

Reference: defect issue #29, PR #31 rollout proposal §2.

The bug (present before the fix in this branch): `redis_pubsub.py`
set `_started = True` before the initial `_pub.ping()`. When the
ping raised because Redis was unreachable, the reader task was
never scheduled and subsequent `start()` calls short-circuited at
the enabled/started gate. The reconnect path became unreachable
and the instance silently degraded to local-only broadcast even
after Redis recovered.

Scope note: this pattern CAN produce a cross-instance isolation
outcome similar to the one observed at Track 1 Gate 2. Whether it
was the actual cause of that Gate 2 failure has NOT been established
— the leading Gate 2 inference remains cross-revision resource
separation with REDIS_ENABLED=0 (see project memory). F-27 covers
the local defect; it does not by itself close the Gate 2 causal
chain.

F-27 exercises the local defect end-to-end. Neither backend is
restarted. The test is event-driven — Redis is restored only AFTER
both backends have emitted the initial-failure log line, so a fast
CI cannot accidentally make the ping succeed on the first try.

The initial-failure detection accepts BOTH the new log line
(`redis pubsub initial connect failed`, emitted by the fixed
adapter) AND the old one (`redis connect failed; falling back to
local-only broadcast`, emitted by the pre-fix adapter). Without
this, running F-27 against the pre-fix adapter would time out
waiting for the new string BEFORE Redis restore — the test would
appear to fail at step 3 instead of at the recovery assertion in
step 4, hiding the actual bisection signal.

Acceptance:
  1. Toxiproxy proxy is DOWN before either backend starts.
  2. Both backends emit an initial-failure log line matching either
     the fixed-adapter or pre-fix wording (see `INITIAL_FAILURE_LOG_LINES`).
  3. Toxiproxy is enabled.
  4. Reconnect observability — soft. Poll for `redis pubsub reconnected`
     on both backends. Present = clean recovery signal; absent = just
     the harness's log-level plumbing, so we proceed. The delivery
     assertion in step 6 is the truth.
  5. Host attaches on A; listener attaches on B (handover shape,
     same as F-25/F-26). Baseline delivery on A must succeed.
  6. A marker sent through A reaches the listener on B via
     cross-instance Redis fanout — **this is the hard acceptance**.
     Under the pre-fix adapter no reader task exists, so no
     reconnect happens, so this delivery cannot succeed. Fixed
     adapter recovers in-process and this delivery passes.
  7. Both backend processes remain alive throughout.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
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
from .harness.firestore_seed import seed_org_and_service, start_room
from .harness.openai_stub import OpenAIStub


HOST_TOKEN = "harness-host-token"

# Accept BOTH the fixed-adapter and pre-fix wording. The bisection
# run (revert redis_pubsub.py to main + rerun F-27) must be able to
# reach step 4 (the recovery assertion) under the OLD adapter — with
# only the new string listed, the test would time out at step 2
# because the old adapter never emits it, and the bisection would
# report the wrong failure mode.
INITIAL_FAILURE_LOG_LINES = (
    "redis pubsub initial connect failed",              # fixed adapter (this branch)
    "redis connect failed; falling back to local-only",  # pre-fix adapter (main)
)
RECONNECT_SUCCESS_LOG_LINE = "redis pubsub reconnected"


def _config(
    instance_id: str,
    redis_port: int,
    deepgram_endpoint: str,
    openai_base_url: str,
) -> BackendConfig:
    """Point the backend at a Toxiproxy port instead of real Redis so
    the test can control availability. Tight connect + command timeouts
    keep the initial ping from stalling."""
    return BackendConfig(
        instance_id=instance_id,
        redis_host="127.0.0.1",
        redis_port=redis_port,
        firestore_emulator_host=FIRESTORE_EMULATOR_HOST,
        gcp_project=GCP_PROJECT,
        deepgram_endpoint=deepgram_endpoint,
        openai_base_url=openai_base_url,
        host_api_token=HOST_TOKEN,
        extra_env={
            # Reconciler off — this test isolates the redis_pubsub
            # startup path from the reconciler's fallback cleanup.
            "ROOM_RECONCILER_ENABLED": "0",
            # Bounded initial-attempt latency. Without these, a
            # DOWN Toxiproxy would hang the ping for the OS default
            # TCP timeout (many seconds) and make the test slow.
            "REDIS_CONNECT_TIMEOUT_SEC": "1",
            "REDIS_COMMAND_TIMEOUT_SEC": "1",
        },
    )


def _wait_for_log_line(
    proc: BackendProcess, needle, *, timeout: float,
) -> bool:
    """Poll the backend's captured log until `needle` appears, or
    give up after `timeout`. `needle` is a single string OR a tuple
    of strings — any match returns True. Returns False on timeout
    or early crash."""
    if isinstance(needle, str):
        needles = (needle,)
    else:
        needles = tuple(needle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            body = proc.logs()
        except Exception:
            body = ""
        for n in needles:
            if n in body:
                return True
        # Detect early crash — no point continuing.
        if proc.proc is not None and proc.proc.poll() is not None:
            return False
        time.sleep(0.2)
    return False


async def _wait_for_log_line_async(
    proc: BackendProcess, needle, *, timeout: float,
) -> bool:
    """asyncio-friendly wrapper that doesn't block the event loop."""
    return await asyncio.to_thread(
        _wait_for_log_line, proc, needle, timeout=timeout,
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


def test_f27_redis_startup_failure_is_recovered_by_reader_loop(
    admin_store, redis_outage_proxies,
):
    asyncio.run(_run_f27(admin_store, redis_outage_proxies))


async def _run_f27(admin_store, redis_outage_proxies):
    proxy_a, proxy_b = redis_outage_proxies

    # STEP 1: both proxies DOWN before either backend starts.
    for proxy in (proxy_a, proxy_b):
        await asyncio.to_thread(proxy.set_enabled, False)

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
            listener_b_post_handover, listener_on_a, host_client,
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
        # Restore proxies so a subsequent test in the same session
        # sees them in the expected state.
        for proxy in (proxy_a, proxy_b):
            with contextlib.suppress(Exception):
                await asyncio.to_thread(proxy.set_enabled, True)
        for name, proc in [("backend_a", backend_a), ("backend_b", backend_b)]:
            if proc is None:
                continue
            body = ""
            try:
                body = proc.logs()
            except BaseException:
                pass
            if body:
                print(f"===== {name} ({proc.config.instance_id}) logs =====")
                print(body)
                print(f"===== end {name} logs =====")

    try:
        seed_org_and_service(
            admin_store,
            org_id=org_id, slug=slug,
            service_key=service_key, host_token=HOST_TOKEN,
        )
        await deepgram_stub.start()
        await openai_stub.start()

        # STEP 2: start both backends against DOWN proxies.
        backend_a = BackendProcess(_config(
            "inst-a", proxy_a.redis_port,
            deepgram_stub.endpoint, openai_stub.base_url,
        ))
        backend_a.start()
        backend_a.wait_ready(timeout=45.0)

        backend_b = BackendProcess(_config(
            "inst-b", proxy_b.redis_port,
            deepgram_stub.endpoint, openai_stub.base_url,
        ))
        backend_b.start()
        backend_b.wait_ready(timeout=45.0)

        # STEP 3: EVENT-DRIVEN — do not restore proxies until BOTH
        # backends have logged the initial-failure line. Sleeping a
        # fixed interval instead would race with fast CI: the first
        # ping could succeed if proxies came up before either
        # backend called _pub.ping().
        #
        # Accepts either the fixed or pre-fix log wording so a
        # bisection run against reverted redis_pubsub.py can still
        # reach step 4 (the actual regression assertion).
        assert await _wait_for_log_line_async(
            backend_a, INITIAL_FAILURE_LOG_LINES, timeout=15.0,
        ), (
            "backend A did not emit any recognised initial-failure log "
            "line within 15s — either Redis was already reachable (proxy "
            "still up?) or the code path was skipped"
        )
        assert await _wait_for_log_line_async(
            backend_b, INITIAL_FAILURE_LOG_LINES, timeout=15.0,
        ), "backend B did not emit any recognised initial-failure log line within 15s"

        # STEP 4: restore Redis. The fixed code's reader-loop
        # _reconnect path should bring the subscriber up on each
        # backend without a restart.
        for proxy in (proxy_a, proxy_b):
            await asyncio.to_thread(proxy.set_enabled, True)

        # Observe reconnect if the harness's log level captures INFO
        # from `redis_pubsub`. This is a SOFT signal — the hard
        # acceptance is step 6's cross-instance delivery, which is
        # independent of log plumbing. Under the pre-fix adapter the
        # reader task was never scheduled, so no reconnect happens
        # and step 6 fails; under the fixed adapter recovery happens
        # in-process and step 6 passes.
        for proc, label in ((backend_a, "A"), (backend_b, "B")):
            observed = await _wait_for_log_line_async(
                proc, RECONNECT_SUCCESS_LOG_LINE, timeout=20.0,
            )
            if not observed:
                # Not fatal — just document why the observability path
                # didn't fire (usually a log-level configuration gap).
                # The delivery assertion in step 6 remains the truth.
                print(
                    f"[F-27 note] backend {label} did not emit "
                    f"{RECONNECT_SUCCESS_LOG_LINE!r} within 20s — "
                    f"proceeding to delivery-based recovery check "
                    f"(step 6) as the acceptance signal."
                )

        # STEP 5: attach host on A, listener on B. Handover shape
        # identical to F-26; this proves cross-instance delivery
        # works via the recovered subscribers.
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

        # BASELINE — a listener on A must receive locally.
        baseline_marker = f"baseline-{uuid.uuid4().hex[:6]}"
        delivered = await deepgram_stub.send_transcript(
            f"안녕하세요 {baseline_marker}", is_final=True,
        )
        assert delivered >= 1, "Deepgram stub had no client attached"
        await listener_on_a.wait_for_frame(
            _has_marker_predicate(baseline_marker), timeout=20.0,
        )

        # HANDOVER — listener moves from A to B.
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

        # STEP 6: cross-instance delivery via the recovered
        # subscriber on B.
        cross_marker = f"cross-instance-{uuid.uuid4().hex[:6]}"
        delivered = await deepgram_stub.send_transcript(
            f"안녕하세요 {cross_marker}", is_final=True,
        )
        assert delivered >= 1
        await listener_b_post_handover.wait_for_frame(
            _has_marker_predicate(cross_marker), timeout=20.0,
        )

        # STEP 7: both backend processes still alive — F-27 tests
        # in-process recovery, not restart.
        assert backend_a.proc is not None and backend_a.proc.poll() is None, (
            "backend A exited during F-27 — the test must exercise "
            "in-process recovery, not restart"
        )
        assert backend_b.proc is not None and backend_b.proc.poll() is None, (
            "backend B exited during F-27"
        )

    finally:
        await teardown()
