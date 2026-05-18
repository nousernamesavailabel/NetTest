#!/usr/bin/env bash
# =============================================================
# NetTest Controller — Install / Upgrade Script
#
# Usage:
#   sudo ./install.sh              # Fresh install
#   sudo ./install.sh --upgrade    # Upgrade code, keep config and keys
#   sudo ./install.sh --show-key   # Print the controller public key
#
# Environment overrides:
#   NETTEST_APP_DIR   Install path      (default: /opt/nettest)
#   NETTEST_USER      Service user      (default: nettest)
# =============================================================

set -euo pipefail

APP_DIR="${NETTEST_APP_DIR:-/opt/nettest}"
APP_USER="${NETTEST_USER:-nettest}"
APP_GROUP="${APP_USER}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY_FILE="${APP_DIR}/.ssh/nettest_key"

# ── Colour helpers ─────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC}  $*"; }
info() { echo -e "${CYAN}  ·${NC}  $*"; }
warn() { echo -e "${YELLOW}  !${NC}  $*"; }
sep()  { echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

# ── Argument parsing ───────────────────────────────────────
UPGRADE=false
SHOW_KEY=false
SETUP_HTTPS=false
for arg in "$@"; do
  case "$arg" in
    --upgrade)     UPGRADE=true      ;;
    --show-key)    SHOW_KEY=true     ;;
    --setup-https) SETUP_HTTPS=true  ;;
  esac
done

# ── Show key mode ──────────────────────────────────────────
if [[ "$SHOW_KEY" == "true" ]]; then
  if [[ -f "${KEY_FILE}.pub" ]]; then
    sep
    echo ""
    echo "  NetTest Controller Public Key"
    echo "  (add this to agents during onboarding)"
    echo ""
    sep
    cat "${KEY_FILE}.pub"
    sep
  else
    echo "No key found at ${KEY_FILE}.pub"
    echo "Run: sudo ./install.sh  to generate one"
  fi
  exit 0
fi

# ── Must run as root ───────────────────────────────────────
if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./install.sh"
  exit 1
fi

sep
if [[ "$UPGRADE" == "true" ]]; then
  echo -e "  ${CYAN}NetTest Controller — Upgrade${NC}"
else
  echo -e "  ${CYAN}NetTest Controller — Fresh Install${NC}"
fi
echo "  Target: ${APP_DIR}  |  User: ${APP_USER}"
sep
echo ""

# ── System packages ────────────────────────────────────────
info "Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -q \
  python3 \
  python3-venv \
  python3-pip \
  rsync \
  iperf3 \
  mtr-tiny \
  iputils-ping \
  traceroute \
  psmisc \
  nginx \
  openssl
ok "System packages ready"

# ── Create user and group ──────────────────────────────────
if ! getent group "${APP_GROUP}" >/dev/null; then
  groupadd --system "${APP_GROUP}"
  ok "Group '${APP_GROUP}' created"
else
  info "Group '${APP_GROUP}' already exists"
fi

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash \
          --gid "${APP_GROUP}" "${APP_USER}"
  ok "User '${APP_USER}' created"
else
  info "User '${APP_USER}' already exists"
fi

# ── Create app directory ───────────────────────────────────
install -d -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}"
ok "App directory: ${APP_DIR}"

# ── Sync code files ────────────────────────────────────────
info "Syncing application files..."
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
  --exclude ".ssh/" \
  "${SRC_DIR}/" "${APP_DIR}/"
ok "Code files synced"

# ── Create runtime directories ─────────────────────────────
install -d -o "${APP_USER}" -g "${APP_GROUP}" \
  "${APP_DIR}/logs" \
  "${APP_DIR}/results" \
  "${APP_DIR}/packages"
install -d -m 755 /opt/nettest/ssl
ok "Runtime directories ready"

# ── Config file ────────────────────────────────────────────
if [[ ! -f "${APP_DIR}/config/config.yaml" ]]; then
  cp "${APP_DIR}/config/config.example.yaml" \
     "${APP_DIR}/config/config.yaml"
  chown "${APP_USER}:${APP_GROUP}" "${APP_DIR}/config/config.yaml"
  chmod 0640 "${APP_DIR}/config/config.yaml"
  ok "Created config/config.yaml from example"
  warn "Edit ${APP_DIR}/config/config.yaml before starting services"
else
  info "Keeping existing config/config.yaml"
fi

# ── Python virtual environment ─────────────────────────────
info "Setting up Python virtual environment..."
if [[ ! -d "${APP_DIR}/venv" ]] || [[ "$UPGRADE" == "true" ]]; then
  python3 -m venv "${APP_DIR}/venv"
  "${APP_DIR}/venv/bin/pip" install --upgrade pip -q
  "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt" -q
  ok "Python environment ready"
else
  info "venv exists — running pip install to sync deps..."
  "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt" -q
  ok "Dependencies up to date"
fi

# ── Fix ownership ──────────────────────────────────────────
chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"
# Keep .ssh permissions strict
if [[ -d "${APP_DIR}/.ssh" ]]; then
  chmod 700 "${APP_DIR}/.ssh"
  chmod 600 "${APP_DIR}/.ssh/nettest_key" 2>/dev/null || true
  chmod 644 "${APP_DIR}/.ssh/nettest_key.pub" 2>/dev/null || true
fi
ok "Ownership and permissions set"

# ── SSH key for agent access ───────────────────────────────
mkdir -p "${APP_DIR}/.ssh"
chown "${APP_USER}:${APP_GROUP}" "${APP_DIR}/.ssh"
chmod 700 "${APP_DIR}/.ssh"

