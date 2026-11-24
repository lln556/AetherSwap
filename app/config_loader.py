import copy
import threading
import time as _time
from app.config_schema import DEFAULTS, _validate_ranges, merge, validate_and_fill
from config import (
    get_app_config_path,
    get_buff,
    get_steam,
    load_app_config,
    save_app_config,
    update_buff_credentials,
    update_steam_credentials,
)
REDACTED_SECRET = "••••••••"
_SECRET_FIELDS = (
    ("steam_guard", "shared_secret"),
    ("steam_confirm", "identity_secret"),
    ("steam_confirm", "device_id"),
    ("notify", "pushplus_token"),
    ("notify", "email_pass"),
    ("proxy_pool", "webshare_api_key"),
)


def _proxy_identity(entry: dict) -> tuple:
    host = entry.get("host")
    port = entry.get("port")
    try:
        port = int(port)
    except (TypeError, ValueError):
        pass
    return (host, port, entry.get("username") or "")


def redact_config_secrets(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        return {}
    redacted = copy.deepcopy(cfg)
    if not redacted:
        return redacted
    for section, field in _SECRET_FIELDS:
        block = redacted.get(section)
        if not isinstance(block, dict):
            continue
        value = block.get(field)
        if isinstance(value, str) and value != "":
            block[field] = REDACTED_SECRET
    pool = redacted.get("proxy_pool")
    proxies = pool.get("proxies") if isinstance(pool, dict) else None
    if isinstance(proxies, list):
        for entry in proxies:
            if not isinstance(entry, dict):
                continue
            password = entry.get("password")
            if isinstance(password, str) and password != "":
                entry["password"] = REDACTED_SECRET
    return redacted


def restore_redacted_secrets(patch: dict, stored: dict) -> dict:
    if not isinstance(patch, dict):
        return {}
    restored = copy.deepcopy(patch)
    source = stored if isinstance(stored, dict) else {}
    for section, field in _SECRET_FIELDS:
        block = restored.get(section)
        if not isinstance(block, dict) or block.get(field) != REDACTED_SECRET:
            continue
        stored_block = source.get(section)
        if isinstance(stored_block, dict) and field in stored_block:
            block[field] = copy.deepcopy(stored_block.get(field))
    pool = restored.get("proxy_pool")
    proxies = pool.get("proxies") if isinstance(pool, dict) else None
    if isinstance(proxies, list):
        stored_pool = source.get("proxy_pool") if isinstance(source.get("proxy_pool"), dict) else {}
        stored_proxies = stored_pool.get("proxies") if isinstance(stored_pool.get("proxies"), list) else []
        by_identity = {}
        for entry in stored_proxies:
            if isinstance(entry, dict):
                by_identity.setdefault(_proxy_identity(entry), entry.get("password", ""))
        for entry in proxies:
            if isinstance(entry, dict) and entry.get("password") == REDACTED_SECRET:
                identity = _proxy_identity(entry)
                if identity in by_identity:
                    entry["password"] = by_identity[identity]
    return restored
_config_cache: dict = {}
_config_cache_ts: float = 0.0
_config_cache_revision = None
_CONFIG_CACHE_TTL = 5.0  
_CONFIG_SNAPSHOT_RETRIES = 3
_config_cache_lock = threading.Lock()
_config_update_lock = threading.RLock()
def _config_file_revision():
    try:
        stat = get_app_config_path().stat()
        return stat.st_mtime_ns, stat.st_size, stat.st_ino
    except OSError:
        return None
def _load_stable_app_config():
    raw = {}
    revision = None
    for _ in range(_CONFIG_SNAPSHOT_RETRIES):
        before = _config_file_revision()
        raw = load_app_config()
        revision = _config_file_revision()
        if before == revision:
            return raw, revision, True
    return raw, revision, False
def _invalidate_config_cache() -> None:
    global _config_cache, _config_cache_ts, _config_cache_revision
    with _config_cache_lock:
        _config_cache = {}
        _config_cache_ts = 0.0
        _config_cache_revision = None
def get_steam_credentials() -> dict:
    return get_steam()
def get_buff_credentials() -> dict:
    return get_buff()
def update_steam_creds(cookies: str, session_id: str, steam_id: str = None) -> None:
    update_steam_credentials(cookies, session_id, steam_id)
def update_buff_creds(
    cookies: str,
    user_agent: str = None,
    source: str = None,
) -> None:
    update_buff_credentials(cookies, user_agent=user_agent, source=source)
def get_buff_credentials_generation() -> int:
    """Return the credential revision used by long-lived BUFF clients."""
    try:
        return int((get_buff_credentials() or {}).get("generation", 0))
    except (TypeError, ValueError):
        return 0
def load_app_config_validated() -> dict:
    global _config_cache, _config_cache_ts, _config_cache_revision
    now = _time.monotonic()
    with _config_cache_lock:
        revision = _config_file_revision()
        if (
            _config_cache
            and revision == _config_cache_revision
            and (now - _config_cache_ts) < _CONFIG_CACHE_TTL
        ):
            return copy.deepcopy(_config_cache)
        raw, revision, stable = _load_stable_app_config()
        result = _validate_ranges(validate_and_fill(merge(DEFAULTS, raw)))
        if stable:
            _config_cache = result
            _config_cache_ts = now
            _config_cache_revision = revision
        else:
            # Never associate content from one file generation with the
            # revision of another. The next call will retry the disk read.
            _config_cache = {}
            _config_cache_ts = 0.0
            _config_cache_revision = None
        return copy.deepcopy(result)
def save_app_config_validated(data: dict) -> None:
    with _config_update_lock:
        filled = _validate_ranges(validate_and_fill(merge(DEFAULTS, data)))
        save_app_config(filled)
        _invalidate_config_cache()


def update_app_config_validated(patch: dict) -> dict:
    """Atomically merge a partial update into the latest app configuration."""
    with _config_update_lock:
        current = load_app_config_validated()
        incoming = restore_redacted_secrets(patch if isinstance(patch, dict) else {}, current)
        updated = merge(current, incoming)
        save_app_config_validated(updated)
        return load_app_config_validated()
