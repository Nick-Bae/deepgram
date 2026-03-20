from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException

from app.auth.firebase_auth import AuthenticatedUser, get_current_user_required
from app.services.multichurch_store import multichurch_store

router = APIRouter()
logger = logging.getLogger(__name__)

# Simple in-memory cache for GCP usage (avoids hammering Monitoring API on every refresh)
_gcp_usage_cache: Dict[str, Any] = {}
_GCP_USAGE_CACHE_TTL = 300  # 5 minutes

# Firestore pricing ($/100K operations)
_FS_READ_COST_PER_100K = 0.06
_FS_WRITE_COST_PER_100K = 0.18
_FS_DELETE_COST_PER_100K = 0.02
_FS_STORAGE_COST_PER_GIB = 0.108


def _billing_url(cfg: Dict[str, Any]) -> str:
    billing_account_id = str(cfg.get("gcpBillingAccountId") or "").strip()
    return (
        f"https://console.cloud.google.com/billing/{billing_account_id}/reports"
        if billing_account_id
        else "https://console.cloud.google.com/billing"
    )


def _fetch_monitoring_metric(headers: Dict[str, str], project_id: str, metric_type: str, start_iso: str, end_iso: str, duration_secs: int) -> tuple[int, Optional[str]]:
    """Returns (count, error_message). error_message is None on success."""
    import httpx
    url = (
        f"https://monitoring.googleapis.com/v3/projects/{quote(project_id, safe='')}/timeSeries"
        f"?filter=metric.type%3D%22{quote(metric_type)}%22"
        f"&interval.startTime={start_iso}"
        f"&interval.endTime={end_iso}"
        f"&aggregation.alignmentPeriod={duration_secs}s"
        f"&aggregation.perSeriesAligner=ALIGN_SUM"
        f"&aggregation.crossSeriesReducer=REDUCE_SUM"
    )
    try:
        response = httpx.get(url, headers=headers, timeout=20)
        if response.status_code == 403:
            msg = (response.json().get("error") or {}).get("message", "Permission denied")
            return 0, f"403: {msg} — grant roles/monitoring.viewer to the service account in GCP IAM."
        if response.status_code != 200:
            return 0, f"Monitoring API returned HTTP {response.status_code}"
        total = 0
        for ts in response.json().get("timeSeries", []):
            for point in ts.get("points", []):
                val = point.get("value", {})
                total += int(float(val.get("int64Value") or val.get("doubleValue") or 0))
        return total, None
    except Exception as exc:
        return 0, str(exc)


