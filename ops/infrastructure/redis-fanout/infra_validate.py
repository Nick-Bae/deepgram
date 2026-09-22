"""Structural validator for the redis-fanout infrastructure YAMLs.

Loads `manifest.yaml`, every declared resource YAML, and enforces
the invariants task #136 demands so an operator cannot land a
disagreement between (say) VPC mode and Cloud Run annotations
without CI flagging it first.

Purely offline — no Google Cloud call, no live-config lookup.
Verifying the SERVING revision matches these declarations lives
in `backend/scripts/gate2_helpers/` (v6 helpers) and runs at
§4d step 4 during a real deploy window.

Enforced here:

  - Each declared resource file exists, parses, and matches its
    manifest entry (kind, apiVersion).
  - Every entry in `manifest.pinned_constraints` finds its
    target field via a bounded JSONPath-lite lookup, and the
    field's actual value equals the manifest's `expected`. A
    drift OR a missing field fails closed.
  - `vpc_mode_matches_cloudrun_annotations` — the mode named in
    `vpc.active_mode` must be the mode whose block populates
    real values (as opposed to placeholder strings), AND
    cloudrun's `required_template_annotations` must reference
    the matching serialized network-interfaces JSON.
  - `memorystore_auth_matches_secret_binding` — AUTH on
    ⇔ redis_password.present on.
  - `cloudrun_redis_host_matches_memorystore_binding` — the
    cloudrun REDIS_HOST env's `value_from` must equal
    `memorystore.host_binding`.
  - `transit_encryption_matches_client_ssl_kwarg` — if TLS is
    turned on, the client ssl kwarg would also need to change;
    this PR keeps the value at DISABLED and any drift fails
    the check (a follow-up PR flips both together).

Usable as a CLI (`python validate.py`) or a library
(`load_and_validate() -> Report`)."""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        f"PyYAML required for infra_validate.py — install via "
        f"requirements-dev.txt ({exc!r})"
    )


# Memorystore knobs that MUST be present and MUST equal these
# values. `tier` in particular is a fail-closed target: Basic
# tier lacks the HA replication PR #31 §2's 15 s failover budget
# assumed.
_MEMORYSTORE_TIER_ALLOWED = {"STANDARD_HA"}
_MEMORYSTORE_REDIS_VERSION_ALLOWED = {"REDIS_7_2"}
_MEMORYSTORE_REGION_ALLOWED = {"us-central1"}
_MEMORYSTORE_MEMORY_MIN_GB = 1
_MEMORYSTORE_MEMORY_MAX_GB = 32
_MEMORYSTORE_TRANSIT_ENCRYPTION_ALLOWED = {"DISABLED"}


HERE = Path(__file__).resolve().parent
MANIFEST_PATH = HERE / "manifest.yaml"

# JSONPath-lite pattern for pinned_constraints:
#   `.field.subfield[?(@.name=='X')].value`
# Matches a chain of dotted keys, with optional filter clauses
# of the form `[?(@.name=='X')]` selecting a list element by
# equality on the `name` field. `[0]` selects the first element.
_PATH_SEGMENT_RE = re.compile(
    r"""
    \.                    # separator
    (?P<field>[A-Za-z_][A-Za-z0-9_]*)
    (?:\[(?P<index>\d+)\])?              # optional [N]
    (?:\[\?\(@\.name=='(?P<name>[^']*)'\)\])?  # optional filter
    """,
    re.VERBOSE,
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name=name, ok=ok, detail=detail))

    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def print_summary(self) -> None:
        for c in self.checks:
            flag = "OK  " if c.ok else "FAIL"
            print(f"[{flag}] {c.name}" + (f" — {c.detail}" if c.detail else ""))
        if self.ok:
            print(f"\n{len(self.checks)} checks passed.")
        else:
            failed = self.failed()
            print(f"\n{len(failed)}/{len(self.checks)} FAILED:")
            for c in failed:
                print(f"  - {c.name}: {c.detail}")


