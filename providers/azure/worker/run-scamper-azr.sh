#!/bin/bash
# Azure worker: provision the VM and hand the campaign to the shared runner.
#
# The previous version of this script called scamper directly with
# "trace -P UDP-Paris -f 6 -g 10 -q 1" and uploaded only the warts file. That
# had two consequences worth remembering:
#
#   * it produced no metadata at all - no return_code, no target hashes, no
#     shuffle record - so a run could not be validated or reproduced;
#   * it probed differently from the campaign definition (UDP-Paris starting at
#     TTL 6, rather than ICMP from TTL 1), so its output was not comparable with
#     the GCP or AWS runs.
#
# Those flags were deliberate, not accidental: UDP-Paris avoids per-flow
# load-balancer artifacts and -f 6 skips the provider's internal fabric. They are
# recorded in docs/probe-configurations.md with what each one buys and costs, so
# the reasoning survives this script no longer using them. Reintroduce them as a
# named experiment under experiments/, not by editing this file.
#
# Delegating to run_campaign.py means the probe definition, target validation
# and provenance live in one provider-neutral place instead of drifting per
# provider.
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
# Smoke-test args only; the campaign's own probe definition lives in
# experiments/common/run_campaign.py and must not be duplicated here.
TRACE_ARGS="trace -m 20 -g 8 -w 3 -q 2 -P ICMP"
OUTPUT_DIR="results"
OUTPUT_PREFIX="$OUTPUT_DIR/$RUN_OUTPUT_PREFIX"

echo "apt-get update and enable universe"
sudo apt-get update
sudo add-apt-repository universe -y
sudo apt-get update

echo "apt install -y scamper python3-pip"
sudo apt install -y scamper python3-pip

echo "pip install google-cloud-storage"
sudo python3 -m pip install --upgrade google-cloud-storage

if [[ "${SCAMPER_SKIP_SMOKE:-0}" == "1" ]]; then
  echo "Skipping Scamper smoke test by request"
else
  run_scamper_smoke_test azr "$TRACE_ARGS"
  if [[ ",${SCAMPER_MEASUREMENTS:-trace,rr}," == *",trace6,"* ]]; then
    run_scamper_smoke_test azr "$TRACE_ARGS" 6
  fi
fi

mkdir -p "$OUTPUT_DIR"
chmod +x ./run_campaign.py

campaign_args=(
  --trace-targets "$TRACE_TARGETS"
  --rr-targets "$RR_TARGETS"
  --output-prefix "$OUTPUT_PREFIX"
  --provider "${SCAMPER_PROVIDER:-azure}"
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

set +e
sudo -E /usr/bin/env python3 ./run_campaign.py "${campaign_args[@]}"
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
  sudo -E /usr/bin/env python3 ./upload.py "$artifact" "$BUCKET_NAME" "$object_name"
done

exit "$campaign_status"
