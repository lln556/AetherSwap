"""
Background worker threads – extracted from api.py.
Contains holdings report, receive, listing check, exchange rate,
and account region sync workers.
"""
import json
import time
from pathlib import Path
from typing import Optional
from app.state import (
    get_status,
    get_purchases,
    get_sales,
    is_steam_background_allowed,
    log,
    set_buff_auth_expired,
    set_buff_verification_required,
    set_inventory,
    set_steam_auth_expired,
    update_purchase,
    update_purchase_by_id,
)
from app.config_loader import (
    get_buff_credentials,
    get_steam_credentials,
    load_app_config_validated,
)
from app.notify import send_pushplus, build_holdings_report_content, compute_holdings_stats
from app.inventory_cs2 import scan_cs2_inventory
from app.accounts import get_current_account, update_account
from app.services.lifecycle import is_shutting_down, is_shutdown_error, wait_for_shutdown
_HOLDINGS_REPORT_LAST_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "holdings_report_last.json"
_worker_alert_last: dict = {}  
_WORKER_ALERT_COOLDOWN = 3600  
def _worker_alert(worker_name: str, error: Exception) -> None:
    """发送 PushPlus 告警，每个 worker 每小时至多发一次。"""
    if is_shutdown_error(error):
        return
    now = time.time()
    last = _worker_alert_last.get(worker_name, 0.0)
    if now - last < _WORKER_ALERT_COOLDOWN:
        return
    try:
        cfg = load_app_config_validated()
        token = (cfg.get("notify") or {}).get("pushplus_token", "") or ""
        if not token:
            return
        msg = str(error)[:200] if error else "未知异常"
        if send_pushplus(token, f"[Worker异常] {worker_name}", f"后台任务 <b>{worker_name}</b> 发生异常，本轮已中止，60 秒后重试。<br>错误信息：{msg}"):
            _worker_alert_last[worker_name] = now
            log(f"[{worker_name}] 异常告警已发送", "info", category="alert")
    except Exception:
        pass
def _load_last_pl_pct() -> Optional[float]:
    try:
        if _HOLDINGS_REPORT_LAST_FILE.exists():
            with open(_HOLDINGS_REPORT_LAST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                v = data.get("pl_pct")
                return float(v) if v is not None else None
    except Exception:
        pass
    return None
def _save_last_pl_pct(pl_pct: float) -> None:
    try:
        _HOLDINGS_REPORT_LAST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_HOLDINGS_REPORT_LAST_FILE, "w", encoding="utf-8") as f:
            json.dump({"pl_pct": pl_pct}, f)
    except Exception:
        pass
def _enrich_purchases_with_current_prices(transactions: list) -> None:
    from app.shared_market import fill_current_market_prices
    fill_current_market_prices(transactions)
def run_holdings_report_once(force: bool = False) -> bool:
    if not force and not is_steam_background_allowed():
        return False
    cfg = load_app_config_validated()
    notify_cfg = cfg.get("notify") or {}
    token = (notify_cfg.get("pushplus_token") or "").strip()
    if not token:
        return False
    resell_ratio = float(cfg.get("pipeline", {}).get("resell_ratio", 0.85))
    if resell_ratio <= 0:
        resell_ratio = 0.85
    def _build_out():
        purchases = get_purchases()
        out = []
        for i, p in enumerate(purchases):
            row = {"type": "purchase", "idx": i, "name": p.get("name", ""), "price": float(p.get("price", 0)), "market_price": p.get("market_price"), "sale_price": p.get("sale_price")}
            if row["market_price"] is not None:
                row["market_price"] = round(float(row["market_price"]), 2)
            out.append(row)
        return out, purchases
    def _enriched_holdings(out):
        _enrich_purchases_with_current_prices(out)
        return [t for t in out if t.get("type") == "purchase" and (t.get("sale_price") is None or float(t.get("sale_price", 0) or 0) <= 0)]
    out, purchases = _build_out()
    holdings = [p for p in purchases if not (p.get("sale_price") is not None and float(p.get("sale_price", 0) or 0) > 0)]
    if not holdings:
        return False
    holdings_enriched = _enriched_holdings(out)
    if not holdings_enriched:
        return False
    all_have_price = all(t.get("current_market_price") is not None for t in holdings_enriched)
    if not all_have_price:
        return False
    _, total_mp, _, _, pl_pct, _ = compute_holdings_stats(holdings_enriched, resell_ratio)
    if not force:
        drop_threshold_pct = float(notify_cfg.get("holdings_report_change_threshold_pct", 20) or 20)
        last_pl_pct = _load_last_pl_pct()
        if last_pl_pct is None:
            if pl_pct is not None:
                _save_last_pl_pct(pl_pct)
            return False
        if pl_pct is None:
            return False
        drop = last_pl_pct - pl_pct  
        if drop < drop_threshold_pct:
            return False
    content = build_holdings_report_content(holdings_enriched, resell_ratio)
    ok = send_pushplus(token, "持有饰品紧急回报" if not force else "持有饰品定时回报", content)
    if ok and pl_pct is not None:
        _save_last_pl_pct(pl_pct)
    return ok
