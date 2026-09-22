"""Gate 2 helpers v6 — `analyze_cloud_run`.

Review-only skeleton. See `docs/03-analysis/gate2-helpers-v6-spec.md`
for the acceptance criteria and the hard-prerequisite list (D1-D9).

This module intentionally reimplements ONLY the diff vs v5 so the
review focus stays on:
  (a) the expected_redis / vpc_mode contract,
  (b) the literal-string checks on REDIS_ENABLED (strict typing;
      secret-refs and non-string types refused),
  (c) the additional `=1` guards (VPC egress + AUTH secret
      binding) — read from the SERVING REVISION, not the template,
      per PR #31 §4a-3 + Y5 / round-2 review of PR #36,
  (d) the `=0` config-vs-log-absence separation.

Every other v5 guard (traffic split, latestReady/latestCreated
identity, Ready + seconds-Ready, ROOM_RECONCILER_ENABLED,
max-instances) is preserved verbatim and MUST also live in the
final v6 lib. This skeleton stubs them via `_run_v5_guards()` so
the v6 tests can assert they still fire — full copy lands with the
enablement PR that merges v6.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional


# Sentinel to distinguish "field absent" from "field present with
# a None value" when inspecting env entries.
_MISSING = object()


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

    IMPORTANT: `revision_desc` is expected to be the SERVING
    revision's own `gcloud run revisions describe --format=json`
    output — NOT the service template. VPC egress and env config
    are read from `revision_desc.spec` for all v6 checks so a
    canary-only annotation on the service template cannot mask a
    misconfigured serving revision.
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
        return _snapshot(revision_desc, expected_redis, vpc_mode), guards

    # ---- Preserved v5 guards (delegated to the shared v5 body) ------------
    v5_snapshot, v5_guards = _run_v5_guards(
        service_desc, revision_desc, stabilization_sec, now=now,
    )
    guards.extend(v5_guards)

    # ---- v5 → v6 diff #1: literal-string REDIS_ENABLED check --------------
    redis_enabled_raw = v5_snapshot.get("redis_enabled_raw", _MISSING)
    guards.extend(_check_redis_enabled_literal(redis_enabled_raw, expected_redis))

    # ---- v5 → v6 diff #2: `=1` extra checks (VPC + AUTH) -----------------
    if expected_redis == "1":
        guards.extend(_check_vpc_egress(revision_desc, vpc_mode))
        guards.extend(_check_redis_password_binding(revision_desc))

    # ---- v5 → v6 diff #3: `=0` log-absence check is a runtime helper -----
    # See `log_checks.py::assert_no_redis_events_in_last_window` — it
    # queries Cloud Logging and is NOT reachable from a pure static
    # analyzer. Recorded on the snapshot so the postdeploy driver
    # knows it still owes that runtime call.
    snapshot = _snapshot(revision_desc, expected_redis, vpc_mode)
    snapshot["v5_snapshot"] = v5_snapshot
    snapshot["runtime_check_owed"] = (
        "log_checks.assert_no_redis_events_in_last_window"
        if expected_redis == "0"
        else "log_checks.assert_adapter_up_paired_a5_and_a8b"
    )
    return snapshot, guards


# ---------------------------------------------------------------------------
# REDIS_ENABLED literal — strict-typed
# ---------------------------------------------------------------------------


def _check_redis_enabled_literal(
    raw: Any, expected_redis: str,
) -> list[str]:
    """Reject non-string types and secret-refs; accept only the
    literal expected string. `str()` coercion is deliberately NOT
    used — `REDIS_ENABLED=0` as a JSON integer 0 would coerce to
    `"0"` and silently pass an intended-`"1"` window."""
    guards: list[str] = []
    if raw is _MISSING:
        guards.append(
            f"REDIS_ENABLED is missing on the serving revision; "
            f"expected literal string {expected_redis!r}"
        )
        return guards
    # Secret-ref marker — `_env_map` records this as the string
    # sentinel "<secretRef>" instead of a literal value.
    if raw == "<secretRef>":
        guards.append(
            f"REDIS_ENABLED must be a literal string, not a secret ref; "
            f"expected {expected_redis!r}"
        )
        return guards
    if not isinstance(raw, str):
        guards.append(
            f"REDIS_ENABLED must be a literal STRING; got "
            f"{type(raw).__name__} {raw!r}"
        )
        return guards
    if raw != expected_redis:
        guards.append(
            f"REDIS_ENABLED must be exactly {expected_redis!r}; got {raw!r}"
        )
    return guards


# ---------------------------------------------------------------------------
# `=1` checks — v6-specific
# ---------------------------------------------------------------------------


def _check_vpc_egress(
    revision_desc: dict, vpc_mode: Optional[str],
) -> list[str]:
    """Mutually-exclusive check. Reads from the SERVING REVISION,
    not the service template. The network-interface annotation is
    parsed as JSON and validated structurally — an annotation
    containing `[]`, `"not-json"`, or a list of dicts missing
    `subnetwork` MUST fail here rather than silently pass."""
    guards: list[str] = []

    connector = _connector_annotation(revision_desc)
    direct_subnet, direct_parse_errors = _direct_vpc_subnet(revision_desc)
    guards.extend(direct_parse_errors)

    if vpc_mode == "connector":
        if not connector:
            guards.append(
                "expected connector VPC egress (GATE2_VPC_MODE=connector); "
                "no vpc-access-connector attached to the serving revision"
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
                "no valid egress subnet attached to the serving revision"
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
    revision desc alone, so v6 requires the operator to supply it
    as `REDIS_INSTANCE_HAS_AUTH=0|1`. v6 refuses to guess.

    The binding structure itself is validated strictly — plaintext
    values, empty valueFrom, and secretKeyRef with missing/empty
    `name` or `key` are all rejected when AUTH is on."""
    guards: list[str] = []
    has_auth = os.environ.get("REDIS_INSTANCE_HAS_AUTH")
    if has_auth not in ("0", "1"):
        guards.append(
            f"REDIS_INSTANCE_HAS_AUTH must be '0' or '1' when "
            f"expected_redis=1; got {has_auth!r}"
        )
        return guards

    entry = _redis_password_entry(revision_desc)
    if has_auth == "1":
        # Must be a valid secret ref.
        if entry is None:
            guards.append(
                "REDIS_INSTANCE_HAS_AUTH=1 but REDIS_PASSWORD is not "
                "bound on the serving revision"
            )
            return guards
        # Plaintext values are refused for AUTH-on — a plaintext
        # password in the revision spec is a security leak AND not
        # what Cloud Run's Secret Manager integration produces.
        if "value" in entry:
            guards.append(
                "REDIS_INSTANCE_HAS_AUTH=1 but REDIS_PASSWORD is a "
                "plaintext value; must be a Secret Manager secretKeyRef"
            )
            return guards
        vf = entry.get("valueFrom")
        if not isinstance(vf, dict):
            guards.append(
                "REDIS_INSTANCE_HAS_AUTH=1 but REDIS_PASSWORD has no "
                "valueFrom section; expected secretKeyRef"
            )
            return guards
        skr = vf.get("secretKeyRef")
        if not isinstance(skr, dict):
            guards.append(
                "REDIS_INSTANCE_HAS_AUTH=1 but REDIS_PASSWORD.valueFrom "
                "has no secretKeyRef object"
            )
            return guards
        name = skr.get("name")
        key = skr.get("key")
        if not isinstance(name, str) or not name.strip():
            guards.append(
                "REDIS_PASSWORD secretKeyRef.name must be a non-empty "
                f"string; got {name!r}"
            )
        if not isinstance(key, str) or not key.strip():
            guards.append(
                "REDIS_PASSWORD secretKeyRef.key must be a non-empty "
                f"string; got {key!r}"
            )
    else:  # has_auth == "0"
        if entry is not None:
            guards.append(
                "REDIS_INSTANCE_HAS_AUTH=0 but REDIS_PASSWORD is still "
                "bound on the serving revision (stale binding — remove "
                "in this window per PR #31 §4c step 4)"
            )
    return guards


