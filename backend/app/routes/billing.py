from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.firebase_auth import AuthenticatedUser, get_current_user_required
from app.auth.guards import require_org_role
from app.billing.config import BILLING_CONFIG
from app.billing.models import plan_spec
from app.billing.stripe_client import StripeBillingClient, StripeClientError
from app.services.multichurch_store import multichurch_store

router = APIRouter(tags=["billing"])

_WEBHOOK_TOLERANCE_SECONDS = max(
    30,
    min(3600, int((os.getenv("STRIPE_WEBHOOK_TOLERANCE_SECONDS") or "300").strip() or "300")),
)


class CheckoutSessionRequest(BaseModel):
    orgId: str = Field(..., min_length=2, max_length=120)
    planKey: str = Field(..., min_length=3, max_length=32)
    successUrl: str = Field(..., min_length=8, max_length=2048)
    cancelUrl: str = Field(..., min_length=8, max_length=2048)


class PortalSessionRequest(BaseModel):
    orgId: str = Field(..., min_length=2, max_length=120)
    returnUrl: str = Field(..., min_length=8, max_length=2048)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stripe_client() -> StripeBillingClient:
    return StripeBillingClient(secret_key=BILLING_CONFIG.stripe_secret_key)


def _resolve_plan_from_price_id(price_id: str, *, fallback_plan_key: str) -> str:
    clean = _clean(price_id)
    if not clean:
        return plan_spec(fallback_plan_key).key
    for plan_key, configured_price in BILLING_CONFIG.stripe_price_ids.items():
        if _clean(configured_price) and _clean(configured_price) == clean:
            return plan_key
    return plan_spec(fallback_plan_key).key


def _status_from_stripe(raw_status: str, *, fallback_status: str) -> str:
    token = _clean(raw_status).lower()
    if token in {"trialing", "active", "past_due", "canceled", "incomplete", "unpaid"}:
        return token
    if token == "incomplete_expired":
        return "canceled"
    return _clean(fallback_status).lower() or "trialing"


def _to_datetime(raw_unix: Any) -> Optional[datetime]:
    try:
        value = int(raw_unix)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _can_start_from_status(*, status: str, grace_ends_at: Optional[datetime], now: datetime) -> bool:
    token = _clean(status).lower()
    if token in {"trialing", "active"}:
        return True
    if token == "past_due" and isinstance(grace_ends_at, datetime):
        return now <= grace_ends_at
    return False


def _parse_stripe_signature(raw_header: str) -> tuple[int, list[str]]:
    timestamp = None
    signatures: list[str] = []
    for part in str(raw_header or "").split(","):
        token = part.strip()
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                timestamp = None
        elif key == "v1" and value:
            signatures.append(value)
    if timestamp is None or not signatures:
        raise HTTPException(status_code=400, detail="invalid_stripe_signature_header")
    return timestamp, signatures


def _verify_webhook_signature(*, payload: bytes, signature_header: str) -> None:
    secret = _clean(BILLING_CONFIG.stripe_webhook_secret)
    if not secret:
        raise HTTPException(status_code=503, detail="stripe_webhook_not_configured")
    timestamp, signatures = _parse_stripe_signature(signature_header)
    now_ts = int(_utcnow().timestamp())
    if abs(now_ts - timestamp) > _WEBHOOK_TOLERANCE_SECONDS:
        raise HTTPException(status_code=400, detail="stripe_signature_timestamp_out_of_range")
    signed = f"{timestamp}.{payload.decode('utf-8')}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise HTTPException(status_code=400, detail="invalid_stripe_signature")


def _resolve_org_id_from_event(event_type: str, stripe_obj: Dict[str, Any]) -> Optional[str]:
    metadata = stripe_obj.get("metadata")
    if isinstance(metadata, dict):
        org_id = _clean(metadata.get("orgId") or metadata.get("org_id"))
        if org_id:
            return org_id
    customer_id = _clean(stripe_obj.get("customer"))
    subscription_id = _clean(stripe_obj.get("subscription"))
    if event_type.startswith("customer.subscription."):
        subscription_id = _clean(stripe_obj.get("id")) or subscription_id
    return multichurch_store.find_org_id_by_billing_refs(
        stripe_customer_id=customer_id or None,
        stripe_subscription_id=subscription_id or None,
    )


