"""Independent, persisted market reconciliation schedule."""
import threading
import time
from sqlmodel import select
from app.trading_models import TradingPolicy, RepriceJob, MarketListing
from app.services.lifecycle import is_shutting_down, is_shutdown_error, wait_for_shutdown

_cycle_lock = threading.Lock()


def run_trading_cycle():
    from app.database import Purchase, get_session, get_engine
    from app.config_loader import get_steam_credentials, load_app_config_validated
    from app.steam_listings import fetch_listing_snapshot
    from app.steam_sale_history import fetch_sale_history
    from app.trading_ledger import record_snapshot, record_settlements
    from app.services.listing_confirmation import sync_listing_confirmations
    from app.services.repricing import plan_repricing, resume_jobs
    from app.sell_pipeline import _sell_phase_lock
    from app.pipeline_context import PipelineContext
    from app.state import get_state
    if not _cycle_lock.acquire(blocking=False):
        return {"ok": False, "error": "市场核实正在执行"}
    if not _sell_phase_lock.acquire(blocking=False):
        _cycle_lock.release()
        return {"ok": False, "error": "正在提交上架，稍后核实"}
    db_lock = None
    try:
        if get_engine().dialect.name == "postgresql":
            from sqlalchemy import text
            db_lock = get_engine().connect()
            if not db_lock.execute(text("SELECT pg_try_advisory_lock(427184, 1)")).scalar():
                return {"ok": False, "error": "另一实例正在核实市场状态"}
        cred, cfg = get_steam_credentials(), load_app_config_validated()
        account = str(cred.get("steam_id") or "")
        if not account or not cred.get("cookies"):
            return {"ok": False, "error": "缺少 Steam 登录信息"}
        with get_session() as s:
            owned = {str(p.assetid): p.at for p in s.exec(select(Purchase)).all()
                     if p.assetid and (not p.account_id or p.account_id == account)}
            for row in s.exec(select(MarketListing).where(MarketListing.account_id == account)).all():
                p = s.get(Purchase, row.purchase_id)
                if p:
                    owned[row.assetid] = p.at
        ok, snapshot, err = fetch_listing_snapshot(cred["cookies"])
        if not ok:
            return {"ok": False, "error": err}
        # Re-check account after requests; never apply one account's snapshot to another.
        if get_steam_credentials().get("cookies") != cred["cookies"]:
            return {"ok": False, "error": "Steam 会话已切换，推迟核实"}
        record_snapshot(snapshot, account)
        ok_history, records, hist_error = fetch_sale_history(cred["cookies"], owned)
        if get_steam_credentials().get("cookies") != cred["cookies"]:
            return {"ok": False, "error": "Steam 会话已切换，推迟核实"}
        sold = record_settlements(records, account) if ok_history else 0
        ctx = PipelineContext(get_state(), "market_reconcile")
        ok_confirm, refreshed, _ = sync_listing_confirmations(ctx, cfg, cred, snapshot=snapshot)
        if ok_confirm:
            snapshot = refreshed
            record_snapshot(snapshot, account)
        # Unknown sale evidence blocks repricing, but doesn't discard valid listing observations.
        if ok_history:
            resume_jobs(snapshot, cred, cfg)
            plan_repricing(snapshot, cred, cfg)
        return {"ok": ok_history, "sold": sold, "listings": len(snapshot), "error": hist_error}
    finally:
        try:
            if db_lock is not None:
                from sqlalchemy import text
                try:
                    db_lock.execute(text("SELECT pg_advisory_unlock(427184, 1)"))
                finally:
                    db_lock.close()
        finally:
            _sell_phase_lock.release()
            _cycle_lock.release()


def next_cycle_delay(policy, jobs, result, now=None):
    now = time.time() if now is None else now
    if not result.get("ok"):
        return 600 if "429" in (result.get("error") or "") else 120
    delay = policy.check_interval_seconds
    for job in jobs:
        if job.status in ("planned", "removing") and not policy.enabled:
            continue
        due = job.retry_at
        if job.status in ("planned", "removing", "ready"):
            due = max(due, policy.mutation_retry_at)
        delay = min(delay, max(60, due - now))
    return delay


def trading_worker():
    from app.database import get_session
    from app.state import log
    while not is_shutting_down():
        try:
            with get_session() as s:
                policy = s.get(TradingPolicy, 1) or TradingPolicy()
                due = not policy.next_check_at or policy.next_check_at <= time.time()
            if not due:
                if wait_for_shutdown(10):
                    return
                continue
            try:
                result = run_trading_cycle()
            except Exception as exc:
                if is_shutdown_error(exc):
                    return
                result = {"ok": False, "error": f"市场核实异常（{type(exc).__name__}）"}
            with get_session() as s:
                policy = s.get(TradingPolicy, 1) or TradingPolicy()
                active = s.exec(select(RepriceJob).where(RepriceJob.status.in_({"planned", "removing", "awaiting_inventory", "ready", "submitting", "confirming"}))).all()
                policy.last_check_at = time.time()
                policy.last_error = result.get("error") or ""
                policy.next_check_at = time.time() + next_cycle_delay(policy, active, result)
                s.add(policy)
                s.commit()
            if not result.get("ok"):
                log(result.get("error") or "市场核实失败", "warn", category="market_reconcile")
            elif result.get("sold"):
                log(f"已同步 {result['sold']} 条真实成交记录", "info", category="market_reconcile")
        except Exception as exc:
            if is_shutdown_error(exc):
                return
            # Includes database reads/scheduling writes: keep the worker alive after outages.
            log(f"市场核实暂不可用（{type(exc).__name__}），60 秒后重试", "warn", category="market_reconcile")
            if wait_for_shutdown(60):
                return
            continue
        if wait_for_shutdown(10):
            return
