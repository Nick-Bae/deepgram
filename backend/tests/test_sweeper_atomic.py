"""Regression tests for PR-T1-B (atomic conditional termination in the
sweeper's idle-timeout path).

Locks in the behavior change from `docs/03-analysis/resource-cleanup-audit.md`
§4a.2 and `docs/01-plan/features/resource-cleanup-track-1.plan.md` PR-T1-B:
`end_room(..., require_idle_seconds=N)` may only stage the terminal
write when the room's `lastAudioAt` at commit time still exceeds N.

Evidence is split deliberately along the boundaries the reviewer
called out — the two coverage pieces are distinguishable:

  1. Fast in-memory tests cover the API contract (F-23 stale-read
     race, successful termination, already-ended, non-idle reasons,
     `skipped` result mutates no state) and the sweeper's flow
     (via AST inspection — the `if result.get("skipped"): continue`
     branch must precede all external cleanup effects).

  2. Firestore emulator test covers **only** the property
     "activity committed BEFORE the termination transaction opens
     is respected." A concurrent write committed before the
     transaction's read is what the transactional recheck exists
     to catch. This does NOT prove Firestore's production
     optimistic-concurrency retry: Google explicitly notes the
     emulator uses simplified locking and does not reproduce all
     production concurrency modes.

  3. Controlled retry test uses a patched transactional decorator
     to invoke the callback twice with different reads, proving
     that the callback code (a) rechecks eligibility on each
     invocation, and (b) produces no external side effects
     regardless of how many times it runs. This exercises the
     retry code path deterministically without relying on the
     emulator's simplified concurrency semantics.

Together, (2) and (3) are the reviewer's "two clearly distinguished
pieces of evidence." (1) is the baseline unit coverage.

Locally, run the emulator tests with:

    gcloud emulators firestore start --host-port=127.0.0.1:8085 &
    FIRESTORE_EMULATOR_HOST=127.0.0.1:8085 \\
      GOOGLE_CLOUD_PROJECT=cleanup-track1-emulator \\
      MULTICHURCH_STORE_MODE=firestore \\
      pytest backend/tests/test_sweeper_atomic.py -v

CI wires this automatically in the `firestore-emulator-tests` job.
"""
from __future__ import annotations

import ast
import inspect
import os
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from app.services import multichurch_store as store_mod


def _now():
    return datetime.now(tz=timezone.utc)


def _seed_org_service(store, org_id, service_key):
    """Populate the minimal org + service records end_room reads from."""
    store._orgs[org_id] = {
        "id": org_id,
        "slug": "test-church",
        "name": "Test Church",
        "plan": "starter",
        "billing": {"planKey": "starter"},
        "status": "active",
    }
    store._services[(org_id, service_key)] = {
        "orgId": org_id,
        "serviceKey": service_key,
        "activeRoomId": None,
        "lastRoomId": None,
        "updatedAt": _now(),
    }


def _seed_live_room(store, org_id, room_id, *, service_key="sunday", last_audio_offset_sec=0):
    """Seed a live room whose lastAudioAt is `last_audio_offset_sec` in the past
    (0 = just now, 1000 = 1000 seconds ago)."""
    now = _now()
    last_audio_at = now - timedelta(seconds=last_audio_offset_sec)
    store._rooms[(org_id, room_id)] = {
        "serviceKey": service_key,
        "status": "live",
        "startedAt": now - timedelta(seconds=max(last_audio_offset_sec, 1)),
        "endedAt": None,
        "hostUid": "host-uid",
        "languagePair": {"source": "ko", "target": "en"},
        "listenerCountPeak": 0,
        "billingPeriodKey": "202601",
        "endReason": None,
        "lastAudioAt": last_audio_at,
        "lastUsageTickAt": now - timedelta(seconds=max(last_audio_offset_sec, 1)),
        "finalTranscript": "",
    }
    svc = store._services.get((org_id, service_key))
    if svc is not None:
        svc["activeRoomId"] = room_id


# ---------------------------------------------------------------------------
# (1) API contract, in-memory store
# ---------------------------------------------------------------------------


