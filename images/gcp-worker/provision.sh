#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  ca-certificates \
  python3 \
  python3-venv \
  scamper

sudo install -d -m 0755 /opt/scamper-worker
sudo python3 -m venv /opt/scamper-worker/venv
sudo /opt/scamper-worker/venv/bin/pip install --no-cache-dir \
  -r /tmp/scamper-worker-requirements.txt

scamper -v
/opt/scamper-worker/venv/bin/python -c 'from google.cloud import storage'

sudo apt-get clean
sudo find /var/lib/apt/lists -mindepth 1 -delete
rm -f /tmp/scamper-worker-requirements.txt
