#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${NETTEST_APP_DIR:-/opt/nettest}"
APP_USER="${NETTEST_USER:-nettest}"
APP_GROUP="${NETTEST_GROUP:-$APP_USER}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./install.sh"
  exit 1
fi

echo "Installing NetTest to ${APP_DIR} as ${APP_USER}:${APP_GROUP}"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  python3 \
  python3-venv \
  python3-pip \
  rsync \
  iperf3 \
  mtr-tiny \
  iputils-ping

if ! getent group "${APP_GROUP}" >/dev/null; then
  groupadd --system "${APP_GROUP}"
fi

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash --gid "${APP_GROUP}" "${APP_USER}"
fi

install -d -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}"

rsync -a \
  --exclude ".git/" \
  --exclude ".agents/" \
  --exclude ".codex/" \
  --exclude "venv/" \
  --exclude ".venv/" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude "logs/" \
  --exclude "results/" \
  --exclude "config/config.yaml" \
  "${SRC_DIR}/" "${APP_DIR}/"

install -d -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}/logs" "${APP_DIR}/results"

if [[ ! -f "${APP_DIR}/config/config.yaml" ]]; then
  cp "${APP_DIR}/config/config.example.yaml" "${APP_DIR}/config/config.yaml"
  chown "${APP_USER}:${APP_GROUP}" "${APP_DIR}/config/config.yaml"
  chmod 0640 "${APP_DIR}/config/config.yaml"
  echo "Created ${APP_DIR}/config/config.yaml from example. Edit it before starting services."
else
  echo "Keeping existing ${APP_DIR}/config/config.yaml"
fi

python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install --upgrade pip
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"

install -m 0644 "${APP_DIR}/systemd/nettest.service" /etc/systemd/system/nettest.service
install -m 0644 "${APP_DIR}/systemd/nettest-web.service" /etc/systemd/system/nettest-web.service

systemctl daemon-reload
systemctl enable nettest.service nettest-web.service

cat <<EOF

NetTest installed.

Next steps:
  1. Edit ${APP_DIR}/config/config.yaml
  2. Start services:
       sudo systemctl start nettest
       sudo systemctl start nettest-web
  3. Check logs:
       sudo journalctl -u nettest -f
       sudo journalctl -u nettest-web -f

Dashboard default: http://<server-ip>:8080
EOF

