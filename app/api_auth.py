"""HTTP Basic auth gate for the local API.

Enabled only when ``AETHERSWAP_AUTH_PASS`` is set, so tests and local
desktop runs stay open by default.  The health-check endpoint
``/api/runtime`` is exempt so the container healthcheck keeps working.
"""
import base64
import os
import secrets

EXEMPT_PATHS = frozenset({"/api/runtime"})


def credentials_from_env(env=None):
    """Return (username, password) when auth is enabled, else None."""
    source = os.environ if env is None else env
    password = (source.get("AETHERSWAP_AUTH_PASS") or "").strip()
    if not password:
        return None
    username = (source.get("AETHERSWAP_AUTH_USER") or "").strip() or "aetherswap"
    return username, password


def requires_auth(path, creds) -> bool:
    """True when the request path must present credentials."""
    if not creds:
        return False
    text = str(path or "")
    return text.startswith("/api") and text not in EXEMPT_PATHS


def basic_header_matches(header, creds) -> bool:
    """Validate an ``Authorization: Basic ...`` header against creds."""
    if not creds:
        return True
    if not header or header[:6].lower() != "basic ":
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except Exception:
        return False
    user, _, password = decoded.partition(":")
    return secrets.compare_digest(user, creds[0]) and secrets.compare_digest(password, creds[1])
