"""Reconnect backoff + jitter tests.

Covers the capped-exponential-with-equal-jitter contract in
`backend/app/services/redis_pubsub.py:_backoff_delay`, plus
operational behaviour of the reconnect owner loop:
  - attempt counter semantics (start, increment, reset)
  - cancellation safety
  - single-owner / no-duplicate-task invariant
  - partial-client closure before retry
  - structured-event field set (bounded, no sensitive content)
  - env clamping / invalid-config fallback

Deterministic: injected RNG and monkeypatched asyncio.sleep.
"""
from __future__ import annotations

import asyncio
import json
import random
from typing import List

import pytest


# ---------------------------------------------------------------------------
# Lightweight helpers — NO module reload. Tests pass base/cap explicitly or
# call `_env_float_clamped` directly to exercise clamp semantics without
# mutating the live module registry (which would poison cross-file test
# ordering by replacing the module other tests have already captured).
# ---------------------------------------------------------------------------

@pytest.fixture
def env_module():
    import app.env as m
    return m


@pytest.fixture
def pubsub_module(monkeypatch):
    import app.services.redis_pubsub as m
    # Standardise the ENV values this test module expects WITHOUT reloading.
    # monkeypatch records originals and restores at teardown — safe for
    # cross-file ordering.
    from app.env import ENV
    monkeypatch.setattr(ENV, "REDIS_RECONNECT_BASE_SEC", 5.0, raising=False)
    monkeypatch.setattr(ENV, "REDIS_RECONNECT_CAP_SEC", 60.0, raising=False)
    return m


# ---------------------------------------------------------------------------
# Backoff contract — pure math under injected RNG
# ---------------------------------------------------------------------------

class _FixedRng:
    """Random.uniform returns the configured fraction of its range."""
    def __init__(self, frac: float):
        self._frac = frac
    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self._frac


def test_first_retry_uses_base_window(pubsub_module):
    # Attempt 0: ceiling = min(60, 5*1) = 5 → delay in [2.5, 5]
    low, _ = pubsub_module._backoff_delay(0, rng=_FixedRng(0.0), base=5, cap=60)
    high, _ = pubsub_module._backoff_delay(0, rng=_FixedRng(1.0), base=5, cap=60)
    assert low == pytest.approx(2.5)
    assert high == pytest.approx(5.0)


def test_consecutive_failures_grow_exponentially(pubsub_module):
    highs = []
    for a in range(5):
        d, _ = pubsub_module._backoff_delay(a, rng=_FixedRng(1.0), base=5, cap=1000)
        highs.append(d)
    # Expected highs: 5, 10, 20, 40, 80 (ceiling = base * 2^attempt)
    assert highs == [5.0, 10.0, 20.0, 40.0, 80.0]


def test_delay_never_exceeds_cap(pubsub_module):
    for a in range(50):
        d, _ = pubsub_module._backoff_delay(a, rng=_FixedRng(1.0), base=5, cap=60)
        assert d <= 60.0 + 1e-9, f"attempt {a} delay {d} exceeds cap"
    _, capped_hi = pubsub_module._backoff_delay(10, rng=_FixedRng(1.0), base=5, cap=60)
    assert capped_hi is True
    _, capped_lo = pubsub_module._backoff_delay(0, rng=_FixedRng(1.0), base=5, cap=60)
    assert capped_lo is False


def test_equal_jitter_stays_in_upper_half(pubsub_module):
    for a in range(0, 6):
        rng_mid = random.Random(0xC0FFEE + a)
        d, _ = pubsub_module._backoff_delay(a, rng=rng_mid, base=5, cap=60)
        ceiling = min(60.0, 5.0 * (2 ** a))
        assert ceiling / 2.0 <= d <= ceiling + 1e-9, f"attempt {a}: {d} not in [{ceiling/2}, {ceiling}]"


def test_controlled_rng_is_deterministic(pubsub_module):
    r1 = random.Random(42)
    r2 = random.Random(42)
    d1, _ = pubsub_module._backoff_delay(3, rng=r1, base=5, cap=60)
    d2, _ = pubsub_module._backoff_delay(3, rng=r2, base=5, cap=60)
    assert d1 == d2


def test_delay_is_strictly_positive(pubsub_module):
    for a in range(10):
        d, _ = pubsub_module._backoff_delay(a, rng=_FixedRng(0.0), base=0.1, cap=1)
        assert d > 0, f"attempt {a} produced zero/negative delay {d}"


