#!/usr/bin/env bash
# =============================================================
# LinkState Controller — Install / Upgrade Script
#
# Usage:
#   sudo ./install.sh                    # Fresh install
#   sudo ./install.sh --upgrade          # Upgrade code, keep config and keys
#   sudo ./install.sh --show-key         # Print the controller public key
#   sudo ./install.sh --setup-local-user # Add/update a local dashboard login
#
# Environment overrides:
#   LINKSTATE_APP_DIR        Install path  (default: /opt/linkstate)
#   LINKSTATE_USER           Service user  (default: linkstate)
#   LINKSTATE_ONLINE         "false" for an offline / air-gapped install
#                          (skips the interactive prompt). Release bundles
#                          ship their dependencies in vendor/ and install
#                          offline automatically; set "true" to use apt
#                          and PyPI instead.
#   LINKSTATE_PACKAGES_DIR   Directory of .deb files for an offline install
#                          (skips the interactive prompt)
#   LINKSTATE_WHEELS_DIR     Directory of .whl files for an offline install
#                          (skips the interactive prompt)
#   LINKSTATE_LOCAL_USER     Username for --setup-local-user (skips the prompt)
#   LINKSTATE_LOCAL_PASSWORD Password for --setup-local-user (skips the prompt)
# =============================================================

set -euo pipefail

APP_DIR="${LINKSTATE_APP_DIR:-/opt/linkstate}"
APP_USER="${LINKSTATE_USER:-linkstate}"
APP_GROUP="${APP_USER}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY_FILE="${APP_DIR}/.ssh/linkstate_key"

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
SETUP_LOCAL_USER=false
for arg in "$@"; do
  case "$arg" in
    --upgrade)          UPGRADE=true          ;;
    --show-key)         SHOW_KEY=true         ;;
    --setup-https)      SETUP_HTTPS=true      ;;
    --setup-local-user) SETUP_LOCAL_USER=true ;;
  esac
done

# ── Show key mode ──────────────────────────────────────────
if [[ "$SHOW_KEY" == "true" ]]; then
  if [[ -f "${KEY_FILE}.pub" ]]; then
    sep
    echo ""
    echo "  LinkState Controller Public Key"
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

# ── Setup local user mode ──────────────────────────────────
# Adds (or resets the password of) a local dashboard login account, stored
# as a salted hash in config/config.yaml — no external RADIUS server
# needed. If RADIUS is already the active login method, this account also
# serves as an automatic fallback login whenever the RADIUS server can't
# be reached.
if [[ "$SETUP_LOCAL_USER" == "true" ]]; then
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: sudo ./install.sh --setup-local-user"
    exit 1
  fi

  CONFIG_FILE="${APP_DIR}/config/config.yaml"
  if [[ ! -f "${CONFIG_FILE}" ]]; then
    echo "No config found at ${CONFIG_FILE} — run the installer first (sudo ./install.sh)."
    exit 1
  fi
  if [[ ! -x "${APP_DIR}/venv/bin/python3" ]]; then
    echo "No Python venv found at ${APP_DIR}/venv — run the installer first (sudo ./install.sh)."
    exit 1
  fi

  sep
  echo -e "  ${CYAN}LinkState — Local Dashboard Login${NC}"
  sep
  echo ""

  LOCAL_USERNAME="${LINKSTATE_LOCAL_USER:-}"
  if [[ -z "$LOCAL_USERNAME" ]]; then
    read -r -p "  Username: " LOCAL_USERNAME || true
  fi
  if [[ -z "$LOCAL_USERNAME" ]]; then
    echo "Username cannot be empty"
    exit 1
  fi

  LOCAL_PASSWORD="${LINKSTATE_LOCAL_PASSWORD:-}"
  if [[ -z "$LOCAL_PASSWORD" ]]; then
    read -r -s -p "  Password: " LOCAL_PASSWORD || true; echo ""
    read -r -s -p "  Confirm password: " LOCAL_PASSWORD_CONFIRM || true; echo ""
    if [[ "$LOCAL_PASSWORD" != "$LOCAL_PASSWORD_CONFIRM" ]]; then
      echo "Passwords do not match"
      exit 1
    fi
  fi
  if [[ -z "$LOCAL_PASSWORD" ]]; then
    echo "Password cannot be empty"
    exit 1
  fi
  if [[ "${#LOCAL_PASSWORD}" -lt 8 ]]; then
    echo "Password must be at least 8 characters"
    exit 1
  fi

  echo ""
  info "Hashing password and updating config..."
  LINKSTATE_LU_USER="$LOCAL_USERNAME" LINKSTATE_LU_PASS="$LOCAL_PASSWORD" \
    "${APP_DIR}/venv/bin/python3" - "$CONFIG_FILE" << 'PYEOF'
import os, sys
import yaml
from werkzeug.security import generate_password_hash

config_path = sys.argv[1]
username = os.environ["LINKSTATE_LU_USER"]
password = os.environ["LINKSTATE_LU_PASS"]