# ---------------------------------------------------------------------------
# service_desc / revision_desc parsers
# ---------------------------------------------------------------------------


def _revision_annotations(revision_desc: dict) -> dict:
    """The serving revision's annotations. run.v1 puts them on
    metadata; run.v2 uses a distinct annotations field on the
    revision itself. Both surfaces checked."""
    md_ann = (revision_desc.get("metadata") or {}).get("annotations") or {}
    top_ann = revision_desc.get("annotations") or {}
    merged = dict(md_ann)
    merged.update(top_ann)
    return merged


def _connector_annotation(revision_desc: dict) -> str:
    ann = _revision_annotations(revision_desc)
    val = ann.get("run.googleapis.com/vpc-access-connector", "")
    return val if isinstance(val, str) else ""


def _direct_vpc_subnet(revision_desc: dict) -> tuple[str, list[str]]:
    """Return `(subnet_or_empty, parse_errors)`. The
    `network-interfaces` annotation is a JSON array; each entry
    must be a dict with a non-empty `subnetwork` string. Any of the
    following fails:
      - annotation is not a JSON string (`"not-json"`, `123`, etc.)
      - annotation parses but is not a list
      - annotation is an empty list (`"[]"`)
      - any list entry is not a dict
      - any list entry lacks a non-empty `subnetwork` / `subnet`

    Also inspects `revision_desc.spec.vpc_access.network_interfaces`
    (run.v2 admin-API shape) as a fallback; the annotation is the
    canonical run.v1 surface.
    """
    parse_errors: list[str] = []
    ann = _revision_annotations(revision_desc)
    raw = ann.get("run.googleapis.com/network-interfaces")
    if raw is not None:
        if not isinstance(raw, str):
            parse_errors.append(
                f"network-interfaces annotation must be a string; "
                f"got {type(raw).__name__}"
            )
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                parse_errors.append(
                    f"network-interfaces annotation is not valid JSON: {exc}"
                )
                parsed = None
            if parsed is not None:
                if not isinstance(parsed, list):
                    parse_errors.append(
                        f"network-interfaces annotation must be a JSON list; "
                        f"got {type(parsed).__name__}"
                    )
                elif not parsed:
                    parse_errors.append(
                        "network-interfaces annotation is an empty list; "
                        "expected at least one interface with a subnetwork"
                    )
                else:
                    for i, ni in enumerate(parsed):
                        if not isinstance(ni, dict):
                            parse_errors.append(
                                f"network-interfaces[{i}] must be an object; "
                                f"got {type(ni).__name__}"
                            )
                            continue
                        subnet = ni.get("subnetwork") or ni.get("subnet")
                        if not isinstance(subnet, str) or not subnet.strip():
                            parse_errors.append(
                                f"network-interfaces[{i}].subnetwork must be a "
                                f"non-empty string; got {subnet!r}"
                            )
                            continue
                        # Found a valid subnet — return it.
                        return subnet, parse_errors

    # Fallback: run.v2 admin-API shape on the revision spec.
    spec = revision_desc.get("spec") or {}
    vpc_access = spec.get("vpc_access") or spec.get("vpcAccess") or {}
    nis = (
        vpc_access.get("network_interfaces")
        or vpc_access.get("networkInterfaces")
        or []
    )
    if isinstance(nis, list):
        for ni in nis:
            if not isinstance(ni, dict):
                continue
            subnet = ni.get("subnetwork") or ni.get("subnet")
            if isinstance(subnet, str) and subnet.strip():
                return subnet, parse_errors

    return "", parse_errors


