"""Centralised transactional email sending via Resend.

All public functions are fire-and-forget: they log failures but never raise,
so callers (especially webhook handlers) are never blocked or forced to 500.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("app.email")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _from_email() -> str:
    return (
        (os.getenv("PASSWORD_RESET_FROM_EMAIL") or "").strip()
        or (os.getenv("AUTH_FROM_EMAIL") or "").strip()
        or (os.getenv("CONTACT_FROM_EMAIL") or "").strip()
        or ""
    )


def _fallback_from_email() -> str:
    return (os.getenv("RESEND_FALLBACK_FROM_EMAIL") or "").strip() or "Worship <onboarding@resend.dev>"


def _brand() -> str:
    return (
        (os.getenv("PASSWORD_RESET_BRAND_NAME") or "").strip()
        or (os.getenv("APP_BRAND_NAME") or "").strip()
        or "Worship Translation"
    )


def _resend_key() -> str:
    return (os.getenv("RESEND_API_KEY") or "").strip()


def _app_url() -> str:
    return (os.getenv("APP_URL") or os.getenv("NEXT_PUBLIC_APP_URL") or "").strip().rstrip("/")


# ---------------------------------------------------------------------------
# Core send helper
# ---------------------------------------------------------------------------

def _is_domain_unverified(status: int, body: str) -> bool:
    if status != 403:
        return False
    low = (body or "").lower()
    return "domain is not verified" in low or "verify your domain" in low


def _send(*, to: List[str], subject: str, html: str, text: str, tag: str) -> None:
    """Send via Resend. Falls back to onboarding@resend.dev if primary domain unverified."""
    key = _resend_key()
    if not key:
        logger.warning("email_skipped tag=%s reason=RESEND_API_KEY_missing", tag)
        return
    from_addr = _from_email()
    if not from_addr:
        logger.warning("email_skipped tag=%s reason=from_email_not_configured", tag)
        return
    fallback = _fallback_from_email()

    payload: Dict[str, Any] = {
        "from": from_addr,
        "to": to,
        "subject": subject,
        "html": html,
        "text": text,
        "tags": [{"name": "source", "value": tag}],
    }

    try:
        with httpx.Client(timeout=10) as client:
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }
            res = client.post("https://api.resend.com/emails", headers=headers, json=payload)
            if _is_domain_unverified(res.status_code, res.text) and fallback and fallback != from_addr:
                logger.warning("email_domain_unverified tag=%s retrying_with_fallback", tag)
                payload["from"] = fallback
                res = client.post("https://api.resend.com/emails", headers=headers, json=payload)
            if res.status_code >= 400:
                logger.error("email_send_failed tag=%s status=%s body=%s", tag, res.status_code, res.text[:400])
    except Exception:
        logger.exception("email_send_exception tag=%s to=%s", tag, to)


# ---------------------------------------------------------------------------
# Shared HTML primitives
# ---------------------------------------------------------------------------

def _btn(label: str, href: str) -> str:
    return (
        f'<p style="margin:22px 0;">'
        f'<a href="{href}" style="display:inline-block;padding:12px 22px;border-radius:999px;'
        f'background:#4f73aa;color:#ffffff;text-decoration:none;font-weight:700;">{label}</a>'
        f"</p>"
    )


def _layout(*, heading: str, body_html: str) -> str:
    brand = _brand()
    return f"""
<div style="font-family:Arial,sans-serif;line-height:1.6;color:#0f172a;max-width:540px;">
  <h2 style="margin-bottom:12px;">{heading}</h2>
  {body_html}
  <p style="margin-top:32px;color:#64748b;font-size:13px;">Thanks,<br />{brand}</p>
