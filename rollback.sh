#!/usr/bin/env bash
# =============================================================
# NetTest — Rollback to Previous Version
#
# Usage:
#   sudo ./rollback.sh             # list available snapshots
#   sudo ./rollback.sh --list      # list available snapshots
#   sudo ./rollback.sh <snapshot>  # restore named snapshot
# =============================================================

set -euo pipefail

APP_DIR="${NETTEST_APP_DIR:-/opt/nettest}"
SNAPSHOTS_DIR="${APP_DIR}/snapshots"

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
echo -e "  ${CYAN}NetTest Rollback${NC}"
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
systemctl stop nettest nettest-web

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
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt" -q
ok "Dependencies synced"

info "Starting services..."
systemctl start nettest nettest-web
ok "Services started"

echo ""
sep
echo -e "  ${GREEN}Rollback complete!${NC}"
sep
echo ""
echo "  Restored to: v${SNAP_VERSION}"
echo "  Check status: sudo systemctl status nettest nettest-web"
echo ""
