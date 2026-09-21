"""Gate 2 helpers v6 — `analyze_cloud_run`.

Review-only skeleton. See `docs/03-analysis/gate2-helpers-v6-spec.md`
for the acceptance criteria and the hard-prerequisite list (D1-D8).

This module intentionally reimplements ONLY the diff vs v5 so the
review focus stays on:
  (a) the expected_redis / vpc_mode contract,
  (b) the literal-string checks on REDIS_ENABLED,
  (c) the additional `=1` guards (VPC egress + AUTH secret binding),
  (d) the `=0` config-vs-log-absence separation.

Every other v5 guard (traffic split, latestReady/latestCreated
identity, Ready + seconds-Ready, ROOM_RECONCILER_ENABLED,
max-instances) is preserved verbatim and MUST also live in the
final v6 lib. This skeleton stubs them via `_run_v5_guards()` so
the v6 tests can assert they still fire — full copy lands with the
enablement PR that merges v6.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional


# ---------------------------------------------------------------------------
# v5 → v6 diff (the checks that changed)
# ---------------------------------------------------------------------------


def analyze_cloud_run(
    service_desc: dict,
    revision_desc: dict,
    stabilization_sec: int,
    *,
    expected_redis: str,
    vpc_mode: Optional[str] = None,
    now: Optional[datetime] = None,
) -> tuple[dict[str, Any], list[str]]:
    """Return (snapshot, guards). All guards must be empty for the
    service to count as Gate-2-compliant for the DIRECTION named by
    `expected_redis`.

    `expected_redis` MUST be "0" or "1" — the literal env value that
    the operator's set/clear script writes into the revision. v6
    refuses any other value including the v5-legacy truthy variants
    (`"false"`, `"True"`, etc.) because the downstream metric
    filters key on the literal too.

    `vpc_mode` MUST be "connector" or "direct" when
    `expected_redis == "1"`. It selects which of the two
    mutually-exclusive VPC egress paths (Serverless VPC Access
    Connector vs Direct VPC egress) the operator intended per
    PR #31 §4c step 2.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    guards: list[str] = []

    # ---- Guard: expected_redis parameter contract --------------------------
    if expected_redis not in ("0", "1"):
        guards.append(
            f"expected_redis must be '0' or '1'; got {expected_redis!r}"
        )
        # Return early — every subsequent check depends on knowing
        # the intended state.
        return _snapshot(service_desc, revision_desc, expected_redis, vpc_mode), guards

    # ---- Preserved v5 guards (delegated to the shared v5 body) ------------
    v5_snapshot, v5_guards = _run_v5_guards(
        service_desc, revision_desc, stabilization_sec, now=now,
    )
    guards.extend(v5_guards)

    # ---- v5 → v6 diff #1: literal-string REDIS_ENABLED check --------------
    redis_enabled = v5_snapshot.get("redis_enabled_raw", "unset")
    if redis_enabled != expected_redis:
        guards.append(
            f"REDIS_ENABLED must be exactly {expected_redis!r}; "
            f"got {redis_enabled!r}"
        )

    # ---- v5 → v6 diff #2: `=1` extra checks (VPC + AUTH) -----------------
    if expected_redis == "1":
        guards.extend(_check_vpc_egress(service_desc, vpc_mode))
        guards.extend(_check_redis_password_binding(revision_desc))

    # ---- v5 → v6 diff #3: `=0` log-absence check is a runtime helper -----
    # See `log_checks.py::assert_no_redis_events_in_last_window` — it
    # queries Cloud Logging and is NOT reachable from a pure static
    # analyzer. Recorded on the snapshot so the postdeploy driver
    # knows it still owes that runtime call.
    snapshot = _snapshot(service_desc, revision_desc, expected_redis, vpc_mode)
    snapshot["v5_snapshot"] = v5_snapshot
    snapshot["runtime_check_owed"] = (
        "log_checks.assert_no_redis_events_in_last_window"
        if expected_redis == "0"
        else "log_checks.assert_adapter_up_paired_a5_and_a8b"
    )
    return snapshot, guards


# ---------------------------------------------------------------------------
# `=1` checks — v6-specific
# ---------------------------------------------------------------------------


def _check_vpc_egress(service_desc: dict, vpc_mode: Optional[str]) -> list[str]:
    """Mutually-exclusive check. `vpc-access-connector` XOR
    Direct VPC egress subnet — never both, never neither."""
    guards: list[str] = []
    connector = _connector_annotation(service_desc)
    direct_subnet = _direct_vpc_subnet(service_desc)

    if vpc_mode == "connector":
        if not connector:
            guards.append(
                "expected connector VPC egress (GATE2_VPC_MODE=connector); "
                "no vpc-access-connector attached"
            )
        if direct_subnet:
            guards.append(
                "connector and Direct VPC egress are both attached; "
                "must be exactly one per PR #31 §4c step 2"
            )
    elif vpc_mode == "direct":
        if not direct_subnet:
            guards.append(
                "expected Direct VPC egress (GATE2_VPC_MODE=direct); "
                "no egress subnet attached"
            )
        if connector:
            guards.append(
                "Direct VPC egress and connector are both attached; "
                "must be exactly one per PR #31 §4c step 2"
            )
    else:
        guards.append(
            f"GATE2_VPC_MODE must be 'connector' or 'direct' when "
            f"expected_redis=1; got {vpc_mode!r}"
        )
    return guards