class InMemoryAtomicIdleTerminationTests(unittest.TestCase):
    def setUp(self):
        self.store = store_mod.InMemoryMultiChurchStore()
        self.org_id = "org-A"
        self.room_id = "room-A"
        _seed_org_service(self.store, self.org_id, "sunday")

    def test_F23_stale_read_race_aborts_termination(self):
        """F-23 (Track 1 variant of the stale-read race).

        Room has fresh audio (0s old). Sweeper — acting on a stale
        earlier read — calls end_room with require_idle_seconds=900.
        The precondition inside the lock/transaction sees fresh
        lastAudioAt, returns skipped=no_longer_idle. Room stays live.
        """
        _seed_live_room(self.store, self.org_id, self.room_id, last_audio_offset_sec=0)
        result = self.store.end_room(
            self.org_id,
            self.room_id,
            reason="idle_timeout",
            require_idle_seconds=900,
        )
        self.assertEqual(result.get("skipped"), "no_longer_idle")
        self.assertLess(result.get("idleSeconds"), 900)
        self.assertEqual(result.get("requiredIdleSeconds"), 900)
        self.assertEqual(
            self.store._rooms[(self.org_id, self.room_id)]["status"],
            "live",
        )

    def test_successful_idle_termination(self):
        """Baseline: a genuinely idle room passes the precondition and
        ends normally, with the same shape non-idle end_room returns."""
        _seed_live_room(self.store, self.org_id, self.room_id, last_audio_offset_sec=1000)
        result = self.store.end_room(
            self.org_id,
            self.room_id,
            reason="idle_timeout",
            require_idle_seconds=900,
        )
        self.assertEqual(result.get("status"), "ended")
        self.assertNotIn("skipped", result)
        self.assertNotIn("alreadyEnded", result)
        self.assertEqual(
            self.store._rooms[(self.org_id, self.room_id)]["status"],
            "ended",
        )

    def test_already_ended_room_returns_alreadyEnded_not_skipped(self):
        """Preserves the alreadyEnded path.

        `alreadyEnded=True` and `skipped=no_longer_idle` are DIFFERENT
        signals to the sweeper. The former still runs external cleanup
        (repair path from a prior failed cleanup); the latter does
        not. This test locks the distinction so a future refactor
        cannot collapse them.
        """
        _seed_live_room(self.store, self.org_id, self.room_id, last_audio_offset_sec=1000)
        first = self.store.end_room(
            self.org_id,
            self.room_id,
            reason="idle_timeout",
            require_idle_seconds=900,
        )
        self.assertEqual(first.get("status"), "ended")
        second = self.store.end_room(
            self.org_id,
            self.room_id,
            reason="idle_timeout",
            require_idle_seconds=900,
        )
        self.assertTrue(second.get("alreadyEnded"))
        self.assertNotIn("skipped", second)

    def test_non_idle_reasons_unaffected(self):
        """End Service, max_duration, and cap enforcement paths pass
        `require_idle_seconds=None`. Behavior must match pre-PR-T1-B
        exactly — a live room with fresh audio still terminates."""
        for reason in ["host_end", "max_duration", "trial_expired", "monthly_limit_reached"]:
            _seed_live_room(self.store, self.org_id, self.room_id, last_audio_offset_sec=0)
            result = self.store.end_room(self.org_id, self.room_id, reason=reason)
            self.assertEqual(
                result.get("status"),
                "ended",
                f"reason={reason} must terminate a fresh-audio room "
                f"when require_idle_seconds is None",
            )
            self.assertNotIn("skipped", result)

    def test_skipped_result_does_not_mutate_room_state(self):
        """The reviewer's verification requirement: a skipped
        termination must change NO fields on the room."""
        _seed_live_room(self.store, self.org_id, self.room_id, last_audio_offset_sec=0)
        before = dict(self.store._rooms[(self.org_id, self.room_id)])
        result = self.store.end_room(
            self.org_id,
            self.room_id,
            reason="idle_timeout",
            require_idle_seconds=900,
        )
        self.assertEqual(result.get("skipped"), "no_longer_idle")
        after = dict(self.store._rooms[(self.org_id, self.room_id)])
        self.assertEqual(
            before,
            after,
            "skipped termination must not mutate room state — every "
            "field must be identical before and after the call.",
        )

    def test_skipped_result_does_not_write_finalTranscript(self):
        """A skipped `end_room` call must not persist `finalTranscript`
        even when the caller supplied one. `transcript` is a payload
        the caller intended to persist alongside a successful
        termination; on a race-aborted call, no persistence should
        happen.

        InMemory: the transcript-write branch is inside the same block
        as the status flip and only runs when the room actually ends.
        The Firestore variant applies the same guard AFTER the tx
        returns (see `did_terminate_now` gate).
        """
        _seed_live_room(self.store, self.org_id, self.room_id, last_audio_offset_sec=0)
        result = self.store.end_room(
            self.org_id,
            self.room_id,
            reason="idle_timeout",
            require_idle_seconds=900,
            transcript="Sunday sermon transcript that must not be persisted on skipped",
        )
        self.assertEqual(result.get("skipped"), "no_longer_idle")
        room = self.store._rooms[(self.org_id, self.room_id)]
        self.assertEqual(
            room.get("finalTranscript"),
            "",
            "finalTranscript was persisted on a skipped call — this "
            "leaks a caller-supplied write past the race-abort guard.",
        )


