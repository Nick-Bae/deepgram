"""Structural validator for the reconciler monitoring config.

Run:
    python ops/monitoring/reconciler/validate.py

Exit code non-zero on any failure. CI wires this into the backend-tests
job so a broken monitoring definition fails the build before it can be
applied against a real project.

Verifies:
  - Every YAML in metrics/, dashboards/, alerts/ parses.
  - Metric names are unique.
  - Metric filters reference `component="room_reconciler"` (so they
    catch only reconciler events and not stray logs).
  - Metric labels are bounded (from the enum sets defined in
    app.services.room_reconciler).
  - Alert-policy `filter` fields reference metric types that exist in
    metrics/.
  - Alert-policy notification channels use the substitution
    placeholder (not a bare channel ID committed to the repo).
  - Exactly one alert policy has `enabled: false` (the deliberately-
    disabled recovery-actions baseline).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML is required for validation", file=sys.stderr)
    sys.exit(2)

ROOT = Path(__file__).resolve().parent
METRICS_DIR = ROOT / "metrics"
DASHBOARDS_DIR = ROOT / "dashboards"
ALERTS_DIR = ROOT / "alerts"

# Import the reconciler's enums as source of truth. If the reconciler
# grows a new outcome/reason and monitoring is not updated, this fails
# — closes the "schema drift silently breaks alerts" gap.
BACKEND_ROOT = ROOT.parent.parent.parent / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
from app.services.room_reconciler import (  # noqa: E402
    _ACTION_REASONS,
    _TICK_OUTCOMES,
)


class Fail(Exception):
    pass


def _load_yaml(path: Path) -> dict:
    with path.open("r") as f:
        try:
            return yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            raise Fail(f"{path}: invalid YAML — {exc}")


def validate_metrics() -> dict[str, dict]:
    metrics: dict[str, dict] = {}
    for path in sorted(METRICS_DIR.glob("*.yaml")):
        doc = _load_yaml(path)
        name = doc.get("name")
        if not name:
            raise Fail(f"{path}: missing `name`")
        if name in metrics:
            raise Fail(f"{path}: duplicate metric name {name}")
        # LogMetric does not support user labels — the Cloud Logging
        # API rejects them. Idempotence for metrics is via the
        # client-assigned `name` alone; management labels apply only
        # to alert policies and dashboards.
        if "userLabels" in doc or "labels" in doc:
            raise Fail(
                f"{path}: log-based metrics do not support "
                f"userLabels/labels — the API rejects them. "
                f"Metrics are looked up idempotently by `name` only."
            )
        f = doc.get("filter", "")
        if 'component="room_reconciler"' not in f:
            raise Fail(
                f"{path}: filter must include "
                f'component="room_reconciler" (scope to this module)'
            )
        if 'resource.labels.service_name="__CLOUD_RUN_SERVICE__"' not in f:
            raise Fail(
                f"{path}: filter must include "
                f'resource.labels.service_name="__CLOUD_RUN_SERVICE__" — '
                f'the placeholder is substituted at apply time; without it '
                f'metrics would count logs from every Cloud Run service '
                f'in the project (staging, tests, unrelated services)'
            )
        if 'jsonPayload.event=' not in f:
            raise Fail(f"{path}: filter must pin jsonPayload.event")
        # Extract the event value from filter so we can verify it's
        # one this reconciler actually emits.
        m = re.search(r'jsonPayload\.event="([^"]+)"', f)
        if not m:
            raise Fail(f"{path}: filter must set jsonPayload.event= to a literal")
        event_val = m.group(1)
        if event_val not in {"reconciler_tick", "reconciler_action"}:
            raise Fail(
                f"{path}: metric filters may only match reconciler_tick or "
                f"reconciler_action events; got {event_val!r}"
            )
        # Validate label extractors don't include known-cardinality-
        # explosive fields.
        for label in (doc.get("metricDescriptor", {}).get("labels") or []):
            key = label.get("key")
            if key in {"org_id", "room_id", "instance_id"}:
                raise Fail(
                    f"{path}: label {key!r} is high-cardinality; "
                    f"log body only, not metric label"
                )
        metrics[name] = doc
    return metrics


def _extractor_matches_enum(doc: dict, kind: str, allowed: set[str]) -> None:
    """If a metric label extracts one of the reconciler's enums,
    fail if the reconciler's enum has grown and this file doesn't
    mention every value in the description (best-effort sanity check
    to make schema drift visible)."""
    for label in (doc.get("metricDescriptor", {}).get("labels") or []):
        key = label.get("key")
        if key != kind:
            continue
        desc = label.get("description", "")
        for value in allowed:
            if value not in desc:
                raise Fail(
                    f"{doc.get('name')}: label {key!r} description "
                    f"missing enum value {value!r}. Update the label "
                    f"description or delete the enum entry."
                )


def _condition_filter_uses_correct_label_selector(filt: str) -> tuple[bool, str]:
    """Cloud Monitoring filter syntax:
      - `metric.labels.<name>` in FILTER expressions (plural).
      - `metric.label.<name>` ONLY in groupByFields.
    Return (ok, reason). This check runs against alert-policy
    condition FILTER strings; groupByFields is validated separately."""
    if re.search(r"\bmetric\.label\.[a-zA-Z_]+", filt):
        return (
            False,
            "condition filter uses `metric.label.<name>` (singular); "
            "filter syntax requires `metric.labels.<name>` (plural). "
            "The singular form is valid only inside groupByFields.",
        )
    return (True, "")


def _agg_reduces_across_revisions(condition: dict) -> bool:
    """The condition must aggregate every revision-scoped series
    into ONE service-level series before evaluating. If it doesn't,
    a rolling deploy where the retired revision goes silent will
    look like an absence / drop-off on THAT specific revision's
    series and page ops on healthy rollouts."""
    body = condition.get("conditionThreshold") or condition.get("conditionAbsent") or {}
    for agg in body.get("aggregations", []) or []:
        reducer = agg.get("crossSeriesReducer")
        if reducer not in ("REDUCE_SUM", "REDUCE_MEAN", "REDUCE_COUNT", "REDUCE_MAX", "REDUCE_MIN"):
            continue
        group = agg.get("groupByFields") or []
        # Grouping ONLY by service_name (not revision_name) reduces
        # revision series into one per service.
        if any("revision_name" in g for g in group):
            continue
        # A service_name group is what we want.
        if any("service_name" in g for g in group) or not group:
            return True
    return False


def validate_alerts(metrics: dict[str, dict]) -> None:
    disabled_count = 0
    seen_names: set[str] = set()
    seen_resource_ids: set[str] = set()
    metric_types = {
        f"logging.googleapis.com/user/{name}" for name in metrics
    }
    for path in sorted(ALERTS_DIR.glob("*.yaml")):
        doc = _load_yaml(path)
        display = doc.get("displayName")
        if not display:
            raise Fail(f"{path}: missing displayName")
        if display in seen_names:
            raise Fail(f"{path}: duplicate displayName {display!r}")
        seen_names.add(display)

        user_labels = doc.get("userLabels") or {}
        if user_labels.get("managed_by") != "reconciler-monitoring":
            raise Fail(
                f"{path}: userLabels.managed_by must be "
                f"'reconciler-monitoring' — the apply script uses "
                f"this label to look up existing policies "
                f"idempotently instead of matching by displayName"
            )
        resource_id = user_labels.get("resource_id")
        if not resource_id:
            raise Fail(f"{path}: userLabels.resource_id required")
        if resource_id in seen_resource_ids:
            raise Fail(f"{path}: duplicate resource_id {resource_id!r}")
        seen_resource_ids.add(resource_id)

        enabled = doc.get("enabled")
        if enabled is False:
            disabled_count += 1

        channels = doc.get("notificationChannels")
        if channels != "__NOTIFICATION_CHANNELS__":
            raise Fail(
                f"{path}: notificationChannels must be the "
                f"__NOTIFICATION_CHANNELS__ placeholder — "
                f"the apply script substitutes real channel IDs at "
                f"deploy time"
            )

        cond_display_names: set[str] = set()
        for condition in doc.get("conditions", []):
            cond_display = condition.get("displayName")
            if not cond_display:
                raise Fail(
                    f"{path}: every condition needs a displayName — the "
                    f"apply script uses displayName to preserve the "
                    f"condition's server-assigned `name` across updates. "
                    f"Without a stable displayName, an update recreates "
                    f"the condition and breaks the second-apply-is-noop "
                    f"invariant."
                )
            if cond_display in cond_display_names:
                raise Fail(
                    f"{path}: duplicate condition displayName "
                    f"{cond_display!r}; condition displayNames must be "
                    f"unique within a policy so the apply script can "
                    f"match them to existing conditions by name."
                )
            cond_display_names.add(cond_display)

            filt = ""
            if "conditionThreshold" in condition:
                filt = condition["conditionThreshold"].get("filter", "")
            elif "conditionAbsent" in condition:
                filt = condition["conditionAbsent"].get("filter", "")
            m = re.search(r'metric\.type="([^"]+)"', filt)
            if not m:
                raise Fail(f"{path}: condition filter missing metric.type")
            mtype = m.group(1)
            if mtype not in metric_types:
                raise Fail(
                    f"{path}: references undefined metric.type={mtype!r}. "
                    f"Known metrics: {sorted(metric_types)}"
                )
            ok, reason = _condition_filter_uses_correct_label_selector(filt)
            if not ok:
                raise Fail(
                    f"{path}: condition {cond_display!r} — {reason}"
                )
            if not _agg_reduces_across_revisions(condition):
                raise Fail(
                    f"{path}: condition {cond_display!r} "
                    f"must aggregate across revision-scoped series "
                    f"(crossSeriesReducer + groupByFields = "
                    f"['resource.label.service_name']). Without this, "
                    f"a rolling deploy triggers a false alert every "
                    f"time the retired revision goes silent."
                )

    if disabled_count != 1:
        raise Fail(
            f"expected exactly one disabled alert policy (the "
            f"recovery-actions baseline); found {disabled_count}"
        )


def _walk_widgets(layout: dict):
    """Yield every widget dict from a mosaic or grid layout."""
    for tile in (layout.get("tiles") or []):
        widget = tile.get("widget")
        if widget:
            yield widget
    for widget in (layout.get("widgets") or []):
        yield widget


def validate_dashboards() -> None:
    for path in sorted(DASHBOARDS_DIR.glob("*.yaml")):
        doc = _load_yaml(path)
        if not doc.get("displayName"):
            raise Fail(f"{path}: missing displayName")
        layout = doc.get("mosaicLayout") or doc.get("gridLayout")
        if layout is None:
            raise Fail(f"{path}: needs mosaicLayout or gridLayout")
        # Cloud Monitoring Dashboard uses top-level `labels`, NOT
        # `userLabels`. A YAML that puts management labels under
        # `userLabels` would apply, but the apply-script lookup
        # (filter on `labels.*`) would miss it and create a
        # duplicate on the next apply.
        if "userLabels" in doc:
            raise Fail(
                f"{path}: Dashboard uses top-level `labels`, not "
                f"`userLabels`. Move `managed_by` / `resource_id` "
                f"under `labels`."
            )
        labels = doc.get("labels") or {}
        if labels.get("managed_by") != "reconciler-monitoring":
            raise Fail(
                f"{path}: labels.managed_by must be "
                f"'reconciler-monitoring' — the apply script uses "
                f"this label for idempotent lookup"
            )
        if not labels.get("resource_id"):
            raise Fail(f"{path}: labels.resource_id required")
        # Every logs panel must scope to the Cloud Run service.
        for widget in _walk_widgets(layout):
            panel = widget.get("logsPanel")
            if panel is None:
                continue
            filt = panel.get("filter", "")
            if 'resource.type="cloud_run_revision"' not in filt:
                raise Fail(
                    f"{path}: logs panel widget {widget.get('title')!r} "
                    f"filter must include resource.type=\"cloud_run_revision\""
                )
            if 'resource.labels.service_name="__CLOUD_RUN_SERVICE__"' not in filt:
                raise Fail(
                    f"{path}: logs panel widget {widget.get('title')!r} "
                    f"filter must include "
                    f'resource.labels.service_name="__CLOUD_RUN_SERVICE__"'
                    f" — otherwise the panel shows logs from every service"
                )


def validate_apply_script() -> None:
    """Structural checks over apply.sh:
      - `bash -n` catches shell syntax errors.
      - Presence of the etag-preservation helper on dashboard
        updates (without it, the second apply fails because the
        server rejects a stale/missing etag).
      - Presence of the condition-name-preservation helper on
        alert-policy updates (without it, --policy-from-file
        deletes existing conditions and recreates them, breaking
        the "second apply is a no-op" invariant).
      - Dashboard lookup uses top-level `labels.*`, not
        `userLabels.*`.
    """
    import subprocess
    apply_path = ROOT / "apply.sh"
    result = subprocess.run(
        ["bash", "-n", str(apply_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise Fail(f"apply.sh: bash -n failed:\n{result.stderr}")
    src = apply_path.read_text()

    # Extract a shell-function body by balanced braces. Each helper
    # must be CALLED from inside the function that owns its update
    # path — not merely defined. The earlier substring check missed
    # the case where a call site is deleted but the function
    # definition (which contains the identifier) remains.
    def _function_body(name: str) -> str | None:
        m = re.search(rf"^{re.escape(name)}\(\)\s*\{{", src, re.MULTILINE)
        if not m:
            return None
        depth = 0
        start = m.end() - 1  # position of opening `{`
        for i in range(start, len(src)):
            c = src[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return src[start + 1 : i]
        return None

    # A call site inside a function body looks like the identifier
    # followed by a space or a double quote (bash function-call
    # form): `_merge_dashboard_etag "$existing_id" "$tmp"`. A
    # definition line looks like `_merge_dashboard_etag() {` and
    # is by construction outside its own body.
    _CALL_RE = r"\b{name}\b[ \"]"

    def _strip_bash_comments(body: str) -> str:
        """Remove bash line-comments so a commented-out call
        (`# _merge_dashboard_etag "$a" "$b"`) does not look like a
        live invocation to the regex below. The strip is
        line-oriented and does not attempt to preserve quoted `#`
        characters — good enough for the narrow contract this
        validator enforces (a helper is called somewhere inside
        the function's active body)."""
        out_lines = []
        for line in body.splitlines():
            # Strip everything after the first `#` that is not
            # inside a bash single-quoted string. Simple heuristic:
            # if there's a `#` with a leading space or start-of-line,
            # cut it. This misses `#` inside quotes but our apply.sh
            # doesn't have those in the paths we care about.
            i = line.find("#")
            if i == 0:
                continue
            if i > 0 and line[i - 1] in (" ", "\t"):
                out_lines.append(line[:i])
                continue
            out_lines.append(line)
        return "\n".join(out_lines)

    apply_dashboard = _function_body("apply_dashboard")
    if apply_dashboard is None:
        raise Fail("apply.sh: apply_dashboard function not found")
    apply_dashboard_active = _strip_bash_comments(apply_dashboard)
    if not re.search(_CALL_RE.format(name="_merge_dashboard_etag"), apply_dashboard_active):
        raise Fail(
            "apply.sh: apply_dashboard does not call _merge_dashboard_etag "
            "in its update path (searched with bash comments stripped so a "
            "commented-out call does not satisfy the check). Without merging "
            "the current server etag into the submitted body, the dashboard "
            "update fails on the second apply."
        )

    apply_alert = _function_body("apply_alert")
    if apply_alert is None:
        raise Fail("apply.sh: apply_alert function not found")
    apply_alert_active = _strip_bash_comments(apply_alert)
    if not re.search(_CALL_RE.format(name="_merge_alert_condition_names"), apply_alert_active):
        raise Fail(
            "apply.sh: apply_alert does not call _merge_alert_condition_names "
            "in its update path (searched with bash comments stripped so a "
            "commented-out call does not satisfy the check). Without merging "
            "existing condition names, --policy-from-file treats every "
            "submitted condition as new and deletes the existing ones — the "
            "'second apply is a no-op' invariant breaks."
        )
    # Dashboard lookup must use `labels.*`, not `userLabels.*`.
    if re.search(r"dashboards list.*userLabels\.", src, re.DOTALL):
        raise Fail(
            "apply.sh: dashboards list filter uses `userLabels.*`; "
            "Dashboard resources expose top-level `labels`. Change "
            "the filter to `labels.managed_by` / `labels.resource_id`."
        )


def main() -> int:
    try:
        metrics = validate_metrics()
        # Best-effort sanity checks against enum drift.
        for doc in metrics.values():
            _extractor_matches_enum(doc, "outcome", _TICK_OUTCOMES)
            _extractor_matches_enum(doc, "reason", _ACTION_REASONS)
        validate_alerts(metrics)
        validate_dashboards()
        validate_apply_script()
    except Fail as exc:
        print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"ok — {len(metrics)} metric(s), "
          f"{len(list(DASHBOARDS_DIR.glob('*.yaml')))} dashboard(s), "
          f"{len(list(ALERTS_DIR.glob('*.yaml')))} alert policy/policies, "
          f"apply.sh syntax ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
