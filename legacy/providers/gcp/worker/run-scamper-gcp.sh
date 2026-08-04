#!/bin/bash
set -euo pipefail

if [[ $# -lt 4 ]]
  then
    echo "$0: trace_targets [rr_targets] output_prefix bucket_name object_prefix"
    exit 1
fi

TRACE_TARGETS="$1"
if [[ $# -ge 5 ]]; then
  RR_TARGETS="$2"
  RUN_OUTPUT_PREFIX="$3"
  BUCKET_NAME="$4"
  OBJECT_PREFIX="$5"
else
  RR_TARGETS="$1"
  RUN_OUTPUT_PREFIX="$2"
  BUCKET_NAME="$3"
  OBJECT_PREFIX="$4"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/scamper-smoke.sh"
TRACE_ARGS="trace -m 20 -g 8 -w 3 -q 2 -P ICMP"
OUTPUT_DIR="results"
OUTPUT_PREFIX="$OUTPUT_DIR/$RUN_OUTPUT_PREFIX"
PREBUILT_PYTHON="/opt/scamper-worker/venv/bin/python"
if [[ -x "$PREBUILT_PYTHON" ]]; then
  WORKER_PYTHON="$PREBUILT_PYTHON"
else
  WORKER_PYTHON="$(command -v python3 || true)"
fi

if ! command -v scamper >/dev/null 2>&1 \
  || [[ -z "$WORKER_PYTHON" ]] \
  || ! "$WORKER_PYTHON" -c 'from google.cloud import storage' >/dev/null 2>&1; then
  if [[ "${SCAMPER_ALLOW_RUNTIME_INSTALL:-1}" != "1" ]]; then
    echo "Worker image is missing Scamper or the GCS Python client" >&2
    exit 69
  fi
  echo "Worker dependencies missing; installing compatibility fallback"
  sudo apt-get update
  sudo apt-get install -y scamper python3-pip
  sudo pip install google-cloud-storage
  WORKER_PYTHON="$(command -v python3)"
else
  echo "Using preinstalled worker dependencies with $WORKER_PYTHON"
fi

if [[ "${SCAMPER_SKIP_SMOKE:-0}" == "1" ]]; then
  echo "Skipping Scamper smoke test by request"
else
  run_scamper_smoke_test gcp "$TRACE_ARGS"
fi

mkdir -p "$OUTPUT_DIR"
chmod +x ./run_campaign.py

campaign_args=(
  --trace-targets "$TRACE_TARGETS"
  --rr-targets "$RR_TARGETS"
  --output-prefix "$OUTPUT_PREFIX"
  --provider "${SCAMPER_PROVIDER:-gcp}"
  --region "${SCAMPER_REGION:-unknown}"
  --node "${SCAMPER_NODE:-$(hostname)}"
  --trace-target-source "${SCAMPER_TRACE_TARGET_SOURCE:-${SCAMPER_TARGET_SOURCE:-$TRACE_TARGETS}}"
  --trace-target-version "${SCAMPER_TRACE_TARGET_VERSION:-${SCAMPER_TARGET_VERSION:-unknown}}"
  --rr-target-source "${SCAMPER_RR_TARGET_SOURCE:-${SCAMPER_TARGET_SOURCE:-$RR_TARGETS}}"
  --rr-target-version "${SCAMPER_RR_TARGET_VERSION:-${SCAMPER_TARGET_VERSION:-unknown}}"
  --trace-rate "${SCAMPER_TRACE_RATE_PPS:-100}"
  --rr-rate "${SCAMPER_RR_RATE_PPS:-10}"
  --rr-timeout "${SCAMPER_RR_TIMEOUT_SECONDS:-2}"
  --measurements "${SCAMPER_MEASUREMENTS:-trace,rr}"
)
if [[ -n "${SCAMPER_PROBE_PAYLOAD_TEXT:-}" ]]; then
  campaign_args+=(--probe-payload "$SCAMPER_PROBE_PAYLOAD_TEXT")
fi
if [[ -n "${SCAMPER_MEASUREMENT_CONTACT:-}" ]]; then
  campaign_args+=(--measurement-contact "$SCAMPER_MEASUREMENT_CONTACT")
fi
if [[ -n "${SCAMPER_DO_NOT_PROBE_VERSION:-}" ]]; then
  campaign_args+=(--do-not-probe-version "$SCAMPER_DO_NOT_PROBE_VERSION")
fi
if [[ -n "${SCAMPER_TRACE_TARGET_COUNT:-}" ]]; then
  campaign_args+=(--trace-target-count "$SCAMPER_TRACE_TARGET_COUNT")
fi
if [[ -n "${SCAMPER_TRACE_TARGET_SHA256:-}" ]]; then
  campaign_args+=(--trace-target-sha256 "$SCAMPER_TRACE_TARGET_SHA256")
fi
if [[ -n "${SCAMPER_RR_TARGET_COUNT:-}" ]]; then
  campaign_args+=(--rr-target-count "$SCAMPER_RR_TARGET_COUNT")
fi
if [[ -n "${SCAMPER_RR_TARGET_SHA256:-}" ]]; then
  campaign_args+=(--rr-target-sha256 "$SCAMPER_RR_TARGET_SHA256")
fi
campaign_args+=(
  --checkpoint-command
  "$WORKER_PYTHON" ./upload.py "{artifact}" "$BUCKET_NAME" "$OBJECT_PREFIX/{artifact_name}"
)

set +e
sudo "$WORKER_PYTHON" ./run_campaign.py "${campaign_args[@]}"
campaign_status=$?
set -e

shopt -s nullglob
if [[ $campaign_status -eq 0 ]]; then
  artifacts=("$OUTPUT_PREFIX.status.json")
else
  artifacts=("$OUTPUT_PREFIX".*)
fi
if [[ ${#artifacts[@]} -eq 0 ]]; then
  echo "No campaign artifacts were produced" >&2
  exit 1
fi
for artifact in "${artifacts[@]}"; do
  object_name="$OBJECT_PREFIX/$(basename "$artifact")"
  echo "$WORKER_PYTHON ./upload.py $artifact $BUCKET_NAME $object_name"
  sudo "$WORKER_PYTHON" ./upload.py "$artifact" "$BUCKET_NAME" "$object_name"
done

exit "$campaign_status"
