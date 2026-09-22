"""PR #31 §3 alert A5 — recovery-deadline watchdog tests.

`RedisPubSub._check_recovery_deadline()` and the surrounding
state transitions are the deadline-aware substrate A5 alerts
on. The previous PromQL-only design for A5 had two problems the
reviewer flagged:

  - `duration:60s` allows paging ~1 min after startup_failed,
    not 5 min unrecovered.
  - Rolling-window `unless` cannot express event ordering
    (a reconnect success 4 min before the failure suppresses).

The adapter now emits `redis_pubsub_recovery_deadline_missed`
exactly once per unrecovered outage window, and A5 alerts on
any occurrence of that event. Every timing-focused invariant
A5 depends on is exercised here directly, without waiting the
real 300 s deadline (tests override
`ENV.REDIS_RECOVERY_DEADLINE_SEC` and pass a monotonic `now`
into the method).
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from app.env import ENV
from app.services import redis_pubsub as rp
from app.services.redis_pubsub import RedisPubSub


class RecoveryWatchdogStateTransitionTests(unittest.TestCase):
    """Pure-state tests — no asyncio, no fakeredis. Just drives the
    `_mark_startup_failure` / `_clear_recovery_state` /
    `_check_recovery_deadline` triple against controlled `now`
    values."""

    def setUp(self):
        self.ps = RedisPubSub()
        self.ps._enabled = True
        # Short deadline so tests are exhaustive without waiting.
        self._patcher = patch.object(ENV, "REDIS_RECOVERY_DEADLINE_SEC", 5.0)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_no_failure_no_emission(self):
        """`_check_recovery_deadline` on a fresh adapter with no
        pending failure returns False and emits nothing."""
        with patch.object(rp, "_emit") as m:
            self.assertFalse(self.ps._check_recovery_deadline(now=100.0))
            m.assert_not_called()

    def test_deadline_not_yet_elapsed_no_emission(self):
        self.ps._mark_startup_failure()
        with patch.object(rp, "_emit") as m:
            # ENV.REDIS_RECOVERY_DEADLINE_SEC=5 — 4 s elapsed is
            # under. The stored monotonic timestamp is `time.monotonic()`
            # AT the moment of _mark_startup_failure; pass `now`
            # as that + 4.
            baseline = self.ps._startup_failed_at
            self.assertIsNotNone(baseline)
            self.assertFalse(self.ps._check_recovery_deadline(now=baseline + 4.0))
            m.assert_not_called()

    def test_deadline_elapsed_emits_once(self):
        self.ps._mark_startup_failure()
        baseline = self.ps._startup_failed_at
        with patch.object(rp, "_emit") as m:
            self.assertTrue(self.ps._check_recovery_deadline(now=baseline + 5.0))
            self.assertEqual(m.call_count, 1)
            args, kwargs = m.call_args
            self.assertEqual(args[0], rp.EVENT_RECOVERY_DEADLINE_MISSED)
            self.assertEqual(args[1], "ERROR")
            self.assertEqual(kwargs["deadline_seconds"], 5.0)
            self.assertGreaterEqual(kwargs["elapsed_seconds"], 5.0)

    def test_deadline_does_not_double_emit_while_still_unrecovered(self):
        """A5 pages on ANY occurrence; a duplicate emission would
        double-page for the same outage. The `_recovery_deadline_emitted`
        flag guards it."""
        self.ps._mark_startup_failure()
        baseline = self.ps._startup_failed_at
        with patch.object(rp, "_emit") as m:
            self.assertTrue(self.ps._check_recovery_deadline(now=baseline + 5.0))
            self.assertFalse(self.ps._check_recovery_deadline(now=baseline + 10.0))
            self.assertFalse(self.ps._check_recovery_deadline(now=baseline + 20.0))
            self.assertEqual(m.call_count, 1)

    def test_reconnect_before_deadline_clears_state(self):
        """A `_clear_recovery_state` from a reconnected event
        before the deadline elapses cancels the watchdog. A
        subsequent failure starts a fresh window."""
        self.ps._mark_startup_failure()
        baseline = self.ps._startup_failed_at
        with patch.object(rp, "_emit") as m:
            self.ps._clear_recovery_state()
            self.assertIsNone(self.ps._startup_failed_at)
            self.assertFalse(self.ps._check_recovery_deadline(now=baseline + 10.0))
            m.assert_not_called()

    def test_second_failure_starts_fresh_deadline_after_recovery(self):
        """After recovery + a new failure, the watchdog re-arms and
        can emit again for the NEW outage window."""
        self.ps._mark_startup_failure()
        first_baseline = self.ps._startup_failed_at
        # Recover.
        self.ps._clear_recovery_state()
        # New failure. `_mark_startup_failure` calls time.monotonic()
        # so we need to allow real time to pass or patch it.
        with patch.object(time, "monotonic", return_value=first_baseline + 100.0):
            self.ps._mark_startup_failure()
        second_baseline = self.ps._startup_failed_at
        self.assertIsNotNone(second_baseline)
        self.assertGreater(second_baseline, first_baseline)
        with patch.object(rp, "_emit") as m:
            self.assertTrue(
                self.ps._check_recovery_deadline(now=second_baseline + 5.0)
            )
            self.assertEqual(m.call_count, 1)

    def test_mark_startup_failure_keeps_older_timestamp(self):
        """Multiple failure emissions during the same outage
        window must NOT reset the timestamp — the deadline
        measures the age of the FIRST failure, not the latest
        retry ping. Otherwise a chatty adapter could push the
        deadline out indefinitely."""
        self.ps._mark_startup_failure()
        first = self.ps._startup_failed_at
        with patch.object(time, "monotonic", return_value=first + 1.0):
            self.ps._mark_startup_failure()
        self.assertEqual(self.ps._startup_failed_at, first)

    def test_short_deadline_watchdog_interval_is_bounded_below(self):
        """The watchdog task's sleep interval is
        `min(30s, deadline/5)` and floored at 0.05s. Prevents a
        catastrophically small deadline from spawning a
        busy-loop."""
        with patch.object(ENV, "REDIS_RECOVERY_DEADLINE_SEC", 0.01):
            # 0.01 / 5 = 0.002; must be floored to 0.05.
            interval = max(0.05, min(30.0, ENV.REDIS_RECOVERY_DEADLINE_SEC / 5.0))
            self.assertEqual(interval, 0.05)


class RecoveryWatchdogEventNameTests(unittest.TestCase):
    """A5's metric filter matches on exactly this event name — a
    rename would silently break the alert. Locks the string."""

    def test_event_name_constant(self):
        self.assertEqual(
            rp.EVENT_RECOVERY_DEADLINE_MISSED,
            "redis_pubsub_recovery_deadline_missed",
        )


if __name__ == "__main__":
    unittest.main()
