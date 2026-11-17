"""Unit test for `StripeBillingClient.create_subscription_schedule`
request shape — specifically the operator-review requirement:

  1. Step 1 (`POST /v1/subscription_schedules`) must send
     `from_subscription` BY ITSELF — Stripe rejects additional
     fields (phases, metadata) on that creation form.
  2. Step 2 (`POST /v1/subscription_schedules/<id>`) is where
     phases AND metadata land.

A regression that bundled metadata or phases into step 1 would
pass the current fake-client tests (which don't scrutinize the
request shape) but would 400 against real Stripe.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest import TestCase
from unittest.mock import patch

from app.billing.stripe_client import StripeBillingClient


class _CapturingClient(StripeBillingClient):
    """Captures each `_request` call's (method, path, data) so tests
    can scrutinize the exact bytes going to Stripe."""

    def __init__(self) -> None:
        super().__init__(secret_key="sk_test_capture")
        self.calls: List[Dict[str, Any]] = []
        self.responses: List[Dict[str, Any]] = []

    def _request(  # type: ignore[override]
        self,
        method: str,
        path: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.calls.append({
            "method": method, "path": path, "data": dict(data or {}),
            "params": dict(params or {}), "idempotency_key": idempotency_key,
        })
        if not self.responses:
            raise AssertionError("no queued response for _request call")
        return self.responses.pop(0)


class CreateSubscriptionScheduleRequestShape(TestCase):

    def setUp(self) -> None:
        self.client = _CapturingClient()
        # Queue: step-1 response (schedule created), step-2 response (updated).
        self.client.responses = [
            {"id": "sub_sched_test_abc", "status": "not_started"},
            {"id": "sub_sched_test_abc", "status": "active",
             "phases": [{}, {}], "metadata": {"orgId": "org-under-test"}},
        ]

    def test_step_one_sends_from_subscription_alone(self) -> None:
        """Operator-mandated: Stripe rejects anything beyond
        `from_subscription` on the creation form. Guard against a
        future refactor that tries to bundle metadata or phases on
        step 1."""
        self.client.create_subscription_schedule(
            subscription_id="sub_demo_123",
            current_price_id="price_growth_test",
            new_price_id="price_starter_test",
            phase_end_unix=1700_000_000,
            metadata={"orgId": "org-under-test"},
        )
        # Two calls total.
        self.assertEqual(len(self.client.calls), 2, "expected two Stripe calls")

        step1 = self.client.calls[0]
        self.assertEqual(step1["method"], "POST")
        self.assertEqual(step1["path"], "/subscription_schedules")
        # Step 1 data MUST be exactly {"from_subscription": "sub_demo_123"}.
        self.assertEqual(step1["data"], {"from_subscription": "sub_demo_123"})
        # No metadata or phases on step 1.
        for forbidden in ("end_behavior", "metadata"):
            self.assertNotIn(
                forbidden, step1["data"],
                f"step 1 must NOT include '{forbidden}' — Stripe rejects additional fields on the from_subscription creation form",
            )
        # No "phases[" prefix on any step-1 key.
        bad_phase_keys = [k for k in step1["data"] if k.startswith("phases[")]
        self.assertEqual(bad_phase_keys, [], "step 1 must NOT include phases")
        # No metadata prefix either.
        bad_meta_keys = [k for k in step1["data"] if k.startswith("metadata[")]
        self.assertEqual(bad_meta_keys, [], "step 1 must NOT include metadata")

    def test_step_two_carries_phases_and_metadata_on_the_update(self) -> None:
        """Step 2 (`POST /subscription_schedules/<id>`) is where
        phases AND metadata are applied."""
        self.client.create_subscription_schedule(
            subscription_id="sub_demo_123",
            current_price_id="price_growth_test",
            new_price_id="price_starter_test",
            phase_end_unix=1700_000_000,
            metadata={"orgId": "org-under-test"},
        )
        step2 = self.client.calls[1]
        self.assertEqual(step2["method"], "POST")
        self.assertEqual(step2["path"], "/subscription_schedules/sub_sched_test_abc")

        # Phase shape.
        self.assertEqual(step2["data"].get("end_behavior"), "release")
        self.assertEqual(step2["data"].get("phases[0][items][0][price]"), "price_growth_test")
        self.assertEqual(step2["data"].get("phases[0][items][0][quantity]"), "1")
        self.assertEqual(step2["data"].get("phases[0][end_date]"), str(1700_000_000))
        self.assertEqual(step2["data"].get("phases[1][items][0][price]"), "price_starter_test")
        self.assertEqual(step2["data"].get("phases[1][items][0][quantity]"), "1")

        # Metadata is applied in step 2.
        self.assertEqual(step2["data"].get("metadata[orgId]"), "org-under-test")

    def test_no_metadata_kwarg_still_sends_from_subscription_alone_on_step_one(self) -> None:
        """When the caller passes no metadata, step 1's data is
        still `from_subscription` alone, and step 2 simply omits the
        metadata keys."""
        self.client.create_subscription_schedule(
            subscription_id="sub_demo_456",
            current_price_id="price_growth_test",
            new_price_id="price_starter_test",
            phase_end_unix=1700_000_000,
            # metadata omitted
        )
        step1 = self.client.calls[0]
        self.assertEqual(step1["data"], {"from_subscription": "sub_demo_456"})
        step2 = self.client.calls[1]
        meta_keys = [k for k in step2["data"] if k.startswith("metadata[")]
        self.assertEqual(meta_keys, [], "no metadata keys when the kwarg is omitted")

    def test_step_one_create_response_missing_id_raises_without_firing_step_two(self) -> None:
        """If Stripe's step-1 response is missing an `id` field, the
        helper raises BEFORE issuing step 2."""
        from app.billing.stripe_client import StripeClientError  # local import for test clarity

        self.client.responses = [{"status": "not_started"}]  # no id
        with self.assertRaises(StripeClientError):
            self.client.create_subscription_schedule(
                subscription_id="sub_demo_err",
                current_price_id="p_growth", new_price_id="p_starter",
                phase_end_unix=1700_000_000, metadata={"orgId": "x"},
            )
        self.assertEqual(len(self.client.calls), 1, "step 2 must not fire when step 1's response is malformed")