@router.get("/billing/org/{org_id}/status")
def billing_status(
    org_id: str,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=org_id, user=user, roles={"owner", "admin", "host"}, store=multichurch_store, missing_membership_detail="forbidden")
    try:
        billing = multichurch_store.get_org_billing_profile(org_id=org_id)
    except ValueError as exc:
        detail = str(exc)
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "billing_status_fetch_failed") from exc
    return {"orgId": org_id, "billing": billing}


@router.post("/billing/checkout-session")
def create_checkout_session(
    body: CheckoutSessionRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=body.orgId, user=user, roles={"owner", "admin"}, store=multichurch_store, missing_membership_detail="forbidden")
    target_plan = plan_spec(body.planKey)
    if target_plan.key == "trial":
        raise HTTPException(status_code=400, detail="invalid_plan")
    price_id = _clean(BILLING_CONFIG.stripe_price_ids.get(target_plan.key))
    if not price_id:
        raise HTTPException(status_code=503, detail="billing_not_configured")
    if not _clean(BILLING_CONFIG.stripe_secret_key):
        raise HTTPException(status_code=503, detail="billing_not_configured")

    try:
        billing = multichurch_store.get_org_billing_profile(org_id=body.orgId)
    except ValueError as exc:
        detail = str(exc)
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "billing_profile_fetch_failed") from exc

    client = _stripe_client()
    customer_id = _clean((billing or {}).get("stripeCustomerId"))
    try:
        if not customer_id:
            customer = client.create_customer(
                email=user.email,
                name=user.displayName,
                metadata={"orgId": body.orgId, "createdByUid": user.uid},
            )
            customer_id = _clean((customer or {}).get("id"))
            if not customer_id:
                raise HTTPException(status_code=502, detail="stripe_customer_create_failed")
            billing["stripeCustomerId"] = customer_id
            multichurch_store.set_org_billing_profile(org_id=body.orgId, billing=billing)

        session = client.create_checkout_session(
            customer_id=customer_id,
            price_id=price_id,
            success_url=body.successUrl,
            cancel_url=body.cancelUrl,
            trial_days=int(BILLING_CONFIG.trial_days),
            allow_no_payment_method=True,
            metadata={"orgId": body.orgId, "planKey": target_plan.key, "requestedByUid": user.uid},
        )
    except StripeClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    checkout_url = _clean((session or {}).get("url"))
    session_id = _clean((session or {}).get("id"))
    if not checkout_url or not session_id:
        raise HTTPException(status_code=502, detail="stripe_checkout_session_failed")
    return {"url": checkout_url, "sessionId": session_id}


@router.post("/billing/portal-session")
def create_portal_session(
    body: PortalSessionRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=body.orgId, user=user, roles={"owner", "admin"}, store=multichurch_store, missing_membership_detail="forbidden")
    if not _clean(BILLING_CONFIG.stripe_secret_key):
        raise HTTPException(status_code=503, detail="billing_not_configured")

    try:
        billing = multichurch_store.get_org_billing_profile(org_id=body.orgId)
    except ValueError as exc:
        detail = str(exc)
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "billing_profile_fetch_failed") from exc

    customer_id = _clean((billing or {}).get("stripeCustomerId"))
    if not customer_id:
        raise HTTPException(status_code=404, detail="billing_customer_not_found")

    try:
        session = _stripe_client().create_billing_portal_session(customer_id=customer_id, return_url=body.returnUrl)
    except StripeClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    portal_url = _clean((session or {}).get("url"))
    if not portal_url:
        raise HTTPException(status_code=502, detail="stripe_portal_session_failed")
    return {"url": portal_url}