with open(config_path) as f:
    raw = yaml.safe_load(f)

auth  = raw.setdefault("auth", {})
users = auth.setdefault("local_users", [])
pw_hash = generate_password_hash(password)

for u in users:
    if u.get("username") == username:
        u["password_hash"] = pw_hash
        break
else:
    users.append({"username": username, "password_hash": pw_hash})

# Only set a method if one isn't already active. If RADIUS is already
# configured, leave it as the primary method — this new local account
# becomes its fallback instead of replacing it.
if auth.get("method") in (None, "", "none"):
    auth["method"] = "radius" if auth.get("radius_server") else "local"

with open(config_path, "w") as f:
    yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

print(f"auth.method is now: {auth['method']!r}")
PYEOF

  chown "${APP_USER}:${APP_GROUP}" "${CONFIG_FILE}"
  chmod 0640 "${CONFIG_FILE}"
  ok "Local user '${LOCAL_USERNAME}' saved"
  echo ""

  if systemctl is-active --quiet linkstate-web 2>/dev/null; then
    info "Restarting linkstate-web to apply..."
    systemctl restart linkstate-web
    ok "linkstate-web restarted"
  else
    warn "linkstate-web isn't running — start it to apply this change:"
    echo "     sudo systemctl start linkstate-web"
  fi
  echo ""
  exit 0
fi

# ── Must run as root ───────────────────────────────────────
if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./install.sh"
  exit 1
fi

# ── Migrate a NetTest install to LinkState ─────────────────
# LinkState was formerly called NetTest. If this host has a NetTest
# install and no LinkState one, move it over: app directory, service user
# and group (same UID/GID), systemd units, sudoers entry, nginx site, SSH key
# and certificate file names, and paths in config.yaml. Config, results,
# logs, snapshots and the key itself are kept, so onboarded agents keep
# working. Agents onboarded under NetTest log in as 'nettest', which is
# pinned on each of them in config.yaml. Re-onboarding moves an agent to the
# new default user.
LEGACY_APP_DIR="${NETTEST_APP_DIR:-/opt/nettest}"
LEGACY_USER="nettest"
MIGRATED_FROM_LEGACY=false
if [[ ! -e "${APP_DIR}" && -f "${LEGACY_APP_DIR}/version.txt" ]]; then
  sep
  echo -e "  ${CYAN}Migrating NetTest → LinkState${NC}"
  echo "  ${LEGACY_APP_DIR} → ${APP_DIR}  |  user ${LEGACY_USER} → ${APP_USER}"
  sep
  echo ""

  info "Stopping and removing NetTest services..."
  systemctl stop nettest.service nettest-web.service 2>/dev/null || true
  systemctl stop 'nettest-iperf3@*.service' 2>/dev/null || true
  systemctl disable nettest.service nettest-web.service 2>/dev/null || true
  rm -f /etc/systemd/system/multi-user.target.wants/nettest-iperf3@*.service
  rm -f /etc/systemd/system/nettest.service /etc/systemd/system/nettest-web.service \
        /etc/systemd/system/nettest-iperf3@.service
  systemctl daemon-reload
  systemctl reset-failed 2>/dev/null || true
  rm -f /etc/sudoers.d/nettest-restart
  rm -f /etc/nginx/sites-enabled/nettest /etc/nginx/sites-available/nettest
  ok "NetTest units, sudoers entry and nginx site removed"

  mv "${LEGACY_APP_DIR}" "${APP_DIR}"
  ok "Moved ${LEGACY_APP_DIR} → ${APP_DIR}"
  # Installing from inside the old directory (e.g. a dev copy): follow the move
  if [[ "${SRC_DIR}" == "${LEGACY_APP_DIR}" || "${SRC_DIR}" == "${LEGACY_APP_DIR}/"* ]]; then
    SRC_DIR="${APP_DIR}${SRC_DIR#"${LEGACY_APP_DIR}"}"
  fi

  # venv console scripts (gunicorn, pip, ...) have the venv path in their
  # shebang lines, and the activate scripts set it too
  if [[ -d "${APP_DIR}/venv/bin" ]]; then
    grep -rlI --null -F "${LEGACY_APP_DIR}/venv" "${APP_DIR}/venv/bin" "${APP_DIR}/venv/pyvenv.cfg" 2>/dev/null |
      xargs -0 -r sed -i "s|${LEGACY_APP_DIR}/venv|${APP_DIR}/venv|g" || true
    ok "Python environment paths updated"
  fi

  if id -u "${LEGACY_USER}" >/dev/null 2>&1 && ! id -u "${APP_USER}" >/dev/null 2>&1; then
    # usermod refuses while the user has processes (e.g. a stray iperf3)
    pkill -u "${LEGACY_USER}" 2>/dev/null || true
    sleep 1
    pkill -KILL -u "${LEGACY_USER}" 2>/dev/null || true
    usermod -l "${APP_USER}" "${LEGACY_USER}"
    if [[ "$(getent passwd "${APP_USER}" | cut -d: -f6)" == "/home/${LEGACY_USER}" ]]; then
      if [[ -d "/home/${LEGACY_USER}" && ! -e "/home/${APP_USER}" ]]; then
        usermod -d "/home/${APP_USER}" -m "${APP_USER}"
      else
        usermod -d "/home/${APP_USER}" "${APP_USER}"
      fi
    fi
    ok "Renamed user '${LEGACY_USER}' → '${APP_USER}' (UID $(id -u "${APP_USER}"))"
  fi
  if getent group "${LEGACY_USER}" >/dev/null && ! getent group "${APP_GROUP}" >/dev/null; then
    groupmod -n "${APP_GROUP}" "${LEGACY_USER}"
    ok "Renamed group '${LEGACY_USER}' → '${APP_GROUP}' (members kept)"
  fi

  for ext in "" ".pub"; do
    if [[ -f "${APP_DIR}/.ssh/nettest_key${ext}" && ! -e "${APP_DIR}/.ssh/linkstate_key${ext}" ]]; then
      mv "${APP_DIR}/.ssh/nettest_key${ext}" "${APP_DIR}/.ssh/linkstate_key${ext}"
    fi
  done
  for ext in crt key; do
    if [[ -f "${APP_DIR}/ssl/nettest.${ext}" && ! -e "${APP_DIR}/ssl/linkstate.${ext}" ]]; then
      mv "${APP_DIR}/ssl/nettest.${ext}" "${APP_DIR}/ssl/linkstate.${ext}"
    fi
  done
  ok "SSH key and certificate renamed (contents unchanged)"

  MIG_CONFIG="${APP_DIR}/config/config.yaml"
  if [[ -f "${MIG_CONFIG}" ]]; then
    cp -p "${MIG_CONFIG}" "${MIG_CONFIG}.pre-linkstate"
    "${APP_DIR}/venv/bin/python3" - "${MIG_CONFIG}" "${LEGACY_APP_DIR}" "${APP_DIR}" "${APP_USER}" << 'PYEOF'