def holdings_report_worker() -> None:
    """定时回报 worker（holdings_report_interval_hours > 0 才运行）.
    定时回报不受跌幅限制，每隔设定小时强制推送一次。
    紧急回报另由 run_holdings_report_once(force=False) 负责（在有新市场价时自动触发）。
    """
    first_run = True
    while not is_shutting_down():
        try:
            cfg = load_app_config_validated()
            n = cfg.get("notify") or {}
            interval_h = int(n.get("holdings_report_interval_hours", 0) or 0)
            if interval_h <= 0:
                first_run = True
                if wait_for_shutdown(3600):
                    return
                continue
            if first_run:
                first_run = False
                if wait_for_shutdown(60):
                    return
            else:
                if wait_for_shutdown(interval_h * 3600):
                    return
            while not is_steam_background_allowed():
                if wait_for_shutdown(60):
                    return
            run_holdings_report_once(force=True)
        except Exception:
            if wait_for_shutdown(60):
                return
_EXCHANGE_RATE_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "exchange_rate.json"
def _fetch_exchange_rates(base: str = "CNY", targets: Optional[list] = None) -> Optional[dict]:
    try:
        from utils import http_client as http
        from utils.proxy_manager import get_proxy_manager, mask_proxy_url
        pm = get_proxy_manager()
        proxies = pm.get_proxies_for_request()  
        url = f"https://open.er-api.com/v6/latest/{base}"
        from app.state import log
        if proxies:
            log(f"exchange_rate: 正在使用代理 {mask_proxy_url(proxies.get('http'))} 访问: {url}", "debug", category="exchange_rate")
        r = http.get(url, timeout=10, proxies=proxies)
        data = r.json()
        if data.get("result") != "success":
            return None
        all_rates = data.get("rates") or {}
        if not targets:
            return {}  
        out = {}
        for code in targets:
            if code not in all_rates:
                continue
            rate_val = all_rates[code]
            if not rate_val:
                continue
            out[code] = 1.0 / float(rate_val)
        return out or None
    except Exception:
        return None
def _save_exchange_rates(rates: dict, base: str = "CNY") -> None:
    try:
        from datetime import datetime, timezone
        _EXCHANGE_RATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "base": base,
            "rates": rates,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        from utils.json_store import atomic_write_json
        atomic_write_json(_EXCHANGE_RATE_FILE, payload, indent=2)
    except Exception:
        return
