"""Observed-window-boundary + wall-clock-drift fields on the sampler.

Scope: assert the sampler emits six observation-related fields
(`observed_window_start_utc`, `observed_window_end_utc`,
`observed_window_elapsed_seconds`, `wall_clock_drift_seconds`,
preserved `samples`, preserved nominal `window_seconds`) and verify
their contract against the exact values produced by an injected
clock:

  1. Single-sample case — observed elapsed equals that sample's full
     measurement interval, NOT zero. Drift-free injected clocks yield
     `wall_clock_drift_seconds == 0.0`.
  2. Multi-sample case — observed elapsed equals newest-end minus
     oldest-start. Drift-free injected clocks yield drift zero.
  3. Buffer rollover — once samples evict, the observed start UTC
     advances specifically to the NEXT RETAINED interval's start.
  4. Reversed wall clock — monotonic duration preserved; drift is a
     specific large negative value matching the reversal magnitude;
     UTC endpoints are inverted (secondary signal of the pathology).
  5. FORWARD wall adjustment — wall advances MORE than monotonic
     during the window (e.g., clock jumped forward mid-sample). Drift
     is positive by a specific value. UTC endpoints remain correctly
     ordered (end > start). Monotonic duration is unchanged.
  6. BACKWARD wall adjustment with endpoints ordered — wall advances
     LESS than monotonic (clock dialed back some amount mid-sample)
     but net wall delta is still positive. Drift is negative by a
     specific value. UTC endpoints remain correctly ordered. This is
     the case the UTC-order check alone would MISS; the drift field
     exposes it.

Contract of `wall_clock_drift_seconds` (as emitted by the sampler):

    wall_clock_drift_seconds =
        (last_end_wall − first_start_wall)
        − observed_window_elapsed_seconds

Pure fact; the sampler asserts no tolerance of its own. Zero does
NOT prove UTC accuracy (compensating adjustments can cancel inside
the window). Negative does NOT by itself prove reversal (wall may
simply have advanced less than monotonic). Consumers must apply
their own explicit policy before correlating on UTC.

Clock injection uses the module-level aliases `_now_mono` /
`_now_wall`; it does NOT patch the global `time` module, so
`asyncio.sleep` is untouched. Existing lag calculations, severity
thresholds, cadence, nominal `window_seconds`, and other preserved
field meanings are asserted inline so a regression on them is also
caught here.
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
#        (drift-free clocks → drift == 0.0)


def test_single_sample_span_equals_measurement_interval(capsys, monkeypatch):
    """With exactly one buffered sample, the emitted
    `observed_window_elapsed_seconds` must equal that sample's full
    measurement interval (`end_mono − start_mono`), NOT zero.

    Clocks are drift-free (wall delta == monotonic delta) so
    `wall_clock_drift_seconds == 0.0`.
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
    # Wall delta = 0.75, matches monotonic delta — drift-free.
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
    # Exact span: end_mono − start_mono = 10.75 − 10.0 = 0.75
    assert first["observed_window_elapsed_seconds"] == pytest.approx(0.75, abs=1e-9)
    # Drift-free: wall delta (0.75) − mono elapsed (0.75) = 0.0
    assert first["wall_clock_drift_seconds"] == pytest.approx(0.0, abs=1e-9), (
        f"drift-free clocks must emit drift==0.0; got {first['wall_clock_drift_seconds']}"
    )
    # Preserved: nominal window_seconds
    assert first["window_seconds"] == pytest.approx(0.001, abs=1e-9)
    # UTC boundaries match the injected wall clock
    assert first["observed_window_start_utc"] == _iso_utc(1_000_000.0)
    assert first["observed_window_end_utc"] == _iso_utc(1_000_000.75)
    # Preserved: schema envelope + core lag fields
    assert first["component"] == "event_loop"
    assert first["schema_version"] == "1"
    assert isinstance(first["p99_ms"], (int, float))


# ----- 2. Exact multi-sample span (first-start → last-end) -------------
#        (drift-free clocks → drift == 0.0)


