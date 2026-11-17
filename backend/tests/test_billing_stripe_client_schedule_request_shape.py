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
    can scrutinize the exact bytes going to Stripe. A queued response
    may be a dict (returned as-is) OR an Exception instance (raised),
    letting tests exercise Stripe-side failure paths."""

    def __init__(self) -> None:
        super().__init__(secret_key="sk_test_capture")
        self.calls: List[Dict[str, Any]] = []
        self.responses: List[Any] = []

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
        nxt = self.responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


class CreateSubscriptionScheduleRequestShape(TestCase):

    # Fixed active-phase start_date used by every test in this file.
    # Stripe's `from_subscription` creation form always echoes the
    # subscription's `current_period_start` as `phases[0].start_date`
    # on the step-1 response.
    STEP1_ACTIVE_START = 1699_900_000

    def setUp(self) -> None:
        self.client = _CapturingClient()
        # Queue: step-1 response (schedule created, phases[0] echoes
        # the subscription's active phase), step-2 response (updated).
        self.client.responses = [
            {"id": "sub_sched_test_abc", "status": "not_started",
             "phases": [{"start_date": self.STEP1_ACTIVE_START,
                         "end_date": 1700_000_000}]},
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

    def test_step_two_phases_zero_start_date_matches_step_one_active_phase(self) -> None:
        """Operator-mandated (post-sandbox-finding): Stripe's
        `from_subscription` creation form echoes the subscription's
        active phase as `phases[0]` on the step-1 response. The
        step-2 update MUST set `phases[0][start_date]` to that same
        timestamp — otherwise Stripe either rejects the update or
        drifts the active phase off the subscription's
        `current_period_start`, which is what happened in sandbox
        run 20261117t1415z (first change_plan 502 with Stripe-side
        schedule attached but step 2 definitively failed).
        """
        self.client.create_subscription_schedule(
            subscription_id="sub_demo_123",
            current_price_id="price_growth_test",
            new_price_id="price_starter_test",
            phase_end_unix=1700_000_000,
            metadata={"orgId": "org-under-test"},
        )
        step2 = self.client.calls[1]
        self.assertEqual(
            step2["data"].get("phases[0][start_date]"),
            str(self.STEP1_ACTIVE_START),
            "step 2 must carry the step-1 active phase's start_date "
            "unchanged so phases[0] stays contiguous with the "
            "subscription's current period",
        )
        self.assertEqual(
            step2["data"].get("phases[0][end_date]"),
            str(1700_000_000),
            "step 2's phases[0][end_date] is the caller's intended "
            "downgrade boundary; preserved verbatim",
        )

    def test_step_two_definitive_400_invalid_request_reproduces_sandbox_finding_and_releases(self) -> None:
        """Reproduces the exact sandbox-run signal from 20261117t1415z:
        Stripe HTTP 400 with `error.type == invalid_request_error`
        for 'phases[0][start_date] must be present'. The client
        releases the step-1 schedule (payload-validation rejection,
        not applied) and re-raises the ORIGINAL 400, unchanged."""
        from app.billing.stripe_client import StripeClientError

        step2_error = StripeClientError(
            "stripe_http_400:phases[0][start_date] must be present",
            http_status=400,
            stripe_error_type="invalid_request_error",
            stripe_error_code="parameter_missing",
        )
        self.client.responses = [
            {"id": "sub_sched_fail_xyz", "status": "not_started",
             "phases": [{"start_date": self.STEP1_ACTIVE_START, "end_date": 1700_000_000}]},
            step2_error,
            {"id": "sub_sched_fail_xyz", "status": "released"},
        ]
        with self.assertRaises(StripeClientError) as cm:
            self.client.create_subscription_schedule(
                subscription_id="sub_demo_fail",
                current_price_id="p_growth", new_price_id="p_starter",
                phase_end_unix=1700_000_000, metadata={"orgId": "org-under-test"},
            )
        self.assertIs(cm.exception, step2_error,
                      "caller sees the original 400 — never masked by compensation")
        self.assertEqual(len(self.client.calls), 3,
                         "exactly three calls: step-1 create, step-2 update, release")
        release = self.client.calls[2]
        self.assertEqual(release["method"], "POST")
        self.assertEqual(
            release["path"], "/subscription_schedules/sub_sched_fail_xyz/release",
            "release must target ONLY the schedule id returned by this "
            "operation's step 1 — never a prior / ambient / externally-attached schedule",
        )

    def test_step_two_409_state_conflict_preserves_schedule_and_logs_id(self) -> None:
        """409 means Stripe sees a state conflict (e.g. the sub had a
        schedule attached between step 1 and step 2, or an
        idempotency clash). Releasing would risk clobbering work we
        don't own. Preserve the schedule; log its id for operator
        reconciliation."""
        from app.billing.stripe_client import StripeClientError

        step2_error = StripeClientError(
            "stripe_http_409:a resource is in an unexpected state",
            http_status=409,
            stripe_error_type="invalid_request_error",
            stripe_error_code="resource_already_exists",
        )
        self.client.responses = [
            {"id": "sub_sched_409_ccc", "status": "not_started",
             "phases": [{"start_date": self.STEP1_ACTIVE_START, "end_date": 1700_000_000}]},
            step2_error,
        ]
        with self.assertLogs("app.billing.stripe_client", level="WARNING") as cap:
            with self.assertRaises(StripeClientError) as cm:
                self.client.create_subscription_schedule(
                    subscription_id="sub_demo_409",
                    current_price_id="p_growth", new_price_id="p_starter",
                    phase_end_unix=1700_000_000,
                )
        self.assertIs(cm.exception, step2_error)
        self.assertEqual(
            len(self.client.calls), 2,
            "409 must NOT trigger a release — releasing can clobber a "
            "concurrent writer's schedule or a legitimately-attached one",
        )
        joined = "\n".join(cap.output)
        self.assertIn("stripe_schedule_update_uncertain", joined)
        self.assertIn("sub_sched_409_ccc", joined)
        self.assertIn("http_status=409", joined)
        self.assertIn("compensation release SKIPPED", joined)

    def test_step_two_429_lock_timeout_preserves_schedule_and_logs_id(self) -> None:
        """429 with `error.code == lock_timeout` means Stripe
        contended on a database lock; the update may have reached a
        point where retry collisions are possible. Preserve; log."""
        from app.billing.stripe_client import StripeClientError

        step2_error = StripeClientError(
            "stripe_http_429:lock timeout, please retry",
            http_status=429,
            stripe_error_type="api_error",
            stripe_error_code="lock_timeout",
        )
        self.client.responses = [
            {"id": "sub_sched_429_ddd", "status": "not_started",
             "phases": [{"start_date": self.STEP1_ACTIVE_START, "end_date": 1700_000_000}]},
            step2_error,
        ]
        with self.assertLogs("app.billing.stripe_client", level="WARNING") as cap:
            with self.assertRaises(StripeClientError) as cm:
                self.client.create_subscription_schedule(
                    subscription_id="sub_demo_429",
                    current_price_id="p_growth", new_price_id="p_starter",
                    phase_end_unix=1700_000_000,
                )
        self.assertIs(cm.exception, step2_error)
        self.assertEqual(
            len(self.client.calls), 2,
            "429 lock_timeout must NOT trigger a release — operator "
            "reconciles out-of-band",
        )
        joined = "\n".join(cap.output)
        self.assertIn("stripe_schedule_update_uncertain", joined)
        self.assertIn("sub_sched_429_ddd", joined)
        self.assertIn("err_code=lock_timeout", joined)

    def test_step_two_untyped_400_preserves_schedule_and_logs_id(self) -> None:
        """A 400 WITHOUT `error.type` is uncertain — the response
        didn't carry Stripe's own classification, so we can't tell
        whether our payload was definitively rejected or transformed
        and applied by an intermediary. Preserve the schedule; log
        the id for operator reconciliation."""
        from app.billing.stripe_client import StripeClientError

        step2_error = StripeClientError(
            "stripe_http_400:bad request",
            http_status=400,
            stripe_error_type=None,
            stripe_error_code=None,
        )
        self.client.responses = [
            {"id": "sub_sched_untyped400_fff", "status": "not_started",
             "phases": [{"start_date": self.STEP1_ACTIVE_START, "end_date": 1700_000_000}]},
            step2_error,
        ]
        with self.assertLogs("app.billing.stripe_client", level="WARNING") as cap:
            with self.assertRaises(StripeClientError) as cm:
                self.client.create_subscription_schedule(
                    subscription_id="sub_demo_untyped400",
                    current_price_id="p_growth", new_price_id="p_starter",
                    phase_end_unix=1700_000_000,
                )
        self.assertIs(cm.exception, step2_error)
        self.assertEqual(
            len(self.client.calls), 2,
            "untyped 400 must NOT trigger a release — without Stripe's "
            "error.type we can't classify the failure as a payload-"
            "validation rejection",
        )
        joined = "\n".join(cap.output)
        self.assertIn("stripe_schedule_update_uncertain", joined)
        self.assertIn("sub_sched_untyped400_fff", joined)
        self.assertIn("http_status=400", joined)
        self.assertIn("err_type=None", joined)
        self.assertIn("compensation release SKIPPED", joined)

    def test_step_two_401_authentication_error_preserves_schedule(self) -> None:
        """A 401 isn't a payload-validation rejection. Preserve; log."""
        from app.billing.stripe_client import StripeClientError

        step2_error = StripeClientError(
            "stripe_http_401:invalid api key",
            http_status=401,
            stripe_error_type="authentication_error",
        )
        self.client.responses = [
            {"id": "sub_sched_401_eee", "status": "not_started",
             "phases": [{"start_date": self.STEP1_ACTIVE_START, "end_date": 1700_000_000}]},
            step2_error,
        ]
        with self.assertRaises(StripeClientError) as cm:
            self.client.create_subscription_schedule(
                subscription_id="sub_demo_401",
                current_price_id="p_growth", new_price_id="p_starter",
                phase_end_unix=1700_000_000,
            )
        self.assertIs(cm.exception, step2_error)
        self.assertEqual(len(self.client.calls), 2, "no release on 401")

    def test_step_two_5xx_does_not_release_because_update_may_have_been_applied(self) -> None:
        """A 5xx response is UNCERTAIN — Stripe may have applied the
        phase update server-side while the client never saw the
        success response. Releasing the schedule would discard a
        potentially-successful update. The client must NOT release;
        it must log the schedule id for reconciliation and re-raise
        the original 5xx so the caller sees the real cause."""
        from app.billing.stripe_client import StripeClientError

        step2_error = StripeClientError(
            "stripe_http_503:service unavailable",
            http_status=503,
            stripe_error_type="api_error",
        )
        self.client.responses = [
            {"id": "sub_sched_5xx_aaa", "status": "not_started",
             "phases": [{"start_date": self.STEP1_ACTIVE_START, "end_date": 1700_000_000}]},
            step2_error,
        ]
        with self.assertLogs("app.billing.stripe_client", level="WARNING") as cap:
            with self.assertRaises(StripeClientError) as cm:
                self.client.create_subscription_schedule(
                    subscription_id="sub_demo_5xx",
                    current_price_id="p_growth", new_price_id="p_starter",
                    phase_end_unix=1700_000_000,
                )
        self.assertIs(cm.exception, step2_error)
        self.assertEqual(len(self.client.calls), 2,
                         "no release call — only step-1 create + step-2 update attempt")
        joined = "\n".join(cap.output)
        self.assertIn("stripe_schedule_update_uncertain", joined)
        self.assertIn("sub_sched_5xx_aaa", joined)
        self.assertIn("compensation release SKIPPED", joined)

    def test_step_two_network_timeout_does_not_release_because_update_may_have_been_applied(self) -> None:
        """Same uncertainty argument as 5xx: a network error means we
        don't know what happened server-side. Do NOT release; log the
        schedule id; re-raise the network error. No http_status/type
        fields available — pure transport layer."""
        from app.billing.stripe_client import StripeClientError

        step2_error = StripeClientError("stripe_network_error:TimeoutException")
        self.client.responses = [
            {"id": "sub_sched_net_bbb", "status": "not_started",
             "phases": [{"start_date": self.STEP1_ACTIVE_START, "end_date": 1700_000_000}]},
            step2_error,
        ]
        with self.assertLogs("app.billing.stripe_client", level="WARNING") as cap:
            with self.assertRaises(StripeClientError) as cm:
                self.client.create_subscription_schedule(
                    subscription_id="sub_demo_net",
                    current_price_id="p_growth", new_price_id="p_starter",
                    phase_end_unix=1700_000_000,
                )
        self.assertIs(cm.exception, step2_error)
        self.assertEqual(len(self.client.calls), 2, "no release call on network error")
        joined = "\n".join(cap.output)
        self.assertIn("stripe_schedule_update_uncertain", joined)
        self.assertIn("sub_sched_net_bbb", joined)

    def test_step_two_definitive_failure_when_release_also_fails_logs_schedule_id_and_raises_original(self) -> None:
        """Pathological: Stripe rejects step 2 with 400
        invalid_request_error, AND the compensation release itself
        errors. The client swallows the release error and still
        re-raises the original step-2 error — BUT the schedule id is
        logged so an operator can reconcile the now-orphaned attached
        schedule out-of-band."""
        from app.billing.stripe_client import StripeClientError

        step2_error = StripeClientError(
            "stripe_http_400:phases[0][start_date] must be present",
            http_status=400,
            stripe_error_type="invalid_request_error",
            stripe_error_code="parameter_missing",
        )
        release_error = StripeClientError("stripe_network_error:TimeoutException")
        self.client.responses = [
            {"id": "sub_sched_fail_xyz", "status": "not_started",
             "phases": [{"start_date": self.STEP1_ACTIVE_START, "end_date": 1700_000_000}]},
            step2_error,
            release_error,
        ]
        with self.assertLogs("app.billing.stripe_client", level="WARNING") as cap:
            with self.assertRaises(StripeClientError) as cm:
                self.client.create_subscription_schedule(
                    subscription_id="sub_demo_fail",
                    current_price_id="p_growth", new_price_id="p_starter",
                    phase_end_unix=1700_000_000,
                )
        self.assertIs(cm.exception, step2_error,
                      "release failure must NEVER mask the step-2 error — "
                      "the caller needs the real root cause")
        joined = "\n".join(cap.output)
        self.assertIn("stripe_schedule_compensation_release_failed", joined,
                      "release-failure log must identify the orphan schedule")
        self.assertIn("sub_sched_fail_xyz", joined)

    def test_step_one_response_without_phase_start_date_releases_and_raises_before_step_two(self) -> None:
        """Defense in depth: if Stripe's step-1 response somehow
        comes back without a `phases[0].start_date` (we've never seen
        this against real Stripe, but the API shape doesn't formally
        forbid it), the client refuses to guess the start_date,
        releases the just-created schedule, and raises so the caller
        can retry from a clean state."""
        from app.billing.stripe_client import StripeClientError

        self.client.responses = [
            {"id": "sub_sched_nostart_qqq", "status": "not_started",
             "phases": [{"end_date": 1700_000_000}]},  # start_date absent
            {"id": "sub_sched_nostart_qqq", "status": "released"},
        ]
        with self.assertRaises(StripeClientError) as cm:
            self.client.create_subscription_schedule(
                subscription_id="sub_demo_nostart",
                current_price_id="p_growth", new_price_id="p_starter",
                phase_end_unix=1700_000_000,
            )
        self.assertEqual(
            str(cm.exception), "stripe_schedule_create_missing_active_start_date",
            "distinct error string so the backend can surface a specific 502 subtype",
        )
        self.assertEqual(len(self.client.calls), 2, "step-1 create + release only; no step-2 update")
        self.assertEqual(
            self.client.calls[1]["path"],
            "/subscription_schedules/sub_sched_nostart_qqq/release",
        )
