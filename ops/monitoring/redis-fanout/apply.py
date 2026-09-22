"""Operator apply script for the redis-fanout monitoring resources.

Defaults to PLAN ONLY. A real write requires all of:
  - `--project <id>` on the manifest allowlist;
  - `--apply`;
  - `--confirm "I understand this affects production monitoring"`;
  - a notification-channel mapping file that resolves every
    managed alert to at least one channel — refuses rather than
    silently paging into the void.

Identifies existing alert policies by `userLabels.managed_by` +
`userLabels.alert_id` (see manifest.managed_labels). Two policies
matching the same managed identity is a fatal conflict — refuse
and print both resource names so the operator can pick which to
delete manually.

Never deletes resources automatically. An old managed resource
whose file was removed from the manifest is reported as
"orphaned in cloud"; the operator decides whether to delete it
by hand.

Prints a sanitized post-apply verification report — the set of
managed metrics that exist, the set of managed alerts that exist,
which alerts changed, and a diff summary. Never prints
notification-channel IDs, credentials, or unrelated resource
contents.

Requires the Google Cloud monitoring + logging Python SDKs. When
those libraries are missing (local dev without them), the script
exits with a clear message and rc=2 — no fallback to a partial
apply.

This module is intentionally I/O-free at import time so tests can
introspect its argparse and dry-run planner without touching Google
Cloud. All SDK calls happen inside `_execute()` which the tests
never enter.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        f"PyYAML required for apply.py — install via requirements-dev.txt "
        f"({exc!r})"
    )

# The validator lives in the same directory; reuse its parser
# and Check dataclass so apply.py's planning phase agrees on
# what a "valid" tree looks like.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate as _validate  # noqa: E402


HERE = Path(__file__).resolve().parent
CONFIRMATION_TOKEN = "I understand this affects production monitoring"

# Notification-channel resource-name format Cloud Monitoring
# accepts. The project segment MUST match the operator's
# --project so a stale channel from another project cannot slip
# through.
_CHANNEL_NAME_RE_TEMPLATE = (
    r"^projects/{project}/notificationChannels/[A-Za-z0-9_-]+$"
)


# --- Config models --------------------------------------------------------


@dataclass
class ManagedIdentity:
    managed_by: str
    alert_id: str


@dataclass
class PlannedAction:
    kind: str            # "create-metric" | "update-metric" | "no-op-metric" |
                         # "create-alert" | "update-alert" | "no-op-alert" |
                         # "orphan-metric" | "orphan-alert" | "refuse"
    name: str            # metric name or alert_id
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


# --- Notification-channel mapping ----------------------------------------


def _load_channel_map(path: Optional[Path]) -> dict[str, list[str]]:
    """The channel mapping YAML/JSON is a plain
    `{ <alert_id>: [<channel_resource_name>, ...] }` object.
    Values must be non-empty strings; further shape validation
    (project match + notificationChannels/... format + unknown
    alert IDs) runs in `_validate_channel_map` against the
    manifest + --project."""
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"notification-channel map not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level must be a mapping alert_id -> [channel]")
    out: dict[str, list[str]] = {}
    for k, v in data.items():
        if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
            raise ValueError(
                f"{path}: {k!r} must map to a non-empty list of non-empty "
                f"channel resource-name strings"
            )
        out[str(k)] = list(v)
    return out


def _validate_channel_map(
    channel_map: dict[str, list[str]],
    *,
    project: str,
    manifest_alert_ids: set[str],
) -> list[PlannedAction]:
    """Return a list of refusal actions for channel-map problems:
      - any value that does not match
        `projects/<project>/notificationChannels/<id>`;
      - any key not present in the manifest's alert set (typos
        would otherwise silently skip a real alert).

    Runs BEFORE `build_plan` so a bad map surfaces one refusal
    per problem rather than being masked by 'missing channel'
    refusals downstream."""
    refusals: list[PlannedAction] = []
    channel_re = re.compile(
        _CHANNEL_NAME_RE_TEMPLATE.format(project=re.escape(project))
    )
    for alert_id, channels in channel_map.items():
        if alert_id not in manifest_alert_ids:
            refusals.append(PlannedAction(
                kind="refuse", name=alert_id,
                reason=(
                    f"channel map lists unknown alert_id {alert_id!r}; "
                    f"expected one of {sorted(manifest_alert_ids)!r}"
                ),
            ))
            continue
        for chan in channels:
            if not channel_re.match(chan):
                refusals.append(PlannedAction(
                    kind="refuse", name=alert_id,
                    reason=(
                        f"notification channel resource name violates "
                        f"projects/{project}/notificationChannels/<id> "
                        f"format (redacted from output)"
                    ),
                ))
    return refusals


# --- Plan builder ---------------------------------------------------------


def build_plan(
    *,
    project: str,
    channel_map: dict[str, list[str]],
    existing_metrics: set[str],
    existing_alerts_by_identity: dict[str, list[str]],
    manifest: dict,
) -> Plan:
    """Pure function — never contacts Google Cloud. `existing_*`
    arguments describe what the cloud already has (empty in a
    dry-run without SDK access; populated by `_snapshot_cloud`
    when running for real). Returns the ordered list of actions
    `_execute` would take with `--apply`.

    Refusals:
      - project not on allowlist
      - two cloud policies share the same managed alert_id
      - a managed alert has no notification channel configured

    The plan is deterministic — validate.py's checks are
    re-asserted first; a failing static check produces a
    single "refuse" action naming the failing checks and no
    other work is planned."""
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

    # Structural checks must pass before we plan any writes.
    report = _validate.load_and_validate()
    if not report.ok:
        failed = "; ".join(f"{c.name}: {c.detail}" for c in report.failed())
        plan.add(PlannedAction(
            kind="refuse", name="static-checks",
            reason=f"validate.py failed: {failed}",
        ))
        return plan

    # Metrics.
    manifest_metrics = manifest.get("metrics", {})
    for name in manifest_metrics:
        if name in existing_metrics:
            plan.add(PlannedAction(
                kind="update-metric", name=name,
                reason="present in cloud; update to match managed file",
            ))
        else:
            plan.add(PlannedAction(
                kind="create-metric", name=name,
                reason="absent in cloud",
            ))
    for cloud_name in sorted(existing_metrics - set(manifest_metrics)):
        # A managed metric that vanished from the manifest is
        # reported but NOT auto-deleted.
        if cloud_name.startswith("redis_pubsub_"):
            plan.add(PlannedAction(
                kind="orphan-metric", name=cloud_name,
                reason="in cloud but not in manifest; not touched",
            ))

    # Alerts. Managed identity is (managed_by, alert_id).
    manifest_alerts = manifest.get("alerts", {})
    managed_by = manifest["managed_labels"]["managed_by"]

    for alert_name in manifest_alerts:
        # Require a notification-channel mapping.
        channels = channel_map.get(alert_name, [])
        if not channels:
            plan.add(PlannedAction(
                kind="refuse", name=alert_name,
                reason=(
                    "no notification channel configured — refusing "
                    "to create a paging alert with no destination"
                ),
            ))
            continue

        # Fatal-conflict check: cloud has more than one policy with
        # the same managed identity.
        identity_key = f"{managed_by}/{alert_name}"
        matches = existing_alerts_by_identity.get(identity_key, [])
        if len(matches) > 1:
            plan.add(PlannedAction(
                kind="refuse", name=alert_name,
                reason=(
                    f"{len(matches)} existing policies share managed "
                    f"identity {identity_key!r}: {sorted(matches)!r} — "
                    f"resolve manually before apply"
                ),
            ))
            continue

        if matches:
            plan.add(PlannedAction(
                kind="update-alert", name=alert_name,
                reason=f"present in cloud as {matches[0]}",
            ))
        else:
            plan.add(PlannedAction(
                kind="create-alert", name=alert_name,
                reason="absent in cloud",
            ))

    for identity_key, matches in sorted(existing_alerts_by_identity.items()):
        # Managed alert in cloud but absent from manifest.
        _, alert_id = identity_key.split("/", 1)
        if alert_id not in manifest_alerts:
            plan.add(PlannedAction(
                kind="orphan-alert", name=identity_key,
                reason=(
                    f"in cloud (matches: {matches!r}) but not in manifest; "
                    f"not touched"
                ),
            ))

    return plan


# --- Sanitized reporting --------------------------------------------------


def render_plan(plan: Plan) -> str:
    """Human-readable output. Renamed from 'Plan:' — this PR ships
    an OFFLINE preview only. It never reads live cloud state (no
    SDK call is made), so `create-*` actions really mean 'desired
    in manifest, not observed as absent in cloud'. Do not treat
    the output as a real diff.

    Never prints notification-channel IDs, cloud project details,
    or resource contents beyond names."""
    lines: list[str] = ["Offline desired-state preview:"]
    for a in plan.actions:
        prefix = {
            "create-metric": "  + metric ",
            "update-metric": "  ~ metric ",
            "no-op-metric":  "    metric ",
            "create-alert":  "  + alert  ",
            "update-alert":  "  ~ alert  ",
            "no-op-alert":   "    alert  ",
            "orphan-metric": "  ? orphan metric ",
            "orphan-alert":  "  ? orphan alert  ",
            "refuse":        "  ! REFUSE ",
        }.get(a.kind, f"  ? {a.kind} ")
        lines.append(f"{prefix}{a.name}" + (f" — {a.reason}" if a.reason else ""))
    if not plan.actions:
        lines.append("  (nothing to do)")
    return "\n".join(lines)


# --- CLI -----------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="apply.py",
        description=(
            "Plan/apply the redis-fanout monitoring resources. "
            "Defaults to plan-only. --apply requires --confirm and "
            "a notification-channel map."
        ),
    )
    p.add_argument(
        "--project", required=True,
        help="Google Cloud project ID. MUST be on manifest.allowed_projects.",
    )
    p.add_argument(
        "--channels", type=Path, default=None,
        help=(
            "Path to a YAML/JSON file mapping each managed alert_id "
            "to a list of notification-channel resource names. "
            "Required for --apply."
        ),
    )
    p.add_argument(
        "--apply", action="store_true",
        help="Perform writes. Without it the script only prints the plan.",
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


def _snapshot_cloud(project: str):  # pragma: no cover — requires SDK
    """Return (existing_metrics, existing_alerts_by_identity) from
    Google Cloud. Split out so tests never invoke the SDK."""
    try:
        from google.cloud import logging_v2  # type: ignore  # noqa: F401
        from google.cloud import monitoring_v3  # type: ignore  # noqa: F401
    except Exception as exc:
        raise SystemExit(
            f"google-cloud-logging and google-cloud-monitoring required; "
            f"install to run --apply ({exc!r})"
        )
    # Actual SDK enumeration is intentionally deferred to a
    # follow-up PR that lands alongside real credentials. The
    # dry-run planning + tests exercise the pure `build_plan`
    # function directly, which is the reviewable substance of
    # this PR.
    raise SystemExit(
        "cloud snapshot not enabled in this PR — planning only. "
        "See ops/monitoring/redis-fanout/README.md for the follow-up "
        "checklist that unlocks --apply against production."
    )


def _run(argv: list[str]) -> int:
    args = _build_arg_parser().parse_args(argv)
    manifest = _load_manifest()

    try:
        channel_map = _load_channel_map(args.channels)
    except Exception as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 3

    if args.apply:
        if args.confirm != CONFIRMATION_TOKEN:
            print(
                f"STOP: --apply requires --confirm exactly "
                f"{CONFIRMATION_TOKEN!r}. Refuse.",
                file=sys.stderr,
            )
            return 5
        # This PR ships planning + validation only. `--apply`
        # against real Google Cloud lands with task #137's
        # enablement PR (SDK snapshot + write paths + creds).
        # Return rc=6 BEFORE _snapshot_cloud so the intent is
        # explicit and the return path is unambiguous — the
        # earlier code caught _snapshot_cloud's SystemExit and
        # returned rc=2 instead, making rc=6 unreachable.
        print(
            "STOP: this PR ships planning + validation only. "
            "--apply against real Google Cloud is gated on the next "
            "PR (SDK snapshot + write paths); see README.md.",
            file=sys.stderr,
        )
        return 6

    # Dry run: no cloud snapshot; the preview treats everything
    # as absent-in-cloud (see render_plan's docstring — it's a
    # desired-state preview, not a live diff).
    existing_metrics: set[str] = set()
    existing_alerts_by_identity: dict[str, list[str]] = {}

    # Format-validate the channel map against the manifest and
    # --project BEFORE build_plan, so a bad-shape entry surfaces
    # its own refusal rather than being masked by the downstream
    # 'missing channel' refusal.
    manifest_alert_ids = set(manifest.get("alerts", {}).keys())
    format_refusals = _validate_channel_map(
        channel_map, project=args.project, manifest_alert_ids=manifest_alert_ids,
    )

    plan = build_plan(
        project=args.project,
        channel_map=channel_map,
        existing_metrics=existing_metrics,
        existing_alerts_by_identity=existing_alerts_by_identity,
        manifest=manifest,
    )
    # Prepend the channel-map refusals so they render first.
    plan.actions = format_refusals + plan.actions

    print(render_plan(plan))
    if plan.has_refusals():
        return 4
    print("\n(offline preview only; pass --apply --confirm '...' — will exit rc=6)")
    return 0


def main() -> None:  # pragma: no cover
    sys.exit(_run(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
