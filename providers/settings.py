"""Environment-backed settings for direct-provider compatibility drivers."""

from __future__ import annotations

import os

SCAMPER_IP_DST = os.environ.get("SCAMPER_IP_DST", "./datasets/ipv4-24")
SCAMPER_UPLOAD_SCRIPT = "./providers/gcp/worker/upload.py"
SCAMPER_SMOKE_SCRIPT = "./providers/common/worker/scamper-smoke.sh"
SCAMPER_CAMPAIGN_RUNNER = "./experiments/common/run_campaign.py"
WARTS_STORAGE_CREDENTIALS = os.environ.get(
    "WARTS_STORAGE_CREDENTIALS", "./credentials/gcp-service-account.json"
)

GCP_SCAMPER_SCRIPT = "./providers/gcp/worker/run-scamper-gcp.sh"
GCP_SCAMPER_SSH_KEY = os.environ.get("GCP_SCAMPER_SSH_KEY", "~/.ssh/nsf")
GCP_SCAMPER_USER = os.environ.get("GCP_SCAMPER_USER", "scamper-gcp")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "nsf-2148275-66720")
SCAMPER_RESULTS_BUCKET = os.environ.get(
    "SCAMPER_RESULTS_BUCKET", f"{GCP_PROJECT}-scamper-measurements"
)
GCP_SERVICE_ACCOUNT = os.environ.get(
    "GCP_SERVICE_ACCOUNT", "345441712870-compute@developer.gserviceaccount.com"
)
GCP_IMAGE_PROJECT = os.environ.get("GCP_IMAGE_PROJECT", "debian-cloud")
GCP_IMAGE_FAMILY = os.environ.get("GCP_IMAGE_FAMILY", "debian-11")
GCP_MACHINE_TYPE = os.environ.get("GCP_MACHINE_TYPE", "e2-micro")
GCP_NETWORK_TIER = os.environ.get("GCP_NETWORK_TIER", "STANDARD")
GCP_STORAGE_CLASS = "Standard"
GCP_STORAGE_LOCATION = "us-central1"
GCP_SCOPES = [
    "https://www.googleapis.com/auth/devstorage.read_write",
    "https://www.googleapis.com/auth/logging.write",
    "https://www.googleapis.com/auth/monitoring.write",
]

AWS_SCAMPER_VM_SCRIPT = "./providers/aws/worker/run-scamper-aws.sh"
AWS_SCAMPER_SSH_KEY = os.environ.get(
    "AWS_SCAMPER_SSH_KEY", "./credentials/aws-scamper-key-pair.pem"
)
AWS_SCAMPER_USER = os.environ.get("AWS_SCAMPER_USER", "ubuntu")
AZR_SCAMPER_VM_SCRIPT = "./providers/azure/worker/run-scamper-azr.sh"
AZR_SCAMPER_SSH_KEY = os.environ.get(
    "AZR_SCAMPER_SSH_KEY", "./credentials/azr-scamper-key-pair.pem"
)
AZR_SCAMPER_USER = os.environ.get("AZR_SCAMPER_USER", "azureuser")