class PathNotFound(Exception):
    """Raised by `_lookup_path` when a manifest path cannot be resolved."""


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return data


def _lookup_path(root: Any, path: str) -> Any:
    """Resolve a manifest constraint path against a loaded YAML
    tree. Bounded — only supports the shapes the manifest actually
    uses. An unresolvable path raises `PathNotFound` so pinned-
    constraint checks fail loud when a rename orphans a
    reference."""
    current: Any = root
    # Allow the leading segment to omit the '.' so paths can be
    # written naturally (`spec.max_instances` rather than
    # `.spec.max_instances`).
    remaining = path if path.startswith(".") else "." + path
    while remaining:
        m = _PATH_SEGMENT_RE.match(remaining)
        if not m:
            raise PathNotFound(f"cannot parse path segment: {remaining!r}")
        remaining = remaining[m.end():]
        field_name = m.group("field")
        if not isinstance(current, dict) or field_name not in current:
            raise PathNotFound(
                f"key {field_name!r} not found in {type(current).__name__}"
            )
        current = current[field_name]
        idx = m.group("index")
        name_filter = m.group("name")
        if idx is not None:
            if not isinstance(current, list):
                raise PathNotFound(
                    f"{field_name}[{idx}] expected list, got {type(current).__name__}"
                )
            try:
                current = current[int(idx)]
            except IndexError:
                raise PathNotFound(f"{field_name}[{idx}] out of range")
        if name_filter is not None:
            if not isinstance(current, list):
                raise PathNotFound(
                    f"{field_name}[?(@.name=='{name_filter}')] expected list"
                )
            match = None
            for item in current:
                if isinstance(item, dict) and item.get("name") == name_filter:
                    match = item
                    break
            if match is None:
                raise PathNotFound(
                    f"{field_name} has no entry with name={name_filter!r}"
                )
            current = match
    return current


def load_and_validate() -> Report:
    """Parse + validate the whole infrastructure config tree."""
    report = Report()

    if not MANIFEST_PATH.exists():
        report.add("manifest.exists", False, f"{MANIFEST_PATH} missing")
        return report
    report.add("manifest.exists", True)

    manifest = _load_yaml(MANIFEST_PATH)
    _validate_manifest_shape(manifest, report)

    resources = manifest.get("resources", {})
    loaded: dict[str, dict] = {}
    for name, meta in resources.items():
        file_rel = meta.get("file", "")
        path = HERE / file_rel
        exists = path.exists()
        report.add(f"resource.file_exists:{name}", exists, f"{path}")
        if not exists:
            continue
        try:
            data = _load_yaml(path)
        except Exception as exc:
            report.add(f"resource.parse:{name}", False, repr(exc))
            continue
        report.add(f"resource.parse:{name}", True)
        loaded[name] = data

        expected_kind = meta.get("kind")
        expected_api = meta.get("apiVersion")
        report.add(
            f"resource.kind_matches:{name}",
            data.get("kind") == expected_kind,
            f"YAML kind={data.get('kind')!r}, manifest expects {expected_kind!r}",
        )
        report.add(
            f"resource.apiVersion_matches:{name}",
            data.get("apiVersion") == expected_api,
            f"YAML apiVersion={data.get('apiVersion')!r}, manifest expects {expected_api!r}",
        )

    _validate_pinned_constraints(manifest, loaded, report)
    _validate_cross_resource_invariants(manifest, loaded, report)
    return report


def _validate_manifest_shape(manifest: dict, report: Report) -> None:
    for key in (
        "version", "service_name", "region",
        "resources", "pinned_constraints",
        "cross_resource_invariants", "allowed_projects",
    ):
        report.add(
            f"manifest.has:{key}",
            key in manifest,
            f"top-level key {key!r} missing",
        )


