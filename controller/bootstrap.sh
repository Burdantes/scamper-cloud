#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 5 ]]; then
  echo "usage: bootstrap.sh BUNDLE PROJECT SERVICE_ACCOUNT BUCKET RELEASE" >&2
  exit 2
fi

bundle=$1
project=$2
service_account=$3
bucket=$4
release=$5
controller_user=scamper-controller
install_root=/opt/scamper-cloud
state_root=/var/lib/scamper-controller
release_dir=${install_root}/releases/${release}

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip openssh-client netcat-openbsd scamper

if ! id "${controller_user}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${state_root}" --shell /bin/bash "${controller_user}"
fi
install -d -o "${controller_user}" -g "${controller_user}" "${state_root}/jobs" "${state_root}/targets" "${state_root}/ssh"
install -d "${install_root}/releases"
install -d "${release_dir}"
tar -xzf "${bundle}" -C "${release_dir}"
chown -R "${controller_user}:${controller_user}" "${release_dir}"

python3 -m venv "${release_dir}/.venv"
"${release_dir}/.venv/bin/pip" install --upgrade pip
"${release_dir}/.venv/bin/pip" install -r "${release_dir}/controller/requirements.txt"

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
GCP_SCAMPER_USER=scamper-gcp
WARTS_STORAGE_CREDENTIALS=/var/lib/scamper-controller/adc-only.json
PYTHONUNBUFFERED=1
EOF
chmod 0644 /etc/scamper-controller.env

install -m 0755 "${release_dir}/controller/controller-status" /usr/local/bin/scamper-controller-status
install -m 0755 "${release_dir}/controller/run-campaign" /usr/local/bin/scamper-controller-run
echo "CONTROLLER_READY release=${release} project=${project} bucket=${bucket}"
