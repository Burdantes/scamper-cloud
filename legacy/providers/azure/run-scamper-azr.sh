#!/bin/bash
set -euo pipefail

if [[ $# -lt 4 ]]
  then
    echo "$0: ip_target_name warts_file_name bucket_name object_name"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/scamper-smoke.sh"
TRACE_ARGS="trace -P UDP-Paris -f 6 -g 10 -q 1"

echo "apt-get update and enable universe"
sudo apt-get update
sudo add-apt-repository universe -y
sudo apt-get update

echo "apt install -y scamper python3-pip dnsutils"
sudo apt install -y scamper python3-pip dnsutils

echo "pip install google-cloud-storage scamper-pywarts pyopenssl"
sudo python3 -m pip install --upgrade google-cloud-storage scamper-pywarts pyopenssl

run_scamper_smoke_test azr "$TRACE_ARGS"

echo "sudo scamper -c trace -P UDP-Paris -f 6 -g 10 -q 1 -p 10000 -f $1 -o $2 -O warts 2>&1"
sudo scamper -c "$TRACE_ARGS" -p 10000 -f "$1" -o "$2" -O warts 2>&1

echo "/usr/bin/env python3 ./upload.py $2 $3 $4"
sudo /usr/bin/env python3 ./upload.py "$2" "$3" "$4" 2>&1
