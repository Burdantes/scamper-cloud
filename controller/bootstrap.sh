#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 7 ]]; then
  echo "usage: bootstrap.sh BUNDLE PROJECT SERVICE_ACCOUNT BUCKET RELEASE REVISION BUNDLE_SHA256" >&2
  exit 2
fi

bundle=$1
project=$2
service_account=$3
bucket=$4
release=$5
revision=$6
expected_bundle_sha256=$7
controller_user=scamper-controller
install_root=/opt/scamper-cloud
state_root=/var/lib/scamper-controller
release_dir=${install_root}/releases/${release}
timer_was_enabled=false
if systemctl is-enabled --quiet scamper-monthly.timer 2>/dev/null; then
  timer_was_enabled=true
fi

actual_bundle_sha256=$(sha256sum "${bundle}" | awk '{print $1}')
if [[ "${actual_bundle_sha256}" != "${expected_bundle_sha256}" ]]; then
  echo "bundle SHA-256 mismatch: expected ${expected_bundle_sha256}, got ${actual_bundle_sha256}" >&2
  exit 65
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip openssh-client netcat-openbsd scamper

if ! id "${controller_user}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${state_root}" --shell /bin/bash "${controller_user}"
fi
install -d -o "${controller_user}" -g "${controller_user}" "${state_root}/jobs" "${state_root}/targets" "${state_root}/ssh" "${state_root}/.aws"
install -d "${install_root}/releases"
install -d "${release_dir}"
tar -xzf "${bundle}" -C "${release_dir}"
chown -R "${controller_user}:${controller_user}" "${release_dir}"

python3 -m venv "${release_dir}/.venv"
"${release_dir}/.venv/bin/pip" install --upgrade pip
"${release_dir}/.venv/bin/pip" install -r "${release_dir}/controller/requirements.txt"
"${release_dir}/.venv/bin/pip" install -r "${release_dir}/controller/requirements-test.txt"

# Test the staged release on the controller's actual Python and operating system.
# The active symlink is not changed unless every required test passes.
(
  cd "${release_dir}"
  "${release_dir}/.venv/bin/python" -m pytest tests/required
)

cat > "${release_dir}/release.json" <<EOF
{
  "schema_version": 1,
  "release": "${release}",
  "source_revision": "${revision}",
  "bundle_sha256": "${actual_bundle_sha256}"
}
EOF
chown "${controller_user}:${controller_user}" "${release_dir}/release.json"

if [[ ! -f "${state_root}/ssh/id_ed25519" ]]; then
  sudo -u "${controller_user}" ssh-keygen -q -t ed25519 -N '' -C scamper-controller -f "${state_root}/ssh/id_ed25519"
fi

ln -sfn "${release_dir}" "${install_root}/current"
cat > /etc/scamper-controller.env <<EOF
GCP_PROJECT=${project}
GCP_SERVICE_ACCOUNT=${service_account}
SCAMPER_RESULTS_BUCKET=${bucket}
GCP_NETWORK_TIER=STANDARD
GCP_SCAMPER_SSH_KEY=${state_root}/ssh/id_ed25519
# Every provider uses the key the controller generated for itself above. The
# defaults in providers/settings.py are repo-local ./credentials/*.pem paths that
# exist only in a developer checkout, so a controller-launched AWS or Azure
# campaign died on FileNotFoundError reading the public half. Pointing all three
# here also means no private key is ever copied onto this host.
AWS_SCAMPER_SSH_KEY=${state_root}/ssh/id_ed25519
AWS_CONFIG_FILE=${state_root}/.aws/config
AWS_SDK_LOAD_CONFIG=1
AWS_DEFAULT_REGION=us-east-1
AWS_EC2_METADATA_DISABLED=true
AZR_SCAMPER_SSH_KEY=${state_root}/ssh/id_ed25519
GCP_SCAMPER_USER=scamper-gcp
WARTS_STORAGE_CREDENTIALS=/var/lib/scamper-controller/adc-only.json
PYTHONUNBUFFERED=1
EOF
chmod 0644 /etc/scamper-controller.env

# Provider credentials are operator-provisioned, never generated here and never
# committed. AWS exchanges the VM's native Google identity for one-hour role
# credentials; no long-lived AWS access key belongs on the controller.
if [[ ! -f /etc/scamper-controller-secrets.env ]]; then
  cat > /etc/scamper-controller-secrets.env <<'SECRETS'
# Fill in to launch non-GCP campaigns from this controller, then chmod 0600.
# Azure (service principal):
#   AZURE_TENANT_ID=
#   AZURE_CLIENT_ID=
#   AZURE_CLIENT_SECRET=
#   AZURE_SUBSCRIPTION_ID=
# AWS:
#   AWS_ROLE_ARN=arn:aws:iam::123456789012:role/ScamperCloudController
#   AWS_EXPECTED_ACCOUNT_ID=123456789012
#   AWS_GCP_AUDIENCE=scamper-controller-aws
#   SCAMPER_AWS_SSH_CIDR=192.0.2.10/32
SECRETS
fi
# Readable by the controller user, because run-campaign executes as that user
# via systemd --uid=scamper-controller. Root-owned 0600 would be unreadable to it
# and every non-GCP campaign would fail with CredentialUnavailableError.
chown "${controller_user}:${controller_user}" /etc/scamper-controller-secrets.env
chmod 0600 /etc/scamper-controller-secrets.env

cat > "${state_root}/.aws/config" <<EOF
[default]
credential_process = /usr/local/bin/scamper-controller-aws-credentials
region = us-east-1
output = json
EOF
chown "${controller_user}:${controller_user}" "${state_root}/.aws/config"
chmod 0600 "${state_root}/.aws/config"

install -m 0755 "${release_dir}/controller/controller-status" /usr/local/bin/scamper-controller-status
install -m 0755 "${release_dir}/controller/run-campaign" /usr/local/bin/scamper-controller-run
install -m 0755 "${release_dir}/controller/run-monthly" /usr/local/bin/scamper-controller-monthly
install -m 0755 "${release_dir}/controller/run-aws-credentials" /usr/local/bin/scamper-controller-aws-credentials
install -m 0755 "${release_dir}/controller/run-aws-setup" /usr/local/bin/scamper-controller-aws
install -m 0644 "${release_dir}/controller/scamper-monthly.service" /etc/systemd/system/scamper-monthly.service
install -m 0644 "${release_dir}/controller/scamper-monthly.timer" /etc/systemd/system/scamper-monthly.timer
if [[ ! -f /etc/scamper-controller-monthly.json ]]; then
  install -m 0600 "${release_dir}/controller/monthly-config.example.json" /etc/scamper-controller-monthly.json
fi
systemctl daemon-reload
# Preserve a previously approved schedule across safe releases, but never let a
# deployment turn on a new monthly schedule. Initial enablement is a deliberate
# operator action after regional preparation and live provider canaries.
if [[ "${timer_was_enabled}" == true ]] && \
   /usr/local/bin/scamper-controller-monthly check >/dev/null 2>&1; then
  systemctl enable --now scamper-monthly.timer
else
  systemctl disable --now scamper-monthly.timer >/dev/null 2>&1 || true
fi
echo "CONTROLLER_READY release=${release} project=${project} bucket=${bucket}"