def test_exact_multi_sample_span_with_injected_clock(capsys, monkeypatch):
    """Multi-sample emission: span equals newest-end − oldest-start.
    Wall delta per-sample matches monotonic delta, so drift == 0.0.

    Scenario (clock units are arbitrary; sampler calls `_now_mono`
    three times per iteration — start, end, now-emit-check):

      init          : last_emit = 0
      sample 1      : start=1,  end=2,  now=2    (no emit)
      sample 2      : start=3,  end=4,  now=4    (no emit)
      sample 3      : start=5,  end=8,  now=8    (emit — 8-0 >= 5)

    Expected monotonic span = 8 − 1 = 7. Wall values chosen so wall
    delta per sample equals mono delta per sample, hence drift = 0.
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
    # Wall deltas per sample: 1, 1, 3 — identical to mono deltas.
    # First-start → last-end wall delta = 1_000_007 − 1_000_000 = 7.
    monkeypatch.setattr(
        _ell, "_now_wall",
        _chain_then_repeat(
            [1_000_000.0, 1_000_001.0,   # sample 1 (mono delta 1 == wall delta 1)
             1_000_002.0, 1_000_003.0,   # sample 2 (mono delta 1 == wall delta 1)
             1_000_004.0, 1_000_007.0],  # sample 3 (mono delta 3 == wall delta 3)
            tail=1_000_007.0,
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
    assert first["observed_window_elapsed_seconds"] == pytest.approx(7.0, abs=1e-9)
    # Drift-free across all three samples: wall_delta (7) − mono_elapsed (7) = 0
    assert first["wall_clock_drift_seconds"] == pytest.approx(0.0, abs=1e-9)
    assert first["observed_window_start_utc"] == _iso_utc(1_000_000.0)
    assert first["observed_window_end_utc"] == _iso_utc(1_000_007.0)
    # Nominal window_seconds is preserved — nominal, not measured
    assert first["window_seconds"] == pytest.approx(0.001 * 3, abs=1e-9)


# ----- 3. Buffer rollover advances to next-retained-interval start -----
#        (clocks are not drift-free in this scenario; drift value asserted)


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

    Monotonic elapsed = S4.end (10) − S3.start (5) = 5.
    Wall delta = S4.end_wall (1_000_006.0) − S3.start_wall (1_000_002.0) = 4.
    Drift = 4 − 5 = −1 (wall advanced less than monotonic over the
    retained window; a specific, non-zero drift value — asserted exactly).
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
            emit_interval_s=7.0,
            sample_interval_s=0.001,
            window_samples=2,
            real_timeout_s=0.10,
        )
    )
    events = _parse_event_lines(capsys.readouterr().out)
    assert events

    first = events[0]
    assert first["samples"] == 2
    # Oldest-retained is S3 (S1, S2 evicted).
    assert first["observed_window_start_utc"] == _iso_utc(S3_SW)
    assert first["observed_window_start_utc"] != _iso_utc(S1_SW), (
        "regression: start_utc stuck on evicted S1"
    )
    assert first["observed_window_start_utc"] != _iso_utc(S2_SW), (
        "regression: start_utc stuck on evicted S2"
    )
    assert first["observed_window_end_utc"] == _iso_utc(S4_EW)
    assert first["observed_window_elapsed_seconds"] == pytest.approx(5.0, abs=1e-9)
    # Exact drift: (S4_EW − S3_SW) − 5.0 = 4.0 − 5.0 = −1.0
    assert first["wall_clock_drift_seconds"] == pytest.approx(-1.0, abs=1e-9)


# ----- 4. Reversed wall clock: monotonic preserved, drift large negative -


def test_reversed_wall_clock_preserves_monotonic_and_emits_large_negative_drift(
    capsys, monkeypatch
):
    """A fully reversed wall clock must NOT corrupt the monotonic
    duration. The drift field reports the specific disagreement; the
    UTC endpoints are inverted as a secondary signal of the pathology.

    Monotonic elapsed = 3 − 1 = 2 (unchanged by wall reversal).
    Wall delta = END_WALL − START_WALL = 1_000_000 − 2_000_000 = −1_000_000.
    Drift = −1_000_000 − 2 = −1_000_002 (exact, large negative).
    """
    from app.observability import event_loop_lag as _ell  # noqa: PLC0415

    monkeypatch.setattr(
        _ell, "_now_mono",
        _chain_then_repeat([0.0, 1.0, 3.0, 3.0], tail=4.0).__next__,
    )
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
    # Monotonic span unaffected by wall reversal.
    assert first["observed_window_elapsed_seconds"] == pytest.approx(2.0, abs=1e-9)
    assert first["observed_window_elapsed_seconds"] >= 0.0
    # Exact drift value — large negative.
    assert first["wall_clock_drift_seconds"] == pytest.approx(-1_000_002.0, abs=1e-9)
    # UTC endpoints reflect the reversed wall clock (secondary signal).
    assert first["observed_window_start_utc"] == _iso_utc(START_WALL)
    assert first["observed_window_end_utc"] == _iso_utc(END_WALL)
    assert first["observed_window_end_utc"] < first["observed_window_start_utc"]


# ----- 5. Forward wall adjustment: positive drift, UTC endpoints ordered


def test_forward_wall_adjustment_emits_positive_drift_with_endpoints_ordered(
    capsys, monkeypatch
):
    """Wall advances MORE than monotonic during the window (e.g., an
    NTP forward step mid-sample). Drift is positive by the step
    magnitude. UTC endpoints remain correctly ordered (end > start).
    Monotonic elapsed is unchanged.

    Monotonic: start=1, end=3  → elapsed = 2.
    Wall: start=1_000_000, end=1_000_005 → wall delta = 5
    (clock jumped forward by 3 somewhere during the sample).
    Drift = 5 − 2 = +3.0 (exact, positive).
    end_utc > start_utc (forward adjustments preserve ordering).
    """
    from app.observability import event_loop_lag as _ell  # noqa: PLC0415

    monkeypatch.setattr(
        _ell, "_now_mono",
        _chain_then_repeat([0.0, 1.0, 3.0, 3.0], tail=4.0).__next__,
    )
    START_WALL = 1_000_000.0
    END_WALL = 1_000_005.0  # wall advanced 5s; mono advanced only 2s
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
    # Monotonic duration unchanged by the forward wall adjustment.
    assert first["observed_window_elapsed_seconds"] == pytest.approx(2.0, abs=1e-9)
    # Exact drift: 5 − 2 = 3.0 (positive).
    assert first["wall_clock_drift_seconds"] == pytest.approx(3.0, abs=1e-9)
    assert first["wall_clock_drift_seconds"] > 0.0
    # UTC endpoints remain correctly ordered.
    assert first["observed_window_start_utc"] == _iso_utc(START_WALL)
    assert first["observed_window_end_utc"] == _iso_utc(END_WALL)
    assert first["observed_window_end_utc"] > first["observed_window_start_utc"], (
        "forward adjustments preserve UTC ordering; drift alone exposes the shift"
    )


# ----- 6. Backward wall adjustment with endpoints ordered: the key case -


def test_backward_wall_adjustment_with_endpoints_ordered_emits_negative_drift(
    capsys, monkeypatch
):
    """Wall advances LESS than monotonic — the clock stepped BACKWARD
    partway through the sample (e.g., an NTP correction dialed it
    back) but net wall advance is still positive. UTC endpoints
    therefore REMAIN CORRECTLY ORDERED (end > start) and a UTC-only
    reader would see nothing wrong. The drift field is the ONLY
    signal that distinguishes this from a drift-free window.

    This is the case the reviewer specifically asked be covered: UTC
    endpoints are ordered, yet correlation is unreliable because
    wall time disagreed with monotonic during the window.

    Monotonic: start=1, end=6  → elapsed = 5.
    Wall: start=1_000_000, end=1_000_002 → wall delta = 2 (clock went
    backward 3s partway, net wall advance still positive).
    Drift = 2 − 5 = −3.0 (exact, negative, but UTC still ordered).
    """
    from app.observability import event_loop_lag as _ell  # noqa: PLC0415

    monkeypatch.setattr(
        _ell, "_now_mono",
        _chain_then_repeat([0.0, 1.0, 6.0, 6.0], tail=7.0).__next__,
    )
    START_WALL = 1_000_000.0
    END_WALL = 1_000_002.0  # end_wall > start_wall (ordered), but wall delta < mono delta
    monkeypatch.setattr(
        _ell, "_now_wall",
        _chain_then_repeat([START_WALL, END_WALL], tail=END_WALL).__next__,
    )

    asyncio.run(
        _run_until_first_emit_or_timeout(
            emit_interval_s=5.0,
            sample_interval_s=0.001,
            window_samples=1,
            real_timeout_s=0.10,
        )
    )
    events = _parse_event_lines(capsys.readouterr().out)
    assert events

    first = events[0]
    assert first["samples"] == 1
    # Monotonic duration unchanged.
    assert first["observed_window_elapsed_seconds"] == pytest.approx(5.0, abs=1e-9)
    # Exact drift: 2 − 5 = −3.0.
    assert first["wall_clock_drift_seconds"] == pytest.approx(-3.0, abs=1e-9)
    assert first["wall_clock_drift_seconds"] < 0.0
    # UTC endpoints: correctly ordered. This is the whole point —
    # a UTC-only check would NOT flag this emission as suspicious.
    assert first["observed_window_start_utc"] == _iso_utc(START_WALL)
    assert first["observed_window_end_utc"] == _iso_utc(END_WALL)
    assert first["observed_window_end_utc"] > first["observed_window_start_utc"], (
        "backward adjustments can leave UTC endpoints correctly ordered — "
        "exactly the scenario the drift field is designed to surface"
    )
