"""Operator planner for the redis-fanout infrastructure config.

Defaults to OFFLINE desired-state preview. A real write requires:
  - `--project <id>` on the manifest allowlist;
  - `--apply`;
  - `--confirm "I understand this affects production infrastructure"`.

**Real writes are NOT enabled in this PR (task #136).** The
reviewer's directive: "Task #136 may now begin as review-only
work. Do not apply Cloud Run, Memorystore, VPC, CPU, secret, or
REDIS_ENABLED production changes yet." `--apply` therefore
returns rc=6 with a "planning + validation only" message — the
SDK snapshot + write paths land with task #137's enablement PR.

Runs the shared `validate.py` structural pass first; a failure
there produces a single refuse action and no other work is
planned.

Never prints Memorystore internal IPs, Secret Manager values,
or Cloud Run env values marked `kind=secret_or_absent`.
`render_plan` sanitises those fields — the value's key path is
printed but the value itself is redacted."""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        f"PyYAML required for apply.py — install via requirements-dev.txt "
        f"({exc!r})"
    )

sys.path.insert(0, str(Path(__file__).resolve().parent))
import infra_validate as _validate  # noqa: E402


HERE = Path(__file__).resolve().parent
CONFIRMATION_TOKEN = "I understand this affects production infrastructure"


@dataclass
class PlannedAction:
    kind: str            # "declared-config" | "refuse" | "static-check-failure"
    name: str
    reason: str = ""


@dataclass
class Plan:
    actions: list[PlannedAction] = field(default_factory=list)

    def add(self, action: PlannedAction) -> None:
        self.actions.append(action)

    def refusals(self) -> list[PlannedAction]:
        return [a for a in self.actions if a.kind == "refuse"]

    def has_refusals(self) -> bool:
        return bool(self.refusals())


def build_plan(
    *, project: str, manifest: dict,
) -> Plan:
    """Pure function — never contacts Google Cloud. Just enumerates
    what the manifest declares and confirms the static validator
    is green. Task #137's enablement PR is what turns this into a
    real diff against live infrastructure."""
    plan = Plan()

    if project not in manifest.get("allowed_projects", []):
        plan.add(PlannedAction(
            kind="refuse", name=project,
            reason=(
                f"project {project!r} is not on the manifest allowlist "
                f"{sorted(manifest.get('allowed_projects', []))!r}"
            ),
        ))
        return plan

    report = _validate.load_and_validate()
    if not report.ok:
        failed = "; ".join(f"{c.name}: {c.detail}" for c in report.failed())
        plan.add(PlannedAction(
            kind="refuse", name="static-checks",
            reason=f"validate.py failed: {failed}",
        ))
        return plan

    for resource_name in manifest.get("resources", {}).keys():
        plan.add(PlannedAction(
            kind="declared-config",
            name=resource_name,
            reason="declared in manifest; enablement PR (task #137) plans the diff",
        ))
    return plan


# Fields whose VALUES must never appear in printed output. Keys /
# paths CAN appear so the operator can locate the source.
_REDACT_FIELD_NAMES = frozenset({
    "REDIS_PASSWORD",
    "host_binding",
    "reserved_ip_range_placeholder",
})


def _redact(text: str) -> str:
    """Replace occurrences of secret-shaped tokens with a marker
    so a rendered plan cannot leak them through interpolation."""
    for name in _REDACT_FIELD_NAMES:
        # Redact colon-delimited "field: value" pairs and
        # `name=value` pairs. Keys stay visible; values become
        # `<redacted>`.
        text = re.sub(
            rf'({re.escape(name)}\s*[:=]\s*)("[^"]*"|[^\s,]+)',
            r"\1<redacted>", text,
        )
    return text


def render_plan(plan: Plan) -> str:
    lines: list[str] = ["Offline desired-state preview:"]
    for a in plan.actions:
        prefix = {
            "declared-config": "    resource ",
            "refuse":          "  ! REFUSE ",
        }.get(a.kind, f"  ? {a.kind} ")
        line = f"{prefix}{a.name}" + (f" — {a.reason}" if a.reason else "")
        lines.append(_redact(line))
    if not plan.actions:
        lines.append("  (nothing to preview)")
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="apply.py",
        description=(
            "Offline desired-state preview for the redis-fanout "
            "infrastructure. --apply currently exits rc=6."
        ),
    )
    p.add_argument(
        "--project", required=True,
        help="Google Cloud project ID. MUST be on manifest.allowed_projects.",
    )
    p.add_argument(
        "--apply", action="store_true",
        help="Perform writes. Not enabled in this PR — returns rc=6.",
    )
    p.add_argument(
        "--confirm", default="",
        help=(
            f"Required with --apply. Must be exactly "
            f"'{CONFIRMATION_TOKEN}'."
        ),
    )
    return p


def _load_manifest() -> dict:
    with _validate.MANIFEST_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _run(argv: list[str]) -> int:
    args = _build_arg_parser().parse_args(argv)
    manifest = _load_manifest()

    # ALWAYS run allowlist + static validation via build_plan
    # before any --apply decision. The earlier version returned
    # rc=6 before build_plan, which meant `--apply --project
    # unauthorized-project` bypassed the allowlist entirely.
    # Task #137 would inherit that unsafe control flow. The
    # ordering here is:
    #   rc=5 — --apply without correct --confirm (input error)
    #   rc=4 — allowlist / static-check refusal (config error)
    #   rc=6 — real writes intentionally deferred to task #137
    #   rc=0 — clean plan, no --apply
    if args.apply and args.confirm != CONFIRMATION_TOKEN:
        print(
            f"STOP: --apply requires --confirm exactly "
            f"{CONFIRMATION_TOKEN!r}. Refuse.",
            file=sys.stderr,
        )
        return 5

    plan = build_plan(project=args.project, manifest=manifest)

    if plan.has_refusals():
        print(render_plan(plan))
        return 4

    if args.apply:
        print(
            "STOP: this PR ships planning + validation only. "
            "--apply against real Google Cloud is gated on the next "
            "PR (task #137's enablement PR). See README.md.",
            file=sys.stderr,
        )
        return 6

    print(render_plan(plan))
    print("\n(offline preview only; pass --apply --confirm '...' — will exit rc=6)")
    return 0


def main() -> None:  # pragma: no cover
    sys.exit(_run(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
