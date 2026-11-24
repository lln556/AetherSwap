"""HTTP Basic auth gate for the local API.

Enabled only when ``AETHERSWAP_AUTH_PASS`` is set, so tests and local
desktop runs stay open by default.  The health-check endpoint
``/api/runtime`` is exempt so the container healthcheck keeps working.
"""
import base64
import os
import secrets
from ipaddress import ip_address

from app.runtime_env import parse_bool

EXEMPT_PATHS = frozenset({"/api/runtime"})
_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1",
    "::1",
    "localhost",
    "::ffff:127.0.0.1",
    "testclient",
})


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


def is_loopback_client(host: str | None) -> bool:
    if host is None:
        return False
    text = str(host).strip().lower()
    if text in _LOOPBACK_HOSTS:
        return True
    candidate = text
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1:candidate.index("]")]
    candidate = candidate.split("%", 1)[0]
    try:
        return ip_address(candidate).is_loopback
    except ValueError:
        return False


def allow_unauthenticated_remote(env=None) -> bool:
    source = os.environ if env is None else env
    return bool(parse_bool(source.get("AETHERSWAP_ALLOW_UNAUTHENTICATED"), False))


def remote_access_blocked(path: str, client_host: str | None, creds, env=None) -> bool:
    text = str(path or "")
    if not text.startswith("/api") or text in EXEMPT_PATHS:
        return False
    if creds is not None or allow_unauthenticated_remote(env):
        return False
    host = "" if client_host is None else str(client_host).strip()
    if not host:
        return False
    return not is_loopback_client(host)


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
