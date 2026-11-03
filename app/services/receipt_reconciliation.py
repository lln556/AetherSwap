"""Reconcile BUFF orders using unique fingerprints or Steam asset transfers."""

from collections import Counter, defaultdict
from decimal import Decimal
import json
import hashlib
import threading
import math
import os
from pathlib import Path
import struct
import tempfile
import time
import httpx

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


def _mapped_receipts(evidence, inventory, used, asset_ids):
    rows = (evidence or {}).get("mappings") or []
    rows = [r for r in rows if isinstance(r, dict)]
    old_counts = Counter(str(r.get("old_assetid")) for r in rows)
    new_counts = Counter(str(r.get("assetid")) for r in rows)
    available = {str(i.get("assetid")): i for i in inventory
                 if str(i.get("assetid")) not in used and asset_ids[str(i.get("assetid"))] == 1}
    return {str(r.get("old_assetid")): (r, available[str(r.get("assetid"))]) for r in rows
            if old_counts[str(r.get("old_assetid"))] == 1
            and new_counts[str(r.get("assetid"))] == 1 and str(r.get("assetid")) in available}


def _verified_transfer(mapping, asset, name, steam_id, paid_at):
    proof, item = mapping
    try:
        at = float(proof.get("time_init"))
        return (str(proof.get("steam_id")) == str(steam_id)
                and str(proof.get("trade_id") or "").isdigit()
                and int(proof["trade_id"]) > 0
                and str(proof.get("assetid") or "").isdigit() and int(proof["assetid"]) > 0
                and str(asset.get("appid")) == str(proof.get("appid")) == str(item.get("appid")) == "730"
                and str(asset.get("contextid")) == "2"
                and str(proof.get("contextid")) == str(item.get("contextid")) in {"2", "16"}
                and str(proof.get("classid") or "") == str(asset.get("classid")) == str(item.get("classid"))
                and item.get("market_hash_name") == name
                and not isinstance(proof.get("amount"), bool) and str(proof.get("amount")) == "1"
                and not isinstance(proof.get("time_init"), bool) and math.isfinite(at) and at >= paid_at)
    except (ValueError, TypeError, OverflowError):
        return False


def build_reconciliation_plan(purchases, orders, inventory, steam_id, *, diagnostics=None, trade_evidence=None):
    unmatched = Counter()
    by_order = defaultdict(list)
    for order in orders:
        if isinstance(order, dict):
            by_order[str(order.get("id") or "")].append(order)
    local_ids = Counter(str(p.get("buff_order_id") or "") for p in purchases)
    used = {str(p["assetid"]) for p in purchases if p.get("assetid")}
    asset_ids = Counter(str(x.get("assetid") or "") for x in inventory)
    transfers = _mapped_receipts(trade_evidence, inventory, used, asset_ids)
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
                or not purchase.get("_db_id")
                or purchase.get("account_id") and str(purchase["account_id"]) != str(steam_id)):
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
        elif not fp:
            old_id = str(asset.get("assetid") or "")
            mapping = transfers.get(old_id)
            if mapping and _verified_transfer(mapping, asset, name, steam_id, paid_at):
                proof, item = mapping
                received.append({"purchase": purchase, "assetid": str(item["assetid"]),
                                 "receipt_evidence": dict(proof)})
            elif trade_evidence is None:
                unmatched["missing_item_fingerprint"] += 1
            elif old_id in (trade_evidence.get("pending_protection") or []):
                unmatched["steam_trade_protected"] += 1
            elif trade_evidence.get("error"):
                unmatched["steam_trade_history_unavailable"] += 1
            else:
                unmatched["steam_asset_mapping_unavailable"] += 1
        elif not candidates:
            unmatched["no_matching_inventory_asset"] += 1
        else:
            unmatched["ambiguous_inventory_assets"] += 1
    claims = Counter(x["assetid"] for x in received)
    for row in received:
        if claims[row["assetid"]] > 1:
            unmatched["ambiguous_inventory_assets"] += 1
    if diagnostics is not None:
        diagnostics.update(unmatched)
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


# The worker runs frequently. Cache only public evidence, never tokens or keys;
# changing the Steam session, account or target orders invalidates the cache.
_evidence_cache = None
_evidence_lock = threading.Lock()