class StaleLiveRoomsPrecedenceTests(unittest.TestCase):
    """`stale_live_rooms` must return `max_duration` when a room has
    exceeded BOTH thresholds. If it returned `idle_timeout` first,
    the sweeper's idle-recheck could postpone max-duration
    enforcement forever when audio has resumed."""

    def test_max_duration_wins_when_both_thresholds_exceeded(self):
        store = store_mod.InMemoryMultiChurchStore()
        _seed_org_service(store, "org-X", "sunday")
        # Room started far in the past AND has stale audio.
        now = _now()
        store._rooms[("org-X", "room-X")] = {
            "serviceKey": "sunday",
            "status": "live",
            "startedAt": now - timedelta(seconds=10800),  # past max_duration
            "endedAt": None,
            "hostUid": "host-uid",
            "languagePair": {"source": "ko", "target": "en"},
            "listenerCountPeak": 0,
            "billingPeriodKey": "202601",
            "endReason": None,
            "lastAudioAt": now - timedelta(seconds=1000),  # past idle_timeout
            "lastUsageTickAt": now - timedelta(seconds=1000),
            "finalTranscript": "",
        }
        stale = store.stale_live_rooms(idle_seconds=900, max_duration_seconds=3600)
        self.assertEqual(len(stale), 1)
        self.assertEqual(
            stale[0]["reason"],
            "max_duration",
            "when both idle_timeout AND max_duration are exceeded, "
            "stale_live_rooms must return max_duration — otherwise a "
            "room whose audio resumed just before the sweeper fires "
            "could survive past max_duration indefinitely (the idle "
            "recheck would abort each termination). See audit §4a.2.",
        )

    def test_idle_timeout_still_returned_when_max_duration_not_exceeded(self):
        store = store_mod.InMemoryMultiChurchStore()
        _seed_org_service(store, "org-X", "sunday")
        now = _now()
        store._rooms[("org-X", "room-X")] = {
            "serviceKey": "sunday",
            "status": "live",
            "startedAt": now - timedelta(seconds=1200),  # under max_duration
            "endedAt": None,
            "hostUid": "host-uid",
            "languagePair": {"source": "ko", "target": "en"},
            "listenerCountPeak": 0,
            "billingPeriodKey": "202601",
            "endReason": None,
            "lastAudioAt": now - timedelta(seconds=1000),  # past idle_timeout
            "lastUsageTickAt": now - timedelta(seconds=1000),
            "finalTranscript": "",
        }
        stale = store.stale_live_rooms(idle_seconds=900, max_duration_seconds=3600)
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["reason"], "idle_timeout")


# ---------------------------------------------------------------------------
# (1b) Sweeper structural flow
# ---------------------------------------------------------------------------


def _extract_called_name(func_node):
    """Return the callable's name at an ast.Call node, or None."""
    if isinstance(func_node, ast.Name):
        return func_node.id
    if isinstance(func_node, ast.Attribute):
        return func_node.attr
    return None


