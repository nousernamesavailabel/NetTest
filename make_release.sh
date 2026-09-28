#!/usr/bin/env bash
# =============================================================
# NetTest — Release Bundle Builder
#
# Usage:
#   ./make_release.sh              # builds nettest-vX.Y.Z.tar.gz
#   ./make_release.sh --version    # prints current version
#   ./make_release.sh --lock       # re-resolve requirements.txt into
#                                  # requirements.lock (needs internet)
#
# Output: nettest-vX.Y.Z.tar.gz in current directory
#
# The bundle is self-contained: vendor/debs holds every system package
# (plus dependencies) and vendor/wheels every Python wheel pinned in
# requirements.lock, so install.sh needs no internet connection.
#
# Build on an online machine running the SAME OS release as the target
# controllers — the .debs come from this machine's apt sources, and the
# wheels are chosen for this machine's Python version and glibc.
#
# Environment overrides:
#   NETTEST_TARGET_PY      Target Python version  (default: this python3)
#   NETTEST_TARGET_GLIBC   Target glibc version   (default: this glibc)
#   NETTEST_TARGET_MANIFEST  Package list from a target controller
#                          (dpkg-query -W -f='${Package}\n'), used to pick
#                          extra .debs to bundle (default: this machine)
# =============================================================

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION_FILE="${SRC_DIR}/version.txt"
LOCK_FILE="${SRC_DIR}/requirements.lock"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC}  $*"; }
info() { echo -e "${CYAN}  ·${NC}  $*"; }
warn() { echo -e "${YELLOW}  !${NC}  $*"; }
sep()  { echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

# Files and directories that make up a release. Anything not listed here
# (notes, logs, old tarballs, local package dirs) stays out of the bundle.
RELEASE_FILES=(
  main.py
  web_dashboard.py
  tui.py
  onboard.py
  core
  runners
  web
  systemd
  config/config.example.yaml
  config/radius_dictionary
  install.sh
  uninstall.sh
  rollback.sh
  requirements.txt
  requirements.lock
  version.txt
  CHANGELOG.md
  README.md
)

# System packages the controller needs — keep in sync with install.sh.
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

if [[ ! -f "${VERSION_FILE}" ]]; then
  echo "ERROR: version.txt not found in ${SRC_DIR}"
  exit 1
fi

VERSION=$(cat "${VERSION_FILE}" | tr -d '[:space:]')

if [[ "${1:-}" == "--version" ]]; then
  echo "${VERSION}"
  exit 0
fi

# ── Lock mode: resolve requirements.txt to exact pins ──────
if [[ "${1:-}" == "--lock" ]]; then
  LOCK_VENV="$(mktemp -d)"
  trap 'rm -rf "${LOCK_VENV}"' EXIT
  info "Resolving requirements.txt in a clean venv..."
  python3 -m venv "${LOCK_VENV}"
  "${LOCK_VENV}/bin/pip" install -q -r "${SRC_DIR}/requirements.txt"
  {
    echo "# NetTest — pinned Python dependencies (generated — do not edit by hand)"
    echo "#"
    echo "# Exact versions of every package, including transitive dependencies."
    echo "# make_release.sh bundles wheels for exactly these into vendor/wheels, and"
    echo "# install.sh / rollback.sh / the web updater install from this file."
    echo "#"
    echo "# Edit requirements.txt, then regenerate with:  ./make_release.sh --lock"
    "${LOCK_VENV}/bin/pip" freeze
  } > "${LOCK_FILE}"
  ok "requirements.lock updated ($(grep -c '==' "${LOCK_FILE}") packages)"
  exit 0
fi

BUNDLE_NAME="nettest-v${VERSION}.tar.gz"
BUNDLE_PATH="${SRC_DIR}/${BUNDLE_NAME}"
STAGING_DIR="/tmp/nettest-release-${VERSION}"

TARGET_PY="${NETTEST_TARGET_PY:-$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')}"
TARGET_GLIBC="${NETTEST_TARGET_GLIBC:-$(getconf GNU_LIBC_VERSION | awk '{print $2}')}"
OS_DESC="$(. /etc/os-release; echo "${PRETTY_NAME:-unknown}")"

sep
echo -e "  ${CYAN}NetTest Release Builder${NC}"
echo "  Version : ${VERSION}"
echo "  Output  : ${BUNDLE_NAME}"
echo "  Target  : ${OS_DESC} · Python ${TARGET_PY} · glibc ${TARGET_GLIBC} · $(dpkg --print-architecture)"
sep
echo ""

# Check for required files
for f in "${RELEASE_FILES[@]}"; do
  if [[ ! -e "${SRC_DIR}/${f}" ]]; then
    echo "ERROR: Required file missing: ${f}"
    [[ "$f" == "requirements.lock" ]] && echo "       Generate it with: ./make_release.sh --lock"
    exit 1
  fi
done
ok "Required files present"

# Clean staging dir
rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}"
trap 'rm -rf "${STAGING_DIR}"' EXIT