</div>""".strip()


# ---------------------------------------------------------------------------
# Email senders
# ---------------------------------------------------------------------------

def send_welcome_email(*, email: str, display_name: Optional[str], org_name: str, org_slug: Optional[str] = None) -> None:
    """Sent to the org owner immediately after they create their church workspace."""
    if not email:
        return
    brand = _brand()
    name = display_name or "there"
    base = _app_url()
    if base and org_slug:
        dashboard_url = f"{base}/host/c/{org_slug}/broadcast"
    elif base:
        dashboard_url = f"{base}/host"
    else:
        dashboard_url = ""
    html = _layout(
        heading=f"Welcome to {brand}",
        body_html=(
            f"<p>Hi {name},</p>"
            f"<p>Your church workspace <strong>{org_name}</strong> is ready. "
            f"You can start a live translation broadcast from your host dashboard.</p>"
            + (_btn("Open Host Dashboard", dashboard_url) if dashboard_url else "")
            + "<p>If you have any questions, just reply to this email.</p>"
        ),
    )
    text = "\n".join([
        f"Welcome to {brand}",
        "",
        f"Hi {name},",
        f"Your church workspace '{org_name}' is ready.",
        "Open your host dashboard to start a live translation broadcast:",
        dashboard_url or "",
        "",
        "Thanks,",
        brand,
    ])
    _send(to=[email], subject=f"Welcome to {brand} — {org_name} is ready", html=html, text=text, tag="welcome")


def send_member_joined_email(
    *,
    admin_emails: List[str],
    member_name: Optional[str],
    member_email: str,
    org_name: str,
    role: str,
) -> None:
    """Notify org owners/admins that a new member accepted an invite."""
    if not admin_emails:
        return
    brand = _brand()
    name = member_name or member_email or "A new member"
    html = _layout(
        heading=f"New team member joined {org_name}",
        body_html=(
            f"<p><strong>{name}</strong> ({member_email}) has joined "
            f"<strong>{org_name}</strong> as <strong>{role}</strong>.</p>"
            f"<p>You can manage team access from the Team tab in your host dashboard.</p>"
        ),
    )
    text = "\n".join([
        f"New team member joined {org_name}",
        "",
        f"{name} ({member_email}) joined {org_name} as {role}.",
        "",
        f"Thanks,",
        brand,
    ])
    _send(to=admin_emails, subject=f"{name} joined {org_name}", html=html, text=text, tag="member-joined")


def send_you_joined_email(
    *,
    email: str,
    display_name: Optional[str],
    org_name: str,
    role: str,
) -> None:
    """Confirm to the new member that they successfully joined."""
    if not email:
        return
    brand = _brand()
    name = display_name or "there"
    dashboard_url = _app_url() + "/host" if _app_url() else ""
    html = _layout(
        heading=f"You joined {org_name}",
        body_html=(
            f"<p>Hi {name},</p>"
            f"<p>You've been added to <strong>{org_name}</strong> as <strong>{role}</strong>.</p>"
            + (_btn("Open Dashboard", dashboard_url) if dashboard_url else "")
        ),
    )
    text = "\n".join([
        f"You joined {org_name}",
        "",
        f"Hi {name}, you've been added to {org_name} as {role}.",
        dashboard_url or "",
        "",
        f"Thanks,",
        brand,
    ])
    _send(to=[email], subject=f"You joined {org_name} on {brand}", html=html, text=text, tag="you-joined")


def send_subscription_started_email(
    *,
    admin_emails: List[str],
    org_name: str,
    plan_key: str,
) -> None:
    """Sent when a Stripe checkout session completes (paid plan activated)."""
    if not admin_emails:
        return
    brand = _brand()
    plan_label = plan_key.capitalize()
    dashboard_url = _app_url() + "/host" if _app_url() else ""
    html = _layout(
        heading=f"{plan_label} plan activated",
        body_html=(
            f"<p>Your <strong>{brand}</strong> subscription for <strong>{org_name}</strong> "
            f"is now active on the <strong>{plan_label}</strong> plan.</p>"
            f"<p>All features for your plan are immediately available.</p>"
            + (_btn("Go to Dashboard", dashboard_url) if dashboard_url else "")
        ),
    )
    text = "\n".join([
        f"{plan_label} plan activated — {org_name}",
        "",
        f"Your {brand} subscription for {org_name} is active on the {plan_label} plan.",
        dashboard_url or "",
        "",
        f"Thanks,",
        brand,
    ])
    _send(
        to=admin_emails,
        subject=f"{org_name} — {plan_label} plan activated",
        html=html,
        text=text,
        tag="subscription-started",
    )


def send_subscription_canceled_email(
    *,
    admin_emails: List[str],
    org_name: str,
) -> None:
    """Sent when a subscription is canceled in Stripe."""
    if not admin_emails:
        return
    brand = _brand()
    billing_url = _app_url() + "/host" if _app_url() else ""
    html = _layout(
        heading="Subscription canceled",
        body_html=(
            f"<p>The <strong>{brand}</strong> subscription for <strong>{org_name}</strong> "
            f"has been canceled.</p>"
            f"<p>Broadcasting will remain available until the end of the current billing period. "
            f"You can resubscribe at any time from the Billing tab.</p>"
            + (_btn("Manage Billing", billing_url) if billing_url else "")
        ),
    )
    text = "\n".join([
        f"Subscription canceled — {org_name}",
        "",
        f"The {brand} subscription for {org_name} has been canceled.",
        "You can resubscribe from the Billing tab.",
        billing_url or "",
        "",
        f"Thanks,",
        brand,
    ])
    _send(
        to=admin_emails,
        subject=f"{org_name} — subscription canceled",
        html=html,
        text=text,
        tag="subscription-canceled",
    )


def send_payment_failed_email(
    *,
    admin_emails: List[str],
    org_name: str,
    grace_days: int,
) -> None:
    """Sent on the first invoice.payment_failed for a subscription."""
    if not admin_emails:
        return
    brand = _brand()
    billing_url = _app_url() + "/host" if _app_url() else ""
    html = _layout(
        heading="Payment failed — action required",
        body_html=(
            f"<p>A payment for your <strong>{brand}</strong> subscription "
            f"(<strong>{org_name}</strong>) could not be processed.</p>"
            f"<p>You have a <strong>{grace_days}-day grace period</strong> to update your payment "
            f"method before broadcasting is paused. Stripe will automatically retry the charge.</p>"
            + (_btn("Update Payment Method", billing_url) if billing_url else "")
            + "<p>If you've already updated your card, no further action is needed.</p>"
        ),
    )
    text = "\n".join([
        f"Payment failed — {org_name}",
        "",
        f"A payment for your {brand} subscription could not be processed.",
        f"You have {grace_days} days to update your payment method.",
        billing_url or "",
        "",
        f"Thanks,",
        brand,
    ])
    _send(
        to=admin_emails,
        subject=f"{org_name} — payment failed, action required",
        html=html,
        text=text,
        tag="payment-failed",
    )


def send_spend_alert_email(
    *,
    to: str,
    provider: str,
    spend_usd: float,
    threshold_usd: float,
    period_key: str,
) -> None:
    """Alert the platform admin when monthly spend exceeds a threshold."""
    if not to:
        return
    brand = _brand()
    admin_url = _app_url() + "/admin/dashboard" if _app_url() else ""
    month_label = f"{period_key[:4]}-{period_key[4:]}" if len(period_key) == 6 else period_key
    html = _layout(
        heading=f"⚠️ {provider} spend alert",
        body_html=(
            f"<p>Your <strong>{provider}</strong> spend this month ({month_label}) has exceeded your alert threshold.</p>"
            f"<table style='border-collapse:collapse;margin:12px 0;'>"
            f"<tr><td style='padding:4px 16px 4px 0;color:#64748b;'>Current spend</td>"
            f"<td style='padding:4px 0;font-weight:700;'>${spend_usd:.4f}</td></tr>"
            f"<tr><td style='padding:4px 16px 4px 0;color:#64748b;'>Alert threshold</td>"
            f"<td style='padding:4px 0;'>${threshold_usd:.2f}</td></tr>"
            f"</table>"
            + (_btn("View Admin Dashboard", admin_url) if admin_url else "")
            + "<p style='color:#64748b;font-size:13px;'>You can update alert thresholds in the Pricing Rates section of the admin dashboard.</p>"
        ),
    )
    text = "\n".join([
        f"⚠️ {provider} spend alert — {month_label}",
        "",
        f"Current spend: ${spend_usd:.4f}",
        f"Alert threshold: ${threshold_usd:.2f}",
        "",
        admin_url or "",
        "",
        f"Thanks,",
        brand,
    ])
    _send(to=[to], subject=f"⚠️ {provider} spend alert — ${spend_usd:.2f} this month", html=html, text=text, tag="spend-alert")


def send_soft_cap_reached_email(
    *,
    admin_emails: List[str],
    org_name: str,
    minutes_used: int,
    minutes_limit: int,
    plan_key: str,
) -> None:
    """Sent once per billing period when a paid plan hits its monthly broadcast limit.
    Broadcasts continue uninterrupted — this is a notification, not a block.
    """
    if not admin_emails:
        return
    brand = _brand()
    plan_label = plan_key.capitalize()
    billing_url = _app_url() + "/host" if _app_url() else ""
    html = _layout(
        heading=f"Monthly broadcast limit reached — {org_name}",
        body_html=(
            f"<p>Your <strong>{org_name}</strong> workspace has used all "
            f"<strong>{minutes_limit:,} minutes</strong> included in your {plan_label} plan this month.</p>"
            f"<p><strong>Your broadcasts are continuing uninterrupted.</strong> "
            f"You will still be able to run services for the rest of this billing period.</p>"
            f"<p>To increase your monthly broadcast time, consider upgrading your plan.</p>"
            + (_btn("Upgrade Plan", billing_url) if billing_url else "")
            + f"<p style='color:#64748b;font-size:13px;'>Minutes used this month: {minutes_used:,} / {minutes_limit:,}</p>"
        ),
    )
    text = "\n".join([
        f"Monthly broadcast limit reached — {org_name}",
        "",
        f"You've used all {minutes_limit:,} minutes in your {plan_label} plan this month.",
        "Your broadcasts are continuing uninterrupted.",
        "Consider upgrading your plan to increase your monthly broadcast time.",
        billing_url or "",
        "",
        f"Minutes used: {minutes_used:,} / {minutes_limit:,}",
        "",
        "Thanks,",
        brand,
    ])
    _send(
        to=admin_emails,
        subject=f"{org_name} — monthly broadcast limit reached",
        html=html,
        text=text,
        tag="soft-cap-reached",
    )


def send_payment_recovered_email(
    *,
    admin_emails: List[str],
    org_name: str,
) -> None:
    """Sent when a previously failed payment is successfully collected."""
    if not admin_emails:
        return
    brand = _brand()
    html = _layout(
        heading="Payment successful — subscription restored",
        body_html=(
            f"<p>Great news! The outstanding payment for <strong>{org_name}</strong> "
            f"on <strong>{brand}</strong> was successfully collected.</p>"
            f"<p>Your subscription is fully active again.</p>"
        ),
    )
    text = "\n".join([
        f"Payment recovered — {org_name}",
        "",
        f"The outstanding payment for {org_name} was collected. Subscription fully restored.",
        "",
        f"Thanks,",
        brand,
    ])
    _send(
        to=admin_emails,
        subject=f"{org_name} — payment successful, subscription restored",
        html=html,
        text=text,
        tag="payment-recovered",
    )