class SweeperFlowStructuralTests(unittest.TestCase):
    """Structural (AST) checks over the sweeper's cleanup body.

    Behavioral end-to-end coverage of the sweeper loop is heavier —
    it requires an async iteration harness. These structural checks
    lock in the two invariants that behavioral coverage would prove:

    - The sweeper branches on `result.get("skipped")` and `continue`s
      before any external effect fires.
    - The `end_room` transaction callback contains no side-effect
      calls; all external work happens after the transaction returns.
    """

    def test_sweeper_continues_immediately_on_skipped_result(self):
        """AST scan of `_room_sweeper_loop`: the `skipped` branch
        must use `continue`, not fall through to external effects."""
        from app import main as app_main
        source = textwrap.dedent(inspect.getsource(app_main._room_sweeper_loop))
        tree = ast.parse(source)

        # Find every `if result.get("skipped"):` (or equivalent) node.
        found_skipped_branches = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            # Match: `if result.get("skipped")` OR `if result.get("skipped"):`
            test = node.test
            if not isinstance(test, ast.Call):
                continue
            called = _extract_called_name(test.func)
            if called != "get":
                continue
            if not test.args:
                continue
            arg = test.args[0]
            if not isinstance(arg, ast.Constant) or arg.value != "skipped":
                continue
            found_skipped_branches += 1
            # The body must reach a `continue` before any await/call
            # that could be an external effect. Simplest correct
            # check: body contains a `continue` statement.
            has_continue = any(isinstance(n, ast.Continue) for n in ast.walk(node))
            self.assertTrue(
                has_continue,
                "sweeper's `if result.get('skipped'):` branch must "
                "`continue` to skip external cleanup effects — "
                "otherwise a healthy room gets its listeners closed.",
            )
        self.assertGreaterEqual(
            found_skipped_branches,
            1,
            "PR-T1-B added a branch on result.get('skipped') to the "
            "sweeper. If this assertion fails, the branch was removed "
            "and the sweeper will run external effects on a "
            "successfully-race-aborted termination — see audit §4a.2.",
        )

    def test_end_room_transaction_callback_has_no_external_side_effects(self):
        """AST scan of `FirestoreMultiChurchStore.end_room`'s nested
        transaction callback: no calls to close_room_*, broadcast_room,
        forget_room, disconnect, or module-level cleanup helpers.

        External effects fire only in the caller AFTER end_room
        returns. This is the transaction-callback purity guarantee
        that makes Firestore's automatic retry safe: reruns cannot
        cause duplicate socket closes or duplicate Redis publishes.
        """
        source = textwrap.dedent(inspect.getsource(store_mod.FirestoreMultiChurchStore.end_room))
        tree = ast.parse(source)

        # Locate the inner `_tx` function definition — the transaction callback.
        tx_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_tx":
                tx_func = node
                break
        self.assertIsNotNone(
            tx_func,
            "expected `_tx` transaction callback inside end_room; "
            "structure has changed and this test needs updating.",
        )

        # Any callable named here must NOT appear inside the transaction
        # callback. External effects (socket close, Redis publish,
        # in-memory cleanup) run only in the caller after commit.
        # Billing side-effect helpers are also forbidden:
        #   - `_dispatch_soft_cap_email` sends an email; the callback
        #     stashes org data on the returned dict instead, and the
        #     caller dispatches only when did_terminate_now is True.
        #   - `_roll_billing_period_if_needed` on the Firestore store
        #     writes a non-transactional `org_ref.set()` that would
        #     survive an aborted retry. The rollover logic is inlined
        #     into the callback and staged via `transaction.set`.
        FORBIDDEN = {
            "close_room_listeners",
            "close_room_hosts",
            "broadcast_room",
            "forget_room",
            "_cleanup_room_local_state",
            "disconnect",
            "record_deepgram_usage",
            "_dispatch_soft_cap_email",
            "_roll_billing_period_if_needed",
        }
        offenders = []
        for node in ast.walk(tx_func):
            if isinstance(node, ast.Call):
                name = _extract_called_name(node.func)
                if name in FORBIDDEN:
                    offenders.append((node.lineno, name))
        self.assertEqual(
            offenders,
            [],
            f"transaction callback body must be pure Firestore. "
            f"Forbidden calls found: {offenders}. External effects "
            f"belong to the caller — Firestore may rerun this "
            f"callback on retry and side effects would fire multiple "
            f"times (audit §4a.2 transaction callback purity).",
        )


# ---------------------------------------------------------------------------
# (3) Controlled retry test — no emulator, deterministic
# ---------------------------------------------------------------------------


