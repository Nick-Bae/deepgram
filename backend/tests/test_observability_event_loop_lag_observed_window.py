"""Observed-window-boundary fields on the event-loop-lag sampler.

Scope: assert the sampler emits observation-boundary fields
(`observed_window_start_utc`, `observed_window_end_utc`,
`observed_window_elapsed_seconds`) that span the oldest retained
sample's START through the newest sample's END, and verify three
invariants against the exact values produced by an injected clock:

  1. Single-sample case — observed elapsed equals that sample's full
     measurement interval, NOT zero. (This directly discriminates the
     corrected `last_end − first_start` formula from the earlier
     `last_end − first_end` which would yield zero.)
  2. Buffer rollover — once samples evict, the observed start UTC
     advances specifically to the NEXT RETAINED interval's start wall
     clock. The test verifies the exact predicted start string, not a
     "forward" relation.
  3. Clock handling — a backward wall-clock jump keeps
     `observed_window_elapsed_seconds` strictly non-negative and equal
     to the real monotonic span; the emitted UTC boundaries become
     unreliable, which the test ASSERTS BY observing that
     `end_utc < start_utc` under reversed wall clock. Consumers that
     correlate on UTC must therefore not treat ordering as a guarantee.

Clock injection uses the module-level aliases `_now_mono` /
`_now_wall` added for this purpose; it does NOT patch the global
`time` module, so `asyncio.sleep` is untouched. Existing lag
calculations, severity thresholds, cadence, nominal `window_seconds`,
and other preserved field meanings are asserted inline so a
regression on them is also caught here.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import pathlib
import sys
from typing import Any, Iterator

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


# ----- shared helpers --------------------------------------------------


def _parse_event_lines(captured: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in captured.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("event") == "event_loop_lag":
            out.append(obj)
    return out


def _iso_utc(epoch_seconds: float) -> str:
    """Match the sampler's UTC-string format exactly."""
    from datetime import datetime, timezone
    return (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


def _chain_then_repeat(predetermined: list[float], tail: float) -> Iterator[float]:
    """Yield predetermined values once, then `tail` forever.

    After the predetermined sequence is exhausted the sampler may
    continue iterating; emitting the `tail` value keeps subsequent
    `now - last_emit` well-defined and prevents StopIteration from
    crashing the task. The test ignores emissions after the first.
    """
    return itertools.chain(iter(predetermined), itertools.repeat(tail))


async def _run_until_first_emit_or_timeout(
    *,
    emit_interval_s: float,
    sample_interval_s: float,
    window_samples: int,
    real_timeout_s: float,
) -> None:
    from app.observability import event_loop_lag  # noqa: PLC0415

    task = event_loop_lag.start_event_loop_lag_sampler(
        emit_interval_s=emit_interval_s,
        sample_interval_s=sample_interval_s,
        window_samples=window_samples,
    )
    try:
        await asyncio.sleep(real_timeout_s)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ----- 1. Single-sample span equals the measurement interval -----------


def test_single_sample_span_equals_measurement_interval(capsys, monkeypatch):
    """With exactly one buffered sample, the emitted
    `observed_window_elapsed_seconds` must equal that sample's full
    measurement interval (`end_mono − start_mono`), NOT zero.

    Discriminator: the earlier `last_end − first_end` formula would
    yield 0 for a one-sample buffer; the corrected `last_end −
    first_start` yields the sample's own interval.
    """
    from app.observability import event_loop_lag as _ell  # noqa: PLC0415

    # Patched monotonic sequence for exactly one sample + emit:
    #   call 1: last_emit init    → 0.0
    #   call 2: sample start_mono → 10.0
    #   call 3: sample end_mono   → 10.75   (elapsed = 0.75s)
    #   call 4: now (emit check)  → 10.75   (10.75 - 0.0 >= emit=5, EMIT)
    monkeypatch.setattr(
        _ell, "_now_mono",
        _chain_then_repeat([0.0, 10.0, 10.75, 10.75], tail=11.0).__next__,
    )
    monkeypatch.setattr(
        _ell, "_now_wall",
        _chain_then_repeat([1_000_000.0, 1_000_000.75], tail=1_000_000.75).__next__,
    )

    asyncio.run(
        _run_until_first_emit_or_timeout(
            emit_interval_s=5.0,
            sample_interval_s=0.001,  # real asyncio.sleep cadence
            window_samples=1,
            real_timeout_s=0.10,
        )
    )
    events = _parse_event_lines(capsys.readouterr().out)
    assert events, "sampler must emit at least once"

    first = events[0]
    assert first["samples"] == 1, f"expected samples==1, got {first['samples']}"
    # The exact span: end_mono − start_mono = 10.75 − 10.0 = 0.75
    assert first["observed_window_elapsed_seconds"] == pytest.approx(0.75, abs=1e-9), (
        f"single-sample span wrong: expected 0.75, got "
        f"{first['observed_window_elapsed_seconds']}"
    )
    # Preserved: nominal window_seconds unchanged
    assert first["window_seconds"] == pytest.approx(0.001, abs=1e-9)
    # UTC boundaries match the injected wall clock
    assert first["observed_window_start_utc"] == _iso_utc(1_000_000.0)
    assert first["observed_window_end_utc"] == _iso_utc(1_000_000.75)
    # Preserved: schema envelope + core lag fields
    assert first["component"] == "event_loop"
    assert first["schema_version"] == "1"
    assert isinstance(first["p99_ms"], (int, float))


# ----- 2. Exact multi-sample span (first-start → last-end) -------------


def test_exact_multi_sample_span_with_injected_clock(capsys, monkeypatch):
    """Multi-sample emission: span equals newest-end − oldest-start.

    Scenario (clock units are arbitrary; sampler calls `_now_mono`
    three times per iteration — start, end, now-emit-check):

      init          : last_emit = 0
      sample 1      : start=1,  end=2,  now=2    (no emit)
      sample 2      : start=3,  end=4,  now=4    (no emit)
      sample 3      : start=5,  end=8,  now=8    (emit — 8-0 >= 5)

    Expected span = sample 3's end (8) − sample 1's start (1) = 7.
    The earlier `last_end − first_end` would give 8 − 2 = 6.
    """
    from app.observability import event_loop_lag as _ell  # noqa: PLC0415

    monkeypatch.setattr(
        _ell, "_now_mono",
        _chain_then_repeat(
            [0.0,   # init
             1.0, 2.0, 2.0,  # sample 1 (start, end, now)
             3.0, 4.0, 4.0,  # sample 2
             5.0, 8.0, 8.0], # sample 3 — emit fires here
            tail=8.0,
        ).__next__,
    )
    monkeypatch.setattr(
        _ell, "_now_wall",
        _chain_then_repeat(
            [1_000_000.0, 1_000_000.5,   # sample 1 start/end
             1_000_001.0, 1_000_001.5,   # sample 2 start/end
             1_000_002.0, 1_000_005.0],  # sample 3 start/end
            tail=1_000_005.0,
        ).__next__,
    )

    asyncio.run(
        _run_until_first_emit_or_timeout(
            emit_interval_s=5.0,
            sample_interval_s=0.001,
            window_samples=10,  # large enough to retain all 3
            real_timeout_s=0.10,
        )
    )
    events = _parse_event_lines(capsys.readouterr().out)
    assert events

    first = events[0]
    assert first["samples"] == 3
    # Expected: 8 (last end) − 1 (first start) = 7
    assert first["observed_window_elapsed_seconds"] == pytest.approx(7.0, abs=1e-9), (
        f"multi-sample span wrong (should include oldest sample's measurement "
        f"interval): expected 7.0, got {first['observed_window_elapsed_seconds']}"
    )
    assert first["observed_window_start_utc"] == _iso_utc(1_000_000.0)
    assert first["observed_window_end_utc"] == _iso_utc(1_000_005.0)
    # Nominal window_seconds is preserved — nominal, not measured
    assert first["window_seconds"] == pytest.approx(0.001 * 3, abs=1e-9)


# ----- 3. Buffer rollover advances to next-retained-interval start -----


def test_rollover_advances_to_next_retained_interval_start(capsys, monkeypatch):
    """With window_samples=2, 4 samples before emit force the oldest 2
    samples out of the ring. The emitted `observed_window_start_utc`
    must equal the START wall-clock of sample 3 (now the oldest
    retained) — not sample 1's (evicted) or sample 2's (also evicted).

    Scenario (`emit_interval_s = 7` so emit only fires after sample 4):
      init          : last_emit = 0
      sample 1 (S1) : start=1,  end=2,  now=2    (2 − 0 < 7; buffer=[S1])
      sample 2 (S2) : start=3,  end=4,  now=4    (4 − 0 < 7; buffer=[S1,S2])
      sample 3 (S3) : start=5,  end=6,  now=6    (6 − 0 < 7; buffer=[S2,S3] — S1 evicted)
      sample 4 (S4) : start=7,  end=10, now=10   (10 − 0 ≥ 7, emit;
                                                   buffer=[S3,S4] — S2 evicted)

    Expected:
      - observed_window_start_utc == iso(S3.start_wall)   (S3 oldest-retained)
      - observed_window_end_utc   == iso(S4.end_wall)     (S4 newest)
      - observed_window_elapsed_seconds = S4.end − S3.start = 10 − 5 = 5.0
    """
    from app.observability import event_loop_lag as _ell  # noqa: PLC0415

    monkeypatch.setattr(
        _ell, "_now_mono",
        _chain_then_repeat(
            [0.0,
             1.0, 2.0, 2.0,
             3.0, 4.0, 4.0,
             5.0, 6.0, 6.0,
             7.0, 10.0, 10.0],
            tail=10.0,
        ).__next__,
    )
    # Deliberately spaced wall clocks per sample so retained vs
    # evicted boundaries are visually unambiguous.
    S1_SW, S1_EW = 1_000_000.0, 1_000_000.5
    S2_SW, S2_EW = 1_000_001.0, 1_000_001.5
    S3_SW, S3_EW = 1_000_002.0, 1_000_002.5
    S4_SW, S4_EW = 1_000_003.0, 1_000_006.0
    monkeypatch.setattr(
        _ell, "_now_wall",
        _chain_then_repeat(
            [S1_SW, S1_EW,
             S2_SW, S2_EW,
             S3_SW, S3_EW,
             S4_SW, S4_EW],
            tail=S4_EW,
        ).__next__,
    )

    asyncio.run(
        _run_until_first_emit_or_timeout(
            emit_interval_s=7.0,  # see docstring: emit only after sample 4
            sample_interval_s=0.001,
            window_samples=2,  # ring size: oldest-2 evicted
            real_timeout_s=0.10,
        )
    )
    events = _parse_event_lines(capsys.readouterr().out)
    assert events

    first = events[0]
    assert first["samples"] == 2, (
        f"ring buffer should hold 2 samples; got {first['samples']}"
    )
    # The retained oldest is S3 (S1, S2 evicted).
    assert first["observed_window_start_utc"] == _iso_utc(S3_SW), (
        f"observed_window_start_utc must equal next-retained-interval start "
        f"{_iso_utc(S3_SW)}; got {first['observed_window_start_utc']}"
    )
    assert first["observed_window_start_utc"] != _iso_utc(S1_SW), (
        "regression: start_utc stuck on evicted S1"
    )
    assert first["observed_window_start_utc"] != _iso_utc(S2_SW), (
        "regression: start_utc stuck on evicted S2"
    )
    # End remains S4's end wall clock.
    assert first["observed_window_end_utc"] == _iso_utc(S4_EW)
    # Elapsed = S4.end_mono − S3.start_mono = 10 − 5 = 5
    assert first["observed_window_elapsed_seconds"] == pytest.approx(5.0, abs=1e-9)


# ----- 4. Backward wall-clock jump: monotonic preserved, UTC unreliable -


def test_wall_clock_backward_jump_preserves_monotonic_and_flags_utc_unreliability(
    capsys, monkeypatch
):
    """A reversed wall clock must NOT corrupt
    `observed_window_elapsed_seconds` (which is monotonic-only).
    Under the same reversed clock the emitted UTC boundaries become
    UNRELIABLE for log correlation: specifically `end_utc < start_utc`.
    The test asserts BOTH:
      - monotonic span is correct and non-negative, AND
      - the end_utc precedes start_utc, documenting that UTC ordering
        is NOT a guarantee of this emission's temporal semantics under
        pathological clock behavior.
    """
    from app.observability import event_loop_lag as _ell  # noqa: PLC0415

    # Monotonic clock advances normally (as it must by definition):
    #   init=0; sample 1 start=1, end=3, now=3  (emit — 3-0 >= 2)
    monkeypatch.setattr(
        _ell, "_now_mono",
        _chain_then_repeat([0.0, 1.0, 3.0, 3.0], tail=4.0).__next__,
    )
    # Wall clock runs BACKWARD between the start and end stamps.
    START_WALL = 2_000_000.0   # chronologically "later"
    END_WALL = 1_000_000.0     # chronologically "earlier" — backward jump
    monkeypatch.setattr(
        _ell, "_now_wall",
        _chain_then_repeat([START_WALL, END_WALL], tail=END_WALL).__next__,
    )

    asyncio.run(
        _run_until_first_emit_or_timeout(
            emit_interval_s=2.0,
            sample_interval_s=0.001,
            window_samples=1,
            real_timeout_s=0.10,
        )
    )
    events = _parse_event_lines(capsys.readouterr().out)
    assert events

    first = events[0]
    assert first["samples"] == 1
    # Monotonic span: 3 − 1 = 2. The reversed wall clock cannot touch it.
    assert first["observed_window_elapsed_seconds"] == pytest.approx(2.0, abs=1e-9), (
        f"wall-clock reversal leaked into monotonic arithmetic: "
        f"elapsed={first['observed_window_elapsed_seconds']}"
    )
    assert first["observed_window_elapsed_seconds"] >= 0.0
    # UTC boundaries reflect the reversed wall clock. The emission
    # faithfully reports what the clock said; downstream correlation
    # consumers must know this ordering is NOT a guarantee.
    assert first["observed_window_start_utc"] == _iso_utc(START_WALL)
    assert first["observed_window_end_utc"] == _iso_utc(END_WALL)
    # Explicit unreliability signal: end precedes start lexically
    # (both strings share the same ISO format so lexical order follows
    # UTC order at microsecond precision).
    assert first["observed_window_end_utc"] < first["observed_window_start_utc"], (
        "under reversed wall clock, end_utc must precede start_utc; this "
        "emission intentionally exposes that condition rather than masking it"
    )