import sys
import yaml

path, old_dir, new_dir, new_user = sys.argv[1:]
with open(path) as f:
    raw = yaml.safe_load(f) or {}

old_key = f"{old_dir}/.ssh/nettest_key"
new_key = f"{new_dir}/.ssh/linkstate_key"

def fix(v):
    if isinstance(v, dict):
        return {k: fix(x) for k, x in v.items()}
    if isinstance(v, list):
        return [fix(x) for x in v]
    if isinstance(v, str):
        if v == old_key or v.startswith(old_key + "."):
            return new_key + v[len(old_key):]
        if v == old_dir or v.startswith(old_dir + "/"):
            return new_dir + v[len(old_dir):]
    return v

raw = fix(raw)

ctl = raw.get("controller") or {}
if ctl.get("name") == "NetTest Controller":
    ctl["name"] = "LinkState Controller"

# Agents onboarded so far log in as 'nettest'. Pin that on each of them,
# then make the new default user apply to agents onboarded from now on.
sd = raw.setdefault("ssh_defaults", {})
if (sd.get("username") or "nettest") == "nettest":
    for a in raw.get("agents") or []:
        if not a.get("username"):
            a["username"] = "nettest"
    sd["username"] = new_user

with open(path, "w") as f:
    yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
PYEOF
    ok "config.yaml updated (previous copy: config/config.yaml.pre-linkstate)"
  fi

  MIGRATED_FROM_LEGACY=true
  if [[ "$UPGRADE" != "true" ]]; then
    UPGRADE=true
    info "Existing install found — continuing as an upgrade"
  fi
  echo ""
fi

sep
CURRENT_VERSION="unknown"
if [[ -f "${APP_DIR}/version.txt" ]]; then
  CURRENT_VERSION=$(cat "${APP_DIR}/version.txt" | tr -d "[:space:]")
fi
INCOMING_VERSION="unknown"
if [[ -f "${SRC_DIR}/version.txt" ]]; then
  INCOMING_VERSION=$(cat "${SRC_DIR}/version.txt" | tr -d "[:space:]")
fi

if [[ "$UPGRADE" == "true" ]]; then
  echo -e "  ${CYAN}LinkState Controller — Upgrade${NC}"
  echo "  ${CURRENT_VERSION} → ${INCOMING_VERSION}"
else
  echo -e "  ${CYAN}LinkState Controller — Fresh Install${NC}"
  echo "  Version : ${INCOMING_VERSION}"
fi
echo "  Target: ${APP_DIR}  |  User: ${APP_USER}"
sep
echo ""

# ── Connectivity: online or offline (air-gapped)? ─────────
# Governs how both system packages and Python wheels are obtained.
# A release bundle (make_release.sh) carries every dependency in vendor/,
# so it installs offline without asking.
BUNDLED_DEBS="${SRC_DIR}/vendor/debs"
BUNDLED_WHEELS="${SRC_DIR}/vendor/wheels"
HAS_BUNDLED_DEBS=false; HAS_BUNDLED_WHEELS=false
compgen -G "${BUNDLED_DEBS}/*.deb"   > /dev/null && HAS_BUNDLED_DEBS=true
compgen -G "${BUNDLED_WHEELS}/*.whl" > /dev/null && HAS_BUNDLED_WHEELS=true

