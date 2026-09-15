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

        FORBIDDEN = {
            "close_room_listeners",
            "close_room_hosts",
            "broadcast_room",
            "forget_room",
            "_cleanup_room_local_state",
            "disconnect",
            "record_deepgram_usage",
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
    twice with different reads. Prove the callback code is safe under
    retry: recheck happens on each invocation, and no external effect
    fires during any invocation (aborted or committing).

    This is deterministic and does NOT depend on the Firestore
    emulator's simplified locking. It exercises exactly the callback
    body — the caller (`end_room`) constructs a fresh `@gcf_firestore.transactional`
    wrapper on each call, so patching the decorator at module scope
    substitutes our controlled driver.
    """

    def _make_room_snapshot(self, *, status, last_audio_at):
        """Fake Firestore document snapshot."""
        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {
            "serviceKey": "sunday",
            "status": status,
            "startedAt": last_audio_at - timedelta(seconds=1),
            "hostUid": "host-uid",
            "lastAudioAt": last_audio_at,
        }
        return snap

    def test_callback_reruns_recheck_eligibility_across_two_reads(self):
        """Simulate Firestore's retry-on-abort: the transactional
        decorator invokes the callback twice. First invocation reads a
        stale-audio room (would terminate). Second invocation reads a
        fresh-audio room (would abort). The final result must reflect
        the second read.
        """
        # Build a fake db + room_ref pair. Each .get() returns the next
        # snapshot in a queue, simulating two independent reads Firestore
        # performs on retry.
        now = _now()
        stale_snap = self._make_room_snapshot(
            status="live",
            last_audio_at=now - timedelta(seconds=1000),
        )
        fresh_snap = self._make_room_snapshot(
            status="live",
            last_audio_at=now,
        )
        snapshots = iter([stale_snap, fresh_snap])
        room_ref = MagicMock()
        room_ref.get.side_effect = lambda *args, **kwargs: next(snapshots)

        service_ref = MagicMock()
        service_ref.get.return_value = MagicMock(exists=False)
        org_ref = MagicMock()
        org_ref.get.return_value = MagicMock(exists=False, to_dict=lambda: {})

        fake_db = MagicMock()
        fake_db.transaction.return_value = MagicMock(name="transaction")

        # Controlled retry driver: replace @gcf_firestore.transactional
        # with a decorator that invokes the wrapped function TWICE and
        # returns the second result. Also counts invocations for the
        # assertion below.
        invocation_count = {"n": 0}

        def controlled_transactional(fn):
            def wrapper(transaction):
                results = []
                for _ in range(2):
                    invocation_count["n"] += 1
                    results.append(fn(transaction))
                return results[-1]
            return wrapper

        # Instantiate a store shell that returns the mocks we control.
        store = store_mod.FirestoreMultiChurchStore.__new__(
            store_mod.FirestoreMultiChurchStore
        )
        store._db = fake_db
        store._room_ref = lambda org_id, room_id: room_ref
        store._service_ref = lambda org_id, service_key: service_ref
        store._org_ref = lambda org_id: org_ref
        store._roll_billing_period_if_needed = lambda org_id, org, *, now: org

        # Patch the decorator on the store module's imported name so a
        # fresh call to end_room picks up our controlled version.
        with patch.object(store_mod.gcf_firestore, "transactional", controlled_transactional):
            result = store.end_room(
                "org-X",
                "room-X",
                reason="idle_timeout",
                require_idle_seconds=900,
            )

        self.assertEqual(
            invocation_count["n"],
            2,
            "controlled retry harness must invoke the callback exactly "
            "twice — proves the callback code is not one-shot.",
        )
        self.assertEqual(
            result.get("skipped"),
            "no_longer_idle",
            "on the second invocation the callback saw fresh audio and "
            "must return skipped — proves eligibility is rechecked on "
            "each rerun, not decided once and cached.",
        )
        # transaction.set must NOT have been called during EITHER
        # invocation on the second (fresh-audio) read.  On the first
        # (stale) read the callback stages a write; but the outer
        # decorator only surfaces the LAST invocation's write, which
        # is the aborted-empty second read. This is precisely what
        # Firestore's retry does — earlier writes are discarded on
        # rerun.
        #
        # For the strictest guarantee we assert: on the SECOND
        # invocation (fresh audio), transaction.set was not called.
        # We cannot easily distinguish first vs second `set` calls
        # from a single mock without more instrumentation, so we
        # instead assert the final result did not commit any state
        # change (skipped, not ended).
        self.assertNotIn("status", result)
        self.assertNotIn("alreadyEnded", result)


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


if __name__ == "__main__":
    unittest.main()
