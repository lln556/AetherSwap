"""Keep the last known CNY quotes for each Steam account across restarts."""
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time

from app.config_loader import get_steam_credentials

_PATH = Path(__file__).resolve().parents[2] / "config" / "steam_market_price_cache.json"
_LOCK = threading.RLock()


def _read():
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _valid_quote(quote):
    if not isinstance(quote, dict):
        return False
    try:
        return (not isinstance(quote.get("price"), bool)
                and math.isfinite(float(quote["price"])) and float(quote["price"]) > 0
                and math.isfinite(float(quote["at"])) and 0 < float(quote["at"]) <= time.time() + 60)
    except (KeyError, TypeError, ValueError):
        return False


def get_cached_market_prices(names, app_id=730, max_age=None):
    steam_id = str(get_steam_credentials().get("steam_id") or "")
    if not steam_id:
        return {}, None
    with _LOCK:
        quotes = _read().get(f"{app_id}:{steam_id}", {})
        if not isinstance(quotes, dict):
            return {}, None
        now = time.time()
        selected = {name: quotes[name] for name in names
                    if name in quotes and _valid_quote(quotes[name])}
        if max_age is not None:
            selected = {name: quote for name, quote in selected.items()
                        if now - float(quote["at"]) <= max_age}
        return ({name: float(quote["price"]) for name, quote in selected.items()},
                min((float(q["at"]) for q in selected.values()), default=None))


def remember_market_prices(prices, steam_id, app_id=730):
    if not steam_id or not prices:
        return
    now = time.time()
    valid = {name: {"price": price, "at": now} for name, price in prices.items()
             if isinstance(name, str) and name and _valid_quote({"price": price, "at": now})}
    if not valid:
        return
    with _LOCK:
        data = _read()
        key = f"{app_id}:{steam_id}"
        quotes = data.get(key)
        if not isinstance(quotes, dict):
            quotes = {}
        quotes.update(valid)
        data[key] = quotes
        temporary = None
        try:
            _PATH.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=_PATH.parent, prefix=".steam_market_price_cache.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, _PATH)
        except OSError:
            # A cache write failure must not discard a successful live quote.
            pass
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