ONLINE="${LINKSTATE_ONLINE:-}"
if [[ -z "$ONLINE" && ( -n "${LINKSTATE_PACKAGES_DIR:-}" || -n "${LINKSTATE_WHEELS_DIR:-}" ) ]]; then
  ONLINE=false
fi
if [[ -z "$ONLINE" && "$HAS_BUNDLED_DEBS" == "true" && "$HAS_BUNDLED_WHEELS" == "true" ]]; then
  ONLINE=false
  info "Using dependencies bundled in vendor/ — no internet connection needed"
fi
if [[ -z "$ONLINE" && "$UPGRADE" != "true" ]]; then
  echo ""
  _ans=""
  read -r -p "  Will this server have an internet connection? [Y/n] " _ans || true
  case "${_ans,,}" in
    n|no) ONLINE=false ;;
    *)    ONLINE=true  ;;
  esac
fi
ONLINE="${ONLINE:-true}"
[[ "$ONLINE" == "false" ]] && warn "Offline / air-gapped install — apt and PyPI will not be used"

# ── System packages ────────────────────────────────────────
# Top-level packages the controller needs. For an offline install the
# packages directory must also hold any of their dependencies that are
# not already present on the target system.
REQUIRED_PACKAGES=(
  python3
  python3-venv
  python3-pip
  rsync
  iperf3
  mtr-tiny
  iputils-ping
  traceroute
  psmisc
  nginx
  openssl
)

if [[ "$UPGRADE" == "true" ]]; then
  info "Upgrade mode — skipping system package install"
elif [[ "$ONLINE" != "false" ]]; then
  # ── Online: install from apt (unchanged behaviour) ──
  info "Installing system packages..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -q "${REQUIRED_PACKAGES[@]}"
  ok "System packages ready"