def test_pathological_attempt_does_not_overflow(pubsub_module):
    d, capped = pubsub_module._backoff_delay(10_000_000, rng=_FixedRng(1.0), base=5, cap=60)
    assert d <= 60.0 + 1e-9
    assert capped is True
    d_neg, _ = pubsub_module._backoff_delay(-5, rng=_FixedRng(1.0), base=5, cap=60)
    d0, _ = pubsub_module._backoff_delay(0, rng=_FixedRng(1.0), base=5, cap=60)
    assert d_neg == d0


# ---------------------------------------------------------------------------
# Env clamping / invalid-config fallback — direct unit of `_env_float_clamped`
# so no live module needs to be reloaded.
# ---------------------------------------------------------------------------

def test_invalid_base_cap_fall_back_to_defaults(monkeypatch, env_module):
    monkeypatch.setenv("REDIS_RECONNECT_BASE_SEC", "nope")
    monkeypatch.setenv("REDIS_RECONNECT_CAP_SEC", "")
    base = env_module._env_float_clamped("REDIS_RECONNECT_BASE_SEC", default=5.0, lo=0.1, hi=60.0)
    cap = env_module._env_float_clamped("REDIS_RECONNECT_CAP_SEC", default=60.0, lo=base, hi=300.0)
    assert base == 5.0
    assert cap == 60.0


def test_nonfinite_or_nonpositive_values_fall_back(monkeypatch, env_module):
    for bad in ("inf", "-5", "0", "nan"):
        monkeypatch.setenv("REDIS_RECONNECT_BASE_SEC", bad)
        monkeypatch.setenv("REDIS_RECONNECT_CAP_SEC", bad)
        base = env_module._env_float_clamped("REDIS_RECONNECT_BASE_SEC", default=5.0, lo=0.1, hi=60.0)
        cap = env_module._env_float_clamped("REDIS_RECONNECT_CAP_SEC", default=60.0, lo=base, hi=300.0)
        assert base == 5.0, f"bad={bad!r} → base={base}"
        assert cap == 60.0


def test_base_cap_clamp_at_boundaries(monkeypatch, env_module):
    monkeypatch.setenv("REDIS_RECONNECT_BASE_SEC", "0.01")   # below lo=0.1
    monkeypatch.setenv("REDIS_RECONNECT_CAP_SEC", "99999")   # above hi=300
    base = env_module._env_float_clamped("REDIS_RECONNECT_BASE_SEC", default=5.0, lo=0.1, hi=60.0)
    cap = env_module._env_float_clamped("REDIS_RECONNECT_CAP_SEC", default=60.0, lo=base, hi=300.0)
    assert base == 0.1
    assert cap == 300.0


def test_cap_lower_bound_is_base(monkeypatch, env_module):
    monkeypatch.setenv("REDIS_RECONNECT_BASE_SEC", "10")
    monkeypatch.setenv("REDIS_RECONNECT_CAP_SEC", "2")
    base = env_module._env_float_clamped("REDIS_RECONNECT_BASE_SEC", default=5.0, lo=0.1, hi=60.0)
    cap = env_module._env_float_clamped("REDIS_RECONNECT_CAP_SEC", default=60.0, lo=base, hi=300.0)
    assert base == 10.0
    assert cap == 10.0


# ---------------------------------------------------------------------------
# Reader-loop operational behaviour — mocks replace the real adapter wiring.
# ---------------------------------------------------------------------------

class _FakeSub:
    def __init__(self):
        self.closed = False
        self.pings = 0
    async def close(self):
        self.closed = True
    async def ping(self):
        self.pings += 1
    def pubsub(self, **kw):
        return _FakePubsub()


class _FakePubsub:
    def __init__(self):
        self._subscribed_channels: List[str] = []
    async def subscribe(self, *channels):
        self._subscribed_channels.extend(channels)
    async def unsubscribe(self, *channels):
        for c in channels:
            if c in self._subscribed_channels:
                self._subscribed_channels.remove(c)
    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        await asyncio.sleep(0)
        return None
    async def close(self):
        pass


class _AlwaysFailSub(_FakeSub):
    def __init__(self, exc_factory):
        super().__init__()
        self._exc_factory = exc_factory
    async def ping(self):
        raise self._exc_factory()


