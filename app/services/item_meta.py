"""Persist display metadata (icon / type / wear) learned from inventory scans.

Steam inventory descriptions carry the icon path, the item type and the wear
category; asset properties carry the exact wear float.  The inventory page
renders that data directly, while holdings and operation records are database
rows that outlive the item itself.  Anything learned during a scan is written
back to matching purchase rows (exact asset match first, item-name fallback
for the icon/type/wear label) and remembered per item name so sold records
keep their artwork in history views.
"""
import json
import os
import tempfile
import threading
from pathlib import Path

from app import database

_PATH = Path(__file__).resolve().parents[2] / "config" / "item_meta_cache.json"
_LOCK = threading.RLock()

_NAME_META_FIELDS = ("icon_url", "item_type", "exterior")
_ASSET_META_FIELDS = ("icon_url", "item_type", "exterior", "paintwear")


def _read() -> dict:
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _clean(value) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _name_meta(item: dict) -> dict:
    out = {}
    for key in _NAME_META_FIELDS:
        value = _clean((item or {}).get(key))
        if value:
            out[key] = value
    return out


def remember_items(items) -> None:
    """Merge name -> {icon_url,item_type,exterior} learned from an inventory scan."""
    updates = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("market_hash_name")) or _clean(item.get("name"))
        meta = _name_meta(item)
        if name and meta:
            updates[name] = meta
    if not updates:
        return
    with _LOCK:
        data = _read()
        for name, meta in updates.items():
            existing = data.get(name)
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update(meta)
            data[name] = merged
        temporary = None
        try:
            _PATH.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=_PATH.parent, prefix=".item-meta-cache.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, _PATH)
        except OSError:
            # A cache write failure must not discard freshly learned metadata.
            pass
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


def get_item_meta(names) -> dict:
    """Look up remembered metadata for the given item names."""
    wanted = {_clean(name) for name in names or []}
    wanted.discard("")
    if not wanted:
        return {}
    with _LOCK:
        data = _read()
    out = {}
    for name in wanted:
        entry = data.get(name)
        meta = _name_meta(entry) if isinstance(entry, dict) else {}
        if meta:
            out[name] = meta
    return out


def _index_inventory(items):
    by_asset, by_name = {}, {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        assetid = _clean(item.get("assetid"))
        if assetid:
            by_asset.setdefault(assetid, item)
        for key in ("market_hash_name", "name"):
            name = _clean(item.get(key))
            if name:
                by_name.setdefault(name, item)
    return by_asset, by_name


def enrich_purchases_from_inventory(items) -> None:
    """Write icon/type/wear onto purchase rows that lack it.

    Exact asset matches also carry the per-item wear float; name-only matches
    never fill ``paintwear`` because different items share one name.
    """
    purchases = database.db_get_purchases()
    if not purchases:
        return
    by_asset, by_name = _index_inventory(items)
    for purchase in purchases:
        db_id = purchase.get("_db_id")
        if not db_id:
            continue
        asset_item = by_asset.get(_clean(purchase.get("assetid")))
        name_item = by_name.get(_clean(purchase.get("name")))
        updates = {}
        for key in _ASSET_META_FIELDS if asset_item is not None else _NAME_META_FIELDS:
            source = asset_item if asset_item is not None else name_item
            value = _clean((source or {}).get(key))
            if value and purchase.get(key) != value:
                updates[key] = value
        if updates:
            database.db_update_purchase_by_id(db_id, updates)


def enrich_transaction_rows(rows) -> None:
    """Fill display metadata on /api/transactions purchase rows (read-only)."""
    purchases = [row for row in rows or []
                 if isinstance(row, dict) and row.get("type") == "purchase"]
    if not purchases:
        return
    from app.state import get_inventory
    by_asset, by_name = _index_inventory(get_inventory() or [])
    stored = get_item_meta(_clean(row.get("name")) for row in purchases)
    for row in purchases:
        asset_item = by_asset.get(_clean(row.get("assetid")))
        if asset_item is not None:
            for key in _ASSET_META_FIELDS:
                value = _clean(asset_item.get(key))
                if value and not row.get(key):
                    row[key] = value
        name = _clean(row.get("name"))
        for key in _NAME_META_FIELDS:
            if row.get(key):
                continue
            value = _clean((by_name.get(name) or {}).get(key)) or (stored.get(name) or {}).get(key, "")
            if value:
                row[key] = value