def _validate_pinned_constraints(
    manifest: dict, loaded: dict[str, dict], report: Report,
) -> None:
    for i, constraint in enumerate(manifest.get("pinned_constraints", []) or []):
        resource_name = constraint.get("resource", "")
        path = constraint.get("path", "")
        expected = constraint.get("expected")
        check_name = f"pinned:{resource_name}:{path}"
        data = loaded.get(resource_name)
        if data is None:
            report.add(
                check_name, False,
                f"resource {resource_name!r} not loaded — cannot evaluate",
            )
            continue
        try:
            actual = _lookup_path(data, path)
        except PathNotFound as exc:
            report.add(check_name, False, f"path lookup failed: {exc}")
            continue
        report.add(
            check_name,
            actual == expected,
            f"actual={actual!r}, expected={expected!r}",
        )


def _validate_cross_resource_invariants(
    manifest: dict, loaded: dict[str, dict], report: Report,
) -> None:
    invariants = {
        "vpc_mode_matches_cloudrun_annotations":
            _check_vpc_matches_cloudrun,
        "memorystore_auth_matches_secret_binding":
            _check_auth_matches_secret,
        "cloudrun_redis_host_matches_memorystore_binding":
            _check_host_matches_memorystore,
        "transit_encryption_matches_client_ssl_kwarg":
            _check_transit_encryption,
        "memorystore_declaration_is_fail_closed":
            _check_memorystore_declaration,
        "cloudrun_redis_password_binding_is_valid":
            _check_cloudrun_redis_password_binding,
    }
    for inv in manifest.get("cross_resource_invariants", []) or []:
        name = inv.get("name", "")
        checker = invariants.get(name)
        if checker is None:
            report.add(
                f"invariant:{name}", False,
                f"no checker registered for invariant {name!r} — "
                f"validate.py must be extended when a new invariant "
                f"is added to the manifest",
            )
            continue
        try:
            ok, detail = checker(loaded)
        except Exception as exc:
            report.add(f"invariant:{name}", False, f"checker raised: {exc!r}")
            continue
        report.add(f"invariant:{name}", ok, detail)


