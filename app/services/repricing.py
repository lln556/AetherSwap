"""Persist-before-send, recover-by-observation automatic market repricing."""
import math
import time
from decimal import Decimal, ROUND_CEILING
from sqlmodel import select
from app.trading_models import MarketListing, RepriceJob, TradingPolicy, TradeSettlement
from app.trading_ledger import OPEN_JOBS, add_event, minor


def buyer_total(seller_minor):
    # CS2, CNY wallet: 5% Steam + 10% publisher, each with a 7-fen minimum.
    return seller_minor + max(7, seller_minor * 5 // 100) + max(7, seller_minor * 10 // 100)


def seller_for_budget(buyer_minor):
    low, high = 1, max(1, buyer_minor)
    while low < high:
        mid = (low + high + 1) // 2
        if buyer_total(mid) <= buyer_minor:
            low = mid
        else:
            high = mid - 1
    return low


def evaluate_price(*, cost_minor, old_buyer_minor, market_minor, max_loss_percent=30,
                   min_change_minor=10, min_change_percent=1):
    if cost_minor <= 0 or old_buyer_minor <= 0 or market_minor <= 0 or not 0 <= max_loss_percent < 100:
        return {"adjust": False, "reason": "价格或成本数据无效"}
    floor = int((Decimal(cost_minor) * (100 - Decimal(str(max_loss_percent))) / 100).to_integral_value(rounding=ROUND_CEILING))
    seller = max(floor, seller_for_budget(market_minor))
    buyer = buyer_total(seller)
    result = {"target_seller_minor": seller, "target_buyer_minor": buyer, "floor_seller_minor": floor}
    if buyer >= old_buyer_minor:
        return {**result, "adjust": False, "reason": "新建议价不低于原价，保留挂单"}
    delta = old_buyer_minor - buyer
    if delta < min_change_minor or delta * 100 < old_buyer_minor * min_change_percent:
        return {**result, "adjust": False, "reason": "价差不足，保留原挂单排队位置"}
    return {**result, "adjust": True, "reason": "已触及亏损底线，按底线定价" if floor > seller_for_budget(market_minor) else "挂售满期，按当前行情降价"}


def item_has_job(purchase_id):
    from app.database import get_session
    with get_session() as s:
        return s.exec(select(RepriceJob).where(RepriceJob.purchase_id == purchase_id, RepriceJob.status.in_(OPEN_JOBS))).first() is not None


def reserved_purchase_ids():
    from app.database import get_session
    with get_session() as s:
        return set(s.exec(select(RepriceJob.purchase_id).where(RepriceJob.status.in_(OPEN_JOBS))).all())


def _update_job(job_id, status, error="", *, evidence=None, **fields):
    from app.database import get_session
    with get_session() as s:
        job = s.get(RepriceJob, job_id)
        if not job or job.status in ("completed", "sold", "cancelled"):
            return
        changed = job.status != status or job.error != error
        if status == "needs_review" and job.status != "needs_review":
            job.review_from = job.status
        job.status, job.error, job.updated_at = status, error, time.time()
        for k, v in fields.items():
            setattr(job, k, v)
        s.add(job)
        if changed:
            labels = {"planned": "调价：等待重试", "removing": "调价：准备撤单，核实库存", "awaiting_inventory": "调价：撤单结果待核实", "ready": "调价：已确认返库", "submitting": "调价：提交新挂单", "confirming": "调价：等待新挂单确认", "completed": "自动调价完成", "needs_review": "调价需要核实", "cancelled": "调价任务已取消"}
            if status == "ready" and error:
                labels["ready"] = "调价：等待重新上架"
            add_event(s, job.purchase_id, "reprice_" + status, labels.get(status, status), at=time.time(),
                      details={"job_id": job.id, "old_buyer_minor": job.old_buyer_minor, "new_buyer_minor": job.target_buyer_minor, "reason": error, "retry_at": job.retry_at, "consecutive_failures": job.consecutive_failures, **(evidence or {})})
        s.commit()


def _defer_withdrawal(job, error, outcome, *, uncertain=False, count_failure=True):
    """Persist bounded backoff shared by the account, without claiming a withdrawal."""
    from app.database import get_session
    failures = job["consecutive_failures"] + int(count_failure)
    limited = outcome.get("http_status") == 429 or "HTTP 429" in error
    base = 1800 if limited else 600
    delay = max(min(7200, base * 2 ** min(max(failures - 1, 0), 4)), outcome.get("retry_after") or 0)
    retry_at = time.time() + delay
    status = "awaiting_inventory" if uncertain else "planned"
    if failures >= 5 and not uncertain:
        status = "needs_review"
        error += "；连续失败 5 次，已暂停自动撤单，请排查后重新核实"
    _update_job(job["id"], status, error, consecutive_failures=failures,
                retry_at=retry_at, review_from="planned" if not uncertain else "awaiting_inventory",
                evidence={"request_sent": outcome.get("request_sent"), "http_status": outcome.get("http_status"), "phase": outcome.get("phase")})
    with get_session() as s:
        policy = s.get(TradingPolicy, 1) or TradingPolicy()
        policy.mutation_retry_at = max(policy.mutation_retry_at, retry_at)
        policy.mutation_error = error
        s.add(policy); s.commit()


def _listing_evidence(out):
    from app.sell_pipeline import _listing_response_body_preview
    out = out or {}
    return {"phase": "listing", "request_sent": out.get("request_sent", True),
            "http_status": out.get("status_code"), "transport": out.get("transport"),
            "response_body": _listing_response_body_preview(out.get("text")),
            "retry_after": _listing_response_body_preview(out.get("retry_after"))}


def _defer_listing(job, out):
    """Known rejections may retry; uncertain submissions remain observation-only."""
    from app.database import get_session
    from app.sell_pipeline import _listing_response_body_preview, _listing_cooldown
    from steam.market import parse_sell_response
    from steam.request_policy import parse_retry_after
    evidence = _listing_evidence(out)
    out = out or {}
    status = out.get("status_code")
    _, body = parse_sell_response(out.get("text"))
    error = (_listing_response_body_preview(out["error"]) if out.get("error") else
             f"上架响应 HTTP {status if status is not None else '未知'}；{evidence['response_body']}")
    if status == 429 or out.get("request_sent") is False:
        failures = job["consecutive_failures"] + 1
        delay = max(min(7200, (1800 if status == 429 else 600) * 2 ** min(failures - 1, 4)),
                    parse_retry_after(out.get("retry_after")))
        retry_at = time.time() + delay
        _update_job(job["id"], "needs_review" if failures >= 5 else "ready", error,
                    consecutive_failures=failures, retry_at=retry_at, review_from="ready", evidence=evidence)
        with get_session() as s:
            policy = s.get(TradingPolicy, 1) or TradingPolicy()
            policy.mutation_retry_at = max(policy.mutation_retry_at, retry_at)
            policy.mutation_error = error
            s.add(policy); s.commit()
        _listing_cooldown.defer(str(delay))
    elif status in (400, 401, 403, 404) or (status == 200 and body and body.get("success") is False):
        _update_job(job["id"], "needs_review", error, review_from="ready", evidence=evidence)
    else:
        _update_job(job["id"], "submitting", error + "；先核实挂单，勿重复提交",
                    retry_at=time.time() + 60, evidence=evidence)


def _confirm_new_listing(job, cred, cfg):
    """Confirm only this asset's observed market listing, then verify its price/state."""
    from app.services.listing_confirmation import sync_listing_confirmations
    from app.pipeline_context import PipelineContext
    from app.state import get_state
    from app.trading_ledger import record_snapshot
    ctx = PipelineContext(get_state(), "market_reconcile")
    ok, snapshot, error = sync_listing_confirmations(ctx, cfg, cred, assetids=[job["assetid"]])
    if not ok:
        _update_job(job["id"], "confirming", error, retry_at=time.time() + 60)
        return
    record_snapshot(snapshot, str(cred["steam_id"]))
    row = snapshot.get(job["assetid"])
    if not row:
        return  # Accepted POST is not proof of a live listing.
    if row["listing_id"] == job["old_steam_listing_id"] or row.get("seller_minor") != job["target_seller_minor"]:
        _update_job(job["id"], "needs_review", "新挂单与目标价格不一致，请核实", review_from="confirming")
    else:
        _update_job(job["id"], "completed" if row["status"] == "active" else "confirming")


def _refresh_job_quote(job, purchase, snapshot, cred, cfg, policy):
    from steam.session import create_market_session
    from app.strategy_engine import apply_strategy_to_config
    from app.database import get_session
    effective = apply_strategy_to_config(cfg, "sell")
    session = create_market_session(cred["cookies"], str(cred["steam_id"]))
    try:
        price, reason = _strategy_quote(session, purchase, snapshot, effective, effective.get("pipeline", {}))
    finally:
        session.close()
    if price is None:
        _defer_withdrawal(job, "撤单前重新获取定价未通过：" + reason, {"request_sent": False, "phase": "quote"})
        return False
    remote = snapshot[job["assetid"]]
    decision = evaluate_price(cost_minor=minor(purchase["price"]), old_buyer_minor=remote.get("buyer_minor") or 0,
                              market_minor=price, max_loss_percent=policy.max_loss_percent,
                              min_change_minor=policy.min_change_minor, min_change_percent=policy.min_change_percent)
    if not decision["adjust"]:
        _update_job(job["id"], "cancelled", "重新评估后保留原挂单：" + decision["reason"])
        return False
    fields = {k: decision[k] for k in ("target_seller_minor", "target_buyer_minor", "floor_seller_minor")}
    fields["last_quoted_at"] = time.time()
    _update_job(job["id"], "planned", "撤单前已按最新行情重新定价", **fields)
    job.update(fields)
    return True


def _strategy_quote(session, purchase, snapshot, cfg, pcfg):
    """Reuse normal listing guards, excluding only the listing being replaced."""
    from app.sell_pipeline import _build_listing_plan
    from app.pipeline_context import PipelineContext
    from app.state import get_state

    class QuoteContext(PipelineContext):
        last_reason = "当前出售策略不允许重新上架，保留原挂单"

        def is_stop_requested(self):
            # Purchase pipeline stop does not stop the independent market worker.
            return False

        def log(self, msg, level="info", category="steam"):
            self.last_reason = msg
            super().log(msg, level, category)

    ctx = QuoteContext(get_state(), "market_reprice")
    aid = str(purchase["assetid"])
    remaining = {k: v for k, v in snapshot.items() if k != aid}
    names = {k: v.get("market_hash_name") or v.get("name") or "" for k, v in remaining.items()}
    record = {**purchase, "listing": False, "reprice_reserved": False}
    plan = _build_listing_plan(
        ctx, cfg, session,
        [{"name": purchase["name"], "market_hash_name": purchase["name"], "assetid": aid, "appid": 730}],
        int(pcfg.get("sell_strategy", 4)), pcfg, [record], True, set(remaining), names, names, "CNY", {},
    )
    return (minor(plan[0]["list_price"]), plan[0]["reason"]) if plan else (None, ctx.last_reason)


def plan_repricing(snapshot, cred, cfg):
    from app.database import get_session, Purchase
    from steam.session import create_market_session
    from app.strategy_engine import apply_strategy_to_config
    account_id = str(cred.get("steam_id") or "")
    with get_session() as s:
        policy = s.get(TradingPolicy, 1) or TradingPolicy()
        if not policy.enabled or policy.mutation_retry_at > time.time():
            return
        policy_data = policy.model_dump()
        due = [r.model_dump() for r in s.exec(select(MarketListing).where(MarketListing.status == "active", MarketListing.account_id == account_id)).all()
               if r.activated_at and r.next_evaluation_at and r.next_evaluation_at <= time.time()]
    effective = apply_strategy_to_config(cfg, "sell")
    pcfg = effective.get("pipeline", {})
    if int(pcfg.get("sell_strategy", 4)) == 4:
        return
    session = create_market_session(cred["cookies"], account_id)
    try:
        for listing in due[:5]:
            remote = snapshot.get(listing["assetid"])
            if not remote or remote["status"] != "active" or remote["listing_id"] != listing["steam_listing_id"]:
                continue
            if remote.get("currency") != "CNY" or not remote.get("buyer_minor") or not remote.get("seller_minor"):
                continue  # Explicit currency/amount evidence is required before withdrawing.
            with get_session() as s:
                p = s.get(Purchase, listing["purchase_id"])
                if not p or p.archived_at or p.sale_price or s.get(TradeSettlement, p.id) or item_has_job(p.id):
                    continue
                purchase, cost = p.model_dump(), minor(p.price)
            price, reason = _strategy_quote(session, purchase, snapshot, effective, pcfg)
            if price is None:
                decision = {"adjust": False, "reason": reason}
            else:
                decision = evaluate_price(cost_minor=cost, old_buyer_minor=remote["buyer_minor"], market_minor=price,
                                          max_loss_percent=policy_data["max_loss_percent"], min_change_minor=policy_data["min_change_minor"], min_change_percent=policy_data["min_change_percent"])
                decision["market_reason"] = reason
            with get_session() as s:
                current = s.get(MarketListing, listing["id"])
                p = s.get(Purchase, listing["purchase_id"])
                if not current or current.status != "active" or not p or p.sale_price:
                    continue
                current.last_reason = decision["reason"]
                current.next_evaluation_at = time.time() + policy_data["reevaluate_minutes"] * 60
                s.add(current)
                add_event(s, p.id, "reprice_evaluated", decision["reason"], at=time.time(), details=decision)
                previous = s.exec(select(RepriceJob).where(RepriceJob.listing_id == current.id)).first()
                if decision["adjust"] and (not previous or previous.status == "cancelled"):
                    fields = dict(listing_id=current.id, purchase_id=p.id, account_id=account_id, old_assetid=p.assetid,
                                  assetid=p.assetid, old_steam_listing_id=current.steam_listing_id,
                                  old_buyer_minor=remote["buyer_minor"], target_buyer_minor=decision["target_buyer_minor"],
                                  target_seller_minor=decision["target_seller_minor"], floor_seller_minor=decision["floor_seller_minor"],
                                  status="planned", retry_at=0, consecutive_failures=0, last_quoted_at=time.time(), updated_at=time.time(), error="")
                    if previous:
                        for key, value in fields.items():
                            setattr(previous, key, value)
                        s.add(previous)
                        add_event(s, p.id, "reprice_replanned", "行情变化，重新安排调价", at=time.time(), details=fields)
                    else:
                        s.add(RepriceJob(**fields))
                s.commit()
    finally:
        session.close()


def resume_jobs(snapshot, cred, cfg):
    from app.database import get_session, Purchase
    from app.config_loader import get_steam_credentials
    from app.strategy_engine import apply_strategy_to_config
    from app.steam_delist import delist_item
    from app.steam_listings import fetch_listing_snapshot
    from app.inventory_cs2 import scan_cs2_inventory
    from app.trading_ledger import record_snapshot
    from steam.session import create_market_session, parse_cookies
    from steam.market import list_item, parse_sell_response
    account_id = str(cred.get("steam_id") or "")
    auto_sell_paused = int(apply_strategy_to_config(cfg, "sell").get("pipeline", {}).get("sell_strategy", 4)) == 4
    with get_session() as s:
        policy = s.get(TradingPolicy, 1) or TradingPolicy()
        jobs = [j.model_dump() for j in s.exec(select(RepriceJob).where(RepriceJob.account_id == account_id, RepriceJob.status.in_(OPEN_JOBS))).all()]
    for job in jobs:
        if job["status"] == "needs_review":
            continue
        if (not policy.enabled or auto_sell_paused) and job["status"] in ("planned", "removing"):
            continue
        current_cred = get_steam_credentials()
        if current_cred.get("cookies") != cred.get("cookies") or str(current_cred.get("steam_id") or "") != account_id:
            return
        with get_session() as s:
            p = s.get(Purchase, job["purchase_id"])
            if not p or p.archived_at or p.account_id != account_id or p.sale_price or s.get(TradeSettlement, p.id):
                _update_job(job["id"], "cancelled", "购买记录已变化或已售出")
                continue
            name, purchase = p.name, p.model_dump()
            floor = int((Decimal(str(p.price)) * 100 * (100 - policy.max_loss_percent) / 100).to_integral_value(rounding=ROUND_CEILING))
        if job["target_seller_minor"] < floor:
            _update_job(job["id"], "needs_review", "新设置的亏损底线高于任务目标价格")
            continue
        remote = snapshot.get(job["assetid"])
        if remote and remote["listing_id"] != job["old_steam_listing_id"]:
            if remote.get("seller_minor") != job["target_seller_minor"]:
                _update_job(job["id"], "needs_review", "发现价格不同的新挂单，请核实人工操作")
                continue
            record_snapshot(snapshot, account_id)
            _update_job(job["id"], "completed" if remote["status"] == "active" else "confirming")
            continue
        if job["retry_at"] > time.time():
            continue
        if job["status"] in ("planned", "removing") and remote and remote["listing_id"] == job["old_steam_listing_id"]:
            if policy.mutation_retry_at > time.time():
                continue
            if job["consecutive_failures"] >= 5:
                _update_job(job["id"], "needs_review", "连续失败 5 次，已暂停自动撤单，请排查后重新核实", review_from="planned")
                continue
            if time.time() - job["last_quoted_at"] > 900:
                if not _refresh_job_quote(job, purchase, snapshot, cred, cfg, policy):
                    return
            # Persist intent before the non-transactional external operation.
            _update_job(job["id"], "removing", attempts=job["attempts"] + 1)
            outcome = {}
            ok, aid, error = delist_item(job["assetid"], name, outcome=outcome)
            if ok and aid:
                with get_session() as s:
                    p = s.get(Purchase, job["purchase_id"])
                    other = s.exec(select(Purchase).where(Purchase.assetid == aid, Purchase.id != p.id)).first()
                    if other or p.sale_price:
                        _update_job(job["id"], "needs_review", "返库资产已被其他记录使用或物品已成交")
                        continue
                    p.assetid, p.listing, p.listing_status = aid, False, None
                    s.add(p)
                    old = s.get(MarketListing, job["listing_id"])
                    old.status, old.ended_at = "cancelled", time.time()
                    s.add(old)
                    s.commit()
                _update_job(job["id"], "ready", assetid=aid, consecutive_failures=0, evidence={"request_sent": True})
            else:
                uncertain = outcome.get("request_sent", True) and not outcome.get("rejected")
                _defer_withdrawal(job, error or "撤单结果待核实", outcome, uncertain=uncertain)
            return  # One mutation per cycle; don't send a second request after throttling.
        if job["status"] in ("removing", "awaiting_inventory"):
            if remote:
                _defer_withdrawal(job, "已核实原挂单仍在；" + (job["error"] or "撤单未生效"), {}, count_failure=False)
                continue
            ok, inventory, _ = scan_cs2_inventory()
            if not ok:
                continue
            returned = [i for i in inventory if str(i.get("assetid")) == job["assetid"]]
            if len(returned) == 1:
                with get_session() as s:
                    p = s.get(Purchase, job["purchase_id"])
                    p.listing, p.listing_status = False, None
                    old = s.get(MarketListing, job["listing_id"])
                    old.status, old.ended_at = "cancelled", time.time()
                    s.add(p); s.add(old); s.commit()
                _update_job(job["id"], "ready")
            elif time.time() - job["updated_at"] > 1800:
                _update_job(job["id"], "needs_review", "挂单已消失，但无法唯一确认返库资产；请核实成交与库存")
            continue
        if job["status"] in ("submitting", "confirming"):
            # No blind retry: Steam may have accepted the previous POST.
            if time.time() - job["updated_at"] > 1800:
                _update_job(job["id"], "needs_review", "上架请求结果未能确认，请核实 Steam 挂单后恢复")
            continue
        if job["status"] != "ready" or remote or policy.mutation_retry_at > time.time():
            continue
        from app.sell_pipeline import _listing_cooldown
        if _listing_cooldown.remaining() > 0:
            continue
        from app.services.account_region import refresh_account_region_currency
        region = refresh_account_region_currency(cookies_raw=cred["cookies"])
        if not region.get("ok") or region.get("currency_code") != "CNY":
            _update_job(job["id"], "needs_review", "无法确认人民币结算币种，保留库存")
            continue
        ok, fresh, _ = fetch_listing_snapshot(cred["cookies"])
        if not ok or job["assetid"] in fresh:
            continue
        ok_inventory, inventory, inventory_error = scan_cs2_inventory()
        if not ok_inventory:
            _defer_listing(job, {"request_sent": False, "error": "上架前库存核实失败：" + (inventory_error or "库存暂不可用")})
            return
        matching = [item for item in inventory if str(item.get("assetid")) == job["assetid"]]
        if len(matching) != 1 or not matching[0].get("can_sell"):
            _update_job(job["id"], "needs_review", "库存中未找到唯一可上架的目标资产，请核实", review_from="ready")
            continue
        current_cred = get_steam_credentials()
        if current_cred.get("cookies") != cred.get("cookies") or str(current_cred.get("steam_id") or "") != account_id:
            return
        sess = create_market_session(cred["cookies"], account_id)
        try:
            sid = parse_cookies(cred["cookies"]).get("sessionid")
            if not sid:
                _update_job(job["id"], "needs_review", "Cookie 缺少 sessionid，尚未提交上架", review_from="ready")
                continue
            _update_job(job["id"], "submitting", attempts=job["attempts"] + 1)
            out = list_item(sess, sid, 730, "2", job["assetid"], job["target_seller_minor"])
            ok_response, body = parse_sell_response(out.get("text") if out else None)
            if out and out.get("status_code") == 200 and body and (ok_response or "pending confirmation" in str(body.get("message", "")).lower()):
                with get_session() as s:
                    p = s.get(Purchase, job["purchase_id"])
                    p.listing, p.listing_status = True, "submitted"
                    s.add(p)
                    s.add(MarketListing(purchase_id=p.id, account_id=account_id, assetid=job["assetid"],
                                        buyer_minor=job["target_buyer_minor"], seller_minor=job["target_seller_minor"]))
                    s.commit()
                _update_job(job["id"], "confirming", consecutive_failures=0, retry_at=0, evidence=_listing_evidence(out))
                _confirm_new_listing(job, cred, cfg)
            else:
                _defer_listing(job, out)
        finally:
            sess.close()
        return  # At most one new listing POST per cycle, including after rejection.