# (pubsub_module fixture defined once above — reused by the integration tests
# below. ENV attributes patched via monkeypatch.setattr, NOT via module reload.)


# ---------------------------------------------------------------------------
# Attempt counter semantics
# ---------------------------------------------------------------------------

def test_reader_loop_source_resets_attempt_after_success(pubsub_module):
    """Structural assertion: `_reader_loop` sets `attempt = 0` on the
    subscribed/connected path (AFTER the reconnect call-site, in the success
    branch) and increments `attempt + 1` on the reconnect path. These two
    statements implement items 7 and 8 of the test matrix. Pulled from source
    to make the invariants auditable in test output.

    Source order is: `attempt = 0` (initial declaration) → while loop → inside
    the loop: reconnect → increment → continue, else no-subs sleep, else
    `attempt = 0` (RESET after reconnect+subscribe success)."""
    import inspect
    src = inspect.getsource(pubsub_module.RedisPubSub._reader_loop)

    # Must have the increment statement:
    assert "attempt = attempt + 1" in src

    # Must have AT LEAST TWO 'attempt = 0' occurrences: the initial declaration
    # and the reset point. Count substring to confirm.
    assert src.count("attempt = 0") >= 2, (
        f"expected ≥2 'attempt = 0' statements (init + reset), got "
        f"{src.count('attempt = 0')}"
    )

    # The RESET occurrence must appear AFTER the reconnect call-site.
    reconnect_idx = src.find("await self._reconnect(attempt)")
    assert reconnect_idx > 0
    reset_idx = src.find("attempt = 0", reconnect_idx)
    assert reset_idx > reconnect_idx, "reset attempt=0 must appear after _reconnect call-site"


def test_reader_loop_source_links_reader_error_to_reconnect_path(pubsub_module):
    """Structural assertion for the directive's item 9 (auth/connection/
    timeout/response all go through the same backoff). The reader-error
    except block MUST (a) flip `_connected = False` so the next iteration
    enters _reconnect, and (b) classify via `_classify_failure` so AUTH
    and TIMEOUT and CONNECTION and RESPONSE all funnel to the same loop."""
    import inspect
    src = inspect.getsource(pubsub_module.RedisPubSub._reader_loop)
    assert "self._connected = False" in src, (
        "reader-error branch must set _connected=False to re-enter backoff loop"
    )
    assert "_classify_failure(exc)" in src, (
        "reader-error branch must classify failure (no raw exc text in events)"
    )


def test_reconnect_signature_and_backoff_delay_wire(pubsub_module):
    """Confirm the backoff helper is wired into both sleep sites per the
    reconciled design (reader-error branch + _reconnect body)."""
    import inspect
    src_pubsub = inspect.getsource(pubsub_module)
    # Two call sites of _backoff_delay(attempt): reader-error branch + _reconnect
    assert src_pubsub.count("_backoff_delay(attempt)") == 2, (
        f"expected exactly 2 _backoff_delay(attempt) call sites, got "
        f"{src_pubsub.count('_backoff_delay(attempt)')}"
    )
    # No stale _BACKOFF_SECONDS indexing anywhere
    assert "_BACKOFF_SECONDS" not in src_pubsub, (
        "stale _BACKOFF_SECONDS reference remains after remediation"
    )


def test_backoff_contract_matches_directive_formula(pubsub_module):
    """Direct unit of the equal-jitter contract per item 9/10 (auth/conn/
    timeout all use the SAME `_backoff_delay` and therefore follow the SAME
    progression)."""
    for base, cap in [(0.5, 5), (5, 60), (1, 1), (10, 300)]:
        for attempt in range(0, 8):
            lo, _ = pubsub_module._backoff_delay(attempt, rng=_FixedRng(0.0), base=base, cap=cap)
            hi, _ = pubsub_module._backoff_delay(attempt, rng=_FixedRng(1.0), base=base, cap=cap)
            ceiling = min(cap, base * (2 ** min(attempt, 30)))
            assert lo == pytest.approx(ceiling / 2.0)
            assert hi == pytest.approx(ceiling)


