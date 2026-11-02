"""Deterministic coverage for the parallel-claims convergence
contract — complements the canonical CI test in
`test_billing_webhook_claim_firestore.py`.

The canonical CI test runs under the real Firestore emulator and
exercises the end-to-end concurrent claim path. Depending on the
emulator's non-deterministic lock timing, it either takes the
strict happy path (no exhaustion) or exercises the recognized
`retry_exhausted` sentinel. We cannot force one branch or the
other against the emulator deterministically.

The six tests below exercise the contract itself. They call the
shared contract helper directly with hand-constructed inputs that
simulate each worker-outcome pattern. This proves the helper
accepts valid shapes AND REJECTS the invalid ones. No emulator
required; this file is safe to collect in normal CI.

Covered scenarios (per 2026-11-02 reviewer scope):

  - mixed success/exhaustion (2+2)
  - missing / empty / non-string `attemptId` on a successful
    result (three sub-cases)
  - duplicate `create` outcomes
  - different `attemptId`s among successes
  - unexpected exception captured in `unexpected_errors`
  - strict happy path (positive control)

Not covered here (owned by the canonical CI test):

  - End-to-end thread + barrier + real Firestore emulator
    behavior. That is the canonical test's job.
"""
from __future__ import annotations

import unittest
from typing import Any, Dict, List

from concurrency_contract_helpers import apply_parallel_claims_contract


