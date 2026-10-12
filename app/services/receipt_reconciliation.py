"""Reconcile completed BUFF orders using unique CS2 item fingerprints."""

from collections import Counter, defaultdict
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import time

from app.config_loader import get_steam_credentials, load_app_config_validated
from app.database import db_apply_receipt_reconciliation
from app.inventory_cs2 import scan_cs2_inventory
from app.services.buff_auth import get_buff_auth_lock
from app.services.buff_checkout_guard import buff_activity_guard
from app.state import get_purchases, log

_ARCHIVE_PATH = Path(__file__).resolve().parents[2] / "config" / "buff_receipt_archive.json"


def _fingerprint(name, classid, seed, wear):
    try:
        if (not name or not classid or isinstance(seed, bool) or isinstance(wear, bool)
                or not str(seed).isdigit() or not 0 <= int(seed) <= 1000
                or not math.isfinite(float(wear)) or not 0 <= float(wear) <= 1):
            return None
        # Both services serialize the same IEEE-754 float with different
        # decimal precision. Compare its actual 32-bit representation.
        return (str(name), str(classid), int(seed), struct.pack("!f", float(wear)))
    except (TypeError, ValueError, OverflowError):
        return None


def build_reconciliation_plan(purchases, orders, inventory, steam_id):
    by_order = defaultdict(list)
    for order in orders:
        if isinstance(order, dict):
            by_order[str(order.get("id") or "")].append(order)
    local_ids = Counter(str(p.get("buff_order_id") or "") for p in purchases)
    used = {str(p["assetid"]) for p in purchases if p.get("assetid")}
    asset_ids = Counter(str(x.get("assetid") or "") for x in inventory)
    by_fingerprint = defaultdict(list)
    for item in inventory:
        aid = str(item.get("assetid") or "")
        if (not aid or aid in used or asset_ids[aid] != 1
                or str(item.get("appid")) != "730"):
            continue
        fp = _fingerprint(item.get("market_hash_name"), item.get("classid"),
                          item.get("paintseed"), item.get("paintwear"))
        if fp:
            by_fingerprint[fp].append(aid)
    received, closed = [], []
    for purchase in purchases:
        if (not purchase.get("pending_receipt") or purchase.get("assetid")
                or purchase.get("listing") or purchase.get("sale_price") or purchase.get("sold_at")
                or not purchase.get("_db_id")):
            continue
        oid = str(purchase.get("buff_order_id") or "")
        matches = by_order.get(oid, [])
        if not oid or local_ids[oid] != 1 or len(matches) != 1:
            continue
        order = matches[0]
        try:
            price = Decimal(str(order.get("price")))
            if (isinstance(order.get("goods_id"), bool) or not price.is_finite()
                    or price <= 0 or price != Decimal(str(purchase.get("price")))
                    or int(order.get("goods_id")) != int(purchase.get("goods_id"))
                    or str(order.get("buyer_steamid")) != str(steam_id)
                    or not steam_id or order.get("game") != "csgo"):
                continue
        except (TypeError, ValueError, ArithmeticError):
            continue
        if order.get("state") in {"FAIL", "CANCELLED", "CANCELED", "CLOSED", "REFUNDED"}:
            closed.append({"purchase": purchase, "state": order.get("state"),
                           "state_text": order.get("state_text"), "error": order.get("error_text")})
            continue
        if order.get("state") != "SUCCESS" or order.get("error_text"):
            continue
        try:
            paid_at = float(order.get("buyer_pay_time") or 0)
            if (isinstance(order.get("buyer_pay_time"), bool)
                    or not math.isfinite(paid_at) or paid_at <= 0):
                continue
        except (TypeError, ValueError):
            continue
        name = order.get("_market_hash_name")
        if not name or name != purchase.get("name"):
            continue
        asset = order.get("asset_info") or {}
        if not isinstance(asset, dict):
            continue
        info = asset.get("info") or {}
        if not isinstance(info, dict):
            continue
        fp = _fingerprint(name, asset.get("classid"), info.get("paintseed"), asset.get("paintwear"))
        candidates = by_fingerprint.get(fp, []) if fp else []
        if len(candidates) == 1:
            received.append({"purchase": purchase, "assetid": candidates[0]})
    claims = Counter(x["assetid"] for x in received)
    return [x for x in received if claims[x["assetid"]] == 1], closed


def _archive_closed(rows):
    if not rows:
        return
    data = json.loads(_ARCHIVE_PATH.read_text()) if _ARCHIVE_PATH.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("退款归档格式异常，保留原购买记录")
    for row in rows:
        data[str(row["purchase"]["buff_order_id"])] = dict(row, verified_at=time.time())
    _ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=_ARCHIVE_PATH.parent, prefix=".receipt-archive-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, _ARCHIVE_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def reconcile_receipts_once(buff_client, scan_inventory=scan_cs2_inventory):
    from app.services.workers import _buff_background_request_is_safe
    with get_buff_auth_lock(), buff_activity_guard():
        if not _buff_background_request_is_safe():
            return {"ok": False, "error": "购买任务或订单门禁正占用 BUFF，请等待购买轮次结束"}
        purchases = get_purchases()
        pending = [p for p in purchases if p.get("pending_receipt") and not p.get("assetid")]
        if not pending:
            return {"ok": True, "received": 0, "closed": 0, "pending": 0, "inventory": None}
        cfg = load_app_config_validated()
        orders = buff_client.get_purchase_history_orders(
            (cfg.get("buff") or {}).get("game", "csgo"),
            [p.get("buff_order_id") for p in pending if p.get("buff_order_id")],
        )
        inventory, inventory_error = None, ""
        if any(x.get("state") == "SUCCESS" for x in orders):
            ok, items, inventory_error = scan_inventory()
            if ok:
                inventory = items
        received, closed = build_reconciliation_plan(
            purchases, orders, inventory or [], (get_steam_credentials() or {}).get("steam_id"),
        )
        _archive_closed(closed)  # Keep a durable audit before removing failed holdings.
        n_closed = sum(db_apply_receipt_reconciliation(x["purchase"], closed=True) for x in closed)
        n_received = sum(db_apply_receipt_reconciliation(x["purchase"], assetid=x["assetid"]) for x in received)
        remaining = sum(bool(p.get("pending_receipt")) for p in get_purchases())
        if n_received or n_closed:
            log(f"[收货对账] 已补记到账 {n_received} 件，已归档失败/退款 {n_closed} 笔，剩余待收货 {remaining} 件",
                "info", category="receive")
        return {"ok": True, "received": n_received, "closed": n_closed,
                "pending": remaining, "inventory": inventory, "inventory_error": inventory_error}