def exchange_rate_worker() -> None:
    while not is_shutting_down():
        try:
            cfg = load_app_config_validated()
            sys_cfg = cfg.get("system") or {}
            interval_h = float(sys_cfg.get("exchange_rate_refresh_hours") or 0)
            if interval_h <= 0:
                log("exchange_rate: 已关闭, system.exchange_rate_refresh_hours<=0", "debug", category="exchange_rate")
                if wait_for_shutdown(3600):
                    return
                continue
            targets = [
                "USD", "INR", "RUB", "HKD", "EUR",
                "KZT", "UAH", "PKR", "TRY", "ARS", "AZN",
                "VND", "IDR", "BRL", "CLP", "JPY", "PHP",
            ]
            log(f"exchange_rate: 开始获取, 间隔={interval_h} 小时, 目标={','.join(targets)}", "debug", category="exchange_rate")
            rates = _fetch_exchange_rates("CNY", targets)
            if rates is not None:
                _save_exchange_rates(rates, "CNY")
                preview = ", ".join(f"{k}={v:.4f}" for k, v in rates.items())
                log(f"exchange_rate: 已更新 {len(rates)} 个币种: {preview}", "debug", category="exchange_rate")
            else:
                log("exchange_rate: 获取失败或无有效结果", "error", category="exchange_rate")
            if wait_for_shutdown(max(1, int(interval_h * 3600))):
                return
        except Exception as e:
            if is_shutdown_error(e):
                return
            log(f"exchange_rate: worker 异常 {type(e).__name__}: {e}, 5 分钟后重试", "error", category="exchange_rate")
            if wait_for_shutdown(300):
                return
_BUFF_RECONCILIATION_REQUIRED_STEPS = frozenset(
    {
        "BUFF_WRITE_RESULT_UNKNOWN",
        "BUFF_ORDER_CREATED_PENDING",
        "BUFF_COOLING_DOWN",
        "PIPELINE_UNEXPECTED_ERROR",
    }
)


def _buff_background_request_is_safe() -> bool:
    """Allow idle/waiting passes under the shared BUFF activity lock."""

    from app.pipeline import is_shutdown_pending
    from app.services.buff_checkout_guard import get_unresolved_checkout

    if is_shutdown_pending() or get_unresolved_checkout() is not None:
        return False
    status = get_status() or {}
    return (
        (status.get("status") != "running"
         or status.get("step") in {"WAITING_RETRY", "TIME_LIMIT_WAIT", "NETWORK_OFFLINE", "STEAM_COOLDOWN"})
        and status.get("step") != "CHECKOUT_PENDING"
        and status.get("step") not in _BUFF_RECONCILIATION_REQUIRED_STEPS
        and not status.get("buff_auth_expired")
        and not status.get("buff_verification_required")
    )


def run_receive_check_once(buff_client, *, refresh_inventory: bool = False) -> dict:
    from app.receive_flow import try_receive_once
    from app.services.receipt_reconciliation import reconcile_receipts_once
    from app.services.buff_auth import get_buff_auth_lock
    from app.services.buff_checkout_guard import buff_activity_guard
    inventory = None
    with get_buff_auth_lock(), buff_activity_guard():
        if not is_steam_background_allowed() or not _buff_background_request_is_safe():
            return {"ok": False, "error": "购买任务或订单门禁占用中，请等待购买轮次结束"}
        result = reconcile_receipts_once(buff_client)
        inventory = result.pop("inventory", None)
        n = try_receive_once(
            get_purchases, update_purchase, lambda: buff_client, get_steam_credentials,
            scan_inventory=scan_cs2_inventory, update_purchase_by_id=update_purchase_by_id,
        )
        result["received"] = result.get("received", 0) + n
        # Recheck cooldowns even after all receipts have been reconciled.
        # Without this pass, the first eligible listing needs a UI refresh.
        owned = any(p.get("assetid") and not p.get("listing") and not p.get("sale_price")
                    for p in get_purchases())
        if n or (refresh_inventory and inventory is None and owned):
            ok, items, error = scan_cs2_inventory()
            if ok:
                inventory = items
            elif error:
                result["inventory_error"] = error
        if inventory is not None:
            set_inventory(inventory)
        result["pending"] = sum(bool(p.get("pending_receipt")) for p in get_purchases())
        result["inventory_refreshed"] = inventory is not None
    if inventory is not None:
        from app.sell_pipeline import run_sell_phase_on_inventory_update
        run_sell_phase_on_inventory_update(inventory)
    return result


