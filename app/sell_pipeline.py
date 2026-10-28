import json
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Optional

from app.accounts import get_current_account
from app.config_loader import get_steam_credentials, load_app_config_validated
from app.config_schema import DEFAULTS, merge
from app.decision_sell import _evaluate as evaluate_sell_decision
from app.inventory_cs2 import scan_cs2_inventory
from app.pipeline_context import PipelineContext
from app.services.account_region import refresh_account_region_currency
from app.strategy_engine import apply_strategy_to_config, evaluate_strategy_runtime_modules
from app.state import get_state, append_sale
from app.services.listing_confirmation import sync_listing_confirmations
from app.steam_listings import fetch_my_listings
from steam.market import list_item, parse_sell_response
from steam.market_orders import compute_smart_list_price, get_sell_orders_cny
from steam.request_policy import MarketCooldown
from steam.session import create_market_session
from utils.delay import jittered_sleep
from utils.money import USD_TO_CNY_DEFAULT, list_price_display_to_cents
from utils.time import parse_steam_history_date
from utils.trend import calculate_trend_robust

_sell_phase_lock = threading.Lock()
_listing_cooldown = MarketCooldown()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _steam_latest_price_and_trend(
    market_hash_name: str, trend_days: int = 7, *, with_points: bool = False
):
    """Return the formal trend, optionally with dated points for decision evidence.

    The default three-value contract remains for existing callers. With
    ``with_points=True`` the third value is a snapshot containing every parsed
    observation, its source currency, and volumes, so preview can recompute the
    trend for any temporary window without losing the original samples.
    """
    from app.services.steam_client import SteamClient
    from utils.money import apply_currency, load_exchange_rates
    client = SteamClient()
    raw = client.fetch_history(market_hash_name, app_id=730, return_currency=True)
    if not raw or not isinstance(raw, dict):
        return None, None, None
    history = raw.get("history")
    currency = raw.get("currency")
    if not history:
        return None, None, None
    parsed = []
    for entry in history:
        if len(entry) < 2:
            continue
        dt = parse_steam_history_date(str(entry[0]))
        if dt is None:
            continue
        try:
            p = float(entry[1])
        except (ValueError, TypeError):
            continue
        try:
            volume = int(entry[2]) if len(entry) >= 3 else 0
        except (TypeError, ValueError, OverflowError):
            volume = 0
        parsed.append((dt, p, max(0, volume)))
    if not parsed:
        return None, None, None
    prices = [p for (_, p, _) in parsed]
    source_currency = str(currency or "CNY").strip().upper()
    currency_source = "history" if currency else "default"
    rate_map = load_exchange_rates()
    prices_cny, _ = apply_currency(
        prices, source_currency, USD_TO_CNY_DEFAULT, rate_map=rate_map
    )
    if not prices_cny:
        return None, None, None
    if source_currency in {"CNY", "RMB"}:
        cny_per_currency = 1.0
        exchange_rate_source = "identity"
    elif source_currency == "USD" and not rate_map.get("USD"):
        cny_per_currency = float(USD_TO_CNY_DEFAULT)
        exchange_rate_source = "usd_default"
    else:
        cny_per_currency = rate_map.get(source_currency)
        exchange_rate_source = "exchange_rate_file" if cny_per_currency else "unavailable"
    parsed_prices = [
        (dt, price, volume)
        for (dt, _, volume), price in zip(parsed, prices_cny)
    ]
    parsed_prices.sort(key=lambda x: x[0])
    latest_price = parsed_prices[-1][1]
    newest_dt = parsed_prices[-1][0]
    cutoff = newest_dt - timedelta(days=trend_days)
    in_range = [(dt, p, volume) for dt, p, volume in parsed_prices if dt >= cutoff]
    prices_in_range = [p for (_, p, _) in in_range]
    trend = calculate_trend_robust(prices_in_range, use_dynamic_sensitivity=True) if len(prices_in_range) >= 3 else 0
    if with_points:
        snapshot = {
            "source": "steam_history",
            "available": True,
            "currency": source_currency,
            "source_currency": source_currency,
            "currency_source": currency_source,
            "cny_per_currency": cny_per_currency,
            "exchange_rate_source": exchange_rate_source,
            "points": [
                {
                    "captured_at": dt.timestamp(),
                    "price": round(float(source_price), 6),
                    "volume": volume,
                }
                for dt, source_price, volume in parsed
            ],
            "window_days": trend_days,
        }
        return latest_price, trend, snapshot
    return latest_price, trend, prices_in_range


def _decision_config(cfg: dict) -> dict:
    """Keep the strategy snapshot contract without persisting credentials."""
    allowed = ("_strategy_runtime", "_decision_account_id", "pipeline", "strategies")
    return {key: cfg[key] for key in allowed if key in cfg}


