"""
RADIUS authentication helper for the web dashboard.
"""

import logging
import os
from collections import OrderedDict
from typing import Optional

from core.config_loader import normalize_role, ROLE_ADMIN, ROLE_READONLY

_log = logging.getLogger("auth")


class RadiusAuthError(Exception):
    pass


# Attributes whose values are safe to write to the log. Anything else (vendor-
# specific attributes such as MS-MPPE-Send/Recv-Key, unknown types) is logged
# by type and length only, since it can carry key material.
_LOGGABLE_ATTRIBUTES = ("Class", "Filter-Id", "Reply-Message", "Service-Type")


def _decode_value(v) -> str:
    """Role attribute value as a comparable string. NPS can also return binary
    Class values of its own; those decode to junk that never matches."""
    if isinstance(v, bytes):
        v = v.decode("utf-8", errors="replace")
    # Strip whitespace and NUL padding some servers add to string values.
    return str(v).strip().strip("\x00").strip().lower()


def _fmt_raw(v) -> str:
    if isinstance(v, bytes):
        return f"hex={v.hex()} ({len(v)} bytes)"
    return repr(v)


def _log_reply_attributes(reply) -> None:
    if not _log.isEnabledFor(logging.DEBUG):
        return
    _log.debug("RADIUS Access-Accept carries %d attribute type(s)", len(list(reply.keys())))
    for key in reply.keys():
        if isinstance(key, str) and key in _LOGGABLE_ATTRIBUTES:
            for i, v in enumerate(reply[key]):
                _log.debug("  %s[%d]: %s", key, i, _fmt_raw(v))
            continue
        try:
            raw = OrderedDict.__getitem__(reply, reply._EncodeKey(key))
        except Exception:
            raw = []
        if isinstance(key, tuple):
            label = f"Vendor-Specific vendor={key[0]} type={key[1]}"
        else:
            label = str(key)
        lengths = [len(r) if isinstance(r, (bytes, str)) else None for r in raw]
        _log.debug("  %s: %d value(s), length(s) %s (value not logged)", label, len(raw), lengths)


def _reply_values(reply, attribute: str) -> list:
    """Every instance of `attribute` in the reply, decoded. pyrad keeps repeated
    attributes (NPS often sends several Class values) as a list, so all are
    evaluated."""
    try:
        values = reply[attribute]
    except KeyError:
        _log.debug("RADIUS role attribute %s: not present in Access-Accept", attribute)
        return []
    out = []
    for i, v in enumerate(values):
        decoded = _decode_value(v)
        _log.debug("RADIUS role attribute %s[%d]: %s -> decoded %s",
                   attribute, i, _fmt_raw(v),
                   repr(decoded) if decoded.isprintable() else "<binary, cannot match>")
        out.append(decoded)
    return out


def role_from_reply(reply, auth_config, username: str = "") -> str:
    """Pick the account type from the Access-Accept's role attribute."""
    attribute      = auth_config.radius_role_attribute or "Class"
    admin_value    = (auth_config.radius_admin_value or "").strip().lower()
    readonly_value = (auth_config.radius_readonly_value or "").strip().lower()
    _log.debug("RADIUS role mapping for '%s': attribute=%s admin_value=%r "
               "readonly_value=%r default=%s", username, attribute, admin_value,
               readonly_value, auth_config.radius_default_role)
    _log_reply_attributes(reply)
    values = _reply_values(reply, attribute)
    # Admin wins if a server sends both.
    if admin_value and admin_value in values:
        _log.info("RADIUS role for '%s': admin (%s matched %r)", username, attribute, admin_value)
        return ROLE_ADMIN
    if readonly_value and readonly_value in values:
        _log.info("RADIUS role for '%s': readonly (%s matched %r)", username, attribute, readonly_value)
        return ROLE_READONLY
    role = normalize_role(auth_config.radius_default_role)
    _log.info("RADIUS role for '%s': %s (default — no %s value matched; %d value(s) received: %s)",
              username, role, attribute, len(values),
              [v if v.isprintable() else "<binary>" for v in values])
    return role


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

    # Lives in core/, not config/: every updater syncs core/, but web updaters
    # before 2.2.1 skipped config/ and left a stale dictionary (without Class
    # or Filter-Id) behind, so every RADIUS user got the default role.
    dict_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "radius_dictionary")

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
        _log.info("RADIUS rejected user '%s' (reply code %s)", username, reply.code)
        return None
    role = role_from_reply(reply, auth_config, username)
    _log.info("RADIUS accepted user '%s' as %s", username, role)
    return role
