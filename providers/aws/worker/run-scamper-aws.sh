#!/bin/bash
set -euo pipefail

if [[ $# -lt 4 ]]
  then
    echo "$0: ip_target_name output_prefix bucket_name object_prefix"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/scamper-smoke.sh"
TRACE_ARGS="trace -m 20 -g 8 -w 3 -q 2 -P ICMP"
OUTPUT_DIR="results"
OUTPUT_PREFIX="$OUTPUT_DIR/$2"

echo "apt-get update"
sudo apt-get update

echo "apt install -y scamper python3-pip"
sudo apt install -y scamper python3-pip

echo "pip install google-cloud-storage"
sudo pip install google-cloud-storage

run_scamper_smoke_test aws "$TRACE_ARGS"

mkdir -p "$OUTPUT_DIR"
chmod +x ./run_campaign.py

set +e
sudo /usr/bin/env python3 ./run_campaign.py \
  --targets "$1" \
  --output-prefix "$OUTPUT_PREFIX" \
  --provider "${SCAMPER_PROVIDER:-aws}" \
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