# Copy files into staging
info "Staging release files..."
(cd "${SRC_DIR}" && rsync -aR \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  "${RELEASE_FILES[@]}" "${STAGING_DIR}/")
ok "Files staged"

# Verify version.txt is in staging
echo "${VERSION}" > "${STAGING_DIR}/version.txt"

# ── Python wheels ──────────────────────────────────────────
# Wheels only (no sdists) so the target never needs a compiler. pip does
# not widen a single --platform to older glibc tags, so list every
# manylinux tag the target glibc can run.
info "Downloading Python wheels for ${LOCK_FILE##*/}..."
WHEELS_DIR="${STAGING_DIR}/vendor/wheels"
mkdir -p "${WHEELS_DIR}"
GLIBC_MINOR="${TARGET_GLIBC#*.}"
ARCH="$(uname -m)"
PLATFORM_ARGS=(--platform "manylinux1_${ARCH}" --platform "manylinux2014_${ARCH}")
for ((v = 5; v <= GLIBC_MINOR; v++)); do
  PLATFORM_ARGS+=(--platform "manylinux_2_${v}_${ARCH}")
done
if ! python3 -m pip download -q \
     -r "${LOCK_FILE}" -d "${WHEELS_DIR}" \
     --only-binary=:all: --python-version "${TARGET_PY}" "${PLATFORM_ARGS[@]}"; then
  echo "ERROR: could not download every wheel in requirements.lock"
  exit 1
fi

# Prove the wheelhouse is complete: resolve the lock with no index at all.
if ! python3 -m pip install -q --dry-run --ignore-installed --no-index \
     --find-links "${WHEELS_DIR}" --target "${STAGING_DIR}/.pipcheck" \
     --only-binary=:all: --python-version "${TARGET_PY}" "${PLATFORM_ARGS[@]}" \
     -r "${LOCK_FILE}"; then
  echo "ERROR: vendor/wheels does not satisfy requirements.lock offline"
  exit 1
fi
rm -rf "${STAGING_DIR}/.pipcheck"
ok "$(ls "${WHEELS_DIR}" | wc -l) wheels bundled ($(du -sh "${WHEELS_DIR}" | cut -f1)) — verified offline"

# ── System packages ────────────────────────────────────────
# Every package in the dependency tree, including base-system ones the
# target almost certainly has. install.sh builds a local apt repo from
# these, so apt installs only what the target is actually missing.
info "Downloading system packages and dependencies..."
DEBS_DIR="${STAGING_DIR}/vendor/debs"
mkdir -p "${DEBS_DIR}"
mapfile -t DEB_LIST < <(apt-cache depends --recurse --no-recommends --no-suggests \
  --no-conflicts --no-breaks --no-replaces --no-enhances \
  "${REQUIRED_PACKAGES[@]}" | grep '^[a-z0-9]' | sort -u)

