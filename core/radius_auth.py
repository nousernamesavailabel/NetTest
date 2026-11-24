"""
RADIUS authentication helper for the web dashboard.
"""

import logging
import os
from typing import Optional

from core.config_loader import normalize_role, ROLE_ADMIN, ROLE_READONLY

_log = logging.getLogger("auth")


class RadiusAuthError(Exception):
    pass


def _reply_values(reply, attribute: str) -> list:
    try:
        values = reply[attribute]
    except KeyError:
        return []
    out = []
    for v in values:
        if isinstance(v, bytes):
            v = v.decode("utf-8", errors="replace")
        out.append(str(v).strip().lower())
    return out


def role_from_reply(reply, auth_config) -> str:
    """Pick the account type from the Access-Accept's role attribute."""
    values = _reply_values(reply, auth_config.radius_role_attribute or "Class")
    admin_value    = (auth_config.radius_admin_value or "").strip().lower()
    readonly_value = (auth_config.radius_readonly_value or "").strip().lower()
    # Admin wins if a server sends both.
    if admin_value and admin_value in values:
        return ROLE_ADMIN
    if readonly_value and readonly_value in values:
        return ROLE_READONLY
    return normalize_role(auth_config.radius_default_role)


def authenticate_radius(username: str, password: str, auth_config) -> Optional[str]:
    """Return the account's role ("admin" / "readonly") on Access-Accept, else None."""
    if not auth_config.radius_server:
        raise RadiusAuthError("RADIUS server is not configured")
    if not auth_config.radius_secret:
        raise RadiusAuthError("RADIUS shared secret is not configured")

    try:
        from pyrad.client import Client
        from pyrad.dictionary import Dictionary
        from pyrad.packet import AccessAccept
    except ImportError as exc:
        raise RadiusAuthError("pyrad is not installed. Run: pip install -r requirements.txt") from exc

    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dict_path = os.path.join(root_dir, "config", "radius_dictionary")

    client = Client(
        server=auth_config.radius_server,
        authport=auth_config.radius_port,
        secret=auth_config.radius_secret.encode("utf-8"),
        dict=Dictionary(dict_path),
    )
    client.timeout = auth_config.radius_timeout

    req = client.CreateAuthPacket(
        code=1,
        User_Name=username,
        NAS_Identifier="linkstate-webui",
    )
    req["User-Password"] = req.PwCrypt(password)

    try:
        reply = client.SendPacket(req)
    except Exception as exc:
        raise RadiusAuthError(f"RADIUS request failed: {exc}") from exc

    if reply.code != AccessAccept:
        return None
    role = role_from_reply(reply, auth_config)
    _log.info("RADIUS accepted user '%s' as %s", username, role)
    return role