def receive_worker() -> None:
    from app.receive_flow import try_receive_once
    from app.services.buff_auth import get_buff_auth_lock
    from app.services.buff_client import create_buff_client_from_config
    from app.services.buff_checkout_guard import buff_activity_guard
    from buff import (
        BuffAuthExpired,
        BuffRateLimited,
        BuffRequestBlocked,
        BuffVerificationRequired,
    )

    buff_client = None
    last_inventory_check = 0.0
    try:
        while not is_shutting_down():
            try:
                cfg = load_app_config_validated()
                interval = max(10, int(cfg.get("pipeline", {}).get("receive_poll_interval_seconds", 30) or 30))
                if wait_for_shutdown(interval):
                    return
                if not is_steam_background_allowed() or not _buff_background_request_is_safe():
                    continue
                credentials = get_buff_credentials() or {}
                if not credentials.get("cookies"):
                    continue
                if buff_client is None:
                    buff_client = create_buff_client_from_config(credentials, cfg)
                refresh_interval = max(60, int(cfg.get("pipeline", {}).get("listing_check_interval_seconds", 600) or 600))
                result = run_receive_check_once(
                    buff_client, refresh_inventory=time.monotonic() - last_inventory_check >= refresh_interval,
                )
                if result.get("inventory_refreshed"):
                    last_inventory_check = time.monotonic()
                if result.get("received", 0) > 0:
                    log(f"receive_worker: 本轮确认到账 {result['received']} 件", "info", category="receive")
            except BuffRateLimited as e:
                log(f"receive_worker: Buff 请求处于限流冷却: {e}", "warn", category="receive")
                if wait_for_shutdown(min(60, max(1, int(e.retry_after)))):
                    return
            except BuffVerificationRequired as e:
                set_buff_verification_required(True, str(e))
                log(f"receive_worker: Buff 需要安全验证: {e}", "warn", category="receive")
                if wait_for_shutdown(60):
                    return
            except BuffAuthExpired:
                set_buff_auth_expired(True)
                log("receive_worker: Buff 登录已失效，暂停待收货查询", "warn", category="receive")
                if wait_for_shutdown(60):
                    return
            except BuffRequestBlocked as e:
                log(f"receive_worker: Buff 请求策略已阻止后台查询: {e}", "warn", category="receive")
                if wait_for_shutdown(60):
                    return
            except Exception as e:
                if is_shutdown_error(e):
                    return
                log(f"receive_worker 异常 {type(e).__name__}: {e}", "error", category="receive")
                _worker_alert("receive_worker", e)
                if wait_for_shutdown(60):
                    return

    finally:
        if buff_client is not None:
            buff_client.close()