def _get_token(scopes: List[str]) -> tuple[str, str]:
    """Returns (access_token, project_id). Raises on failure."""
    from google.oauth2 import service_account as sa_mod

    cred_path = (
        (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
        or (os.getenv("FIREBASE_ADMIN_CREDENTIALS") or "").strip()
    )
    project_id = (
        (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
        or (os.getenv("GCP_PROJECT") or "").strip()
    )

    if not cred_path or not os.path.exists(cred_path):
        raise RuntimeError("No service account file found (GOOGLE_APPLICATION_CREDENTIALS)")

    with open(cred_path) as f:
        sa_info = json.load(f)
    if not project_id:
        project_id = sa_info.get("project_id", "")
    if not project_id:
        raise RuntimeError("GCP project ID not found. Set GOOGLE_CLOUD_PROJECT env var.")

    credentials = sa_mod.Credentials.from_service_account_info(sa_info, scopes=scopes)

    token: Optional[str] = None

    try:
        from google.auth.transport.requests import Request as _Req
        credentials.refresh(_Req())
        token = credentials.token
    except Exception:
        pass

    if not token:
        try:
            import urllib3
            from google.auth.transport.urllib3 import Request as _UReq
            credentials.refresh(_UReq(urllib3.PoolManager()))
            token = credentials.token
        except Exception:
            pass

    if not token:
        import httpx as _httpx
        assertion = credentials._make_authorization_grant_assertion()
        resp = _httpx.post(
            "https://oauth2.googleapis.com/token",
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
            timeout=20,
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]

    if not token:
        raise RuntimeError("Could not obtain access token via any transport")

    return token, project_id


def _get_monitoring_token() -> tuple[str, str]:
    return _get_token(["https://www.googleapis.com/auth/monitoring.read"])


def _fetch_billing_total(billing_account_id: str) -> tuple[Optional[float], Optional[str], Optional[float]]:
    """Returns (current_spend_usd, error_message, budget_usd).
    Uses Budget API: budgets carry current spend in their allUpdatesRule / threshold state,
    but actual spend comes from forecasted spend in the budget response (v1beta).
    Falls back to showing budget limit if spend isn't queryable.
    """
    import httpx
    try:
        token, _ = _get_token([
            "https://www.googleapis.com/auth/cloud-billing",
        ])
    except Exception as exc:
        return None, str(exc), None

    headers = {"Authorization": f"Bearer {token}"}

    # Try v1beta budgets — it includes forecastedSpend (actual month-to-date cost)
    url = f"https://billingbudgets.googleapis.com/v1beta1/billingAccounts/{quote(billing_account_id, safe='')}/budgets"
    try:
        resp = httpx.get(url, headers=headers, timeout=20)
        if resp.status_code == 403:
            msg = (resp.json().get("error") or {}).get("message", "Permission denied")
            return None, f"403: {msg}", None
        if resp.status_code not in (200, 404):
            return None, f"Budget API returned HTTP {resp.status_code}", None

        budgets = resp.json().get("budgets", []) if resp.status_code == 200 else []
        if not budgets:
            return None, "No budgets found — create a budget in Cloud Billing.", None

        # Use the first budget
        b = budgets[0]
        budget_usd: Optional[float] = None
        specified = (b.get("amount") or {}).get("specifiedAmount", {})
        if specified:
            budget_usd = float(specified.get("units") or 0) + float(specified.get("nanos") or 0) / 1e9

        # forecastedSpend is the actual month-to-date spend (v1beta field)
        forecasted = b.get("forecastedSpend") or b.get("currentSpend") or {}
        if forecasted:
            units = float(forecasted.get("units") or 0)
            nanos = float(forecasted.get("nanos") or 0) / 1e9
            return units + nanos, None, budget_usd

        return None, "Spend data not yet available — may take up to 24h after budget creation.", budget_usd

    except Exception as exc:
        return None, str(exc), None


def _build_gcp_usage() -> Dict[str, Any]:
    try:
        token, project_id = _get_monitoring_token()
    except Exception as exc:
        return {"available": False, "error": str(exc), "billingConsoleUrl": _billing_url(multichurch_store.get_platform_config())}

    import httpx
    auth_headers = {"Authorization": f"Bearer {token}"}

    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    duration_secs = max(1, int((now - start).total_seconds()))
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    reads, err = _fetch_monitoring_metric(auth_headers, project_id, "firestore.googleapis.com/document/read_count", start_iso, end_iso, duration_secs)
    if err:
        return {"available": False, "error": err, "billingConsoleUrl": _billing_url(multichurch_store.get_platform_config())}
    writes, _ = _fetch_monitoring_metric(auth_headers, project_id, "firestore.googleapis.com/document/write_count", start_iso, end_iso, duration_secs)
    deletes, _ = _fetch_monitoring_metric(auth_headers, project_id, "firestore.googleapis.com/document/delete_count", start_iso, end_iso, duration_secs)

    estimated_usd = round(
        reads / 100_000 * _FS_READ_COST_PER_100K
        + writes / 100_000 * _FS_WRITE_COST_PER_100K
        + deletes / 100_000 * _FS_DELETE_COST_PER_100K,
        6,
    )

    cfg = multichurch_store.get_platform_config()
    billing_account_id = str(cfg.get("gcpBillingAccountId") or "").strip()

    spend_usd, spend_err, budget_usd = (
        _fetch_billing_total(billing_account_id)
        if billing_account_id
        else (None, "No billing account ID configured.", None)
    )

    return {
        "available": True,
        "projectId": project_id,
        "periodKey": start.strftime("%Y%m"),
        "firestore": {
            "reads": reads,
            "writes": writes,
            "deletes": deletes,
            "estimatedUsd": estimated_usd,
        },
        "billing": {
            "spendUsd": spend_usd,
            "budgetUsd": budget_usd,
            "error": spend_err,
        },
        "billingAccountId": billing_account_id,
        "billingConsoleUrl": _billing_url(cfg),
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_str(v: Any, fallback: str = "") -> str:
    return str(v or fallback)


def _safe_int(v: Any, fallback: int = 0) -> int:
    try:
        return int(v or fallback)
    except (TypeError, ValueError):
        return fallback


def _require_master(user: AuthenticatedUser) -> None:
    if not multichurch_store.is_master_user(user.uid) and not user.isSuper:
        raise HTTPException(status_code=403, detail="forbidden")


def _serialize_dt(dt: Any) -> Optional[str]:
    if isinstance(dt, datetime):
        return dt.isoformat()
    if isinstance(dt, str):
        return dt
    return None


@router.get("/admin/platform-config")
def get_platform_config(current_user: AuthenticatedUser = Depends(get_current_user_required)):
    _require_master(current_user)
    return multichurch_store.get_platform_config()


@router.post("/admin/platform-config")
def save_platform_config(payload: Dict[str, Any] = Body(...), current_user: AuthenticatedUser = Depends(get_current_user_required)):
    _require_master(current_user)
    try:
        result = multichurch_store.save_platform_config(payload)
        _gcp_usage_cache.clear()  # billing URL may have changed
        return result
    except Exception as exc:
        logger.exception("Failed to save platform config")
        raise HTTPException(status_code=500, detail="platform_config_error") from exc


@router.get("/admin/gcp-usage")
def get_gcp_usage(current_user: AuthenticatedUser = Depends(get_current_user_required)):
    _require_master(current_user)
    cached = _gcp_usage_cache.get("data")
    if cached and (time.time() - _gcp_usage_cache.get("ts", 0)) < _GCP_USAGE_CACHE_TTL:
        return cached
    result = _build_gcp_usage()
    _gcp_usage_cache["data"] = result
    _gcp_usage_cache["ts"] = time.time()
    return result


@router.get("/admin/platform-usage")
def get_platform_usage(
    current_user: AuthenticatedUser = Depends(get_current_user_required),
    period: Optional[str] = None,
):
    _require_master(current_user)
    import re as _re
    clean_period: Optional[str] = None
    if period:
        if not _re.fullmatch(r"\d{6}", period.strip()):
            raise HTTPException(status_code=422, detail="period must be YYYYMM (e.g. 202603)")
        clean_period = period.strip()
    try:
        return multichurch_store.get_platform_usage_summary(period_key=clean_period)
    except Exception as exc:
        logger.exception("Failed to get platform usage summary")
        raise HTTPException(status_code=500, detail="platform_usage_error") from exc


@router.get("/admin/dashboard")
def get_admin_dashboard(current_user: AuthenticatedUser = Depends(get_current_user_required)):
    _require_master(current_user)

    try:
        return _build_dashboard_inmemory()
    except AttributeError:
        pass

    try:
        return _build_dashboard_firestore()
    except Exception as exc:
        logger.exception("Failed to build admin dashboard")
        raise HTTPException(status_code=500, detail="dashboard_error") from exc


def _build_dashboard_inmemory() -> Dict[str, Any]:
    store = multichurch_store
    # Access internal state (InMemoryMultiChurchStore only)
    orgs_dict: dict = store._orgs  # type: ignore[attr-defined]
    services_dict: dict = store._services  # type: ignore[attr-defined]
    rooms_dict: dict = store._rooms  # type: ignore[attr-defined]
    members_dict: dict = store._members  # type: ignore[attr-defined]

    now = _utcnow()
    org_rows: List[Dict[str, Any]] = []
    live_rooms: List[Dict[str, Any]] = []
    plan_counts: Dict[str, int] = {}
    status_counts: Dict[str, int] = {}
    total_members = 0

    for org_id, org in orgs_dict.items():
        plan = _safe_str(org.get("plan"), "trial")
        billing = org.get("billing") or {}
        b_status = _safe_str(billing.get("status"), "trialing")
        plan_counts[plan] = plan_counts.get(plan, 0) + 1
        status_counts[b_status] = status_counts.get(b_status, 0) + 1

        member_count = sum(1 for (oid, _) in members_dict if oid == org_id)
        total_members += member_count

        service_count = sum(1 for (oid, _) in services_dict if oid == org_id)

        org_live_rooms = []
        for (oid, rid), room in rooms_dict.items():
            if oid == org_id and room.get("status") == "live":
                service_key = _safe_str(room.get("serviceKey"))
                started_at = room.get("startedAt")
                elapsed_min = None
                if isinstance(started_at, datetime):
                    elapsed_min = int((now - started_at).total_seconds() / 60)
                live_info = {
                    "roomId": rid,
                    "serviceKey": service_key,
                    "source": _safe_str(room.get("source")),
                    "target": _safe_str(room.get("target")),
                    "startedAt": _serialize_dt(started_at),
                    "elapsedMinutes": elapsed_min,
                }
                org_live_rooms.append(live_info)
                live_rooms.append({"orgId": org_id, "orgName": _safe_str(org.get("name"), org_id), **live_info})

        trial_seconds_remaining = None
        trial_limit = _safe_int(billing.get("trialSecondsLimit") or billing.get("trialMinutesLimit", 0) * 60)
        trial_used = _safe_int(billing.get("trialSecondsUsed"))
        if trial_limit > 0:
            trial_seconds_remaining = max(0, trial_limit - trial_used)

        org_rows.append({
            "orgId": org_id,
            "name": _safe_str(org.get("name"), org_id),
            "slug": _safe_str(org.get("slug"), org_id),
            "plan": plan,
            "status": _safe_str(org.get("status"), "active"),
            "billingStatus": b_status,
            "hardCapReached": bool(org.get("hardCapReached")),
            "memberCount": member_count,
            "serviceCount": service_count,
            "liveRoomCount": len(org_live_rooms),
            "liveRooms": org_live_rooms,
            "currentMonthMinutes": _safe_int(org.get("currentMonthMinutes")),
            "maxMinutesPerMonth": _safe_int(org.get("maxMinutesPerMonth")),
            "trialSecondsRemaining": trial_seconds_remaining,
            "sermonPrepUsageUsd": float(org.get("sermonPrepUsageUsd") or 0),
            "sermonPrepBudgetUsd": float(org.get("sermonPrepBudgetUsd") or 0),
            "createdAt": _serialize_dt(org.get("createdAt")),
            "stripeCustomerId": _safe_str(billing.get("stripeCustomerId")),
            "stripeSubscriptionId": _safe_str(billing.get("stripeSubscriptionId")),
        })

    org_rows.sort(key=lambda r: r["name"].lower())

    return {
        "generatedAt": now.isoformat(),
        "summary": {
            "totalOrgs": len(org_rows),
            "totalMembers": total_members,
            "liveRoomCount": len(live_rooms),
            "planCounts": plan_counts,
            "billingStatusCounts": status_counts,
        },
        "organizations": org_rows,
        "liveRooms": live_rooms,
    }


def _build_dashboard_firestore() -> Dict[str, Any]:
    store = multichurch_store
    db = store._db  # type: ignore[attr-defined]
    now = _utcnow()

    org_rows: List[Dict[str, Any]] = []
    live_rooms: List[Dict[str, Any]] = []
    plan_counts: Dict[str, int] = {}
    status_counts: Dict[str, int] = {}
    total_members = 0

    for org_snap in db.collection("organizations").stream():
        org_id: str = org_snap.id
        org: Dict[str, Any] = org_snap.to_dict() or {}
        plan = _safe_str(org.get("plan"), "trial")
        billing = org.get("billing") or {}
        b_status = _safe_str(billing.get("status"), "trialing")
        plan_counts[plan] = plan_counts.get(plan, 0) + 1
        status_counts[b_status] = status_counts.get(b_status, 0) + 1

        # Count members
        member_snaps = list(db.collection("organizations").document(org_id).collection("members").stream())
        member_count = len(member_snaps)
        total_members += member_count

        # Count services and check live rooms
        service_snaps = list(db.collection("organizations").document(org_id).collection("services").stream())
        service_count = len(service_snaps)

        org_live_rooms: List[Dict[str, Any]] = []
        for svc_snap in service_snaps:
            svc = svc_snap.to_dict() or {}
            active_room_id = svc.get("activeRoomId")
            if active_room_id:
                room_snap = (
                    db.collection("organizations")
                    .document(org_id)
                    .collection("rooms")
                    .document(str(active_room_id))
                    .get()
                )
                if room_snap.exists:
                    room = room_snap.to_dict() or {}
                    if room.get("status") == "live":
                        started_at = room.get("startedAt")
                        elapsed_min = None
                        if isinstance(started_at, datetime):
                            elapsed_min = int((now - started_at).total_seconds() / 60)
                        live_info = {
                            "roomId": str(active_room_id),
                            "serviceKey": svc_snap.id,
                            "source": _safe_str(room.get("source")),
                            "target": _safe_str(room.get("target")),
                            "startedAt": _serialize_dt(started_at),
                            "elapsedMinutes": elapsed_min,
                        }
                        org_live_rooms.append(live_info)
                        live_rooms.append({
                            "orgId": org_id,
                            "orgName": _safe_str(org.get("name"), org_id),
                            **live_info,
                        })

        trial_seconds_remaining = None
        trial_limit = _safe_int(billing.get("trialSecondsLimit") or billing.get("trialMinutesLimit", 0) * 60)
        trial_used = _safe_int(billing.get("trialSecondsUsed"))
        if trial_limit > 0:
            trial_seconds_remaining = max(0, trial_limit - trial_used)

        org_rows.append({
            "orgId": org_id,
            "name": _safe_str(org.get("name"), org_id),
            "slug": _safe_str(org.get("slug"), org_id),
            "plan": plan,
            "status": _safe_str(org.get("status"), "active"),
            "billingStatus": b_status,
            "hardCapReached": bool(org.get("hardCapReached")),
            "memberCount": member_count,
            "serviceCount": service_count,
            "liveRoomCount": len(org_live_rooms),
            "liveRooms": org_live_rooms,
            "currentMonthMinutes": _safe_int(org.get("currentMonthMinutes")),
            "maxMinutesPerMonth": _safe_int(org.get("maxMinutesPerMonth")),
            "trialSecondsRemaining": trial_seconds_remaining,
            "sermonPrepUsageUsd": float(org.get("sermonPrepUsageUsd") or 0),
            "sermonPrepBudgetUsd": float(org.get("sermonPrepBudgetUsd") or 0),
            "createdAt": _serialize_dt(org.get("createdAt")),
            "stripeCustomerId": _safe_str(billing.get("stripeCustomerId")),
            "stripeSubscriptionId": _safe_str(billing.get("stripeSubscriptionId")),
        })

    org_rows.sort(key=lambda r: r["name"].lower())

    return {
        "generatedAt": now.isoformat(),
        "summary": {
            "totalOrgs": len(org_rows),
            "totalMembers": total_members,
            "liveRoomCount": len(live_rooms),
            "planCounts": plan_counts,
            "billingStatusCounts": status_counts,
        },
        "organizations": org_rows,
        "liveRooms": live_rooms,
    }