def _fallback_sell_strategy(sell_strategy: int, pipeline_cfg: dict) -> dict:
    """Hydrated strategy approximation for callers without a runtime snapshot."""
    steps = [
        {"module_id": "sell.sellable_inventory_filter", "enabled": True, "params": {}},
        {"module_id": "guard.max_listings_per_item", "enabled": True, "params": {
            "max_listings_per_item": pipeline_cfg.get("max_listings_per_item", 5),
        }},
        {"module_id": "pricing.steam_wall_price", "enabled": True, "params": {
            "sell_price_wall_volume": pipeline_cfg.get("sell_price_wall_volume", 20),
            "sell_price_max_ignore_volume": pipeline_cfg.get("sell_price_max_ignore_volume", 4),
        }},
        {"module_id": "pricing.price_offset", "enabled": True, "params": {
            "sell_price_offset": pipeline_cfg.get("sell_price_offset", 0),
        }},
    ]
    if sell_strategy in (2, 3):
        steps.append({"module_id": "guard.rising_trend_wait", "enabled": True, "params": {
            "sell_trend_days": pipeline_cfg.get("sell_trend_days", 7),
        }})
    if sell_strategy == 3:
        steps.append({"module_id": "guard.profit_ratio", "enabled": True, "params": {
            "profit_ratio_multiplier": pipeline_cfg.get("profit_ratio_multiplier", 1.05),
        }})
    if sell_strategy == 4:
        steps.append({"module_id": "action.pause_auto_sell", "enabled": True, "params": {}})
    else:
        steps.append({"module_id": "action.steam_list", "enabled": True, "params": {}})
    return {
        "id": f"legacy.sell.{sell_strategy}",
        "strategy_type": "sell",
        "pipeline": dict(pipeline_cfg),
        "steps": steps,
    }


def _sell_strategy_snapshot(cfg: dict, sell_strategy: int, pipeline_cfg: dict):
    safe_cfg = _decision_config(cfg)
    try:
        from app.decision_records import snapshot_strategy
        strategy, modules = snapshot_strategy(safe_cfg, "sell")
        return safe_cfg, strategy, modules or {}
    except Exception:
        # Evidence capture must never change the listing decision.
        return safe_cfg, _fallback_sell_strategy(sell_strategy, pipeline_cfg), {}


def _capture_sell_decision(cfg: dict, item: dict, inputs: dict, result: dict,
                           evaluated_at: float, ctx=None) -> None:
    try:
        from app.decision_records import capture_decision
        capture_decision(cfg, "sell", item, inputs, result, evaluated_at=evaluated_at)
    except Exception as exc:
        # Recording is advisory and cannot authorize or abort a transaction.
        if ctx is not None:
            ctx.log(
                f"[出售] 检查记录写入异常，不影响正式判断（{type(exc).__name__}）",
                "warn", category="steam",
            )
        return


def _listing_inputs(*, item: dict, inventory: dict, ownership: dict, listing: dict,
                    market: dict, trend: dict, currency: dict) -> dict:
    return {
        "item": item,
        "inventory": inventory,
        "ownership": ownership,
        "listing": listing,
        "market": market,
        "trend": trend,
        "currency": currency,
    }


def _blocking_step(result: dict) -> dict:
    """Return the first step that blocked the decision, ignoring trailing not_run steps."""
    for step in result.get("steps") or []:
        if step.get("status") in {"reject", "wait", "unavailable", "unsupported"}:
            return step
    return {}


def _evaluate_listing(inputs: dict, strategy: dict, evaluated_at: float, modules: dict) -> dict:
    """Evaluate through the shared rules while preserving the live pricing seam."""
    return evaluate_sell_decision(
        inputs,
        strategy,
        evaluated_at,
        modules=modules,
        pricing_fn=compute_smart_list_price,
    )


def _evaluate_listing_safe(inputs: dict, strategy: dict, evaluated_at: float,
                           modules: dict) -> dict:
    """Fail closed if the shared evaluator cannot complete.

    A shared-rule exception is decision evidence, not a reason to bypass the
    strategy. Keep the complete configured step order so the preview can show
    the failed check followed by explicit ``not_run`` steps.
    """
    try:
        return _evaluate_listing(inputs, strategy, evaluated_at, modules)
    except Exception as exc:
        runtime = ((strategy or {}).get("_strategy_runtime") or {}).get("sell") or {}
        definitions = runtime.get("steps") or (strategy or {}).get("steps") or []
        steps = []
        failed = False
        for definition in definitions:
            if not isinstance(definition, dict):
                continue
            module_id = definition.get("module_id") or "unknown"
            if definition.get("enabled") is False:
                steps.append({
                    "module_id": module_id,
                    "status": "disabled",
                    "reason": "模块未启用，本次未执行",
                })
            elif not failed:
                failed = True
                steps.append({
                    "module_id": module_id,
                    "status": "unavailable",
                    "reason": f"共享规则执行异常，无法判断（{type(exc).__name__}）",
                })
            else:
                steps.append({
                    "module_id": module_id,
                    "status": "not_run",
                    "reason": "前置检查已阻断，未执行（阻断步骤：共享规则执行异常）",
                })
        if not steps:
            steps = [{
                "module_id": "sell.shared_rule_evaluator",
                "status": "unavailable",
                "reason": f"共享规则执行异常，无法判断（{type(exc).__name__}）",
            }]
        return {"status": "unavailable", "steps": steps}


