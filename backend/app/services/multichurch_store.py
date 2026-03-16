from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
import hashlib
import math
import os
import re
import secrets
import time
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from app.billing.config import BILLING_CONFIG
from app.billing.models import (
    default_billing_state as _default_billing_state_model,
    normalize_billing_state as _normalize_billing_state_model,
)

try:
    from app.auth.firebase_auth import is_super_uid as _claim_is_super_uid
except Exception:  # pragma: no cover - auth module may be unavailable in some test/bootstrap paths
    def _claim_is_super_uid(uid: Optional[str]) -> bool:
        return False

try:
    from google.cloud import firestore as gcf_firestore  # type: ignore
except Exception:  # pragma: no cover - optional dependency in dev
    gcf_firestore = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _yyyymm(dt: Optional[datetime] = None) -> str:
    now = dt or _utcnow()
    return f"{now.year:04d}{now.month:02d}"


def _new_room_id() -> str:
    return f"room_{uuid4().hex[:12]}"


def _clean_token(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    txt = str(raw).strip()
    return txt or None


def _tick_minutes(tick_seconds: int) -> int:
    return max(1, int(math.ceil(max(1, int(tick_seconds)) / 60)))


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return max(min_value, min(max_value, parsed))


def _env_float(name: str, default: float, *, min_value: float, max_value: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        return default
    if not math.isfinite(parsed):
        return default
    return max(min_value, min(max_value, parsed))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _env_uid_set(name: str) -> set[str]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part and part.strip()}


def _env_email_set(name: str) -> set[str]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return set()
    out: set[str] = set()
    for part in raw.split(","):
        token = str(part or "").strip().lower()
        if token:
            out.add(token)
    return out


DEFAULT_SERVICE_SEEDS: tuple[tuple[str, str], ...] = (
    ("sun-11am", "Sunday 11 AM"),
    ("sun-2pm", "Sunday 2 PM"),
    ("wed-7pm", "Wednesday 7 PM"),
)

ALLOWED_MEMBER_ROLES: set[str] = {"owner", "admin", "host", "viewer"}
PROMPT_EDITOR_ROLES: set[str] = {"owner", "admin", "host"}
ALLOWED_INVITE_ROLES: set[str] = {"admin", "host", "viewer"}
INVITE_STATUS_ACTIVE = "active"
INVITE_STATUS_CONSUMED = "consumed"
INVITE_STATUS_EXPIRED = "expired"
INVITE_STATUS_REVOKED = "revoked"
ALLOWED_INVITE_STATUSES: set[str] = {
    INVITE_STATUS_ACTIVE,
    INVITE_STATUS_CONSUMED,
    INVITE_STATUS_EXPIRED,
    INVITE_STATUS_REVOKED,
}
DEFAULT_INVITE_EXPIRY_HOURS = _env_int("INVITE_DEFAULT_EXPIRY_HOURS", 24 * 3, min_value=1, max_value=24 * 30)
MAX_ACTIVE_INVITES_PER_ORG = _env_int("INVITE_MAX_ACTIVE_PER_ORG", 20, min_value=1, max_value=500)
INVITE_RETENTION_DAYS = _env_int("INVITE_RETENTION_DAYS", 30, min_value=1, max_value=365)
BILLING_LIMITS_ENABLED = not _env_bool("DISABLE_BILLING_LIMITS", False)
SERMON_PREP_BUDGETS_ENABLED = not _env_bool("DISABLE_SERMON_PREP_BUDGETS", False)
SERMON_PREP_DEFAULT_BUDGET_USD = _env_float(
    "SERMON_PREP_DEFAULT_BUDGET_USD",
    0.0,
    min_value=0.0,
    max_value=1_000_000.0,
)
SERMON_PREP_INPUT_COST_PER_MILLION = _env_float(
    "SERMON_PREP_INPUT_COST_PER_MILLION",
    0.15,
    min_value=0.0,
    max_value=10_000.0,
)
SERMON_PREP_OUTPUT_COST_PER_MILLION = _env_float(
    "SERMON_PREP_OUTPUT_COST_PER_MILLION",
    0.60,
    min_value=0.0,
    max_value=10_000.0,
)
MASTER_USER_UIDS = set().union(
    _env_uid_set("MASTER_USER_UIDS"),
    _env_uid_set("SUPER_ADMIN_UIDS"),
    _env_uid_set("MASTER_UIDS"),
)
_single_master_uid = _clean_token(os.getenv("MASTER_USER_UID"))
if _single_master_uid:
    MASTER_USER_UIDS.add(_single_master_uid)

BILLING_ADMIN_UIDS = _env_uid_set("BILLING_ADMIN_UIDS")
_single_billing_admin_uid = _clean_token(os.getenv("BILLING_ADMIN_UID"))
if _single_billing_admin_uid:
    BILLING_ADMIN_UIDS.add(_single_billing_admin_uid)

BILLING_ADMIN_EMAILS = _env_email_set("BILLING_ADMIN_EMAILS")
_single_billing_admin_email = _clean_token(os.getenv("BILLING_ADMIN_EMAIL"))
if _single_billing_admin_email:
    BILLING_ADMIN_EMAILS.add(_single_billing_admin_email.lower())


def _is_master_uid(uid: Optional[str]) -> bool:
    clean_uid = _clean_token(uid)
    if not clean_uid:
        return False
    return clean_uid in MASTER_USER_UIDS or bool(_claim_is_super_uid(clean_uid))


def _is_billing_admin(uid: Optional[str], *, email: Optional[str] = None) -> bool:
    clean_uid = _clean_token(uid)
    clean_email = _clean_token(email)
    clean_email = clean_email.lower() if clean_email else None
    if BILLING_ADMIN_UIDS or BILLING_ADMIN_EMAILS:
        if clean_uid and clean_uid in BILLING_ADMIN_UIDS:
            return True
        if clean_email and clean_email in BILLING_ADMIN_EMAILS:
            return True
        return False
    return _is_master_uid(clean_uid)


def _default_billing_state(*, now: Optional[datetime], plan_key: str) -> Dict[str, Any]:
    return _default_billing_state_model(
        now=now,
        plan_key=plan_key,
        trial_days=int(BILLING_CONFIG.trial_days),
        trial_minutes=int(BILLING_CONFIG.trial_minutes),
    )


def _normalize_billing_state(raw: Optional[Dict[str, Any]], *, plan_key: str, now: Optional[datetime]) -> Dict[str, Any]:
    return _normalize_billing_state_model(
        raw,
        now=now,
        fallback_plan_key=plan_key,
    )


def _org_billing_limits_flag(org: Dict[str, Any]) -> bool:
    if "billingLimitsEnabled" not in org:
        return True
    return bool(org.get("billingLimitsEnabled"))


def _org_billing_limits_enabled(org: Dict[str, Any]) -> bool:
    return BILLING_LIMITS_ENABLED and _org_billing_limits_flag(org)


def _billing_limits_payload(*, org_id: str, org: Dict[str, Any]) -> Dict[str, Any]:
    enabled = _org_billing_limits_flag(org)
    return {
        "orgId": org_id,
        "billingLimitsEnabled": enabled,
        "globalBillingLimitsEnabled": bool(BILLING_LIMITS_ENABLED),
        "effectiveBillingLimitsEnabled": bool(BILLING_LIMITS_ENABLED and enabled),
        "hardCapReached": bool(org.get("hardCapReached")),
        "maxMinutesPerMonth": int(org.get("maxMinutesPerMonth") or 0),
        "currentMonthMinutes": int(org.get("currentMonthMinutes") or 0),
        "currentMonthKey": str(org.get("currentMonthKey") or ""),
        "updatedAt": org.get("updatedAt"),
    }


_HARD_INACTIVE_ORG_STATUSES: set[str] = {"inactive", "disabled", "suspended", "deleted"}


def _status_token(raw: Any) -> str:
    return str(raw or "").strip().lower()


def _safe_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _entitlements_v2_effective(org: Dict[str, Any]) -> bool:
    return bool(BILLING_CONFIG.entitlements_v2_enabled and _org_billing_limits_enabled(org))


def _legacy_org_start_allowed(org: Dict[str, Any]) -> bool:
    return _status_token(org.get("status")) in {"active", "trial"}


def _normalize_org_billing(org: Dict[str, Any], *, now: datetime) -> Dict[str, Any]:
    return _normalize_billing_state(
        org.get("billing"),
        plan_key=str(org.get("plan") or "trial"),
        now=now,
    )


def _billing_max_service_keys(billing: Dict[str, Any]) -> int:
    limits = billing.get("limits")
    if not isinstance(limits, dict):
        return 0
    parsed = _safe_int(limits.get("maxServiceKeys"), 0)
    return max(0, parsed)


def _is_trial_plan(billing: Dict[str, Any]) -> bool:
    return _status_token(billing.get("planKey")) == "trial"


def _billing_trial_minutes_limit(billing: Dict[str, Any]) -> int:
    if not _is_trial_plan(billing):
        return 0
    parsed = _safe_int(billing.get("trialMinutesLimit"), 0)
    return max(0, parsed)


def _billing_trial_seconds_limit(billing: Dict[str, Any]) -> int:
    minutes_limit = _billing_trial_minutes_limit(billing)
    if minutes_limit <= 0:
        return 0
    return minutes_limit * 60


def _billing_trial_seconds_used(billing: Dict[str, Any]) -> int:
    seconds_limit = _billing_trial_seconds_limit(billing)
    if seconds_limit <= 0:
        return 0
    raw_seconds = billing.get("trialSecondsUsed")
    try:
        seconds_used = int(raw_seconds)
    except (TypeError, ValueError):
        seconds_used = _safe_int(billing.get("trialMinutesUsed"), 0) * 60
    seconds_used = max(0, seconds_used)
    return min(seconds_limit, seconds_used)


def _set_billing_trial_seconds_used(billing: Dict[str, Any], seconds_used: int) -> int:
    seconds_limit = _billing_trial_seconds_limit(billing)
    if seconds_limit <= 0:
        billing["trialMinutesUsed"] = 0
        billing["trialSecondsUsed"] = 0
        return 0
    clamped_seconds = max(0, min(seconds_limit, int(seconds_used)))
    billing["trialMinutesLimit"] = _billing_trial_minutes_limit(billing)
    billing["trialMinutesUsed"] = min(_billing_trial_minutes_limit(billing), clamped_seconds // 60)
    billing["trialSecondsUsed"] = clamped_seconds
    return clamped_seconds


def _consume_billing_trial_seconds(billing: Dict[str, Any], delta_seconds: int) -> int:
    seconds_limit = _billing_trial_seconds_limit(billing)
    if seconds_limit <= 0:
        return 0
    delta = max(0, int(delta_seconds))
    if delta <= 0:
        return _billing_trial_seconds_used(billing)
    return _set_billing_trial_seconds_used(billing, _billing_trial_seconds_used(billing) + delta)


def _billing_trial_minutes_used(billing: Dict[str, Any]) -> int:
    return _billing_trial_seconds_used(billing) // 60


def _billing_trial_seconds_remaining(billing: Dict[str, Any]) -> Optional[int]:
    seconds_limit = _billing_trial_seconds_limit(billing)
    if seconds_limit <= 0:
        return None
    used = _billing_trial_seconds_used(billing)
    return max(0, seconds_limit - used)


def _billing_trial_minutes_remaining(billing: Dict[str, Any]) -> Optional[int]:
    remaining_seconds = _billing_trial_seconds_remaining(billing)
    if remaining_seconds is None:
        return None
    if remaining_seconds <= 0:
        return 0
    return int(math.ceil(remaining_seconds / 60))


def _billing_trial_minutes_exhausted(billing: Dict[str, Any]) -> bool:
    remaining_seconds = _billing_trial_seconds_remaining(billing)
    return remaining_seconds is not None and remaining_seconds <= 0


def _ceil_minutes_from_seconds(total_seconds: int) -> int:
    seconds = max(0, int(total_seconds))
    if seconds <= 0:
        return 0
    return int(math.ceil(seconds / 60))


def _room_unbilled_elapsed_seconds(room: Dict[str, Any], *, now: datetime) -> int:
    last_tick_at = room.get("lastUsageTickAt") or room.get("startedAt") or now
    if not isinstance(last_tick_at, datetime):
        last_tick_at = now
    elapsed_seconds = (now - last_tick_at).total_seconds()
    if elapsed_seconds <= 0:
        return 0
    return int(elapsed_seconds)


def _billing_payload_from_org_row(org: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
    raw_billing = org.get("billing")
    billing_payload = dict(raw_billing) if isinstance(raw_billing, dict) else {}
    changed = not isinstance(raw_billing, dict)
    for key, value in org.items():
        if not isinstance(key, str) or not key.startswith("billing."):
            continue
        changed = True
        path = [part for part in key.split(".")[1:] if part]
        if not path:
            continue
        target = billing_payload
        for part in path[:-1]:
            next_value = target.get(part)
            if not isinstance(next_value, dict):
                if next_value is None:
                    next_value = {}
                    target[part] = next_value
                    changed = True
                else:
                    next_value = {}
                    target[part] = next_value
                    changed = True
            target = next_value
        if path[-1] not in target or target.get(path[-1]) is None:
            target[path[-1]] = value
            changed = True
    return billing_payload, changed


def _effective_trial_seconds_remaining_for_rooms(
    billing: Dict[str, Any],
    rooms: List[Dict[str, Any]],
    *,
    now: datetime,
) -> Optional[int]:
    remaining_seconds = _billing_trial_seconds_remaining(billing)
    if remaining_seconds is None:
        return None
    live_runtime_seconds = 0
    for room in rooms:
        if str(room.get("status") or "").strip().lower() != "live":
            continue
        live_runtime_seconds += _room_unbilled_elapsed_seconds(room, now=now)
    return max(0, remaining_seconds - live_runtime_seconds)


def _billing_start_denial_reason(*, billing: Dict[str, Any], now: datetime) -> Optional[str]:
    status = _status_token(billing.get("status"))
    trial_ends_at = billing.get("trialEndsAt")
    grace_ends_at = billing.get("graceEndsAt")

    if _billing_trial_minutes_exhausted(billing):
        return "trial_expired"

    if status in {"trialing", "active"}:
        if status == "trialing" and isinstance(trial_ends_at, datetime) and now > trial_ends_at:
            return "trial_expired"
        return None
    if status == "past_due":
        if isinstance(grace_ends_at, datetime) and now <= grace_ends_at:
            return None
        return "grace_expired"
    if status in {"canceled", "unpaid", "incomplete"}:
        return "subscription_required"
    if isinstance(trial_ends_at, datetime) and now > trial_ends_at:
        return "trial_expired"
    return "subscription_required"


def _normalize_period_key(raw: Optional[str], *, now: Optional[datetime] = None) -> str:
    token = str(raw or "").strip()
    if re.fullmatch(r"\d{6}", token):
        return token
    return _yyyymm(now)


def _safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(parsed):
        return fallback
    return parsed


def _round_usd(value: Any) -> float:
    return round(max(0.0, _safe_float(value, 0.0)), 6)


def _int_tokens(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _sermon_budget_usd(org: Dict[str, Any]) -> float:
    raw = org.get("sermonPrepBudgetUsd")
    if raw is None:
        return float(SERMON_PREP_DEFAULT_BUDGET_USD)
    return max(0.0, _safe_float(raw, SERMON_PREP_DEFAULT_BUDGET_USD))


def _sermon_budget_effective_enabled(org: Dict[str, Any]) -> bool:
    return bool(SERMON_PREP_BUDGETS_ENABLED and _sermon_budget_usd(org) > 0.0)


def _sermon_usage_doc_id(sermon_id: str) -> str:
    clean = (sermon_id or "").strip()
    if not clean:
        return ""
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:32]


def _estimate_sermon_prep_usd(*, prompt_tokens: int, completion_tokens: int) -> float:
    total = (
        (max(0, int(prompt_tokens)) * float(SERMON_PREP_INPUT_COST_PER_MILLION))
        + (max(0, int(completion_tokens)) * float(SERMON_PREP_OUTPUT_COST_PER_MILLION))
    ) / 1_000_000.0
    return max(0.0, total)


def _sermon_usage_payload(
    *,
    org_id: str,
    org: Dict[str, Any],
    period_key: str,
    usage_month: Dict[str, Any],
    sermon_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    prompt_tokens = _int_tokens(usage_month.get("sermonPromptTokens"))
    completion_tokens = _int_tokens(usage_month.get("sermonCompletionTokens"))
    total_tokens = _int_tokens(usage_month.get("sermonTotalTokens")) or (prompt_tokens + completion_tokens)
    requests_count = _int_tokens(usage_month.get("sermonRequestsCount"))
    estimated_usd = _round_usd(usage_month.get("sermonEstimatedUsd"))
    budget_usd = _round_usd(_sermon_budget_usd(org))
    effective_enabled = bool(SERMON_PREP_BUDGETS_ENABLED and budget_usd > 0.0)
    cap_reached = bool(effective_enabled and estimated_usd >= budget_usd)
    remaining = _round_usd(max(0.0, budget_usd - estimated_usd)) if effective_enabled else 0.0
    rows_sorted = sorted(
        [
            {
                "sermonId": str(row.get("sermonId") or "").strip(),
                "promptTokens": _int_tokens(row.get("promptTokens")),
                "completionTokens": _int_tokens(row.get("completionTokens")),
                "totalTokens": _int_tokens(row.get("totalTokens")),
                "estimatedUsd": _round_usd(row.get("estimatedUsd")),
                "requestsCount": _int_tokens(row.get("requestsCount")),
                "lastModel": str(row.get("lastModel") or ""),
                "lastUsedAt": row.get("lastUsedAt"),
            }
            for row in sermon_rows
            if str(row.get("sermonId") or "").strip()
        ],
        key=lambda row: (float(row.get("estimatedUsd") or 0.0), int(row.get("totalTokens") or 0)),
        reverse=True,
    )
    return {
        "orgId": org_id,
        "currentMonthKey": period_key,
        "budgetUsd": budget_usd,
        "globalBudgetsEnabled": bool(SERMON_PREP_BUDGETS_ENABLED),
        "effectiveBudgetEnabled": effective_enabled,
        "capReached": cap_reached,
        "remainingBudgetUsd": remaining,
        "currentMonthEstimatedUsd": estimated_usd,
        "currentMonthPromptTokens": prompt_tokens,
        "currentMonthCompletionTokens": completion_tokens,
        "currentMonthTotalTokens": total_tokens,
        "requestsCount": requests_count,
        "inputCostPerMillionTokens": float(SERMON_PREP_INPUT_COST_PER_MILLION),
        "outputCostPerMillionTokens": float(SERMON_PREP_OUTPUT_COST_PER_MILLION),
        "sermons": rows_sorted,
    }


def _normalize_slug(raw: str) -> str:
    token = (raw or "").strip().lower()
    token = re.sub(r"[^a-z0-9]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    if not token:
        raise ValueError("invalid_slug")
    return token


def _normalize_service_key(raw: str) -> str:
    token = (raw or "").strip().lower()
    token = re.sub(r"[^a-z0-9]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    if not token:
        raise ValueError("invalid_service_key")
    return token


def _default_service_title(service_key: str) -> str:
    parts = [part for part in (service_key or "").split("-") if part]
    if not parts:
        return service_key
    return " ".join(part.capitalize() for part in parts)


def _normalize_role(raw: Optional[str], *, fallback: str = "viewer") -> str:
    token = (raw or "").strip().lower()
    if token in ALLOWED_MEMBER_ROLES:
        return token
    return fallback


def _normalize_invite_role(raw: Optional[str]) -> str:
    token = (raw or "").strip().lower()
    if token not in ALLOWED_INVITE_ROLES:
        raise ValueError("invalid_role")
    return token


def _normalize_invite_status(raw: Optional[str]) -> Optional[str]:
    token = (raw or "").strip().lower()
    if not token:
        return None
    if token not in ALLOWED_INVITE_STATUSES:
        raise ValueError("invalid_status")
    return token


def _invite_code_hash(code: str) -> str:
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()


def _new_invite_code() -> str:
    return secrets.token_urlsafe(24)


def _invite_expiry(hours: int) -> datetime:
    safe_hours = max(1, min(24 * 30, int(hours)))
    return _utcnow() + timedelta(hours=safe_hours)


def _serialize_invite(invite: Dict[str, Any], *, fallback_org: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    org_id = _clean_token(invite.get("orgId")) or ""
    slug = _clean_token(invite.get("slug")) or _clean_token((fallback_org or {}).get("slug")) or org_id
    name = _clean_token(invite.get("name")) or _clean_token((fallback_org or {}).get("name")) or org_id
    return {
        "inviteId": str(invite.get("inviteId") or invite.get("codeHash") or ""),
        "orgId": org_id,
        "slug": slug,
        "name": name,
        "role": _normalize_role(invite.get("role"), fallback="viewer"),
        "status": str(invite.get("status") or ""),
        "expiresAt": invite.get("expiresAt"),
        "createdBy": _clean_token(invite.get("createdBy")),
        "createdAt": invite.get("createdAt"),
        "consumedBy": _clean_token(invite.get("consumedBy")),
        "consumedAt": invite.get("consumedAt"),
        "revokedBy": _clean_token(invite.get("revokedBy")),
        "revokedAt": invite.get("revokedAt"),
    }


@dataclass
class RoomRef:
    org_id: str
    room_id: str


class InMemoryMultiChurchStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._global_host_token = (os.getenv("HOST_API_TOKEN") or "").strip()
        self._orgs: Dict[str, Dict[str, Any]] = {}
        self._slug_to_org: Dict[str, str] = {}
        self._services: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._rooms: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._usage: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._usage_sermons: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self._members: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._users: Dict[str, Dict[str, Any]] = {}
        self._org_invites: Dict[str, Dict[str, Any]] = {}
        self._invite_audits: List[Dict[str, Any]] = []
        self._billing_events: Dict[str, Dict[str, Any]] = {}
        self._seed_dev_data()

    def _seed_dev_data(self) -> None:
        # Keep local/dev usable without manual bootstrap.
        self._seed_dev_org(
            org_id="demo-org",
            slug="demo",
            name="Demo Church",
            host_token="demo-host-token",
            host_uid="demo-host",
        )
        self._seed_dev_org(
            org_id="arkchurch",
            slug="arkchurch",
            name="Ark Church",
            host_token="arkchurch-host-token",
            host_uid="ark-host",
        )

    def _seed_dev_org(self, *, org_id: str, slug: str, name: str, host_token: str, host_uid: str) -> None:
        now = _utcnow()
        self._orgs[org_id] = {
            "slug": slug,
            "name": name,
            "plan": "starter",
            "status": "active",
            "billing": _default_billing_state(now=now, plan_key="starter"),
            "maxMinutesPerMonth": 500,
            "currentMonthMinutes": 0,
            "currentMonthKey": _yyyymm(),
            "maxConcurrentRooms": 1,
            "billingLimitsEnabled": True,
            "hardCapReached": False,
            "sermonPrepBudgetUsd": float(SERMON_PREP_DEFAULT_BUDGET_USD),
            "hostToken": host_token,
            "customPrompt": "",
            "servicePrompt": "",
            "createdAt": now,
            "updatedAt": now,
        }
        self._slug_to_org[slug] = org_id
        self._members[(org_id, host_uid)] = {"role": "host", "createdAt": now, "updatedAt": now}
        self._users[host_uid] = {"currentOrgId": org_id, "updatedAt": now}
        for service_key, title in DEFAULT_SERVICE_SEEDS:
            self._services[(org_id, service_key)] = {
                "title": title,
                "timezone": "America/Chicago",
                "rrule": None,
                "defaultLanguagePair": {"source": "ko", "target": "en"},
                "activeRoomId": None,
                "lastRoomId": None,
                "updatedAt": now,
            }

    def _member_role(self, org_id: str, uid: Optional[str]) -> Optional[str]:
        clean_uid = _clean_token(uid)
        if not clean_uid:
            return None
        if _is_master_uid(clean_uid):
            return "owner"
        member = self._members.get((org_id, clean_uid))
        if not member:
            return None
        return _normalize_role(member.get("role"), fallback="viewer")

    def _serialize_room_status(self, room_doc: Optional[Dict[str, Any]]) -> str:
        if not room_doc:
            return "waiting"
        return str(room_doc.get("status") or "waiting")

    def _org_host_token(self, org: Dict[str, Any]) -> Optional[str]:
        token = _clean_token(org.get("hostToken"))
        if token:
            return token
        global_token = _clean_token(self._global_host_token)
        return global_token

    def _roll_billing_period_if_needed(self, org: Dict[str, Any], now: datetime) -> None:
        current_key = _yyyymm(now)
        if str(org.get("currentMonthKey") or "") == current_key:
            return
        org["currentMonthKey"] = current_key
        org["currentMonthMinutes"] = 0
        org["hardCapReached"] = False

    def authorize_host(self, org_id: str, *, host_uid: Optional[str] = None, host_token: Optional[str] = None) -> bool:
        with self._lock:
            org = self._orgs.get(org_id)
            if not org:
                return False
            uid = _clean_token(host_uid)
            if uid:
                if _is_master_uid(uid):
                    return True
                member = self._members.get((org_id, uid)) or {}
                role = str(member.get("role") or "").strip().lower()
                if role in {"owner", "admin", "host"}:
                    return True

            org_token = _clean_token(org.get("hostToken"))
            global_token = _clean_token(self._global_host_token)
            if org_token or global_token:
                provided_token = _clean_token(host_token)
                if not provided_token:
                    return False
                if org_token and compare_digest(org_token, provided_token):
                    return True
                if global_token and compare_digest(global_token, provided_token):
                    return True
                return False
            # Backward compatible local/dev mode when no token exists.
            return True

    def resolve_service(self, slug: str, service_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            org_id = self._slug_to_org.get((slug or "").strip().lower())
            if not org_id:
                return None
            service = self._services.get((org_id, service_key))
            if not service:
                return None
            active_room_id = service.get("activeRoomId")
            room_doc = self._rooms.get((org_id, active_room_id)) if active_room_id else None
            if room_doc and room_doc.get("status") != "live":
                active_room_id = None
            return {
                "orgId": org_id,
                "slug": self._orgs[org_id]["slug"],
                "serviceKey": service_key,
                "activeRoomId": active_room_id,
                "roomStatus": self._serialize_room_status(room_doc),
                "languagePair": (room_doc or {}).get("languagePair")
                or service.get("defaultLanguagePair")
                or {"source": "ko", "target": "en"},
                "service": {
                    "title": service.get("title") or service_key,
                    "timezone": service.get("timezone") or "UTC",
                    "rrule": service.get("rrule"),
                },
            }

    def list_services(self, slug: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            org_id = self._slug_to_org.get((slug or "").strip().lower())
            if not org_id:
                return None
            rows: List[Dict[str, Any]] = []
            for (row_org_id, service_key), service in sorted(self._services.items()):
                if row_org_id != org_id:
                    continue
                active_room_id = service.get("activeRoomId")
                room_doc = self._rooms.get((org_id, active_room_id)) if active_room_id else None
                rows.append(
                    {
                        "serviceKey": service_key,
                        "title": service.get("title") or service_key,
                        "timezone": service.get("timezone") or "UTC",
                        "activeRoomId": active_room_id if room_doc and room_doc.get("status") == "live" else None,
                        "roomStatus": self._serialize_room_status(room_doc),
                        "defaultLanguagePair": service.get("defaultLanguagePair") or {"source": "ko", "target": "en"},
                    }
                )
            return {
                "orgId": org_id,
                "slug": self._orgs[org_id]["slug"],
                "name": self._orgs[org_id]["name"],
                "services": rows,
            }

    def update_org_profile(
        self,
        *,
        org_id: str,
        requested_by_uid: str,
        name: str,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        clean_name = _clean_token(name)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        if len(clean_name) < 2:
            raise ValueError("invalid_name")

        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            role = self._member_role(clean_org_id, clean_uid)
            if role not in {"owner", "admin"}:
                raise PermissionError("forbidden")
            org["name"] = clean_name
            org["updatedAt"] = _utcnow()
            return {
                "orgId": clean_org_id,
                "slug": str(org.get("slug") or clean_org_id),
                "name": clean_name,
            }

    def check_org_slug_availability(self, *, slug: str, max_suggestions: int = 3) -> Dict[str, Any]:
        normalized = _normalize_slug(slug)
        safe_limit = max(0, min(10, int(max_suggestions)))
        suggestions: List[str] = []
        with self._lock:
            available = normalized not in self._slug_to_org
            if not available and safe_limit > 0:
                suffix = 2
                while len(suggestions) < safe_limit and suffix <= 9999:
                    candidate = f"{normalized}-{suffix}"
                    if candidate not in self._slug_to_org:
                        suggestions.append(candidate)
                    suffix += 1
        return {"slug": normalized, "available": available, "suggestions": suggestions}

    def create_service(
        self,
        *,
        org_id: str,
        service_key: str,
        requested_by_uid: str,
        title: Optional[str] = None,
        timezone: Optional[str] = None,
        source: str = "ko",
        target: str = "en",
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        normalized_key = _normalize_service_key(service_key)
        title_token = _clean_token(title) or _default_service_title(normalized_key)
        timezone_token = _clean_token(timezone) or "America/Chicago"
        src = (_clean_token(source) or "ko").lower()
        tgt = (_clean_token(target) or "en").lower()

        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            role = self._member_role(clean_org_id, clean_uid)
            if role not in {"owner", "admin", "host"}:
                raise PermissionError("forbidden")
            row_key = (clean_org_id, normalized_key)
            if row_key in self._services:
                raise ValueError("service_exists")
            now = _utcnow()
            billing = _normalize_org_billing(org, now=now)
            org["billing"] = billing
            if _billing_trial_minutes_exhausted(billing):
                raise PermissionError("trial_expired")
            if _entitlements_v2_effective(org):
                denial = _billing_start_denial_reason(billing=billing, now=now)
                if denial:
                    raise PermissionError(denial)
                max_service_keys = _billing_max_service_keys(billing)
                if max_service_keys > 0:
                    existing_count = sum(1 for (row_org, _service_key) in self._services.keys() if row_org == clean_org_id)
                    if existing_count >= max_service_keys:
                        raise PermissionError("plan_limit_reached")
            self._services[row_key] = {
                "title": title_token,
                "timezone": timezone_token,
                "rrule": None,
                "defaultLanguagePair": {"source": src, "target": tgt},
                "activeRoomId": None,
                "lastRoomId": None,
                "updatedAt": now,
            }
            return {
                "orgId": clean_org_id,
                "serviceKey": normalized_key,
                "title": title_token,
                "timezone": timezone_token,
                "activeRoomId": None,
                "roomStatus": "waiting",
                "defaultLanguagePair": {"source": src, "target": tgt},
            }

    def delete_service(self, *, org_id: str, service_key: str, requested_by_uid: str) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        normalized_key = _normalize_service_key(service_key)

        with self._lock:
            if clean_org_id not in self._orgs:
                raise ValueError("org_not_found")
            role = self._member_role(clean_org_id, clean_uid)
            if role not in {"owner", "admin", "host"}:
                raise PermissionError("forbidden")
            row_key = (clean_org_id, normalized_key)
            service = self._services.get(row_key)
            if not service:
                raise ValueError("service_not_found")
            active_room_id = _clean_token(service.get("activeRoomId"))
            if active_room_id:
                room = self._rooms.get((clean_org_id, active_room_id))
                if room and str(room.get("status") or "") == "live":
                    raise ValueError("service_active")
            self._services.pop(row_key, None)
            return {"deleted": True, "orgId": clean_org_id, "serviceKey": normalized_key}

    def list_memberships(self, uid: str) -> List[Dict[str, Any]]:
        clean_uid = _clean_token(uid)
        if not clean_uid:
            return []
        with self._lock:
            if _is_master_uid(clean_uid):
                rows = [
                    {
                        "orgId": org_id,
                        "slug": str(org.get("slug") or org_id),
                        "name": str(org.get("name") or org_id),
                        "status": str(org.get("status") or "active"),
                        "role": "owner",
                        "hostToken": str(org.get("hostToken") or ""),
                        "billingLimitsEnabled": _org_billing_limits_flag(org),
                    }
                    for org_id, org in self._orgs.items()
                ]
                rows.sort(key=lambda row: (row["orgId"], row["slug"]))
                return rows

            rows: List[Dict[str, Any]] = []
            for (org_id, member_uid), member in self._members.items():
                if member_uid != clean_uid:
                    continue
                org = self._orgs.get(org_id)
                if not org:
                    continue
                role = str(member.get("role") or "viewer")
                lowered_role = role.strip().lower()
                rows.append(
                    {
                        "orgId": org_id,
                        "slug": str(org.get("slug") or org_id),
                        "name": str(org.get("name") or org_id),
                        "status": str(org.get("status") or "active"),
                        "role": role,
                        "hostToken": str(org.get("hostToken") or "") if lowered_role in {"owner", "admin", "host"} else None,
                        "billingLimitsEnabled": _org_billing_limits_flag(org),
                    }
                )
            rows.sort(key=lambda row: (row["orgId"], row["slug"]))
            return rows

    def get_current_org_id(self, uid: str) -> Optional[str]:
        clean_uid = _clean_token(uid)
        if not clean_uid:
            return None
        with self._lock:
            if _is_master_uid(clean_uid):
                current_org_id = _clean_token((self._users.get(clean_uid) or {}).get("currentOrgId"))
                if current_org_id and current_org_id in self._orgs:
                    return current_org_id
                org_ids = sorted(self._orgs.keys())
                if not org_ids:
                    return None
                chosen_org_id = org_ids[0]
                profile = self._users.setdefault(clean_uid, {})
                profile["currentOrgId"] = chosen_org_id
                profile["updatedAt"] = _utcnow()
                return chosen_org_id

            current_org_id = _clean_token((self._users.get(clean_uid) or {}).get("currentOrgId"))
            if current_org_id and (current_org_id, clean_uid) in self._members:
                return current_org_id
            memberships = [
                org_id for (org_id, member_uid) in self._members.keys() if member_uid == clean_uid
            ]
            memberships.sort()
            if not memberships:
                return None
            chosen_org_id = memberships[0]
            profile = self._users.setdefault(clean_uid, {})
            profile["currentOrgId"] = chosen_org_id
            profile["updatedAt"] = _utcnow()
            return chosen_org_id

    def set_current_org(self, uid: str, org_id: str) -> str:
        clean_uid = _clean_token(uid)
        clean_org_id = _clean_token(org_id)
        if not clean_uid:
            raise ValueError("invalid_uid")
        if not clean_org_id:
            raise ValueError("org_not_found")
        with self._lock:
            if clean_org_id not in self._orgs:
                raise ValueError("org_not_found")
            if not _is_master_uid(clean_uid) and (clean_org_id, clean_uid) not in self._members:
                raise PermissionError("org_access_denied")
            profile = self._users.setdefault(clean_uid, {})
            profile["currentOrgId"] = clean_org_id
            profile["updatedAt"] = _utcnow()
            return clean_org_id

    def is_master_user(self, uid: str) -> bool:
        return _is_master_uid(uid)

    def get_org_billing_profile(self, *, org_id: str) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        if not clean_org_id:
            raise ValueError("org_not_found")
        now = _utcnow()
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            billing = _normalize_billing_state(
                org.get("billing"),
                plan_key=str(org.get("plan") or "trial"),
                now=now,
            )
            org["billing"] = billing
            org["updatedAt"] = now
            return dict(billing)

    def set_org_billing_profile(self, *, org_id: str, billing: Dict[str, Any]) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        if not clean_org_id:
            raise ValueError("org_not_found")
        now = _utcnow()
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            merged = _normalize_billing_state(
                billing or {},
                plan_key=str(org.get("plan") or "trial"),
                now=now,
            )
            org["billing"] = merged
            org["plan"] = str(merged.get("planKey") or org.get("plan") or "trial")
            org["status"] = "trial" if str(merged.get("status") or "").strip().lower() == "trialing" else str(
                merged.get("status") or org.get("status") or "active"
            )
            org["updatedAt"] = now
            return dict(merged)

    def get_org_effective_trial_seconds_remaining(self, *, org_id: str) -> Optional[int]:
        clean_org_id = _clean_token(org_id)
        if not clean_org_id:
            raise ValueError("org_not_found")
        now = _utcnow()
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            billing = _normalize_billing_state(
                org.get("billing"),
                plan_key=str(org.get("plan") or "trial"),
                now=now,
            )
            org["billing"] = billing
            live_rooms = [
                room
                for (row_org_id, _room_id), room in self._rooms.items()
                if row_org_id == clean_org_id and str(room.get("status") or "").strip().lower() == "live"
            ]
            return _effective_trial_seconds_remaining_for_rooms(billing, live_rooms, now=now)

    def ensure_org_can_start_service(self, *, org_id: str) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        if not clean_org_id:
            raise ValueError("org_not_found")
        now = _utcnow()
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            self._roll_billing_period_if_needed(org, now)
            org_status = _status_token(org.get("status"))
            if org_status in _HARD_INACTIVE_ORG_STATUSES:
                raise PermissionError("org_inactive")
            if _org_billing_limits_enabled(org) and bool(org.get("hardCapReached")):
                raise PermissionError("hard_cap_reached")
            billing = _normalize_org_billing(org, now=now)
            org["billing"] = billing
            if _billing_trial_minutes_exhausted(billing):
                raise PermissionError("trial_expired")
            if _entitlements_v2_effective(org):
                denial = _billing_start_denial_reason(billing=billing, now=now)
                if denial:
                    raise PermissionError(denial)
            elif not _legacy_org_start_allowed(org):
                raise PermissionError("org_inactive")
            return {
                "orgId": clean_org_id,
                "allowed": True,
                "billing": dict(billing),
                "entitlementsV2": _entitlements_v2_effective(org),
            }

    def find_org_id_by_billing_refs(
        self,
        *,
        stripe_customer_id: Optional[str] = None,
        stripe_subscription_id: Optional[str] = None,
    ) -> Optional[str]:
        clean_customer = _clean_token(stripe_customer_id)
        clean_subscription = _clean_token(stripe_subscription_id)
        if not clean_customer and not clean_subscription:
            return None
        with self._lock:
            for org_id, org in self._orgs.items():
                billing = _normalize_billing_state(
                    org.get("billing"),
                    plan_key=str(org.get("plan") or "trial"),
                    now=_utcnow(),
                )
                if clean_subscription and str(billing.get("stripeSubscriptionId") or "").strip() == clean_subscription:
                    return org_id
                if clean_customer and str(billing.get("stripeCustomerId") or "").strip() == clean_customer:
                    return org_id
        return None

    def try_mark_billing_event(
        self,
        *,
        event_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        clean_event_id = _clean_token(event_id)
        if not clean_event_id:
            raise ValueError("invalid_event_id")
        with self._lock:
            if clean_event_id in self._billing_events:
                return False
            self._billing_events[clean_event_id] = {
                "eventId": clean_event_id,
                "eventType": str(event_type or "").strip(),
                "payload": dict(payload or {}),
                "createdAt": _utcnow(),
            }
            return True

    def get_org_billing_limits(
        self,
        *,
        org_id: str,
        requested_by_uid: str,
        requested_by_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        clean_email = _clean_token(requested_by_email)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            if not _is_billing_admin(clean_uid, email=clean_email):
                raise PermissionError("billing_admin_required")
            return _billing_limits_payload(org_id=clean_org_id, org=org)

    def set_org_billing_limits(
        self,
        *,
        org_id: str,
        requested_by_uid: str,
        enabled: bool,
        requested_by_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        clean_email = _clean_token(requested_by_email)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            if not _is_billing_admin(clean_uid, email=clean_email):
                raise PermissionError("billing_admin_required")
            org["billingLimitsEnabled"] = bool(enabled)
            org["updatedAt"] = _utcnow()
            return _billing_limits_payload(org_id=clean_org_id, org=org)

    def ensure_sermon_prep_budget_not_reached(self, *, org_id: str) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        if not clean_org_id:
            raise ValueError("org_not_found")
        period_key = _yyyymm(_utcnow())
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            usage_month = dict(self._usage.get((clean_org_id, period_key)) or {})
            sermon_rows = [
                dict(row)
                for (row_org_id, row_period_key, _row_sermon_id), row in self._usage_sermons.items()
                if row_org_id == clean_org_id and row_period_key == period_key
            ]
            payload = _sermon_usage_payload(
                org_id=clean_org_id,
                org=org,
                period_key=period_key,
                usage_month=usage_month,
                sermon_rows=sermon_rows,
            )
            if bool(payload.get("capReached")):
                raise PermissionError("sermon_prep_budget_reached")
            return payload

    def get_org_sermon_prep_usage(
        self,
        *,
        org_id: str,
        requested_by_uid: str,
        period_key: Optional[str] = None,
        sermon_id: Optional[str] = None,
        limit: int = 25,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        clean_sermon_id = _clean_token(sermon_id)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        safe_limit = max(1, min(200, int(limit or 25)))
        resolved_period = _normalize_period_key(period_key, now=_utcnow())
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            role = self._member_role(clean_org_id, clean_uid)
            if role not in {"owner", "admin", "host"}:
                raise PermissionError("forbidden")
            usage_month = dict(self._usage.get((clean_org_id, resolved_period)) or {})
            sermon_rows = [
                dict(row)
                for (row_org_id, row_period_key, row_sermon_id), row in self._usage_sermons.items()
                if row_org_id == clean_org_id
                and row_period_key == resolved_period
                and (not clean_sermon_id or row_sermon_id == clean_sermon_id)
            ]
            payload = _sermon_usage_payload(
                org_id=clean_org_id,
                org=org,
                period_key=resolved_period,
                usage_month=usage_month,
                sermon_rows=sermon_rows,
            )
            payload["sermons"] = (payload.get("sermons") or [])[:safe_limit]
            return payload

    def set_org_sermon_prep_budget(
        self,
        *,
        org_id: str,
        requested_by_uid: str,
        budget_usd: float,
        requested_by_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        clean_email = _clean_token(requested_by_email)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        parsed_budget = _safe_float(budget_usd, -1.0)
        if parsed_budget < 0:
            raise ValueError("invalid_budget")
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            if not _is_billing_admin(clean_uid, email=clean_email):
                raise PermissionError("billing_admin_required")
            now = _utcnow()
            org["sermonPrepBudgetUsd"] = float(parsed_budget)
            org["updatedAt"] = now
            period_key = _yyyymm(now)
            usage_month = dict(self._usage.get((clean_org_id, period_key)) or {})
            sermon_rows = [
                dict(row)
                for (row_org_id, row_period_key, _row_sermon_id), row in self._usage_sermons.items()
                if row_org_id == clean_org_id and row_period_key == period_key
            ]
            payload = _sermon_usage_payload(
                org_id=clean_org_id,
                org=org,
                period_key=period_key,
                usage_month=usage_month,
                sermon_rows=sermon_rows,
            )
            payload["sermons"] = (payload.get("sermons") or [])[:25]
            return payload

    def record_sermon_prep_usage(
        self,
        *,
        org_id: str,
        sermon_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        estimated_usd: float,
        requests_count: int = 1,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_sermon_id = _clean_token(sermon_id)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_sermon_id:
            raise ValueError("invalid_sermon_id")
        prompt = _int_tokens(prompt_tokens)
        completion = _int_tokens(completion_tokens)
        total = _int_tokens(total_tokens) or (prompt + completion)
        calls = max(0, _int_tokens(requests_count) or 0)
        usd = _round_usd(estimated_usd)
        if usd <= 0.0 and (prompt > 0 or completion > 0):
            usd = _round_usd(_estimate_sermon_prep_usd(prompt_tokens=prompt, completion_tokens=completion))
        clean_model = str(model or "").strip()

        now = _utcnow()
        period_key = _yyyymm(now)
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            usage_key = (clean_org_id, period_key)
            usage = self._usage.get(usage_key) or {}
            usage["sermonPromptTokens"] = _int_tokens(usage.get("sermonPromptTokens")) + prompt
            usage["sermonCompletionTokens"] = _int_tokens(usage.get("sermonCompletionTokens")) + completion
            usage["sermonTotalTokens"] = _int_tokens(usage.get("sermonTotalTokens")) + total
            usage["sermonEstimatedUsd"] = _round_usd(_safe_float(usage.get("sermonEstimatedUsd"), 0.0) + usd)
            usage["sermonRequestsCount"] = _int_tokens(usage.get("sermonRequestsCount")) + calls
            usage["updatedAt"] = now
            self._usage[usage_key] = usage

            sermon_key = (clean_org_id, period_key, clean_sermon_id)
            sermon_usage = self._usage_sermons.get(sermon_key) or {
                "sermonId": clean_sermon_id,
                "promptTokens": 0,
                "completionTokens": 0,
                "totalTokens": 0,
                "estimatedUsd": 0.0,
                "requestsCount": 0,
                "lastModel": "",
                "lastUsedAt": now,
            }
            sermon_usage["promptTokens"] = _int_tokens(sermon_usage.get("promptTokens")) + prompt
            sermon_usage["completionTokens"] = _int_tokens(sermon_usage.get("completionTokens")) + completion
            sermon_usage["totalTokens"] = _int_tokens(sermon_usage.get("totalTokens")) + total
            sermon_usage["estimatedUsd"] = _round_usd(_safe_float(sermon_usage.get("estimatedUsd"), 0.0) + usd)
            sermon_usage["requestsCount"] = _int_tokens(sermon_usage.get("requestsCount")) + calls
            if clean_model:
                sermon_usage["lastModel"] = clean_model
            sermon_usage["lastUsedAt"] = now
            self._usage_sermons[sermon_key] = sermon_usage

            sermon_rows = [
                dict(row)
                for (row_org_id, row_period_key, _row_sermon_id), row in self._usage_sermons.items()
                if row_org_id == clean_org_id and row_period_key == period_key
            ]
            payload = _sermon_usage_payload(
                org_id=clean_org_id,
                org=org,
                period_key=period_key,
                usage_month=usage,
                sermon_rows=sermon_rows,
            )
            payload["sermons"] = (payload.get("sermons") or [])[:25]
            return payload

    def get_org_prompt(self, *, org_id: str, requested_by_uid: str) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            role = self._member_role(clean_org_id, clean_uid)
            if role not in PROMPT_EDITOR_ROLES:
                raise PermissionError("forbidden")
            return {
                "orgId": clean_org_id,
                "prompt": str(org.get("customPrompt") or ""),
                "service_prompt": str(org.get("servicePrompt") or ""),
                "updatedAt": org.get("updatedAt"),
            }

    def set_org_prompt(
        self,
        *,
        org_id: str,
        requested_by_uid: str,
        prompt: str,
        service_prompt: str,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            role = self._member_role(clean_org_id, clean_uid)
            if role not in PROMPT_EDITOR_ROLES:
                raise PermissionError("forbidden")
            now = _utcnow()
            org["customPrompt"] = str(prompt or "")
            org["servicePrompt"] = str(service_prompt or "")
            org["updatedAt"] = now
            return {
                "orgId": clean_org_id,
                "prompt": str(org.get("customPrompt") or ""),
                "service_prompt": str(org.get("servicePrompt") or ""),
                "updatedAt": now,
            }

    def get_org_prompt_for_translation(self, org_id: Optional[str]) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        if not clean_org_id:
            return {"orgId": "", "prompt": "", "service_prompt": ""}
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                return {"orgId": clean_org_id, "prompt": "", "service_prompt": ""}
            return {
                "orgId": clean_org_id,
                "prompt": str(org.get("customPrompt") or ""),
                "service_prompt": str(org.get("servicePrompt") or ""),
            }

    def _cleanup_invites_locked(self, *, org_id: Optional[str], now: datetime) -> None:
        cutoff = now - timedelta(days=INVITE_RETENTION_DAYS)
        to_delete: List[str] = []
        for invite_id, invite in self._org_invites.items():
            if org_id and _clean_token(invite.get("orgId")) != org_id:
                continue
            invite_status = str(invite.get("status") or "")
            expires_at = invite.get("expiresAt")
            if invite_status == INVITE_STATUS_ACTIVE and isinstance(expires_at, datetime) and expires_at <= now:
                invite["status"] = INVITE_STATUS_EXPIRED
                invite["updatedAt"] = now
                invite_status = INVITE_STATUS_EXPIRED

            if invite_status == INVITE_STATUS_ACTIVE:
                continue

            marker = (
                invite.get("updatedAt")
                or invite.get("consumedAt")
                or invite.get("revokedAt")
                or invite.get("createdAt")
            )
            if isinstance(marker, datetime) and marker <= cutoff:
                to_delete.append(invite_id)

        for invite_id in to_delete:
            self._org_invites.pop(invite_id, None)

    def _active_invite_count_locked(self, org_id: str, *, now: datetime) -> int:
        self._cleanup_invites_locked(org_id=org_id, now=now)
        count = 0
        for invite in self._org_invites.values():
            if _clean_token(invite.get("orgId")) != org_id:
                continue
            if str(invite.get("status") or "") == INVITE_STATUS_ACTIVE:
                count += 1
        return count

    def _record_invite_audit_locked(
        self,
        *,
        org_id: str,
        invite_id: str,
        action: str,
        actor_uid: Optional[str],
        target_uid: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._invite_audits.append(
            {
                "eventId": uuid4().hex,
                "orgId": org_id,
                "inviteId": invite_id,
                "action": action,
                "actorUid": _clean_token(actor_uid),
                "targetUid": _clean_token(target_uid),
                "metadata": dict(metadata or {}),
                "createdAt": _utcnow(),
            }
        )

    def create_invite(
        self,
        *,
        org_id: str,
        created_by_uid: str,
        role: str,
        expires_in_hours: int = DEFAULT_INVITE_EXPIRY_HOURS,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_creator_uid = _clean_token(created_by_uid)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_creator_uid:
            raise ValueError("invalid_uid")
        invite_role = _normalize_invite_role(role)
        expires_at = _invite_expiry(expires_in_hours)
        now = _utcnow()
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            creator_role = self._member_role(clean_org_id, clean_creator_uid)
            if creator_role not in {"owner", "admin"}:
                raise PermissionError("forbidden")
            active_count = self._active_invite_count_locked(clean_org_id, now=now)
            if active_count >= MAX_ACTIVE_INVITES_PER_ORG:
                raise ValueError("invite_active_limit_reached")
            code = _new_invite_code()
            code_hash = _invite_code_hash(code)
            while code_hash in self._org_invites:
                code = _new_invite_code()
                code_hash = _invite_code_hash(code)
            self._org_invites[code_hash] = {
                "inviteId": code_hash,
                "codeHash": code_hash,
                "orgId": clean_org_id,
                "slug": str(org.get("slug") or clean_org_id),
                "name": str(org.get("name") or clean_org_id),
                "role": invite_role,
                "status": INVITE_STATUS_ACTIVE,
                "expiresAt": expires_at,
                "createdBy": clean_creator_uid,
                "createdAt": now,
                "consumedBy": None,
                "consumedAt": None,
                "updatedAt": now,
            }
            self._record_invite_audit_locked(
                org_id=clean_org_id,
                invite_id=code_hash,
                action="created",
                actor_uid=clean_creator_uid,
                metadata={"role": invite_role, "expiresHours": int(expires_in_hours)},
            )
            return {
                "inviteId": code_hash,
                "code": code,
                "orgId": clean_org_id,
                "slug": str(org.get("slug") or clean_org_id),
                "name": str(org.get("name") or clean_org_id),
                "role": invite_role,
                "status": INVITE_STATUS_ACTIVE,
                "expiresAt": expires_at,
                "createdAt": now,
            }

    def preview_invite(self, *, code: str, uid: str) -> Dict[str, Any]:
        clean_uid = _clean_token(uid)
        clean_code = _clean_token(code)
        if not clean_uid:
            raise ValueError("invalid_uid")
        if not clean_code:
            raise ValueError("invite_not_found")
        now = _utcnow()
        with self._lock:
            self._cleanup_invites_locked(org_id=None, now=now)
            invite = self._org_invites.get(_invite_code_hash(clean_code))
            if not invite:
                raise ValueError("invite_not_found")
            status = str(invite.get("status") or "")
            if status != INVITE_STATUS_ACTIVE:
                raise ValueError("invite_invalid")
            expires_at = invite.get("expiresAt")
            if isinstance(expires_at, datetime) and expires_at <= now:
                invite["status"] = INVITE_STATUS_EXPIRED
                invite["updatedAt"] = now
                raise ValueError("invite_expired")

            org_id = _clean_token(invite.get("orgId"))
            if not org_id:
                raise ValueError("org_not_found")
            org = self._orgs.get(org_id)
            if not org:
                raise ValueError("org_not_found")
            already_member = (org_id, clean_uid) in self._members
            return {
                "inviteId": str(invite.get("inviteId") or ""),
                "orgId": org_id,
                "slug": str(org.get("slug") or org_id),
                "name": str(org.get("name") or org_id),
                "role": str(invite.get("role") or "viewer"),
                "status": status,
                "expiresAt": expires_at,
                "alreadyMember": already_member,
            }

    def redeem_invite(
        self,
        *,
        code: str,
        uid: str,
        email: Optional[str],
        display_name: Optional[str],
    ) -> Dict[str, Any]:
        clean_uid = _clean_token(uid)
        clean_code = _clean_token(code)
        if not clean_uid:
            raise ValueError("invalid_uid")
        if not clean_code:
            raise ValueError("invite_not_found")
        clean_email = _clean_token(email)
        clean_display_name = _clean_token(display_name)
        now = _utcnow()
        with self._lock:
            self._cleanup_invites_locked(org_id=None, now=now)
            invite = self._org_invites.get(_invite_code_hash(clean_code))
            if not invite:
                raise ValueError("invite_not_found")
            status = str(invite.get("status") or "")
            if status != INVITE_STATUS_ACTIVE:
                raise ValueError("invite_invalid")
            expires_at = invite.get("expiresAt")
            if isinstance(expires_at, datetime) and expires_at <= now:
                invite["status"] = INVITE_STATUS_EXPIRED
                invite["updatedAt"] = now
                raise ValueError("invite_expired")

            org_id = _clean_token(invite.get("orgId"))
            if not org_id:
                raise ValueError("org_not_found")
            org = self._orgs.get(org_id)
            if not org:
                raise ValueError("org_not_found")

            invite_role = _normalize_invite_role(invite.get("role"))
            created = False
            member = self._members.get((org_id, clean_uid))
            if member:
                member_role = _normalize_role(member.get("role"), fallback="viewer")
                member["updatedAt"] = now
                if clean_email and not _clean_token(member.get("email")):
                    member["email"] = clean_email
                if clean_display_name and not _clean_token(member.get("displayName")):
                    member["displayName"] = clean_display_name
            else:
                created = True
                member_role = invite_role
                self._members[(org_id, clean_uid)] = {
                    "role": member_role,
                    "email": clean_email,
                    "displayName": clean_display_name,
                    "createdAt": now,
                    "updatedAt": now,
                }

            profile = self._users.setdefault(clean_uid, {})
            profile["currentOrgId"] = org_id
            if clean_email:
                profile["email"] = clean_email
            if clean_display_name:
                profile["displayName"] = clean_display_name
            profile["updatedAt"] = now

            invite["status"] = INVITE_STATUS_CONSUMED
            invite["consumedBy"] = clean_uid
            invite["consumedAt"] = now
            invite["updatedAt"] = now
            self._record_invite_audit_locked(
                org_id=org_id,
                invite_id=str(invite.get("inviteId") or _invite_code_hash(clean_code)),
                action="redeemed",
                actor_uid=clean_uid,
                target_uid=clean_uid,
                metadata={"createdMembership": created},
            )

            member_role = _normalize_role(member_role, fallback="viewer")
            return {
                "orgId": org_id,
                "slug": str(org.get("slug") or org_id),
                "name": str(org.get("name") or org_id),
                "role": member_role,
                "created": created,
                "alreadyMember": not created,
                "currentOrgId": org_id,
                "hostToken": str(org.get("hostToken") or "") if member_role in {"owner", "admin", "host"} else None,
            }

    def list_invites(
        self,
        *,
        org_id: str,
        requested_by_uid: str,
        status: Optional[str] = INVITE_STATUS_ACTIVE,
    ) -> List[Dict[str, Any]]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        status_filter = _normalize_invite_status(status)
        now = _utcnow()
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            role = self._member_role(clean_org_id, clean_uid)
            if role not in {"owner", "admin"}:
                raise PermissionError("forbidden")
            self._cleanup_invites_locked(org_id=clean_org_id, now=now)

            rows: List[Dict[str, Any]] = []
            for invite in self._org_invites.values():
                if _clean_token(invite.get("orgId")) != clean_org_id:
                    continue
                invite_status = str(invite.get("status") or "")
                expires_at = invite.get("expiresAt")
                if invite_status == INVITE_STATUS_ACTIVE and isinstance(expires_at, datetime) and expires_at <= now:
                    invite_status = INVITE_STATUS_EXPIRED
                    invite["status"] = INVITE_STATUS_EXPIRED
                    invite["updatedAt"] = now
                if status_filter and invite_status != status_filter:
                    continue
                rows.append(_serialize_invite(invite, fallback_org=org))
            rows.sort(key=lambda row: row.get("createdAt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            return rows

    def revoke_invite(
        self,
        *,
        org_id: str,
        invite_id: str,
        revoked_by_uid: str,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_invite_id = _clean_token(invite_id)
        clean_uid = _clean_token(revoked_by_uid)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_invite_id:
            raise ValueError("invite_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        now = _utcnow()
        with self._lock:
            org = self._orgs.get(clean_org_id)
            if not org:
                raise ValueError("org_not_found")
            role = self._member_role(clean_org_id, clean_uid)
            if role not in {"owner", "admin"}:
                raise PermissionError("forbidden")

            invite = self._org_invites.get(clean_invite_id)
            if not invite:
                raise ValueError("invite_not_found")
            if _clean_token(invite.get("orgId")) != clean_org_id:
                raise ValueError("invite_not_found")

            invite_status = str(invite.get("status") or "")
            expires_at = invite.get("expiresAt")
            if invite_status == INVITE_STATUS_ACTIVE and isinstance(expires_at, datetime) and expires_at <= now:
                invite["status"] = INVITE_STATUS_EXPIRED
                invite["updatedAt"] = now
                raise ValueError("invite_expired")
            if invite_status != INVITE_STATUS_ACTIVE:
                raise ValueError("invite_invalid")

            invite["status"] = INVITE_STATUS_REVOKED
            invite["revokedBy"] = clean_uid
            invite["revokedAt"] = now
            invite["updatedAt"] = now
            self._record_invite_audit_locked(
                org_id=clean_org_id,
                invite_id=clean_invite_id,
                action="revoked",
                actor_uid=clean_uid,
            )
            return _serialize_invite(invite, fallback_org=org)

    def bootstrap_owner_org(
        self,
        *,
        owner_uid: str,
        owner_email: Optional[str],
        owner_display_name: Optional[str],
        church_name: str,
        church_slug: str,
        timezone: str,
        source: str,
        target: str,
    ) -> Dict[str, Any]:
        clean_uid = _clean_token(owner_uid)
        clean_name = _clean_token(church_name)
        if not clean_uid:
            raise ValueError("invalid_uid")
        if not clean_name:
            raise ValueError("invalid_name")
        slug = _normalize_slug(church_slug)
        tz = _clean_token(timezone) or "America/Chicago"
        src = (_clean_token(source) or "ko").lower()
        tgt = (_clean_token(target) or "en").lower()
        host_token = secrets.token_urlsafe(24)

        with self._lock:
            existing_memberships: List[Dict[str, Any]] = []
            for (org_id, member_uid), member in self._members.items():
                if member_uid != clean_uid:
                    continue
                org = self._orgs.get(org_id)
                if not org:
                    continue
                existing_memberships.append(
                    {
                        "orgId": org_id,
                        "slug": str(org.get("slug") or org_id),
                        "name": str(org.get("name") or org_id),
                        "status": str(org.get("status") or "active"),
                        "role": str(member.get("role") or "viewer"),
                    }
                )
            existing_memberships.sort(key=lambda row: (row["orgId"], row["slug"]))
            if existing_memberships:
                org = existing_memberships[0]
                profile = self._users.setdefault(clean_uid, {})
                profile["currentOrgId"] = org["orgId"]
                profile["updatedAt"] = _utcnow()
                return {
                    "created": False,
                    "orgId": org["orgId"],
                    "slug": org["slug"],
                    "name": org["name"],
                    "role": org["role"],
                    "hostToken": None,
                    "services": [],
                }

            if slug in self._slug_to_org:
                raise ValueError("slug_taken")

            org_id = slug
            if org_id in self._orgs:
                suffix = 1
                while f"{slug}-{suffix}" in self._orgs:
                    suffix += 1
                org_id = f"{slug}-{suffix}"

            now = _utcnow()
            self._orgs[org_id] = {
                "slug": slug,
                "name": clean_name,
                "plan": "trial",
                "status": "active",
                "billing": _default_billing_state(now=now, plan_key="trial"),
                "maxMinutesPerMonth": 500,
                "currentMonthMinutes": 0,
                "currentMonthKey": _yyyymm(now),
                "maxConcurrentRooms": 1,
                "billingLimitsEnabled": True,
                "hardCapReached": False,
                "sermonPrepBudgetUsd": float(SERMON_PREP_DEFAULT_BUDGET_USD),
                "hostToken": host_token,
                "customPrompt": "",
                "servicePrompt": "",
                "createdAt": now,
                "updatedAt": now,
            }
            self._slug_to_org[slug] = org_id
            self._members[(org_id, clean_uid)] = {
                "role": "owner",
                "email": _clean_token(owner_email),
                "displayName": _clean_token(owner_display_name),
                "createdAt": now,
                "updatedAt": now,
            }
            self._users[clean_uid] = {
                "email": _clean_token(owner_email),
                "displayName": _clean_token(owner_display_name),
                "currentOrgId": org_id,
                "updatedAt": now,
            }

            service_rows: List[Dict[str, str]] = []
            for service_key, title in DEFAULT_SERVICE_SEEDS:
                self._services[(org_id, service_key)] = {
                    "title": title,
                    "timezone": tz,
                    "rrule": None,
                    "defaultLanguagePair": {"source": src, "target": tgt},
                    "activeRoomId": None,
                    "lastRoomId": None,
                    "updatedAt": now,
                }
                service_rows.append({"serviceKey": service_key, "title": title})

            return {
                "created": True,
                "orgId": org_id,
                "slug": slug,
                "name": clean_name,
                "role": "owner",
                "hostToken": host_token,
                "services": service_rows,
            }

    def start_service(
        self,
        org_id: str,
        service_key: str,
        *,
        host_uid: Optional[str],
        source: str,
        target: str,
    ) -> Dict[str, Any]:
        now = _utcnow()
        with self._lock:
            org = self._orgs.get(org_id)
            if not org:
                raise ValueError("org_not_found")
            self._roll_billing_period_if_needed(org, now)
            org_status = _status_token(org.get("status"))
            if org_status in _HARD_INACTIVE_ORG_STATUSES:
                raise PermissionError("org_inactive")
            if _org_billing_limits_enabled(org) and bool(org.get("hardCapReached")):
                raise PermissionError("hard_cap_reached")
            billing = _normalize_org_billing(org, now=now)
            org["billing"] = billing
            if _billing_trial_minutes_exhausted(billing):
                raise PermissionError("trial_expired")
            if _entitlements_v2_effective(org):
                denial = _billing_start_denial_reason(billing=billing, now=now)
                if denial:
                    raise PermissionError(denial)
            elif not _legacy_org_start_allowed(org):
                raise PermissionError("org_inactive")

            service = self._services.get((org_id, service_key))
            if not service:
                raise ValueError("service_not_found")

            active_room_id = service.get("activeRoomId")
            if active_room_id:
                active_room = self._rooms.get((org_id, active_room_id))
                if active_room and active_room.get("status") == "live":
                    return {
                        "orgId": org_id,
                        "serviceKey": service_key,
                        "roomId": active_room_id,
                        "status": "live",
                        "languagePair": active_room.get("languagePair") or {"source": source, "target": target},
                    }

            max_concurrent = int(org.get("maxConcurrentRooms") or 0)
            if max_concurrent > 0:
                live_count = 0
                for (row_org_id, _room_id), row_room in self._rooms.items():
                    if row_org_id == org_id and row_room.get("status") == "live":
                        live_count += 1
                if live_count >= max_concurrent:
                    raise PermissionError("concurrency_limit_reached")

            room_id = _new_room_id()
            self._rooms[(org_id, room_id)] = {
                "serviceKey": service_key,
                "status": "live",
                "startedAt": now,
                "endedAt": None,
                "hostUid": host_uid or "unknown",
                "languagePair": {"source": source, "target": target},
                "listenerCountPeak": 0,
                "billingPeriodKey": _yyyymm(now),
                "endReason": None,
                "lastAudioAt": now,
                "lastUsageTickAt": now,
                "finalTranscript": "",
            }
            service["activeRoomId"] = room_id
            service["updatedAt"] = now
            return {
                "orgId": org_id,
                "serviceKey": service_key,
                "roomId": room_id,
                "status": "live",
                "languagePair": {"source": source, "target": target},
            }

    def end_room(
        self,
        org_id: str,
        room_id: str,
        *,
        reason: str,
        transcript: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = _utcnow()
        with self._lock:
            room = self._rooms.get((org_id, room_id))
            if not room:
                raise ValueError("room_not_found")
            if room.get("status") == "ended":
                return {"orgId": org_id, "roomId": room_id, "status": "ended", "endedAt": room.get("endedAt"), "alreadyEnded": True}

            room["status"] = "ended"
            room["endedAt"] = now
            room["endReason"] = reason or "host_end"
            if transcript:
                room["finalTranscript"] = transcript

            service_key = room.get("serviceKey")
            service = self._services.get((org_id, service_key)) if service_key else None
            if service:
                if service.get("activeRoomId") == room_id:
                    service["activeRoomId"] = None
                service["lastRoomId"] = room_id
                service["updatedAt"] = now

            usage = self._usage.setdefault(
                (org_id, _yyyymm(now)),
                {
                    "minutesStreamed": 0,
                    "minutesTranslated": 0,
                    "peakListeners": 0,
                    "sessionsCount": 0,
                    "updatedAt": now,
                },
            )
            org = self._orgs.get(org_id)
            if org:
                self._roll_billing_period_if_needed(org, now)
                billing = _normalize_org_billing(org, now=now)
                org["billing"] = billing
                billing_limits_enabled = _org_billing_limits_enabled(org)
                has_monthly_cap = billing_limits_enabled and int(org.get("maxMinutesPerMonth") or 0) > 0
                has_trial_cap = _billing_trial_seconds_limit(billing) > 0
                remainder_seconds = _room_unbilled_elapsed_seconds(room, now=now)
                if remainder_seconds > 0 and (has_monthly_cap or has_trial_cap):
                    delta_minutes = _ceil_minutes_from_seconds(remainder_seconds)
                    usage["minutesTranslated"] += delta_minutes
                    if has_monthly_cap:
                        org["currentMonthMinutes"] = int(org.get("currentMonthMinutes") or 0) + delta_minutes
                        if int(org.get("currentMonthMinutes") or 0) >= int(org.get("maxMinutesPerMonth") or 0):
                            org["hardCapReached"] = True
                    if has_trial_cap:
                        _consume_billing_trial_seconds(billing, remainder_seconds)
                    room["lastUsageTickAt"] = now
                    usage["updatedAt"] = now
                    org["updatedAt"] = now

            started_at: datetime = room.get("startedAt") or now
            duration_min = max(1, int(math.ceil((now - started_at).total_seconds() / 60)))
            usage["minutesStreamed"] += duration_min
            usage["sessionsCount"] += 1
            usage["peakListeners"] = max(int(usage.get("peakListeners", 0)), int(room.get("listenerCountPeak", 0)))
            usage["updatedAt"] = now
            return {"orgId": org_id, "roomId": room_id, "status": "ended", "endedAt": now}

    def touch_audio(self, org_id: str, room_id: str) -> None:
        now = _utcnow()
        with self._lock:
            room = self._rooms.get((org_id, room_id))
            if room and room.get("status") == "live":
                room["lastAudioAt"] = now

    def bump_listener_peak(self, org_id: str, room_id: str, viewer_count: int) -> None:
        with self._lock:
            room = self._rooms.get((org_id, room_id))
            if not room:
                return
            room["listenerCountPeak"] = max(int(room.get("listenerCountPeak", 0)), int(viewer_count))

    def get_active_room(self, org_id: str, service_key: str) -> Optional[str]:
        with self._lock:
            service = self._services.get((org_id, service_key))
            if not service:
                return None
            active_room_id = service.get("activeRoomId")
            if not active_room_id:
                return None
            room = self._rooms.get((org_id, active_room_id))
            if not room or room.get("status") != "live":
                return None
            return active_room_id

    def stale_live_rooms(self, *, idle_seconds: int, max_duration_seconds: int) -> List[Dict[str, Any]]:
        now = _utcnow()
        out: List[Dict[str, Any]] = []
        with self._lock:
            for (org_id, room_id), room in self._rooms.items():
                if room.get("status") != "live":
                    continue
                started_at: datetime = room.get("startedAt") or now
                last_audio_at: datetime = room.get("lastAudioAt") or started_at
                age = (now - started_at).total_seconds()
                idle = (now - last_audio_at).total_seconds()
                if idle >= idle_seconds:
                    out.append({"orgId": org_id, "roomId": room_id, "reason": "idle_timeout"})
                elif age >= max_duration_seconds:
                    out.append({"orgId": org_id, "roomId": room_id, "reason": "max_duration"})
        return out

    def enforce_live_usage_caps(self, *, tick_seconds: int) -> List[Dict[str, Any]]:
        now = _utcnow()
        period_key = _yyyymm(now)
        tick_min = _tick_minutes(tick_seconds)
        out: List[Dict[str, Any]] = []
        with self._lock:
            flagged: Dict[tuple[str, str], str] = {}
            for org_id, org in self._orgs.items():
                self._roll_billing_period_if_needed(org, now)
                billing_limits_enabled = _org_billing_limits_enabled(org)
                billing = _normalize_org_billing(org, now=now)
                org["billing"] = billing
                has_trial_cap = _billing_trial_seconds_limit(billing) > 0
                if not billing_limits_enabled and not has_trial_cap:
                    continue
                monthly_reached = bool(org.get("hardCapReached")) if billing_limits_enabled else False
                trial_reached = _billing_trial_minutes_exhausted(billing)
                if monthly_reached or trial_reached:
                    reason = "trial_expired" if trial_reached else "monthly_limit_reached"
                    for (room_org_id, room_id), room in self._rooms.items():
                        if room_org_id == org_id and room.get("status") == "live":
                            flagged[(org_id, room_id)] = reason

            for (org_id, room_id), room in self._rooms.items():
                if room.get("status") != "live":
                    continue
                org = self._orgs.get(org_id)
                if not org:
                    continue
                billing_limits_enabled = _org_billing_limits_enabled(org)

                billing = org.get("billing")
                if not isinstance(billing, dict):
                    billing = _normalize_org_billing(org, now=now)
                    org["billing"] = billing

                max_minutes = int(org.get("maxMinutesPerMonth") or 0)
                has_monthly_cap = billing_limits_enabled and max_minutes > 0
                has_trial_cap = _billing_trial_seconds_limit(billing) > 0
                if not has_monthly_cap and not has_trial_cap:
                    continue

                monthly_reached_now = has_monthly_cap and bool(org.get("hardCapReached"))
                trial_reached_now = _billing_trial_minutes_exhausted(billing)
                if monthly_reached_now or trial_reached_now:
                    flagged[(org_id, room_id)] = "trial_expired" if trial_reached_now else "monthly_limit_reached"
                    continue

                last_tick_at = room.get("lastUsageTickAt") or room.get("startedAt") or now
                if not isinstance(last_tick_at, datetime):
                    last_tick_at = now
                elapsed_sec = (now - last_tick_at).total_seconds()
                increments = int(elapsed_sec // max(1, tick_seconds))
                if increments <= 0:
                    continue

                delta_minutes = increments * tick_min
                delta_seconds = increments * max(1, tick_seconds)
                room["lastUsageTickAt"] = last_tick_at + timedelta(seconds=increments * tick_seconds)

                usage = self._usage.setdefault(
                    (org_id, period_key),
                    {
                        "minutesStreamed": 0,
                        "minutesTranslated": 0,
                        "peakListeners": 0,
                        "sessionsCount": 0,
                        "updatedAt": now,
                    },
                )
                usage["minutesTranslated"] += delta_minutes
                usage["updatedAt"] = now

                if has_monthly_cap:
                    org["currentMonthMinutes"] = int(org.get("currentMonthMinutes") or 0) + delta_minutes
                if has_trial_cap:
                    _consume_billing_trial_seconds(billing, delta_seconds)

                monthly_reached = has_monthly_cap and int(org.get("currentMonthMinutes") or 0) >= max_minutes
                trial_reached = has_trial_cap and _billing_trial_minutes_exhausted(billing)
                if monthly_reached:
                    org["hardCapReached"] = True
                if monthly_reached or trial_reached:
                    flagged[(org_id, room_id)] = "trial_expired" if trial_reached else "monthly_limit_reached"

            for (org_id, room_id), reason in sorted(flagged.items()):
                out.append({"orgId": org_id, "roomId": room_id, "reason": reason})
        return out


class FirestoreMultiChurchStore:
    def __init__(self) -> None:
        assert gcf_firestore is not None
        project_id = (
            (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
            or (os.getenv("FIRESTORE_PROJECT") or "").strip()
            or (os.getenv("GCP_PROJECT") or "").strip()
            or None
        )
        self._db = gcf_firestore.Client(project=project_id, database="worship-translation")
        self._global_host_token = (os.getenv("HOST_API_TOKEN") or "").strip()
        self._membership_cache_ttl_seconds = _env_int(
            "MULTICHURCH_MEMBERSHIP_CACHE_TTL_SECONDS",
            10,
            min_value=0,
            max_value=300,
        )
        self._membership_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._membership_cache_lock = Lock()
        self._prompt_cache_ttl_seconds = _env_int(
            "MULTICHURCH_PROMPT_CACHE_TTL_SECONDS",
            20,
            min_value=0,
            max_value=300,
        )
        self._prompt_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._prompt_cache_lock = Lock()

    def _where(self, query: Any, field_path: str, op_string: str, value: Any):
        field_filter_cls = getattr(gcf_firestore, "FieldFilter", None)
        if field_filter_cls is not None:
            try:
                return query.where(filter=field_filter_cls(field_path, op_string, value))
            except TypeError:
                pass
        return query.where(field_path, op_string, value)

    def _org_ref(self, org_id: str):
        return self._db.collection("organizations").document(org_id)

    def _service_ref(self, org_id: str, service_key: str):
        return self._org_ref(org_id).collection("services").document(service_key)

    def _room_ref(self, org_id: str, room_id: str):
        return self._org_ref(org_id).collection("rooms").document(room_id)

    def _usage_ref(self, org_id: str, period_key: str):
        return self._org_ref(org_id).collection("usage").document(period_key)

    def _usage_sermon_ref(self, org_id: str, period_key: str, sermon_id: str):
        return self._usage_ref(org_id, period_key).collection("sermons").document(_sermon_usage_doc_id(sermon_id))

    def _user_ref(self, uid: str):
        return self._db.collection("users").document(uid)

    def _user_membership_ref(self, uid: str, org_id: str):
        return self._user_ref(uid).collection("memberships").document(org_id)

    def _get_cached_memberships(self, uid: str) -> Optional[List[Dict[str, Any]]]:
        if self._membership_cache_ttl_seconds <= 0:
            return None
        now = time.monotonic()
        with self._membership_cache_lock:
            cached = self._membership_cache.get(uid)
            if cached is None:
                return None
            expires_at, rows = cached
            if expires_at <= now:
                self._membership_cache.pop(uid, None)
                return None
            return [dict(row) for row in rows]

    def _set_cached_memberships(self, uid: str, rows: List[Dict[str, Any]]) -> None:
        if self._membership_cache_ttl_seconds <= 0:
            return
        expires_at = time.monotonic() + float(self._membership_cache_ttl_seconds)
        with self._membership_cache_lock:
            self._membership_cache[uid] = (expires_at, [dict(row) for row in rows])

    def _invalidate_membership_cache(self, uid: Optional[str]) -> None:
        clean_uid = _clean_token(uid)
        if not clean_uid:
            return
        with self._membership_cache_lock:
            self._membership_cache.pop(clean_uid, None)

    def _upsert_user_membership_index(
        self,
        *,
        uid: str,
        org_id: str,
        role: Optional[str],
        transaction=None,
    ) -> None:
        clean_uid = _clean_token(uid)
        clean_org_id = _clean_token(org_id)
        if not clean_uid or not clean_org_id:
            return
        payload = {
            "orgId": clean_org_id,
            "role": _normalize_role(role, fallback="viewer"),
            "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
        }
        ref = self._user_membership_ref(clean_uid, clean_org_id)
        if transaction is None:
            ref.set(payload, merge=True)
            return
        transaction.set(ref, payload, merge=True)

    def _get_cached_org_prompt(self, org_id: str) -> Optional[Dict[str, Any]]:
        if self._prompt_cache_ttl_seconds <= 0:
            return None
        now = time.monotonic()
        with self._prompt_cache_lock:
            cached = self._prompt_cache.get(org_id)
            if cached is None:
                return None
            expires_at, payload = cached
            if expires_at <= now:
                self._prompt_cache.pop(org_id, None)
                return None
            return dict(payload)

    def _set_cached_org_prompt(self, org_id: str, payload: Dict[str, Any]) -> None:
        if self._prompt_cache_ttl_seconds <= 0:
            return
        expires_at = time.monotonic() + float(self._prompt_cache_ttl_seconds)
        with self._prompt_cache_lock:
            self._prompt_cache[org_id] = (expires_at, dict(payload))

    def _invalidate_org_prompt_cache(self, org_id: Optional[str]) -> None:
        clean_org_id = _clean_token(org_id)
        if not clean_org_id:
            return
        with self._prompt_cache_lock:
            self._prompt_cache.pop(clean_org_id, None)

    def _org_invite_ref(self, invite_id: str):
        return self._db.collection("orgInvites").document(invite_id)

    def _org_invite_audit_ref(self, event_id: str):
        return self._db.collection("orgInviteAudits").document(event_id)

    def _billing_event_ref(self, event_id: str):
        return self._db.collection("billingEvents").document(event_id)

    def _write_invite_audit(
        self,
        *,
        org_id: str,
        invite_id: str,
        action: str,
        actor_uid: Optional[str],
        target_uid: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        transaction=None,
    ) -> None:
        payload = {
            "orgId": org_id,
            "inviteId": invite_id,
            "action": action,
            "actorUid": _clean_token(actor_uid),
            "targetUid": _clean_token(target_uid),
            "metadata": dict(metadata or {}),
            "createdAt": gcf_firestore.SERVER_TIMESTAMP,
        }
        event_ref = self._org_invite_audit_ref(uuid4().hex)
        if transaction is None:
            event_ref.set(payload, merge=True)
            return
        transaction.set(event_ref, payload, merge=True)

    def _cleanup_org_invites(self, *, org_id: str, now: Optional[datetime] = None) -> None:
        current = now or _utcnow()
        cutoff = current - timedelta(days=INVITE_RETENTION_DAYS)
        query = self._where(self._db.collection("orgInvites"), "orgId", "==", org_id)
        for invite_snap in query.stream():
            invite = invite_snap.to_dict() or {}
            invite_status = str(invite.get("status") or "")
            expires_at = invite.get("expiresAt")
            if invite_status == INVITE_STATUS_ACTIVE and isinstance(expires_at, datetime) and expires_at <= current:
                self._org_invite_ref(invite_snap.id).set(
                    {"status": INVITE_STATUS_EXPIRED, "updatedAt": gcf_firestore.SERVER_TIMESTAMP},
                    merge=True,
                )
                invite_status = INVITE_STATUS_EXPIRED
            if invite_status == INVITE_STATUS_ACTIVE:
                continue
            marker = (
                invite.get("updatedAt")
                or invite.get("consumedAt")
                or invite.get("revokedAt")
                or invite.get("createdAt")
            )
            if isinstance(marker, datetime) and marker <= cutoff:
                self._org_invite_ref(invite_snap.id).delete()

    def _count_active_org_invites(self, *, org_id: str, now: Optional[datetime] = None) -> int:
        self._cleanup_org_invites(org_id=org_id, now=now)
        query = self._where(
            self._where(self._db.collection("orgInvites"), "orgId", "==", org_id),
            "status",
            "==",
            INVITE_STATUS_ACTIVE,
        )
        return sum(1 for _ in query.stream())

    def _member_role(self, org_id: str, uid: Optional[str]) -> Optional[str]:
        clean_uid = _clean_token(uid)
        if not clean_uid:
            return None
        if _is_master_uid(clean_uid):
            return "owner"
        member_snap = self._org_ref(org_id).collection("members").document(clean_uid).get()
        if not member_snap.exists:
            return None
        member = member_snap.to_dict() or {}
        return _normalize_role(member.get("role"), fallback="viewer")

    def _roll_billing_period_if_needed(self, org_id: str, org: Dict[str, Any], *, now: datetime) -> Dict[str, Any]:
        current_key = _yyyymm(now)
        if str(org.get("currentMonthKey") or "") == current_key:
            return org
        update = {
            "currentMonthKey": current_key,
            "currentMonthMinutes": 0,
            "hardCapReached": False,
            "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
        }
        self._org_ref(org_id).set(update, merge=True)
        merged = dict(org)
        merged.update({"currentMonthKey": current_key, "currentMonthMinutes": 0, "hardCapReached": False})
        return merged

    def _resolve_org_id_by_slug(self, slug: str) -> Optional[str]:
        rows = (
            self._where(self._db.collection("organizations"), "slug", "==", (slug or "").strip().lower())
            .limit(1)
            .stream()
        )
        row = next(rows, None)
        return row.id if row else None

    def resolve_service(self, slug: str, service_key: str) -> Optional[Dict[str, Any]]:
        org_id = self._resolve_org_id_by_slug(slug)
        if not org_id:
            return None
        service_snap = self._service_ref(org_id, service_key).get()
        if not service_snap.exists:
            return None
        service = service_snap.to_dict() or {}
        active_room_id = service.get("activeRoomId")
        room_doc = None
        room_status = "waiting"
        if active_room_id:
            room_snap = self._room_ref(org_id, active_room_id).get()
            if room_snap.exists:
                room_doc = room_snap.to_dict() or {}
                room_status = str(room_doc.get("status") or "waiting")
                if room_status != "live":
                    active_room_id = None
        return {
            "orgId": org_id,
            "slug": slug,
            "serviceKey": service_key,
            "activeRoomId": active_room_id,
            "roomStatus": room_status,
            "languagePair": (room_doc or {}).get("languagePair")
            or service.get("defaultLanguagePair")
            or {"source": "ko", "target": "en"},
            "service": {
                "title": service.get("title") or service_key,
                "timezone": service.get("timezone") or "UTC",
                "rrule": service.get("rrule"),
            },
        }

    def list_services(self, slug: str) -> Optional[Dict[str, Any]]:
        org_id = self._resolve_org_id_by_slug(slug)
        if not org_id:
            return None
        org_snap = self._org_ref(org_id).get()
        org = org_snap.to_dict() if org_snap.exists else {}
        rows: List[Dict[str, Any]] = []
        for snap in self._org_ref(org_id).collection("services").stream():
            service_key = snap.id
            service = snap.to_dict() or {}
            active_room_id = service.get("activeRoomId")
            room_status = "waiting"
            if active_room_id:
                room_snap = self._room_ref(org_id, active_room_id).get()
                if room_snap.exists:
                    room_status = str((room_snap.to_dict() or {}).get("status") or "waiting")
                if room_status != "live":
                    active_room_id = None
            rows.append(
                {
                    "serviceKey": service_key,
                    "title": service.get("title") or service_key,
                    "timezone": service.get("timezone") or "UTC",
                    "activeRoomId": active_room_id,
                    "roomStatus": room_status,
                    "defaultLanguagePair": service.get("defaultLanguagePair") or {"source": "ko", "target": "en"},
                }
            )
        rows.sort(key=lambda r: r["serviceKey"])
        return {"orgId": org_id, "slug": slug, "name": (org or {}).get("name", slug), "services": rows}

    def update_org_profile(
        self,
        *,
        org_id: str,
        requested_by_uid: str,
        name: str,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        clean_name = _clean_token(name)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        if len(clean_name) < 2:
            raise ValueError("invalid_name")

        org_ref = self._org_ref(clean_org_id)
        org_snap = org_ref.get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        role = self._member_role(clean_org_id, clean_uid)
        if role not in {"owner", "admin"}:
            raise PermissionError("forbidden")

        org_ref.set(
            {
                "name": clean_name,
                "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        self._invalidate_membership_cache(clean_uid)
        org = org_snap.to_dict() or {}
        return {
            "orgId": clean_org_id,
            "slug": str(org.get("slug") or clean_org_id),
            "name": clean_name,
        }

    def check_org_slug_availability(self, *, slug: str, max_suggestions: int = 3) -> Dict[str, Any]:
        normalized = _normalize_slug(slug)
        safe_limit = max(0, min(10, int(max_suggestions)))
        available = self._resolve_org_id_by_slug(normalized) is None
        suggestions: List[str] = []
        if not available and safe_limit > 0:
            suffix = 2
            while len(suggestions) < safe_limit and suffix <= 9999:
                candidate = f"{normalized}-{suffix}"
                if self._resolve_org_id_by_slug(candidate) is None:
                    suggestions.append(candidate)
                suffix += 1
        return {"slug": normalized, "available": available, "suggestions": suggestions}

    def create_service(
        self,
        *,
        org_id: str,
        service_key: str,
        requested_by_uid: str,
        title: Optional[str] = None,
        timezone: Optional[str] = None,
        source: str = "ko",
        target: str = "en",
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        normalized_key = _normalize_service_key(service_key)
        title_token = _clean_token(title) or _default_service_title(normalized_key)
        timezone_token = _clean_token(timezone) or "America/Chicago"
        src = (_clean_token(source) or "ko").lower()
        tgt = (_clean_token(target) or "en").lower()

        org_snap = self._org_ref(clean_org_id).get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        org = org_snap.to_dict() or {}
        role = self._member_role(clean_org_id, clean_uid)
        if role not in {"owner", "admin", "host"}:
            raise PermissionError("forbidden")

        service_ref = self._service_ref(clean_org_id, normalized_key)
        if service_ref.get().exists:
            raise ValueError("service_exists")

        now = _utcnow()
        billing = _normalize_org_billing(org, now=now)
        if _billing_trial_minutes_exhausted(billing):
            raise PermissionError("trial_expired")
        if _entitlements_v2_effective(org):
            denial = _billing_start_denial_reason(billing=billing, now=now)
            if denial:
                raise PermissionError(denial)
            max_service_keys = _billing_max_service_keys(billing)
            if max_service_keys > 0:
                service_query = self._org_ref(clean_org_id).collection("services").limit(max_service_keys)
                existing_count = sum(1 for _ in service_query.stream())
                if existing_count >= max_service_keys:
                    raise PermissionError("plan_limit_reached")

        service_ref.set(
            {
                "title": title_token,
                "timezone": timezone_token,
                "rrule": None,
                "defaultLanguagePair": {"source": src, "target": tgt},
                "activeRoomId": None,
                "lastRoomId": None,
                "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return {
            "orgId": clean_org_id,
            "serviceKey": normalized_key,
            "title": title_token,
            "timezone": timezone_token,
            "activeRoomId": None,
            "roomStatus": "waiting",
            "defaultLanguagePair": {"source": src, "target": tgt},
        }

    def delete_service(self, *, org_id: str, service_key: str, requested_by_uid: str) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        normalized_key = _normalize_service_key(service_key)

        org_snap = self._org_ref(clean_org_id).get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        role = self._member_role(clean_org_id, clean_uid)
        if role not in {"owner", "admin", "host"}:
            raise PermissionError("forbidden")

        service_ref = self._service_ref(clean_org_id, normalized_key)
        service_snap = service_ref.get()
        if not service_snap.exists:
            raise ValueError("service_not_found")
        service = service_snap.to_dict() or {}
        active_room_id = _clean_token(service.get("activeRoomId"))
        if active_room_id:
            room_snap = self._room_ref(clean_org_id, active_room_id).get()
            if room_snap.exists:
                room = room_snap.to_dict() or {}
                if str(room.get("status") or "") == "live":
                    raise ValueError("service_active")
        service_ref.delete()
        return {"deleted": True, "orgId": clean_org_id, "serviceKey": normalized_key}

    def _membership_row(self, *, org_id: str, org: Dict[str, Any], role: Optional[str]) -> Dict[str, Any]:
        normalized_role = _normalize_role(role, fallback="viewer")
        lowered_role = normalized_role.strip().lower()
        return {
            "orgId": org_id,
            "slug": str(org.get("slug") or org_id),
            "name": str(org.get("name") or org_id),
            "status": str(org.get("status") or "active"),
            "role": normalized_role,
            "hostToken": str(org.get("hostToken") or "") if lowered_role in {"owner", "admin", "host"} else None,
            "billingLimitsEnabled": _org_billing_limits_flag(org),
        }

    def _list_memberships_from_collection_group(self, uid: str) -> Optional[List[Dict[str, Any]]]:
        collection_group = getattr(self._db, "collection_group", None)
        if collection_group is None:
            return None

        members_query = collection_group("members")
        field_path: Any = "__name__"
        field_path_cls = getattr(gcf_firestore, "FieldPath", None)
        if field_path_cls is not None and hasattr(field_path_cls, "document_id"):
            try:
                field_path = field_path_cls.document_id()
            except Exception:
                field_path = "__name__"

        try:
            member_snaps = list(self._where(members_query, field_path, "==", uid).stream())
        except Exception:
            try:
                member_snaps = list(members_query.where("__name__", "==", uid).stream())
            except Exception:
                return None

        rows: List[Dict[str, Any]] = []
        seen_org_ids: set[str] = set()
        for member_snap in member_snaps:
            org_ref = member_snap.reference.parent.parent
            if org_ref is None:
                continue
            org_id = _clean_token(getattr(org_ref, "id", None))
            if not org_id or org_id in seen_org_ids:
                continue
            seen_org_ids.add(org_id)
            org_snap = org_ref.get()
            if not org_snap.exists:
                continue
            org = org_snap.to_dict() or {}
            member = member_snap.to_dict() or {}
            rows.append(self._membership_row(org_id=org_id, org=org, role=member.get("role")))
        return rows

    def _list_memberships_by_full_org_scan(self, uid: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for org_snap in self._db.collection("organizations").stream():
            org_id = org_snap.id
            member_snap = self._org_ref(org_id).collection("members").document(uid).get()
            if not member_snap.exists:
                continue
            org = org_snap.to_dict() or {}
            member = member_snap.to_dict() or {}
            rows.append(self._membership_row(org_id=org_id, org=org, role=member.get("role")))
        return rows

    def list_memberships(self, uid: str) -> List[Dict[str, Any]]:
        clean_uid = _clean_token(uid)
        if not clean_uid:
            return []
        cached = self._get_cached_memberships(clean_uid)
        if cached is not None:
            return cached

        if _is_master_uid(clean_uid):
            rows = [
                self._membership_row(
                    org_id=org_snap.id,
                    org=(org_snap.to_dict() or {}),
                    role="owner",
                )
                for org_snap in self._db.collection("organizations").stream()
            ]
            rows.sort(key=lambda row: (row["orgId"], row["slug"]))
            self._set_cached_memberships(clean_uid, rows)
            return rows

        rows: List[Dict[str, Any]] = []
        indexed_memberships = list(self._user_ref(clean_uid).collection("memberships").stream())
        if indexed_memberships:
            stale_org_ids: List[str] = []
            for index_snap in indexed_memberships:
                index_doc = index_snap.to_dict() or {}
                org_id = _clean_token(index_doc.get("orgId")) or index_snap.id
                if not org_id:
                    continue
                org_ref = self._org_ref(org_id)
                member_snap = org_ref.collection("members").document(clean_uid).get()
                if not member_snap.exists:
                    stale_org_ids.append(org_id)
                    continue
                org_snap = org_ref.get()
                if not org_snap.exists:
                    stale_org_ids.append(org_id)
                    continue
                org = org_snap.to_dict() or {}
                member = member_snap.to_dict() or {}
                role = _normalize_role(member.get("role"), fallback=index_doc.get("role"))
                rows.append(self._membership_row(org_id=org_id, org=org, role=role))
                if _normalize_role(index_doc.get("role"), fallback="viewer") != role:
                    self._upsert_user_membership_index(uid=clean_uid, org_id=org_id, role=role)
            for stale_org_id in stale_org_ids:
                self._user_membership_ref(clean_uid, stale_org_id).delete()
        if not indexed_memberships or not rows:
            rows = self._list_memberships_from_collection_group(clean_uid)
            if rows is None:
                rows = self._list_memberships_by_full_org_scan(clean_uid)
            for row in rows:
                self._upsert_user_membership_index(
                    uid=clean_uid,
                    org_id=str(row.get("orgId") or ""),
                    role=row.get("role"),
                )
        rows.sort(key=lambda row: (row["orgId"], row["slug"]))
        self._set_cached_memberships(clean_uid, rows)
        return rows

    def get_current_org_id(self, uid: str) -> Optional[str]:
        clean_uid = _clean_token(uid)
        if not clean_uid:
            return None
        user_snap = self._user_ref(clean_uid).get()
        current_org_id = _clean_token((user_snap.to_dict() or {}).get("currentOrgId")) if user_snap.exists else None
        if _is_master_uid(clean_uid):
            if current_org_id and self._org_ref(current_org_id).get().exists:
                return current_org_id
            memberships = self.list_memberships(clean_uid)
            if not memberships:
                return None
            fallback_org_id = str(memberships[0]["orgId"])
            self._user_ref(clean_uid).set(
                {"currentOrgId": fallback_org_id, "updatedAt": gcf_firestore.SERVER_TIMESTAMP},
                merge=True,
            )
            return fallback_org_id
        if current_org_id:
            member_snap = self._org_ref(current_org_id).collection("members").document(clean_uid).get()
            if member_snap.exists:
                return current_org_id
        memberships = self.list_memberships(clean_uid)
        if not memberships:
            return None
        fallback_org_id = str(memberships[0]["orgId"])
        self._user_ref(clean_uid).set(
            {"currentOrgId": fallback_org_id, "updatedAt": gcf_firestore.SERVER_TIMESTAMP},
            merge=True,
        )
        return fallback_org_id

    def set_current_org(self, uid: str, org_id: str) -> str:
        clean_uid = _clean_token(uid)
        clean_org_id = _clean_token(org_id)
        if not clean_uid:
            raise ValueError("invalid_uid")
        if not clean_org_id:
            raise ValueError("org_not_found")
        org_snap = self._org_ref(clean_org_id).get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        if not _is_master_uid(clean_uid):
            member_snap = self._org_ref(clean_org_id).collection("members").document(clean_uid).get()
            if not member_snap.exists:
                raise PermissionError("org_access_denied")
        self._user_ref(clean_uid).set(
            {"currentOrgId": clean_org_id, "updatedAt": gcf_firestore.SERVER_TIMESTAMP},
            merge=True,
        )
        return clean_org_id

    def is_master_user(self, uid: str) -> bool:
        return _is_master_uid(uid)

    def get_org_billing_profile(self, *, org_id: str) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        if not clean_org_id:
            raise ValueError("org_not_found")
        now = _utcnow()
        org_ref = self._org_ref(clean_org_id)
        org_snap = org_ref.get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        org = org_snap.to_dict() or {}
        billing_payload, lifted_legacy_fields = _billing_payload_from_org_row(org)
        billing = _normalize_billing_state(
            billing_payload,
            plan_key=str(org.get("plan") or "trial"),
            now=now,
        )
        if lifted_legacy_fields or billing != billing_payload:
            org_ref.set(
                {
                    "billing": billing,
                    "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
        return dict(billing)

    def set_org_billing_profile(self, *, org_id: str, billing: Dict[str, Any]) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        if not clean_org_id:
            raise ValueError("org_not_found")
        now = _utcnow()
        org_ref = self._org_ref(clean_org_id)
        org_snap = org_ref.get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        org = org_snap.to_dict() or {}
        merged = _normalize_billing_state(
            billing or {},
            plan_key=str(org.get("plan") or "trial"),
            now=now,
        )
        normalized_status = str(merged.get("status") or "").strip().lower()
        org_ref.set(
            {
                "billing": merged,
                "plan": str(merged.get("planKey") or org.get("plan") or "trial"),
                "status": "trial" if normalized_status == "trialing" else (normalized_status or str(org.get("status") or "active")),
                "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return dict(merged)

    def get_org_effective_trial_seconds_remaining(self, *, org_id: str) -> Optional[int]:
        clean_org_id = _clean_token(org_id)
        if not clean_org_id:
            raise ValueError("org_not_found")
        now = _utcnow()
        org_ref = self._org_ref(clean_org_id)
        org_snap = org_ref.get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        org = org_snap.to_dict() or {}
        billing_payload, lifted_legacy_fields = _billing_payload_from_org_row(org)
        billing = _normalize_billing_state(
            billing_payload,
            plan_key=str(org.get("plan") or "trial"),
            now=now,
        )
        if lifted_legacy_fields or billing != billing_payload:
            org_ref.set(
                {
                    "billing": billing,
                    "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
        live_rooms = [
            room_snap.to_dict() or {}
            for room_snap in self._where(self._org_ref(clean_org_id).collection("rooms"), "status", "==", "live").stream()
        ]
        return _effective_trial_seconds_remaining_for_rooms(billing, live_rooms, now=now)

    def ensure_org_can_start_service(self, *, org_id: str) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        if not clean_org_id:
            raise ValueError("org_not_found")
        now = _utcnow()
        org_ref = self._org_ref(clean_org_id)
        org_snap = org_ref.get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        org = self._roll_billing_period_if_needed(clean_org_id, org_snap.to_dict() or {}, now=now)
        org_status = _status_token(org.get("status"))
        if org_status in _HARD_INACTIVE_ORG_STATUSES:
            raise PermissionError("org_inactive")
        if _org_billing_limits_enabled(org) and bool(org.get("hardCapReached")):
            raise PermissionError("hard_cap_reached")
        billing = _normalize_org_billing(org, now=now)
        if _billing_trial_minutes_exhausted(billing):
            raise PermissionError("trial_expired")
        if _entitlements_v2_effective(org):
            denial = _billing_start_denial_reason(billing=billing, now=now)
            if denial:
                raise PermissionError(denial)
        elif not _legacy_org_start_allowed(org):
            raise PermissionError("org_inactive")
        return {
            "orgId": clean_org_id,
            "allowed": True,
            "billing": dict(billing),
            "entitlementsV2": _entitlements_v2_effective(org),
        }

    def find_org_id_by_billing_refs(
        self,
        *,
        stripe_customer_id: Optional[str] = None,
        stripe_subscription_id: Optional[str] = None,
    ) -> Optional[str]:
        clean_customer = _clean_token(stripe_customer_id)
        clean_subscription = _clean_token(stripe_subscription_id)
        if clean_subscription:
            query = self._where(self._db.collection("organizations"), "billing.stripeSubscriptionId", "==", clean_subscription).limit(1)
            snap = next(query.stream(), None)
            if snap is not None:
                return snap.id
        if clean_customer:
            query = self._where(self._db.collection("organizations"), "billing.stripeCustomerId", "==", clean_customer).limit(1)
            snap = next(query.stream(), None)
            if snap is not None:
                return snap.id
        return None

    def try_mark_billing_event(
        self,
        *,
        event_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        clean_event_id = _clean_token(event_id)
        if not clean_event_id:
            raise ValueError("invalid_event_id")
        ref = self._billing_event_ref(clean_event_id)
        snap = ref.get()
        if snap.exists:
            return False
        ref.set(
            {
                "eventId": clean_event_id,
                "eventType": str(event_type or "").strip(),
                "payload": dict(payload or {}),
                "createdAt": gcf_firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return True

    def get_org_billing_limits(
        self,
        *,
        org_id: str,
        requested_by_uid: str,
        requested_by_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        clean_email = _clean_token(requested_by_email)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")

        org_snap = self._org_ref(clean_org_id).get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        if not _is_billing_admin(clean_uid, email=clean_email):
            raise PermissionError("billing_admin_required")

        org = org_snap.to_dict() or {}
        return _billing_limits_payload(org_id=clean_org_id, org=org)

    def set_org_billing_limits(
        self,
        *,
        org_id: str,
        requested_by_uid: str,
        enabled: bool,
        requested_by_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        clean_email = _clean_token(requested_by_email)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")

        org_ref = self._org_ref(clean_org_id)
        org_snap = org_ref.get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        if not _is_billing_admin(clean_uid, email=clean_email):
            raise PermissionError("billing_admin_required")

        org_ref.set(
            {
                "billingLimitsEnabled": bool(enabled),
                "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        merged_org = org_snap.to_dict() or {}
        merged_org["billingLimitsEnabled"] = bool(enabled)
        merged_org["updatedAt"] = _utcnow()
        return _billing_limits_payload(org_id=clean_org_id, org=merged_org)

    def ensure_sermon_prep_budget_not_reached(self, *, org_id: str) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        if not clean_org_id:
            raise ValueError("org_not_found")
        org_snap = self._org_ref(clean_org_id).get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        now = _utcnow()
        period_key = _yyyymm(now)
        usage_snap = self._usage_ref(clean_org_id, period_key).get()
        payload = _sermon_usage_payload(
            org_id=clean_org_id,
            org=(org_snap.to_dict() or {}),
            period_key=period_key,
            usage_month=(usage_snap.to_dict() or {}) if usage_snap.exists else {},
            sermon_rows=[],
        )
        if bool(payload.get("capReached")):
            raise PermissionError("sermon_prep_budget_reached")
        payload["sermons"] = []
        return payload

    def get_org_sermon_prep_usage(
        self,
        *,
        org_id: str,
        requested_by_uid: str,
        period_key: Optional[str] = None,
        sermon_id: Optional[str] = None,
        limit: int = 25,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        clean_sermon_id = _clean_token(sermon_id)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        safe_limit = max(1, min(200, int(limit or 25)))
        resolved_period = _normalize_period_key(period_key, now=_utcnow())

        org_snap = self._org_ref(clean_org_id).get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        role = self._member_role(clean_org_id, clean_uid)
        if role not in {"owner", "admin", "host"}:
            raise PermissionError("forbidden")

        usage_ref = self._usage_ref(clean_org_id, resolved_period)
        usage_snap = usage_ref.get()
        sermon_rows: List[Dict[str, Any]] = []
        for sermon_snap in usage_ref.collection("sermons").stream():
            row = sermon_snap.to_dict() or {}
            row_sermon_id = _clean_token(row.get("sermonId"))
            if clean_sermon_id and row_sermon_id != clean_sermon_id:
                continue
            sermon_rows.append(row)
        payload = _sermon_usage_payload(
            org_id=clean_org_id,
            org=(org_snap.to_dict() or {}),
            period_key=resolved_period,
            usage_month=(usage_snap.to_dict() or {}) if usage_snap.exists else {},
            sermon_rows=sermon_rows,
        )
        payload["sermons"] = (payload.get("sermons") or [])[:safe_limit]
        return payload

    def set_org_sermon_prep_budget(
        self,
        *,
        org_id: str,
        requested_by_uid: str,
        budget_usd: float,
        requested_by_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        clean_email = _clean_token(requested_by_email)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        parsed_budget = _safe_float(budget_usd, -1.0)
        if parsed_budget < 0:
            raise ValueError("invalid_budget")

        org_ref = self._org_ref(clean_org_id)
        org_snap = org_ref.get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        if not _is_billing_admin(clean_uid, email=clean_email):
            raise PermissionError("billing_admin_required")

        org_ref.set(
            {
                "sermonPrepBudgetUsd": float(parsed_budget),
                "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return self.get_org_sermon_prep_usage(
            org_id=clean_org_id,
            requested_by_uid=clean_uid,
            limit=25,
        )

    def record_sermon_prep_usage(
        self,
        *,
        org_id: str,
        sermon_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        estimated_usd: float,
        requests_count: int = 1,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_sermon_id = _clean_token(sermon_id)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_sermon_id:
            raise ValueError("invalid_sermon_id")
        prompt = _int_tokens(prompt_tokens)
        completion = _int_tokens(completion_tokens)
        total = _int_tokens(total_tokens) or (prompt + completion)
        calls = max(0, _int_tokens(requests_count) or 0)
        usd = _round_usd(estimated_usd)
        if usd <= 0.0 and (prompt > 0 or completion > 0):
            usd = _round_usd(_estimate_sermon_prep_usd(prompt_tokens=prompt, completion_tokens=completion))
        model_name = str(model or "").strip()

        org_snap = self._org_ref(clean_org_id).get()
        if not org_snap.exists:
            raise ValueError("org_not_found")

        now = _utcnow()
        period_key = _yyyymm(now)
        usage_ref = self._usage_ref(clean_org_id, period_key)
        usage_ref.set(
            {
                "sermonPromptTokens": gcf_firestore.Increment(prompt),
                "sermonCompletionTokens": gcf_firestore.Increment(completion),
                "sermonTotalTokens": gcf_firestore.Increment(total),
                "sermonEstimatedUsd": gcf_firestore.Increment(float(usd)),
                "sermonRequestsCount": gcf_firestore.Increment(calls),
                "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )

        sermon_ref = self._usage_sermon_ref(clean_org_id, period_key, clean_sermon_id)
        sermon_payload: Dict[str, Any] = {
            "sermonId": clean_sermon_id,
            "promptTokens": gcf_firestore.Increment(prompt),
            "completionTokens": gcf_firestore.Increment(completion),
            "totalTokens": gcf_firestore.Increment(total),
            "estimatedUsd": gcf_firestore.Increment(float(usd)),
            "requestsCount": gcf_firestore.Increment(calls),
            "lastUsedAt": gcf_firestore.SERVER_TIMESTAMP,
            "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
        }
        if model_name:
            sermon_payload["lastModel"] = model_name
        sermon_ref.set(sermon_payload, merge=True)

        usage_snap = usage_ref.get()
        sermon_rows = [(snap.to_dict() or {}) for snap in usage_ref.collection("sermons").stream()]
        payload = _sermon_usage_payload(
            org_id=clean_org_id,
            org=(org_snap.to_dict() or {}),
            period_key=period_key,
            usage_month=(usage_snap.to_dict() or {}) if usage_snap.exists else {},
            sermon_rows=sermon_rows,
        )
        payload["sermons"] = (payload.get("sermons") or [])[:25]
        return payload

    def create_invite(
        self,
        *,
        org_id: str,
        created_by_uid: str,
        role: str,
        expires_in_hours: int = DEFAULT_INVITE_EXPIRY_HOURS,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_creator_uid = _clean_token(created_by_uid)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_creator_uid:
            raise ValueError("invalid_uid")
        invite_role = _normalize_invite_role(role)

        org_snap = self._org_ref(clean_org_id).get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        creator_role = self._member_role(clean_org_id, clean_creator_uid)
        if creator_role not in {"owner", "admin"}:
            raise PermissionError("forbidden")

        org = org_snap.to_dict() or {}
        now = _utcnow()
        active_count = self._count_active_org_invites(org_id=clean_org_id, now=now)
        if active_count >= MAX_ACTIVE_INVITES_PER_ORG:
            raise ValueError("invite_active_limit_reached")
        expires_at = _invite_expiry(expires_in_hours)
        code = _new_invite_code()
        invite_id = _invite_code_hash(code)
        invite_ref = self._org_invite_ref(invite_id)
        while invite_ref.get().exists:
            code = _new_invite_code()
            invite_id = _invite_code_hash(code)
            invite_ref = self._org_invite_ref(invite_id)

        invite_ref.set(
            {
                "inviteId": invite_id,
                "orgId": clean_org_id,
                "slug": str(org.get("slug") or clean_org_id),
                "name": str(org.get("name") or clean_org_id),
                "role": invite_role,
                "status": INVITE_STATUS_ACTIVE,
                "expiresAt": expires_at,
                "createdBy": clean_creator_uid,
                "createdAt": now,
                "consumedBy": None,
                "consumedAt": None,
                "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
            }
        )
        self._write_invite_audit(
            org_id=clean_org_id,
            invite_id=invite_id,
            action="created",
            actor_uid=clean_creator_uid,
            metadata={"role": invite_role, "expiresHours": int(expires_in_hours)},
        )
        return {
            "inviteId": invite_id,
            "code": code,
            "orgId": clean_org_id,
            "slug": str(org.get("slug") or clean_org_id),
            "name": str(org.get("name") or clean_org_id),
            "role": invite_role,
            "status": INVITE_STATUS_ACTIVE,
            "expiresAt": expires_at,
            "createdAt": now,
        }

    def preview_invite(self, *, code: str, uid: str) -> Dict[str, Any]:
        clean_uid = _clean_token(uid)
        clean_code = _clean_token(code)
        if not clean_uid:
            raise ValueError("invalid_uid")
        if not clean_code:
            raise ValueError("invite_not_found")

        invite_ref = self._org_invite_ref(_invite_code_hash(clean_code))
        invite_snap = invite_ref.get()
        if not invite_snap.exists:
            raise ValueError("invite_not_found")
        invite = invite_snap.to_dict() or {}
        status = str(invite.get("status") or "")
        if status != INVITE_STATUS_ACTIVE:
            raise ValueError("invite_invalid")
        now = _utcnow()
        expires_at = invite.get("expiresAt")
        if isinstance(expires_at, datetime) and expires_at <= now:
            invite_ref.set({"status": INVITE_STATUS_EXPIRED, "updatedAt": gcf_firestore.SERVER_TIMESTAMP}, merge=True)
            raise ValueError("invite_expired")

        org_id = _clean_token(invite.get("orgId"))
        if not org_id:
            raise ValueError("org_not_found")
        org_snap = self._org_ref(org_id).get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        org = org_snap.to_dict() or {}
        member_snap = self._org_ref(org_id).collection("members").document(clean_uid).get()
        return {
            "inviteId": str(invite.get("inviteId") or invite_snap.id),
            "orgId": org_id,
            "slug": str(org.get("slug") or org_id),
            "name": str(org.get("name") or org_id),
            "role": _normalize_role(invite.get("role"), fallback="viewer"),
            "status": status,
            "expiresAt": expires_at,
            "alreadyMember": member_snap.exists,
        }

    def redeem_invite(
        self,
        *,
        code: str,
        uid: str,
        email: Optional[str],
        display_name: Optional[str],
    ) -> Dict[str, Any]:
        clean_uid = _clean_token(uid)
        clean_code = _clean_token(code)
        if not clean_uid:
            raise ValueError("invalid_uid")
        if not clean_code:
            raise ValueError("invite_not_found")

        clean_email = _clean_token(email)
        clean_display_name = _clean_token(display_name)
        invite_ref = self._org_invite_ref(_invite_code_hash(clean_code))
        now = _utcnow()
        db = self._db

        @gcf_firestore.transactional
        def _tx(transaction):
            invite_snap = invite_ref.get(transaction=transaction)
            if not invite_snap.exists:
                raise ValueError("invite_not_found")
            invite = invite_snap.to_dict() or {}
            status = str(invite.get("status") or "")
            if status != INVITE_STATUS_ACTIVE:
                raise ValueError("invite_invalid")

            expires_at = invite.get("expiresAt")
            if isinstance(expires_at, datetime) and expires_at <= now:
                transaction.set(
                    invite_ref,
                    {"status": INVITE_STATUS_EXPIRED, "updatedAt": gcf_firestore.SERVER_TIMESTAMP},
                    merge=True,
                )
                raise ValueError("invite_expired")

            org_id = _clean_token(invite.get("orgId"))
            if not org_id:
                raise ValueError("org_not_found")
            org_ref = self._org_ref(org_id)
            org_snap = org_ref.get(transaction=transaction)
            if not org_snap.exists:
                raise ValueError("org_not_found")
            org = org_snap.to_dict() or {}

            invite_role = _normalize_invite_role(invite.get("role"))
            member_ref = org_ref.collection("members").document(clean_uid)
            member_snap = member_ref.get(transaction=transaction)
            created = False
            member_role = invite_role
            if member_snap.exists:
                member = member_snap.to_dict() or {}
                member_role = _normalize_role(member.get("role"), fallback="viewer")
                merge_payload: Dict[str, Any] = {"updatedAt": gcf_firestore.SERVER_TIMESTAMP}
                if clean_email and not _clean_token(member.get("email")):
                    merge_payload["email"] = clean_email
                if clean_display_name and not _clean_token(member.get("displayName")):
                    merge_payload["displayName"] = clean_display_name
                transaction.set(member_ref, merge_payload, merge=True)
            else:
                created = True
                transaction.set(
                    member_ref,
                    {
                        "role": member_role,
                        "email": clean_email,
                        "displayName": clean_display_name,
                        "createdAt": gcf_firestore.SERVER_TIMESTAMP,
                        "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
                    },
                    merge=True,
                )

            user_payload: Dict[str, Any] = {
                "currentOrgId": org_id,
                "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
            }
            if clean_email:
                user_payload["email"] = clean_email
            if clean_display_name:
                user_payload["displayName"] = clean_display_name
            transaction.set(self._user_ref(clean_uid), user_payload, merge=True)
            self._upsert_user_membership_index(
                uid=clean_uid,
                org_id=org_id,
                role=member_role,
                transaction=transaction,
            )

            transaction.set(
                invite_ref,
                {
                    "status": INVITE_STATUS_CONSUMED,
                    "consumedBy": clean_uid,
                    "consumedAt": gcf_firestore.SERVER_TIMESTAMP,
                    "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
            self._write_invite_audit(
                org_id=org_id,
                invite_id=str(invite.get("inviteId") or invite_snap.id),
                action="redeemed",
                actor_uid=clean_uid,
                target_uid=clean_uid,
                metadata={"createdMembership": created},
                transaction=transaction,
            )

            normalized_member_role = _normalize_role(member_role, fallback="viewer")
            return {
                "orgId": org_id,
                "slug": str(org.get("slug") or org_id),
                "name": str(org.get("name") or org_id),
                "role": normalized_member_role,
                "created": created,
                "alreadyMember": not created,
                "currentOrgId": org_id,
                "hostToken": str(org.get("hostToken") or "") if normalized_member_role in {"owner", "admin", "host"} else None,
            }

        tx = db.transaction()
        redeemed = _tx(tx)
        self._invalidate_membership_cache(clean_uid)
        return redeemed

    def list_invites(
        self,
        *,
        org_id: str,
        requested_by_uid: str,
        status: Optional[str] = INVITE_STATUS_ACTIVE,
    ) -> List[Dict[str, Any]]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        status_filter = _normalize_invite_status(status)

        org_snap = self._org_ref(clean_org_id).get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        org = org_snap.to_dict() or {}
        role = self._member_role(clean_org_id, clean_uid)
        if role not in {"owner", "admin"}:
            raise PermissionError("forbidden")

        now = _utcnow()
        self._cleanup_org_invites(org_id=clean_org_id, now=now)
        rows: List[Dict[str, Any]] = []
        query = self._where(self._db.collection("orgInvites"), "orgId", "==", clean_org_id)
        for invite_snap in query.stream():
            invite = invite_snap.to_dict() or {}
            invite_status = str(invite.get("status") or "")
            expires_at = invite.get("expiresAt")
            if invite_status == INVITE_STATUS_ACTIVE and isinstance(expires_at, datetime) and expires_at <= now:
                invite_status = INVITE_STATUS_EXPIRED
                self._org_invite_ref(invite_snap.id).set(
                    {"status": INVITE_STATUS_EXPIRED, "updatedAt": gcf_firestore.SERVER_TIMESTAMP},
                    merge=True,
                )
                invite["status"] = INVITE_STATUS_EXPIRED
            if status_filter and invite_status != status_filter:
                continue
            rows.append(_serialize_invite(invite, fallback_org=org))

        rows.sort(key=lambda row: row.get("createdAt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rows

    def revoke_invite(
        self,
        *,
        org_id: str,
        invite_id: str,
        revoked_by_uid: str,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_invite_id = _clean_token(invite_id)
        clean_uid = _clean_token(revoked_by_uid)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_invite_id:
            raise ValueError("invite_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")

        org_snap = self._org_ref(clean_org_id).get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        org = org_snap.to_dict() or {}
        role = self._member_role(clean_org_id, clean_uid)
        if role not in {"owner", "admin"}:
            raise PermissionError("forbidden")

        invite_ref = self._org_invite_ref(clean_invite_id)
        now = _utcnow()
        db = self._db

        @gcf_firestore.transactional
        def _tx(transaction):
            invite_snap = invite_ref.get(transaction=transaction)
            if not invite_snap.exists:
                raise ValueError("invite_not_found")
            invite = invite_snap.to_dict() or {}
            if _clean_token(invite.get("orgId")) != clean_org_id:
                raise ValueError("invite_not_found")
            invite_status = str(invite.get("status") or "")
            expires_at = invite.get("expiresAt")
            if invite_status == INVITE_STATUS_ACTIVE and isinstance(expires_at, datetime) and expires_at <= now:
                transaction.set(
                    invite_ref,
                    {"status": INVITE_STATUS_EXPIRED, "updatedAt": gcf_firestore.SERVER_TIMESTAMP},
                    merge=True,
                )
                raise ValueError("invite_expired")
            if invite_status != INVITE_STATUS_ACTIVE:
                raise ValueError("invite_invalid")

            transaction.set(
                invite_ref,
                {
                    "status": INVITE_STATUS_REVOKED,
                    "revokedBy": clean_uid,
                    "revokedAt": gcf_firestore.SERVER_TIMESTAMP,
                    "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
            self._write_invite_audit(
                org_id=clean_org_id,
                invite_id=str(invite.get("inviteId") or invite_snap.id),
                action="revoked",
                actor_uid=clean_uid,
                transaction=transaction,
            )
            merged = dict(invite)
            merged["status"] = INVITE_STATUS_REVOKED
            merged["revokedBy"] = clean_uid
            merged["revokedAt"] = now
            return _serialize_invite(merged, fallback_org=org)

        tx = db.transaction()
        return _tx(tx)

    def bootstrap_owner_org(
        self,
        *,
        owner_uid: str,
        owner_email: Optional[str],
        owner_display_name: Optional[str],
        church_name: str,
        church_slug: str,
        timezone: str,
        source: str,
        target: str,
    ) -> Dict[str, Any]:
        clean_uid = _clean_token(owner_uid)
        clean_name = _clean_token(church_name)
        if not clean_uid:
            raise ValueError("invalid_uid")
        if not clean_name:
            raise ValueError("invalid_name")

        slug = _normalize_slug(church_slug)
        tz = _clean_token(timezone) or "America/Chicago"
        src = (_clean_token(source) or "ko").lower()
        tgt = (_clean_token(target) or "en").lower()

        existing_memberships = self.list_memberships(clean_uid)
        if existing_memberships:
            org = existing_memberships[0]
            self._user_ref(clean_uid).set(
                {
                    "email": _clean_token(owner_email),
                    "displayName": _clean_token(owner_display_name),
                    "currentOrgId": org["orgId"],
                    "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
            return {
                "created": False,
                "orgId": org["orgId"],
                "slug": org["slug"],
                "name": org["name"],
                "role": org["role"],
                "hostToken": None,
                "services": [],
            }

        existing_org_id = self._resolve_org_id_by_slug(slug)
        if existing_org_id:
            raise ValueError("slug_taken")

        org_id = slug
        if self._org_ref(org_id).get().exists:
            suffix = 1
            while self._org_ref(f"{slug}-{suffix}").get().exists:
                suffix += 1
            org_id = f"{slug}-{suffix}"

        now = _utcnow()
        host_token = secrets.token_urlsafe(24)
        org_ref = self._org_ref(org_id)
        org_ref.set(
            {
                "slug": slug,
                "name": clean_name,
                "plan": "trial",
                "status": "active",
                "billing": _default_billing_state(now=now, plan_key="trial"),
                "maxMinutesPerMonth": 500,
                "currentMonthMinutes": 0,
                "currentMonthKey": _yyyymm(now),
                "maxConcurrentRooms": 1,
                "billingLimitsEnabled": True,
                "hardCapReached": False,
                "sermonPrepBudgetUsd": float(SERMON_PREP_DEFAULT_BUDGET_USD),
                "hostToken": host_token,
                "customPrompt": "",
                "servicePrompt": "",
                "createdAt": gcf_firestore.SERVER_TIMESTAMP,
                "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        org_ref.collection("members").document(clean_uid).set(
            {
                "role": "owner",
                "email": _clean_token(owner_email),
                "displayName": _clean_token(owner_display_name),
                "createdAt": gcf_firestore.SERVER_TIMESTAMP,
                "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        self._db.collection("users").document(clean_uid).set(
            {
                "email": _clean_token(owner_email),
                "displayName": _clean_token(owner_display_name),
                "currentOrgId": org_id,
                "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        self._upsert_user_membership_index(uid=clean_uid, org_id=org_id, role="owner")
        self._invalidate_membership_cache(clean_uid)

        service_rows: List[Dict[str, str]] = []
        for service_key, title in DEFAULT_SERVICE_SEEDS:
            org_ref.collection("services").document(service_key).set(
                {
                    "title": title,
                    "timezone": tz,
                    "rrule": None,
                    "defaultLanguagePair": {"source": src, "target": tgt},
                    "activeRoomId": None,
                    "lastRoomId": None,
                    "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
            service_rows.append({"serviceKey": service_key, "title": title})

        return {
            "created": True,
            "orgId": org_id,
            "slug": slug,
            "name": clean_name,
            "role": "owner",
            "hostToken": host_token,
            "services": service_rows,
        }

    def get_org_prompt(self, *, org_id: str, requested_by_uid: str) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        org_snap = self._org_ref(clean_org_id).get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        role = self._member_role(clean_org_id, clean_uid)
        if role not in PROMPT_EDITOR_ROLES:
            raise PermissionError("forbidden")
        org = org_snap.to_dict() or {}
        return {
            "orgId": clean_org_id,
            "prompt": str(org.get("customPrompt") or ""),
            "service_prompt": str(org.get("servicePrompt") or ""),
            "updatedAt": org.get("updatedAt"),
        }

    def set_org_prompt(
        self,
        *,
        org_id: str,
        requested_by_uid: str,
        prompt: str,
        service_prompt: str,
    ) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        clean_uid = _clean_token(requested_by_uid)
        if not clean_org_id:
            raise ValueError("org_not_found")
        if not clean_uid:
            raise ValueError("invalid_uid")
        org_ref = self._org_ref(clean_org_id)
        org_snap = org_ref.get()
        if not org_snap.exists:
            raise ValueError("org_not_found")
        role = self._member_role(clean_org_id, clean_uid)
        if role not in PROMPT_EDITOR_ROLES:
            raise PermissionError("forbidden")
        org_ref.set(
            {
                "customPrompt": str(prompt or ""),
                "servicePrompt": str(service_prompt or ""),
                "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        self._invalidate_org_prompt_cache(clean_org_id)
        return {
            "orgId": clean_org_id,
            "prompt": str(prompt or ""),
            "service_prompt": str(service_prompt or ""),
            "updatedAt": _utcnow(),
        }

    def get_org_prompt_for_translation(self, org_id: Optional[str]) -> Dict[str, Any]:
        clean_org_id = _clean_token(org_id)
        if not clean_org_id:
            return {"orgId": "", "prompt": "", "service_prompt": ""}
        cached = self._get_cached_org_prompt(clean_org_id)
        if cached is not None:
            return cached
        org_snap = self._org_ref(clean_org_id).get()
        if not org_snap.exists:
            payload = {"orgId": clean_org_id, "prompt": "", "service_prompt": ""}
            self._set_cached_org_prompt(clean_org_id, payload)
            return payload
        org = org_snap.to_dict() or {}
        payload = {
            "orgId": clean_org_id,
            "prompt": str(org.get("customPrompt") or ""),
            "service_prompt": str(org.get("servicePrompt") or ""),
        }
        self._set_cached_org_prompt(clean_org_id, payload)
        return payload

    def authorize_host(self, org_id: str, *, host_uid: Optional[str] = None, host_token: Optional[str] = None) -> bool:
        org_snap = self._org_ref(org_id).get()
        if not org_snap.exists:
            return False
        org = org_snap.to_dict() or {}

        uid = _clean_token(host_uid)
        if uid:
            if _is_master_uid(uid):
                return True
            member_snap = self._org_ref(org_id).collection("members").document(uid).get()
            if member_snap.exists:
                role = str((member_snap.to_dict() or {}).get("role") or "").strip().lower()
                if role in {"owner", "admin", "host"}:
                    return True

        org_token = _clean_token(org.get("hostToken"))
        global_token = _clean_token(self._global_host_token)
        if org_token or global_token:
            provided = _clean_token(host_token)
            if not provided:
                return False
            if org_token and compare_digest(org_token, provided):
                return True
            if global_token and compare_digest(global_token, provided):
                return True
            return False
        return True

    def start_service(
        self,
        org_id: str,
        service_key: str,
        *,
        host_uid: Optional[str],
        source: str,
        target: str,
    ) -> Dict[str, Any]:
        db = self._db
        org_ref = self._org_ref(org_id)
        service_ref = self._service_ref(org_id, service_key)
        room_id = _new_room_id()
        room_ref = self._room_ref(org_id, room_id)
        now = _utcnow()

        @gcf_firestore.transactional
        def _tx(transaction):
            org_snap = org_ref.get(transaction=transaction)
            if not org_snap.exists:
                raise ValueError("org_not_found")
            org = org_snap.to_dict() or {}
            current_key = _yyyymm(now)
            if str(org.get("currentMonthKey") or "") != current_key:
                org["currentMonthKey"] = current_key
                org["currentMonthMinutes"] = 0
                org["hardCapReached"] = False
                transaction.set(
                    org_ref,
                    {
                        "currentMonthKey": current_key,
                        "currentMonthMinutes": 0,
                        "hardCapReached": False,
                        "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
                    },
                    merge=True,
                )
            org_status = _status_token(org.get("status"))
            if org_status in _HARD_INACTIVE_ORG_STATUSES:
                raise PermissionError("org_inactive")
            if _org_billing_limits_enabled(org) and bool(org.get("hardCapReached")):
                raise PermissionError("hard_cap_reached")
            billing = _normalize_org_billing(org, now=now)
            if _billing_trial_minutes_exhausted(billing):
                raise PermissionError("trial_expired")
            if _entitlements_v2_effective(org):
                denial = _billing_start_denial_reason(billing=billing, now=now)
                if denial:
                    raise PermissionError(denial)
            elif not _legacy_org_start_allowed(org):
                raise PermissionError("org_inactive")

            service_snap = service_ref.get(transaction=transaction)
            if service_snap.exists:
                service = service_snap.to_dict() or {}
            else:
                raise ValueError("service_not_found")

            existing_room_id = service.get("activeRoomId")
            if existing_room_id:
                existing_room_ref = self._room_ref(org_id, str(existing_room_id))
                existing_room_snap = existing_room_ref.get(transaction=transaction)
                if existing_room_snap.exists:
                    existing_room = existing_room_snap.to_dict() or {}
                    if existing_room.get("status") == "live":
                        return {
                            "orgId": org_id,
                            "serviceKey": service_key,
                            "roomId": str(existing_room_id),
                            "status": "live",
                            "languagePair": existing_room.get("languagePair") or {"source": source, "target": target},
                        }

            max_concurrent = int(org.get("maxConcurrentRooms") or 0)
            if max_concurrent > 0:
                live_rooms = list(self._where(self._org_ref(org_id).collection("rooms"), "status", "==", "live").stream(transaction=transaction))
                if len(live_rooms) >= max_concurrent:
                    raise PermissionError("concurrency_limit_reached")

            transaction.set(
                room_ref,
                {
                    "serviceKey": service_key,
                    "status": "live",
                    "startedAt": gcf_firestore.SERVER_TIMESTAMP,
                    "endedAt": None,
                    "hostUid": host_uid or "unknown",
                    "languagePair": {"source": source, "target": target},
                    "listenerCountPeak": 0,
                    "billingPeriodKey": _yyyymm(now),
                    "endReason": None,
                    "lastAudioAt": gcf_firestore.SERVER_TIMESTAMP,
                    "lastUsageTickAt": gcf_firestore.SERVER_TIMESTAMP,
                },
            )
            transaction.set(
                service_ref,
                {"activeRoomId": room_id, "updatedAt": gcf_firestore.SERVER_TIMESTAMP},
                merge=True,
            )
            return {
                "orgId": org_id,
                "serviceKey": service_key,
                "roomId": room_id,
                "status": "live",
                "languagePair": {"source": source, "target": target},
            }

        tx = db.transaction()
        return _tx(tx)

    def end_room(
        self,
        org_id: str,
        room_id: str,
        *,
        reason: str,
        transcript: Optional[str] = None,
    ) -> Dict[str, Any]:
        db = self._db
        room_ref = self._room_ref(org_id, room_id)
        now = _utcnow()

        @gcf_firestore.transactional
        def _tx(transaction):
            room_snap = room_ref.get(transaction=transaction)
            if not room_snap.exists:
                raise ValueError("room_not_found")
            room = room_snap.to_dict() or {}
            if room.get("status") == "ended":
                return {"orgId": org_id, "roomId": room_id, "status": "ended", "alreadyEnded": True}

            service_key = str(room.get("serviceKey") or "")
            service_ref = self._service_ref(org_id, service_key)
            service_snap = service_ref.get(transaction=transaction)
            service = service_snap.to_dict() if service_snap.exists else {}
            org_ref = self._org_ref(org_id)
            org_snap = org_ref.get(transaction=transaction)
            org = org_snap.to_dict() or {}
            if org_snap.exists:
                org = self._roll_billing_period_if_needed(org_id, org, now=now)
            billing = _normalize_org_billing(org, now=now) if org_snap.exists else {}
            billing_limits_enabled = _org_billing_limits_enabled(org) if org_snap.exists else False
            has_monthly_cap = billing_limits_enabled and int(org.get("maxMinutesPerMonth") or 0) > 0
            has_trial_cap = _billing_trial_seconds_limit(billing) > 0 if org_snap.exists else False
            remainder_seconds = _room_unbilled_elapsed_seconds(room, now=now)
            translated_delta_minutes = 0
            if remainder_seconds > 0 and (has_monthly_cap or has_trial_cap):
                translated_delta_minutes = _ceil_minutes_from_seconds(remainder_seconds)

            started_at = room.get("startedAt")
            if isinstance(started_at, datetime):
                duration_min = max(1, int(math.ceil((now - started_at).total_seconds() / 60)))
            else:
                duration_min = 1

            transaction.set(
                room_ref,
                {
                    "status": "ended",
                    "endedAt": gcf_firestore.SERVER_TIMESTAMP,
                    "endReason": reason or "host_end",
                },
                merge=True,
            )
            if service and service.get("activeRoomId") == room_id:
                transaction.set(
                    service_ref,
                    {
                        "activeRoomId": None,
                        "lastRoomId": room_id,
                        "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
                    },
                    merge=True,
                )
            if org_snap.exists and translated_delta_minutes > 0:
                org_update: Dict[str, Any] = {"updatedAt": gcf_firestore.SERVER_TIMESTAMP}
                if has_monthly_cap:
                    next_month_minutes = int(org.get("currentMonthMinutes") or 0) + translated_delta_minutes
                    org_update["currentMonthMinutes"] = next_month_minutes
                    if next_month_minutes >= int(org.get("maxMinutesPerMonth") or 0):
                        org_update["hardCapReached"] = True
                if has_trial_cap:
                    _consume_billing_trial_seconds(billing, remainder_seconds)
                    billing["updatedAt"] = now
                    org_update["billing"] = dict(billing)
                transaction.set(org_ref, org_update, merge=True)
            usage_ref = self._usage_ref(org_id, _yyyymm(now))
            usage_update: Dict[str, Any] = {
                "minutesStreamed": gcf_firestore.Increment(duration_min),
                "sessionsCount": gcf_firestore.Increment(1),
                "peakListeners": gcf_firestore.Increment(0),
                "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
            }
            if translated_delta_minutes > 0:
                usage_update["minutesTranslated"] = gcf_firestore.Increment(translated_delta_minutes)
            transaction.set(usage_ref, usage_update, merge=True)
            return {"orgId": org_id, "roomId": room_id, "status": "ended"}

        tx = db.transaction()
        result = _tx(tx)
        if transcript:
            room_ref.collection("finalTranscript").document("latest").set(
                {"text": transcript, "createdAt": gcf_firestore.SERVER_TIMESTAMP},
                merge=True,
            )
        return result

    def touch_audio(self, org_id: str, room_id: str) -> None:
        self._room_ref(org_id, room_id).set({"lastAudioAt": gcf_firestore.SERVER_TIMESTAMP}, merge=True)

    def bump_listener_peak(self, org_id: str, room_id: str, viewer_count: int) -> None:
        # Firestore lacks a server-side max primitive; this is best-effort.
        ref = self._room_ref(org_id, room_id)
        snap = ref.get()
        if not snap.exists:
            return
        room = snap.to_dict() or {}
        current = int(room.get("listenerCountPeak") or 0)
        if viewer_count > current:
            ref.set({"listenerCountPeak": int(viewer_count)}, merge=True)

    def get_active_room(self, org_id: str, service_key: str) -> Optional[str]:
        snap = self._service_ref(org_id, service_key).get()
        if not snap.exists:
            return None
        service = snap.to_dict() or {}
        room_id = service.get("activeRoomId")
        if not room_id:
            return None
        room_snap = self._room_ref(org_id, str(room_id)).get()
        if not room_snap.exists:
            return None
        room = room_snap.to_dict() or {}
        return str(room_id) if room.get("status") == "live" else None

    def stale_live_rooms(self, *, idle_seconds: int, max_duration_seconds: int) -> List[Dict[str, Any]]:
        now = _utcnow()
        out: List[Dict[str, Any]] = []
        for org_snap in self._db.collection("organizations").stream():
            org_id = org_snap.id
            for room_snap in self._where(self._org_ref(org_id).collection("rooms"), "status", "==", "live").stream():
                room = room_snap.to_dict() or {}
                started_at = room.get("startedAt")
                last_audio_at = room.get("lastAudioAt") or started_at
                if not isinstance(started_at, datetime):
                    continue
                age = (now - started_at).total_seconds()
                idle = (now - last_audio_at).total_seconds() if isinstance(last_audio_at, datetime) else age
                if idle >= idle_seconds:
                    out.append({"orgId": org_id, "roomId": room_snap.id, "reason": "idle_timeout"})
                elif age >= max_duration_seconds:
                    out.append({"orgId": org_id, "roomId": room_snap.id, "reason": "max_duration"})
        return out

    def enforce_live_usage_caps(self, *, tick_seconds: int) -> List[Dict[str, Any]]:
        now = _utcnow()
        period_key = _yyyymm(now)
        tick_min = _tick_minutes(tick_seconds)
        out: List[Dict[str, Any]] = []

        for org_snap in self._db.collection("organizations").stream():
            org_id = org_snap.id
            org = self._roll_billing_period_if_needed(org_id, org_snap.to_dict() or {}, now=now)
            billing_limits_enabled = _org_billing_limits_enabled(org)

            billing = _normalize_org_billing(org, now=now)
            max_minutes = int(org.get("maxMinutesPerMonth") or 0)
            has_monthly_cap = billing_limits_enabled and max_minutes > 0
            trial_limit = _billing_trial_minutes_limit(billing)
            trial_limit_seconds = _billing_trial_seconds_limit(billing)
            has_trial_cap = trial_limit_seconds > 0
            if not has_monthly_cap and not has_trial_cap:
                continue

            current_minutes = int(org.get("currentMonthMinutes") or 0)
            cap_reached = bool(org.get("hardCapReached")) if has_monthly_cap else False
            trial_billing_next = dict(billing)
            trial_reached = has_trial_cap and _billing_trial_minutes_exhausted(trial_billing_next)

            room_snaps = list(self._where(self._org_ref(org_id).collection("rooms"), "status", "==", "live").stream())
            live_room_ids = [snap.id for snap in room_snaps]
            if not live_room_ids:
                continue

            if cap_reached or trial_reached:
                reason = "trial_expired" if trial_reached else "monthly_limit_reached"
                for room_id in live_room_ids:
                    out.append({"orgId": org_id, "roomId": room_id, "reason": reason})
                continue

            org_delta = 0
            usage_delta = 0
            trial_delta_seconds = 0
            for room_snap in room_snaps:
                room = room_snap.to_dict() or {}
                if cap_reached or trial_reached:
                    break
                last_tick_at = room.get("lastUsageTickAt") or room.get("startedAt")
                if not isinstance(last_tick_at, datetime):
                    continue
                elapsed_sec = (now - last_tick_at).total_seconds()
                increments = int(elapsed_sec // max(1, tick_seconds))
                if increments <= 0:
                    continue
                delta_minutes = increments * tick_min
                delta_seconds = increments * max(1, tick_seconds)
                if has_monthly_cap:
                    org_delta += delta_minutes
                usage_delta += delta_minutes
                if has_trial_cap:
                    trial_delta_seconds += delta_seconds

                next_tick_at = last_tick_at + timedelta(seconds=increments * tick_seconds)
                self._room_ref(org_id, room_snap.id).set({"lastUsageTickAt": next_tick_at}, merge=True)

                if has_monthly_cap and (current_minutes + org_delta) >= max_minutes:
                    cap_reached = True
                if has_trial_cap and (trial_delta_seconds > 0):
                    _set_billing_trial_seconds_used(
                        trial_billing_next,
                        _billing_trial_seconds_used(billing) + trial_delta_seconds,
                    )
                if has_trial_cap and _billing_trial_minutes_exhausted(trial_billing_next):
                    trial_reached = True

            trial_usage_changed = has_trial_cap and _billing_trial_seconds_used(trial_billing_next) != _billing_trial_seconds_used(billing)
            if org_delta > 0 or cap_reached or trial_usage_changed:
                org_update: Dict[str, Any] = {}
                if org_delta > 0:
                    org_update["currentMonthMinutes"] = gcf_firestore.Increment(org_delta)
                if cap_reached:
                    org_update["hardCapReached"] = True
                if has_trial_cap:
                    trial_billing_next["updatedAt"] = now
                    org_update["billing"] = dict(trial_billing_next)
                if org_update:
                    self._org_ref(org_id).set(org_update, merge=True)

            if usage_delta > 0:
                self._usage_ref(org_id, period_key).set(
                    {
                        "minutesTranslated": gcf_firestore.Increment(usage_delta),
                        "updatedAt": gcf_firestore.SERVER_TIMESTAMP,
                    },
                    merge=True,
                )

            if cap_reached or trial_reached:
                reason = "trial_expired" if trial_reached else "monthly_limit_reached"
                for room_id in live_room_ids:
                    out.append({"orgId": org_id, "roomId": room_id, "reason": reason})

        return out


def _build_store():
    backend = (os.getenv("MULTICHURCH_STORE_BACKEND") or "").strip().lower()
    if backend == "memory":
        return InMemoryMultiChurchStore()
    if backend == "firestore":
        if gcf_firestore is None:
            raise RuntimeError("MULTICHURCH_STORE_BACKEND=firestore but google-cloud-firestore is not installed")
        return FirestoreMultiChurchStore()

    # auto-detect
    if gcf_firestore is not None and (
        os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("FIRESTORE_PROJECT") or os.getenv("GCP_PROJECT")
    ):
        return FirestoreMultiChurchStore()
    return InMemoryMultiChurchStore()


multichurch_store = _build_store()
