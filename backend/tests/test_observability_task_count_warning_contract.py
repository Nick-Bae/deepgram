"""Guard SCOPE.md § M2's exact WARNING contract for `asyncio_task_count`:

> WARNING severity ONLY after the asyncio task count has increased by at
> least +10 from a defined BASELINE for 3 CONSECUTIVE samples.

Baseline is a rolling value that resets on recovery (one sample below
baseline + delta). See task_count.py module docstring for the full
state machine.

Each of these cases MUST fail on `7cf47d47` (where the implementation
used a delta_30s-based threshold, not a delta-from-baseline rule) and
pass after the remediation (`cca94471`+).
"""
from __future__ import annotations

import json
import pathlib
import sys
from io import StringIO
from unittest.mock import patch

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _parse_emissions(stdout: str) -> list[dict]:
    rows: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("event") == "asyncio_task_count":
            rows.append(d)
    return rows


def _run_sampler_with_counts(counts: list[int]) -> list[dict]:
    """Stage `counts` as consecutive `len(asyncio.all_tasks())` returns and
    capture the emitted events."""
    from app.observability import task_count as tc

    tc._reset_state_for_tests()
    buf = StringIO()
    with patch("app.observability.task_count.asyncio.all_tasks") as mock_all, \
         patch("sys.stdout", buf):
        mock_all.side_effect = [[None] * n for n in counts]
        for _ in counts:
            tc._task_count_tick()
    return _parse_emissions(buf.getvalue())


def test_no_warning_before_three_consecutive():
    """2 samples at baseline+10, then a sample at baseline+9 (one below).
    Zero WARNINGs. SCOPE.md requires exactly 3-in-a-row."""
    # Baseline = 5 (first sample). Then +10, +10 (above), then +9 (below).
    emissions = _run_sampler_with_counts([5, 15, 15, 14])
    severities = [e["severity"] for e in emissions]
    assert "WARNING" not in severities, (
        f"expected zero WARNINGs across 2-above-then-recovery sequence, "
        f"got {severities}"
    )


def test_warning_on_third_consecutive():
    """3 samples at exactly baseline + 10. First two INFO; third WARNING."""
    # Baseline = 5. Then 15, 15, 15 — all at baseline+10.
    emissions = _run_sampler_with_counts([5, 15, 15, 15])
    severities = [e["severity"] for e in emissions]
    assert severities[0] == "INFO"  # baseline establish
    assert severities[1] == "INFO"  # consecutive_above = 1
    assert severities[2] == "INFO"  # consecutive_above = 2
    assert severities[3] == "WARNING", (
        f"expected WARNING on sample 4 (3rd consecutive above baseline), "
        f"got {severities}"
    )


def test_recovery_resets_state():
    """After a WARNING, feed one sample below baseline+delta.
    `consecutive_above` resets to 0 AND baseline rebases to the recovery
    sample's count."""
    from app.observability import task_count as tc

    tc._reset_state_for_tests()
    buf = StringIO()
    with patch("app.observability.task_count.asyncio.all_tasks") as mock_all, \
         patch("sys.stdout", buf):
        # Baseline establish at 5, then 3 consecutive above (15, 15, 15 →
        # WARNING on the 4th sample), then recovery to 7.
        mock_all.side_effect = [
            [None] * 5,
            [None] * 15,
            [None] * 15,
            [None] * 15,
            [None] * 7,   # recovery — below baseline (5) + delta (10) = 15
        ]
        for _ in range(5):
            tc._task_count_tick()
    emissions = _parse_emissions(buf.getvalue())
    assert emissions[-2]["severity"] == "WARNING"
    assert emissions[-1]["severity"] == "INFO"
    # State introspection: after recovery, baseline should equal the recovery
    # sample's count (7), consecutive_above should be 0.
    assert tc._baseline == 7, f"expected baseline=7 after recovery, got {tc._baseline}"
    assert tc._consecutive_above == 0, (
        f"expected consecutive_above=0 after recovery, got {tc._consecutive_above}"
    )


def test_no_immediate_false_warning_after_reset():
    """After recovery (baseline rebases), feeding 2 more samples at
    new_baseline + 10 must NOT produce a WARNING. The sequence restarts
    and must accumulate 3 above-threshold samples again."""
    # Baseline=5 → above(15,15,15) → WARNING → recovery(7) → new baseline=7
    # Then 2 more above (17, 17 — both at new_baseline+10). ZERO new WARNINGs.
    emissions = _run_sampler_with_counts([5, 15, 15, 15, 7, 17, 17])
    severities = [e["severity"] for e in emissions]
    warning_indices = [i for i, s in enumerate(severities) if s == "WARNING"]
    # Exactly ONE WARNING (at index 3, from the original sequence).
    assert warning_indices == [3], (
        f"expected exactly one WARNING at index 3; got WARNINGs at "
        f"indices {warning_indices}; severities={severities}"
    )


def test_warning_requires_consecutive_not_cumulative():
    """Intersperse: 2 above + 1 below (recovery) + 2 above.
    Zero WARNINGs — only 2 above in each run, counter resets at the
    recovery sample."""
    # Baseline = 5. 15, 15 (2 above), 7 (recovery → new baseline = 7),
    # 17, 17 (2 above new baseline).
    emissions = _run_sampler_with_counts([5, 15, 15, 7, 17, 17])
    severities = [e["severity"] for e in emissions]
    assert "WARNING" not in severities, (
        f"expected ZERO WARNINGs (cumulative-not-consecutive guard), "
        f"got {severities}"
    )