class ControlledRetrySemanticsTests(unittest.TestCase):
    """Force the Firestore transaction decorator to invoke the callback
    with different reads AND exercise the billing-side branches
    (period rollover + soft-cap email) that a real production
    termination touches.

    The reviewer's revision requirements:
    - Separate rollover and email fixtures. A stale-month fixture
      forces rollover but ALSO zeroes `currentMonthMinutes`, so the
      soft-cap email path is unreachable there. A distinct email
      fixture uses the CURRENT month with usage near the cap.
    - Prove the email branch was reached (not merely absent by
      trivial fixture construction).
    - Use separate transaction mocks per attempt so staged writes
      are attributable.
    - Prove the successful case dispatches exactly one email.
    """

    def _make_room_snapshot(self, *, status, last_audio_at, last_usage_tick_at=None):
        """Room snapshot. `last_usage_tick_at` defaults to `last_audio_at`
        so a stale-audio room is also billable for the elapsed period."""
        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {
            "serviceKey": "sunday",
            "status": status,
            "startedAt": last_audio_at - timedelta(seconds=1),
            "hostUid": "host-uid",
            "lastAudioAt": last_audio_at,
            "lastUsageTickAt": last_usage_tick_at or last_audio_at,
        }
        return snap

    def _rollover_org_snapshot(self):
        """Rollover fixture — stale month key, forces billing period
        roll. Soft-cap email is NOT expected here because rollover
        zeroes `currentMonthMinutes` and the ~17 min room delta stays
        under the 100-minute cap."""
        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {
            "id": "org-rollover",
            "plan": "starter",
            "billing": {"planKey": "starter"},
            "billingLimitsEnabled": True,
            "maxMinutesPerMonth": 100,
            "currentMonthMinutes": 99,
            "currentMonthKey": "199901",  # stale — triggers rollover
            "hardCapReached": False,
            "softCapReached": False,
        }
        return snap

    def _email_org_snapshot(self):
        """Email fixture — CURRENT month key so rollover does NOT
        zero `currentMonthMinutes`. Usage at 99/100; any positive
        elapsed-minutes delta pushes total ≥ cap and (with plan !=
        trial and softCapReached=False) triggers the soft-cap email
        path.

        The month key must match `_yyyymm(now)` at test-run time.
        Computed dynamically so this fixture is not a time bomb.
        """
        current_key = f"{_now().year:04d}{_now().month:02d}"
        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {
            "id": "org-email",
            "plan": "starter",
            "billing": {"planKey": "starter"},
            "billingLimitsEnabled": True,
            "maxMinutesPerMonth": 100,
            "currentMonthMinutes": 99,
            "currentMonthKey": current_key,  # current — no rollover
            "hardCapReached": False,
            "softCapReached": False,
        }
        return snap

    def _make_store_with_mocks(
        self,
        *,
        room_snapshots,
        org_snapshot,
    ):
        """Build a FirestoreMultiChurchStore shell whose Firestore
        references are mocks. Each `db.transaction()` call returns a
        FRESH transaction mock, so per-attempt writes can be
        attributed.

        Returns (store, org_ref, transactions_used).
        """
        room_snaps = iter(room_snapshots)
        room_ref = MagicMock()
        room_ref.get.side_effect = lambda *a, **k: next(room_snaps)

        org_ref = MagicMock()
        org_ref.get.return_value = org_snapshot

        service_ref = MagicMock()
        service_ref.get.return_value = MagicMock(exists=False)
        usage_ref = MagicMock()

        transactions_used = []

        def make_new_transaction():
            tx = MagicMock(name=f"transaction-{len(transactions_used)}")
            transactions_used.append(tx)
            return tx

        fake_db = MagicMock()
        fake_db.transaction.side_effect = make_new_transaction

        store = store_mod.FirestoreMultiChurchStore.__new__(
            store_mod.FirestoreMultiChurchStore
        )
        store._db = fake_db
        store._room_ref = lambda org_id, room_id: room_ref
        store._service_ref = lambda org_id, service_key: service_ref
        store._org_ref = lambda org_id: org_ref
        store._usage_ref = lambda org_id, period: usage_ref
        return store, org_ref, transactions_used, make_new_transaction

    def _run_with_forced_retry(
        self,
        store,
        make_new_transaction,
        *,
        invocations,
        org_id,
        room_id,
        require_idle_seconds,
    ):
        """Invoke `store.end_room` with a patched transactional
        decorator that runs the callback `invocations` times, each
        with a fresh transaction mock. Return (result, per_attempt_results,
        invocation_count).
        """
        per_attempt_results = []

        def controlled_transactional(fn):
            def wrapper(transaction):
                nonlocal_first_tx = transaction
                for i in range(invocations):
                    if i > 0:
                        nonlocal_first_tx = make_new_transaction()
                    outcome = fn(nonlocal_first_tx)
                    per_attempt_results.append(outcome)
                return per_attempt_results[-1]
            return wrapper

        with patch.object(
            store_mod.gcf_firestore, "transactional", controlled_transactional
        ), patch.object(store_mod, "_dispatch_soft_cap_email") as email_spy:
            final = store.end_room(
                org_id,
                room_id,
                reason="idle_timeout",
                require_idle_seconds=require_idle_seconds,
            )
        return final, per_attempt_results, email_spy

    # -------------------------------------------------------------------
    # (A) Rollover fixture: prove staged-via-transaction rollover write
    # -------------------------------------------------------------------

    def test_rollover_fixture_stages_write_via_transaction_no_direct_org_set(self):
        """Stale month key forces rollover. The rollover write MUST
        be staged via `transaction.set(org_ref, ...)`, not via a
        direct `org_ref.set(...)`. Zero direct writes are permitted.
        """
        now = _now()
        room_snapshots = [
            self._make_room_snapshot(
                status="live",
                last_audio_at=now - timedelta(seconds=1000),
            ),
        ]
        store, org_ref, transactions_used, mk_tx = self._make_store_with_mocks(
            room_snapshots=room_snapshots,
            org_snapshot=self._rollover_org_snapshot(),
        )
        final, per_attempt, email_spy = self._run_with_forced_retry(
            store, mk_tx,
            invocations=1,
            org_id="org-rollover",
            room_id="room-rollover",
            require_idle_seconds=900,
        )
        # Room ended successfully; no email under this fixture.
        self.assertEqual(final.get("status"), "ended")
        self.assertEqual(email_spy.call_count, 0)
        # No direct writes to org_ref outside the transaction.
        direct_org_writes = [c for c in org_ref.method_calls if c[0] == "set"]
        self.assertEqual(
            direct_org_writes,
            [],
            f"rollover MUST be staged via transaction.set — direct "
            f"org_ref.set calls: {direct_org_writes}",
        )
        # First attempt's transaction MUST have received a set(org_ref, ...)
        # call carrying the rollover fields.
        self.assertEqual(len(transactions_used), 1)
        first_tx_sets = [
            c for c in transactions_used[0].method_calls if c[0] == "set"
        ]
        rollover_set = None
        for call in first_tx_sets:
            _, args, _ = call
            if len(args) >= 2 and args[0] is org_ref:
                update = args[1]
                if "currentMonthKey" in update:
                    rollover_set = update
                    break
        self.assertIsNotNone(
            rollover_set,
            "transaction did not stage a rollover write against "
            "org_ref — see transactions_used[0].method_calls.",
        )
        # Rollover reset `currentMonthMinutes` to 0, then the cap-add
        # block layered the ~17-minute room-usage delta on top. The
        # final staged value is that delta, not zero. What we're
        # asserting here is that the rollover *fields* landed:
        current_key = f"{_now().year:04d}{_now().month:02d}"
        self.assertEqual(
            rollover_set["currentMonthKey"],
            current_key,
            "rollover must write the CURRENT month key, replacing "
            "the stale '199901' from the fixture.",
        )
        # currentMonthMinutes should reflect JUST the new-period usage
        # (rollover reset then usage-add), well under the cap.
        self.assertLess(
            rollover_set["currentMonthMinutes"],
            100,
            f"post-rollover usage should be well under the 100-minute "
            f"cap; got {rollover_set['currentMonthMinutes']}",
        )
        # softCapReached must stay False under this fixture (rollover
        # cleared it and the added delta didn't cross the cap).
        self.assertFalse(rollover_set["softCapReached"])
        self.assertFalse(rollover_set["hardCapReached"])

    # -------------------------------------------------------------------
    # (B) Email fixture: prove branch was reached, then skipped on retry
    # -------------------------------------------------------------------

    def test_email_fixture_first_attempt_reaches_branch_then_skipped_dispatches_zero(self):
        """First invocation reads stale audio + email-fixture org:
        the callback reaches the soft-cap email branch and returns
        with `_pending_soft_cap_email_org` in its result payload
        (proves the branch was reached, not trivially bypassed).

        Second invocation reads fresh audio: returns skipped. The
        caller sees the second result. No email dispatched.
        """
        now = _now()
        room_snapshots = [
            # First read: stale audio, would terminate + reach cap.
            self._make_room_snapshot(
                status="live",
                last_audio_at=now - timedelta(seconds=1000),
            ),
            # Second read: fresh audio — must return skipped.
            self._make_room_snapshot(
                status="live",
                last_audio_at=now,
            ),
        ]
        store, org_ref, transactions_used, mk_tx = self._make_store_with_mocks(
            room_snapshots=room_snapshots,
            org_snapshot=self._email_org_snapshot(),
        )
        final, per_attempt, email_spy = self._run_with_forced_retry(
            store, mk_tx,
            invocations=2,
            org_id="org-email",
            room_id="room-email",
            require_idle_seconds=900,
        )
        # First attempt: proves the email branch was reached — the
        # callback captured pending-email data before returning.
        self.assertEqual(len(per_attempt), 2)
        self.assertIn(
            "_pending_soft_cap_email_org",
            per_attempt[0],
            "first attempt did NOT reach the soft-cap email branch — "
            "the email fixture is misconstructed. Without this, "
            "asserting `email_spy.call_count == 0` is trivial and does "
            "not demonstrate suppression of a pending email.",
        )
        # Second attempt: skipped.
        self.assertEqual(per_attempt[1].get("skipped"), "no_longer_idle")
        self.assertNotIn("_pending_soft_cap_email_org", per_attempt[1])
        # Final result is the second attempt's outcome.
        self.assertEqual(final.get("skipped"), "no_longer_idle")
        self.assertNotIn("_pending_soft_cap_email_org", final)
        # No email dispatched.
        self.assertEqual(
            email_spy.call_count,
            0,
            f"_dispatch_soft_cap_email fired {email_spy.call_count} "
            f"time(s) on a skipped result despite the branch having "
            f"been reached on the first attempt. This is the exact "
            f"regression PR-T1-B must prevent.",
        )
        # First attempt STAGED writes; second attempt (skipped)
        # staged NONE. Prove via the fresh-per-attempt transaction mocks.
        self.assertEqual(len(transactions_used), 2)
        first_tx_sets = [c for c in transactions_used[0].method_calls if c[0] == "set"]
        second_tx_sets = [c for c in transactions_used[1].method_calls if c[0] == "set"]
        self.assertGreater(
            len(first_tx_sets),
            0,
            "first attempt reached the write staging phase — its "
            "transaction should carry ≥ 1 set(...) call.",
        )
        self.assertEqual(
            second_tx_sets,
            [],
            f"skipped second attempt must stage zero writes — got "
            f"{second_tx_sets}. If the second transaction saw ANY "
            f"set(...) call, the skip guard failed and writes leaked.",
        )
        # No direct writes to org_ref outside the transaction.
        direct_org_writes = [c for c in org_ref.method_calls if c[0] == "set"]
        self.assertEqual(direct_org_writes, [])

    # -------------------------------------------------------------------
    # (C) Email fixture, successful termination: exactly one email
    # -------------------------------------------------------------------

    def test_email_fixture_successful_termination_dispatches_exactly_one_email(self):
        """Under identical fixture but no retry (both reads stale),
        the callback commits and the caller dispatches exactly ONE
        email. Confirms the pending-email pathway is correctly wired
        — proves the previous test's "zero emails" assertion is
        meaningful because this test shows it CAN dispatch when
        commit succeeds.
        """
        now = _now()
        room_snapshots = [
            self._make_room_snapshot(
                status="live",
                last_audio_at=now - timedelta(seconds=1000),
            ),
        ]
        store, org_ref, transactions_used, mk_tx = self._make_store_with_mocks(
            room_snapshots=room_snapshots,
            org_snapshot=self._email_org_snapshot(),
        )
        final, per_attempt, email_spy = self._run_with_forced_retry(
            store, mk_tx,
            invocations=1,
            org_id="org-email",
            room_id="room-email",
            require_idle_seconds=900,
        )
        self.assertEqual(final.get("status"), "ended")
        self.assertEqual(
            email_spy.call_count,
            1,
            f"expected exactly one soft-cap email on successful "
            f"termination; got {email_spy.call_count}. "
            f"per_attempt={per_attempt}",
        )
        # The internal signal must not leak to callers.
        self.assertNotIn("_pending_soft_cap_email_org", final)


