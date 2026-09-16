#!/usr/bin/env bash
# Idempotent apply for the reconciler monitoring stack.
#
# Prereqs:
#   gcloud authenticated with monitoring.admin + logging.admin.
#   Required env vars:
#     GCP_PROJECT              — target project ID
#     CLOUD_RUN_SERVICE        — Cloud Run service name to scope
#                                every metric filter to
#     NOTIFICATION_CHANNEL_IDS — comma-separated Cloud Monitoring
#                                notification channel IDs. Required
#                                for enabled policies to page anyone.
#
# Usage:
#     GCP_PROJECT=worshiptranslate \
#     CLOUD_RUN_SERVICE=worshiptranslate-backend \
#     NOTIFICATION_CHANNEL_IDS=projects/…/notificationChannels/abc,projects/…/notificationChannels/xyz \
#         ops/monitoring/reconciler/apply.sh
#
# Idempotent behavior:
#   - Every managed resource carries the user labels
#       managed_by: reconciler-monitoring
#       resource_id: <stable id per file>
#   - Lookup is by those labels, NOT by displayName (which is
#     user-visible text and can drift). A single-match hit means
#     "update in place." A multi-match hit means "somebody
#     duplicated a managed resource" and this script REFUSES to
#     update — bring it back to a single instance manually before
#     re-running.
#   - Placeholder substitution goes through a small Python helper
#     rather than sed to avoid YAML corruption on multi-line
#     values.
#   - Every enabled alert policy is asserted to have at least one
#     notification channel BEFORE any Cloud Monitoring call is made.
#   - After each create/update, `describe` output is emitted to
#     stdout — the run log is the deployment evidence.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${GCP_PROJECT:?GCP_PROJECT env var is required}"
: "${CLOUD_RUN_SERVICE:?CLOUD_RUN_SERVICE env var is required}"
: "${NOTIFICATION_CHANNEL_IDS:?NOTIFICATION_CHANNEL_IDS env var is required}"

# Preflight: PyYAML is required in the OPERATOR's shell (not just CI).
# `backend/requirements-dev.txt` declares it for CI, but this script
# runs from an operator's terminal against a real GCP project — that
# terminal must have PyYAML in the active Python. Fail fast with a
# clear install line instead of trapping the operator inside a
# half-applied deploy.
if ! python3 -c 'import yaml' > /dev/null 2>&1; then
  cat >&2 <<'EOF'
PyYAML is required for placeholder substitution but is not importable
by `python3` on this shell's PATH.

Install with:
  pip install "PyYAML>=6.0"
  # macOS via Homebrew python:
  brew install libyaml && pip install "PyYAML>=6.0"
Then re-run this script.

(PyYAML is a library, not a CLI, so `pipx` is not applicable.)
EOF
  exit 2
fi

