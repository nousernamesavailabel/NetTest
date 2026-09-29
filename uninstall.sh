#!/usr/bin/env bash
# =============================================================
# NetTest Controller — Uninstall Script
#
# Usage:
#   sudo ./uninstall.sh              # Stop/disable services, remove
#                                     # nginx/sudoers/systemd integration.
#                                     # Leaves /opt/nettest (code, config,
#                                     # logs, results, keys) in place.
#   sudo ./uninstall.sh --purge      # Also delete the app directory
#                                     # entirely (config, keys, data — gone).
#   sudo ./uninstall.sh --remove-user  # Also delete the nettest system
#                                       # user and group.
#   sudo ./uninstall.sh --yes        # Don't prompt for confirmation.
#
# Environment overrides:
#   NETTEST_APP_DIR       Install path  (default: /opt/nettest)
#   NETTEST_USER          Service user  (default: nettest)
# =============================================================

set -euo pipefail

APP_DIR="${NETTEST_APP_DIR:-/opt/nettest}"
APP_USER="${NETTEST_USER:-nettest}"
APP_GROUP="${APP_USER}"

# ── Colour helpers ─────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC}  $*"; }
info() { echo -e "${CYAN}  ·${NC}  $*"; }
warn() { echo -e "${YELLOW}  !${NC}  $*"; }
err()  { echo -e "${RED}  ✗${NC}  $*"; }
sep()  { echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

# ── Argument parsing ───────────────────────────────────────
PURGE=false
REMOVE_USER=false
ASSUME_YES=false
for arg in "$@"; do
  case "$arg" in
    --purge)       PURGE=true       ;;
    --remove-user) REMOVE_USER=true ;;
    --yes|-y)      ASSUME_YES=true  ;;
    *) err "Unknown option: $arg"; exit 1 ;;
  esac
done

# ── Must run as root ───────────────────────────────────────
if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./uninstall.sh"
  exit 1
fi

CURRENT_VERSION="unknown"
[[ -f "${APP_DIR}/version.txt" ]] && CURRENT_VERSION=$(cat "${APP_DIR}/version.txt" | tr -d "[:space:]")

sep
echo -e "  ${CYAN}NetTest Controller — Uninstall${NC}"
echo "  Target: ${APP_DIR}  |  User: ${APP_USER}  |  Version: ${CURRENT_VERSION}"
sep
echo ""

if [[ "$PURGE" == "true" ]]; then
  warn "This will DELETE ${APP_DIR} entirely — config, SSH keys, logs, and"
  warn "results will be permanently lost."
else
  info "Config, logs, results, and keys under ${APP_DIR} will be kept."
  info "Re-run with --purge to delete them as well."
fi
if [[ "$REMOVE_USER" == "true" ]]; then
  warn "This will also delete the '${APP_USER}' system user and group."
fi
echo ""

if [[ "$ASSUME_YES" != "true" ]]; then
  read -r -p "  Proceed with uninstall? [y/N] " confirm || true
  if [[ "${confirm,,}" != "y" && "${confirm,,}" != "yes" ]]; then
    echo "  Uninstall cancelled."
    exit 0
  fi
  echo ""
fi

# ── Stop and disable services ──────────────────────────────
info "Stopping services..."
systemctl stop nettest.service nettest-web.service 2>/dev/null || true
systemctl disable nettest.service nettest-web.service 2>/dev/null || true
systemctl stop 'nettest-iperf3@*.service' 2>/dev/null || true
rm -f /etc/systemd/system/multi-user.target.wants/nettest-iperf3@*.service
ok "Services stopped and disabled"

info "Removing systemd unit files..."
rm -f /etc/systemd/system/nettest.service /etc/systemd/system/nettest-web.service \
  /etc/systemd/system/nettest-iperf3@.service
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true
ok "Systemd units removed"

# ── nginx reverse proxy ────────────────────────────────────
info "Removing nginx reverse proxy config..."
rm -f /etc/nginx/sites-enabled/nettest
rm -f /etc/nginx/sites-available/nettest
# Restore the stock default site if it's still present but not enabled,
# so the box doesn't lose nginx's default vhost as a side effect.
if [[ -f /etc/nginx/sites-available/default ]] && [[ ! -e /etc/nginx/sites-enabled/default ]]; then
  ln -sf /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
  info "Restored nginx default site"
fi
if nginx -t 2>/dev/null; then
  systemctl reload nginx 2>/dev/null || systemctl restart nginx 2>/dev/null || true
  ok "nginx config updated"
else
  warn "nginx config test failed after removal — check nginx manually"
fi

# ── Sudoers entry ───────────────────────────────────────────
if [[ -f /etc/sudoers.d/nettest-restart ]]; then
  rm -f /etc/sudoers.d/nettest-restart
  ok "Removed sudoers entry"
fi

# ── App directory ───────────────────────────────────────────
if [[ "$PURGE" == "true" ]]; then
  if [[ -d "${APP_DIR}" ]]; then
    rm -rf "${APP_DIR}"
    ok "Removed ${APP_DIR}"
  fi
else
  info "Leaving ${APP_DIR} in place"
fi

# ── Service user/group ──────────────────────────────────────
if [[ "$REMOVE_USER" == "true" ]]; then
  if id -u "${APP_USER}" >/dev/null 2>&1; then
    userdel "${APP_USER}" 2>/dev/null || warn "Could not remove user '${APP_USER}' (still owns files?)"
    ok "Removed user '${APP_USER}'"
  fi
  if getent group "${APP_GROUP}" >/dev/null 2>&1; then
    groupdel "${APP_GROUP}" 2>/dev/null || warn "Could not remove group '${APP_GROUP}'"
    ok "Removed group '${APP_GROUP}'"
  fi
else
  info "Leaving '${APP_USER}' user/group in place"
fi

# ── Done ─────────────────────────────────────────────────────
echo ""
sep
echo -e "  ${GREEN}Uninstall complete!${NC}"
sep
echo ""

if [[ "$PURGE" != "true" ]]; then
  echo "  ${APP_DIR} was left in place (config, logs, results, SSH keys)."
  echo "  Run 'sudo ./uninstall.sh --purge' to delete it too."
  echo ""
fi
if [[ "$REMOVE_USER" != "true" ]] && id -u "${APP_USER}" >/dev/null 2>&1; then
  echo "  System user '${APP_USER}' was left in place."
  echo "  Run 'sudo ./uninstall.sh --remove-user' to delete it too."
  echo ""
fi

echo "  Note: system packages installed by install.sh (nginx, iperf3,"
echo "  mtr-tiny, etc.) were left in place, since other software on this"
echo "  host may depend on them. Remove manually with apt if desired."
echo ""