def _check_vpc_matches_cloudrun(loaded: dict[str, dict]) -> tuple[bool, str]:
    """Deep validation of the VPC ↔ Cloud Run agreement.

    Per PR #40 round-2 reviewer feedback: the earlier version only
    checked that Direct-mode JSON was nonempty. Task #136's whole
    point is preventing partial-mode-switch failures — every
    corner must fail closed."""
    vpc = loaded.get("vpc") or {}
    cloudrun = loaded.get("cloudrun") or {}
    active_mode = (vpc.get("metadata") or {}).get("active_mode") or ""
    if active_mode not in ("direct_egress", "serverless_connector"):
        return False, (
            f"vpc.active_mode={active_mode!r} — expected "
            f"'direct_egress' or 'serverless_connector'"
        )

    spec = vpc.get("spec") or {}
    declared_network = spec.get("network")
    declared_subnet = spec.get("subnet")
    if not (isinstance(declared_network, str) and declared_network):
        return False, "vpc.spec.network is missing or not a string"
    if not (isinstance(declared_subnet, str) and declared_subnet):
        return False, "vpc.spec.subnet is missing or not a string"

    # Cloud Run's required_template_annotations. Both modes need
    # `vpc-access-egress`; the OTHER annotation depends on mode.
    tpl = (
        (cloudrun.get("spec") or {})
        .get("template", {}).get("metadata", {})
        .get("required_template_annotations", [])
    )
    annot = {a.get("key"): a for a in tpl if isinstance(a, dict)}
    egress = annot.get("run.googleapis.com/vpc-access-egress", {})
    egress_value = egress.get("value")
    if egress_value != "all-traffic":
        return False, (
            f"cloudrun vpc-access-egress annotation={egress_value!r} — "
            f"expected 'all-traffic' for either mode"
        )

    has_network_interfaces_annotation = (
        "run.googleapis.com/network-interfaces" in annot
    )
    has_connector_annotation = (
        "run.googleapis.com/vpc-access-connector" in annot
    )

    direct = spec.get("direct_egress") or {}
    connector = spec.get("serverless_connector") or {}
    direct_populated = _direct_egress_is_populated(direct)
    connector_populated = _serverless_connector_is_populated(connector)

    if direct_populated and connector_populated:
        return False, (
            "vpc.spec both direct_egress and serverless_connector are "
            "populated with real values — pick one mode per rollout"
        )

    if active_mode == "direct_egress":
        # Cloud Run must reference the direct-egress annotation and
        # MUST NOT carry a serverless connector annotation.
        if not has_network_interfaces_annotation:
            return False, (
                "cloudrun template is missing "
                "`run.googleapis.com/network-interfaces` annotation "
                "required by direct-egress mode"
            )
        if has_connector_annotation:
            return False, (
                "cloudrun template carries a "
                "`run.googleapis.com/vpc-access-connector` annotation "
                "while active_mode=direct_egress — a partial switch "
                "would leave BOTH paths configured. Remove the "
                "connector annotation."
            )
        nif = annot.get("run.googleapis.com/network-interfaces", {})
        if nif.get("value_from") != "vpc.direct_egress.network_interfaces_json":
            return False, (
                "cloudrun network-interfaces annotation must be "
                "`value_from: vpc.direct_egress.network_interfaces_json`"
            )
        if not direct_populated:
            return False, (
                "vpc.direct_egress is empty in direct-egress mode"
            )
        # Parse the JSON and cross-check network/subnetwork against
        # vpc.spec.network / vpc.spec.subnet. A mismatch here would
        # mean the operator wrote two different subnets across the
        # file — exactly the partial-mode-switch failure task #136
        # exists to prevent.
        try:
            parsed = json.loads(direct["network_interfaces_json"])
        except Exception as exc:
            return False, (
                f"vpc.direct_egress.network_interfaces_json is not "
                f"valid JSON: {exc}"
            )
        if not isinstance(parsed, list) or not parsed:
            return False, (
                "vpc.direct_egress.network_interfaces_json must be a "
                "non-empty JSON array"
            )
        first = parsed[0]
        if not isinstance(first, dict):
            return False, (
                "vpc.direct_egress.network_interfaces_json[0] must be "
                "an object"
            )
        if first.get("network") != declared_network:
            return False, (
                f"vpc.direct_egress.network_interfaces_json[0].network"
                f"={first.get('network')!r} does not match "
                f"vpc.spec.network={declared_network!r}"
            )
        if first.get("subnetwork") != declared_subnet:
            return False, (
                f"vpc.direct_egress.network_interfaces_json[0]."
                f"subnetwork={first.get('subnetwork')!r} does not match "
                f"vpc.spec.subnet={declared_subnet!r}"
            )
        if direct.get("egress") != "all-traffic":
            return False, (
                f"vpc.direct_egress.egress={direct.get('egress')!r} — "
                f"expected 'all-traffic'"
            )
        if direct.get("private_google_access") is not True:
            return False, (
                "vpc.direct_egress.private_google_access must be true "
                "so Cloud Run can still reach Firestore / Cloud Logging "
                "after the VPC attach"
            )
        return True, ""

    # Serverless connector mode.
    if not has_connector_annotation:
        return False, (
            "cloudrun template is missing "
            "`run.googleapis.com/vpc-access-connector` annotation "
            "required by serverless-connector mode"
        )
    if has_network_interfaces_annotation:
        return False, (
            "cloudrun template carries a "
            "`run.googleapis.com/network-interfaces` annotation while "
            "active_mode=serverless_connector — remove it"
        )
    if not connector_populated:
        return False, (
            "vpc.serverless_connector is empty in serverless-connector mode"
        )
    if connector.get("egress") != "all-traffic":
        return False, (
            f"vpc.serverless_connector.egress={connector.get('egress')!r} "
            f"— expected 'all-traffic'"
        )
    # The connector annotation MUST bind to
    # `vpc.serverless_connector.name` via value_from. An empty or
    # wrong literal `value` here previously slipped through
    # unchecked — a partial-mode-switch failure the reviewer
    # flagged in PR #40 round-3.
    conn_annot = annot["run.googleapis.com/vpc-access-connector"]
    conn_value_from = conn_annot.get("value_from")
    conn_literal = conn_annot.get("value")
    if conn_value_from != "vpc.serverless_connector.name":
        return False, (
            f"cloudrun `run.googleapis.com/vpc-access-connector` "
            f"annotation must be `value_from: "
            f"vpc.serverless_connector.name` — got value_from="
            f"{conn_value_from!r}, value={conn_literal!r}"
        )
    if conn_literal is not None:
        return False, (
            f"cloudrun `run.googleapis.com/vpc-access-connector` "
            f"annotation carries a literal `value={conn_literal!r}` in "
            f"addition to value_from — remove the literal so the source "
            f"of truth stays in vpc.serverless_connector.name"
        )
    return True, ""


