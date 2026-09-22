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
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        f"PyYAML required for validate.py — install via requirements-dev.txt "
        f"({exc!r})"
    )


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
    vpc = loaded.get("vpc") or {}
    cloudrun = loaded.get("cloudrun") or {}
    active_mode = (vpc.get("metadata") or {}).get("active_mode") or ""
    if active_mode not in ("direct_egress", "serverless_connector"):
        return False, f"vpc.active_mode={active_mode!r} — expected direct_egress or serverless_connector"

    # Cloud Run's required_template_annotations must reference the
    # matching mode. Direct expects `all-traffic` +
    # `network-interfaces`; connector expects `all-traffic` + a
    # named connector.
    tpl = (
        (cloudrun.get("spec") or {})
        .get("template", {})
        .get("metadata", {})
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
    if active_mode == "direct_egress":
        nif = annot.get("run.googleapis.com/network-interfaces", {})
        if nif.get("value_from") != "vpc.direct_egress.network_interfaces_json":
            return False, (
                f"cloudrun network-interfaces annotation must be "
                f"`value_from: vpc.direct_egress.network_interfaces_json` "
                f"in direct-egress mode"
            )
        # And the vpc side must actually populate the JSON.
        direct = (vpc.get("spec") or {}).get("direct_egress") or {}
        if not direct.get("network_interfaces_json", "").strip():
            return False, "vpc.direct_egress.network_interfaces_json is empty"
        return True, ""
    # Serverless connector mode.
    conn = (vpc.get("spec") or {}).get("serverless_connector") or {}
    if conn.get("name", "").startswith("<") or not conn.get("name"):
        return False, "vpc.serverless_connector.name is still a placeholder"
    return True, ""


def _check_auth_matches_secret(loaded: dict[str, dict]) -> tuple[bool, str]:
    mem = (loaded.get("memorystore") or {}).get("spec") or {}
    sec = (loaded.get("secrets") or {}).get("spec") or {}
    auth_on = bool(mem.get("auth_enabled"))
    password_bound = bool(((sec.get("redis_password") or {}).get("present")))
    if auth_on != password_bound:
        return False, (
            f"memorystore.auth_enabled={auth_on!r} but "
            f"secrets.redis_password.present={password_bound!r} — pick one mode"
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
