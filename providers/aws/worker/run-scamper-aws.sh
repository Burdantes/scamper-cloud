#!/bin/bash
set -euo pipefail

if [[ $# -lt 4 ]]; then
    echo "$0: trace_targets [rr_targets [trace6_targets]] output_prefix bucket_name object_prefix"
    exit 1
fi

TRACE_TARGETS="$1"
TRACE6_TARGETS=""
if [[ $# -ge 6 ]]; then
  RR_TARGETS="$2"
  TRACE6_TARGETS="$3"
  RUN_OUTPUT_PREFIX="$4"
  BUCKET_NAME="$5"
  OBJECT_PREFIX="$6"
elif [[ $# -ge 5 ]]; then
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

retry_package_command() {
  local attempt=1
  local max_attempts=30
  while ! sudo "$@"; do
    if (( attempt >= max_attempts )); then
      echo "Package command failed after $max_attempts attempts: $*" >&2
      return 1
    fi
    echo "Package manager is busy; retrying in 5 seconds ($attempt/$max_attempts)" >&2
    attempt=$((attempt + 1))
    sleep 5
  done
}

# On first boot cloud-init rewrites apt sources (archive.ubuntu.com -> the
# regional EC2 mirror) after SSH is already open. An apt-get update that runs
# first leaves an index without scamper, and the install fails with "Unable to
# locate package scamper" (canary 2026-10-12). Wait for it, as on Azure.
if command -v cloud-init >/dev/null 2>&1; then
  sudo cloud-init status --wait || true
fi

echo "apt-get update and enable universe"
retry_package_command apt-get update
retry_package_command add-apt-repository universe -y
retry_package_command apt-get update

echo "apt install -y scamper python3-pip"
retry_package_command apt install -y scamper python3-pip

echo "pip install google-cloud-storage"
sudo pip install google-cloud-storage

if [[ "${SCAMPER_SKIP_SMOKE:-0}" == "1" ]]; then
  echo "Skipping Scamper smoke test by request"
else
  run_scamper_smoke_test aws "$TRACE_ARGS"
  if [[ ",${SCAMPER_MEASUREMENTS:-trace,rr}," == *",trace6,"* ]]; then
    run_scamper_smoke_test aws "$TRACE_ARGS" 6
  fi
fi

mkdir -p "$OUTPUT_DIR"
chmod +x ./run_campaign.py

campaign_args=(
  --trace-targets "$TRACE_TARGETS"
  --rr-targets "$RR_TARGETS"
  --output-prefix "$OUTPUT_PREFIX"
  --provider "${SCAMPER_PROVIDER:-aws}"
  --region "${SCAMPER_REGION:-unknown}"
  --node "${SCAMPER_NODE:-$(hostname)}"
  --trace-target-source "${SCAMPER_TRACE_TARGET_SOURCE:-$TRACE_TARGETS}"
  --trace-target-version "${SCAMPER_TRACE_TARGET_VERSION:-unknown}"
  --rr-target-source "${SCAMPER_RR_TARGET_SOURCE:-$RR_TARGETS}"
  --rr-target-version "${SCAMPER_RR_TARGET_VERSION:-unknown}"
  --trace-rate "${SCAMPER_TRACE_RATE_PPS:-100}"
  --rr-rate "${SCAMPER_RR_RATE_PPS:-10}"
  --trace6-rate "${SCAMPER_TRACE6_RATE_PPS:-100}"
  --rr-timeout "${SCAMPER_RR_TIMEOUT_SECONDS:-2}"
  --measurements "${SCAMPER_MEASUREMENTS:-trace,rr}"
)
if [[ -n "$TRACE6_TARGETS" ]]; then
  campaign_args+=(
    --trace6-targets "$TRACE6_TARGETS"
    --trace6-target-source "${SCAMPER_TRACE6_TARGET_SOURCE:-$TRACE6_TARGETS}"
    --trace6-target-version "${SCAMPER_TRACE6_TARGET_VERSION:-unknown}"
  )
fi
[[ -n "${SCAMPER_PROBE_PAYLOAD_TEXT:-}" ]] && campaign_args+=(--probe-payload "$SCAMPER_PROBE_PAYLOAD_TEXT")
[[ -n "${SCAMPER_MEASUREMENT_CONTACT:-}" ]] && campaign_args+=(--measurement-contact "$SCAMPER_MEASUREMENT_CONTACT")
[[ -n "${SCAMPER_DO_NOT_PROBE_VERSION:-}" ]] && campaign_args+=(--do-not-probe-version "$SCAMPER_DO_NOT_PROBE_VERSION")
[[ -n "${SCAMPER_TRACE_TARGET_COUNT:-}" ]] && campaign_args+=(--trace-target-count "$SCAMPER_TRACE_TARGET_COUNT")
[[ -n "${SCAMPER_TRACE_TARGET_SHA256:-}" ]] && campaign_args+=(--trace-target-sha256 "$SCAMPER_TRACE_TARGET_SHA256")
[[ -n "${SCAMPER_RR_TARGET_COUNT:-}" ]] && campaign_args+=(--rr-target-count "$SCAMPER_RR_TARGET_COUNT")
[[ -n "${SCAMPER_RR_TARGET_SHA256:-}" ]] && campaign_args+=(--rr-target-sha256 "$SCAMPER_RR_TARGET_SHA256")
[[ -n "${SCAMPER_TRACE6_TARGET_COUNT:-}" ]] && campaign_args+=(--trace6-target-count "$SCAMPER_TRACE6_TARGET_COUNT")
[[ -n "${SCAMPER_TRACE6_TARGET_SHA256:-}" ]] && campaign_args+=(--trace6-target-sha256 "$SCAMPER_TRACE6_TARGET_SHA256")

# Re-check right before each measurement, not only at launch: a worker can lose
# its IPv6 source hours into a campaign (see scamper-smoke.sh scamper_preflight).
campaign_args+=(--preflight-command "bash $SCRIPT_DIR/scamper-smoke.sh preflight aws {measurement}")
# Chunked mode: each chunk of targets is uploaded the moment it is measured,
# so a stopped or crashed campaign keeps everything up to its last chunk and
# can resume from the chunks already in the bucket.
if [[ "${SCAMPER_CHUNK_TARGETS:-0}" -gt 0 ]]; then
  campaign_args+=(--chunk-targets "$SCAMPER_CHUNK_TARGETS")
  # Resume: skip chunks of this plan already committed by any earlier worker of
  # this region (the parent of this worker's own object prefix).
  campaign_args+=(--completed-chunks-prefix "gs://$BUCKET_NAME/$(dirname "$OBJECT_PREFIX")/")
fi
if [[ "${SCAMPER_CHUNK_TARGETS:-0}" -gt 0 ]]; then
  # Must stay last: --checkpoint-command consumes the rest of the arguments.
  campaign_args+=(--checkpoint-command /usr/bin/env python3 ./upload.py "{artifact}" "$BUCKET_NAME" "$OBJECT_PREFIX/{artifact_name}")
fi

set +e
sudo /usr/bin/env python3 ./run_campaign.py "${campaign_args[@]}"
campaign_status=$?
set -e

shopt -s nullglob
artifacts=("$OUTPUT_PREFIX".*)
if [[ ${#artifacts[@]} -eq 0 ]]; then
  echo "No campaign artifacts were produced" >&2
  exit 1
fi
for artifact in "${artifacts[@]}"; do
  object_name="$OBJECT_PREFIX/$(basename "$artifact")"
  echo "/usr/bin/env python3 ./upload.py $artifact $BUCKET_NAME $object_name"
  sudo /usr/bin/env python3 ./upload.py "$artifact" "$BUCKET_NAME" "$object_name"
done

exit "$campaign_status"
