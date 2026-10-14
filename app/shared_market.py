"""共享 Steam 市场价查询工具.
统一的批量价格查询逻辑，供库存管理和持有饥品两个模块共用，
避免同一物品被重复查询两次。
"""
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Dict, Optional, Set
from app.config_loader import get_steam_credentials, load_app_config_validated
_BATCH_MAX_WORKERS = 4
def get_steam_smart_price_cny(session, market_hash_name: str, app_id: int = 730) -> Optional[float]:
    """获取单个物品的 Steam 市场智能报价（CNY）.
    同时被 inventory.py 和 transactions.py 使用的核心函数。
    """
    from steam.market_orders import get_sell_orders_cny, compute_smart_list_price
    cfg = load_app_config_validated().get("pipeline", {})
    wall_volume = int(cfg.get("sell_price_wall_volume", 20))
    max_ignore = int(cfg.get("sell_price_max_ignore_volume", 4))
    result = get_sell_orders_cny(session, market_hash_name, app_id=app_id, request_delay=1.0)
    if not result or not result.get("sell_orders"):
        return None
    price, _ = compute_smart_list_price(
        result["sell_orders"],
        wall_volume_threshold=wall_volume,
        max_ignore_volume=max_ignore,
        min_step=0,
        offset=0,
    )
    return price
def current_price_max_age_seconds() -> float:
    """Cache freshness window for enrichment callers, from app config."""
    try:
        cfg = (load_app_config_validated().get("pipeline") or {})
        minutes = float(cfg.get("current_price_refresh_minutes") or 10)
    except Exception:
        minutes = 10
    return max(60.0, minutes * 60)
def fill_current_market_prices(transactions: list, max_age: Optional[float] = None) -> None:
    """Fill current_market_price on unsold purchase rows in place."""
    purchases = [t for t in transactions or [] if t.get("type") == "purchase"]
    if not purchases:
        return
    names: Set[str] = set()
    for t in purchases:
        if t.get("sale_price") is not None:
            continue
        name = (t.get("name") or "").strip()
        if name:
            names.add(name)
    if not names:
        return
    if max_age is None:
        max_age = current_price_max_age_seconds()
    prices = batch_fetch_prices(names, max_age=max_age)
    for t in purchases:
        if t.get("sale_price") is not None:
            continue
        name = (t.get("name") or "").strip()
        if name in prices:
            t["current_market_price"] = prices[name]
_inflight_lock = threading.Lock()
_inflight_fetches: Dict[tuple, Future] = {}
def _fetch_one_deduped(name: str, app_id: int, cookies: str, steam_id: str) -> tuple:
    """Fetch one price, sharing the in-flight request across concurrent callers."""
    key = (app_id, name)
    with _inflight_lock:
        future = _inflight_fetches.get(key)
        if future is None:
            future = Future()
            _inflight_fetches[key] = future
            creator = True
        else:
            creator = False
    if not creator:
        try:
            return future.result()
        except Exception:
            return name, None
    from steam.session import create_market_session
    try:
        try:
            session = create_market_session(cookies, steam_id)
            price = get_steam_smart_price_cny(session, name, app_id=app_id)
            value = (name, price)
        except Exception:
            value = (name, None)
    finally:
        with _inflight_lock:
            _inflight_fetches.pop(key, None)
        future.set_result(value)
    return value
def batch_fetch_prices(names: Set[str], app_id: int = 730, max_age: Optional[float] = None) -> Dict[str, float]:
    """批量查询一组物品名称的市场价，返回 {name: price_cny}.
    - max_age 给定时先复用持久缓存，只对过期/缺失的名称发起实时请求
    - 相同名称的并发批次共享同一在途请求（single-flight）
    - 返回字典中只包含成功取到价格的条目（含缓存命中）
    """
    if not names:
        return {}
    cred = get_steam_credentials()
    cookies = cred.get("cookies", "")
    steam_id = cred.get("steam_id", "")
    if not cookies or not steam_id:
        return {}
    wanted = {n for n in names if n}
    cached: Dict[str, float] = {}
    if max_age is not None:
        from app.services.market_price_cache import get_cached_market_prices
        cached, _ = get_cached_market_prices(wanted, app_id=app_id, max_age=max_age)
    missing = [n for n in wanted if n not in cached]
    if not missing:
        return dict(cached)
    prices: Dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=_BATCH_MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_one_deduped, name, app_id, cookies, steam_id): name
                   for name in missing}
        for future in as_completed(futures):
            try:
                name, price = future.result()
                if price is not None and price > 0:
                    prices[name] = round(float(price), 2)
            except Exception:
                pass
    from app.services.market_price_cache import remember_market_prices
    remember_market_prices(prices, steam_id, app_id)
    return {**cached, **prices}
