from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PlanSpec:
    key: str
    max_service_keys: int  # 0 means unlimited
    amount_usd: int
    monthly_minutes: int  # 0 means unlimited; applies to paid plans only


TRIAL_PLAN_KEY = "trial"
STARTER_PLAN_KEY = "starter"
GROWTH_PLAN_KEY = "growth"
PREMIUM_PLAN_KEY = "premium"

PLAN_SPECS: Dict[str, PlanSpec] = {
    TRIAL_PLAN_KEY: PlanSpec(key=TRIAL_PLAN_KEY, max_service_keys=2, amount_usd=0, monthly_minutes=20),
    STARTER_PLAN_KEY: PlanSpec(key=STARTER_PLAN_KEY, max_service_keys=5, amount_usd=20, monthly_minutes=600),
    GROWTH_PLAN_KEY: PlanSpec(key=GROWTH_PLAN_KEY, max_service_keys=12, amount_usd=40, monthly_minutes=1800),
    PREMIUM_PLAN_KEY: PlanSpec(key=PREMIUM_PLAN_KEY, max_service_keys=0, amount_usd=60, monthly_minutes=0),
}


BILLING_STATUSES = {
    "trialing",
    "active",
    "past_due",
    "canceled",
    "incomplete",
    "unpaid",
}


def plan_spec(plan_key: str) -> PlanSpec:
    token = str(plan_key or "").strip().lower()
    return PLAN_SPECS.get(token, PLAN_SPECS[TRIAL_PLAN_KEY])


def default_billing_state(
    *,
    now: Optional[datetime] = None,
    plan_key: str = TRIAL_PLAN_KEY,
    trial_days: int = 30,
    trial_minutes: int = 20,
) -> Dict[str, Any]:
    dt = now or _utcnow()
    resolved_plan = plan_spec(plan_key)
    trial_ends = dt + timedelta(days=max(1, int(trial_days)))
    trial_minutes_limit = max(0, int(trial_minutes)) if resolved_plan.key == TRIAL_PLAN_KEY else 0
    return {
        "version": 1,
        "provider": "stripe",
        "planKey": resolved_plan.key,
        "status": "trialing" if resolved_plan.key == TRIAL_PLAN_KEY else "active",
        "trialEndsAt": trial_ends if resolved_plan.key == TRIAL_PLAN_KEY else None,
        "currentPeriodStart": None,
        "currentPeriodEnd": None,
        "graceEndsAt": None,
        "cancelAtPeriodEnd": False,
        "stripeCustomerId": None,
        "stripeSubscriptionId": None,
        "priceId": None,
        "trialMinutesLimit": trial_minutes_limit,
        "trialMinutesUsed": 0,
        "trialSecondsUsed": 0,
        "limits": {"maxServiceKeys": resolved_plan.max_service_keys},
        "entitlements": {"canStartService": True},
        "updatedAt": dt,
    }


def normalize_billing_state(
    raw: Optional[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    fallback_plan_key: str = TRIAL_PLAN_KEY,
) -> Dict[str, Any]:
    base = default_billing_state(now=now, plan_key=fallback_plan_key)
    if not isinstance(raw, dict):
        return base
    merged = dict(base)
    merged.update({k: v for k, v in raw.items() if k != "limits" and k != "entitlements"})
    limits = dict(base.get("limits") or {})
    limits.update(dict(raw.get("limits") or {}))
    entitlements = dict(base.get("entitlements") or {})
    entitlements.update(dict(raw.get("entitlements") or {}))
    merged["limits"] = limits
    merged["entitlements"] = entitlements

    resolved_status = str(merged.get("status") or "").strip().lower()
    if resolved_status not in BILLING_STATUSES:
        merged["status"] = base["status"]

    resolved_plan = plan_spec(str(merged.get("planKey") or fallback_plan_key))
    merged["planKey"] = resolved_plan.key
    if not isinstance(merged.get("limits"), dict):
        merged["limits"] = {}
    if "maxServiceKeys" not in merged["limits"] or merged["limits"].get("maxServiceKeys") is None:
        merged["limits"]["maxServiceKeys"] = resolved_plan.max_service_keys

    trial_limit_raw = merged.get("trialMinutesLimit")
    trial_used_raw = merged.get("trialMinutesUsed")
    try:
        trial_limit = int(trial_limit_raw)
    except (TypeError, ValueError):
        trial_limit = int(base.get("trialMinutesLimit") or 0)
    try:
        trial_used = int(trial_used_raw)
    except (TypeError, ValueError):
        trial_used = int(base.get("trialMinutesUsed") or 0)
    trial_seconds_raw = merged.get("trialSecondsUsed")
    try:
        trial_seconds_used = int(trial_seconds_raw)
    except (TypeError, ValueError):
        trial_seconds_used = trial_used * 60
    trial_limit = max(0, trial_limit)
    trial_used = max(0, trial_used)
    trial_seconds_used = max(max(0, trial_seconds_used), trial_used * 60)
    if trial_limit > 0:
        trial_seconds_limit = trial_limit * 60
        trial_seconds_used = min(trial_seconds_limit, trial_seconds_used)
        trial_used = min(trial_limit, trial_seconds_used // 60)
    else:
        trial_seconds_used = 0
        trial_used = 0
    merged["trialMinutesLimit"] = trial_limit
    merged["trialMinutesUsed"] = trial_used
    merged["trialSecondsUsed"] = trial_seconds_used
    return merged
