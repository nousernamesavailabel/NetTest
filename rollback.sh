#!/usr/bin/env bash
# =============================================================
# LinkState — Rollback to Previous Version
#
# Usage:
#   sudo ./rollback.sh             # list available snapshots
#   sudo ./rollback.sh --list      # list available snapshots
#   sudo ./rollback.sh <snapshot>  # restore named snapshot
# =============================================================

set -euo pipefail

APP_DIR="${LINKSTATE_APP_DIR:-/opt/linkstate}"
if [[ -z "${LINKSTATE_APP_DIR:-}" && ! -d "${APP_DIR}" && -d /opt/nettest/snapshots ]]; then
  APP_DIR=/opt/nettest   # NetTest install not yet migrated
fi
SNAPSHOTS_DIR="${APP_DIR}/snapshots"

# Service names on this host: LinkState's, or NetTest's on an install
# upgraded through the web UI that hasn't been migrated yet
SVC=linkstate
if [[ ! -f /etc/systemd/system/linkstate.service && -f /etc/systemd/system/nettest.service ]]; then
  SVC=nettest
fi

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC}  $*"; }
info() { echo -e "${CYAN}  ·${NC}  $*"; }
warn() { echo -e "${YELLOW}  !${NC}  $*"; }
err()  { echo -e "${RED}  ✗${NC}  $*"; }
sep()  { echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./rollback.sh"
  exit 1
fi

list_snapshots() {
  if [[ ! -d "${SNAPSHOTS_DIR}" ]] || [[ -z "$(ls -A "${SNAPSHOTS_DIR}" 2>/dev/null)" ]]; then
    echo "  No snapshots available."
    echo "  Snapshots are created automatically during upgrades."
    return
  fi
  echo "  Available snapshots:"
  echo ""
  local i=1
  for snap in $(ls -t "${SNAPSHOTS_DIR}"); do
    local version="unknown"
    if [[ -f "${SNAPSHOTS_DIR}/${snap}/version.txt" ]]; then
      version=$(cat "${SNAPSHOTS_DIR}/${snap}/version.txt" | tr -d '[:space:]')
    fi
    echo "  ${i}. ${snap}  (v${version})"
    i=$((i + 1))
  done
  echo ""
}

sep
echo -e "  ${CYAN}LinkState Rollback${NC}"
sep
echo ""

if [[ "${1:-}" == "--list" ]] || [[ -z "${1:-}" ]]; then
  list_snapshots
  if [[ -n "$(ls -A "${SNAPSHOTS_DIR}" 2>/dev/null)" ]]; then
    echo "  Usage: sudo ./rollback.sh <snapshot-name>"
    echo "  Example: sudo ./rollback.sh $(ls -t "${SNAPSHOTS_DIR}" | head -1)"
  fi
  exit 0
fi

SNAPSHOT="${1}"
SNAP_PATH="${SNAPSHOTS_DIR}/${SNAPSHOT}"

if [[ ! -d "${SNAP_PATH}" ]]; then
  err "Snapshot not found: ${SNAPSHOT}"
  echo ""
  list_snapshots
  exit 1
fi

# A NetTest snapshot from before the rename expects the old service names,
# user and paths, so it can't run on a migrated LinkState install
if [[ "$SVC" == "linkstate" && -f "${SNAP_PATH}/systemd/nettest.service" \
      && ! -f "${SNAP_PATH}/systemd/linkstate.service" ]]; then
  err "${SNAPSHOT} is a NetTest snapshot from before the rename to LinkState."
  err "It can't be restored onto this install. Reinstall that release instead."
  exit 1
fi

SNAP_VERSION="unknown"
if [[ -f "${SNAP_PATH}/version.txt" ]]; then
  SNAP_VERSION=$(cat "${SNAP_PATH}/version.txt" | tr -d '[:space:]')
fi

CURRENT_VERSION="unknown"
if [[ -f "${APP_DIR}/version.txt" ]]; then
  CURRENT_VERSION=$(cat "${APP_DIR}/version.txt" | tr -d '[:space:]')
fi

echo "  Current version : ${CURRENT_VERSION}"
echo "  Restore to      : ${SNAP_VERSION} (${SNAPSHOT})"
echo ""
read -p "  Proceed with rollback? [y/N] " confirm
if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
  echo "  Rollback cancelled."
  exit 0
fi

echo ""
info "Stopping services..."
systemctl stop "${SVC}" "${SVC}-web"

info "Restoring code files from snapshot..."
rsync -a \
  --exclude "config/" \
  --exclude "logs/" \
  --exclude "results/" \
  --exclude "packages/" \
  --exclude "snapshots/" \
  --exclude ".ssh/" \
  --exclude "ssl/" \
  --exclude "venv/" \
  "${SNAP_PATH}/" "${APP_DIR}/"
ok "Code files restored"

info "Syncing Python dependencies..."
# Prefer the pinned lock and the bundled wheels, which need no internet.
REQ_FILE="${APP_DIR}/requirements.lock"
[[ -f "$REQ_FILE" ]] || REQ_FILE="${APP_DIR}/requirements.txt"
PIP_SRC=()
if compgen -G "${APP_DIR}/vendor/wheels/*.whl" > /dev/null; then
  PIP_SRC=(--no-index --find-links "${APP_DIR}/vendor/wheels")
fi
"${APP_DIR}/venv/bin/pip" install "${PIP_SRC[@]}" -r "${REQ_FILE}" -q
ok "Dependencies synced"

info "Starting services..."
systemctl start "${SVC}" "${SVC}-web"
ok "Services started"

echo ""
sep
echo -e "  ${GREEN}Rollback complete!${NC}"
sep
echo ""
echo "  Restored to: v${SNAP_VERSION}"
echo "  Check status: sudo systemctl status ${SVC} ${SVC}-web"
echo ""
