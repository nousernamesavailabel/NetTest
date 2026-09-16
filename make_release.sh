#!/usr/bin/env bash
# =============================================================
# NetTest — Release Bundle Builder
#
# Usage:
#   ./make_release.sh              # builds nettest-vX.Y.Z.tar.gz
#   ./make_release.sh --version    # prints current version
#
# Output: nettest-vX.Y.Z.tar.gz in current directory
# =============================================================

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION_FILE="${SRC_DIR}/version.txt"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC}  $*"; }
info() { echo -e "${CYAN}  ·${NC}  $*"; }
warn() { echo -e "${YELLOW}  !${NC}  $*"; }
sep()  { echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

if [[ ! -f "${VERSION_FILE}" ]]; then
  echo "ERROR: version.txt not found in ${SRC_DIR}"
  exit 1
fi

VERSION=$(cat "${VERSION_FILE}" | tr -d '[:space:]')

if [[ "${1:-}" == "--version" ]]; then
  echo "${VERSION}"
  exit 0
fi

BUNDLE_NAME="nettest-v${VERSION}.tar.gz"
BUNDLE_PATH="${SRC_DIR}/${BUNDLE_NAME}"
STAGING_DIR="/tmp/nettest-release-${VERSION}"

sep
echo -e "  ${CYAN}NetTest Release Builder${NC}"
echo "  Version : ${VERSION}"
echo "  Output  : ${BUNDLE_NAME}"
sep
echo ""

# Check for required files
for f in version.txt CHANGELOG.md install.sh requirements.txt main.py web_dashboard.py; do
  if [[ ! -f "${SRC_DIR}/${f}" ]]; then
    echo "ERROR: Required file missing: ${f}"
    exit 1
  fi
done
ok "Required files present"

# Clean staging dir
rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}"

# Copy files into staging
info "Staging release files..."
rsync -a \
  --exclude ".git/" \
  --exclude ".gitignore" \
  --exclude ".agents/" \
  --exclude ".codex/" \
  --exclude "venv/" \
  --exclude ".venv/" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude "*.tar.gz" \
  --exclude "logs/" \
  --exclude "results/" \
  --exclude "packages/" \
  --exclude "snapshots/" \
  --exclude "config/config.yaml" \
  --exclude ".ssh/" \
  --exclude "ssl/" \
  "${SRC_DIR}/" "${STAGING_DIR}/"
ok "Files staged"

# Verify version.txt is in staging
echo "${VERSION}" > "${STAGING_DIR}/version.txt"

# Add build metadata
BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
cat > "${STAGING_DIR}/.release_info" << RINFO
version=${VERSION}
build_date=${BUILD_DATE}
RINFO

ok "Build metadata written"

# Validate Python files compile
info "Validating Python files..."
ERRORS=0
while IFS= read -r -d '' pyfile; do
  if ! python3 -m py_compile "${pyfile}" 2>/dev/null; then
    warn "Compile error: ${pyfile}"
    ERRORS=$((ERRORS + 1))
  fi
done < <(find "${STAGING_DIR}" -name "*.py" -print0)

if [[ $ERRORS -gt 0 ]]; then
  echo "ERROR: ${ERRORS} Python file(s) failed to compile — fix before releasing"
  rm -rf "${STAGING_DIR}"
  exit 1
fi
ok "All Python files compile cleanly"

# Build the tarball
info "Building ${BUNDLE_NAME}..."
rm -f "${BUNDLE_PATH}"
tar -czf "${BUNDLE_PATH}" -C /tmp "nettest-release-${VERSION}"
ok "Bundle created: ${BUNDLE_NAME} ($(du -sh "${BUNDLE_PATH}" | cut -f1))"

# Cleanup staging
rm -rf "${STAGING_DIR}"

echo ""
sep
echo -e "  ${GREEN}Release bundle ready!${NC}"
sep
echo ""
echo "  File    : ${BUNDLE_PATH}"
echo "  Version : ${VERSION}"
echo "  Date    : ${BUILD_DATE}"
echo ""
echo "  To install on a controller:"
echo "  1. Upload via Config → Updates in the web UI"
echo "  2. Or manually: sudo ./install.sh --upgrade (after extracting)"
echo ""
