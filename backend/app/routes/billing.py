from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import os
import re
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field, field_validator

from app import validators
from app.security_log import security_event, client_ip as _client_ip

logger = logging.getLogger(__name__)


def _validate_redirect_url(url: str) -> str:
    """Accept only http(s) URLs; reject javascript:, data:, protocol-relative, etc."""
    stripped = (url or "").strip()
    parsed = urlsplit(stripped)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("redirect_url_must_be_http_or_https")
    if not parsed.netloc:
        raise ValueError("redirect_url_invalid_host")
    return stripped

from app.auth.firebase_auth import AuthenticatedUser, get_current_user_required
from app.auth.guards import require_org_role
from app.billing.config import BILLING_CONFIG
from app.billing.models import plan_spec
from app.billing.stripe_client import StripeBillingClient, StripeClientError
from app.services.multichurch_store import multichurch_store
from app.services import email_service

router = APIRouter(tags=["billing"])


def _send_billing_email(event: str, *, org_id: str, plan_key: str = "") -> None:
    """Fire-and-forget billing email dispatcher. Never raises."""
    try:
        admin_emails = multichurch_store.get_org_admin_emails(org_id=org_id)
        org_name = multichurch_store.get_org_name(org_id=org_id)
        if event == "subscription_started":
            email_service.send_subscription_started_email(
                admin_emails=admin_emails, org_name=org_name, plan_key=plan_key or "pro"
            )
        elif event == "subscription_canceled":
            email_service.send_subscription_canceled_email(
                admin_emails=admin_emails, org_name=org_name
            )
        elif event == "payment_failed":
            email_service.send_payment_failed_email(
                admin_emails=admin_emails, org_name=org_name, grace_days=int(BILLING_CONFIG.grace_days)
            )
        elif event == "payment_recovered":
            email_service.send_payment_recovered_email(
                admin_emails=admin_emails, org_name=org_name
            )
    except Exception:
        logger.exception("billing_email_dispatch_error event=%s org=%s", event, org_id)

_WEBHOOK_TOLERANCE_SECONDS = max(
    30,
    min(300, int((os.getenv("STRIPE_WEBHOOK_TOLERANCE_SECONDS") or "60").strip() or "60")),
)


class CheckoutSessionRequest(BaseModel):
    orgId: str = Field(..., min_length=2, max_length=120, pattern=validators.ORG_ID)
    planKey: str = Field(..., min_length=3, max_length=32, pattern=validators.PLAN_KEY)
    successUrl: str = Field(..., min_length=8, max_length=2048)
    cancelUrl: str = Field(..., min_length=8, max_length=2048)

    @field_validator("successUrl", "cancelUrl")
    @classmethod
    def _check_redirect_url(cls, v: str) -> str:
        return _validate_redirect_url(v)


class PortalSessionRequest(BaseModel):
    orgId: str = Field(..., min_length=2, max_length=120, pattern=validators.ORG_ID)
    returnUrl: str = Field(..., min_length=8, max_length=2048)

    @field_validator("returnUrl")
    @classmethod
    def _check_redirect_url(cls, v: str) -> str:
        return _validate_redirect_url(v)


class ChangePlanRequest(BaseModel):
    orgId: str = Field(..., min_length=2, max_length=120, pattern=validators.ORG_ID)
    targetPlanKey: str = Field(..., min_length=3, max_length=32, pattern=validators.PLAN_KEY)


class CancelPendingDowngradeRequest(BaseModel):
    orgId: str = Field(..., min_length=2, max_length=120, pattern=validators.ORG_ID)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _clean_stripe_ref(value: Any, *, prefixes: tuple[str, ...]) -> str:
    raw = _clean(value).strip("`'\"")
    if not raw:
        return ""
    for prefix in prefixes:
        match = re.search(rf"({re.escape(prefix)}[A-Za-z0-9_]+)", raw)
        if match:
            return match.group(1)
    return raw


def _clean_customer_id(value: Any) -> str:
    return _clean_stripe_ref(value, prefixes=("cus_",))


def _clean_subscription_id(value: Any) -> str:
    return _clean_stripe_ref(value, prefixes=("sub_",))


def _clean_price_id(value: Any) -> str:
    return _clean_stripe_ref(value, prefixes=("price_",))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stripe_client() -> StripeBillingClient:
    return StripeBillingClient(secret_key=BILLING_CONFIG.stripe_secret_key)


