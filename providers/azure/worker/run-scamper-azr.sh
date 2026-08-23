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
  echo "$0: ip_target_name output_prefix bucket_name object_prefix"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/scamper-smoke.sh"
# Smoke-test args only; the campaign's own probe definition lives in
# experiments/common/run_campaign.py and must not be duplicated here.
TRACE_ARGS="trace -m 20 -g 8 -w 3 -q 2 -P ICMP"
OUTPUT_DIR="results"
OUTPUT_PREFIX="$OUTPUT_DIR/$2"

echo "apt-get update and enable universe"
sudo apt-get update
sudo add-apt-repository universe -y
sudo apt-get update

echo "apt install -y scamper python3-pip"
sudo apt install -y scamper python3-pip

echo "pip install google-cloud-storage"
sudo python3 -m pip install --upgrade google-cloud-storage

run_scamper_smoke_test azr "$TRACE_ARGS"

mkdir -p "$OUTPUT_DIR"
chmod +x ./run_campaign.py

set +e
sudo /usr/bin/env python3 ./run_campaign.py \
  --targets "$1" \
  --output-prefix "$OUTPUT_PREFIX" \
  --provider "${SCAMPER_PROVIDER:-azure}" \
  --region "${SCAMPER_REGION:-unknown}" \
  --node "${SCAMPER_NODE:-$(hostname)}" \
  --target-source "${SCAMPER_TARGET_SOURCE:-$1}" \
  --target-version "${SCAMPER_TARGET_VERSION:-unknown}" \
  --trace-rate "${SCAMPER_TRACE_RATE_PPS:-100}" \
  --rr-rate "${SCAMPER_RR_RATE_PPS:-10}" \
  --rr-timeout "${SCAMPER_RR_TIMEOUT_SECONDS:-2}" \
  --measurements "${SCAMPER_MEASUREMENTS:-trace,rr}"
campaign_status=$?
set -e

shopt -s nullglob
artifacts=("$OUTPUT_PREFIX".*)
if [[ ${#artifacts[@]} -eq 0 ]]; then
  echo "No campaign artifacts were produced" >&2
  exit 1
fi
for artifact in "${artifacts[@]}"; do
  object_name="$4/$(basename "$artifact")"
  echo "/usr/bin/env python3 ./upload.py $artifact $3 $object_name"
  sudo /usr/bin/env python3 ./upload.py "$artifact" "$3" "$object_name"
done

exit "$campaign_status"