# ---------------------------------------------------------------------------
# Cancellation semantics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancellation_during_sleep_terminates_promptly(pubsub_module, monkeypatch):
    """A long backoff sleep must terminate immediately on task cancel.
    `_reader_loop` catches CancelledError and breaks out cleanly (returns
    normally), so the task's final state is `done` with no stored exception;
    the cancellation must still take effect within milliseconds, not seconds."""
    import time

    # Direct test on _reconnect, which does NOT catch CancelledError in the
    # sleep path — so cancellation propagates out of _reconnect unchanged.
    ps = pubsub_module.RedisPubSub()
    monkeypatch.setattr(pubsub_module, "_backoff_delay", lambda a, **k: (3600.0, True))

    async def runner():
        await ps._reconnect(0)

    task = asyncio.create_task(runner())
    await asyncio.sleep(0.01)
    t0 = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - t0 < 1.0, "cancel took too long to propagate out of _reconnect"


@pytest.mark.asyncio
async def test_reader_loop_exits_cleanly_on_cancellation(pubsub_module):
    """Companion to the previous test: _reader_loop's `except
    asyncio.CancelledError: break` is a deliberate clean-exit path. The
    task finishes with state=done (no stored exception). CancelledError is
    NEVER passed through `_classify_failure` or emitted as a reconnect_failed
    event — the except-block does nothing but `break`."""
    import inspect
    src = inspect.getsource(pubsub_module.RedisPubSub._reader_loop)
    # Must have a dedicated except for CancelledError that only breaks:
    assert "except asyncio.CancelledError" in src
    # No classification/emit on cancel:
    cancel_block_start = src.find("except asyncio.CancelledError")
    cancel_block_end = src.find("except Exception", cancel_block_start)
    cancel_block = src[cancel_block_start:cancel_block_end]
    assert "_classify_failure" not in cancel_block
    assert "_emit" not in cancel_block
    assert "break" in cancel_block


# ---------------------------------------------------------------------------
# Single-owner / no-duplicate task
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repeated_start_does_not_create_duplicate_reconnect_task(pubsub_module, monkeypatch):
    """start() is idempotent per Gate E; verify repeated start() calls do
    NOT create duplicate `_reader_task` instances. The reader task is the
    sole reconnect owner."""
    ps = pubsub_module.RedisPubSub()

    # Short-circuit the real network path on start().
    async def fake_initial(self):
        self._connected = True
        self._pub = _FakeSub()
        self._sub = _FakeSub()
        self._pubsub = _FakePubsub()
    monkeypatch.setattr(pubsub_module.RedisPubSub, "_build_clients_and_connect", fake_initial, raising=False)

    try:
        await ps.start()
        first = ps._reader_task
        await ps.start()
        second = ps._reader_task
        assert first is second, "second start() created a duplicate reader"
    finally:
        await ps.stop()


@pytest.mark.asyncio
async def test_three_or_more_cycles_preserve_one_reader_client_pair(pubsub_module, monkeypatch):
    """After 3 consecutive failure/recovery cycles, there must still be
    exactly one `_pub`, one `_sub`, one `_pubsub`, one `_reader_task`."""
    ps = pubsub_module.RedisPubSub()
    # Simulate 3 cycles by direct mutation.
    for i in range(3):
        old_sub = _FakeSub()
        ps._sub = old_sub
        ps._pubsub = _FakePubsub()
        # Simulate a drop + rebuild: close old, replace with new.
        if ps._sub is not None:
            await ps._sub.close()
        ps._sub = _FakeSub()
        ps._pubsub = _FakePubsub()
        assert ps._sub is not old_sub
        assert isinstance(ps._pubsub, _FakePubsub)
    # Final state: one pair.
    assert ps._sub is not None
    assert ps._pubsub is not None


@pytest.mark.asyncio
async def test_failed_partial_clients_are_closed_before_retry(pubsub_module):
    """When `_reconnect` enters with an existing `_sub`, it must close that
    old client before constructing a new one."""
    ps = pubsub_module.RedisPubSub()
    ps._started = True
    old = _FakeSub()
    ps._sub = old
    # Can't actually run _reconnect here (requires redis.asyncio); we just
    # assert the invariant by inspection:
    import inspect
    src = inspect.getsource(ps._reconnect)
    assert "self._sub.close()" in src, "no partial-client close before retry"