def _load_rate_map() -> dict:
    """Load exchange rate JSON from config dir. Returns an empty dict on any failure."""
    try:
        fx_file = Path(__file__).resolve().parent.parent / "config" / "exchange_rate.json"
        if fx_file.exists():
            with open(fx_file, "r", encoding="utf-8") as f:
                fx = json.load(f)
            if isinstance(fx, dict) and isinstance(fx.get("rates"), dict):
                return {k: float(v) for k, v in fx["rates"].items() if isinstance(v, (int, float))}
    except Exception:
        pass
    return {}


def _record_listing_success(ctx, aid: str, name: str, list_price: float, listing_delay: float) -> None:
    """Append sale record, mark purchase as listed, and sleep the listing delay."""
    append_sale({"name": name, "goods_id": 0, "price": list_price, "at": time.time(), "assetid": aid or ""})
    if aid:
        purchases = ctx.state.get_purchases()
        for i, p in enumerate(purchases):
            if str(p.get("assetid") or "") == aid:
                db_id = p.get("_db_id")
                if db_id:
                    ctx.state.update_purchase_by_id(db_id, {"listing": True, "listing_status": "submitted"})
                else:
                    ctx.state.update_purchase(i, {"listing": True, "listing_status": "submitted"})
                break
    jittered_sleep(listing_delay)


# ---------------------------------------------------------------------------
# Phase sub-functions
# ---------------------------------------------------------------------------

def _resolve_steam_session(ctx: PipelineContext, cred_steam: dict):
    """Validate credentials and create a Steam market session.

    Returns ``(session, session_id_effective)`` or ``None`` if setup fails.
    """
    steam_id = cred_steam.get("steam_id")
    session_id = cred_steam.get("session_id")
    cookies = cred_steam.get("cookies")
    if not steam_id or not session_id or not cookies:
        ctx.log("未配置 Steam steam_id / session_id / cookies，跳过出售阶段", "warn", category="steam")
        return None
    session = create_market_session(cookies, steam_id)
    session_id_effective = session.cookies.get("sessionid") or session_id
    if not session_id_effective:
        session.close()
        ctx.log("Cookie 中无 sessionid，无法上架", "warn", category="steam")
        return None
    return session, session_id_effective


def _get_inventory(ctx: PipelineContext, items: Optional[list]) -> Optional[list]:
    """Return the inventory item list, scanning from Steam if *items* is ``None``.

    Attempts auto-relogin once on auth expiry.  Returns ``None`` when the
    inventory cannot be obtained and the sell phase should abort.
    """
    if items is not None:
        return items
    ok, items, err = scan_cs2_inventory()
    if not ok and err and ("登录已过期" in err or "重新登录" in err):
        ctx.log("Steam 登录已过期，尝试自动重新登录…", "warn", category="steam")
        try:
            from app.services.steam_auth import try_steam_auto_relogin
            relogin_ok, _, relogin_msg = try_steam_auto_relogin()
            if relogin_ok:
                ctx.log(f"自动重新登录成功: {relogin_msg}，重新获取库存", "info", category="steam")
                jittered_sleep(2, jitter_ratio=0.2)
                ok, items, err = scan_cs2_inventory()
            else:
                ctx.log(f"自动重新登录失败: {relogin_msg}", "warn", category="steam")
        except Exception as e:
            ctx.log(f"自动重新登录异常: {type(e).__name__} - {e}", "error", category="steam")
    if not ok:
        ctx.log(f"获取库存失败: {err}，跳过", "warn", category="steam")
        return None
    return items