def _load_case_evidence(credentials, orders):
    from app.services.steam_receipt_evidence import read_receipt_evidence
    global _evidence_cache
    old_ids = sorted({str((o.get("asset_info") or {}).get("assetid") or "") for o in orders
                      if o.get("state") == "SUCCESS" and isinstance(o.get("asset_info"), dict)})
    old_ids = [x for x in old_ids if x.isdigit() and int(x) > 0]
    key = (str(credentials.get("steam_id") or ""),
           hashlib.sha256(str(credentials.get("cookies") or "").encode()).digest(), tuple(old_ids))
    with _evidence_lock:
        now = time.monotonic()
        if _evidence_cache and _evidence_cache[0] == key and now < _evidence_cache[1]:
            return _evidence_cache[2]
        result = read_receipt_evidence(credentials, old_ids)
        _evidence_cache = (key, time.monotonic() + 300, result)
        return result


def _same_steam_session(before):
    after = get_steam_credentials() or {}
    return all(str(before.get(k) or "") == str(after.get(k) or "") for k in ("steam_id", "cookies"))


def reconcile_receipts_once(buff_client, scan_inventory=scan_cs2_inventory):
    from app.services.workers import _buff_background_request_is_safe
    with get_buff_auth_lock(), buff_activity_guard():
        if not _buff_background_request_is_safe():
            return {"ok": False, "error": "购买任务或订单门禁正占用 BUFF，请等待购买轮次结束"}
        purchases = get_purchases()
        pending = [p for p in purchases if p.get("pending_receipt") and not p.get("assetid")]
        if not pending:
            return {"ok": True, "received": 0, "closed": 0, "pending": 0, "inventory": None}
        credentials = dict(get_steam_credentials() or {})
        cfg = load_app_config_validated()
        try:
            orders = buff_client.get_purchase_history_orders(
                (cfg.get("buff") or {}).get("game", "csgo"),
                [p.get("buff_order_id") for p in pending if p.get("buff_order_id")],
            )
        except httpx.RequestError as exc:
            reason = "TLS 连接被提前关闭" if "UNEXPECTED_EOF" in str(exc) or "TLS" in str(exc) else type(exc).__name__
            raise RuntimeError(
                "收货对账失败：BUFF 购买历史 GET buff.163.com/api/market/buy_order/history "
                f"连接异常（{reason}），本轮未应用收货匹配。"
            ) from None
        # An older paid order may have been interrupted before the immediate
        # fallback. Its own durable send claim still prevents any replay.
        from app.services.buff_offer_support import try_send_buyer_offer
        by_order = defaultdict(list)
        for order in orders:
            by_order[str(order.get("id") or "")].append(order)
        for purchase in pending:
            matches = by_order.get(str(purchase.get("buff_order_id") or ""), [])
            if (len(matches) == 1 and matches[0].get("state_text") == "等待你发起报价"
                    and matches[0].get("has_sent_offer") is not True):
                try_send_buyer_offer(buff_client, purchase, cfg, "等待你发起报价",
                                     lambda msg, level: log(msg, level, category="receive"))
        inventory, inventory_error = None, ""
        if any(x.get("state") == "SUCCESS" for x in orders):
            ok, items, inventory_error = scan_inventory()
            if ok:
                inventory = items
        diagnostics = {}
        received, closed = build_reconciliation_plan(
            purchases, orders, inventory or [], credentials.get("steam_id"),
            diagnostics=diagnostics,
        )
        if inventory is not None and diagnostics.get("missing_item_fingerprint"):
            evidence = _load_case_evidence(credentials, orders)
            diagnostics = {}
            received, closed = build_reconciliation_plan(
                purchases, orders, inventory, credentials.get("steam_id"),
                diagnostics=diagnostics, trade_evidence=evidence,
            )
        if not _same_steam_session(credentials):
            return {"ok": False, "error": "Steam 登录会话已变更，本轮未应用收货匹配"}
        _archive_closed(closed)  # Keep a durable audit before removing failed holdings.
        n_closed = sum(_same_steam_session(credentials) and db_apply_receipt_reconciliation(x["purchase"], closed=True) for x in closed)
        n_received = sum(_same_steam_session(credentials) and db_apply_receipt_reconciliation(x["purchase"], assetid=x["assetid"],
                        **({"receipt_evidence": x["receipt_evidence"]} if x.get("receipt_evidence") else {})) for x in received)
        remaining = sum(bool(p.get("pending_receipt")) for p in get_purchases())
        if n_received or n_closed:
            log(f"[收货对账] 已补记到账 {n_received} 件，已归档失败/退款 {n_closed} 笔，剩余待收货 {remaining} 件",
                "info", category="receive")
        return {"ok": True, "received": n_received, "closed": n_closed,
                "pending": remaining, "inventory": inventory, "inventory_error": inventory_error,
                "unmatched_reasons": diagnostics}
