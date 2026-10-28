"""Pure sell decision evaluation shared by live listing checks and previews.

The evaluator receives an already hydrated strategy and an immutable input
snapshot. It deliberately performs no network, configuration, database, or
clock reads so a recorded decision can be reproduced exactly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from steam.market_orders import compute_smart_list_price
from utils.time import parse_steam_history_date
from utils.trend import calculate_trend_robust


_PRICING_MODULES = {
    "pricing.steam_wall_price",
    "pricing.steam_wall_gap",
}
_CURRENCY_FOLD_MODULES = _PRICING_MODULES | {"pricing.price_offset", "pricing.currency"}
_BLOCKING_STATUSES = {"reject", "wait", "unavailable", "unsupported"}


def _step(
    module_id: str,
    status: str,
    reason: str,
    *,
    actual=None,
    threshold=None,
    output=None,
) -> dict:
    step = {"module_id": module_id, "status": status, "reason": reason}
    if actual is not None:
        step["actual"] = actual
    if threshold is not None:
        step["threshold"] = threshold
    if output is not None:
        step["output"] = output
    return step


def _runtime(strategy: dict) -> dict:
    runtime = ((strategy or {}).get("_strategy_runtime") or {}).get("sell") or {}
    if runtime:
        return runtime
    return {"steps": (strategy or {}).get("steps") or [], "modules": {}}


def _strategy_steps(strategy: dict) -> list[dict]:
    steps = _runtime(strategy).get("steps")
    if not isinstance(steps, list) or not steps:
        steps = (strategy or {}).get("steps") or []
    return [step for step in steps if isinstance(step, dict)]


def _param(step: dict, pipeline: dict, key: str, default=None):
    params = step.get("params") if isinstance(step.get("params"), dict) else {}
    if key in params:
        return params[key]
    return pipeline.get(key, default)


def _enabled_steps(steps: list[dict]) -> list[dict]:
    return [step for step in steps if step.get("enabled") is not False]


def _orders(market: dict) -> list[tuple[float, int]]:
    orders = []
    for row in market.get("sell_orders") or []:
        if isinstance(row, dict):
            raw_price, raw_quantity = row.get("price"), row.get("quantity")
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            raw_price, raw_quantity = row[0], row[1]
        else:
            continue
        try:
            price = float(raw_price)
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            continue
        if price > 0 and quantity > 0:
            orders.append((price, quantity))
    return orders


def _history_timestamp(value) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, str):
        parsed = parse_steam_history_date(value)
        if parsed is None:
            return None
        return parsed.timestamp()
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if timestamp > 0 else None


def _trend_point(row) -> tuple[float, float] | None:
    if isinstance(row, dict):
        timestamp = next(
            (
                _history_timestamp(row.get(key))
                for key in ("captured_at", "timestamp", "time", "date", "date_text")
                if _history_timestamp(row.get(key)) is not None
            ),
            None,
        )
        raw_price = row.get("price_cny", row.get("price"))
    elif isinstance(row, (list, tuple)) and len(row) >= 2:
        timestamp = _history_timestamp(row[0])
        raw_price = row[1]
    else:
        return None
    try:
        price = float(raw_price)
    except (TypeError, ValueError, OverflowError):
        return None
    if timestamp is None or price <= 0:
        return None
    return timestamp, price


def _trend_cny_rate(trend: dict, currency: dict) -> tuple[float | None, str, str]:
    """Return CNY per source-history unit and its source currency code."""
    source_code = str(trend.get("currency") or "").strip().upper()
    if not source_code:
        return None, "历史行情缺少币种，无法按统一币种计算趋势", ""
    if source_code in {"CNY", "RMB"}:
        return 1.0, "", source_code

    explicit_rate = trend.get("cny_per_currency")
    if explicit_rate not in (None, ""):
        try:
            rate = float(explicit_rate)
        except (TypeError, ValueError, OverflowError):
            rate = 0.0
        if rate > 0:
            return rate, "", source_code

    account_code = str(currency.get("code") or "").strip().upper()
    if source_code == account_code:
        rate = _cny_per_account(currency)
        if rate > 0:
            return rate, "", source_code

    return (
        None,
        f"历史行情币种 {source_code} 缺少人民币汇率，无法按统一币种计算趋势",
        source_code,
    )


def _history_trend(
    trend: dict, window_days: int, evaluated_at=None, currency: dict | None = None
) -> tuple[float | None, str, int, float | None, str]:
    """Recompute the original robust trend over the parameter's sample window.

    History points retain their source currency. They are converted to CNY
    before the robust trend so dynamic sensitivity has the original formal
    rule's absolute-price semantics. Samples after the decision timestamp are
    not evidence available at that decision and are excluded. The window is
    then anchored to the newest remaining sample, matching the live listing
    rule; it is intentionally not anchored to the wall clock.
    """
    currency = currency if isinstance(currency, dict) else {}
    rate, rate_error, source_currency = _trend_cny_rate(trend, currency)
    if rate is None:
        return None, rate_error, 0, None, source_currency

    rows = []
    for raw in trend.get("points") or []:
        point = _trend_point(raw)
        if point is not None:
            rows.append(point)
    had_timestamped_points = bool(rows)

    cutoffs = [
        cutoff
        for cutoff in (
            _history_timestamp(trend.get("captured_at")),
            _history_timestamp(evaluated_at),
        )
        if cutoff is not None
    ]
    if cutoffs:
        evidence_cutoff = min(cutoffs)
        rows = [(timestamp, price) for timestamp, price in rows if timestamp <= evidence_cutoff]
    rows.sort(key=lambda point: point[0])

    if rows:
        newest = rows[-1][0]
        cutoff = newest - timedelta(days=window_days).total_seconds()
        prices = [price * rate for timestamp, price in rows if timestamp >= cutoff]
        change = calculate_trend_robust(prices, use_dynamic_sensitivity=True) if len(prices) >= 3 else 0
        return float(change), "", len(prices), rate, source_currency

    # Compatibility for older recorded inputs that only retained the computed
    # trend. Preview should use timestamped points whenever they exist so a
    # changed window can be evaluated against the original observations.
    raw_change = trend.get("change")
    if not had_timestamped_points and raw_change is not None:
        try:
            return float(raw_change), "", 0, rate, source_currency
        except (TypeError, ValueError, OverflowError):
            pass
    return (
        None,
        "历史行情没有可用的带时间样本，无法按参数窗口计算趋势",
        0,
        rate,
        source_currency,
    )


def _not_run(step: dict, reason: str) -> dict:
    return _step(step.get("module_id") or "unknown", "not_run", reason)


def _disabled(step: dict) -> dict:
    return _step(step.get("module_id") or "unknown", "disabled", "模块未启用，本次未执行")


def _blocks(status: str) -> bool:
    return status in _BLOCKING_STATUSES


def _finish(steps: list[dict], results: list[dict], status: str, blocker_id: str = "") -> dict:
    """Complete every strategy step, preserving its exact position and order."""
    output = []
    blocker = blocker_id or (results[-1].get("module_id") if results else "")
    emitted = 0
    for definition in steps:
        if emitted < len(results) and results[emitted].get("module_id") == definition.get("module_id"):
            output.append(results[emitted])
            emitted += 1
            continue
        if definition.get("enabled") is False:
            output.append(_disabled(definition))
        else:
            reason = "前置检查已阻断，未执行"
            if blocker:
                reason += f"（阻断步骤：{blocker}）"
            output.append(_not_run(definition, reason))
    output.extend(results[emitted:])

    final_status = status
    if final_status == "pass":
        actions = [
            result
            for result, definition in zip(output, steps)
            if definition.get("module_id") in {"action.steam_list", "action.pause_auto_sell"}
        ]
        if actions and all(result.get("status") == "disabled" for result in actions):
            final_status = "wait"
    return {"status": final_status, "steps": output}


def _cny_per_account(currency: dict) -> float:
    """Return CNY per one account-currency unit, accepting the legacy name."""
    raw_rate = currency.get("cny_per_account")
    if raw_rate in (None, ""):
        raw_rate = currency.get("account_per_cny")
    try:
        rate = float(raw_rate)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return rate if rate > 0 else 0.0


def _currency_check(currency: dict, list_price, module_id: str) -> dict:
    """Return currency evidence without adding an unmodeled strategy step."""
    code = str(currency.get("code") or "").strip().upper()
    supplied_rate = _cny_per_account(currency) or None
    base_actual = {"code": code or None, "cny_per_account": supplied_rate}
    if "account_per_cny" in currency:
        base_actual["account_per_cny"] = supplied_rate
    if not code:
        return {
            "status": "unavailable",
            "reason": "缺少账号结算币种，无法安全定价",
            "actual": base_actual,
            "threshold": {"code": "非空"},
        }
    if list_price is None:
        return {
            "status": "unavailable",
            "reason": "尚未计算出人民币建议价，无法换算账号结算价格",
            "actual": base_actual,
            "threshold": {"list_price_cny": "> 0"},
        }

    display_price = list_price
    rate = 1.0 if code == "CNY" else supplied_rate
    if rate is None or rate <= 0:
        return {
            "status": "unavailable",
            "reason": f"账号币种 {code} 缺少人民币汇率，无法安全定价",
            "actual": base_actual,
            "threshold": {"cny_per_account": "> 0"},
        }
    if code != "CNY":
        display_price = round(float(list_price) / rate, 2)
    actual = {
        "code": code,
        "cny_per_account": rate,
        "list_price_cny": list_price,
        "display_price": display_price,
    }
    if "account_per_cny" in currency:
        actual["account_per_cny"] = rate
    from utils.money import list_price_display_to_cents
    estimated_proceeds = list_price_display_to_cents(display_price, code) / 100
    actual["estimated_proceeds"] = estimated_proceeds
    return {
        "status": "pass",
        "reason": f"已按账号结算币种 {code} 换算；预演不提交，真实提交仍由调用方执行",
        "actual": actual,
        "output": {
            "list_price_cny": list_price,
            "display_price": display_price,
            "estimated_proceeds": estimated_proceeds,
            "currency": code,
        },
        "display_price": display_price,
        "currency": code,
    }


def _fold_currency(step: dict, check: dict) -> None:
    """Attach currency evidence to the last pricing step in the strategy."""
    step["actual"] = {**(step.get("actual") or {}), **(check.get("actual") or {})}
    if check["status"] == "pass":
        step["output"] = {**(step.get("output") or {}), **(check.get("output") or {})}
        if check["reason"] not in step["reason"]:
            step["reason"] += f"；{check['reason']}"
        return
    step["status"] = "unavailable"
    step["reason"] += f"；{check['reason']}"
    if check.get("threshold"):
        step["threshold"] = {**(step.get("threshold") or {}), **check["threshold"]}


def _declarative_result(
    module: dict,
    params: dict,
    context: dict,
    outputs: dict,
    evaluator: Callable | None,
) -> dict:
    from app import strategy_engine as se
    data = {**context, "params": params, "outputs": outputs}
    conditions = module.get("conditions") or []
    missing = not conditions
    for condition in conditions:
        paths = [condition.get("left")]
        if "right_path" in condition:
            paths.append(condition["right_path"])
        for path in paths:
            value = se._resolve_path(path, data)
            if value is se._MISSING or value is None:
                missing = True
    if missing:
        return _step(str(module.get("id") or ""), "unavailable", "声明式模块所需业务输入缺失")
    try:
        if evaluator is None:
            from app.strategy_engine import _evaluate_declarative_module

            result = _evaluate_declarative_module(
                module, params, context=context, outputs=outputs
            )
        else:
            result = evaluator(module, params, context, outputs)
    except Exception as exc:  # A failed shared rule is unavailable, never a pass.
        return _step(
            str(module.get("id") or "unknown"),
            "unavailable",
            f"共享策略规则执行异常，无法判断（{type(exc).__name__}）",
            output={"exception": type(exc).__name__},
        )

    status = str((result or {}).get("status") or "unsupported").lower()
    if status == "error":
        status = "unavailable"
    if status not in {"pass", "reject", "wait", "unavailable"}:
        status = "unsupported"
    reason = str((result or {}).get("reason") or "").strip()
    if not reason:
        reason = "声明式模块未给出判断原因"
    return _step(
        str(module.get("id") or ""),
        status,
        reason,
        output=(result or {}).get("output"),
    )


def _evaluate(
    inputs: dict,
    strategy: dict,
    evaluated_at,
    modules: dict | None = None,
    *,
    pricing_fn: Callable | None = None,
    declarative_evaluator: Callable | None = None,
) -> dict:
    """Internal evaluator with injectable canonical algorithm seams for live callers."""
    # The supplied timestamp fixes the evidence horizon; no wall clock is read.
    strategy = strategy or {}
    steps = _strategy_steps(strategy)
    if str(strategy.get("strategy_type") or "").strip().lower() == "reprice":
        reason = "自动调价仅支持解释记录，不支持作为普通上架策略预演"
        return {
            "status": "unsupported",
            "steps": [
                _step(step.get("module_id") or "unknown", "unsupported", reason)
                for step in steps
            ],
        }

    modules = modules if isinstance(modules, dict) else _runtime(strategy).get("modules") or {}
    pipeline = strategy.get("pipeline") if isinstance(strategy.get("pipeline"), dict) else {}
    inputs = inputs if isinstance(inputs, dict) else {}
    item = inputs.get("item") if isinstance(inputs.get("item"), dict) else {}
    inventory = inputs.get("inventory") if isinstance(inputs.get("inventory"), dict) else {}
    ownership = inputs.get("ownership") if isinstance(inputs.get("ownership"), dict) else {}
    listing = inputs.get("listing") if isinstance(inputs.get("listing"), dict) else {}
    market = inputs.get("market") if isinstance(inputs.get("market"), dict) else {}
    trend = inputs.get("trend") if isinstance(inputs.get("trend"), dict) else {}
    currency = inputs.get("currency") if isinstance(inputs.get("currency"), dict) else {}

    enabled = _enabled_steps(steps)
    currency_indices = [
        index
        for index, step in enumerate(steps)
        if step.get("enabled") is not False
        and str(step.get("module_id") or "") in _CURRENCY_FOLD_MODULES
    ]
    currency_index = currency_indices[-1] if currency_indices else None
    if currency_index is None:
        currency_indices = [
            index
            for index, step in enumerate(steps)
            if step.get("enabled") is not False
            and str(step.get("module_id") or "") in {"action.steam_list", "action.pause_auto_sell"}
        ]
        currency_index = currency_indices[0] if currency_indices else None

    results: list[dict] = []
    outputs: dict[str, Any] = {}
    list_price = None
    display_price = None
    account_currency = None
    pricing_reason = ""
    currency_done = False
    ownership_done = False

    def block(status: str, step: dict) -> dict:
        results.append(step)
        return _finish(steps, results, status, str(step.get("module_id") or ""))

    def finish_currency(index: int, result: dict) -> tuple[dict, bool, bool]:
        nonlocal display_price, account_currency, currency_done
        if index != currency_index:
            return result, False, False
        check = _currency_check(currency, list_price, result["module_id"])
        _fold_currency(result, check)
        if check["status"] != "pass":
            return result, True, False
        display_price = check["display_price"]
        account_currency = check["currency"]
        currency_done = True
        return result, False, True

    for index, step in enumerate(steps):
        module_id = str(step.get("module_id") or "").strip()
        if step.get("enabled") is False:
            results.append(_disabled(step))
            continue

        if module_id == "sell.sellable_inventory_filter":
            can_sell = inventory.get("can_sell") is True
            eligible = ownership.get("eligible") is True
            ownership_done = True
            passed = can_sell and eligible
            reason = "Steam 标记该资产可出售，且归属来自本账号的有效购买记录"
            if not can_sell:
                reason = "Steam 未标记该资产可出售"
            elif not eligible:
                reason = "资产不在本账号有效购买记录中，保护个人物品不自动上架"
            result = _step(
                module_id,
                "pass" if passed else "reject",
                reason,
                actual={
                    "can_sell": can_sell,
                    "ownership_eligible": eligible,
                    "purchase_id": ownership.get("purchase_id"),
                },
                threshold={"can_sell": True, "ownership_eligible": True},
            )
            if not passed:
                return block("reject", result)
            results.append(result)
            continue

        if module_id == "sell.ownership":
            eligible = ownership.get("eligible") is True
            ownership_done = True
            result = _step(
                module_id,
                "pass" if eligible else "reject",
                "资产归属来自本账号的有效购买记录"
                if eligible
                else "资产不在本账号有效购买记录中，保护个人物品不自动上架",
                actual={"eligible": eligible, "purchase_id": ownership.get("purchase_id")},
                threshold={"eligible": True},
            )
            if not eligible:
                return block("reject", result)
            results.append(result)
            continue

        if module_id == "guard.max_listings_per_item":
            if not ownership_done and ownership.get("eligible") is not True:
                result = _step(
                    module_id,
                    "reject",
                    "缺少有效购买归属，不能执行同名在售检查",
                    actual={"eligible": False},
                    threshold={"eligible": True},
                )
                return block("reject", result)
            try:
                max_per_item = max(
                    1, int(_param(step, pipeline, "max_listings_per_item", 5) or 5)
                )
                same_name = int(listing.get("same_name_count") or 0)
                round_same_name = int(listing.get("round_same_name_count") or 0)
            except (TypeError, ValueError):
                result = _step(
                    module_id,
                    "unavailable",
                    "同名在售数量或上限数据无效，无法执行检查",
                )
                return block("unavailable", result)
            total_same_name = same_name + round_same_name
            passed = total_same_name < max_per_item
            result = _step(
                module_id,
                "pass" if passed else "reject",
                "同名在售数量未达到上限"
                if passed
                else "同名在售数量已达到策略上限",
                actual={
                    "same_name_count": same_name,
                    "round_same_name_count": round_same_name,
                    "total": total_same_name,
                },
                threshold={"max_listings_per_item": max_per_item},
                output={
                    "steam_same_name": same_name,
                    "round_same_name": round_same_name,
                    "max_per_item": max_per_item,
                },
            )
            if not passed:
                return block("reject", result)
            outputs[module_id] = result["output"]
            results.append(result)
            continue

        if module_id in _PRICING_MODULES:
            if market.get("available") is not True:
                result = _step(
                    module_id,
                    "unavailable",
                    "无法获取 Steam 卖单行情"
                    + (f"：{market.get('error')}" if market.get("error") else ""),
                    actual={"available": False},
                    threshold={"available": True},
                )
                return block("unavailable", result)
            orders = _orders(market)
            try:
                wall_volume = int(
                    _param(step, pipeline, "sell_price_wall_volume", 20) or 20
                )
                max_ignore = int(
                    _param(step, pipeline, "sell_price_max_ignore_volume", 4) or 4
                )
                if module_id == "pricing.steam_wall_gap":
                    offset = float(
                        _param(step, pipeline, "sell_price_offset", 0) or 0
                    )
                else:
                    offset_step = next(
                        (
                            candidate
                            for candidate in enabled
                            if candidate.get("module_id") == "pricing.price_offset"
                        ),
                        None,
                    )
                    offset = (
                        float(
                            _param(
                                offset_step,
                                pipeline,
                                "sell_price_offset",
                                0,
                            )
                            or 0
                        )
                        if offset_step
                        else 0.0
                    )
                price_fn = pricing_fn or compute_smart_list_price
                list_price, pricing_reason = price_fn(
                    orders,
                    wall_volume_threshold=wall_volume,
                    max_ignore_volume=max_ignore,
                    offset=offset,
                )
            except Exception as exc:
                result = _step(
                    module_id,
                    "unavailable",
                    f"共享定价算法执行异常，无法安全定价（{type(exc).__name__}）",
                    output={"exception": type(exc).__name__},
                )
                return block("unavailable", result)
            if list_price is None or list_price <= 0:
                result = _step(
                    module_id,
                    "unavailable",
                    f"无法计算 Steam 建议价：{pricing_reason}",
                    actual={"order_count": len(orders)},
                    output={"reason": pricing_reason},
                )
                return block("unavailable", result)
            list_price = round(float(list_price), 2)
            result = _step(
                module_id,
                "pass",
                f"已按 Steam 卖单墙和断层计算建议价：{pricing_reason}",
                actual={"order_count": len(orders), "list_price_cny": list_price},
                threshold={
                    "wall_volume": wall_volume,
                    "max_ignore_volume": max_ignore,
                },
                output={
                    "list_price": list_price,
                    "reason": pricing_reason,
                    "order_count": len(orders),
                    "sell_price_offset": offset,
                },
            )
            outputs[module_id] = result["output"]
            result, blocked, _ = finish_currency(index, result)
            if blocked:
                return block("unavailable", result)
            results.append(result)
            continue

        if module_id == "pricing.price_offset":
            if list_price is None:
                result = _step(
                    module_id,
                    "unavailable",
                    "尚未得到基础建议价，无法应用上架价补偿",
                    actual={"sell_price_offset": _param(step, pipeline, "sell_price_offset", 0)},
                )
                return block("unavailable", result)
            try:
                offset = float(_param(step, pipeline, "sell_price_offset", 0) or 0)
            except (TypeError, ValueError):
                result = _step(module_id, "unavailable", "上架价补偿参数无效")
                return block("unavailable", result)
            result = _step(
                module_id,
                "pass",
                "已应用上架价补偿" if offset else "上架价补偿为 0",
                actual={"sell_price_offset": offset},
                output={"sell_price_offset": offset, "list_price": list_price},
            )
            outputs[module_id] = result["output"]
            result, blocked, _ = finish_currency(index, result)
            if blocked:
                return block("unavailable", result)
            results.append(result)
            continue

        if module_id == "pricing.currency":
            check = _currency_check(currency, list_price, module_id)
            result = _step(
                module_id,
                check["status"],
                check["reason"],
                actual=check.get("actual"),
                threshold=check.get("threshold"),
                output=check.get("output"),
            )
            if result["status"] != "pass":
                return block("unavailable", result)
            display_price = check["display_price"]
            account_currency = check["currency"]
            currency_done = True
            results.append(result)
            continue

        if module_id == "guard.rising_trend_wait":
            try:
                trend_days = int(
                    _param(step, pipeline, "sell_trend_days", 7) or 7
                )
                if trend_days <= 0:
                    raise ValueError("invalid trend window")
            except (TypeError, ValueError):
                result = _step(
                    module_id,
                    "unavailable",
                    "价格趋势窗口参数无效，无法判断是否应等待",
                    actual={"window_days": trend_days if "trend_days" in locals() else None},
                    threshold={"window_days": "> 0"},
                )
                return block("unavailable", result)

            change = None
            sample_count = 0
            trend_currency = str(trend.get("currency") or "").strip().upper()
            trend_cny_rate = None
            if trend.get("available") is True:
                change, trend_error, sample_count, trend_cny_rate, trend_currency = _history_trend(
                    trend, trend_days, evaluated_at, currency
                )
            else:
                trend_error = "历史行情不可用"
            if change is None:
                if trend.get("error"):
                    trend_error = str(trend["error"])
                result = _step(
                    module_id,
                    "unavailable",
                    f"缺少近 {trend_days} 天价格趋势数据，无法判断是否应等待：{trend_error}",
                    actual={"available": False, "sample_count": sample_count},
                    threshold={"window_days": trend_days},
                )
                return block("unavailable", result)
            passed = change <= 0
            result = _step(
                module_id,
                "pass" if passed else "wait",
                f"近 {trend_days} 天趋势未上涨，可以继续检查"
                if passed
                else f"近 {trend_days} 天价格上涨，暂缓出售",
                actual={
                    "change": change,
                    "sample_count": sample_count,
                    "trend_currency": trend_currency,
                    "cny_per_trend_unit": trend_cny_rate,
                },
                threshold={"max_change": 0},
                output={"trend": change, "trend_days": trend_days},
            )
            outputs[module_id] = result["output"]
            if not passed:
                return block("wait", result)
            results.append(result)
            continue

        if module_id == "guard.profit_ratio":
            try:
                buy_price = float(ownership.get("purchase_price_cny") or 0)
                market_at_buy = float(
                    ownership.get("market_price_cny_at_purchase") or 0
                )
            except (TypeError, ValueError):
                buy_price = market_at_buy = 0
            if buy_price <= 0 or market_at_buy <= 0 or not list_price:
                result = _step(
                    module_id,
                    "unavailable",
                    "缺少原始购买价或购入时市场价，无法计算利润比例",
                    actual={
                        "purchase_price_cny": buy_price or None,
                        "market_price_cny_at_purchase": market_at_buy or None,
                        "list_price_cny": list_price,
                    },
                )
                return block("unavailable", result)
            try:
                multiplier = float(
                    _param(step, pipeline, "profit_ratio_multiplier", 1.05) or 1.05
                )
            except (TypeError, ValueError):
                result = _step(module_id, "unavailable", "利润比例倍数参数无效")
                return block("unavailable", result)
            current_ratio = buy_price / (list_price / 1.15)
            original_ratio = buy_price / (market_at_buy / 1.15)
            ratio_limit = original_ratio * multiplier
            passed = current_ratio <= ratio_limit
            result = _step(
                module_id,
                "pass" if passed else "reject",
                "当前利润比例在保护范围内"
                if passed
                else "当前利润比例超过购入时比例允许倍数，避免过低价格售出",
                actual={"current_ratio": current_ratio, "original_ratio": original_ratio},
                threshold={"ratio_limit": ratio_limit},
                output={
                    "current_ratio": current_ratio,
                    "original_ratio": original_ratio,
                    "ratio_limit": ratio_limit,
                    "profit_ratio_multiplier": multiplier,
                },
            )
            outputs[module_id] = result["output"]
            if not passed:
                return block("reject", result)
            results.append(result)
            continue

        if module_id == "action.pause_auto_sell":
            result = _step(
                module_id,
                "wait",
                "当前出售策略为暂停自动出售",
                output={"enabled": True, "executed": False},
            )
            return block("wait", result)

        if module_id == "action.steam_list":
            if not currency_done:
                check = _currency_check(currency, list_price, module_id)
                if check["status"] != "pass":
                    result = _step(
                        module_id,
                        "unavailable",
                        "普通上架检查无法完成：" + check["reason"],
                        actual=check.get("actual"),
                        threshold=check.get("threshold"),
                    )
                    return block("unavailable", result)
                display_price = check["display_price"]
                account_currency = check["currency"]
                currency_done = True
            if list_price is None:
                result = _step(
                    module_id,
                    "unavailable",
                    "没有可用的建议价，无法形成上架计划",
                    actual={"list_price_cny": None},
                )
                return block("unavailable", result)
            result = _step(
                module_id,
                "pass",
                "普通上架检查通过；预演不提交上架，真实流程由调用方继续执行",
                actual={"ready_to_list": True},
                output={
                    "list_price": list_price,
                    "display_price": display_price,
                    "currency": account_currency,
                    "reason": pricing_reason or None,
                    "executed": False,
                    "item": {
                        "name": item.get("name"),
                        "market_hash_name": item.get("market_hash_name"),
                        "goods_id": item.get("goods_id"),
                        "assetid": item.get("assetid"),
                    },
                },
            )
            results.append(result)
            continue

        module = modules.get(module_id) if isinstance(modules.get(module_id), dict) else {}
        try:
            from app.strategy_engine import (
                _is_declarative_user_module,
                _module_stage_matches,
            )

            declarative = bool(module) and _is_declarative_user_module(module)
            stage_matches = declarative and _module_stage_matches(
                module, "sell", "sell.listing_guard"
            )
        except Exception:
            declarative = False
            stage_matches = False
        if not declarative:
            result = _step(
                module_id or "unknown",
                "unsupported",
                "该模块不支持普通上架解释或自动执行，未默认通过",
            )
            return block("unsupported", result)
        if not stage_matches:
            result = _step(
                module_id,
                "unsupported",
                "该模块不适用于普通上架检查阶段",
            )
            return block("unsupported", result)

        business_pipeline = dict(pipeline)
        for definition in steps:
            mid = definition.get("module_id") or ""
            if definition.get("enabled") is not False and mid.startswith(("sell.", "guard.", "pricing.", "action.")):
                business_pipeline.update(definition.get("params") or {})
        context = {
            "config": {"pipeline": business_pipeline},
            "item": item,
            "inventory": inventory,
            "ownership": ownership,
            "market": market,
            "trend": trend,
            "buy_record": {
                "price": ownership.get("purchase_price_cny"),
                "market_price": ownership.get("market_price_cny_at_purchase"),
            },
            "listing": {
                "list_price": list_price,
                "display_price": display_price,
                "same_name_count": listing.get("same_name_count"),
                "round_same_name_count": listing.get("round_same_name_count"),
            },
            "outputs": outputs,
        }
        params = step.get("params") if isinstance(step.get("params"), dict) else {}
        result = _declarative_result(
            module, params, context, outputs, declarative_evaluator
        )
        result["module_id"] = module_id
        outputs[module_id] = result.get("output") or {}
        if _blocks(result["status"]):
            return block(result["status"], result)
        results.append(result)

    return _finish(steps, results, "pass")


def evaluate(inputs: dict, strategy: dict, evaluated_at, modules: dict | None = None) -> dict:
    """Evaluate one sell decision without network, current config, or current clock."""
    return _evaluate(inputs, strategy, evaluated_at, modules)
