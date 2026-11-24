"""
Local (config-file) authentication helper for the web dashboard.
"""

from typing import Optional

from werkzeug.security import check_password_hash


class LocalAuthError(Exception):
    pass


def authenticate_local(username: str, password: str, auth_config) -> Optional[str]:
    """Return the account's role ("admin" / "readonly") on success, else None."""
    if not auth_config.local_users:
        raise LocalAuthError("No local users are configured")

    user = next((u for u in auth_config.local_users if u.username == username), None)
    if not user:
        return None

    return user.role if check_password_hash(user.password_hash, password) else None