def _check_redis_password_binding(revision_desc: dict) -> list[str]:
    """AUTH secret binding must match the Memorystore instance's
    actual AUTH state. Instance state is not observable from the
    service desc alone, so v6 requires the operator to supply it as
    `REDIS_INSTANCE_HAS_AUTH=0|1`. v6 refuses to guess."""
    guards: list[str] = []
    has_auth = os.environ.get("REDIS_INSTANCE_HAS_AUTH")
    if has_auth not in ("0", "1"):
        guards.append(
            f"REDIS_INSTANCE_HAS_AUTH must be '0' or '1' when "
            f"expected_redis=1; got {has_auth!r}"
        )
        return guards

    binding = _redis_password_binding(revision_desc)
    if has_auth == "1" and not binding:
        guards.append(
            "REDIS_INSTANCE_HAS_AUTH=1 but REDIS_PASSWORD is not "
            "bound on the serving revision"
        )
    if has_auth == "0" and binding:
        guards.append(
            "REDIS_INSTANCE_HAS_AUTH=0 but REDIS_PASSWORD is still "
            "bound on the serving revision (stale binding — remove "
            "in this window per PR #31 §4c step 4)"
        )
    return guards


# ---------------------------------------------------------------------------
# service_desc / revision_desc parsers
# ---------------------------------------------------------------------------


def _connector_annotation(service_desc: dict) -> str:
    template = (service_desc.get("spec") or {}).get("template") or {}
    ann = (template.get("metadata") or {}).get("annotations") or {}
    return ann.get("run.googleapis.com/vpc-access-connector", "") or ""


def _direct_vpc_subnet(service_desc: dict) -> str:
    """Direct VPC egress attaches a network-interface subnet.
    Google's schema exposes this as
    `spec.template.spec.vpc_access.network_interfaces[*].subnetwork`
    in the run.v2 admin API; run.v1 exposes it via the
    `run.googleapis.com/network-interfaces` annotation. v6 checks
    both; a match on EITHER form counts."""
    template = (service_desc.get("spec") or {}).get("template") or {}
    ann = (template.get("metadata") or {}).get("annotations") or {}
    ann_ni = ann.get("run.googleapis.com/network-interfaces", "") or ""
    if ann_ni:
        return ann_ni

    tspec = template.get("spec") or {}
    vpc_access = tspec.get("vpc_access") or tspec.get("vpcAccess") or {}
    for ni in vpc_access.get("network_interfaces") or vpc_access.get("networkInterfaces") or []:
        subnetwork = ni.get("subnetwork") or ni.get("subnet") or ""
        if subnetwork:
            return subnetwork
    return ""


def _redis_password_binding(revision_desc: dict) -> Optional[dict]:
    """Return the REDIS_PASSWORD env entry from the revision spec
    (a dict with `valueFrom.secretKeyRef` when bound), or `None`
    when absent. NB: revision_desc SHOULD be the serving revision's
    own JSON (per PR #31 §4a-3), NOT the service template — v5's
    revision_desc arg already carries this shape."""
    spec = revision_desc.get("spec") or {}
    containers = spec.get("containers") or []
    if not containers:
        return None
    env = containers[0].get("env") or []
    for e in env:
        if isinstance(e, dict) and e.get("name") == "REDIS_PASSWORD":
            return e
    return None


# ---------------------------------------------------------------------------
# Preserved v5 guards — STUB in this review-only branch
# ---------------------------------------------------------------------------


def _run_v5_guards(
    service_desc: dict,
    revision_desc: dict,
    stabilization_sec: int,
    *,
    now: datetime,
) -> tuple[dict[str, Any], list[str]]:
    """STUB. In v5, `analyze_cloud_run` runs the traffic/latestReady/
    Ready/max-instances/reconciler_enabled checks inline. v6 will
    copy that body verbatim (only the REDIS_ENABLED branch changes).

    This stub returns the raw REDIS_ENABLED value from the revision
    env so the caller's literal-string check can run against it,
    plus an empty guard list. Fixture tests in this branch cover
    the v6 diff only; v5-guard preservation is covered by the
    existing v5 test suite (`~/.gate2-helpers-v5/_gate2_test.py`),
    which the enablement PR will copy into `tests/` alongside the
    v6 tests.
    """
    env = _env_map(revision_desc)
    return {"redis_enabled_raw": env.get("REDIS_ENABLED", "unset")}, []


def _env_map(revision_desc: dict) -> dict[str, str]:
    """Serving-revision env as {name: literal_value}. Secret-ref
    entries appear with value=None; the AUTH check reads the full
    entry via `_redis_password_binding` for value-vs-secretRef
    discrimination."""
    spec = revision_desc.get("spec") or {}
    containers = spec.get("containers") or []
    if not containers:
        return {}
    out: dict[str, str] = {}
    for e in containers[0].get("env") or []:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        if not name:
            continue
        # Literal value only; secret-refs are handled separately.
        if "value" in e:
            out[name] = str(e.get("value"))
    return out


def _snapshot(
    service_desc: dict,
    revision_desc: dict,
    expected_redis: str,
    vpc_mode: Optional[str],
) -> dict[str, Any]:
    return {
        "expected_redis": expected_redis,
        "vpc_mode": vpc_mode,
        "connector_attached": bool(_connector_annotation(service_desc)),
        "direct_vpc_subnet": _direct_vpc_subnet(service_desc) or None,
        "redis_password_binding_present": _redis_password_binding(revision_desc) is not None,
    }