def _build_listing_plan(
    ctx: PipelineContext,
    cfg: dict,
    session,
    sellable: list,
    sell_strategy: int,
    pipeline_cfg: dict,
    purchases_snapshot: list,
    ok_listings: bool,
    active_listing_ids: set,
    listing_assetid_to_name: dict,
    assetid_to_name_map: dict,
    account_currency: str,
    rate_map: dict,
    *,
    capture_decision: bool = True,
    decision_sink: list | None = None,
) -> list:
    """Build live listing decisions through the same pure evaluator used by preview."""
    account_currency = str(account_currency or "").strip().upper()
    if account_currency != "CNY":
        try:
            safe_rate = float(rate_map.get(account_currency))
        except (TypeError, ValueError, AttributeError):
            safe_rate = 0.0
        if safe_rate <= 0:
            ctx.log(
                f"[出售] 账号币种={account_currency or '未知'}，但汇率文件缺少该币种数据，"
                "无法安全定价，终止本次出售以防价格错误（可等汇率更新后重试）",
                "error", category="steam",
            )
            return []

    to_list_by_name: dict = defaultdict(int)
    skip_same_name_cap: dict = defaultdict(int)
    to_list = []
    seen_assetids: set = set()
    evaluated_at = time.time()
    decision_cfg, strategy, modules = _sell_strategy_snapshot(cfg, sell_strategy, pipeline_cfg)
    explicit_account_id = decision_cfg.get("_decision_account_id")
    listing_name_counts = Counter(
        (listing_assetid_to_name.get(lid) or "").strip() for lid in active_listing_ids
    )
    listing_name_map_counts = Counter(
        (assetid_to_name_map.get(lid) or "").strip() for lid in active_listing_ids
    )
    held_listing_name_counts = Counter(
        (p.get("market_hash_name") or p.get("name") or "").strip()
        for p in purchases_snapshot
        if p.get("listing")
    )

    for it in sellable:
        if ctx.is_stop_requested():
            ctx.set_status("stopped", "已停止")
            return to_list

        name = it.get("name") or ""
        market_hash_name = (it.get("market_hash_name") or name).strip()
        aid = str(it.get("assetid", "")).strip()

        if aid in seen_assetids:
            continue
        seen_assetids.add(aid)
        if not market_hash_name:
            ctx.log(f"[出售] 跳过无名物品 assetid={aid}", "info", category="steam")
            continue

        buy_record = _find_buy_record(purchases_snapshot, aid, market_hash_name)
        if ok_listings and listing_assetid_to_name:
            steam_same_name = listing_name_counts.get(market_hash_name, 0)
        elif ok_listings:
            steam_same_name = listing_name_map_counts.get(market_hash_name, 0)
        else:
            steam_same_name = held_listing_name_counts.get(market_hash_name, 0)
        already_in_this_round = to_list_by_name.get(market_hash_name, 0)

        # Bind before this item's first network check. A batch can span an
        # account switch, so an old item's market data must never be recorded
        # against whichever account happens to be current at capture time.
        decision_cfg["_decision_account_id"] = explicit_account_id
        if not decision_cfg["_decision_account_id"]:
            account = get_current_account() or {}
            if account.get("id") is not None:
                decision_cfg["_decision_account_id"] = str(account["id"])

        safe_item = {
            "name": name,
            "market_hash_name": market_hash_name,
            "goods_id": it.get("goods_id", buy_record.get("goods_id") if buy_record else None),
            "assetid": aid,
        }
        inputs = _listing_inputs(
            item=safe_item,
            inventory={"can_sell": it.get("can_sell") is True, "appid": int(it.get("appid", 730) or 730)},
            ownership={
                "eligible": buy_record is not None,
                "purchase_id": buy_record.get("_db_id") if buy_record else None,
                "purchase_price_cny": buy_record.get("price") if buy_record else None,
                "market_price_cny_at_purchase": buy_record.get("market_price") if buy_record else None,
            },
            listing={
                "same_name_count": steam_same_name,
                "round_same_name_count": already_in_this_round,
                "source": "steam" if ok_listings else "purchase_records",
            },
            market={
                "source": "steam_market",
                "captured_at": evaluated_at,
                "currency": "CNY",
                "available": False,
                "sell_orders": [],
                "error": None,
            },
            trend={
                "source": "steam_history",
                "captured_at": evaluated_at,
                "window_days": int(pipeline_cfg.get("sell_trend_days", 7) or 7),
                "available": False,
                "change": None,
                "points": [],
            },
            currency={
                "code": account_currency,
                "account_per_cny": 1.0 if account_currency == "CNY" else rate_map.get(account_currency),
            },
        )

        result = _evaluate_listing_safe(inputs, strategy, evaluated_at, modules)

        # Fetch only after the shared rules have reached the pricing stage. This
        # preserves ownership and same-name early stops without extra requests.
        blocker = _blocking_step(result)
        if result.get("status") == "unavailable" and str(blocker.get("module_id") or "").startswith("pricing.steam_wall"):
            try:
                orders_result = get_sell_orders_cny(
                    session,
                    market_hash_name,
                    app_id=int(it.get("appid", 730)),
                    return_error=True,
                )
                if isinstance(orders_result, tuple) and len(orders_result) == 2:
                    orders_data, orders_error = orders_result
                else:
                    orders_data, orders_error = orders_result, None
            except Exception as e:
                orders_data, orders_error = None, f"请求异常（{type(e).__name__}）"
                ctx.log(f"[出售] {name} assetid={aid} 拉取卖单异常: {type(e).__name__} - {e}", "error", category="steam")

            orders = (orders_data or {}).get("sell_orders") if isinstance(orders_data, dict) else None
            inputs["market"] = {
                "source": "steam_market",
                "captured_at": evaluated_at,
                "currency": "CNY",
                "available": bool(orders),
                "sell_orders": orders if isinstance(orders, list) else [],
                "error": orders_error if orders_data else (orders_error or "Steam 未返回卖单"),
            }
            result = _evaluate_listing_safe(inputs, strategy, evaluated_at, modules)

        # Trend is likewise fetched only when the original strategy would reach
        # that guard. Strategy 1 must never issue a history request here.
        blocker = _blocking_step(result)
        if result.get("status") == "unavailable" and blocker.get("module_id") == "guard.rising_trend_wait":
            trend_days = int(pipeline_cfg.get("sell_trend_days", 7) or 7)
            _, trend_change, trend_snapshot = _steam_latest_price_and_trend(
                market_hash_name, trend_days=trend_days, with_points=True
            )
            if isinstance(trend_snapshot, dict):
                points = trend_snapshot.get("points") or []
                trend_inputs = {
                    "source": "steam_history",
                    "captured_at": evaluated_at,
                    "window_days": trend_days,
                    "available": bool(points),
                    "currency": trend_snapshot.get("currency"),
                    "source_currency": trend_snapshot.get("source_currency"),
                    "points": points,
                }
                for key in (
                    "currency_source",
                    "cny_per_currency",
                    "exchange_rate_source",
                ):
                    if key in trend_snapshot:
                        trend_inputs[key] = trend_snapshot[key]
                inputs["trend"] = trend_inputs
            else:
                # Compatibility with callers/tests that still provide the old
                # (latest_price, trend, prices_in_window) tuple. Dated points are
                # unavailable here, so evaluator may use the computed trend.
                points = list(trend_snapshot or [])
                inputs["trend"] = {
                    "source": "steam_history",
                    "captured_at": evaluated_at,
                    "window_days": trend_days,
                    "available": trend_change is not None,
                    "change": trend_change,
                    "points": [{"price": price} for price in points],
                }
            result = _evaluate_listing_safe(inputs, strategy, evaluated_at, modules)

        # Re-evaluate the blocker from the final pass. Earlier passes may have
        # stopped at a missing market/history placeholder that was then filled.
        blocker = _blocking_step(result)
        if capture_decision:
            _capture_sell_decision(
                decision_cfg, safe_item, inputs, result, evaluated_at, ctx=ctx
            )
        # Consumers such as repricing need the shared explanation for every
        # final status, not only when a listing plan was produced.
        if decision_sink is not None:
            decision_sink.append(result)

        if result.get("status") != "pass":
            last_id = blocker.get("module_id", "")
            if last_id == "guard.max_listings_per_item":
                skip_same_name_cap[market_hash_name] += 1
            reason = blocker.get("reason") or "无法完成上架判断"
            level = "warn" if result.get("status") == "unavailable" else "info"
            ctx.log(f"[出售] {name} assetid={aid} 跳过：{reason}", level, category="steam")
            continue

        pricing_step = next(
            (s for s in result.get("steps") or [] if str(s.get("module_id", "")).startswith("pricing.steam_wall")),
            {},
        )
        action_step = next(
            (s for s in reversed(result.get("steps") or []) if s.get("module_id") == "action.steam_list"),
            {},
        )
        action_output = action_step.get("output") or {}
        list_price = action_output.get("list_price", (pricing_step.get("output") or {}).get("list_price"))
        display_price = action_output.get("display_price", list_price)
        reason = (pricing_step.get("output") or {}).get("reason") or pricing_step.get("reason") or ""
        try:
            list_price = round(float(list_price), 2)
            display_price = round(float(display_price), 2)
        except (TypeError, ValueError):
            ctx.log(f"[出售] {name} assetid={aid} 规则未给出有效建议价，跳过", "warn", category="steam")
            continue

        price_cents = list_price_display_to_cents(display_price, account_currency)
        to_list_by_name[market_hash_name] += 1
        n_this_name = to_list_by_name[market_hash_name]
        ctx.log(
            f"[出售] 列入待上架 assetid={aid} {name} 价格={list_price:.2f}"
            f"（该同名 Steam 在售 {steam_same_name}，本轮回第 {n_this_name} 件，"
            f"上限 {int(pipeline_cfg.get('max_listings_per_item', 5) or 5)}）",
            "info", category="steam",
        )
        to_list.append({
            "it": it, "list_price": list_price, "reason": reason,
            "price_cents": price_cents, "market_hash_name": market_hash_name,
            "name": name, "aid": aid,
            "purchase_id": buy_record.get("_db_id"), "currency": account_currency,
            "buyer_minor": int(round(display_price * 100)),
        })

    if skip_same_name_cap:
        total_skip = sum(skip_same_name_cap.values())
        parts = [f"{n} x{c}" for n, c in sorted(skip_same_name_cap.items(), key=lambda x: -x[1])[:5]]
        if len(skip_same_name_cap) > 5:
            parts.append(f"等共 {len(skip_same_name_cap)} 种")
        ctx.log(f"[出售] 同名在售已达上限跳过 共 {total_skip} 件（{', '.join(parts)}）", "info", category="steam")

    return to_list

