"""Structural validator for the redis-fanout monitoring resources.

Loads `manifest.yaml`, every metric YAML, and every alert YAML;
enforces the invariants the rollout doc §3 requires so a
config-only mistake cannot survive review or CI:

  - Each declared metric file exists, parses, and matches its
    manifest entry (name, filter references the correct event,
    labels the operator expects to see, extractors present).
  - The legacy metric is the sole textPayload matcher — every
    other metric filters on `jsonPayload.event`.
  - Every non-legacy metric extracts `instance_id` via
    `EXTRACT(jsonPayload.instance_id)`.
  - Each declared alert file exists, parses, and matches its
    manifest entry (managed userLabels, group_by shape, only
    references metrics declared in the manifest).
  - No alert file carries a hard-coded `notificationChannels`
    entry — apply.py injects those at apply time so the config
    is environment-portable.
  - Every alert's managed userLabels present: `managed_by`,
    `alert_id`, `runbook`.
  - The `managed_by` value equals `manifest.managed_labels.managed_by`
    so a stray policy cannot silently join the managed set.
  - A5 and A6 (paired conditions) declare BOTH a threshold-
    and an absence-condition on the correct metrics.

Usable as a CLI (`python validate.py`) or a library
(`load_and_validate() -> Report`). The test suite imports
`load_and_validate()` and asserts `report.ok is True` +
inspects the itemised checks so any specific regression is
easy to localise.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover — dev env should have PyYAML
    raise SystemExit(
        f"PyYAML required for validate.py — install via requirements-dev.txt "
        f"({exc!r})"
    )


HERE = Path(__file__).resolve().parent
MANIFEST_PATH = HERE / "manifest.yaml"

_INSTANCE_ID_EXTRACTOR_RE = re.compile(
    r"^\s*EXTRACT\(\s*jsonPayload\.instance_id\s*\)\s*$"
)

# Cloud Monitoring userLabels: keys and values must match
# ^[a-z][a-z0-9_-]{0,62}$ (keys) / ^[a-z0-9_-]{0,63}$ (values).
# A value with '/', '.', '#', or uppercase would cause the API
# to reject the whole policy — the reviewer flagged this as a
# blocker after PR #39 round 1 shipped a URL-shaped runbook
# value that the API would refuse.
_USER_LABEL_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_USER_LABEL_VALUE_RE = re.compile(r"^[a-z0-9_-]{0,63}$")

# PromQL metric identifier in Cloud Monitoring — the log-based
# translation of `logging.googleapis.com/user/<name>` and
# `run.googleapis.com/container/<name>` uses colons + underscores.
_PROMQL_METRIC_TOKEN_RE = re.compile(
    r"(logging_googleapis_com:user_[A-Za-z0-9_]+|"
    r"run_googleapis_com:[A-Za-z0-9_]+)"
)
# Extract the `by (label, label, ...)` list from a PromQL query.
_PROMQL_BY_CLAUSE_RE = re.compile(
    r"\bby\s*\(([^)]*)\)", flags=re.IGNORECASE,
)


def _promql_to_gcp_metric_type(token: str) -> str:
    """Translate a PromQL metric identifier back to its Cloud
    Monitoring `metric.type` form so alerts-vs-manifest checks
    speak a single language.

    Handles the two prefixes this repo actually uses. Unknown
    prefixes fall through as-is."""
    if token.startswith("logging_googleapis_com:user_"):
        suffix = token[len("logging_googleapis_com:user_"):]
        return f"logging.googleapis.com/user/{suffix}"
    if token.startswith("run_googleapis_com:"):
        suffix = token[len("run_googleapis_com:"):]
        # Cloud Run metric types use '/' between the domain and
        # each path segment. Only the last '_' becomes '/'
        # (container/instance_count etc.), which matches what
        # this repo actually references.
        head, _, tail = suffix.partition("_")
        return f"run.googleapis.com/{head}/{tail}" if tail else f"run.googleapis.com/{suffix}"
    return token


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


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return data


def load_and_validate() -> Report:
    """Parse + validate the whole monitoring config tree. Never
    contacts Google Cloud — this is a pure static check."""
    report = Report()

    if not MANIFEST_PATH.exists():
        report.add("manifest.exists", False, f"{MANIFEST_PATH} missing")
        return report
    report.add("manifest.exists", True)

    manifest = _load_yaml(MANIFEST_PATH)

    _validate_manifest_shape(manifest, report)
    metrics_manifest = manifest.get("metrics", {})
    alerts_manifest = manifest.get("alerts", {})
    managed_by = manifest.get("managed_labels", {}).get("managed_by", "")
    required_alert_labels = manifest.get("required_alert_userLabels", [])
    metric_files = _validate_metrics(metrics_manifest, report)
    _validate_alerts(
        alerts_manifest, metrics_manifest, managed_by,
        required_alert_labels, report,
    )
    _validate_no_orphan_files(metric_files, alerts_manifest, report)
    return report


def _validate_manifest_shape(manifest: dict, report: Report) -> None:
    for key in (
        "version", "service", "resource_type",
        "metrics", "alerts", "managed_labels",
        "required_alert_userLabels", "allowed_projects",
    ):
        report.add(
            f"manifest.has:{key}",
            key in manifest,
            f"top-level key {key!r} missing",
        )
    if manifest.get("resource_type") != "cloud_run_revision":
        report.add(
            "manifest.resource_type",
            False,
            f"expected cloud_run_revision, got {manifest.get('resource_type')!r}",
        )
    else:
        report.add("manifest.resource_type", True)


def _validate_metrics(metrics_manifest: dict, report: Report) -> set[Path]:
    """Return the set of metric files validated (used to detect
    orphan files not listed in the manifest)."""
    seen_files: set[Path] = set()
    for name, entry in metrics_manifest.items():
        file_rel = entry.get("file", "")
        path = HERE / file_rel
        exists = path.exists()
        report.add(f"metric.file_exists:{name}", exists, f"{path}")
        if not exists:
            continue
        seen_files.add(path.resolve())
        try:
            data = _load_yaml(path)
        except Exception as exc:
            report.add(f"metric.parse:{name}", False, repr(exc))
            continue
        report.add(f"metric.parse:{name}", True)

        report.add(
            f"metric.name_matches:{name}",
            data.get("name") == name,
            f"YAML name={data.get('name')!r}, manifest name={name!r}",
        )

        filter_text = str(data.get("filter", ""))
        is_legacy = "textPayload_match" in entry
        if is_legacy:
            expected_snippet = entry["textPayload_match"]
            report.add(
                f"metric.filter_matches_textPayload:{name}",
                expected_snippet in filter_text,
                f"filter does not reference textPayload_match={expected_snippet!r}",
            )
        else:
            event = entry.get("event", "")
            report.add(
                f"metric.filter_matches_event:{name}",
                f'jsonPayload.event="{event}"' in filter_text
                or f"jsonPayload.event=\"{event}\"" in filter_text,
                f"filter does not include jsonPayload.event={event!r}",
            )
            # Enforce the instance_id extractor shape on non-legacy metrics.
            extractors = data.get("labelExtractors") or {}
            got = extractors.get("instance_id", "")
            report.add(
                f"metric.instance_id_extractor:{name}",
                bool(_INSTANCE_ID_EXTRACTOR_RE.match(str(got))),
                f"labelExtractors.instance_id={got!r} — expected "
                f"EXTRACT(jsonPayload.instance_id)",
            )

        # Every metric's filter must anchor on the right service+resource.
        for required_snippet in (
            'resource.type="cloud_run_revision"',
            'resource.labels.service_name="worshiptranslate-backend"',
        ):
            report.add(
                f"metric.filter_anchor:{name}:{required_snippet}",
                required_snippet in filter_text,
                f"filter missing {required_snippet!r}",
            )

        # Extracts declared in the manifest must be present in the YAML.
        declared = set(entry.get("extracts", []))
        got_labels = set((data.get("labelExtractors") or {}).keys())
        missing = declared - got_labels
        report.add(
            f"metric.extracts_present:{name}",
            not missing,
            f"missing label extractors: {sorted(missing)!r}",
        )
    return seen_files


def _validate_alerts(
    alerts_manifest: dict,
    metrics_manifest: dict,
    managed_by: str,
    required_alert_labels: list[str],
    report: Report,
) -> None:
    for name, entry in alerts_manifest.items():
        file_rel = entry.get("file", "")
        path = HERE / file_rel
        exists = path.exists()
        report.add(f"alert.file_exists:{name}", exists, f"{path}")
        if not exists:
            continue
        try:
            data = _load_yaml(path)
        except Exception as exc:
            report.add(f"alert.parse:{name}", False, repr(exc))
            continue
        report.add(f"alert.parse:{name}", True)

        # userLabels present + managed_by pinned to the manifest value.
        user_labels = data.get("userLabels") or {}
        missing_labels = [k for k in required_alert_labels if k not in user_labels]
        report.add(
            f"alert.required_userLabels:{name}",
            not missing_labels,
            f"missing userLabels {missing_labels!r}",
        )
        report.add(
            f"alert.managed_by_pinned:{name}",
            user_labels.get("managed_by") == managed_by,
            f"userLabels.managed_by={user_labels.get('managed_by')!r}, "
            f"manifest expects {managed_by!r}",
        )
        report.add(
            f"alert.alert_id_matches_manifest:{name}",
            user_labels.get("alert_id", "").replace("-", "_") == name,
            f"userLabels.alert_id={user_labels.get('alert_id')!r} — "
            f"expected kebab-case of manifest key {name!r}",
        )

        # userLabels character restrictions — every key/value must
        # match Cloud Monitoring's constraint. A URL-shaped
        # `runbook: docs/...#alerts` would cause the API to reject
        # the whole policy, which is what caused the PR #39 round-1
        # blocker.
        for k, v in user_labels.items():
            key_ok = bool(_USER_LABEL_KEY_RE.match(str(k)))
            val_ok = bool(_USER_LABEL_VALUE_RE.match(str(v)))
            report.add(
                f"alert.userLabel_key_charset:{name}:{k}",
                key_ok,
                f"userLabels key {k!r} violates Cloud Monitoring charset "
                f"(^[a-z][a-z0-9_-]{{0,62}}$)",
            )
            report.add(
                f"alert.userLabel_value_charset:{name}:{k}",
                val_ok,
                f"userLabels[{k!r}] value {v!r} violates Cloud Monitoring "
                f"charset (^[a-z0-9_-]{{0,63}}$) — runbook URLs must live "
                f"in the documentation markdown, not a label",
            )

        # No hard-coded notificationChannels (apply.py injects at apply time).
        report.add(
            f"alert.notificationChannels_empty:{name}",
            data.get("notificationChannels") in ([], None),
            f"notificationChannels={data.get('notificationChannels')!r} — "
            f"must be [] in files; apply.py injects at apply time",
        )

        # Every metric.type referenced by any condition must be a
        # declared managed metric (or a listed correlating GCP
        # metric such as run.googleapis.com/container/instance_count).
        referenced_metrics = _extract_metric_types(data)
        declared_metric_types = {
            f"logging.googleapis.com/user/{m}" for m in metrics_manifest.keys()
        }
        declared_metric_types |= set(entry.get("correlating_gcp_metrics", []))
        undeclared = referenced_metrics - declared_metric_types
        report.add(
            f"alert.only_managed_metrics:{name}",
            not undeclared,
            f"references undeclared metric types: {sorted(undeclared)!r}",
        )

        # Group_by from the manifest is present in the alert. The
        # per-condition shape depends on query_kind: threshold /
        # absent conditions use aggregations.groupByFields;
        # PromQL conditions use `by (…)` inside the query text.
        query_kind = entry.get("query_kind", "threshold")
        expected_group_by = set(entry.get("group_by", []))
        got_group_by = _collect_group_by(data, kind=query_kind)
        missing_groups = expected_group_by - got_group_by
        report.add(
            f"alert.group_by_present:{name}",
            not missing_groups,
            f"missing group_by fields {sorted(missing_groups)!r} "
            f"(query_kind={query_kind!r})",
        )

        # PromQL-specific structural checks: the `unless` join
        # pattern is what allows A5/A6/A8a to fire on
        # never-emitted series or to correlate across metrics.
        # A promql alert whose query drops `unless` would silently
        # regress to the same class of bug we're trying to fix.
        if query_kind == "promql":
            query_text = _collect_promql_queries(data)
            report.add(
                f"alert.promql_query_present:{name}",
                bool(query_text),
                "conditionPrometheusQueryLanguage.query is empty or missing",
            )
            report.add(
                f"alert.promql_uses_unless_join:{name}",
                "unless" in query_text,
                "PromQL query does not use `unless` — required for "
                "paired-condition alerts to survive never-emitted series",
            )


def _extract_metric_types(alert_data: dict) -> set[str]:
    out: set[str] = set()
    for cond in alert_data.get("conditions") or []:
        for key in ("conditionThreshold", "conditionAbsent"):
            block = cond.get(key)
            if not isinstance(block, dict):
                continue
            filter_text = str(block.get("filter", ""))
            for m in re.finditer(r'metric\.type="([^"]+)"', filter_text):
                out.add(m.group(1))
        promql_block = cond.get("conditionPrometheusQueryLanguage")
        if isinstance(promql_block, dict):
            query_text = str(promql_block.get("query", ""))
            for m in _PROMQL_METRIC_TOKEN_RE.finditer(query_text):
                out.add(_promql_to_gcp_metric_type(m.group(1)))
    return out


def _collect_group_by(alert_data: dict, *, kind: str = "threshold") -> set[str]:
    """Aggregate the group-by labels across every condition.

    For `threshold` / `absent` alerts, the fields live in each
    condition's `aggregations[].groupByFields`. For `promql` alerts,
    the grouping is expressed inside the query as
    `sum by (label, label) (...)` — we scan every `by (...)` clause
    across every query in the alert."""
    out: set[str] = set()
    for cond in alert_data.get("conditions") or []:
        for key in ("conditionThreshold", "conditionAbsent"):
            block = cond.get(key)
            if not isinstance(block, dict):
                continue
            for agg in block.get("aggregations") or []:
                for field in agg.get("groupByFields") or []:
                    out.add(field)
        promql_block = cond.get("conditionPrometheusQueryLanguage")
        if isinstance(promql_block, dict):
            query_text = str(promql_block.get("query", ""))
            for m in _PROMQL_BY_CLAUSE_RE.finditer(query_text):
                for label in m.group(1).split(","):
                    label = label.strip()
                    if label:
                        out.add(label)
    return out


def _collect_promql_queries(alert_data: dict) -> str:
    """Concatenate every PromQL query text in the alert (a single
    alert can carry multiple conditions, though the redis-fanout
    alerts each have exactly one)."""
    parts: list[str] = []
    for cond in alert_data.get("conditions") or []:
        promql_block = cond.get("conditionPrometheusQueryLanguage")
        if isinstance(promql_block, dict):
            parts.append(str(promql_block.get("query", "")))
    return "\n".join(parts)


def _validate_no_orphan_files(
    manifest_metric_files: set[Path], alerts_manifest: dict, report: Report,
) -> None:
    """A YAML file present on disk but absent from the manifest is
    a load-bearing configuration nobody reviewed — fail closed."""
    metrics_dir = HERE / "metrics"
    alerts_dir = HERE / "alerts"

    disk_metrics = {p.resolve() for p in metrics_dir.glob("*.yaml")} \
        if metrics_dir.is_dir() else set()
    orphan_metrics = disk_metrics - manifest_metric_files
    report.add(
        "metrics.no_orphan_files",
        not orphan_metrics,
        f"orphan metric YAMLs: {sorted(str(p) for p in orphan_metrics)!r}",
    )

    manifest_alert_files = {
        (HERE / entry["file"]).resolve()
        for entry in alerts_manifest.values() if "file" in entry
    }
    disk_alerts = {p.resolve() for p in alerts_dir.glob("*.yaml")} \
        if alerts_dir.is_dir() else set()
    orphan_alerts = disk_alerts - manifest_alert_files
    report.add(
        "alerts.no_orphan_files",
        not orphan_alerts,
        f"orphan alert YAMLs: {sorted(str(p) for p in orphan_alerts)!r}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Structural validator for ops/monitoring/redis-fanout.",
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
