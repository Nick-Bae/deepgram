"""Shared helpers for the parallel-claims-convergence contract.

Not a test module: filename does not start with `test_` so pytest
does not collect it.

The sole function here is `apply_parallel_claims_contract`. It is
used by two call sites:

- The canonical CI test `test_actual_parallel_claims_converge` in
  `test_billing_webhook_claim_firestore.py` (which runs under the
  real Firestore emulator — module-top guard raises SkipTest when
  `FIRESTORE_EMULATOR_HOST` is unset).

- The deterministic coverage tests in
  `test_billing_parallel_claims_contract.py` (which run WITHOUT
  the emulator; `claim_checkout_attempt` is mocked to inject
  specific worker-outcome patterns).

Both call sites gather the same three structures:

  - `results`: list of outcome dicts from the four worker threads.
    Each successful outcome must satisfy
    `action in {"create", "reuse"}` AND `attemptId` is a nonempty
    string. The recognized SDK retry-exhaustion sentinel is
    `{"action": "retry_exhausted", "cause": "Aborted"}` and does
    NOT carry an attemptId.
  - `exhausted_count`: integer count of threads that terminated
    with the recognized retry-exhaustion signal.
  - `unexpected_errors`: list of exceptions that escaped a worker
    thread. Any non-empty list fails the test loudly.

The contract these inputs must satisfy is enumerated below.
"""
from __future__ import annotations

import unittest
from typing import Any, Dict, List


def apply_parallel_claims_contract(
    testcase: unittest.TestCase,
    results: List[Dict[str, Any]],
    exhausted_count: int,
    unexpected_errors: List[BaseException],
) -> None:
    """Apply the parallel-claims convergence contract.

    Invariants, in order:

      0. No unexpected exception escaped a worker thread.
      1. Every worker produced an observable outcome
         (`len(results) == 4`).
      2. Every result in `results` carries `action` in
         `{"create", "reuse", "retry_exhausted"}`. No other
         action values are accepted.
      3. Every successful result (`action in {"create", "reuse"}`)
         carries `attemptId` as a nonempty `str`. A successful
         result with missing, empty, or non-string `attemptId`
         is NOT a valid success and fails the test loudly.
      4. At most one `create` among successful outcomes.
      5. All successful outcomes converge on exactly one
         `attemptId` value (if any successful outcomes exist).
      6. The `exhausted_count` argument equals the number of
         `retry_exhausted` entries in `results`.
      7. Strict happy-path conditional: if `exhausted_count == 0`,
         then exactly one `create`, exactly three `reuse`s, and
         exactly one shared `attemptId` — identical to the
         pre-contract canonical assertion. The preserved original
         method body at
         `tmp/stripe-sandbox/merge-readiness/parallel-claims-flake-diagnosis-20261102/`
         `test_actual_parallel_claims_converge-ORIGINAL-at-dfee0d70.py`
         is the byte-for-byte reference.

    All assertions are strict; no `pytest.skip`, `pytest.xfail`,
    or softened-matcher is used. Any contract violation fails
    the test.
    """
    # Invariant 0.
    testcase.assertFalse(
        unexpected_errors,
        "unexpected worker exceptions escaped: "
        + repr([(type(e).__name__, str(e)) for e in unexpected_errors]),
    )

    # Invariant 1.
    testcase.assertEqual(
        len(results), 4,
        "every worker thread must produce an observable outcome "
        "(success or recognized retry-exhausted sentinel); "
        f"got {len(results)} / 4. exhausted_count={exhausted_count}; "
        f"results={results!r}",
    )

    # Invariant 2.
    valid_actions = {"create", "reuse", "retry_exhausted"}
    for r in results:
        testcase.assertIn(
            r.get("action"), valid_actions,
            f"invalid result action: {r!r}; "
            f"must be one of {sorted(valid_actions)}",
        )

    # Partition after action-validation.
    creates = [r for r in results if r.get("action") == "create"]
    reuses = [r for r in results if r.get("action") == "reuse"]
    exhausted = [r for r in results if r.get("action") == "retry_exhausted"]
    successful = creates + reuses

    # Invariant 3. Each successful result must carry a nonempty
    # string attemptId. Missing / empty / non-string is NOT a
    # valid success.
    for r in successful:
        attempt_id = r.get("attemptId")
        testcase.assertIsInstance(
            attempt_id, str,
            f"successful result missing or non-string attemptId: {r!r}",
        )
        testcase.assertTrue(
            attempt_id,
            f"successful result has empty attemptId: {r!r}",
        )

    # Invariant 4.
    testcase.assertLessEqual(
        len(creates), 1,
        f"at most one `create` allowed among successful outcomes; "
        f"got {len(creates)}. results={results!r}",
    )

    # Invariant 5.
    if successful:
        unique_ids = {r["attemptId"] for r in successful}
        testcase.assertEqual(
            len(unique_ids), 1,
            "successful outcomes must converge on exactly ONE attemptId; "
            f"got {sorted(unique_ids)}. results={results!r}",
        )

    # Invariant 6.
    testcase.assertEqual(
        exhausted_count, len(exhausted),
        f"exhausted_count={exhausted_count} does not match the number "
        f"of retry_exhausted entries in results ({len(exhausted)}). "
        f"results={results!r}",
    )

    # Invariant 7 (strict happy path).
    if exhausted_count == 0:
        testcase.assertEqual(
            len(creates), 1,
            f"happy path: exactly one `create` expected; results={results!r}",
        )
        testcase.assertEqual(
            len(reuses), 3,
            f"happy path: exactly three `reuse`s expected; results={results!r}",
        )
        testcase.assertEqual(
            len(successful), 4,
            f"happy path: all four workers must succeed; results={results!r}",
        )
