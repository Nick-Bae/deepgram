from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Dict

from app.billing.models import GROWTH_PLAN_KEY, PREMIUM_PLAN_KEY, STARTER_PLAN_KEY


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return max(min_value, min(max_value, parsed))


@dataclass(frozen=True)
class BillingConfig:
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_price_ids: Dict[str, str]
    trial_days: int
    trial_minutes: int
    grace_days: int
    entitlements_v2_enabled: bool


def load_billing_config() -> BillingConfig:
    stripe_secret_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    stripe_webhook_secret = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
    trial_days = _env_int("BILLING_TRIAL_DAYS", 30, min_value=1, max_value=90)
    trial_minutes = _env_int("BILLING_TRIAL_MINUTES", 20, min_value=1, max_value=10_000)
    grace_days = _env_int("BILLING_GRACE_DAYS", 3, min_value=0, max_value=30)
    entitlements_v2 = (os.getenv("BILLING_ENTITLEMENTS_V2") or "").strip().lower() in {"1", "true", "yes", "on"}
    price_ids = {
        STARTER_PLAN_KEY: (os.getenv("STRIPE_PRICE_STARTER") or "").strip(),
        GROWTH_PLAN_KEY: (os.getenv("STRIPE_PRICE_GROWTH") or "").strip(),
        PREMIUM_PLAN_KEY: (os.getenv("STRIPE_PRICE_PREMIUM") or "").strip(),
    }
    return BillingConfig(
        stripe_secret_key=stripe_secret_key,
        stripe_webhook_secret=stripe_webhook_secret,
        stripe_price_ids=price_ids,
        trial_days=trial_days,
        trial_minutes=trial_minutes,
        grace_days=grace_days,
        entitlements_v2_enabled=entitlements_v2,
    )


BILLING_CONFIG = load_billing_config()