# If the target is at an older patch level, apt has to upgrade some of its
# installed packages to the bundled versions. Any installed package that
# pins one of those to an exact version (libpython3.14 → libpython3.14-stdlib
# (= X)) must be upgraded in lockstep. If its new version isn't in the bundle,
# apt removes it along with everything depending on it. So bundle those too.
# Only packages installed on a typical controller are added, not every
# possible one (for gcc alone that runs to gigabytes): by default this
# machine's installed set, or NETTEST_TARGET_MANIFEST — a file of package
# names from a real target (dpkg-query -W -f='${Package}\n').
info "Adding exact-version companions of installed packages..."
mapfile -t DEB_LIST < <(python3 - "${DEB_LIST[@]}" <<'PYEOF'
import os, re, subprocess, sys

def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout

arch = run("dpkg", "--print-architecture").strip()
manifest = os.environ.get("NETTEST_TARGET_MANIFEST")
if manifest:
    with open(manifest) as f:
        installed = {l.split(":")[0].strip() for l in f if l.strip()}
else:
    installed = {
        p.split(":")[0]
        for p, st in (l.split("\t") for l in run(
            "dpkg-query", "-W", "-f=${Package}\t${db:Status-Status}\n").splitlines())
        if st == "installed"
    }

def deps(pkgs):
    out = run("apt-cache", "depends", "--recurse", "--no-recommends", "--no-suggests",
              "--no-conflicts", "--no-breaks", "--no-replaces", "--no-enhances", *pkgs)
    return {l for l in out.splitlines() if re.match(r"^[a-z0-9]", l)}

# Candidate version of every available package, and the exact-version
# (= X) dependencies it declares.
recs = {}
for blk in run("apt-cache", "dumpavail").split("\n\n"):
    f = dict(re.findall(r"^([A-Za-z-]+): ?(.*)$", blk, re.M))
    if not f.get("Package") or f.get("Architecture") not in (arch, "all"):
        continue
    ties = set(re.findall(r"([a-z0-9][a-z0-9.+-]*)(?::any)? \(= ([^)]+)\)",
                          f.get("Depends", "") + ", " + f.get("Pre-Depends", "")))
    recs[f["Package"]] = (f["Version"], ties)

# -dev/-doc/-dbg are left out to keep the bundle small; if a target has one,
# install.sh stops (apt --no-remove) rather than removing it.
skip = re.compile(r"-(dev|doc|dbg|dbgsym)$")
bundle = set(sys.argv[1:])
while True:
    pinned = {(p, recs[p][0]) for p in bundle if p in recs}
    add = {q for q, (_, ties) in recs.items()
           if q in installed and q not in bundle and not skip.search(q) and ties & pinned}
    if not add:
        break
    bundle |= add | {d for d in deps(sorted(add)) if d in installed}
print("\n".join(sorted(bundle)))
PYEOF
)

if ! (cd "${DEBS_DIR}" && apt-get download -q "${DEB_LIST[@]}" > /dev/null); then
  echo "ERROR: apt-get download failed — run 'sudo apt-get update' and retry"
  exit 1
fi
ok "${#DEB_LIST[@]} .deb packages bundled ($(du -sh "${DEBS_DIR}" | cut -f1))"

# Add build metadata
BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
cat > "${STAGING_DIR}/.release_info" << RINFO
version=${VERSION}
build_date=${BUILD_DATE}
target_os=${OS_DESC}
target_python=${TARGET_PY}
target_glibc=${TARGET_GLIBC}
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
done < <(find "${STAGING_DIR}" -name "*.py" -not -path "*/vendor/*" -print0)
find "${STAGING_DIR}" -name "__pycache__" -type d -prune -exec rm -rf {} +

if [[ $ERRORS -gt 0 ]]; then
  echo "ERROR: ${ERRORS} Python file(s) failed to compile — fix before releasing"
  exit 1
fi
ok "All Python files compile cleanly"

# Build the tarball
info "Building ${BUNDLE_NAME}..."
rm -f "${BUNDLE_PATH}"
tar -czf "${BUNDLE_PATH}" -C /tmp "nettest-release-${VERSION}"
ok "Bundle created: ${BUNDLE_NAME} ($(du -sh "${BUNDLE_PATH}" | cut -f1))"

echo ""
sep
echo -e "  ${GREEN}Release bundle ready!${NC}"
sep
echo ""
echo "  File    : ${BUNDLE_PATH}"
echo "  Version : ${VERSION}"
echo "  Date    : ${BUILD_DATE}"
echo ""
echo "  Installs with no internet connection on ${OS_DESC}."
echo ""
echo "  To install on a controller:"
echo "  1. Upload via Config → Updates in the web UI"
echo "  2. Or manually: sudo ./install.sh --upgrade (after extracting)"
echo ""
