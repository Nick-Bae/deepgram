from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import httpx


logger = logging.getLogger(__name__)


class StripeClientError(RuntimeError):
    """Raised for every non-2xx Stripe response and every transport
    failure. When Stripe returned a structured error body, the
    optional attributes `http_status`, `stripe_error_type`, and
    `stripe_error_code` carry those fields so callers can gate
    compensation behavior precisely."""

    def __init__(
        self,
        message: str,
        *,
        http_status: Optional[int] = None,
        stripe_error_type: Optional[str] = None,
        stripe_error_code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.stripe_error_type = stripe_error_type
        self.stripe_error_code = stripe_error_code


def _is_schedule_update_validation_rejection(exc: StripeClientError) -> bool:
    """Narrow compensation gate for `create_subscription_schedule`'s
    step-2 phase-update failure.

    Release ONLY when Stripe definitively rejected the payload itself:
    HTTP 400 with `error.type == invalid_request_error` (the observed
    sandbox-run shape — "phases[0][start_date] must be present" is a
    parameter-missing validation rejection). The request was not
    applied; the just-created schedule is safe to release so a retry
    can proceed.

    Preserve the schedule (NO release; log the id for reconciliation)
    on every other failure mode:

      - 409 (state conflict): the sub may have had a schedule
        attached between step 1 and step 2, or Stripe sees an
        idempotency or state conflict. Releasing would clobber work
        we don't own.
      - 429 (rate-limit / `lock_timeout`): Stripe returned before
        applying the update, but a subsequent retry may collide
        with a write that landed. Releasing here can race with a
        concurrent write.
      - 401 / 403 / 404 and any non-`invalid_request_error` 400:
        not a payload-validation rejection. Treat as uncertain.
      - 5xx / network / invalid-JSON: Stripe may have applied the
        update server-side. Never release — releasing a schedule
        whose update actually took effect would silently discard
        that work.

    Structured fields are used when the server supplied them; the
    gate falls back to HTTP status alone only if the type field is
    absent (older Stripe responses, test harnesses).
    """
    # `lock_timeout` is always uncertain regardless of HTTP status.
    if exc.stripe_error_code == "lock_timeout":
        return False
    if exc.http_status != 400:
        return False
    # Require the explicit `invalid_request_error` type. An untyped
    # 400 (missing structured type field — malformed/intercepted
    # response, older Stripe-compat shim, a non-Stripe proxy) is
    # uncertain: without Stripe's own classification we can't tell
    # whether the payload was rejected or transformed-then-applied.
    # Preserve the schedule; the operator reconciles out-of-band.
    if exc.stripe_error_type != "invalid_request_error":
        return False
    return True


class StripeBillingClient:
    def __init__(self, *, secret_key: str, api_base: str = "https://api.stripe.com/v1", timeout_seconds: float = 15.0) -> None:
        self._secret_key = (secret_key or "").strip()
        self._api_base = (api_base or "https://api.stripe.com/v1").rstrip("/")
        self._timeout = max(3.0, float(timeout_seconds))

    # Pin to a specific Stripe API version to prevent silent breakage on upstream upgrades.
    _STRIPE_API_VERSION = "2023-10-16"

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self._secret_key:
            raise StripeClientError("stripe_secret_key_missing")
        url = f"{self._api_base}{path}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                headers: Dict[str, str] = {
                    "Authorization": f"Bearer {self._secret_key}",
                    "Stripe-Version": self._STRIPE_API_VERSION,
                }
                if idempotency_key:
                    headers["Idempotency-Key"] = idempotency_key
                request_kwargs: Dict[str, Any] = {
                    "headers": headers,
                }
                if data:
                    request_kwargs["data"] = data
                if params:
                    request_kwargs["params"] = params
                res = client.request(
                    method.upper(),
                    url,
                    **request_kwargs,
                )
        except Exception as exc:  # pragma: no cover - network layer
            raise StripeClientError(f"stripe_network_error:{type(exc).__name__}") from exc
        if res.status_code >= 400:
            detail = ""
            err_type: Optional[str] = None
            err_code: Optional[str] = None
            try:
                payload = res.json()
                err_obj = (payload or {}).get("error") or {}
                detail = str(err_obj.get("message") or "")
                err_type = (
                    str(err_obj["type"]) if err_obj.get("type") else None
                )
                err_code = (
                    str(err_obj["code"]) if err_obj.get("code") else None
                )
            except Exception:
                detail = (res.text or "").strip()
            raise StripeClientError(
                f"stripe_http_{res.status_code}:{detail[:200]}",
                http_status=res.status_code,
                stripe_error_type=err_type,
                stripe_error_code=err_code,
            )
        try:
            return res.json()
        except json.JSONDecodeError as exc:
            raise StripeClientError("stripe_invalid_json_response") from exc

    def create_customer(
        self,
        *,
        email: Optional[str],
        name: Optional[str],
        metadata: Optional[Dict[str, str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if email:
            payload["email"] = str(email).strip()
        if name:
            payload["name"] = str(name).strip()
        for key, value in (metadata or {}).items():
            payload[f"metadata[{key}]"] = str(value)
        return self._request("POST", "/customers", data=payload, idempotency_key=idempotency_key)

    def create_checkout_session(
        self,
        *,
        customer_id: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        trial_days: int,
        allow_no_payment_method: bool = True,
        metadata: Optional[Dict[str, str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "mode": "subscription",
            "customer": customer_id,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
            "subscription_data[trial_from_plan]": "false",
        }
        if int(trial_days) > 0:
            payload["subscription_data[trial_period_days]"] = str(int(trial_days))
        if allow_no_payment_method:
            payload["payment_method_collection"] = "if_required"
        for key, value in (metadata or {}).items():
            payload[f"metadata[{key}]"] = str(value)
            payload[f"subscription_data[metadata][{key}]"] = str(value)
        return self._request("POST", "/checkout/sessions", data=payload, idempotency_key=idempotency_key)

    def create_billing_portal_session(self, *, customer_id: str, return_url: str) -> Dict[str, Any]:
        payload = {
            "customer": customer_id,
            "return_url": return_url,
        }
        return self._request("POST", "/billing_portal/sessions", data=payload)

    def retrieve_subscription(self, *, subscription_id: str) -> Dict[str, Any]:
        clean_id = str(subscription_id or "").strip()
        if not clean_id:
            raise StripeClientError("stripe_subscription_id_missing")
        return self._request("GET", f"/subscriptions/{clean_id}")

    def update_subscription(
        self,
        *,
        subscription_id: str,
        subscription_item_id: str,
        new_price_id: str,
        proration_behavior: str = "create_prorations",
    ) -> Dict[str, Any]:
        clean_sub_id = str(subscription_id or "").strip()
        clean_item_id = str(subscription_item_id or "").strip()
        clean_price_id = str(new_price_id or "").strip()
        if not clean_sub_id:
            raise StripeClientError("stripe_subscription_id_missing")
        if not clean_item_id:
            raise StripeClientError("stripe_subscription_item_id_missing")
        if not clean_price_id:
            raise StripeClientError("stripe_price_id_missing")
        payload: Dict[str, Any] = {
            "items[0][id]": clean_item_id,
            "items[0][price]": clean_price_id,
            "proration_behavior": str(proration_behavior or "create_prorations"),
        }
        return self._request("POST", f"/subscriptions/{clean_sub_id}", data=payload)

    def create_subscription_schedule(
        self,
        *,
        subscription_id: str,
        current_price_id: str,
        new_price_id: str,
        phase_end_unix: int,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Create a two-phase schedule: current plan until phase_end_unix, then new plan.

        Two-step sequence (Stripe's documented contract):

          1. `POST /v1/subscription_schedules` with ONLY
             `from_subscription` as the body — Stripe rejects
             additional fields on that creation form. Stripe populates
             `phases[0]` from the subscription's active phase.

          2. `POST /v1/subscription_schedules/<id>` with the full
             `phases` payload AND any `metadata`. `phases[0][start_date]`
             must match the active phase Stripe returned on step 1 —
             otherwise Stripe can either reject the update or drift
             the active phase off the subscription's
             `current_period_start`, leaving the sub with a
             mis-dated attached schedule.

        Compensation: if step 2 is DEFINITIVELY rejected (4xx) after
        step 1 succeeded, release the schedule created in step 1 so
        the subscription returns to normal and a retry can proceed.
        A 5xx / network timeout / invalid-JSON response is UNCERTAIN
        — Stripe may have applied the update server-side — so we
        do NOT release in that case; we log the schedule id for
        operator-side reconciliation and re-raise the original error
        unchanged. The release itself is best-effort: a release
        failure never masks the original step-2 error, but its
        failure IS logged with the schedule id for reconciliation.
        """
        clean_sub_id = str(subscription_id or "").strip()
        if not clean_sub_id:
            raise StripeClientError("stripe_subscription_id_missing")
        # Step 1: create schedule from existing subscription.
        schedule = self._request(
            "POST",
            "/subscription_schedules",
            data={"from_subscription": clean_sub_id},
        )
        schedule_id = str((schedule or {}).get("id") or "").strip()
        if not schedule_id:
            raise StripeClientError("stripe_schedule_create_failed")
        # Read the active phase's start_date from the step-1 response.
        # Stripe's `from_subscription` form always writes phases[0]
        # with the subscription's `current_period_start` as start_date.
        step1_phases = (schedule or {}).get("phases") or []
        active_start: Optional[int] = None
        if step1_phases:
            raw_start = (step1_phases[0] or {}).get("start_date")
            try:
                active_start = int(raw_start) if raw_start is not None else None
            except (TypeError, ValueError):
                active_start = None
        if not active_start:
            self._release_schedule_best_effort(schedule_id)
            raise StripeClientError("stripe_schedule_create_missing_active_start_date")
        # Step 2: update schedule with two explicit phases + metadata.
        phase_payload: Dict[str, Any] = {
            "end_behavior": "release",
            "phases[0][start_date]": str(active_start),
            "phases[0][items][0][price]": str(current_price_id or "").strip(),
            "phases[0][items][0][quantity]": "1",
            "phases[0][end_date]": str(int(phase_end_unix)),
            "phases[1][items][0][price]": str(new_price_id or "").strip(),
            "phases[1][items][0][quantity]": "1",
        }
        for key, value in (metadata or {}).items():
            phase_payload[f"metadata[{key}]"] = str(value)
        try:
            return self._request(
                "POST", f"/subscription_schedules/{schedule_id}", data=phase_payload,
            )
        except StripeClientError as step2_exc:
            if _is_schedule_update_validation_rejection(step2_exc):
                self._release_schedule_best_effort(schedule_id)
            else:
                # Not a payload-validation rejection — preserve the
                # schedule and surface its id on the exception so the
                # caller can reconcile it into local state. Covers 409
                # state conflicts, 429 lock_timeout / rate-limit, 5xx,
                # network errors, and any non-invalid_request_error 400.
                step2_exc.preserved_schedule_id = schedule_id
                logger.warning(
                    "stripe_schedule_update_uncertain "
                    "schedule_id=%s http_status=%s err_type=%s err_code=%s "
                    "err=%s — compensation release SKIPPED",
                    schedule_id,
                    step2_exc.http_status,
                    step2_exc.stripe_error_type,
                    step2_exc.stripe_error_code,
                    step2_exc,
                )
            raise

    def _release_schedule_best_effort(self, schedule_id: str) -> None:
        """Release a schedule created in THIS operation when step 2
        of `create_subscription_schedule` is DEFINITIVELY rejected.
        A release failure is swallowed so the caller still sees the
        original step-2 error — but the schedule id is logged so an
        operator can reconcile the now-orphaned (but attached)
        schedule."""
        try:
            self._request(
                "POST", f"/subscription_schedules/{schedule_id}/release",
            )
        except Exception as release_exc:
            logger.warning(
                "stripe_schedule_compensation_release_failed "
                "schedule_id=%s err=%s — reconcile this schedule out-of-band",
                schedule_id, release_exc,
            )
            return

    def cancel_subscription_schedule(self, *, schedule_id: str) -> Dict[str, Any]:
        """Release a subscription schedule, returning the subscription to normal."""
        clean_id = str(schedule_id or "").strip()
        if not clean_id:
            raise StripeClientError("stripe_schedule_id_missing")
        return self._request("POST", f"/subscription_schedules/{clean_id}/release")

    def retrieve_subscription_schedule(self, *, schedule_id: str) -> Dict[str, Any]:
        """Retrieve a Stripe subscription_schedule by id. Used by the
        change_plan route to verify a preserved schedule's actual
        phases (target price and effective date) before adopting it
        into local pending state — subscription attachment alone
        proves neither."""
        clean_id = str(schedule_id or "").strip()
        if not clean_id:
            raise StripeClientError("stripe_schedule_id_missing")
        return self._request("GET", f"/subscription_schedules/{clean_id}")

    def list_subscriptions(self, *, customer_id: str, status: str = "all", limit: int = 5) -> Dict[str, Any]:
        clean_customer_id = str(customer_id or "").strip()
        if not clean_customer_id:
            raise StripeClientError("stripe_customer_id_missing")
        clean_status = str(status or "").strip() or "all"
        clean_limit = max(1, min(100, int(limit)))
        return self._request(
            "GET",
            "/subscriptions",
            params={
                "customer": clean_customer_id,
                "status": clean_status,
                "limit": str(clean_limit),
            },
        )