def _redis_password_entry(revision_desc: dict) -> Optional[dict]:
    """Return the REDIS_PASSWORD env entry from the serving
    revision (dict as-in the manifest), or `None` when absent."""
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

    Returns the raw REDIS_ENABLED with its ORIGINAL type — no
    `str()` coercion. `_check_redis_enabled_literal` refuses
    non-string types explicitly."""
    entry = _named_env_entry(revision_desc, "REDIS_ENABLED")
    if entry is None:
        raw: Any = _MISSING
    elif "valueFrom" in entry:
        # Secret ref — REDIS_ENABLED must be a literal string, not
        # a secret. Sentinel so `_check_redis_enabled_literal` can
        # emit a distinct guard.
        raw = "<secretRef>"
    else:
        # Preserve the ORIGINAL type. If gcloud produced a JSON
        # integer 0, we get int 0 here — the check rejects it.
        raw = entry.get("value", _MISSING)
    return {"redis_enabled_raw": raw}, []


def _named_env_entry(revision_desc: dict, name: str) -> Optional[dict]:
    spec = revision_desc.get("spec") or {}
    containers = spec.get("containers") or []
    if not containers:
        return None
    for e in containers[0].get("env") or []:
        if isinstance(e, dict) and e.get("name") == name:
            return e
    return None


def _snapshot(
    revision_desc: dict,
    expected_redis: str,
    vpc_mode: Optional[str],
) -> dict[str, Any]:
    direct, _ = _direct_vpc_subnet(revision_desc)
    return {
        "expected_redis": expected_redis,
        "vpc_mode": vpc_mode,
        "connector_attached": bool(_connector_annotation(revision_desc)),
        "direct_vpc_subnet": direct or None,
        "redis_password_binding_present": _redis_password_entry(revision_desc) is not None,
    }