# Split comma-separated channels into an array for the Python
# substitution helper.
IFS=',' read -ra _channels <<< "${NOTIFICATION_CHANNEL_IDS}"
if [[ ${#_channels[@]} -eq 0 ]]; then
  echo "NOTIFICATION_CHANNEL_IDS must list at least one channel" >&2
  exit 2
fi

echo "==> Target project: ${GCP_PROJECT}"
echo "==> Cloud Run service: ${CLOUD_RUN_SERVICE}"
echo "==> Notification channels: ${NOTIFICATION_CHANNEL_IDS}"

# Python helper. Reads YAML, substitutes placeholders safely,
# validates enabled-policy notification requirement, writes to
# stdout.
_subst() {
  local file="$1"
  local kind="$2"  # "metric" | "dashboard" | "alert"
  # shellcheck disable=SC2016
  CHANNELS="${NOTIFICATION_CHANNEL_IDS}" \
  SERVICE="${CLOUD_RUN_SERVICE}" \
  KIND="${kind}" \
  python3 - "${file}" <<'PY'
import os, sys, yaml, json
path = sys.argv[1]
kind = os.environ["KIND"]
service = os.environ["SERVICE"]
channels = [c.strip() for c in os.environ["CHANNELS"].split(",") if c.strip()]

with open(path, "r") as fh:
    doc = yaml.safe_load(fh)

def walk(obj):
    if isinstance(obj, dict):
        return {k: walk(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [walk(v) for v in obj]
    if isinstance(obj, str):
        return obj.replace("__CLOUD_RUN_SERVICE__", service)
    return obj

doc = walk(doc)

if kind == "alert":
    # Substitute notification channels (this is a scalar placeholder
    # in the YAML, not a string embed).
    if doc.get("notificationChannels") == "__NOTIFICATION_CHANNELS__":
        doc["notificationChannels"] = channels
    # Enabled alerts MUST have at least one channel.
    if doc.get("enabled") and not doc.get("notificationChannels"):
        sys.exit(f"{path}: enabled alert requires at least one notification channel")

json.dump(doc, sys.stdout)
PY
}

apply_metric() {
  local file="$1"
  local name
  name=$(python3 -c "import sys,yaml;print(yaml.safe_load(open('${file}'))['name'])")
  echo "== metric: ${name}"
  local tmp
  tmp=$(mktemp --suffix=.json)
  _subst "${file}" "metric" > "${tmp}"
  local exists=0
  if gcloud logging metrics describe "${name}" \
      --project="${GCP_PROJECT}" --format=json > /dev/null 2>&1; then
    exists=1
  fi
  if [[ ${exists} -eq 1 ]]; then
    gcloud logging metrics update "${name}" \
      --config-from-file="${tmp}" --project="${GCP_PROJECT}"
  else
    gcloud logging metrics create "${name}" \
      --config-from-file="${tmp}" --project="${GCP_PROJECT}"
  fi
  gcloud logging metrics describe "${name}" --project="${GCP_PROJECT}"
  rm -f "${tmp}"
}

# Look up a managed alert policy by (managed_by, resource_id)
# userLabels. AlertPolicy exposes `userLabels`.
_lookup_alert_by_labels() {
  local resource_id="$1"
  gcloud alpha monitoring policies list \
    --project="${GCP_PROJECT}" \
    --filter="userLabels.managed_by=\"reconciler-monitoring\" AND userLabels.resource_id=\"${resource_id}\"" \
    --format="value(name)"
}

# Dashboard resources expose top-level `labels`, NOT `userLabels`.
# Reference: cloud.google.com/monitoring/dashboards/api-dashboard —
# the Dashboard message has a `labels` map at the top level. Filtering
# by `userLabels.*` would return zero matches even when a managed
# dashboard exists, and the next apply would create a duplicate.
_lookup_dashboard_by_labels() {
  local resource_id="$1"
  gcloud monitoring dashboards list \
    --project="${GCP_PROJECT}" \
    --filter="labels.managed_by=\"reconciler-monitoring\" AND labels.resource_id=\"${resource_id}\"" \
    --format="value(name)"
}

# Merge the existing policy's condition NAMES into the new policy
# body, matched by condition displayName. Cloud Monitoring treats a
# --policy-from-file update with unnamed conditions as "these are
# NEW conditions" and deletes the existing ones — the "second apply
# is a no-op" property breaks. Preserving names by displayName
# match makes updates in-place edits.
_merge_alert_condition_names() {
  local policy_id="$1"
  local new_body_file="$2"
  # shellcheck disable=SC2016
  EXISTING_ID="${policy_id}" NEW_BODY_FILE="${new_body_file}" \
  PROJECT="${GCP_PROJECT}" \
  python3 - <<'PY'
import json, os, subprocess, sys

existing_id = os.environ["EXISTING_ID"]
new_body_file = os.environ["NEW_BODY_FILE"]
project = os.environ["PROJECT"]

with open(new_body_file, "r") as fh:
    new_body = json.load(fh)

# Describe the existing policy to pull current condition names.
result = subprocess.run(
    ["gcloud", "alpha", "monitoring", "policies", "describe",
     existing_id, "--project", project, "--format", "json"],
    capture_output=True, text=True, check=True,
)
existing = json.loads(result.stdout)
name_by_display = {}
for cond in existing.get("conditions", []) or []:
    display = cond.get("displayName")
    name = cond.get("name")
    if display and name:
        name_by_display[display] = name

# Attach the existing name to any new condition sharing the same
# displayName. Unmatched new conditions are treated as truly new
# (created without a `name` field).
matched = 0
for cond in new_body.get("conditions", []) or []:
    display = cond.get("displayName")
    if display in name_by_display:
        cond["name"] = name_by_display[display]
        matched += 1

# Also carry forward the policy `name` so gcloud knows what to update
# (some `--policy-from-file` codepaths care).
new_body["name"] = existing_id

json.dump(new_body, sys.stdout)
sys.stderr.write(
    f"merged {matched} existing condition name(s) into new policy body\n"
)
PY
}

apply_alert() {
  local file="$1"
  local resource_id
  resource_id=$(python3 -c "import sys,yaml;print(yaml.safe_load(open('${file}'))['userLabels']['resource_id'])")
  local displayName
  displayName=$(python3 -c "import sys,yaml;print(yaml.safe_load(open('${file}'))['displayName'])")
  echo "== alert policy: ${displayName}  (resource_id=${resource_id})"
  local tmp
  tmp=$(mktemp --suffix=.json)
  _subst "${file}" "alert" > "${tmp}"

  local matches
  matches=$(_lookup_alert_by_labels "${resource_id}")
  local count
  count=$(printf '%s\n' "${matches}" | sed '/^$/d' | wc -l | awk '{print $1}')
  if [[ "${count}" -gt 1 ]]; then
    echo "REFUSE: multiple alert policies with resource_id=${resource_id}:" >&2
    printf '%s\n' "${matches}" >&2
    echo "Bring back to a single managed policy before re-running." >&2
    rm -f "${tmp}"
    return 1
  fi
  if [[ "${count}" -eq 1 ]]; then
    local existing_id
    existing_id=$(printf '%s\n' "${matches}" | sed '/^$/d' | head -1)
    # Preserve condition names — without this, --policy-from-file
    # deletes every existing condition and recreates them, so the
    # "second apply is a no-op" invariant breaks.
    local merged
    merged=$(mktemp --suffix=.json)
    _merge_alert_condition_names "${existing_id}" "${tmp}" > "${merged}"
    gcloud alpha monitoring policies update "${existing_id}" \
      --policy-from-file="${merged}" --project="${GCP_PROJECT}"
    rm -f "${merged}"
    gcloud alpha monitoring policies describe "${existing_id}" --project="${GCP_PROJECT}"
  else
    local created
    created=$(gcloud alpha monitoring policies create \
      --policy-from-file="${tmp}" --project="${GCP_PROJECT}" \
      --format="value(name)")
    gcloud alpha monitoring policies describe "${created}" --project="${GCP_PROJECT}"
  fi
  rm -f "${tmp}"
}

# Merge the current dashboard's etag into the new config before
# calling update. gcloud rejects a dashboard update whose body's
# `etag` field does not match the server's current value — the
# check exists specifically to prevent lost-update overwrites when
# two operators race. Fetching-and-embedding closes that gap for a
# single-operator apply.sh flow.
_merge_dashboard_etag() {
  local dashboard_id="$1"
  local new_body_file="$2"
  # shellcheck disable=SC2016
  DASH_ID="${dashboard_id}" NEW_BODY_FILE="${new_body_file}" \
  PROJECT="${GCP_PROJECT}" \
  python3 - <<'PY'
import json, os, subprocess, sys

with open(os.environ["NEW_BODY_FILE"], "r") as fh:
    body = json.load(fh)

result = subprocess.run(
    ["gcloud", "monitoring", "dashboards", "describe",
     os.environ["DASH_ID"], "--project", os.environ["PROJECT"],
     "--format", "json"],
    capture_output=True, text=True, check=True,
)
existing = json.loads(result.stdout)
etag = existing.get("etag")
if etag:
    body["etag"] = etag
# gcloud also expects the dashboard `name` field on updates.
body["name"] = os.environ["DASH_ID"]

json.dump(body, sys.stdout)
sys.stderr.write(f"merged existing dashboard etag={etag!r}\n")
PY
}

apply_dashboard() {
  local file="$1"
  local resource_id
  # Dashboards use top-level `labels`, NOT `userLabels`.
  resource_id=$(python3 -c "import sys,yaml;d=yaml.safe_load(open('${file}'));print((d.get('labels') or {}).get('resource_id') or d.get('displayName'))")
  local displayName
  displayName=$(python3 -c "import sys,yaml;print(yaml.safe_load(open('${file}'))['displayName'])")
  echo "== dashboard: ${displayName}"
  local tmp
  tmp=$(mktemp --suffix=.json)
  _subst "${file}" "dashboard" > "${tmp}"
  local matches
  matches=$(_lookup_dashboard_by_labels "${resource_id}")
  local count
  count=$(printf '%s\n' "${matches}" | sed '/^$/d' | wc -l | awk '{print $1}')
  if [[ "${count}" -gt 1 ]]; then
    echo "REFUSE: multiple dashboards with resource_id=${resource_id}:" >&2
    printf '%s\n' "${matches}" >&2
    echo "Bring back to a single managed dashboard before re-running." >&2
    rm -f "${tmp}"
    return 1
  fi
  if [[ "${count}" -eq 1 ]]; then
    local existing_id
    existing_id=$(printf '%s\n' "${matches}" | sed '/^$/d' | head -1)
    # Fetch the current etag and merge it in — updates require it.
    local merged
    merged=$(mktemp --suffix=.json)
    _merge_dashboard_etag "${existing_id}" "${tmp}" > "${merged}"
    gcloud monitoring dashboards update "${existing_id}" \
      --config-from-file="${merged}" --project="${GCP_PROJECT}"
    rm -f "${merged}"
    gcloud monitoring dashboards describe "${existing_id}" --project="${GCP_PROJECT}"
  else
    local created
    created=$(gcloud monitoring dashboards create \
      --config-from-file="${tmp}" --project="${GCP_PROJECT}" \
      --format="value(name)")
    gcloud monitoring dashboards describe "${created}" --project="${GCP_PROJECT}"
  fi
  rm -f "${tmp}"
}

echo "==> Metrics"
for f in "${ROOT_DIR}"/metrics/*.yaml; do
  apply_metric "${f}"
done

echo "==> Dashboards"
for f in "${ROOT_DIR}"/dashboards/*.yaml; do
  apply_dashboard "${f}"
done

echo "==> Alert policies"
for f in "${ROOT_DIR}"/alerts/*.yaml; do
  apply_alert "${f}"
done

echo "==> apply complete"