else
  # ── Offline: install from a local directory of .deb files ──
  PKG_DIR="${LINKSTATE_PACKAGES_DIR:-}"
  if [[ -z "$PKG_DIR" && "$HAS_BUNDLED_DEBS" == "true" ]]; then
    PKG_DIR="${BUNDLED_DEBS}"
  fi

  if [[ -z "$PKG_DIR" ]]; then
    OS_ID="ubuntu"; OS_VER="24.04"
    [[ -r /etc/os-release ]] && OS_VER="$(. /etc/os-release; echo "${VERSION_ID:-$OS_VER}")"

    warn "Offline install — system packages will come from a local directory"
    echo ""
    echo "  Needed: ${REQUIRED_PACKAGES[*]}"
    echo ""
    echo "  Release bundles built by make_release.sh already include these in"
    echo "  vendor/debs. Otherwise, collect them plus every dependency on an"
    echo "  online machine of the SAME OS release. Two ways — the installer only"
    echo "  installs what the target is actually missing from either:"
    echo ""
    echo "  a) Any online box (apt-get download ignores what that box already"
    echo "     has installed, so it works even on a fully-provisioned machine):"
    echo ""
    echo "        mkdir pkgs && cd pkgs"
    echo "        apt-get download \$(apt-cache depends --recurse --no-recommends \\"
    echo "          --no-suggests --no-conflicts --no-breaks --no-replaces \\"
    echo "          --no-enhances ${REQUIRED_PACKAGES[*]} | grep '^[a-z0-9]' | sort -u)"
    echo ""
    echo "  b) Fresh container (smaller bundle — base already matches):"
    echo ""
    echo "        docker run --rm -v \"\$PWD/pkgs:/pkgs\" ${OS_ID}:${OS_VER} sh -c '\\"
    echo "          apt-get update && apt-get install -y --no-install-recommends \\"
    echo "            --download-only ${REQUIRED_PACKAGES[*]} && \\"
    echo "          cp /var/cache/apt/archives/*.deb /pkgs/'"
    echo ""
    echo "  Then point this installer at that 'pkgs' directory."
    echo ""
  fi

  while true; do
    if [[ -z "$PKG_DIR" ]]; then
      read -r -p "  Path to directory containing the .deb packages: " PKG_DIR || true
    fi
    PKG_DIR="${PKG_DIR/#\~/$HOME}"
    if [[ -n "$PKG_DIR" ]] && [[ -d "$PKG_DIR" ]] && compgen -G "${PKG_DIR}/*.deb" > /dev/null; then
      break
    fi
    warn "No .deb files found in: ${PKG_DIR:-<none entered>}"
    [[ -n "${LINKSTATE_PACKAGES_DIR:-}" ]] && exit 1
    PKG_DIR=""
  done
  PKG_DIR="$(cd "$PKG_DIR" && pwd)"

  info "Installing system packages from ${PKG_DIR}..."
  export DEBIAN_FRONTEND=noninteractive

  # Serve the directory to apt as a temporary local repository and install
  # the package *names*, not the files. apt then installs only what this
  # server is missing, never downgrades anything already installed, and
  # skips alternatives it doesn't need. Only this repo is consulted, so
  # nothing reaches for the network.
  LOCAL_REPO="$(mktemp -d /tmp/linkstate-apt-XXXXXX)"
  mkdir -p "${LOCAL_REPO}/repo" "${LOCAL_REPO}/lists/partial"
  for _deb in "${PKG_DIR}"/*.deb; do
    ln -s "$_deb" "${LOCAL_REPO}/repo/"
    {
      dpkg-deb -f "$_deb"
      echo "Filename: ./$(basename "$_deb")"
      echo "Size: $(stat -c %s "$_deb")"
      echo "SHA256: $(sha256sum "$_deb" | cut -d' ' -f1)"
      echo ""
    } >> "${LOCAL_REPO}/repo/Packages"
  done
  echo "deb [trusted=yes] file:${LOCAL_REPO}/repo ./" > "${LOCAL_REPO}/sources.list"
  APT_LOCAL=(
    -o Dir::Etc::SourceList="${LOCAL_REPO}/sources.list"
    -o Dir::Etc::SourceParts=/nonexistent
    -o Dir::State::Lists="${LOCAL_REPO}/lists"
    -o APT::Sandbox::User=root
  )

  # --no-remove: only the bundle is visible to apt here, so an installed
  # package whose matching upgrade isn't bundled has no candidate, and apt
  # would remove it (and everything depending on it) to proceed. Stop instead.
  if ! apt-get "${APT_LOCAL[@]}" update -qq ||
     ! apt-get "${APT_LOCAL[@]}" install -y -q --no-install-recommends --no-remove \
         -o Dpkg::Options::="--force-confold" \
         "${REQUIRED_PACKAGES[@]}"; then
    rm -rf "${LOCAL_REPO}"
    echo ""
    warn "apt can't install from ${PKG_DIR} without changes to this server"
    warn "(details above). Nothing was installed or removed. Either:"
    warn "  - a required dependency .deb is missing, or"
    warn "  - installing would REMOVE packages already on this server, because"
    warn "    the bundle upgrades a package they're tied to without including"
    warn "    their matching upgrade."
    warn "Add the .deb files for the packages apt named (same versions as the"
    warn "bundle, from an online machine on the same OS release) to ${PKG_DIR},"
    warn "or rebuild the bundle with LINKSTATE_TARGET_MANIFEST set to this"
    warn "server's package list:  dpkg-query -W -f='\${Package}\\n' > manifest.txt"
    warn "then run this installer again."
    exit 1
  fi
  rm -rf "${LOCAL_REPO}"
  ok "System packages installed from ${PKG_DIR}"
fi

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

# ── Snapshot current version before upgrade ───────────────
if [[ "$UPGRADE" == "true" ]] && [[ -f "${APP_DIR}/version.txt" ]]; then
  SNAP_VER=$(cat "${APP_DIR}/version.txt" | tr -d "[:space:]")
  SNAP_TS=$(date -u +"%Y%m%d-%H%M%S")
  SNAP_DIR="${APP_DIR}/snapshots/${SNAP_VER}-${SNAP_TS}"
  info "Snapshotting current version v${SNAP_VER}..."
  mkdir -p "${SNAP_DIR}"
  rsync -a     --exclude "config/"     --exclude "logs/"     --exclude "results/"     --exclude "packages/"     --exclude "snapshots/"     --exclude ".ssh/"     --exclude "ssl/"     --exclude "venv/"     "${APP_DIR}/" "${SNAP_DIR}/"
  ok "Snapshot saved: snapshots/${SNAP_VER}-${SNAP_TS}"
  # Keep only the 3 most recent snapshots
  SNAP_COUNT=$(ls -1 "${APP_DIR}/snapshots/" 2>/dev/null | wc -l)
  if [[ $SNAP_COUNT -gt 3 ]]; then
    ls -1t "${APP_DIR}/snapshots/" | tail -n +4 | while read old_snap; do
      rm -rf "${APP_DIR}/snapshots/${old_snap}"
      info "Removed old snapshot: ${old_snap}"
    done
  fi
fi

# ── Sync code files ────────────────────────────────────────
# Back up SSH key before sync in case it lives outside .ssh/
if [[ "$UPGRADE" == "true" ]]; then
  KEY_FILE_CONF=$(grep "key_file:" "${APP_DIR}/config/config.yaml" 2>/dev/null |                   awk '{print $2}' | tr -d '"' | sed "s|~|$HOME|g" | head -1)
  KEY_FILE_CONF="${KEY_FILE_CONF:-${APP_DIR}/.ssh/linkstate_key}"
  # Private (0700) and unpredictable, and removed however the script exits —
  # a failed step before the restore below must not leave a key copy in /tmp.
  KEY_BACKUP_DIR="$(mktemp -d /tmp/linkstate-key-backup-XXXXXX)"
  trap 'rm -rf "${KEY_BACKUP_DIR}"' EXIT
  for kf in "${KEY_FILE_CONF}" "${KEY_FILE_CONF}.pub"; do
    [[ -f "$kf" ]] && cp "$kf" "${KEY_BACKUP_DIR}/" && info "Backed up: $kf"
  done
fi

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
  --exclude "vendor/" \
  "${SRC_DIR}/" "${APP_DIR}/"
# Keep the bundled wheels (not the .debs — those are only needed once) so
# rollbacks and web-UI updates can reinstall Python packages offline.
if [[ "$HAS_BUNDLED_WHEELS" == "true" ]]; then
  mkdir -p "${APP_DIR}/vendor/wheels"
  rsync -a --delete "${BUNDLED_WHEELS}/" "${APP_DIR}/vendor/wheels/"
fi
ok "Code files synced"

# Restore SSH keys if they were wiped by rsync
if [[ "$UPGRADE" == "true" ]] && [[ -d "${KEY_BACKUP_DIR:-}" ]]; then
  for kf in "${KEY_BACKUP_DIR}"/*; do
    [[ -f "$kf" ]] || continue
    DEST="${KEY_FILE_CONF%/*}/$(basename "$kf")"
    if [[ ! -f "$DEST" ]]; then
      mkdir -p "$(dirname "$DEST")"
      cp "$kf" "$DEST"
      [[ "$DEST" == *.pub ]] && chmod 644 "$DEST" || chmod 600 "$DEST"
      ok "Restored SSH key: $DEST"
    fi
  done
  rm -rf "${KEY_BACKUP_DIR}"
fi

# ── Create runtime directories ─────────────────────────────
install -d -o "${APP_USER}" -g "${APP_GROUP}" \
  "${APP_DIR}/logs" \
  "${APP_DIR}/results" \
  "${APP_DIR}/packages" \
  "${APP_DIR}/snapshots"
install -d -m 755 /opt/linkstate/ssl
ok "Runtime directories ready"

# ── Agent packages for air-gapped onboarding ───────────────
# The bundled .debs include the agent tools, built for this server's OS
# release. Stage them so agents on the same release onboard offline with
# nothing uploaded (packages/bundled/<os>-<version>/).
if [[ "$HAS_BUNDLED_DEBS" == "true" ]]; then
  if (cd "${APP_DIR}" && python3 -m core.agent_packages "${BUNDLED_DEBS}" "${APP_DIR}/packages"); then
    ok "Agent packages staged for air-gapped onboarding"
  else
    warn "Couldn't stage bundled agent packages — upload them via Config → Packages instead"
  fi
fi

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
# Installs the pinned requirements.lock (falling back to requirements.txt)
# from PyPI (online) or from a local wheelhouse (offline). Offline wheels
# must match this server's OS and Python version; release bundles carry
# a matching set in vendor/wheels.
REQ_FILE="${APP_DIR}/requirements.lock"
[[ -f "$REQ_FILE" ]] || REQ_FILE="${APP_DIR}/requirements.txt"

install_python_deps() {
  if [[ "$ONLINE" != "false" ]]; then
    "${APP_DIR}/venv/bin/pip" install --upgrade pip -q
    "${APP_DIR}/venv/bin/pip" install -r "${REQ_FILE}" -q
    return
  fi

  WHEELS_DIR="${LINKSTATE_WHEELS_DIR:-}"
  if [[ -z "$WHEELS_DIR" && "$HAS_BUNDLED_WHEELS" == "true" ]]; then
    WHEELS_DIR="${BUNDLED_WHEELS}"
  fi

  if [[ -z "$WHEELS_DIR" ]]; then
    echo ""
    echo "  Offline install — Python packages will come from a local directory."
    echo "  It must contain wheels (.whl) for every entry in ${REQ_FILE##*/}"
    echo "  AND all their transitive dependencies. Generate the full set on an"
    echo "  online machine with the same OS and Python $(python3 -V 2>&1 | awk '{print $2}'):"
    echo ""
    echo "      pip download -r ${REQ_FILE##*/} -d <wheels-dir>"
    echo ""
    echo "  (Release bundles built by make_release.sh include these in vendor/wheels.)"
    echo ""
  fi

  while true; do
    if [[ -z "$WHEELS_DIR" ]]; then
      read -r -p "  Path to directory containing the Python wheels: " WHEELS_DIR || true
    fi
    WHEELS_DIR="${WHEELS_DIR/#\~/$HOME}"
    if [[ -n "$WHEELS_DIR" ]] && [[ -d "$WHEELS_DIR" ]] && compgen -G "${WHEELS_DIR}/*.whl" > /dev/null; then
      break
    fi
    warn "No .whl files found in: ${WHEELS_DIR:-<none entered>}"
    [[ -n "${LINKSTATE_WHEELS_DIR:-}" ]] && exit 1
    WHEELS_DIR=""
  done

  info "Installing Python packages from ${WHEELS_DIR}..."
  if ! "${APP_DIR}/venv/bin/pip" install --no-index --find-links "${WHEELS_DIR}" \
       -r "${REQ_FILE}" -q; then
    warn "pip could not resolve every package from ${WHEELS_DIR}."
    warn "Add the missing .whl files and run this installer again."
    exit 1
  fi
}