@router.post("/billing/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
):
    payload_raw = await request.body()
    _verify_webhook_signature(payload=payload_raw, signature_header=_clean(stripe_signature))
    try:
        event = json.loads(payload_raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid_webhook_payload") from exc

    event_id = _clean((event or {}).get("id"))
    event_type = _clean((event or {}).get("type"))
    if not event_id or not event_type:
        raise HTTPException(status_code=400, detail="invalid_webhook_event")
    payload_obj = ((event or {}).get("data") or {}).get("object") or {}
    if not isinstance(payload_obj, dict):
        payload_obj = {}

    try:
        fresh = multichurch_store.try_mark_billing_event(
            event_id=event_id,
            event_type=event_type,
            payload={"type": event_type},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "invalid_webhook_event") from exc
    if not fresh:
        return {"ok": True, "deduped": True}

    org_id = _resolve_org_id_from_event(event_type, payload_obj)
    if not org_id:
        return {"ok": True, "unmatched": True}

    try:
        billing = multichurch_store.get_org_billing_profile(org_id=org_id)
    except ValueError as exc:
        detail = str(exc)
        if detail == "org_not_found":
            return {"ok": True, "unmatched": True}
        raise HTTPException(status_code=400, detail=detail or "billing_profile_fetch_failed") from exc

    now = _utcnow()
    next_billing = dict(billing)
    next_billing["updatedAt"] = now

    metadata = payload_obj.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    if event_type == "checkout.session.completed":
        customer_id = _clean(payload_obj.get("customer"))
        subscription_id = _clean(payload_obj.get("subscription"))
        requested_plan = plan_spec(metadata.get("planKey") or next_billing.get("planKey") or "trial")
        if customer_id:
            next_billing["stripeCustomerId"] = customer_id
        if subscription_id:
            next_billing["stripeSubscriptionId"] = subscription_id
        next_billing["planKey"] = requested_plan.key
        next_billing.setdefault("limits", {})
        next_billing["limits"]["maxServiceKeys"] = requested_plan.max_service_keys

    elif event_type in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
        stripe_status = _status_from_stripe(str(payload_obj.get("status") or ""), fallback_status=str(next_billing.get("status") or "trialing"))
        if event_type == "customer.subscription.deleted":
            stripe_status = "canceled"

        subscription_id = _clean(payload_obj.get("id"))
        customer_id = _clean(payload_obj.get("customer"))
        cancel_at_period_end = bool(payload_obj.get("cancel_at_period_end"))
        current_period_start = _to_datetime(payload_obj.get("current_period_start"))
        current_period_end = _to_datetime(payload_obj.get("current_period_end"))
        trial_end = _to_datetime(payload_obj.get("trial_end"))

        items = (payload_obj.get("items") or {}).get("data") if isinstance(payload_obj.get("items"), dict) else None
        price_id = ""
        if isinstance(items, list) and items:
            first = items[0] or {}
            if isinstance(first, dict):
                price = first.get("price") or {}
                if isinstance(price, dict):
                    price_id = _clean(price.get("id"))

        requested_plan = plan_spec(metadata.get("planKey") or _resolve_plan_from_price_id(price_id, fallback_plan_key=str(next_billing.get("planKey") or "trial")))

        next_billing["status"] = stripe_status
        next_billing["cancelAtPeriodEnd"] = cancel_at_period_end
        next_billing["currentPeriodStart"] = current_period_start
        next_billing["currentPeriodEnd"] = current_period_end
        next_billing["trialEndsAt"] = trial_end if stripe_status == "trialing" else next_billing.get("trialEndsAt")
        next_billing["priceId"] = price_id or next_billing.get("priceId")
        next_billing["planKey"] = requested_plan.key
        next_billing.setdefault("limits", {})
        next_billing["limits"]["maxServiceKeys"] = requested_plan.max_service_keys
        if customer_id:
            next_billing["stripeCustomerId"] = customer_id
        if subscription_id:
            next_billing["stripeSubscriptionId"] = subscription_id
        if stripe_status == "past_due":
            next_billing["graceEndsAt"] = now + timedelta(days=int(BILLING_CONFIG.grace_days))
        elif stripe_status in {"active", "trialing"}:
            next_billing["graceEndsAt"] = None
        else:
            next_billing["graceEndsAt"] = next_billing.get("graceEndsAt")

    elif event_type == "invoice.payment_succeeded":
        current_status = _clean(next_billing.get("status")).lower()
        if current_status in {"past_due", "incomplete", "unpaid"}:
            next_billing["status"] = "active"
            next_billing["graceEndsAt"] = None

    elif event_type == "invoice.payment_failed":
        next_billing["status"] = "past_due"
        next_billing["graceEndsAt"] = now + timedelta(days=int(BILLING_CONFIG.grace_days))

    next_billing.setdefault("entitlements", {})
    next_billing["entitlements"]["canStartService"] = _can_start_from_status(
        status=str(next_billing.get("status") or ""),
        grace_ends_at=next_billing.get("graceEndsAt") if isinstance(next_billing.get("graceEndsAt"), datetime) else None,
        now=now,
    )

    try:
        updated = multichurch_store.set_org_billing_profile(org_id=org_id, billing=next_billing)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "billing_profile_update_failed") from exc
    return {
        "ok": True,
        "orgId": org_id,
        "eventId": event_id,
        "eventType": event_type,
        "status": str(updated.get("status") or ""),
    }
