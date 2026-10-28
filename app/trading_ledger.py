"""Transaction-local business journal and materialized listing/settlement records."""
import json
import math
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from sqlmodel import select
from app.trading_models import MarketListing, RepriceJob, TradeEvent, TradeSettlement, TradingPolicy

OPEN_JOBS = {"planned", "removing", "awaiting_inventory", "ready", "submitting", "confirming", "needs_review"}
OPEN_LISTINGS = {"submitted", "pending_confirmation", "active", "unknown", "repricing"}
TZ = ZoneInfo("Asia/Shanghai")
_snapshot_lock = threading.RLock()


def minor(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def add_event(session, purchase_id, kind, message, *, key=None, at=None,
              precision="exact", source="application", details=None):
    if key and session.exec(select(TradeEvent).where(TradeEvent.dedupe_key == key)).first():
        return
    session.add(TradeEvent(purchase_id=purchase_id, kind=kind, message=message,
                          dedupe_key=key, occurred_at=at, precision=precision, source=source,
                          details=json.dumps(details or {}, ensure_ascii=False, sort_keys=True)))


def audit_purchase(session, row, before, *, legacy=False):
    """Called before the same DB transaction commits the purchase mutation."""
    now = time.time()
    if not row.account_id and not legacy and before is None:
        from app.config_loader import get_steam_credentials
        row.account_id = str(get_steam_credentials().get("steam_id") or "")
    after = row.model_dump()
    if before is None:
        add_event(session, row.id, "purchased", "已记录购买", key=f"purchase:{row.id}",
                  at=row.at or None, source="legacy" if legacy else "application",
                  details={"name": row.name, "cost_minor": minor(row.price), "order_id": row.buff_order_id})
    else:
        tracked = ("assetid", "pending_receipt", "listing", "listing_status", "sale_price", "price", "name")
        changes = {k: {"old": before.get(k), "new": after.get(k)} for k in tracked if before.get(k) != after.get(k)}
        if changes:
            kind, message = "record_updated", "交易记录已更新"
            if before.get("pending_receipt") and not row.pending_receipt and row.assetid:
                kind, message = "received", "饰品已收货"
            elif before.get("assetid") and before.get("assetid") != row.assetid:
                kind, message = "asset_changed", "资产标识已变更"
            elif "listing_status" in changes:
                labels = {"submitted": "上架请求已提交，等待核实", "pending_confirmation": "等待 Steam 确认", "active": "已核实正式在售", "error": "状态需要核实"}
                kind, message = "listing_state", labels.get(row.listing_status, "挂单状态已更新")
            add_event(session, row.id, kind, message, at=now, details=changes)
    # Legacy sold_at is discovery time, never a precise Steam execution time.
    if row.sale_price and row.sale_price > 0 and not session.get(TradeSettlement, row.id):
        session.add(TradeSettlement(purchase_id=row.id, assetid=row.assetid or "",
                                   proceeds_minor=minor(Decimal(str(row.sale_price)) / Decimal("1.15")),
                                   detected_at=row.sold_at or now, amount_source="estimated_legacy"))
        add_event(session, row.id, "sale_recorded", "已记录售出，实际成交时间待核实",
                  key=f"legacy-settlement:{row.id}", precision="unknown", source="legacy")


def import_listing_action(session, sale):
    from app.database import Purchase
    if session.exec(select(MarketListing).where(MarketListing.legacy_sale_id == sale.id)).first():
        return
    rows = session.exec(select(Purchase).where(Purchase.assetid == sale.assetid)).all()
    if len(rows) != 1:
        return  # Never bind personal or same-name items by name.
    p = rows[0]
    pending = session.exec(select(MarketListing).where(MarketListing.purchase_id == p.id,
                  MarketListing.status == "submitted", MarketListing.legacy_sale_id == None,
                  MarketListing.steam_listing_id == None).order_by(MarketListing.submitted_at.desc())).first()
    if pending:
        pending.legacy_sale_id = sale.id
        session.add(pending)
    else:
        session.add(MarketListing(purchase_id=p.id, account_id=p.account_id, assetid=sale.assetid,
                                 legacy_sale_id=sale.id, buyer_minor=minor(sale.price),
                                 submitted_at=sale.at, status="sold" if p.sale_price else "submitted"))
    add_event(session, p.id, "listing_submitted", "上架请求已提交", key=f"listing-action:{sale.id}",
              at=sale.at, source="listing_action", details={"buyer_minor": minor(sale.price)})


def bootstrap_ledger():
    from app.database import Purchase, Sale, get_session
    with get_session() as s:
        for p in s.exec(select(Purchase)).all():
            audit_purchase(s, p, None, legacy=True)
        for action in s.exec(select(Sale).order_by(Sale.at)).all():
            import_listing_action(s, action)
        if not s.get(TradingPolicy, 1):
            s.add(TradingPolicy())
        s.commit()


def get_policy():
    from app.database import get_session
    with get_session() as s:
        p = s.get(TradingPolicy, 1)
        return (p or TradingPolicy()).model_dump()


def live_listing(s, purchase_id):
    return s.exec(select(MarketListing).where(MarketListing.purchase_id == purchase_id,
                  MarketListing.status.in_(OPEN_LISTINGS)).order_by(MarketListing.submitted_at.desc())).first()


def record_snapshot(snapshot, account_id, now=None, *, purchase_ids=None):
    """A complete remote snapshot provides positive listing evidence, not sale evidence."""
    from app.database import Purchase, get_session
    now = time.time() if now is None else now
    with _snapshot_lock, get_session() as s:
        policy = s.get(TradingPolicy, 1) or TradingPolicy()
        for p in s.exec(select(Purchase)).all():
            if purchase_ids is not None and (p.id not in purchase_ids or not p.listing):
                continue
            if p.archived_at or p.sale_price or p.pending_receipt or (p.account_id and p.account_id != account_id):
                continue
            remote = snapshot.get(str(p.assetid or ""))
            if not remote:
                current = live_listing(s, p.id)
                if current and current.status in ("active", "pending_confirmation"):
                    current.status = "unknown"
                    current.checked_at = now
                    current.last_reason = "挂单未出现在最新列表，等待成交或返库证据"
                    s.add(current)
                continue
            p.account_id = account_id
            current = s.exec(select(MarketListing).where(MarketListing.steam_listing_id == remote["listing_id"])).first()
            if current and current.purchase_id != p.id:
                continue
            if not current:
                candidate = live_listing(s, p.id)
                if candidate and not candidate.steam_listing_id:
                    current = candidate
                else:
                    current = MarketListing(purchase_id=p.id, assetid=p.assetid, account_id=account_id,
                                            submitted_at=now)
                current.steam_listing_id = remote["listing_id"]
            old_status = current.status
            current.account_id = account_id
            current.status = remote["status"]
            current.checked_at = now
            if remote.get("buyer_minor") is not None:
                current.buyer_minor = remote["buyer_minor"]
            if remote.get("seller_minor") is not None:
                current.seller_minor = remote["seller_minor"]
            if remote.get("currency"):
                current.currency = remote["currency"]
            if current.status == "active" and current.activated_at is None:
                remote_created = remote.get("created_at")
                if old_status != "pending_confirmation" and isinstance(remote_created, (int, float)) and p.at <= remote_created <= now:
                    current.activated_at = remote_created
                    current.activation_source = "steam_created"
                else:
                    current.activated_at = now
                    current.activation_source = "first_verified"
                current.next_evaluation_at = current.activated_at + policy.age_hours * 3600
            p.listing = True
            p.listing_status = current.status
            s.add(current)
            s.add(p)
            if old_status != current.status:
                # A temporarily missing/repricing listing can be observed again.
                # Its identity is unchanged: record each verified state once.
                previous = s.exec(select(TradeEvent).where(
                    TradeEvent.purchase_id == p.id, TradeEvent.kind == "listing_verified")).all()
                observed = any(json.loads(e.details).get("listing_id") == current.steam_listing_id
                               and json.loads(e.details).get("state") == current.status for e in previous)
                if not observed:
                    add_event(s, p.id, "listing_verified", "已核实正式在售" if current.status == "active" else "等待 Steam 确认",
                              key=f"listing:{current.steam_listing_id}:{current.status}",
                              at=now, details={"listing_id": current.steam_listing_id, "state": current.status})
        s.commit()


def record_settlements(records, account_id):
    from app.database import Purchase, get_session
    now = time.time()
    count = 0
    with get_session() as s:
        purchases = s.exec(select(Purchase)).all()
        for rec in records:
            matches = [p for p in purchases if str(p.assetid or "") == rec["assetid"] and (not p.account_id or p.account_id == account_id)]
            if len(matches) != 1:
                # A relisted item can have an earlier asset ID.
                listings = s.exec(select(MarketListing).where(MarketListing.assetid == rec["assetid"], MarketListing.account_id == account_id)).all()
                ids = {q.purchase_id for q in listings}
                matches = [p for p in purchases if p.id in ids]
            if len(matches) != 1:
                continue
            p = matches[0]
            existing = s.get(TradeSettlement, p.id)
            if existing and existing.external_id == rec["external_id"] and existing.amount_source == "steam_history":
                continue
            settlement = existing or TradeSettlement(purchase_id=p.id)
            settlement.external_id = rec["external_id"]
            settlement.assetid = rec["assetid"]
            settlement.proceeds_minor = rec["proceeds_minor"]
            settlement.currency = rec["currency"]
            settlement.amount_source = "steam_history"
            settlement.date_label = rec.get("date_label", "")
            settlement.sold_day = rec.get("sold_day")
            settlement.time_precision = "day" if settlement.sold_day else "unknown"
            settlement.detected_at = existing.detected_at if existing else now
            p.account_id = account_id
            p.listing = False
            p.listing_status = None
            # Compatibility values are not used as authoritative amounts in the new UI.
            if rec["currency"] == "CNY":
                p.sale_price = round(rec["proceeds_minor"] / 100 * 1.15, 2)
            p.sold_at = p.sold_at or now
            s.add(p)
            s.add(settlement)
            for listing in s.exec(select(MarketListing).where(MarketListing.purchase_id == p.id)).all():
                if listing.status in OPEN_LISTINGS:
                    listing.status = "sold"
                    listing.ended_at = now
                    listing.last_reason = "Steam 成交历史已确认售出"
                    s.add(listing)
            for job in s.exec(select(RepriceJob).where(RepriceJob.purchase_id == p.id, RepriceJob.status.in_(OPEN_JOBS))).all():
                job.status, job.updated_at = "sold", now
                s.add(job)
            add_event(s, p.id, "sold", "Steam 已确认成交", key="sale:" + rec["external_id"],
                      precision=settlement.time_precision, source="steam_history",
                      details={"proceeds_minor": rec["proceeds_minor"], "currency": rec["currency"], "date_label": settlement.date_label})
            count += 1
        s.commit()
    return count


def record_error(purchase_id, message, key=None):
    from app.database import get_session
    with get_session() as s:
        add_event(s, purchase_id, "attention", message, key=key, at=time.time())
        s.commit()


def begin_listing_attempt(purchase_id, assetid, buyer_minor, seller_minor, currency):
    from app.database import get_session, Purchase
    from app.services.repricing import item_has_job
    with get_session() as s:
        p = s.get(Purchase, purchase_id)
        if (not p or p.archived_at or p.assetid != assetid or p.listing or p.pending_receipt or p.sale_price
                or s.get(TradeSettlement, p.id) or item_has_job(p.id)):
            raise ValueError("物品状态已变化，未提交上架")
        listing = MarketListing(purchase_id=p.id, account_id=p.account_id, assetid=assetid,
                                buyer_minor=buyer_minor, seller_minor=seller_minor, currency=currency)
        p.listing, p.listing_status = True, "submitted"
        s.add(p); s.add(listing)
        add_event(s, p.id, "listing_attempt", "准备提交 Steam 上架请求", at=time.time(),
                  details={"buyer_minor": buyer_minor, "seller_minor": seller_minor, "currency": currency})
        s.commit()
        return listing.id


def finish_listing_attempt(listing_id, *, rejected=False, message=""):
    if not listing_id:
        return
    from app.database import get_session, Purchase
    with get_session() as s:
        listing = s.get(MarketListing, listing_id)
        if not listing or listing.status != "submitted":
            return
        listing.last_reason = message
        if rejected:
            listing.status, listing.ended_at = "rejected", time.time()
            p = s.get(Purchase, listing.purchase_id)
            if p and p.assetid == listing.assetid and p.listing_status == "submitted":
                p.listing, p.listing_status = False, "error"
                s.add(p)
        s.add(listing)
        add_event(s, listing.purchase_id, "listing_rejected" if rejected else "listing_unknown",
                  "上架被拒绝" if rejected else "上架结果待核实", at=time.time(), details={"reason": message})
        s.commit()


def sync_manual_sale(session, row, data):
    if "_sale_proceeds_minor" not in data:
        return
    amount = data["_sale_proceeds_minor"]
    settlement = session.get(TradeSettlement, row.id)
    if amount > 0:
        settlement = settlement or TradeSettlement(purchase_id=row.id, assetid=row.assetid or "")
        settlement.proceeds_minor, settlement.currency = amount, "CNY"
        settlement.amount_source = "user_reported"
        settlement.external_id = None
        settlement.sold_at, settlement.sold_day, settlement.time_precision = None, None, "unknown"
        settlement.detected_at = time.time()
        row.listing, row.listing_status = False, None
        session.add(row); session.add(settlement)
    elif settlement:
        session.delete(settlement)
    add_event(session, row.id, "manual_settlement", "用户修正成交记录", at=time.time(), source="user",
              details={"proceeds_minor": amount, "currency": "CNY"})


def purchase_state(purchase, settlement, listing=None, job=None):
    """Shared sale/holding classification; compatibility prices are not evidence."""
    if settlement is not None:
        return "sold"
    if purchase.archived_at:
        return "archived"
    if job:
        return "attention" if job.status == "needs_review" else "repricing"
    if purchase.pending_receipt:
        return "pending_receipt"
    if listing and listing.status in OPEN_LISTINGS:
        return listing.status
    return "unknown" if purchase.listing or purchase.listing_status == "error" else "holding"


def dashboard_overview():
    """Aggregate the same durable purchase/settlement facts as the trading center."""
    from app.database import Purchase, get_session
    with get_session() as s:
        purchases = s.exec(select(Purchase).order_by(Purchase.at.desc(), Purchase.id.desc())).all()
        settlements = {r.purchase_id: r for r in s.exec(select(TradeSettlement)).all()}
        states = {p.id: purchase_state(p, settlements.get(p.id)) for p in purchases}
        sold = [p for p in purchases if states[p.id] == "sold"]
        holdings = [p for p in purchases if states[p.id] not in ("sold", "archived")]
        recent = [p for p in purchases if p.archive_reason != "order_closed" or states[p.id] == "sold"]
        by_currency = {}
        profit_minor, cny_count, estimated_count, unknown_count = 0, 0, 0, 0
        ratios = []
        for p in sold:
            settlement = settlements[p.id]
            currency = settlement.currency or "UNKNOWN"
            group = by_currency.setdefault(currency, {"count": 0, "proceeds_minor": None,
                                                       "estimated_count": 0, "unknown_count": 0})
            group["count"] += 1
            if settlement.amount_source not in ("steam_history", "user_reported"):
                group["estimated_count"] += 1
                estimated_count += 1
            amount = settlement.proceeds_minor
            if amount is None:
                group["unknown_count"] += 1
                unknown_count += 1
                continue
            group["proceeds_minor"] = (group["proceeds_minor"] or 0) + amount
            if currency == "CNY":
                cny_count += 1
                cost = minor(p.price)
                profit_minor += amount - cost
                if amount > 0 and cost > 0:
                    ratios.append(Decimal(cost) / Decimal(amount))
        return {
            "holding_cost": sum(minor(p.price) for p in holdings) / 100,
            "holding_count": len(holdings),
            "sold_count": len(sold),
            "total_purchased": sum(minor(p.price) for p in sold) / 100,
            # Retain the old CNY gross field for API compatibility only. The UI
            # uses proceeds_by_currency and never reverses fees on this value.
            "total_sold": sum(minor(p.sale_price or 0) for p in sold
                              if settlements[p.id].currency == "CNY") / 100,
            "total_profit": profit_minor / 100 if cny_count or not sold else None,
            "discount_ratio": float(round(sum(ratios) / len(ratios), 4)) if ratios else None,
            "proceeds_by_currency": by_currency,
            "cny_settled_count": cny_count,
            "estimated_sold_count": estimated_count,
            "unknown_proceeds_count": unknown_count,
            "last_purchase_at": max((p.at for p in recent), default=None),
            "recent_purchases": [{"name": p.name, "price": p.price, "at": p.at,
                                  "sold": states[p.id] == "sold", "state": states[p.id],
                                  "pending_receipt": bool(p.pending_receipt)} for p in recent[:5]],
        }


def trading_overview(days=0, status="", query="", page=1, page_size=50):
    from app.database import Purchase, get_session
    today = datetime.now(TZ).date()
    cutoff = datetime.combine(today - timedelta(days=max(days - 1, 0)), datetime.min.time(), TZ).timestamp() if days else 0
    with get_session() as s:
        purchases = s.exec(select(Purchase).order_by(Purchase.at.desc())).all()
        settlements = {r.purchase_id: r for r in s.exec(select(TradeSettlement)).all()}
        listings = s.exec(select(MarketListing).order_by(MarketListing.submitted_at)).all()
        latest = {r.purchase_id: r for r in listings}
        jobs = s.exec(select(RepriceJob).order_by(RepriceJob.created_at)).all()
        active_jobs = {r.purchase_id: r for r in jobs if r.status in OPEN_JOBS}
        summary = {"today_bought": 0, "today_cost_minor": 0, "today_sold": 0, "today_proceeds_minor": 0,
                   "active": 0, "pending_confirmation": 0, "attention": 0, "repricing": 0}
        items, sold_rows, listed_rows = [], [], []
        for p in purchases:
            settlement, listing, job = settlements.get(p.id), latest.get(p.id), active_jobs.get(p.id)
            state = purchase_state(p, settlement, listing, job)
            row = {"id": p.id, "name": p.name, "assetid": p.assetid, "icon_url": p.icon_url,
                   "cost_minor": minor(p.price), "purchased_at": p.at, "state": state,
                   "listing": listing.model_dump() if listing else None,
                   "settlement": settlement.model_dump() if settlement else None,
                   "job": job.model_dump() if job else None,
                   "listing_count": sum(r.purchase_id == p.id for r in listings)}
            if p.archive_reason != "order_closed" and p.at and datetime.fromtimestamp(p.at, TZ).date() == today:
                summary["today_bought"] += 1
                summary["today_cost_minor"] += minor(p.price)
            if settlement and settlement.sold_day == today.isoformat():
                summary["today_sold"] += 1
                if settlement.currency == "CNY" and settlement.proceeds_minor is not None:
                    summary["today_proceeds_minor"] += settlement.proceeds_minor
            if not settlement and not p.archived_at and listing and listing.status in ("active", "pending_confirmation"):
                summary[listing.status] += 1
            if state == "repricing":
                summary["repricing"] += 1
            if state in ("attention", "unknown", "submitted") or (job and job.error):
                summary["attention"] += 1
            matches_query = not query or query.casefold() in p.name.casefold() or query in str(p.assetid or "")
            if matches_query:
                if settlement and (not days or (settlement.sold_day and settlement.sold_day >= datetime.fromtimestamp(cutoff, TZ).date().isoformat())):
                    sold_rows.append(row)
                if not settlement and (listing and listing.status in OPEN_LISTINGS or job):
                    listed_rows.append(row)
                if p.at >= cutoff and (not status or state == status):
                    items.append(row)
        sold_rows.sort(key=lambda r: (r["settlement"].get("sold_day") or "", r["settlement"]["detected_at"]), reverse=True)
        start = (page - 1) * page_size
        return {"items": items[start:start + page_size], "total": len(items), "page": page, "page_size": page_size,
                "listings": listed_rows, "settlements": sold_rows[start:start + page_size], "settlements_total": len(sold_rows),
                "summary": summary, "timezone": "Asia/Shanghai", "policy": get_policy()}