def _direct_egress_is_populated(block: dict) -> bool:
    """The direct-egress block is populated with real values if
    `network_interfaces_json` is a non-empty string that doesn't
    look like a placeholder."""
    j = block.get("network_interfaces_json") or ""
    if not isinstance(j, str) or not j.strip():
        return False
    return "<" not in j  # no `<placeholder>` tokens


def _serverless_connector_is_populated(block: dict) -> bool:
    name = block.get("name") or ""
    if not isinstance(name, str) or not name.strip():
        return False
    return not name.startswith("<")


def _check_auth_matches_secret(loaded: dict[str, dict]) -> tuple[bool, str]:
    """Strict-typed AUTH ↔ secret binding pairing.

    The earlier `bool(...)` coercion accepted `"false"` (string)
    or `0` (int) as valid Booleans and would let a typo pass —
    the reviewer's PR #40 round-2 blocker. Require literal
    Python `True` / `False` on BOTH sides."""
    mem = (loaded.get("memorystore") or {}).get("spec") or {}
    sec = (loaded.get("secrets") or {}).get("spec") or {}

    auth = mem.get("auth_enabled")
    if not isinstance(auth, bool):
        return False, (
            f"memorystore.spec.auth_enabled={auth!r} — must be a "
            f"YAML boolean literal (`true` / `false`), not a string "
            f"or integer"
        )

    pw = (sec.get("redis_password") or {})
    present = pw.get("present")
    if not isinstance(present, bool):
        return False, (
            f"secrets.spec.redis_password.present={present!r} — must "
            f"be a YAML boolean literal"
        )
    if auth != present:
        return False, (
            f"memorystore.auth_enabled={auth!r} but "
            f"secrets.redis_password.present={present!r} — pick one mode"
        )

    # When AUTH is on, the full secret binding must be declared
    # AND the top-level `secret_name`/`version` must match the
    # secretKeyRef `name`/`key` — a mismatch means the operator
    # copy-pasted from a different secret and the Cloud Run env
    # would reference a version that does not exist.
    if auth:
        skref = pw.get("secret_key_ref")
        if not isinstance(skref, dict):
            return False, (
                "secrets.spec.redis_password.secret_key_ref is missing "
                "or not a mapping — Cloud Run's secretKeyRef binding "
                "needs `name` and `key`"
            )
        for key in ("name", "key"):
            v = skref.get(key)
            if not (isinstance(v, str) and v.strip()):
                return False, (
                    f"secrets.spec.redis_password.secret_key_ref.{key}"
                    f"={v!r} is missing or not a non-empty string"
                )
        for key in ("secret_name", "version"):
            v = pw.get(key)
            if not (isinstance(v, str) and v.strip()):
                return False, (
                    f"secrets.spec.redis_password.{key}={v!r} is "
                    f"missing or not a non-empty string"
                )
        # Cross-field agreement (reviewer's PR #40 round-3 blocker):
        # secret_name → secret_key_ref.name; version → secret_key_ref.key.
        if pw["secret_name"] != skref["name"]:
            return False, (
                f"secrets.spec.redis_password.secret_name="
                f"{pw['secret_name']!r} does not match "
                f"secret_key_ref.name={skref['name']!r} — they must "
                f"reference the same Secret Manager secret"
            )
        if pw["version"] != skref["key"]:
            return False, (
                f"secrets.spec.redis_password.version={pw['version']!r} "
                f"does not match secret_key_ref.key={skref['key']!r} — "
                f"the Cloud Run env would resolve a different version "
                f"than the operator documented"
            )

    return True, ""