# ---------------------------------------------------------------------------
# (2) Firestore emulator — activity committed BEFORE the transaction opens
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    os.getenv("FIRESTORE_EMULATOR_HOST"),
    "Firestore emulator required — set FIRESTORE_EMULATOR_HOST and "
    "MULTICHURCH_STORE_MODE=firestore. See the module docstring.",
)
class FirestoreAtomicIdleTerminationTests(unittest.TestCase):
    """Real-Firestore-emulator coverage. Scope is deliberately narrow:

    Proves that when a fresh `lastAudioAt` value is committed to
    Firestore BEFORE the termination transaction opens, the transactional
    recheck sees the fresh value and returns `skipped`. The room stays
    live.

    This is NOT a proof of Firestore's production optimistic-concurrency
    retry behavior. Google's Firestore emulator documentation notes it
    uses simplified locking and does not reproduce all production
    concurrency semantics. Controlled-retry evidence lives in
    `ControlledRetrySemanticsTests`, which is deterministic.

    See docs/03-analysis/resource-cleanup-audit.md §4a.2.
    """

    def setUp(self):
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "cleanup-track1-emulator")
        os.environ["MULTICHURCH_STORE_MODE"] = "firestore"
        import importlib
        importlib.reload(store_mod)
        self.store = store_mod.FirestoreMultiChurchStore()
        self.org_id = f"org-test-{os.urandom(4).hex()}"
        self.room_id = f"room-test-{os.urandom(4).hex()}"

    def _write_live_room(self, *, last_audio_offset_sec):
        now = datetime.now(tz=timezone.utc)
        last_audio_at = now - timedelta(seconds=last_audio_offset_sec)
        self.store._room_ref(self.org_id, self.room_id).set({
            "serviceKey": "sunday",
            "status": "live",
            "startedAt": now - timedelta(seconds=max(last_audio_offset_sec, 1)),
            "hostUid": "host-uid",
            "lastAudioAt": last_audio_at,
            "endedAt": None,
            "endReason": None,
        })

    def test_activity_committed_before_transaction_prevents_termination(self):
        """Sweeper reads room X — audio was stale at that moment.
        Between the sweeper's decision and end_room's transaction
        opening, a different writer commits touch_audio (fresh
        lastAudioAt). end_room's transaction reads the CURRENT room
        state (fresh audio) and returns skipped.
        """
        # Step 1: seed a room with stale audio (as the sweeper's earlier
        # read would have seen).
        self._write_live_room(last_audio_offset_sec=1000)
        # Step 2: a separate writer commits a fresh touch_audio — this
        # is the "activity committed after candidate selection" event.
        self.store.touch_audio(self.org_id, self.room_id)
        # Step 3: sweeper calls end_room with the idle precondition. The
        # transaction opens, reads the fresh lastAudioAt, returns skipped.
        result = self.store.end_room(
            self.org_id,
            self.room_id,
            reason="idle_timeout",
            require_idle_seconds=60,
        )
        self.assertEqual(result.get("skipped"), "no_longer_idle")
        snap = self.store._room_ref(self.org_id, self.room_id).get()
        self.assertEqual(snap.to_dict().get("status"), "live")

    def test_skipped_result_does_not_write_finalTranscript_firestore(self):
        """Reviewer's Firestore-side transcript guard test.

        Supply `transcript` on a call that will be race-aborted by
        the idle precondition. The Firestore path writes transcripts
        via `room_ref.collection("finalTranscript").document("latest").set(...)`
        AFTER the transaction returns. That write is gated on
        `did_terminate_now`, so a skipped result must produce NO
        finalTranscript document. The InMemory test cannot catch a
        regression in this Firestore-side guard — this one does.
        """
        # Step 1: fresh-audio room. Sweeper's earlier read had it
        # stale; between then and now, audio resumed.
        self._write_live_room(last_audio_offset_sec=0)
        # Step 2: end_room with idle precondition + transcript payload.
        result = self.store.end_room(
            self.org_id,
            self.room_id,
            reason="idle_timeout",
            require_idle_seconds=60,
            transcript="Sunday sermon transcript that must not be persisted on skipped",
        )
        # Skipped as expected.
        self.assertEqual(result.get("skipped"), "no_longer_idle")
        # Now verify no finalTranscript document was written. The
        # Firestore emulator returns a doc snapshot with exists=False
        # if it was never written.
        transcript_snap = (
            self.store._room_ref(self.org_id, self.room_id)
            .collection("finalTranscript")
            .document("latest")
            .get()
        )
        self.assertFalse(
            transcript_snap.exists,
            "finalTranscript/latest was persisted despite the "
            "termination being race-aborted (skipped). This leaks a "
            "caller-supplied write past the did_terminate_now guard "
            "in Firestore end_room.",
        )


if __name__ == "__main__":
    unittest.main()