info "Setting up Python virtual environment..."
if [[ "$UPGRADE" == "true" ]]; then
  if [[ ! -d "${APP_DIR}/venv" ]]; then
    python3 -m venv "${APP_DIR}/venv"
    install_python_deps
    ok "Python environment created"
  elif [[ "$HAS_BUNDLED_WHEELS" == "true" ]]; then
    # The bundle carries its exact dependency set, so syncing is offline
    # and a no-op when nothing changed — new dependencies can't be missed.
    install_python_deps
    ok "Python environment synced to ${REQ_FILE##*/}"
  else
    # No bundled wheels — skip pip; run manually if dependencies changed:
    #   sudo /opt/linkstate/venv/bin/pip install -r /opt/linkstate/requirements.txt
    ok "Python environment unchanged (upgrade mode — skipping pip)"
  fi
else
  # Fresh install: create venv and install deps
  python3 -m venv "${APP_DIR}/venv"
  install_python_deps
  ok "Python environment ready"
fi

# ── Fix ownership ──────────────────────────────────────────
chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}"
# Keep .ssh permissions strict
if [[ -d "${APP_DIR}/.ssh" ]]; then
  chmod 700 "${APP_DIR}/.ssh"
  chmod 600 "${APP_DIR}/.ssh/linkstate_key" 2>/dev/null || true
  chmod 644 "${APP_DIR}/.ssh/linkstate_key.pub" 2>/dev/null || true
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
    -C "linkstate-controller" \
    -N ""
  chmod 600 "${KEY_FILE}"
  chmod 644 "${KEY_FILE}.pub"
  ok "SSH key generated: ${KEY_FILE}"
