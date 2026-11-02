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
from google.api_core import exceptions as _gexc
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
from app.services.multichurch_store import (
    multichurch_store,
    StaleBillingProfileError,
    StaleBillingPatchError,
)
from app.services import email_service

router = APIRouter(tags=["billing"])


def _send_billing_email(event: str, *, org_id: str, plan_key: str = "", old_plan_key: str = "", effective: str = "immediate") -> None:
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
        elif event == "plan_changed":
            email_service.send_plan_changed_email(
                admin_emails=admin_emails,
                org_name=org_name,
                old_plan_key=old_plan_key,
                new_plan_key=plan_key,
                effective=effective,
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
    interval: str = Field(default="month", pattern=r"^(month|year)$")

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
    """Resolve a Stripe `price_...` id to a plan key by consulting BOTH
    the monthly and annual price-id mappings. A customer who upgrades or
    downgrades across intervals via the Customer Portal receives a
    `customer.subscription.updated` carrying the NEW interval's price id;
    without the annual-map lookup the resolver would fall back to the
    stale caller-provided plan key.
    """
    clean = _clean_price_id(price_id)
    if not clean:
        return plan_spec(fallback_plan_key).key
    for plan_key, configured_price in BILLING_CONFIG.stripe_price_ids.items():
        if _clean_price_id(configured_price) and _clean_price_id(configured_price) == clean:
            return plan_key
    for plan_key, configured_price in BILLING_CONFIG.stripe_price_ids_annual.items():
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


def _normalize_billing_refs(
    *, billing: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Normalize the three Stripe reference fields (whitespace / case
    cleanup → None for empty-after-clean). Returns:
      - `next_billing`: a local snapshot with the normalized values.
      - `corrections`: ONLY the fields that needed correction, as
        `{field: new_normalized_value}`. Empty when nothing changed.
      - `observed`: for each corrected field, the pre-correction
        stored value — the identity the caller OBSERVED when it
        decided to correct that field. The persister uses this as
        the `expected=` precondition on the targeted patch so a
        concurrent writer who has since changed that field is not
        overwritten with stale normalized data.
    """
    next_billing = dict(billing or {})
    corrections: Dict[str, Any] = {}
    observed: Dict[str, Any] = {}
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
                corrections[key] = normalized
                observed[key] = raw_value
        elif raw_value not in (None, ""):
            next_billing[key] = None
            corrections[key] = None
            observed[key] = raw_value
    return next_billing, corrections, observed


def _persist_normalized_refs(
    *, org_id: str, corrections: Dict[str, Any], observed: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Review-pass v9 gap #1: send ONLY the fields the normalization
    actually corrected, and send them conditionally — the current
    stored value of each must still equal `observed` or the patch
    is rejected. On rejection, log and abandon the stale correction;
    the field has already been written to a newer value by someone
    else (a webhook, another normalize path), so our stale normalized
    value would downgrade it.
    """
    if not corrections:
        return None
    try:
        return multichurch_store.patch_org_billing_fields(
            org_id=org_id,
            updates=corrections,
            expected=observed,
        )
    except StaleBillingPatchError as exc:
        logger.info(
            "normalize_refs_abandoned_stale_patch org=%s mismatches=%s",
            org_id, list(exc.mismatches.keys()),
        )
        try:
            return multichurch_store.get_org_billing_profile(org_id=org_id)
        except Exception:
            return None


def _clear_stale_stripe_refs(
    *,
    org_id: str,
    billing: Dict[str, Any],
    failed_customer_id: Optional[str] = None,
    failed_subscription_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Review-pass v9 gap #2: cleanup is called when a specific
    Stripe lookup failed ("No such customer: cus_X" / "No such
    subscription: sub_Y"). Only clear the stored refs when they
    STILL match the identity whose lookup failed — otherwise a
    concurrent webhook has already replaced the obsolete references
    with new, valid ones, and clearing would erase valid state.
    """
    stripe_customer_id = (billing or {}).get("stripeCustomerId") or None
    stripe_subscription_id = (billing or {}).get("stripeSubscriptionId") or None
    stripe_price_id = (billing or {}).get("priceId") or None
    current_period_start = (billing or {}).get("currentPeriodStart") or None
    current_period_end = (billing or {}).get("currentPeriodEnd") or None
    cleared: Dict[str, Any] = {}
    if stripe_customer_id is not None:
        cleared["stripeCustomerId"] = None
    if stripe_subscription_id is not None:
        cleared["stripeSubscriptionId"] = None
    if stripe_price_id is not None:
        cleared["priceId"] = None
    if current_period_start is not None:
        cleared["currentPeriodStart"] = None
    if current_period_end is not None:
        cleared["currentPeriodEnd"] = None
    if not cleared:
        return billing
    # Build the identity-match precondition from the caller's intent.
    expected: Dict[str, Any] = {}
    if failed_customer_id is not None:
        expected["stripeCustomerId"] = failed_customer_id
    if failed_subscription_id is not None:
        expected["stripeSubscriptionId"] = failed_subscription_id
    # If no caller-supplied identity was given, fall back to the
    # observed billing's own refs — still better than no check.
    if not expected:
        if stripe_customer_id is not None:
            expected["stripeCustomerId"] = stripe_customer_id
        if stripe_subscription_id is not None:
            expected["stripeSubscriptionId"] = stripe_subscription_id
    try:
        return multichurch_store.patch_org_billing_fields(
            org_id=org_id, updates=cleared, expected=expected or None,
        )
    except StaleBillingPatchError as exc:
        logger.info(
            "clear_stale_refs_abandoned org=%s mismatches=%s",
            org_id, list(exc.mismatches.keys()),
        )
        try:
            return multichurch_store.get_org_billing_profile(org_id=org_id)
        except Exception:
            return billing


def _create_missing_stripe_customer(
    *,
    client: StripeBillingClient,
    org_id: str,
    billing: Dict[str, Any],
    user: AuthenticatedUser,
    idempotency_suffix: str = "",
) -> tuple[str, Dict[str, Any]]:
    """Create a Stripe customer and persist its id.

    Review-pass v9 gap: the Stripe create_customer call is slow;
    a concurrent webhook may land `customer.subscription.created`
    with a brand-new customer id during the gap between our
    pre-call read and our post-call write. The helper now
    OBSERVES the customer id before the Stripe call and makes
    the final patch conditional on that observed value. If a
    webhook has since written a different id, the patch is
    rejected and the helper REUSES that newer id — it does NOT
    overwrite with the one we just created locally.
    """
    # Review-pass v10 narrow fix: preserve the EXACT observed stored
    # representation for the atomic precondition. A previous version
    # ran the raw value through `_clean_customer_id(...) or None`,
    # which collapsed both "" and None to None. The store's
    # `patch_org_billing_fields` compares `expected` against the raw
    # stored value, so expected=None against stored="" always raised
    # StaleBillingPatchError — even with zero concurrent writers —
    # and the handler returned 409 instead of recovering. The
    # precondition is about identity-change, not formatting; the
    # Stripe call doesn't care about leading whitespace in a value
    # we observed and are about to replace, so we observe raw.
    observed_customer_id = (billing or {}).get("stripeCustomerId")
    customer = client.create_customer(
        email=user.email,
        name=user.displayName,
        metadata={"orgId": org_id, "createdByUid": user.uid},
        idempotency_key=f"create-customer-{org_id}{idempotency_suffix}",
    )
    customer_id = _clean_customer_id((customer or {}).get("id"))
    if not customer_id:
        raise HTTPException(status_code=502, detail="stripe_customer_create_failed")
    try:
        saved = multichurch_store.patch_org_billing_fields(
            org_id=org_id,
            updates={"stripeCustomerId": customer_id},
            expected={"stripeCustomerId": observed_customer_id},
        )
        return customer_id, saved
    except StaleBillingPatchError as exc:
        # A webhook (e.g. customer.subscription.created) committed a
        # different stripeCustomerId while our create_customer call
        # was in flight. Reload, and if the current stored id is a
        # valid new id, REUSE it. Our locally-created Stripe
        # customer becomes an orphan on Stripe's side — acceptable:
        # the idempotency_key keeps it unique, and leaving it alone
        # is safer than overwriting a Stripe-tied new identity.
        logger.info(
            "create_missing_customer_conflict_reuse org=%s observed=%s current=%s",
            org_id,
            observed_customer_id,
            (exc.mismatches.get("stripeCustomerId") or (None, None))[1],
        )
        refreshed = multichurch_store.get_org_billing_profile(org_id=org_id)
        existing_id = _clean_customer_id((refreshed or {}).get("stripeCustomerId")) or None
        if existing_id:
            return existing_id, refreshed
        # No valid replacement in the store (should not happen if the
        # mismatch was a real new id, but defensively): surface a
        # retryable conflict rather than overwrite.
        raise HTTPException(
            status_code=409, detail="stripe_customer_write_conflict",
        ) from exc


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
        attempted_customer_id = customer_id
        try:
            return _lookup_customer_subscription(client, customer_id=customer_id)
        except StripeClientError as exc:
            if _is_missing_customer_error(exc):
                # Fix review-pass v9 gap #2: pass the identity whose
                # lookup failed so cleanup verifies the current stored
                # stripeCustomerId still matches it before clearing
                # anything. If a concurrent webhook has already
                # replaced the obsolete customer with a new valid one,
                # the clear is abandoned and the new refs survive.
                billing = _clear_stale_stripe_refs(
                    org_id=org_id,
                    billing=billing,
                    failed_customer_id=attempted_customer_id or None,
                )
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
    # Fix review item #7 (v7): the hydrate helper reads billing and
    # retrieves subscription state from Stripe asynchronously; a
    # webhook or another hydrate call may commit a newer billing
    # profile in the gap between our read and our write. Pass CAS
    # token so a stale hydrate call does NOT overwrite the newer
    # state. On mismatch, abandon and return the current stored
    # billing — the authoritative state was already committed by
    # the other writer.
    cas_token = billing.get("updatedAt") if isinstance(billing, dict) else None
    try:
        return multichurch_store.set_org_billing_profile(
            org_id=org_id,
            billing=next_billing,
            expected_billing_updated_at=cas_token,
        )
    except StaleBillingProfileError:
        logger.info(
            "hydrate_cas_mismatch_abandoning_stale_refresh org=%s", org_id,
        )
        try:
            return multichurch_store.get_org_billing_profile(org_id=org_id)
        except Exception:
            return billing


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
    billing_local, corrections, observed = _normalize_billing_refs(billing=billing)
    if corrections:
        refreshed = _persist_normalized_refs(
            org_id=org_id, corrections=corrections, observed=observed,
        )
        billing = refreshed if refreshed is not None else billing_local
    else:
        billing = billing_local

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
    use_annual = body.interval == "year"
    price_id = _clean_price_id(
        (BILLING_CONFIG.stripe_price_ids_annual if use_annual else BILLING_CONFIG.stripe_price_ids).get(target_plan.key)
    )
    if not price_id:
        raise HTTPException(status_code=503, detail="billing_not_configured")
    if not _is_valid_price_id(price_id):
        raise HTTPException(status_code=503, detail="billing_not_configured")
    if not _clean(BILLING_CONFIG.stripe_secret_key):
        raise HTTPException(status_code=503, detail="billing_not_configured")

    # fix-batch-2: duplicate-subscription prevention + stable idempotency.
    # `claim_checkout_attempt` is a single transactional read-decide-write
    # that serializes concurrent callers on the same org and returns one
    # of four actions.
    #
    # Error-mapping (follow-up to the merge-readiness flake diagnosis):
    # the transactional commit path can surface
    # `google.api_core.exceptions.Aborted` (and, when the Firestore client
    # library's own Retry wrapper exhausts, `google.api_core.exceptions.RetryError`
    # with `.cause` set to an Aborted) under contention. Distinguish by
    # exception TYPE — not by matching the English message string, since
    # unrelated code paths may raise RuntimeError or similar with
    # overlapping text — and map ONLY the Aborted/RetryError-wrapping-Aborted
    # case to HTTP 503 with a stable public detail. Occurs BEFORE any Stripe
    # operation is initiated, so a retry-exhausted store transaction does
    # not leak into a half-created Stripe session. No application-level
    # retry is added here; the Firestore client library owns its own retry
    # policy, and the HTTP 503 lets the caller decide whether to retry.
    try:
        claim = multichurch_store.claim_checkout_attempt(
            org_id=body.orgId,
            plan_key=target_plan.key,
            interval=body.interval,
            success_url=body.successUrl,
            cancel_url=body.cancelUrl,
            requested_by_uid=user.uid,
        )
    except _gexc.RetryError as exc:
        _cause = getattr(exc, "cause", None)
        if isinstance(_cause, _gexc.Aborted):
            logger.warning(
                "checkout_claim_firestore_retry_exhausted org=%s cause=%s",
                body.orgId, type(_cause).__name__,
            )
            raise HTTPException(
                status_code=503, detail="checkout_backend_unavailable",
            ) from exc
        # RetryError with a non-Aborted cause is a different failure
        # mode (e.g. DeadlineExceeded with no retryable child); preserve
        # it rather than silently reclassifying.
        raise
    except _gexc.Aborted as exc:
        logger.warning(
            "checkout_claim_firestore_aborted org=%s", body.orgId,
        )
        raise HTTPException(
            status_code=503, detail="checkout_backend_unavailable",
        ) from exc
    except ValueError as exc:
        # Completion to the two except branches above: the Firestore
        # Python client's `@firestore.transactional` decorator surfaces
        # a retry-exhausted ABORTED to the caller as `ValueError("Failed
        # to commit transaction in N attempts.")` with `__cause__` set
        # to the underlying `google.api_core.exceptions.Aborted`. That
        # path is NOT caught by the `_gexc.Aborted` or `_gexc.RetryError`
        # branches above; it reaches this `ValueError` branch instead.
        # Distinguish it from genuine validation errors by inspecting
        # `__cause__` (an attribute, not a message substring) BEFORE
        # the existing 404 / 400 mapping.
        if isinstance(exc.__cause__, _gexc.Aborted):
            logger.warning(
                "checkout_claim_firestore_retry_exhausted_via_valueerror "
                "org=%s cause=%s",
                body.orgId, type(exc.__cause__).__name__,
            )
            raise HTTPException(
                status_code=503, detail="checkout_backend_unavailable",
            ) from exc
        detail = str(exc)
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "billing_profile_fetch_failed") from exc

    if claim.get("action") == "already_subscribed":
        # Existing subscribers manage plan via `change_plan` or the Portal,
        # not a second checkout — prevents H1.
        raise HTTPException(status_code=409, detail="subscription_already_active")
    if claim.get("action") == "pending_different_plan":
        # A pending non-expired attempt exists for a DIFFERENT plan or
        # interval. Protects against accidental mid-attempt plan switches
        # creating a second Stripe session on the same customer.
        raise HTTPException(status_code=409, detail="pending_checkout_different_plan")
    if claim.get("action") == "pending_params_mismatch":
        # Same plan+interval but DIFFERENT success_url/cancel_url/uid.
        # Reusing the stored Idempotency-Key with changed parameters
        # would be a Stripe-contract violation. Refuse.
        raise HTTPException(status_code=409, detail="pending_checkout_params_mismatch")
    if claim.get("action") == "reuse":
        reused_url = _clean(claim.get("sessionUrl"))
        reused_id = _clean(claim.get("sessionId"))
        if reused_url and reused_id:
            return {"url": reused_url, "sessionId": reused_id, "reused": True}
        # If a prior attempt existed but we never persisted the Stripe
        # session (e.g. the earlier POST crashed between claim and record),
        # the attemptId is still valid: fall through and let Stripe return
        # the already-cached session via the Idempotency-Key.
    attempt_id = _clean(claim.get("attemptId"))
    if not attempt_id:
        # Defensive: should not happen for action in {"create","reuse"}.
        raise HTTPException(status_code=500, detail="billing_profile_fetch_failed")

    try:
        billing = multichurch_store.get_org_billing_profile(org_id=body.orgId)
    except ValueError as exc:
        detail = str(exc)
        if detail == "org_not_found":
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail or "billing_profile_fetch_failed") from exc

    billing_local, corrections, observed = _normalize_billing_refs(billing=billing)
    if corrections:
        refreshed = _persist_normalized_refs(
            org_id=body.orgId, corrections=corrections, observed=observed,
        )
        billing = refreshed if refreshed is not None else billing_local
    else:
        billing = billing_local

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
            allow_no_payment_method=False,
            metadata={"orgId": body.orgId, "planKey": target_plan.key, "interval": body.interval, "requestedByUid": user.uid},
            idempotency_key=attempt_id,
        )
    except StripeClientError as exc:
        # If the stored Stripe customer belongs to a different account/mode, recreate and retry once.
        if customer_id and _is_missing_customer_error(exc):
            try:
                # Review-pass v9 gap: identity-matched cleanup first;
                # if a concurrent webhook has committed a new valid
                # customer, reuse it instead of creating a replacement.
                refreshed_billing = _clear_stale_stripe_refs(
                    org_id=body.orgId,
                    billing=billing,
                    failed_customer_id=customer_id or None,
                )
                refreshed_customer_id = _clean_customer_id(
                    (refreshed_billing or {}).get("stripeCustomerId"),
                )
                if refreshed_customer_id and refreshed_customer_id != customer_id:
                    customer_id, billing = refreshed_customer_id, refreshed_billing
                else:
                    customer_id, billing = _create_missing_stripe_customer(
                        client=client,
                        org_id=body.orgId,
                        billing=refreshed_billing,
                        user=user,
                        idempotency_suffix="-retry",
                    )
                session = client.create_checkout_session(
                    customer_id=customer_id,
                    price_id=price_id,
                    success_url=body.successUrl,
                    cancel_url=body.cancelUrl,
                    trial_days=0,
                    allow_no_payment_method=False,
                    metadata={"orgId": body.orgId, "planKey": target_plan.key, "interval": body.interval, "requestedByUid": user.uid},
                    idempotency_key=attempt_id,
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
    # Persist the Stripe-returned session so future retries with the same
    # logical attempt (same user, same plan/interval, within TTL) reuse it.
    try:
        multichurch_store.record_checkout_attempt_session(
            org_id=body.orgId,
            attempt_id=attempt_id,
            session_id=session_id,
            session_url=checkout_url,
        )
    except Exception:
        logger.exception("record_checkout_attempt_session_failed org=%s attempt=%s", body.orgId, attempt_id)
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

    billing_local, corrections, observed = _normalize_billing_refs(billing=billing)
    if corrections:
        refreshed = _persist_normalized_refs(
            org_id=body.orgId, corrections=corrections, observed=observed,
        )
        billing = refreshed if refreshed is not None else billing_local
    else:
        billing = billing_local

    try:
        billing = _hydrate_billing_from_stripe(
            org_id=body.orgId,
            billing=billing,
            force_refresh=True,
        )
    except StripeClientError as exc:
        logger.warning("stripe_portal_sync_failed org=%s: %s", body.orgId, exc)
        raise HTTPException(status_code=502, detail="stripe_sync_failed") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "billing_status_sync_failed") from exc

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
                refreshed_billing = _clear_stale_stripe_refs(
                    org_id=body.orgId,
                    billing=billing,
                    failed_customer_id=customer_id or None,
                )
                # Review-pass v9 gap: if `_clear_stale_stripe_refs`
                # abandoned the clear because a concurrent webhook
                # replaced our obsolete customer with a new valid
                # id, REUSE that new id instead of creating a
                # replacement customer blindly.
                refreshed_customer_id = _clean_customer_id(
                    (refreshed_billing or {}).get("stripeCustomerId"),
                )
                if refreshed_customer_id and refreshed_customer_id != customer_id:
                    customer_id, billing = refreshed_customer_id, refreshed_billing
                else:
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

    billing_local, corrections, observed = _normalize_billing_refs(billing=billing)
    if corrections:
        refreshed = _persist_normalized_refs(
            org_id=body.orgId, corrections=corrections, observed=observed,
        )
        billing = refreshed if refreshed is not None else billing_local
    else:
        billing = billing_local

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
        _send_billing_email("plan_changed", org_id=body.orgId, plan_key=target_plan.key, old_plan_key=current_plan_key, effective="immediate")
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
    # Review-pass v8 gap #1: patch only the pending-plan fields.
    try:
        multichurch_store.patch_org_billing_fields(
            org_id=body.orgId,
            updates={
                "pendingPlanKey": target_plan.key,
                "pendingPlanDate": period_end_dt,
                "pendingScheduleId": schedule_id,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "billing_profile_update_failed") from exc

    effective_iso = period_end_dt.isoformat()
    _send_billing_email("plan_changed", org_id=body.orgId, plan_key=target_plan.key, old_plan_key=current_plan_key, effective=effective_iso)
    return {
        "ok": True,
        "effective": effective_iso,
        "pendingPlanKey": target_plan.key,
        "pendingPlanDate": effective_iso,
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

    # Review-pass v8 gap #1: patch only the pending-plan fields.
    try:
        multichurch_store.patch_org_billing_fields(
            org_id=body.orgId,
            updates={
                "pendingPlanKey": None,
                "pendingPlanDate": None,
                "pendingScheduleId": None,
            },
        )
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

    # fix-batch-1: claim-and-complete replaces mark-at-start dedup.
    #   - "fresh"      → we hold the lease; process + complete at end.
    #   - "deduped"    → prior delivery already COMPLETED. Safe to return.
    #   - "concurrent" → prior delivery holds an active lease. Return 409
    #                    so Stripe retries after its back-off (prevents
    #                    double-persistence under racing deliveries).
    try:
        claim = multichurch_store.try_claim_billing_event(
            event_id=event_id,
            event_type=event_type,
            payload={"type": event_type},
        )
    except ValueError as exc:
        detail = str(exc)
        if detail in {"invalid_event_id", "invalid_event_type"}:
            raise HTTPException(status_code=400, detail=detail) from exc
        raise HTTPException(status_code=400, detail="invalid_webhook_event") from exc
    claim_status = (claim or {}).get("status")
    claim_token = (claim or {}).get("claimToken")
    if claim_status == "deduped":
        return {"ok": True, "eventId": event_id, "deduped": True}
    if claim_status == "concurrent":
        # 409 is a non-2xx that Stripe retries. The in-flight sibling
        # delivery completes first; the retry then finds "deduped".
        raise HTTPException(status_code=409, detail="webhook_concurrent_delivery")

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

    # CAS baseline (fix review item #1): capture the billing-profile
    # version we read so the atomic apply can reject the write if a
    # sibling delivery updated the org between our read and our write.
    # Every writer that mutates billing fields (checkout attempt claim,
    # session record, webhook apply) bumps `billing.updatedAt`, so a
    # race between a webhook handler and a concurrent /checkout-session
    # or Stripe-session record is caught here.
    expected_billing_updated_at = billing.get("updatedAt") if isinstance(billing, dict) else None

    now = _utcnow()
    next_billing = dict(billing)
    next_billing["updatedAt"] = now

    metadata = payload_obj.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    # Notifications are collected here and dispatched AFTER successful
    # billing persistence (see fix-batch-1 — webhook retry safety). Each
    # entry is passed verbatim to `_send_billing_email`.
    pending_emails: list[dict[str, Any]] = []

    # The monthly-minute value we'll apply ATOMICALLY alongside the
    # billing profile (fix: boundary 1, fenced persistence). None means
    # "don't touch the org-level minute limit on this event."
    monthly_minutes_for_apply: Optional[int] = None

    if event_type == "checkout.session.completed":
        customer_id = _clean(payload_obj.get("customer"))
        subscription_id = _clean(payload_obj.get("subscription"))
        session_id = _clean(payload_obj.get("id"))
        # Fix review item #8 (v7): reconcile `requested_plan` against
        # the subscription's CURRENT state (via Stripe retrieve) when
        # the checkout.session.completed is DELAYED for an already-
        # active subscription. Using the event's stale metadata.planKey
        # would downgrade a Growth-upgraded sub back to Starter.
        # For the initial-checkout case (no current subscription id or
        # mismatched id handled by the obsolete/terminal guards below),
        # trust the checkout metadata — that's the authoritative plan
        # the user just purchased.
        current_sub_id_pre = _clean(next_billing.get("stripeSubscriptionId"))
        reconciled_price_id: Optional[str] = None
        if subscription_id and current_sub_id_pre and current_sub_id_pre == subscription_id:
            try:
                sub_live = _stripe_client().retrieve_subscription(
                    subscription_id=subscription_id,
                )
            except StripeClientError as exc:
                logger.warning(
                    "checkout_completed_reconcile_retrieve_failed org=%s sub=%s: %s",
                    org_id, subscription_id, exc,
                )
                raise HTTPException(
                    status_code=502, detail="stripe_reconcile_failed",
                ) from exc
            price_id_live = _clean_price_id(_price_id_from_subscription(sub_live or {}))
            if price_id_live:
                resolved_plan_key = _resolve_plan_from_price_id(
                    price_id_live,
                    fallback_plan_key=str(next_billing.get("planKey") or metadata.get("planKey") or "trial"),
                )
                requested_plan = plan_spec(resolved_plan_key)
                reconciled_price_id = price_id_live
            else:
                requested_plan = plan_spec(
                    metadata.get("planKey") or next_billing.get("planKey") or "trial"
                )
        else:
            requested_plan = plan_spec(metadata.get("planKey") or next_billing.get("planKey") or "trial")

        # Review-pass identity checks (fix #2):
        #   - Terminal-arrival guard requires BOTH matching
        #     subscription id AND matching session id — otherwise a
        #     late completion for an obsolete subscription could
        #     overwrite the current active one.
        #   - Pending-attempt clearing is now gated on sessionId
        #     match (if the attempt captured one) rather than
        #     plan+interval, because distinct attempts can share plan
        #     and interval.
        current_sub_id = str(next_billing.get("stripeSubscriptionId") or "").strip()
        current_status = _clean(next_billing.get("status")).lower()
        is_terminal_late_arrival = bool(
            subscription_id
            and current_sub_id == subscription_id
            and current_status in {"canceled", "incomplete_expired", "unpaid"}
        )
        pending_attempt = next_billing.get("pendingCheckoutAttempt") or {}
        pending_session_matches = (
            isinstance(pending_attempt, dict)
            and bool(pending_attempt.get("sessionId"))
            and str(pending_attempt.get("sessionId") or "") == session_id
        )

        # Obsolete-subscription guard: an active subscription is on
        # record (not terminal) and the incoming completion refers to
        # a DIFFERENT subscription id. The event is for an obsolete
        # or orphan subscription (manually created in dashboard, or a
        # delayed older session); do not overwrite the current
        # stripeSubscriptionId / planKey / limits. Treat the event as
        # a late-arrival (mark completed; no mutation).
        is_obsolete_subscription_arrival = bool(
            subscription_id
            and current_sub_id
            and current_sub_id != subscription_id
            and current_status in {"active", "past_due", "trialing"}
        )

        if is_terminal_late_arrival or is_obsolete_subscription_arrival:
            logger.warning(
                "checkout.session.completed late/obsolete arrival "
                "org=%s session=%s sub=%s current_sub=%s current_status=%s",
                org_id, session_id, subscription_id, current_sub_id, current_status,
            )
            result = multichurch_store.apply_webhook_event_atomic(
                org_id=org_id,
                billing=None,  # NO billing snapshot — late arrival must not resurrect state.
                monthly_minutes=None,
                event_id=event_id,
                claim_token=claim_token,
                expected_billing_updated_at=expected_billing_updated_at,
            )
            if not result.get("applied"):
                reason = result.get("reason") or "unknown"
                if reason in {"stale_snapshot", "claim_lost"}:
                    raise HTTPException(status_code=409, detail="webhook_claim_lost")
                return {"ok": True, "eventId": event_id, "lateArrival": True, "noop": reason}
            return {"ok": True, "eventId": event_id, "lateArrival": True}

        if customer_id:
            next_billing["stripeCustomerId"] = customer_id
        if subscription_id:
            next_billing["stripeSubscriptionId"] = subscription_id
        next_billing["planKey"] = requested_plan.key
        # Fix review item #2 (v8): when the delayed-checkout
        # reconciliation determined a current price_id from the live
        # subscription, persist it alongside planKey/limits so the
        # stored priceId is never stale against the resolved plan.
        if reconciled_price_id:
            next_billing["priceId"] = reconciled_price_id
        next_billing.setdefault("limits", {})
        next_billing["limits"]["maxServiceKeys"] = requested_plan.max_service_keys
        # Fix issue #2 (prior pass): represent "awaiting subscription
        # confirmation" as a dedicated field, NOT a fabricated
        # entitlement status.
        if subscription_id:
            next_billing["pendingSubscriptionConfirmation"] = True
        # Clear pending-checkout-attempt only when the completion
        # SESSION-ID matches the recorded one (review-pass identity
        # fix). A delayed completion for a sibling attempt that
        # happens to share plan+interval MUST NOT erase a replacement.
        if pending_session_matches:
            next_billing["pendingCheckoutAttempt"] = None
        monthly_minutes_for_apply = requested_plan.monthly_minutes
        # Only email "subscription_started" the FIRST time we see this
        # subscription id — avoids double-emailing on a delayed
        # checkout.session.completed for an already-active sub.
        if requested_plan.key != "trial" and (
            not current_sub_id_pre or current_sub_id_pre != subscription_id
        ):
            pending_emails.append({"event": "subscription_started", "plan_key": requested_plan.key})

    elif event_type in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
        # Obsolete-subscription guard (pre-reconciliation).
        incoming_sub_id = str(payload_obj.get("id") or "").strip()
        current_sub_id = str(next_billing.get("stripeSubscriptionId") or "").strip()
        current_status = _clean(next_billing.get("status")).lower()
        if (
            incoming_sub_id
            and current_sub_id
            and incoming_sub_id != current_sub_id
            and current_status in {"active", "past_due", "trialing"}
        ):
            logger.warning(
                "customer.subscription.* arrival for obsolete sub "
                "org=%s incoming_sub=%s current_sub=%s current_status=%s",
                org_id, incoming_sub_id, current_sub_id, current_status,
            )
            result = multichurch_store.apply_webhook_event_atomic(
                org_id=org_id,
                billing=None,
                monthly_minutes=None,
                event_id=event_id,
                claim_token=claim_token,
                expected_billing_updated_at=expected_billing_updated_at,
            )
            if not result.get("applied"):
                reason = result.get("reason") or "unknown"
                if reason in {"stale_snapshot", "claim_lost"}:
                    raise HTTPException(status_code=409, detail="webhook_claim_lost")
                return {"ok": True, "eventId": event_id, "obsoleteSubscription": True, "noop": reason}
            return {"ok": True, "eventId": event_id, "obsoleteSubscription": True}

        # Fix review item #1 (v6): current-state reconciliation.
        # The event payload is a POINT-IN-TIME snapshot and Stripe
        # does NOT guarantee ordering by `created`. A later-delivered
        # older event for the same subscription could carry stale
        # status/price/cancel_at_period_end — using it directly
        # restores canceled subs to active, downgrades upgrades, etc.
        # Retrieve the CURRENT subscription from Stripe and build the
        # snapshot from THAT instead of the payload. For
        # `subscription.deleted` the retrieval may 404 — treat that
        # as "subscription is gone", which matches the deleted event.
        reconciled_payload: Dict[str, Any] = payload_obj
        if event_type != "customer.subscription.deleted":
            try:
                sub_live = _stripe_client().retrieve_subscription(
                    subscription_id=incoming_sub_id,
                )
                if isinstance(sub_live, dict) and sub_live:
                    reconciled_payload = sub_live
            except StripeClientError as exc:
                # Fix review item #2: transient retrieval failures
                # must NOT complete the event. Raise 502 so Stripe
                # retries; the event stays claimed (and ages out).
                logger.warning(
                    "subscription_reconcile_retrieve_failed org=%s sub=%s: %s",
                    org_id, incoming_sub_id, exc,
                )
                raise HTTPException(
                    status_code=502, detail="stripe_reconcile_failed",
                ) from exc
        next_billing = _merge_subscription_snapshot(
            billing=next_billing,
            stripe_subscription=reconciled_payload,
            now=now,
        )
        stripe_status = _clean(next_billing.get("status")).lower()
        # fix-batch-3 (H3): the previous override — `metadata.get("planKey")
        # or next_billing.get("planKey")` — allowed a STALE
        # `subscription.metadata.planKey` (set at the original checkout and
        # never updated by the Customer Portal) to overwrite the
        # price-id-resolved plan key just set by
        # `_merge_subscription_snapshot`. Trust the price-id-resolved
        # value; `_resolve_plan_from_price_id` now consults both monthly
        # and annual mappings and falls back to the stored planKey only
        # when the price_id is unrecognised.
        requested_plan = plan_spec(str(next_billing.get("planKey") or "trial"))
        next_billing.setdefault("limits", {})
        next_billing["limits"]["maxServiceKeys"] = requested_plan.max_service_keys

        # Fix issue #2: once a real status arrives from Stripe, clear
        # the "awaiting confirmation" flag that
        # checkout.session.completed may have set.
        if "pendingSubscriptionConfirmation" in next_billing:
            next_billing["pendingSubscriptionConfirmation"] = False

        # Clear pending downgrade if the active plan now matches the pending target
        pending_plan_key = _clean(next_billing.get("pendingPlanKey")).lower()
        if pending_plan_key and next_billing.get("planKey") == pending_plan_key:
            next_billing["pendingPlanKey"] = None
            next_billing["pendingPlanDate"] = None
            next_billing["pendingScheduleId"] = None

        # Monthly-minute limit will be applied ATOMICALLY alongside
        # the billing-profile write via `apply_webhook_event_atomic`
        # (fix: boundary 1, fenced persistence).
        monthly_minutes_for_apply = requested_plan.monthly_minutes

        if event_type == "customer.subscription.deleted":
            next_billing["status"] = "canceled"
            next_billing["trialEndsAt"] = None
            stripe_status = "canceled"
            pending_emails.append({"event": "subscription_canceled"})
        elif event_type == "customer.subscription.updated":
            # Send cancellation email immediately when user sets cancel_at_period_end=true
            # (e.g. via Customer Portal). The subscription.deleted event fires much later.
            just_set_cancel = bool(payload_obj.get("cancel_at_period_end")) and not bool(billing.get("cancelAtPeriodEnd"))
            if just_set_cancel:
                pending_emails.append({"event": "subscription_canceled"})
        if stripe_status == "past_due":
            if not next_billing.get("graceEndsAt"):
                next_billing["graceEndsAt"] = now + timedelta(days=int(BILLING_CONFIG.grace_days))
        elif stripe_status in {"active", "trialing"}:
            next_billing["graceEndsAt"] = None
        else:
            next_billing["graceEndsAt"] = next_billing.get("graceEndsAt")

    elif event_type == "invoice.payment_succeeded":
        # Fix review item #1 (v6): on `payment_succeeded`, retrieve
        # the subscription from Stripe and act on its CURRENT state.
        # The old gate — "current stored status in {past_due, ...}
        # AND same subscription id" — would set `active` when an
        # OLDER invoice.payment_succeeded arrives while the
        # subscription is still past_due for newer debt. Reconciling
        # against Stripe prevents that.
        invoice_sub_id = _clean(payload_obj.get("subscription"))
        current_sub_id = _clean(next_billing.get("stripeSubscriptionId"))
        if invoice_sub_id and current_sub_id and invoice_sub_id == current_sub_id:
            try:
                sub_live = _stripe_client().retrieve_subscription(
                    subscription_id=invoice_sub_id,
                )
            except StripeClientError as exc:
                # Fix review item #2 (v6): required reconciliation
                # must remain retryable. Raise 502 so Stripe retries;
                # event is NOT completed.
                logger.warning(
                    "payment_succeeded_reconcile_retrieve_failed org=%s sub=%s: %s",
                    org_id, invoice_sub_id, exc,
                )
                raise HTTPException(
                    status_code=502, detail="stripe_reconcile_failed",
                ) from exc
            stripe_status = _status_from_stripe(
                str((sub_live or {}).get("status") or ""),
                fallback_status=_clean(next_billing.get("status")).lower(),
            )
            current_local_status = _clean(next_billing.get("status")).lower()
            # Only transition to active when Stripe itself reports
            # active AND we were in a recovery-eligible state locally.
            if stripe_status == "active" and current_local_status in {"past_due", "incomplete", "unpaid"}:
                next_billing["status"] = "active"
                next_billing["graceEndsAt"] = None
                pending_emails.append({"event": "payment_recovered"})

    elif event_type == "invoice.payment_failed":
        # Fix review item #1 (v5+v6): reconcile against Stripe's
        # current subscription status. Only apply when Stripe itself
        # still reports a payment-not-recovered status.
        invoice_sub_id = _clean(payload_obj.get("subscription"))
        current_sub_id = _clean(next_billing.get("stripeSubscriptionId"))
        if invoice_sub_id and current_sub_id and invoice_sub_id == current_sub_id:
            try:
                sub_live = _stripe_client().retrieve_subscription(
                    subscription_id=invoice_sub_id,
                )
            except StripeClientError as exc:
                # Fix review item #2 (v6): make retrieval failure
                # retryable (raise 502; event stays claimed).
                logger.warning(
                    "payment_failed_reconcile_retrieve_sub_failed org=%s sub=%s: %s",
                    org_id, invoice_sub_id, exc,
                )
                raise HTTPException(
                    status_code=502, detail="stripe_reconcile_failed",
                ) from exc
            stripe_status = _status_from_stripe(
                str((sub_live or {}).get("status") or ""),
                fallback_status=_clean(next_billing.get("status")).lower(),
            )
            if stripe_status in {"past_due", "unpaid", "incomplete"}:
                next_billing["status"] = "past_due"
                if not next_billing.get("graceEndsAt"):
                    next_billing["graceEndsAt"] = now + timedelta(days=int(BILLING_CONFIG.grace_days))
                    pending_emails.append({"event": "payment_failed"})

    next_billing.setdefault("entitlements", {})
    next_billing["entitlements"]["canStartService"] = _can_start_from_status(
        status=str(next_billing.get("status") or ""),
        grace_ends_at=next_billing.get("graceEndsAt") if isinstance(next_billing.get("graceEndsAt"), datetime) else None,
        now=now,
    )

    # Fenced apply with CAS (fix review item #1 v5: no event-timestamp
    # watermark; Stripe explicitly documents that `created` MUST NOT
    # determine event ordering). Billing-profile write, monthly-minute
    # write, and event completion happen as a single critical section
    # gated on `claim_token` AND `expected_billing_updated_at`.
    # - `claim_lost` or `stale_snapshot` → 409 so Stripe retries.
    # - `already_completed` or `no_claim` → dedup.
    try:
        result = multichurch_store.apply_webhook_event_atomic(
            org_id=org_id,
            billing=next_billing,
            monthly_minutes=monthly_minutes_for_apply,
            event_id=event_id,
            claim_token=claim_token,
            expected_billing_updated_at=expected_billing_updated_at,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail == "org_not_found":
            multichurch_store.complete_billing_event(
                event_id=event_id, claim_token=claim_token,
            )
            return {"ok": True, "eventId": event_id, "unmatched": True}
        raise HTTPException(status_code=400, detail=detail or "billing_profile_update_failed") from exc
    if not result.get("applied"):
        reason = result.get("reason") or "unknown"
        if reason in {"stale_snapshot", "claim_lost"}:
            raise HTTPException(status_code=409, detail="webhook_claim_lost")
        # already_completed / no_claim — treat as dedup.
        return {"ok": True, "eventId": event_id, "deduped": True, "reason": reason}

    # Notifications fire ONLY after the atomic apply succeeded, so a
    # failed persistence followed by a Stripe retry doesn't double-email.
    for item in pending_emails:
        _send_billing_email(org_id=org_id, **item)

    return {"ok": True, "eventId": event_id}
