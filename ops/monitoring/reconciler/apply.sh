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

Install one of:
  pip install PyYAML>=6.0
  pipx install PyYAML                    # if using pipx
  brew install libyaml && pip install PyYAML   # macOS via Homebrew python
Then re-run this script.
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

# Look up a managed resource by (managed_by, resource_id) userLabels
# rather than by displayName. Returns 0 matches (creates), 1 match
# (updates), or fails on >= 2 matches.
_lookup_alert_by_labels() {
  local resource_id="$1"
  gcloud alpha monitoring policies list \
    --project="${GCP_PROJECT}" \
    --filter="userLabels.managed_by=\"reconciler-monitoring\" AND userLabels.resource_id=\"${resource_id}\"" \
    --format="value(name)"
}

_lookup_dashboard_by_labels() {
  local resource_id="$1"
  gcloud monitoring dashboards list \
    --project="${GCP_PROJECT}" \
    --filter="userLabels.managed_by=\"reconciler-monitoring\" AND userLabels.resource_id=\"${resource_id}\"" \
    --format="value(name)"
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
    gcloud alpha monitoring policies update "${existing_id}" \
      --policy-from-file="${tmp}" --project="${GCP_PROJECT}"
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

apply_dashboard() {
  local file="$1"
  local resource_id
  # Dashboards must also carry the same labels; the YAML file adds
  # them alongside displayName.
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
    gcloud monitoring dashboards update "${existing_id}" \
      --config-from-file="${tmp}" --project="${GCP_PROJECT}"
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