def _resolve_plan_from_price_id(price_id: str, *, fallback_plan_key: str) -> str:
    clean = _clean_price_id(price_id)
    if not clean:
        return plan_spec(fallback_plan_key).key
    for plan_key, configured_price in BILLING_CONFIG.stripe_price_ids.items():
        if _clean_price_id(configured_price) and _clean_price_id(configured_price) == clean:
            return plan_key
    return plan_spec(fallback_plan_key).key


def _is_valid_price_id(price_id: str) -> bool:
    return _clean_price_id(price_id).startswith("price_")


def _is_missing_customer_error(exc: StripeClientError) -> bool:
    token = _clean(str(exc)).lower()
    if not token:
        return False
    return "no such customer" in token or ("customer" in token and "not found" in token)


def _is_missing_subscription_error(exc: StripeClientError) -> bool:
    token = _clean(str(exc)).lower()
    if not token:
        return False
    return "no such subscription" in token or ("subscription" in token and "not found" in token)


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


def _to_positive_int(raw_value: Any) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _price_id_from_subscription(subscription: Dict[str, Any]) -> str:
    items = subscription.get("items")
    if not isinstance(items, dict):
        return ""
    rows = items.get("data")
    if not isinstance(rows, list):
        return ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        price = row.get("price")
        if isinstance(price, dict):
            token = _clean_price_id(price.get("id"))
            if token:
                return token
        else:
            token = _clean_price_id(price)
            if token:
                return token
    return ""


