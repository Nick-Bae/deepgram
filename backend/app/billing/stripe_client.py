from __future__ import annotations

import json
from typing import Any, Dict, Optional

import httpx


class StripeClientError(RuntimeError):
    pass


class StripeBillingClient:
    def __init__(self, *, secret_key: str, api_base: str = "https://api.stripe.com/v1", timeout_seconds: float = 15.0) -> None:
        self._secret_key = (secret_key or "").strip()
        self._api_base = (api_base or "https://api.stripe.com/v1").rstrip("/")
        self._timeout = max(3.0, float(timeout_seconds))

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self._secret_key:
            raise StripeClientError("stripe_secret_key_missing")
        url = f"{self._api_base}{path}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                request_kwargs: Dict[str, Any] = {
                    "headers": {"Authorization": f"Bearer {self._secret_key}"},
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
            try:
                payload = res.json()
                detail = str(((payload or {}).get("error") or {}).get("message") or "")
            except Exception:
                detail = (res.text or "").strip()
            raise StripeClientError(f"stripe_http_{res.status_code}:{detail[:200]}")
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
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if email:
            payload["email"] = str(email).strip()
        if name:
            payload["name"] = str(name).strip()
        for key, value in (metadata or {}).items():
            payload[f"metadata[{key}]"] = str(value)
        return self._request("POST", "/customers", data=payload)

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
        return self._request("POST", "/checkout/sessions", data=payload)

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