if [[ ! -f "${KEY_FILE}" ]]; then
  info "Generating SSH key for agent access..."
  sudo -u "${APP_USER}" ssh-keygen \
    -t ed25519 \
    -f "${KEY_FILE}" \
    -C "nettest-controller" \
    -N ""
  chmod 600 "${KEY_FILE}"
  chmod 644 "${KEY_FILE}.pub"
  ok "SSH key generated: ${KEY_FILE}"
else
  info "SSH key already exists — keeping existing key"
  info "(run with --show-key to display it)"
fi

# ── Allow nettest user to restart scheduler without password ─
SUDOERS_FILE="/etc/sudoers.d/nettest-restart"
cat > "${SUDOERS_FILE}" << SUDOERS
# Allow nettest service user to restart the scheduler
# (triggered automatically when config is saved from the web UI)
# dpkg is needed for air-gapped agent package installation
${APP_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart nettest, /usr/bin/dpkg, /usr/bin/systemctl restart nginx, /usr/bin/systemctl reload nginx, /usr/bin/systemctl reload-or-restart nginx, /usr/bin/systemctl stop nginx, /usr/bin/systemctl enable nginx, /usr/bin/systemctl start nginx, /usr/bin/tee, /usr/bin/ln, /usr/bin/rm
SUDOERS
chmod 440 "${SUDOERS_FILE}"
visudo -c -f "${SUDOERS_FILE}" > /dev/null 2>&1 && \
  ok "Sudoers entry for scheduler restart configured" || \
  warn "Sudoers validation failed — check ${SUDOERS_FILE}"

# ── nginx reverse proxy setup ──────────────────────────────
info "Configuring nginx reverse proxy..."
NGINX_CONF="/etc/nginx/sites-available/nettest"
cat > "${NGINX_CONF}" << 'NGINXCONF'
server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name _;

    ssl_certificate     /opt/nettest/ssl/nettest.crt;
    ssl_certificate_key /opt/nettest/ssl/nettest.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 10m;

    proxy_buffering           off;
    proxy_cache               off;
    chunked_transfer_encoding on;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   Connection        "";
        proxy_read_timeout 300s;
    }
}
NGINXCONF
ln -sf "${NGINX_CONF}" /etc/nginx/sites-enabled/nettest
rm -f /etc/nginx/sites-enabled/default

# Generate self-signed cert if none exists
if [[ ! -f /opt/nettest/ssl/nettest.crt ]]; then
  SERVER_IP=$(hostname -I | awk '{print $1}')
  openssl req -x509 -nodes -newkey rsa:4096 \
    -keyout /opt/nettest/ssl/nettest.key \
    -out    /opt/nettest/ssl/nettest.crt \
    -days 3650 \
    -subj "/CN=${SERVER_IP}/O=NetTest" \
    -addext "subjectAltName=IP:${SERVER_IP}" \
    2>/dev/null
  chmod 600 /opt/nettest/ssl/nettest.key
  chmod 644 /opt/nettest/ssl/nettest.crt
  ok "Self-signed certificate generated for ${SERVER_IP}"
else
  info "SSL certificate already exists — keeping existing cert"
fi

if nginx -t 2>/dev/null; then
  systemctl enable nginx
  systemctl restart nginx
  ok "nginx configured and started"
else
  warn "nginx config test failed — check /etc/nginx/sites-available/nettest"
fi

# ── Systemd service files ──────────────────────────────────
info "Installing systemd services..."
install -m 0644 "${APP_DIR}/systemd/nettest.service" \
  /etc/systemd/system/nettest.service
install -m 0644 "${APP_DIR}/systemd/nettest-web.service" \
  /etc/systemd/system/nettest-web.service
systemctl daemon-reload
systemctl enable nettest.service nettest-web.service
ok "Services installed and enabled"

if [[ "$UPGRADE" == "true" ]]; then
  info "Restarting services..."
  systemctl restart nettest.service
  systemctl restart nettest-web.service
  ok "Services restarted"
fi

# ── Done ───────────────────────────────────────────────────
echo ""
sep
if [[ "$UPGRADE" == "true" ]]; then
  echo -e "  ${GREEN}Upgrade complete!${NC}"
else
  echo -e "  ${GREEN}Installation complete!${NC}"
fi
sep
echo ""

if [[ "$UPGRADE" == "false" ]]; then
  echo "  Next steps:"
  echo ""
  echo "  1. Edit the config file:"
  echo "     sudo nano ${APP_DIR}/config/config.yaml"
  echo ""
  echo "  2. Start the services:"
  echo "     sudo systemctl start nettest"
  echo "     sudo systemctl start nettest-web"
  echo ""
  echo "  3. Check service status:"
  echo "     sudo systemctl status nettest nettest-web"
  echo ""
  echo "  4. Open the dashboard:"
  echo "     http://<this-server-ip>:8080    (direct, always available)"
  echo "     https://<this-server-ip>        (HTTPS via nginx)"
  echo ""
  echo "     Note: HTTPS uses a self-signed certificate."
  echo "     Your browser will show a security warning — this is expected."
  echo "     Add an exception or configure a real cert via Config → HTTPS."
  echo ""
  echo "  5. Air-gapped agents only:"
  echo "     Upload .deb packages via Config → Packages in the web UI"
  echo "     before onboarding any air-gapped agents."
  echo "     Required packages: iperf3, libiperf0, libsctp1, mtr-tiny,"
  echo "     iputils-ping, traceroute, psmisc"
  echo ""
fi

echo "  Controller public key"
echo "  (needed when onboarding agents):"
echo ""
cat "${KEY_FILE}.pub"
echo ""
sep
echo ""