else
  info "SSH key already exists — keeping existing key"
  info "(run with --show-key to display it)"
fi

# ── iPerf3 ports the tests use (one linkstate-iperf3@ instance each) ─
IPERF_PORTS=$(cd "${APP_DIR}" && "${APP_DIR}/venv/bin/python3" -c '
from core.config_loader import load_config
tp = load_config("config/config.yaml").test_params
print(" ".join(str(p) for p in sorted({tp.throughput.iperf3_port, tp.jitter.iperf3_port})))
' 2>/dev/null) || true
IPERF_PORTS="${IPERF_PORTS:-5201}"
# sudo(-rs) allows no wildcards in arguments — list each instance
IPERF_SUDO=""
for p in ${IPERF_PORTS}; do
  IPERF_SUDO+=", /usr/bin/systemctl restart linkstate-iperf3@${p}.service"
done

# ── Allow linkstate user to restart scheduler without password ─
SUDOERS_FILE="/etc/sudoers.d/linkstate-restart"
cat > "${SUDOERS_FILE}" << SUDOERS
# Allow linkstate service user to restart the scheduler
# (triggered automatically when config is saved from the web UI)
# dpkg is needed for air-gapped agent package installation
${APP_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart linkstate, /usr/bin/systemctl restart linkstate-web, /usr/bin/dpkg, /usr/bin/systemctl restart nginx, /usr/bin/systemctl reload nginx, /usr/bin/systemctl reload-or-restart nginx, /usr/bin/systemctl stop nginx, /usr/bin/systemctl enable nginx, /usr/bin/systemctl start nginx, /usr/bin/tee, /usr/bin/ln, /usr/bin/rm${IPERF_SUDO}
SUDOERS
chmod 440 "${SUDOERS_FILE}"
visudo -c -f "${SUDOERS_FILE}" > /dev/null 2>&1 && \
  ok "Sudoers entry for scheduler restart configured" || \
  warn "Sudoers validation failed — check ${SUDOERS_FILE}"

# ── nginx reverse proxy setup ──────────────────────────────
info "Configuring nginx reverse proxy..."
NGINX_CONF="/etc/nginx/sites-available/linkstate"
cat > "${NGINX_CONF}" << 'NGINXCONF'
server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name _;

    ssl_certificate     /opt/linkstate/ssl/linkstate.crt;
    ssl_certificate_key /opt/linkstate/ssl/linkstate.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 10m;

    client_max_body_size      100M;
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
ln -sf "${NGINX_CONF}" /etc/nginx/sites-enabled/linkstate
rm -f /etc/nginx/sites-enabled/default

# Generate self-signed cert if none exists
if [[ ! -f /opt/linkstate/ssl/linkstate.crt ]]; then
  SERVER_IP=$(hostname -I | awk '{print $1}')
  # Also cover the controller's hostname and FQDN, so the dashboard can be
  # reached by name without a certificate name mismatch.
  SAN="IP:${SERVER_IP}"
  for _n in "$(hostname -s 2>/dev/null)" "$(hostname -f 2>/dev/null)"; do
    if [[ "$_n" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ && "$_n" != "localhost" \
          && ",${SAN}," != *",DNS:${_n},"* ]]; then
      SAN="${SAN},DNS:${_n}"
    fi
  done
  openssl req -x509 -nodes -newkey rsa:4096 \
    -keyout /opt/linkstate/ssl/linkstate.key \
    -out    /opt/linkstate/ssl/linkstate.crt \
    -days 3650 \
    -subj "/CN=${SERVER_IP}/O=LinkState" \
    -addext "subjectAltName=${SAN}" \
    2>/dev/null
  chmod 600 /opt/linkstate/ssl/linkstate.key
  chmod 644 /opt/linkstate/ssl/linkstate.crt
  ok "Self-signed certificate generated for ${SAN}"
else
  info "SSL certificate already exists — keeping existing cert"
fi

if nginx -t 2>/dev/null; then
  systemctl enable nginx
  systemctl restart nginx
  ok "nginx configured and started"
else
  warn "nginx config test failed — check /etc/nginx/sites-available/linkstate"
fi

# ── Systemd service files ──────────────────────────────────
info "Installing systemd services..."
install -m 0644 "${APP_DIR}/systemd/linkstate.service" \
  /etc/systemd/system/linkstate.service
install -m 0644 "${APP_DIR}/systemd/linkstate-web.service" \
  /etc/systemd/system/linkstate-web.service
install -m 0644 "${APP_DIR}/systemd/linkstate-iperf3@.service" \
  /etc/systemd/system/linkstate-iperf3@.service
systemctl daemon-reload
systemctl enable linkstate.service linkstate-web.service
ok "Services installed and enabled"

# Persistent iPerf3 server(s), so this controller can also be a test
# endpoint. A pre-service temporary server would hold the port — stop it.
pkill -u "${APP_USER}" -x iperf3 2>/dev/null || true
for p in ${IPERF_PORTS}; do
  systemctl enable "linkstate-iperf3@${p}.service" >/dev/null 2>&1
  if systemctl restart "linkstate-iperf3@${p}.service"; then
    ok "iPerf3 service listening on port ${p} (linkstate-iperf3@${p})"
  else
    warn "linkstate-iperf3@${p} failed to start — check: journalctl -u linkstate-iperf3@${p}"
  fi
done

if [[ "$UPGRADE" == "true" ]]; then
  info "Restarting services..."
  systemctl restart linkstate.service
  systemctl restart linkstate-web.service
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
  echo "     sudo systemctl start linkstate"
  echo "     sudo systemctl start linkstate-web"
  echo ""
  echo "  3. Check service status:"
  echo "     sudo systemctl status linkstate linkstate-web"
  echo ""
  echo "  4. Open the dashboard:"
  echo "     http://<this-server-ip>:8080    (direct, always available)"
  echo "     https://<this-server-ip>        (HTTPS via nginx)"
  echo ""
  echo "     Note: HTTPS uses a self-signed certificate."
  echo "     Your browser will show a security warning — this is expected."
  echo "     Add an exception or configure a real cert via Config → HTTPS."
  echo ""
  echo "  5. To rollback to a previous version:"
  echo "     sudo ./rollback.sh --list"
  echo "     sudo ./rollback.sh <snapshot-name>"
  echo ""
  echo "  6. Air-gapped agents only:"
  echo "     Upload .deb packages via Config → Packages in the web UI"
  echo "     before onboarding any air-gapped agents."
  echo "     Required packages: iperf3, libiperf0, libsctp1, mtr-tiny,"
  echo "     iputils-ping, traceroute, psmisc"
  echo ""
  echo "  7. To require dashboard login without a RADIUS server (or as a"
  echo "     fallback login if RADIUS is unreachable), add a local account:"
  echo "     sudo ./install.sh --setup-local-user"
  echo "     (local users can also be managed later from Config → Auth)"
  echo ""
fi

if [[ "$MIGRATED_FROM_LEGACY" == "true" ]]; then
  echo "  Migrated from NetTest:"
  echo ""
  echo "  - The app now lives in ${APP_DIR} and runs as '${APP_USER}'"
  echo "    (services: linkstate, linkstate-web, linkstate-iperf3@<port>)."
  echo "  - The SSH key is unchanged, so onboarded agents keep working. They"
  echo "    still log in as 'nettest' and run the nettest-iperf3@ service."
  echo "    Re-onboard an agent to move it to the '${APP_USER}' user and the"
  echo "    linkstate-iperf3@ service (the old 'nettest' user stays on it)."
  echo "  - NetTest snapshots taken before this upgrade can't be rolled back to."
  echo ""
fi

echo "  Controller public key"
echo "  (needed when onboarding agents):"
echo ""
cat "${KEY_FILE}.pub"
echo ""
sep
echo ""