def _check_cloudrun_redis_password_binding(loaded: dict[str, dict]) -> tuple[bool, str]:
    """End-to-end binding check for the REDIS_PASSWORD env on
    Cloud Run against secrets.yaml. Reviewer's PR #40 round-3
    blocker: a plaintext value here, or a `value_from` that
    references something OTHER than the managed secret block,
    would have passed the earlier validator."""
    envs = (
        (loaded.get("cloudrun") or {}).get("spec", {})
        .get("template", {}).get("spec", {})
        .get("containers", [{}])[0].get("env", [])
    )
    # Reviewer's PR #40 round-3 nonblocking hardening: exactly ONE
    # REDIS_PASSWORD entry — a stale duplicate would let the first
    # match govern the binding while the second silently overrode
    # it on the running revision. Cloud Run's env array is
    # order-sensitive; a duplicate is always a bug.
    matches = [
        e for e in envs
        if isinstance(e, dict) and e.get("name") == "REDIS_PASSWORD"
    ]
    if len(matches) == 0:
        return False, "cloudrun env missing REDIS_PASSWORD"
    if len(matches) > 1:
        return False, (
            f"cloudrun env contains {len(matches)} REDIS_PASSWORD "
            f"entries — must be exactly 1. A duplicate would let the "
            f"first entry pass validation while the second silently "
            f"overrode it on the running revision"
        )
    redis_password = matches[0]

    kind = redis_password.get("kind")
    if kind != "secret_or_absent":
        return False, (
            f"cloudrun REDIS_PASSWORD.kind={kind!r} — must be "
            f"'secret_or_absent' so the value is sourced from Secret "
            f"Manager (never a literal in the manifest)"
        )
    if "value" in redis_password:
        return False, (
            f"cloudrun REDIS_PASSWORD carries a literal `value` field "
            f"({redis_password.get('value')!r}) — remove it. The Secret "
            f"Manager binding is the source of truth."
        )
    value_from = redis_password.get("value_from")
    if value_from != "secrets.redis_password":
        return False, (
            f"cloudrun REDIS_PASSWORD.value_from={value_from!r} — must "
            f"be 'secrets.redis_password' so the env resolves to the "
            f"declared secret binding, not some other block"
        )
    return True, ""