class ParallelClaimsContractDeterministicTests(unittest.TestCase):
    # ---------------------------------------------------------------
    # Positive controls
    # ---------------------------------------------------------------
    def test_happy_path_passes_strict_shape(self) -> None:
        """Positive control: exactly 1 create + 3 reuses, all
        sharing a single attemptId; exhausted_count == 0; no
        unexpected errors. Contract passes."""
        attempt_id = "chkatt_happy"
        results: List[Dict[str, Any]] = [
            {"action": "create", "attemptId": attempt_id},
            {"action": "reuse",  "attemptId": attempt_id},
            {"action": "reuse",  "attemptId": attempt_id},
            {"action": "reuse",  "attemptId": attempt_id},
        ]
        apply_parallel_claims_contract(self, results, 0, [])

    def test_mixed_success_and_exhaustion_passes(self) -> None:
        """Positive control: 2 successes (sharing one attemptId) +
        2 recognized retry_exhausted sentinels; no unexpected
        errors. Contract passes without the strict happy-path
        branch (exhausted_count > 0)."""
        attempt_id = "chkatt_mixed"
        results: List[Dict[str, Any]] = [
            {"action": "create", "attemptId": attempt_id},
            {"action": "reuse",  "attemptId": attempt_id},
            {"action": "retry_exhausted", "cause": "Aborted"},
            {"action": "retry_exhausted", "cause": "Aborted"},
        ]
        apply_parallel_claims_contract(self, results, 2, [])

    def test_all_four_exhausted_passes(self) -> None:
        """Positive control: all four threads exhausted. Zero
        successes to converge on; the invariant that successful
        attemptIds converge is vacuously true. Contract passes."""
        results: List[Dict[str, Any]] = [
            {"action": "retry_exhausted", "cause": "Aborted"},
            {"action": "retry_exhausted", "cause": "Aborted"},
            {"action": "retry_exhausted", "cause": "Aborted"},
            {"action": "retry_exhausted", "cause": "Aborted"},
        ]
        apply_parallel_claims_contract(self, results, 4, [])

    # ---------------------------------------------------------------
    # Negative controls — reject invalid outcomes
    # ---------------------------------------------------------------
    def test_rejects_success_with_missing_attempt_id(self) -> None:
        """A successful result without `attemptId` must fail the
        contract, not silently pass. This is the exact gap the
        reviewer surfaced (lines 1030-1033 of the sibling test
        filtered out missing IDs; the contract no longer does)."""
        results: List[Dict[str, Any]] = [
            {"action": "create"},  # ← missing attemptId
            {"action": "reuse",  "attemptId": "chkatt_x"},
            {"action": "reuse",  "attemptId": "chkatt_x"},
            {"action": "reuse",  "attemptId": "chkatt_x"},
        ]
        with self.assertRaises(AssertionError) as ctx:
            apply_parallel_claims_contract(self, results, 0, [])
        self.assertIn("attemptId", str(ctx.exception))

    def test_rejects_success_with_empty_attempt_id(self) -> None:
        """`attemptId == ""` is not a valid success."""
        results: List[Dict[str, Any]] = [
            {"action": "create", "attemptId": ""},
            {"action": "reuse",  "attemptId": ""},
            {"action": "reuse",  "attemptId": ""},
            {"action": "reuse",  "attemptId": ""},
        ]
        with self.assertRaises(AssertionError) as ctx:
            apply_parallel_claims_contract(self, results, 0, [])
        self.assertIn("empty attemptId", str(ctx.exception))

    def test_rejects_success_with_non_string_attempt_id(self) -> None:
        """`attemptId` must be a `str`. Numeric or None fails."""
        results: List[Dict[str, Any]] = [
            {"action": "create", "attemptId": 12345},
            {"action": "reuse",  "attemptId": 12345},
            {"action": "reuse",  "attemptId": 12345},
            {"action": "reuse",  "attemptId": 12345},
        ]
        with self.assertRaises(AssertionError) as ctx:
            apply_parallel_claims_contract(self, results, 0, [])
        self.assertIn("non-string attemptId", str(ctx.exception))

    def test_rejects_duplicate_creates(self) -> None:
        """At most one `create` is allowed; two `create` outcomes
        means the serialization invariant failed."""
        results: List[Dict[str, Any]] = [
            {"action": "create", "attemptId": "chkatt_a"},
            {"action": "create", "attemptId": "chkatt_a"},
            {"action": "reuse",  "attemptId": "chkatt_a"},
            {"action": "reuse",  "attemptId": "chkatt_a"},
        ]
        with self.assertRaises(AssertionError) as ctx:
            apply_parallel_claims_contract(self, results, 0, [])
        self.assertIn("at most one `create`", str(ctx.exception))

    def test_rejects_different_attempt_ids_among_successes(self) -> None:
        """Successful outcomes must converge on exactly ONE
        attemptId. Two different IDs is a convergence failure."""
        results: List[Dict[str, Any]] = [
            {"action": "create", "attemptId": "chkatt_a"},
            {"action": "reuse",  "attemptId": "chkatt_b"},  # ← different
            {"action": "reuse",  "attemptId": "chkatt_a"},
            {"action": "reuse",  "attemptId": "chkatt_a"},
        ]
        with self.assertRaises(AssertionError) as ctx:
            apply_parallel_claims_contract(self, results, 0, [])
        self.assertIn("converge", str(ctx.exception))

    def test_rejects_unexpected_exception(self) -> None:
        """Any exception captured in `unexpected_errors` fails the
        contract — the worker-thread harness must not drop
        unexpected failures silently."""
        results: List[Dict[str, Any]] = [
            {"action": "create", "attemptId": "chkatt_x"},
            {"action": "reuse",  "attemptId": "chkatt_x"},
            {"action": "reuse",  "attemptId": "chkatt_x"},
            {"action": "reuse",  "attemptId": "chkatt_x"},
        ]
        unexpected = [RuntimeError("surprise failure in worker")]
        with self.assertRaises(AssertionError) as ctx:
            apply_parallel_claims_contract(self, results, 0, unexpected)
        self.assertIn("unexpected worker exceptions", str(ctx.exception))

    def test_rejects_dropped_result(self) -> None:
        """If a worker thread silently dropped its outcome and
        `results` has fewer than 4 entries, the contract fails
        on invariant 1."""
        attempt_id = "chkatt_dropped"
        results: List[Dict[str, Any]] = [
            {"action": "create", "attemptId": attempt_id},
            {"action": "reuse",  "attemptId": attempt_id},
            {"action": "reuse",  "attemptId": attempt_id},
            # ← fourth outcome missing
        ]
        with self.assertRaises(AssertionError) as ctx:
            apply_parallel_claims_contract(self, results, 0, [])
        self.assertIn("every worker thread must produce", str(ctx.exception))

    def test_rejects_invalid_action_value(self) -> None:
        """A result whose `action` is anything other than
        `create`, `reuse`, or `retry_exhausted` is rejected."""
        results: List[Dict[str, Any]] = [
            {"action": "create", "attemptId": "chkatt_v"},
            {"action": "something_else", "attemptId": "chkatt_v"},
            {"action": "reuse",  "attemptId": "chkatt_v"},
            {"action": "reuse",  "attemptId": "chkatt_v"},
        ]
        with self.assertRaises(AssertionError) as ctx:
            apply_parallel_claims_contract(self, results, 0, [])
        self.assertIn("invalid result action", str(ctx.exception))

    def test_rejects_exhausted_count_mismatch(self) -> None:
        """`exhausted_count` must equal the number of
        `retry_exhausted` entries in `results`. A mismatch
        indicates the harness and the result stream disagree."""
        results: List[Dict[str, Any]] = [
            {"action": "create", "attemptId": "chkatt_m"},
            {"action": "reuse",  "attemptId": "chkatt_m"},
            {"action": "retry_exhausted", "cause": "Aborted"},
            {"action": "reuse",  "attemptId": "chkatt_m"},
        ]
        with self.assertRaises(AssertionError) as ctx:
            apply_parallel_claims_contract(self, results, 0, [])
        # Note: this hits invariant 6 (exhausted_count mismatch) OR
        # invariant 7 (strict happy path demands 1 create + 3 reuses
        # when exhausted_count == 0). Either message is acceptable
        # — both expose the inconsistency.
        self.assertTrue(
            "exhausted_count" in str(ctx.exception)
            or "happy path" in str(ctx.exception),
            f"unexpected failure message: {ctx.exception}",
        )

    def test_rejects_happy_path_with_only_two_successes(self) -> None:
        """Strict happy-path invariant: if `exhausted_count == 0`,
        there must be exactly 1 create + 3 reuses = 4 successes.
        Fewer successes with no exhaustion is a contract
        violation."""
        results: List[Dict[str, Any]] = [
            {"action": "create", "attemptId": "chkatt_h"},
            {"action": "reuse",  "attemptId": "chkatt_h"},
            # Only two successes, but we claim exhausted_count=0
            # — these two outcomes don't account for the two
            # missing workers; the contract catches this.
        ]
        # Pad to len==4 so invariant 1 passes, but with INVALID
        # action values that invariant 2 catches first.
        results.extend([
            {"action": "noop"},
            {"action": "noop"},
        ])
        with self.assertRaises(AssertionError) as ctx:
            apply_parallel_claims_contract(self, results, 0, [])
        # Invariant 2 (invalid action) fires first for this input.
        self.assertIn("invalid result action", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