def listing_check_worker() -> None:
    from app.steam_listings import fetch_my_history_sold
    from app.services.listing_confirmation import sync_listing_confirmations
    from app.pipeline_context import PipelineContext
    from app.state import get_state
    while not is_shutting_down():
        try:
            cfg = load_app_config_validated()
            interval = max(60, int(cfg.get("pipeline", {}).get("listing_check_interval_seconds", 600) or 600))
            if wait_for_shutdown(interval):
                return
            if not is_steam_background_allowed():
                continue
            purchases = get_purchases()
            listing_idx = [(i, p) for i, p in enumerate(purchases) if p.get("listing") and p.get("assetid")]
            if not listing_idx:
                continue
            tracked_listings = {(p.get("_db_id"), str(p["assetid"])) for _, p in listing_idx}
            cred = get_steam_credentials()
            cookies = cred.get("cookies") or ""
            if not cookies:
                continue
            pipeline_cfg = cfg.get("pipeline") or {}
            steam_debug = bool(pipeline_cfg.get("steam_listings_debug") or pipeline_cfg.get("verbose_debug"))
            debug_fn = (lambda m: log(m, "debug", category="steam")) if steam_debug else None
            ctx = PipelineContext(get_state(), "listing_check")
            ok, snapshot, err = sync_listing_confirmations(ctx, cfg, cred)
            if not ok:
                continue
            # Pending confirmations are still owned market requests, not sales.
            active_ids = set(snapshot)
            purchases = get_purchases()
            listing_idx = [(i, p) for i, p in enumerate(purchases)
                           if p.get("listing") and p.get("assetid")
                           and (p.get("_db_id"), str(p["assetid"])) in tracked_listings]
            not_in_active = [(i, p) for i, p in listing_idx if str(p.get("assetid") or "") and str(p.get("assetid") or "") not in active_ids]
            if steam_debug and not_in_active:
                log(f"[listing_check] 本地 {len(listing_idx)} 条在售, Steam 活跃 {len(active_ids)}, 可能已售 {len(not_in_active)} 条", "debug", category="steam")
            if not not_in_active:
                continue
            ok2, sold_map, _ = fetch_my_history_sold(cookies, debug_fn=debug_fn)
            seen_aids = set()
            sold_updates = 0
            for _i, p in not_in_active:
                aid = str(p.get("assetid") or "")
                if not aid or aid in seen_aids:
                    continue
                seen_aids.add(aid)
                db_id = p.get("_db_id")
                sale_price_rounded = round(sold_map[aid], 2) if (ok2 and aid in sold_map) else None
                if db_id:
                    if sale_price_rounded is not None:
                        sold_at = time.time()
                        update_purchase_by_id(db_id, {"sale_price": sale_price_rounded, "sold_at": sold_at, "listing": False, "listing_status": None})
                        sold_updates += 1
                    else:
                        update_purchase_by_id(db_id, {"listing": False, "listing_status": "error"})
                else:
                    current = get_purchases()
                    matched = [j for j, q in enumerate(current) if str(q.get("assetid") or "") == aid]
                    if sale_price_rounded is not None:
                        sold_at = time.time()
                        for idx in matched:
                            update_purchase(idx, {"sale_price": sale_price_rounded, "sold_at": sold_at, "listing": False, "listing_status": None})
                        if matched:
                            sold_updates += 1
                    else:
                        for idx in matched:
                            update_purchase(idx, {"listing": False, "listing_status": "error"})
            if sold_updates > 0:
                log(f"[listing_check] 确认售出 {sold_updates} 件，刷新库存并触发自动补挂", "info", category="steam")
                ok_inv, inv_items, inv_err = scan_cs2_inventory()
                if ok_inv:
                    set_inventory(inv_items)
                    from app.sell_pipeline import run_sell_phase_on_inventory_update
                    run_sell_phase_on_inventory_update(inv_items)
                else:
                    log(f"[listing_check] 售出后刷新库存失败，暂不补挂: {inv_err}", "warn", category="steam")
        except Exception as e:
            if is_shutdown_error(e):
                return
            log(f"listing_check_worker 异常 {type(e).__name__}: {e}", "error", category="steam")
            _worker_alert("listing_check_worker", e)
            if wait_for_shutdown(60):
                return
def _sync_account_profile_and_region(acc: dict) -> None:
    from app.services.steam_auth import fetch_steam_profile_via_api
    account_id = acc.get("id")
    cred = get_steam_credentials()
    cred_steam_id = (cred.get("steam_id") or "").strip()
    acc_steam_id = (acc.get("steam_id") or "").strip()
    cookies_str = cred.get("cookies") or ""
    if cred_steam_id and not acc_steam_id:
        update_account(account_id, steam_id=cred_steam_id)
        acc_steam_id = cred_steam_id
        log(f"account_sync: 从 credentials 同步 steam_id={cred_steam_id}", "info", category="account")
    steam_id = acc_steam_id or cred_steam_id
    if steam_id and cookies_str and (not acc.get("display_name") or not acc.get("avatar_url")):
        display_name, avatar_url = fetch_steam_profile_via_api(steam_id, cookies_str)
        if display_name or avatar_url:
            updates = {}
            if display_name and not acc.get("display_name"):
                updates["display_name"] = display_name
            if avatar_url and not acc.get("avatar_url"):
                updates["avatar_url"] = avatar_url
            if updates:
                update_account(account_id, **updates)
                log(f"account_sync: 已获取 Steam 资料 name={display_name or '(无)'} avatar={'(有)' if avatar_url else '(无)'}", "info", category="account")
        else:
            log("account_sync: 未能获取 Steam 资料（网络异常或 Cookie 失效）", "debug", category="account")