def _find_buy_record(purchases_snapshot: list, aid: str, market_hash_name: str) -> Optional[dict]:
    """Return the current purchase record that owns *aid*.

    A market name is not an ownership identifier: the inventory may contain a
    personal drop or a later acquisition with the same name as an old purchase.
    Automatic listing therefore fails closed unless a current, exact asset-id
    record exists.
    """
    aid = str(aid or "").strip()
    if not aid:
        return None

    for p in purchases_snapshot:
        if str(p.get("assetid") or "").strip() != aid:
            continue

        # Do not let stale/finished records authorize another listing.  These
        # checks also protect against a stale inventory snapshot immediately
        # after a listing or sale.
        sale_price = p.get("sale_price")
        if sale_price is not None:
            try:
                if float(sale_price) > 0:
                    continue
            except (TypeError, ValueError):
                continue
        if p.get("sold_at") not in (None, "", 0, 0.0):
            continue
        if p.get("pending_receipt") or p.get("listing") or p.get("reprice_reserved"):
            continue
        return p
    return None


def _listing_response_body_preview(value: object, limit: int = 160) -> str:
    """Return a compact, bounded response excerpt suitable for logs."""
    if value is None:
        return "<empty>"
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    source_was_truncated = len(text) > max(limit * 4, 640)
    if source_was_truncated:
        text = text[: max(limit * 4, 640)]
    text = " ".join(text.split())
    if not text:
        return "<empty>"
    text = re.sub(
        r"(?i)\b(bearer)\s+[a-z0-9._~+/=-]+",
        r"\1 ***",
        text,
    )
    text = re.sub(
        (
            r"(?i)\b("
            r"steamloginsecure|sessionid|access[_-]?token|"
            r"refresh[_-]?token|token|cookie"
            r")(\s*[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;&}<]+)"
        ),
        r"\1\2***",
        text,
    )
    if source_was_truncated or len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _parse_listing_response(out: object) -> tuple[Optional[dict], str]:
    """Validate a low-level listing response and return diagnostics on failure."""
    if out is None:
        return None, "无响应"
    if not isinstance(out, dict):
        return None, f"返回类型异常: {type(out).__name__}"
    if out.get("error"):
        return None, _listing_response_body_preview(out["error"])

    status_code = out.get("status_code")
    status_label = str(status_code) if status_code is not None else "unknown"
    raw_text = out.get("text")
    _, data = parse_sell_response(raw_text)
    if data is None:
        return (
            None,
            f"HTTP {status_label}; body={_listing_response_body_preview(raw_text)}",
        )
    return data, ""


