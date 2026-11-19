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


# ── Install branding assets (Config → Branding) ─────────────
# Uploaded logo and favicon live in config/branding/ — every upgrade, web
# update and rollback path skips config/, so they survive all of them.
# One file per kind, named <kind>.<ext>; the extension records the type.

ASSET_DIR = os.path.join(APP_DIR, "config", "branding")

ASSET_KINDS = {
    "logo":    {"max_bytes": 512 * 1024, "types": ("png", "jpg", "gif", "webp", "svg")},
    "favicon": {"max_bytes": 128 * 1024, "types": ("png", "ico", "gif", "svg")},
}

ASSET_MIMETYPES = {
    "png":  "image/png",
    "jpg":  "image/jpeg",
    "gif":  "image/gif",
    "webp": "image/webp",
    "svg":  "image/svg+xml",
    "ico":  "image/x-icon",
}


def sniff_image(data: bytes):
    """Image type from the file's content (not its name), or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"\x00\x00\x01\x00":
        return "ico"
    try:
        head = data[:2048].decode("utf-8").lstrip("﻿ \t\r\n")
    except UnicodeDecodeError:
        return None
    if head.startswith("<") and "<svg" in head:
        return "svg"
    return None


def find_asset(kind: str):
    """(path, ext, mtime) of the uploaded asset of this kind, or None."""
    if kind not in ASSET_KINDS:
        return None
    for ext in ASSET_KINDS[kind]["types"]:
        path = os.path.join(ASSET_DIR, f"{kind}.{ext}")
        if os.path.isfile(path):
            return path, ext, int(os.path.getmtime(path))
    return None


def asset_url(kind: str):
    """Cache-busted URL for an uploaded asset, or None if there isn't one."""
    found = find_asset(kind)
    return f"/branding/{kind}?v={found[2]}" if found else None


def save_asset(kind: str, data: bytes):
    """Validate and store an upload. Returns an error message, or None."""
    spec = ASSET_KINDS.get(kind)
    if not spec:
        return f"Unknown asset '{kind}'"
    if not data:
        return "The file is empty"
    if len(data) > spec["max_bytes"]:
        return f"The file is larger than {spec['max_bytes'] // 1024} KB"
    ext = sniff_image(data)
    if ext not in spec["types"]:
        allowed = ", ".join(t.upper() for t in spec["types"])
        return f"Unsupported image type — use {allowed}"
    os.makedirs(ASSET_DIR, exist_ok=True)
    tmp = os.path.join(ASSET_DIR, f".{kind}.upload")
    with open(tmp, "wb") as f:
        f.write(data)
    delete_asset(kind)
    os.replace(tmp, os.path.join(ASSET_DIR, f"{kind}.{ext}"))
    return None


def delete_asset(kind: str):
    for ext in ASSET_KINDS.get(kind, {}).get("types", ()):
        try:
            os.remove(os.path.join(ASSET_DIR, f"{kind}.{ext}"))
        except FileNotFoundError:
            pass
