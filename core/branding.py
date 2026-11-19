"""
branding.py
Product name, plus the service, nginx and certificate names this install uses.

LinkState was formerly called NetTest. A controller upgraded through the
web UI from a NetTest release keeps its old systemd units (nettest,
nettest-web), service user, nginx site and certificate names. The web updater
runs without root, so it can't rename them. `sudo ./install.sh --upgrade`
migrates them. Until it does, the names below follow what the host actually
has, so restarts and HTTPS management keep working on an unmigrated install.
"""

import os

PRODUCT       = "LinkState"
NAME          = "linkstate"
LEGACY_NAME   = "nettest"
LEGACY_PRODUCT = "NetTest"

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SYSTEMD_DIR = "/etc/systemd/system"


def _detect_service_prefix() -> str:
    if os.path.exists(os.path.join(_SYSTEMD_DIR, f"{NAME}.service")):
        return NAME
    if os.path.exists(os.path.join(_SYSTEMD_DIR, f"{LEGACY_NAME}.service")):
        return LEGACY_NAME
    return NAME


SERVICE_PREFIX = _detect_service_prefix()

# True on a NetTest install that got LinkState code through the web updater
# but hasn't run `sudo ./install.sh --upgrade` yet.
LEGACY_INSTALL = SERVICE_PREFIX == LEGACY_NAME

SCHEDULER_SERVICE = SERVICE_PREFIX
WEB_SERVICE       = f"{SERVICE_PREFIX}-web"

SSL_DIR    = os.path.join(APP_DIR, "ssl")
SSL_CERT   = os.path.join(SSL_DIR, f"{SERVICE_PREFIX}.crt")
SSL_KEY    = os.path.join(SSL_DIR, f"{SERVICE_PREFIX}.key")
NGINX_SITE = SERVICE_PREFIX

MIGRATE_HINT = ("This controller still uses its NetTest service names. "
                "To finish the rename to LinkState, run "
                "'sudo ./install.sh --upgrade' from the extracted release bundle.")