def _normalize_billing_refs(*, billing: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
    next_billing = dict(billing or {})
    changed = False
    for key, cleaner in (
        ("stripeCustomerId", _clean_customer_id),
        ("stripeSubscriptionId", _clean_subscription_id),
        ("priceId", _clean_price_id),
    ):
        raw_value = next_billing.get(key)
        normalized = cleaner(raw_value)
        if normalized:
            if normalized != raw_value:
                next_billing[key] = normalized
                changed = True
        elif raw_value not in (None, ""):
            next_billing[key] = None
            changed = True
    return next_billing, changed


def _clear_stale_stripe_refs(*, org_id: str, billing: Dict[str, Any]) -> Dict[str, Any]:
    next_billing = dict(billing or {})
    changed = False
    for key in ("stripeCustomerId", "stripeSubscriptionId", "priceId", "currentPeriodStart", "currentPeriodEnd"):
        if next_billing.get(key) not in (None, ""):
            next_billing[key] = None
            changed = True
    if not changed:
        return billing
    next_billing["updatedAt"] = _utcnow()
    return multichurch_store.set_org_billing_profile(org_id=org_id, billing=next_billing)


def _create_missing_stripe_customer(
    *,
    client: StripeBillingClient,
    org_id: str,
    billing: Dict[str, Any],
    user: AuthenticatedUser,
    idempotency_suffix: str = "",
) -> tuple[str, Dict[str, Any]]:
    customer = client.create_customer(
        email=user.email,
        name=user.displayName,
        metadata={"orgId": org_id, "createdByUid": user.uid},
        idempotency_key=f"create-customer-{org_id}{idempotency_suffix}",
    )
    customer_id = _clean_customer_id((customer or {}).get("id"))
    if not customer_id:
        raise HTTPException(status_code=502, detail="stripe_customer_create_failed")
    next_billing = dict(billing or {})
    next_billing["stripeCustomerId"] = customer_id
    saved = multichurch_store.set_org_billing_profile(org_id=org_id, billing=next_billing)
    return customer_id, saved


def _first_subscription_item(subscription: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    items = subscription.get("items")
    if not isinstance(items, dict):
        return None
    rows = items.get("data")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict):
            return row
    return None


def _subscription_period_datetime(subscription: Dict[str, Any], field_name: str) -> Optional[datetime]:
    direct_value = _to_datetime(subscription.get(field_name))
    if direct_value is not None:
        return direct_value
    first_item = _first_subscription_item(subscription)
    if not isinstance(first_item, dict):
        return None
    return _to_datetime(first_item.get(field_name))


def _merge_subscription_snapshot(
    *,
    billing: Dict[str, Any],
    stripe_subscription: Dict[str, Any],
    now: datetime,
) -> Dict[str, Any]:
    next_billing = dict(billing or {})
    stripe_status = _status_from_stripe(
        str(stripe_subscription.get("status") or ""),
        fallback_status=str(next_billing.get("status") or "trialing"),
    )
    subscription_id = _clean_subscription_id(stripe_subscription.get("id"))
    customer_id = _clean_customer_id(stripe_subscription.get("customer"))
    cancel_at_period_end = bool(stripe_subscription.get("cancel_at_period_end"))
    current_period_start = _subscription_period_datetime(stripe_subscription, "current_period_start")
    current_period_end = _subscription_period_datetime(stripe_subscription, "current_period_end")
    trial_end = _to_datetime(stripe_subscription.get("trial_end"))
    price_id = _clean_price_id(_price_id_from_subscription(stripe_subscription))
    requested_plan = plan_spec(
        _resolve_plan_from_price_id(
            price_id,
            fallback_plan_key=str(next_billing.get("planKey") or "trial"),
        )
    )

    next_billing["status"] = stripe_status
    next_billing["cancelAtPeriodEnd"] = cancel_at_period_end
    next_billing["currentPeriodStart"] = current_period_start
    next_billing["currentPeriodEnd"] = current_period_end
    next_billing["trialEndsAt"] = trial_end if stripe_status == "trialing" else None
    next_billing["priceId"] = price_id or next_billing.get("priceId")
    next_billing["planKey"] = requested_plan.key
    next_billing.setdefault("limits", {})
    next_billing["limits"]["maxServiceKeys"] = requested_plan.max_service_keys
    if customer_id:
        next_billing["stripeCustomerId"] = customer_id
    if subscription_id:
        next_billing["stripeSubscriptionId"] = subscription_id

    if stripe_status == "past_due":
        # Only set graceEndsAt when first entering past_due; don't reset on repeated updates.
        if not next_billing.get("graceEndsAt"):
            next_billing["graceEndsAt"] = now + timedelta(days=int(BILLING_CONFIG.grace_days))
    elif stripe_status in {"active", "trialing"}:
        next_billing["graceEndsAt"] = None

    next_billing.setdefault("entitlements", {})
    next_billing["entitlements"]["canStartService"] = _can_start_from_status(
        status=str(next_billing.get("status") or ""),
        grace_ends_at=next_billing.get("graceEndsAt") if isinstance(next_billing.get("graceEndsAt"), datetime) else None,
        now=now,
    )
    next_billing["updatedAt"] = now
    return next_billing


def _subscription_sort_key(subscription: Dict[str, Any]) -> tuple[int, int, int]:
    status = _status_from_stripe(
        str(subscription.get("status") or ""),
        fallback_status="canceled",
    )
    status_rank = {
        "active": 0,
        "trialing": 1,
        "past_due": 2,
        "incomplete": 3,
        "unpaid": 4,
        "canceled": 5,
    }.get(status, 99)
    current_period_end = _to_positive_int(subscription.get("current_period_end"))
    created_at = _to_positive_int(subscription.get("created"))
    return (status_rank, -current_period_end, -created_at)


def _pick_customer_subscription(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        return None
    candidates = [row for row in rows if isinstance(row, dict)]
    if not candidates:
        return None
    return min(candidates, key=_subscription_sort_key)


def _lookup_customer_subscription(client: StripeBillingClient, *, customer_id: str) -> Optional[Dict[str, Any]]:
    clean_customer_id = _clean_customer_id(customer_id)
    if not clean_customer_id:
        return None
    return _pick_customer_subscription(
        client.list_subscriptions(customer_id=clean_customer_id, status="all", limit=10)
    )


def _should_sync_billing_from_stripe(*, billing: Dict[str, Any], force_refresh: bool) -> bool:
    if force_refresh:
        return True
    has_current_period = bool((billing or {}).get("currentPeriodStart")) and bool((billing or {}).get("currentPeriodEnd"))
    plan_token = _clean((billing or {}).get("planKey")).lower()
    if (not has_current_period) or plan_token == "trial":
        return True
    # Auto-refresh if the stored billing period has already expired (missed webhook recovery).
    period_end = (billing or {}).get("currentPeriodEnd")
    if isinstance(period_end, datetime) and period_end < _utcnow():
        return True
    return False


def _hydrate_billing_from_stripe(
    *,
    org_id: str,
    billing: Dict[str, Any],
    force_refresh: bool,
) -> Dict[str, Any]:
    if not _clean(BILLING_CONFIG.stripe_secret_key):
        return billing
    if not _should_sync_billing_from_stripe(billing=billing, force_refresh=force_refresh):
        return billing

    client = _stripe_client()
    subscription_id = _clean_subscription_id((billing or {}).get("stripeSubscriptionId"))
    customer_id = _clean_customer_id((billing or {}).get("stripeCustomerId"))
    stripe_subscription: Optional[Dict[str, Any]] = None
    customer_refs_invalid = False

    def _lookup_by_customer() -> Optional[Dict[str, Any]]:
        nonlocal billing, customer_refs_invalid, customer_id, subscription_id
        try:
            return _lookup_customer_subscription(client, customer_id=customer_id)
        except StripeClientError as exc:
            if _is_missing_customer_error(exc):
                billing = _clear_stale_stripe_refs(org_id=org_id, billing=billing)
                customer_refs_invalid = True
                customer_id = ""
                subscription_id = ""
                return None
            raise

    if force_refresh and customer_id:
        stripe_subscription = _lookup_by_customer()
        if customer_refs_invalid:
            return billing

    if not stripe_subscription and subscription_id:
        try:
            stripe_subscription = client.retrieve_subscription(subscription_id=subscription_id)
        except StripeClientError as exc:
            if not customer_id or not _is_missing_subscription_error(exc):
                raise

    if not stripe_subscription and customer_id:
        stripe_subscription = _lookup_by_customer()
        if customer_refs_invalid:
            return billing

    if not stripe_subscription:
        return billing

    next_billing = _merge_subscription_snapshot(
        billing=billing,
        stripe_subscription=stripe_subscription,
        now=_utcnow(),
    )
    return multichurch_store.set_org_billing_profile(org_id=org_id, billing=next_billing)


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


def _verify_webhook_signature(*, payload: bytes, signature_header: str, client_ip: str = "") -> None:
    secret = _clean(BILLING_CONFIG.stripe_webhook_secret)
    if not secret:
        raise HTTPException(status_code=503, detail="stripe_webhook_not_configured")
    timestamp, signatures = _parse_stripe_signature(signature_header)
    now_ts = int(_utcnow().timestamp())
    if abs(now_ts - timestamp) > _WEBHOOK_TOLERANCE_SECONDS:
        security_event("webhook_sig_expired", severity="ERROR", ip=client_ip,
                       detail=f"age_seconds={abs(now_ts - timestamp)}")
        raise HTTPException(status_code=400, detail="stripe_signature_timestamp_out_of_range")
    signed = f"{timestamp}.{payload.decode('utf-8')}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    # Compare against ALL signatures without early exit to avoid timing side-channels.
    matched = False
    for sig in signatures:
        if hmac.compare_digest(expected, sig):
            matched = True
    if not matched:
        security_event("webhook_sig_invalid", severity="ERROR", ip=client_ip)
        raise HTTPException(status_code=400, detail="invalid_stripe_signature")


def _resolve_org_id_from_event(event_type: str, stripe_obj: Dict[str, Any]) -> Optional[str]:
    metadata = stripe_obj.get("metadata")
    if isinstance(metadata, dict):
        org_id = _clean(metadata.get("orgId") or metadata.get("org_id"))
        if org_id:
            return org_id
    customer_id = _clean_customer_id(stripe_obj.get("customer"))
    subscription_id = _clean_subscription_id(stripe_obj.get("subscription"))
    if event_type.startswith("customer.subscription."):
        subscription_id = _clean_subscription_id(stripe_obj.get("id")) or subscription_id
    return multichurch_store.find_org_id_by_billing_refs(
        stripe_customer_id=customer_id or None,
        stripe_subscription_id=subscription_id or None,
    )


@router.get("/billing/org/{org_id}/status")
def billing_status(
    org_id: str = Path(pattern=validators.ORG_ID),
    refresh: bool = Query(default=False),
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
    billing, refs_changed = _normalize_billing_refs(billing=billing)
    if refs_changed:
        billing = multichurch_store.set_org_billing_profile(org_id=org_id, billing=billing)

    try:
        billing = _hydrate_billing_from_stripe(
            org_id=org_id,
            billing=billing,
            force_refresh=bool(refresh),
        )
    except StripeClientError as exc:
        if refresh:
            logger.warning("stripe_sync_failed org=%s: %s", org_id, exc)
            raise HTTPException(status_code=502, detail="stripe_sync_failed") from exc
        # Keep returning local billing snapshot even if Stripe sync is temporarily unavailable.
    except ValueError as exc:
        if refresh:
            raise HTTPException(status_code=400, detail=str(exc) or "billing_status_sync_failed") from exc
        # Keep returning local billing snapshot even if Stripe sync is temporarily unavailable.
    try:
        effective_trial_seconds_remaining = multichurch_store.get_org_effective_trial_seconds_remaining(org_id=org_id)
    except ValueError:
        effective_trial_seconds_remaining = None
    if effective_trial_seconds_remaining is not None:
        billing = dict(billing)
        billing["trialSecondsRemaining"] = effective_trial_seconds_remaining
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
    price_id = _clean_price_id(BILLING_CONFIG.stripe_price_ids.get(target_plan.key))
    if not price_id:
        raise HTTPException(status_code=503, detail="billing_not_configured")
    if not _is_valid_price_id(price_id):
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

    billing, refs_changed = _normalize_billing_refs(billing=billing)
    if refs_changed:
        billing = multichurch_store.set_org_billing_profile(org_id=body.orgId, billing=billing)

    client = _stripe_client()
    customer_id = _clean_customer_id((billing or {}).get("stripeCustomerId"))
    try:
        if not customer_id:
            customer_id, billing = _create_missing_stripe_customer(
                client=client,
                org_id=body.orgId,
                billing=billing,
                user=user,
            )

        session = client.create_checkout_session(
            customer_id=customer_id,
            price_id=price_id,
            success_url=body.successUrl,
            cancel_url=body.cancelUrl,
            trial_days=0,
            allow_no_payment_method=True,
            metadata={"orgId": body.orgId, "planKey": target_plan.key, "requestedByUid": user.uid},
        )
    except StripeClientError as exc:
        # If the stored Stripe customer belongs to a different account/mode, recreate and retry once.
        if customer_id and _is_missing_customer_error(exc):
            try:
                customer_id, billing = _create_missing_stripe_customer(
                    client=client,
                    org_id=body.orgId,
                    billing=billing,
                    user=user,
                    idempotency_suffix="-retry",
                )
                session = client.create_checkout_session(
                    customer_id=customer_id,
                    price_id=price_id,
                    success_url=body.successUrl,
                    cancel_url=body.cancelUrl,
                    trial_days=0,
                    allow_no_payment_method=True,
                    metadata={"orgId": body.orgId, "planKey": target_plan.key, "requestedByUid": user.uid},
                )
            except StripeClientError as retry_exc:
                logger.warning("stripe_checkout_failed (retry) org=%s: %s", body.orgId, retry_exc)
                raise HTTPException(status_code=502, detail="stripe_checkout_failed") from retry_exc
        else:
            logger.warning("stripe_checkout_failed org=%s: %s", body.orgId, exc)
            raise HTTPException(status_code=502, detail="stripe_checkout_failed") from exc

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

    billing, refs_changed = _normalize_billing_refs(billing=billing)
    if refs_changed:
        billing = multichurch_store.set_org_billing_profile(org_id=body.orgId, billing=billing)

    client = _stripe_client()
    customer_id = _clean_customer_id((billing or {}).get("stripeCustomerId"))
    try:
        if not customer_id:
            customer_id, billing = _create_missing_stripe_customer(
                client=client,
                org_id=body.orgId,
                billing=billing,
                user=user,
            )
        session = client.create_billing_portal_session(customer_id=customer_id, return_url=body.returnUrl)
    except StripeClientError as exc:
        if _is_missing_customer_error(exc):
            try:
                refreshed_billing = _clear_stale_stripe_refs(org_id=body.orgId, billing=billing)
                customer_id, billing = _create_missing_stripe_customer(
                    client=client,
                    org_id=body.orgId,
                    billing=refreshed_billing,
                    user=user,
                    idempotency_suffix="-retry",
                )
                session = client.create_billing_portal_session(customer_id=customer_id, return_url=body.returnUrl)
            except StripeClientError as retry_exc:
                logger.warning("stripe_portal_failed (retry) org=%s: %s", body.orgId, retry_exc)
                raise HTTPException(status_code=502, detail="stripe_portal_failed") from retry_exc
        else:
            logger.warning("stripe_portal_failed org=%s: %s", body.orgId, exc)
            raise HTTPException(status_code=502, detail="stripe_portal_failed") from exc

    portal_url = _clean((session or {}).get("url"))
    if not portal_url:
        raise HTTPException(status_code=502, detail="stripe_portal_session_failed")
    return {"url": portal_url}


def _is_upgrade(current_plan_key: str, target_plan_key: str) -> bool:
    from app.billing.models import plan_spec as _ps
    return _ps(target_plan_key).amount_usd > _ps(current_plan_key).amount_usd


@router.post("/billing/change-plan")
def change_plan(
    body: ChangePlanRequest,
    user: AuthenticatedUser = Depends(get_current_user_required),
):
    require_org_role(org_id=body.orgId, user=user, roles={"owner", "admin"}, store=multichurch_store, missing_membership_detail="forbidden")
    target_plan = plan_spec(body.targetPlanKey)
    if target_plan.key == "trial":
        raise HTTPException(status_code=400, detail="invalid_plan")

    price_id = _clean_price_id(BILLING_CONFIG.stripe_price_ids.get(target_plan.key))
    if not price_id or not _is_valid_price_id(price_id):
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

    billing, refs_changed = _normalize_billing_refs(billing=billing)
    if refs_changed:
        billing = multichurch_store.set_org_billing_profile(org_id=body.orgId, billing=billing)

    current_status = _clean(billing.get("status")).lower()
    if current_status not in {"active", "past_due"}:
        raise HTTPException(status_code=400, detail="no_active_subscription")

    current_plan_key = _clean(billing.get("planKey")).lower() or "trial"
    if current_plan_key == target_plan.key:
        raise HTTPException(status_code=400, detail="same_plan")

    subscription_id = _clean_subscription_id(billing.get("stripeSubscriptionId"))
    if not subscription_id:
        raise HTTPException(status_code=400, detail="no_active_subscription")

    client = _stripe_client()
    try:
        subscription = client.retrieve_subscription(subscription_id=subscription_id)
    except StripeClientError as exc:
        logger.warning("change_plan_retrieve_sub_failed org=%s: %s", body.orgId, exc)
        raise HTTPException(status_code=502, detail="stripe_update_failed") from exc

    first_item = _first_subscription_item(subscription)
    if not first_item or not first_item.get("id"):
        raise HTTPException(status_code=502, detail="stripe_update_failed")
    item_id = _clean(first_item["id"])

    upgrading = _is_upgrade(current_plan_key, target_plan.key)

    if upgrading:
        try:
            client.update_subscription(
                subscription_id=subscription_id,
                subscription_item_id=item_id,
                new_price_id=price_id,
                proration_behavior="create_prorations",
            )
        except StripeClientError as exc:
            logger.warning("change_plan_upgrade_failed org=%s: %s", body.orgId, exc)
            raise HTTPException(status_code=502, detail="stripe_update_failed") from exc
        return {"ok": True, "effective": "immediate", "pendingPlanKey": None, "pendingPlanDate": None}

    # Downgrade: create subscription schedule for period end
    current_price_id = _clean_price_id(_price_id_from_subscription(subscription))
    period_end_unix = subscription.get("current_period_end")
    period_end_dt = _to_datetime(period_end_unix)
    if not period_end_dt or not period_end_unix:
        raise HTTPException(status_code=502, detail="stripe_update_failed")

    try:
        schedule = client.create_subscription_schedule(
            subscription_id=subscription_id,
            current_price_id=current_price_id or price_id,
            new_price_id=price_id,
            phase_end_unix=int(period_end_unix),
        )
    except StripeClientError as exc:
        logger.warning("change_plan_schedule_failed org=%s: %s", body.orgId, exc)
        raise HTTPException(status_code=502, detail="stripe_schedule_failed") from exc

    schedule_id = _clean((schedule or {}).get("id"))
    next_billing = dict(billing)
    next_billing["pendingPlanKey"] = target_plan.key
    next_billing["pendingPlanDate"] = period_end_dt
    next_billing["pendingScheduleId"] = schedule_id
    next_billing["updatedAt"] = _utcnow()
    try:
        multichurch_store.set_org_billing_profile(org_id=body.orgId, billing=next_billing)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "billing_profile_update_failed") from exc

    return {
        "ok": True,
        "effective": period_end_dt.isoformat(),
        "pendingPlanKey": target_plan.key,
        "pendingPlanDate": period_end_dt.isoformat(),
    }


@router.post("/billing/cancel-pending-downgrade")
def cancel_pending_downgrade(
    body: CancelPendingDowngradeRequest,
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

    schedule_id = _clean(billing.get("pendingScheduleId"))
    if not schedule_id:
        raise HTTPException(status_code=400, detail="no_pending_downgrade")

    try:
        _stripe_client().cancel_subscription_schedule(schedule_id=schedule_id)
    except StripeClientError as exc:
        logger.warning("cancel_downgrade_failed org=%s: %s", body.orgId, exc)
        raise HTTPException(status_code=502, detail="stripe_cancel_schedule_failed") from exc

    next_billing = dict(billing)
    next_billing["pendingPlanKey"] = None
    next_billing["pendingPlanDate"] = None
    next_billing["pendingScheduleId"] = None
    next_billing["updatedAt"] = _utcnow()
    try:
        multichurch_store.set_org_billing_profile(org_id=body.orgId, billing=next_billing)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "billing_profile_update_failed") from exc

    return {"ok": True}


@router.post("/billing/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
):
    payload_raw = await request.body()
    _verify_webhook_signature(payload=payload_raw, signature_header=_clean(stripe_signature),
                              client_ip=_client_ip(request))
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
        detail = str(exc)
        if detail in {"invalid_event_id", "invalid_event_type"}:
            raise HTTPException(status_code=400, detail=detail) from exc
        raise HTTPException(status_code=400, detail="invalid_webhook_event") from exc
    if not fresh:
        return {"ok": True, "eventId": event_id, "deduped": True}

    org_id = _resolve_org_id_from_event(event_type, payload_obj)
    if not org_id:
        return {"ok": True, "eventId": event_id, "unmatched": True}

    try:
        billing = multichurch_store.get_org_billing_profile(org_id=org_id)
    except ValueError as exc:
        detail = str(exc)
        if detail == "org_not_found":
            return {"ok": True, "eventId": event_id, "unmatched": True}
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
        if requested_plan.key != "trial":
            _send_billing_email("subscription_started", org_id=org_id, plan_key=requested_plan.key)

    elif event_type in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
        next_billing = _merge_subscription_snapshot(
            billing=next_billing,
            stripe_subscription=payload_obj,
            now=now,
        )
        stripe_status = _clean(next_billing.get("status")).lower()
        requested_plan = plan_spec(str(metadata.get("planKey") or next_billing.get("planKey") or "trial"))
        next_billing["planKey"] = requested_plan.key
        next_billing.setdefault("limits", {})
        next_billing["limits"]["maxServiceKeys"] = requested_plan.max_service_keys

        # Clear pending downgrade if the active plan now matches the pending target
        pending_plan_key = _clean(next_billing.get("pendingPlanKey")).lower()
        if pending_plan_key and next_billing.get("planKey") == pending_plan_key:
            next_billing["pendingPlanKey"] = None
            next_billing["pendingPlanDate"] = None
            next_billing["pendingScheduleId"] = None

        # Sync org-level minute limit to match the new plan spec
        try:
            multichurch_store.set_org_monthly_minutes_limit(
                org_id=org_id,
                monthly_minutes=requested_plan.monthly_minutes,
            )
        except Exception:
            logger.exception("set_org_monthly_minutes_limit_failed org=%s plan=%s", org_id, requested_plan.key)

        if event_type == "customer.subscription.deleted":
            next_billing["status"] = "canceled"
            next_billing["trialEndsAt"] = None
            stripe_status = "canceled"
            _send_billing_email("subscription_canceled", org_id=org_id)
        if stripe_status == "past_due":
            if not next_billing.get("graceEndsAt"):
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
            _send_billing_email("payment_recovered", org_id=org_id)

    elif event_type == "invoice.payment_failed":
        # Only act on subscription invoices; ignore setup intents and one-time charges.
        if _clean(payload_obj.get("subscription")):
            next_billing["status"] = "past_due"
            # Only set graceEndsAt on the first failure; don't reset on each Stripe retry.
            if not next_billing.get("graceEndsAt"):
                next_billing["graceEndsAt"] = now + timedelta(days=int(BILLING_CONFIG.grace_days))
                _send_billing_email("payment_failed", org_id=org_id)

    next_billing.setdefault("entitlements", {})
    next_billing["entitlements"]["canStartService"] = _can_start_from_status(
        status=str(next_billing.get("status") or ""),
        grace_ends_at=next_billing.get("graceEndsAt") if isinstance(next_billing.get("graceEndsAt"), datetime) else None,
        now=now,
    )

    try:
        multichurch_store.set_org_billing_profile(org_id=org_id, billing=next_billing)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "billing_profile_update_failed") from exc
    return {"ok": True, "eventId": event_id}