def sync_account_region_worker() -> None:
    if is_shutting_down():
        return
    try:
        acc = get_current_account()
        if not acc:
            log("account_region: 无当前账号，跳过同步", "debug", category="account")
            return
        log(
            f"account_region: 开始同步 account_id={acc.get('id')} username={acc.get('username') or ''} steam_id={acc.get('steam_id') or ''}",
            "debug",
            category="account",
        )
        _sync_account_profile_and_region(acc)
        
        from app.services.account_region import refresh_account_region_currency
        result = refresh_account_region_currency(acc.get("id"), skip_unconfigured=True)
        if not result.get("ok"):
            if result.get("skipped"):
                log(
                    f"account_region: {result.get('error') or '尚未完成 Steam 登录配置'}",
                    "debug",
                    category="account",
                )
                return
            log(
                f"account_region: 同步失败，已暂停自动出售安全许可: {result.get('error') or '未知原因'}",
                "error",
                category="account",
            )
            return
        log(
            f"account_region: 同步完成 account_id={acc.get('id')} "
            f"币种={result.get('currency_code')} 派生地区={result.get('region_code')}",
            "debug",
            category="account",
        )
    except Exception as e:
        if is_shutdown_error(e):
            return
        log(f"account_region: 同步异常 {str(e)[:120]}", "error", category="account")
        return
def _session_keepalive_enabled(cfg: dict) -> bool:
    return bool((cfg.get("system") or {}).get("buff_session_keepalive_enabled", False))


def _session_keepalive_is_safe() -> bool:
    """Never open an auth browser while a pipeline or checkout is active."""

    from app.pipeline import is_shutdown_pending
    from app.services.buff_checkout_guard import get_unresolved_checkout

    if is_shutdown_pending() or get_unresolved_checkout() is not None:
        return False
    status = get_status() or {}
    return (
        status.get("status") != "running"
        and status.get("step") != "CHECKOUT_PENDING"
        and status.get("step") not in _BUFF_RECONCILIATION_REQUIRED_STEPS
    )


def session_keepalive_worker() -> None:
    from app.services.buff_auth import try_buff_auto_relogin

    while not is_shutting_down():
        try:
            cfg = load_app_config_validated()
            if not _session_keepalive_enabled(cfg):
                # Poll the opt-in switch without touching either remote service.
                if wait_for_shutdown(60):
                    return
                continue

            sys_cfg = cfg.get("system") or {}
            interval_h = float(sys_cfg.get("session_keepalive_hours", 4.0))
            if interval_h <= 0:
                log(
                    "keepalive: 已开启但 system.session_keepalive_hours<=0，等待配置修正",
                    "debug",
                    category="keepalive",
                )
                if wait_for_shutdown(60):
                    return
                continue

            # The first request waits the complete configured interval; startup
            # no longer forces a browser visit after five minutes.
            if wait_for_shutdown(interval_h * 3600):
                return

            cfg = load_app_config_validated()
            if not _session_keepalive_enabled(cfg):
                continue
            while not _session_keepalive_is_safe():
                log("keepalive: 流水线或结账正在进行，延后会话保活", "debug", category="keepalive")
                if wait_for_shutdown(60):
                    return
                cfg = load_app_config_validated()
                if not _session_keepalive_enabled(cfg):
                    break
            if not _session_keepalive_enabled(cfg):
                continue

            # Re-check immediately before launching the browser to close the
            # race between the wait loop and a newly started pipeline.
            if not _session_keepalive_is_safe():
                continue
            log("keepalive: 开始本轮 Buff 后台会话保活...", "info", category="keepalive")
            buff_ok, _buff_status, buff_msg = try_buff_auto_relogin()
            if not buff_ok:
                log(f"keepalive: Buff 保活未完成: {buff_msg}", "warn", category="keepalive")
            else:
                log(f"keepalive: Buff 保活成功: {buff_msg}", "info", category="keepalive")
            log("keepalive: 本轮 Buff 后台会话保活已完成", "info", category="keepalive")
        except Exception as e:
            if is_shutdown_error(e):
                return
            log(f"keepalive: worker 异常 {e}, 15 分钟后重试", "error", category="keepalive")
            if wait_for_shutdown(900):
                return