def _check_memorystore_declaration(loaded: dict[str, dict]) -> tuple[bool, str]:
    """Fail-closed validation of every Memorystore knob PR #31
    §4c step 3 requires. Not a cross-resource invariant per se,
    but sits alongside them so a single failure shows up in the
    same `invariant:...` name space in the report."""
    mem = (loaded.get("memorystore") or {}).get("spec") or {}
    if not mem:
        return False, "memorystore.spec block is missing"

    tier = mem.get("tier")
    if tier not in _MEMORYSTORE_TIER_ALLOWED:
        return False, (
            f"memorystore.spec.tier={tier!r} — must be one of "
            f"{sorted(_MEMORYSTORE_TIER_ALLOWED)!r} (Basic-tier lacks "
            f"the HA replication PR #31 §2's 15 s failover budget "
            f"assumed)"
        )

    version = mem.get("redis_version")
    if version not in _MEMORYSTORE_REDIS_VERSION_ALLOWED:
        return False, (
            f"memorystore.spec.redis_version={version!r} — must be one "
            f"of {sorted(_MEMORYSTORE_REDIS_VERSION_ALLOWED)!r}"
        )

    region = (loaded.get("memorystore") or {}).get("metadata", {}).get("region")
    if region not in _MEMORYSTORE_REGION_ALLOWED:
        return False, (
            f"memorystore.metadata.region={region!r} — must be one of "
            f"{sorted(_MEMORYSTORE_REGION_ALLOWED)!r}"
        )

    memsize = mem.get("memory_size_gb")
    # `isinstance(True, int)` is True in Python — explicitly
    # reject bools so `memory_size_gb: true` cannot pass. The
    # reviewer's PR #40 round-3 "small hardening item".
    if isinstance(memsize, bool) or not isinstance(memsize, int) \
            or memsize < _MEMORYSTORE_MEMORY_MIN_GB \
            or memsize > _MEMORYSTORE_MEMORY_MAX_GB:
        return False, (
            f"memorystore.spec.memory_size_gb={memsize!r} — must be an "
            f"int (bool excluded) in [{_MEMORYSTORE_MEMORY_MIN_GB}, "
            f"{_MEMORYSTORE_MEMORY_MAX_GB}]"
        )

    mode = mem.get("transit_encryption_mode")
    if mode not in _MEMORYSTORE_TRANSIT_ENCRYPTION_ALLOWED:
        # Duplicate-of `_check_transit_encryption` but pinned at
        # the declaration layer too. The other check enforces the
        # client-ssl pairing constraint on a change.
        return False, (
            f"memorystore.spec.transit_encryption_mode={mode!r} — "
            f"must be one of {sorted(_MEMORYSTORE_TRANSIT_ENCRYPTION_ALLOWED)!r} "
            f"until the client ssl kwarg changes"
        )

    if not mem.get("host_binding"):
        return False, (
            "memorystore.spec.host_binding is empty — needed so "
            "cloudrun's REDIS_HOST env can reference it via value_from"
        )

    return True, ""


def _check_host_matches_memorystore(loaded: dict[str, dict]) -> tuple[bool, str]:
    envs = (
        (loaded.get("cloudrun") or {}).get("spec", {})
        .get("template", {}).get("spec", {})
        .get("containers", [{}])[0].get("env", [])
    )
    redis_host = next(
        (e for e in envs if isinstance(e, dict) and e.get("name") == "REDIS_HOST"),
        None,
    )
    if redis_host is None:
        return False, "cloudrun env missing REDIS_HOST"
    value_from = redis_host.get("value_from") or ""
    if value_from != "memorystore.host_binding":
        return False, (
            f"REDIS_HOST value_from={value_from!r} — expected "
            f"'memorystore.host_binding'"
        )
    binding = ((loaded.get("memorystore") or {}).get("spec") or {}).get("host_binding")
    if not binding:
        return False, "memorystore.spec.host_binding is empty"
    return True, ""


def _check_transit_encryption(loaded: dict[str, dict]) -> tuple[bool, str]:
    mem = (loaded.get("memorystore") or {}).get("spec") or {}
    mode = mem.get("transit_encryption_mode")
    if mode == "DISABLED":
        return True, ""
    return False, (
        f"memorystore.transit_encryption_mode={mode!r} — the client "
        f"currently connects with ssl=False; a switch to "
        f"SERVER_AUTHENTICATION must land in the same commit as "
        f"the client-side ssl=True (see redis_pubsub.py:start)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Structural validator for ops/infrastructure/redis-fanout.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Only print the failed checks.",
    )
    args = parser.parse_args()
    report = load_and_validate()
    if args.quiet:
        for c in report.failed():
            print(f"FAIL {c.name}: {c.detail}")
        if report.ok:
            print("OK — all checks passed.")
    else:
        report.print_summary()
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