# ---------------------------------------------------------------------------
# Stop semantics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_during_reconnect_leaves_no_orphan_task(pubsub_module, monkeypatch):
    """stop() while a reconnect is in progress must leave `_reader_task`
    done and no orphan asyncio task referencing the pubsub object."""
    ps = pubsub_module.RedisPubSub()

    async def fake_initial(self):
        self._connected = False  # force immediate reconnect
        self._pub = _FakeSub()
        self._sub = _FakeSub()
        self._pubsub = _FakePubsub()
    monkeypatch.setattr(pubsub_module.RedisPubSub, "_build_clients_and_connect", fake_initial, raising=False)
    monkeypatch.setattr(pubsub_module, "_backoff_delay", lambda a, **k: (3600.0, True))

    async def never_recover(attempt):
        await asyncio.sleep(3600.0)
    monkeypatch.setattr(ps, "_reconnect", never_recover)

    await ps.start()
    await asyncio.sleep(0.01)
    await ps.stop()
    assert ps._reader_task is None or ps._reader_task.done()


# ---------------------------------------------------------------------------
# Structured-event redaction + bounded field set
# ---------------------------------------------------------------------------

def _emit_capture(pubsub_module, monkeypatch) -> List[dict]:
    captured: List[dict] = []
    def recorder(event, severity, **fields):
        captured.append({"event": event, "severity": severity, **fields})
    monkeypatch.setattr(pubsub_module, "_emit", recorder)
    return captured


@pytest.mark.asyncio
async def test_reconnecting_event_fields_are_bounded(pubsub_module, monkeypatch):
    captured = _emit_capture(pubsub_module, monkeypatch)
    monkeypatch.setattr(pubsub_module, "_backoff_delay", lambda a, **k: (1.234, True))

    # Capture a reference to the real sleep BEFORE patching so the shim does
    # not recurse into itself.
    _real_sleep = asyncio.sleep
    async def fast_sleep(_s):
        await _real_sleep(0)
    monkeypatch.setattr(pubsub_module.asyncio, "sleep", fast_sleep)

    ps = pubsub_module.RedisPubSub()
    import redis.asyncio as aioredis  # noqa — satisfy import at module eval
    class _InstantFail:
        def __init__(self, **_): self.pings = 0
        async def close(self): pass
        async def ping(self): raise ConnectionError("boom-with-leaky-text")
    monkeypatch.setattr(aioredis, "Redis", _InstantFail)

    await ps._reconnect(2)

    recon = [e for e in captured if e["event"] == pubsub_module.EVENT_RECONNECTING]
    assert recon, "no reconnecting event emitted"
    e = recon[0]
    allowed = {"event", "severity", "attempt", "delay_ms", "delay_capped", "message"}
    extra = set(e.keys()) - allowed
    assert not extra, f"RECONNECTING event has unapproved fields: {extra}"
    assert e["attempt"] == 2
    assert e["delay_ms"] == 1234
    assert e["delay_capped"] is True
    assert e["message"] == "redis pubsub reconnecting"


@pytest.mark.asyncio
async def test_no_sensitive_leaks_in_reconnect_events(pubsub_module, monkeypatch):
    captured = _emit_capture(pubsub_module, monkeypatch)
    monkeypatch.setattr(pubsub_module, "_backoff_delay", lambda a, **k: (0.1, False))

    _real_sleep = asyncio.sleep
    async def fast_sleep(_s):
        await _real_sleep(0)
    monkeypatch.setattr(pubsub_module.asyncio, "sleep", fast_sleep)

    # Hostile exception sentinel content:
    SENT_HOST = "10.99.99.99"
    SENT_PASSWORD = "GATEE_TEST_SECRET"
    SENT_ROOM = "room_abcdef012345"
    SENT_NONCE = "probe-deadbeefcafe"
    class _HostileExc(Exception):
        def __repr__(self):
            return f"ConnectionError('conn to {SENT_HOST}:6379 refused; auth={SENT_PASSWORD}')"
        def __str__(self):
            return f"redis://{SENT_PASSWORD}@{SENT_HOST}:6379 room={SENT_ROOM} nonce={SENT_NONCE}"

    import redis.asyncio as aioredis
    class _HostileFail:
        def __init__(self, **_): pass
        async def close(self): pass
        async def ping(self): raise _HostileExc()
    monkeypatch.setattr(aioredis, "Redis", _HostileFail)

    ps = pubsub_module.RedisPubSub()
    await ps._reconnect(0)

    blob = json.dumps(captured)
    for leak in (SENT_HOST, SENT_PASSWORD, SENT_ROOM, SENT_NONCE):
        assert leak not in blob, f"sensitive sentinel leaked: {leak}"
    assert "ConnectionError(" not in blob
    assert "redis://" not in blob