def steam_session_worker() -> None:
    """Keep the Steam web session fresh so market requests never lapse silently.

    The stored ``steamLoginSecure`` token is short-lived (~24h) and ordinary
    market requests do not renew it, so once it expires every history request is
    rejected and buying stops without any visible error.  This worker reads the
    token expiry and re-runs the password login shortly before it lapses (and
    heals an already-expired session).  Failures are surfaced through
    ``steam_auth_expired`` so the UI can ask for manual intervention.
    """
    from app.services.steam_auth import steam_cookie_expiry, try_steam_auto_relogin, _verify_steam_cookies_valid

    refresh_window_seconds = 3600.0
    failure_backoff_seconds = 7200.0
    while not is_shutting_down():
        try:
            cfg = load_app_config_validated()
            minutes = float((cfg.get("system") or {}).get("steam_session_keepalive_minutes", 30) or 0)
            if minutes <= 0:
                if wait_for_shutdown(300):
                    return
                continue
            interval = max(60.0, minutes * 60.0)
            acc = get_current_account()
            cookies = (get_steam_credentials() or {}).get("cookies") or ""
            if not acc or not cookies.strip():
                set_steam_auth_expired(False)
                if wait_for_shutdown(interval):
                    return
                continue
            exp = steam_cookie_expiry(cookies)
            now = time.time()
            if exp is not None and exp - now > refresh_window_seconds:
                set_steam_auth_expired(False)
                if wait_for_shutdown(interval):
                    return
                continue
            force = exp is not None
            if force and exp > now:
                log(
                    f"steam_keepalive: 登录令牌约 {max(1.0, (exp - now) / 60):.0f} 分钟后到期，提前续期",
                    "info",
                    category="steam",
                )
            ok, status, msg = try_steam_auto_relogin(force=force)
            if ok:
                set_steam_auth_expired(False)
                if status != "session_valid":
                    log(f"steam_keepalive: Steam 会话已刷新（{msg}）", "info", category="steam")
                if wait_for_shutdown(interval):
                    return
                continue
            # A failed renewal says nothing about the existing web session.
            current = get_current_account()
            if not current or current.get("id") != acc.get("id"):
                continue
            current_cookies = (get_steam_credentials() or {}).get("cookies") or ""
            valid = _verify_steam_cookies_valid(current_cookies, current.get("steam_id") or "")
            if get_current_account() != current or (get_steam_credentials() or {}).get("cookies", "") != current_cookies:
                continue
            if valid is not None:
                set_steam_auth_expired(not valid)
            if valid is False:
                detail = "接口已确认当前登录失效"
                delay = failure_backoff_seconds
            elif valid is True:
                detail = "当前登录仍有效，稍后重试续期"
                remaining = (steam_cookie_expiry(current_cookies) or (time.time() + interval)) - time.time()
                delay = max(60.0, min(interval, remaining / 2))
            else:
                detail = "暂无法确认当前登录状态，保留原状态"
                delay = interval
            log(f"steam_keepalive: 自动重新登录失败（{status}）：{msg}；{detail}", "warn", category="steam")
            if wait_for_shutdown(delay):
                return
        except Exception as e:
            if is_shutdown_error(e):
                return
            log(f"steam_keepalive: worker 异常 {e}，15 分钟后重试", "error", category="steam")
            if wait_for_shutdown(900):
                return