def _listing_response_message(data: dict, limit: int = 80) -> str:
    value = data.get("message")
    if value in (None, ""):
        return ""
    return _listing_response_body_preview(value, limit=limit)


def _listing_response_is_success(out: object, data: dict, message: str) -> bool:
    """Accept success only from the expected Steam HTTP response contract."""
    if not isinstance(out, dict) or out.get("status_code") != 200:
        return False
    message_lower = message.lower()
    return (
        data.get("success") is True
        or "pending confirmation" in message_lower
        or "already have a listing" in message_lower
    )


def _listing_rate_limited(ctx: PipelineContext, out: object) -> bool:
    if not isinstance(out, dict) or out.get("status_code") != 429:
        return False
    remaining = _listing_cooldown.defer(out.get("retry_after"))
    preview = _listing_response_body_preview(out.get("text"))
    ctx.log(
        f"[出售] 上架接口 HTTP 429; body={preview}，冷却约 {remaining:.0f}s，"
        "停止本批次，剩余物品未提交，不自动重试上架",
        "warn", category="steam",
    )
    return True


def _submit_listings(
    ctx: PipelineContext,
    to_list: list,
    session,
    session_id_effective: str,
    listing_delay: float,
) -> int:
    """POST listing requests to Steam and handle retries.

    Returns the count of successfully submitted listings.
    """
    remaining = _listing_cooldown.remaining()
    if remaining > 0:
        ctx.log(f"[出售] 上架接口冷却中，约 {remaining:.0f}s 后可重试，本批次未提交", "warn", category="steam")
        return 0
    listed = 0
    for entry in to_list:
        if ctx.is_stop_requested():
            ctx.set_status("stopped", "已停止")
            return listed

        it = entry["it"]
        list_price = entry["list_price"]
        reason = entry["reason"]
        price_cents = entry["price_cents"]
        name = entry["name"]
        aid = entry["aid"]
        attempt_id = None
        if entry.get("purchase_id"):
            from app.trading_ledger import begin_listing_attempt, minor
            attempt_id = begin_listing_attempt(entry["purchase_id"], aid, entry.get("buyer_minor", minor(list_price)), price_cents, entry.get("currency", "CNY"))
        from app.trading_ledger import finish_listing_attempt

        ctx.log(f"[出售] 上架请求 {name} assetid={aid} 价格={list_price:.2f} ({reason})", "info", category="steam")

        def _do_list():
            return list_item(
                session, session_id_effective,
                int(it.get("appid", 730)),
                str(it.get("contextid") or "2"),
                str(it.get("assetid", "")),
                price_cents,
            )

        try:
            out = _do_list()
            if _listing_rate_limited(ctx, out):
                finish_listing_attempt(attempt_id, rejected=True, message="Steam 限流，等待冷却后再试")
                break
            data, response_error = _parse_listing_response(out)
            if data is None:
                not_sent = isinstance(out, dict) and out.get("request_sent") is False
                finish_listing_attempt(attempt_id, rejected=not_sent, message=response_error)
                ctx.log(
                    f"[出售] {name} assetid={aid} 上架响应异常: {response_error}",
                    "warn",
                    category="steam",
                )
                jittered_sleep(listing_delay)
                continue

            msg = _listing_response_message(data)
            msg_lower = msg.lower()

            if _listing_response_is_success(out, data, msg):
                listed += 1
                entry["submitted"] = True
                ctx.log(f"[出售] 上架请求已接受 assetid={aid} {name} 价格={list_price:.2f}，正在核实在售/待确认状态 ({reason})", "info", category="steam")
                _record_listing_success(ctx, aid, name, list_price, listing_delay)
                continue

            if "previous action completes" in msg_lower or "until your previous" in msg_lower:
                ctx.log(f"[出售] {name} assetid={aid} 前一操作未完成，等待 5s 后重试", "info", category="steam")
                jittered_sleep(5)
                out2 = _do_list()
                if _listing_rate_limited(ctx, out2):
                    finish_listing_attempt(attempt_id, rejected=True, message="Steam 限流，等待冷却后再试")
                    break
                data2, retry_error = _parse_listing_response(out2)
                if data2 is None:
                    ctx.log(
                        f"[出售] {name} assetid={aid} 上架重试响应异常: {retry_error}",
                        "warn",
                        category="steam",
                    )
                else:
                    msg2 = _listing_response_message(data2).lower()
                    if _listing_response_is_success(out2, data2, msg2):
                        listed += 1
                        entry["submitted"] = True
                        ctx.log(f"[出售] 上架请求已接受 assetid={aid} {name} 价格={list_price:.2f}，正在核实在售/待确认状态 ({reason}) [重试成功]", "info", category="steam")
                        _record_listing_success(ctx, aid, name, list_price, listing_delay)
                        continue

            response_preview = (
                _listing_response_body_preview(out.get("text"))
                if isinstance(out, dict)
                else type(out).__name__
            )
            ctx.log(f"[出售] 上架失败 assetid={aid} {name}: {msg or response_preview}", "warn", category="steam")
            finish_listing_attempt(attempt_id, message=msg or "结果待核实")
        except Exception as ex:
            finish_listing_attempt(attempt_id, message=f"请求异常（{type(ex).__name__}），等待核实")
            error_detail = _listing_response_body_preview(ex)
            ctx.log(f"[出售] 上架时发生未捕获异常 assetid={aid} {name}: {type(ex).__name__} - {error_detail}", "error", category="steam")

        jittered_sleep(listing_delay)

    return listed


