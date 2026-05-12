"""
RADIUS authentication helper for the web dashboard.
"""

import os


class RadiusAuthError(Exception):
    pass


def authenticate_radius(username: str, password: str, auth_config) -> bool:
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
        NAS_Identifier="nettest-webui",
    )
    req["User-Password"] = req.PwCrypt(password)

    try:
        reply = client.SendPacket(req)
    except Exception as exc:
        raise RadiusAuthError(f"RADIUS request failed: {exc}") from exc

    return reply.code == AccessAccept