def _auto_confirm_listings(ctx: PipelineContext, cfg: dict, steam_id: str, cookies: str, assetids) -> None:
    """Reconcile this batch, and confirm only its owned pending market listings."""
    sync_listing_confirmations(ctx, cfg, {"steam_id": steam_id, "cookies": cookies}, assetids=assetids)


# ---------------------------------------------------------------------------
# Sell phase orchestrator
# ---------------------------------------------------------------------------

def _run_sell_phase_impl(cfg: dict, state, flow_id: str, items: Optional[list] = None) -> None:
    cfg = apply_strategy_to_config(cfg, "sell")
    pipeline_cfg = cfg.get("pipeline", {})
    verbose = bool(pipeline_cfg.get("verbose_debug", False))
    ctx = PipelineContext(state, flow_id, verbose=verbose)
    sell_strategy = int(pipeline_cfg.get("sell_strategy", 1))

    if sell_strategy == 4:
        ctx.log("策略4 暂停自动出售，跳过", "info")
        return

    cred_steam = get_steam_credentials()
    session_result = _resolve_steam_session(ctx, cred_steam)
    if session_result is None:
        return
    session, session_id_effective = session_result

    try:
        items = _get_inventory(ctx, items)
        if items is None:
            return

        sellable = [it for it in items if it.get("can_sell")]
        if not sellable:
            ctx.log("当前无可出售物品", "info", category="steam")
            return

        listing_delay = max(1, int(pipeline_cfg.get("listing_delay_seconds", 3) or 3))
        steam_debug = bool(pipeline_cfg.get("verbose_debug") or pipeline_cfg.get("steam_listings_debug"))
        debug_fn = (lambda m: ctx.log(m, "debug", category="steam")) if steam_debug else None

        ok_listings, active_listing_ids, err_listings, listing_assetid_to_name = fetch_my_listings(
            cred_steam.get("cookies"), debug_fn=debug_fn
        )
        if ok_listings:
            ctx.log(f"[出售] Steam 在售列表拉取成功，共 {len(active_listing_ids)} 个在售", "info", category="steam")
        else:
            ctx.log(f"[出售] Steam 在售列表拉取失败: {err_listings or '未知'}，同名在售校验将按本地购买记录", "warn", category="steam")

        purchases_snapshot = ctx.state.get_purchases()
        from app.services.repricing import reserved_purchase_ids
        reserved = reserved_purchase_ids() if any(p.get("_db_id") for p in purchases_snapshot) else set()
        purchases_snapshot = [{**p, "reprice_reserved": p.get("_db_id") in reserved} for p in purchases_snapshot]
        account = get_current_account()
        if account is None:
            # 工厂重置后账号列表为空，直接跳过出售避免以错误币种上架
            ctx.log("[出售] 无有效账号（可能刚执行了出厂重置），跳过本次出售", "warn", category="steam")
            return
        account_currency_cached = (account.get("currency_code") or "").strip().upper()
        region_check = refresh_account_region_currency(
            account.get("id"),
            cookies_raw=cred_steam.get("cookies", ""),
        )
        if not region_check.get("ok"):
            ctx.log(
                "[出售] 无法实时确认 Steam 账号结算币种，已拒绝上架，"
                f"防止跨区价格误卖。原因: {region_check.get('error') or '未知原因'}",
                "error",
                category="steam",
            )
            return
        account_currency = (region_check.get("currency_code") or "").strip().upper()
        account_region = (region_check.get("region_code") or "").strip().upper()
        if not account_currency:
            ctx.log(
                "[出售] Steam 未返回结算币种，已拒绝上架，防止跨区价格误卖。",
                "error",
                category="steam",
            )
            return
        if account_currency_cached and account_currency_cached != account_currency:
            ctx.log(
                f"[出售] 检测到账号币种变化: 缓存={account_currency_cached} 实时={account_currency}，"
                "已更新并使用实时币种定价。",
                "warn",
                category="steam",
            )
        if account_region:
            ctx.debug(
                f"[出售] 账号地区按结算币种派生: 币种={account_currency} 地区={account_region}",
                category="steam",
            )
        rate_map = _load_rate_map()

        assetid_to_name_map = {
            str(p.get("assetid") or "").strip(): (p.get("market_hash_name") or p.get("name") or "").strip()
            for p in purchases_snapshot if str(p.get("assetid") or "").strip()
        }

        ctx.log(
            f"策略{sell_strategy}，可出售 {len(sellable)} 件"
            f"（智能定价：墙+断层；上架间隔={listing_delay}s）",
            "info", category="steam",
        )

        to_list = _build_listing_plan(
            ctx, cfg, session, sellable, sell_strategy, pipeline_cfg,
            purchases_snapshot, ok_listings, active_listing_ids,
            listing_assetid_to_name, assetid_to_name_map, account_currency, rate_map,
        )

        if not to_list:
            ctx.debug("[出售] 本轮回无需上架", category="steam")
            return

        ctx.log(f"[出售] 开始上架 {len(to_list)} 件", "info", category="steam")
        listed = _submit_listings(ctx, to_list, session, session_id_effective, listing_delay)

        if listed:
            ctx.log(f"[出售] 本轮回已提交 {listed} 件上架请求，核实 Steam 状态", "info", category="steam")
            _auto_confirm_listings(ctx, cfg, cred_steam.get("steam_id", ""), cred_steam.get("cookies", ""),
                                   [entry["aid"] for entry in to_list if entry.get("submitted")])
    finally:
        session.close()


def _run_sell_phase(cfg: dict, state, flow_id: str, items: Optional[list] = None) -> None:
    from app.services.lifecycle import is_shutting_down
    if is_shutting_down():
        return
    if not _sell_phase_lock.acquire(blocking=False):
        state.log("[出售] 已有出售任务在执行，本次跳过", "info", category="steam", flow_id=flow_id)
        return
    try:
        if is_shutting_down():
            return
        _run_sell_phase_impl(cfg, state, flow_id, items)
    finally:
        _sell_phase_lock.release()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_sell_phase_on_inventory_update(items: list) -> None:
    from app.services.lifecycle import is_shutting_down
    if is_shutting_down():
        return
    cfg = merge(DEFAULTS, load_app_config_validated())
    state = get_state()
    t = threading.Thread(
        target=_run_sell_phase,
        args=(cfg, state, "inventory"),
        kwargs={"items": items},
        daemon=True,
    )
    t.start()
