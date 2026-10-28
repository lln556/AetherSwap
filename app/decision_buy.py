"""Pure, replayable evaluation of the buy decision rules.

The formal pipeline and strategy preview both call this module.  It must not
fetch market data, read the active configuration, or consult the wall clock.
"""

from __future__ import annotations

import math
import statistics
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from analysis.stability import analyze_by_time
from utils.money import CURRENCY_CNY, CURRENCY_USD
from utils.time import parse_steam_history_date


STEAM_FEE_FACTOR = 1.15
HISTORY_MODULE_IDS = (
    "guard.history_stability",
    "guard.history_data_window",
    "guard.volatility_cv",
    "guard.trend_quality",
    "guard.price_position",
)
PURCHASE_LIMIT_MODULE_IDS = (
    "guard.safe_purchase_limit",
    "guard.purchase_hard_cap",
    "guard.purchase_liquidity_cap",
    "guard.low_price_purchase_guard",
    "guard.held_same_item_guard",
)


def _steps(strategy: Optional[dict]) -> List[dict]:
    steps = (strategy or {}).get("steps")
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def _enabled_ids(strategy: Optional[dict]) -> set:
    return {
        str(step.get("module_id") or "")
        for step in _steps(strategy)
        if step.get("enabled") is not False and step.get("module_id")
    }


def _params(strategy: Optional[dict], module_id: str) -> Dict[str, Any]:
    for step in _steps(strategy):
        if (
            step.get("module_id") == module_id
            and step.get("enabled") is not False
            and isinstance(step.get("params"), dict)
        ):
            return dict(step["params"])
    return {}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any, default: int = 0) -> int:
    number = _number(value)
    return int(number) if number is not None else default


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list:
    return value if isinstance(value, list) else []


def _step(
    module_id: str,
    status: str,
    reason: str,
    *,
    actual: Any = None,
    threshold: Any = None,
    output: Any = None,
) -> Dict[str, Any]:
    step: Dict[str, Any] = {
        "module_id": module_id,
        "status": status,
        "reason": reason,
    }
    if actual is not None:
        step["actual"] = actual
    if threshold is not None:
        step["threshold"] = threshold
    if output is not None:
        step["output"] = output
    return step


def _result(steps: Sequence[dict]) -> Dict[str, Any]:
    for status in ("reject", "unavailable", "unsupported", "wait"):
        if any(step.get("status") == status for step in steps):
            return {"status": status, "steps": list(steps)}
    return {"status": "pass", "steps": list(steps)}


def _order_rows(orders: Any) -> List[Tuple[float, int]]:
    normalized: List[Tuple[float, int]] = []
    for order in _rows(orders):
        if isinstance(order, dict):
            price = _number(order.get("price"))
            quantity = _integer(order.get("quantity"))
        elif isinstance(order, (list, tuple)) and len(order) >= 2:
            price = _number(order[0])
            quantity = _integer(order[1])
        else:
            continue
        if price is not None and price > 0 and quantity > 0:
            normalized.append((price, quantity))
    return normalized


def compute_sell_pressure(
    sell_orders: Any,
    daily_volume: Any,
    n_orders: int = 5,
) -> Optional[float]:
    """Current sell-pressure rule, including thin-wall dampening."""

    daily = _integer(daily_volume)
    orders = _order_rows(sell_orders)
    if daily <= 0 or not orders:
        return None
    target_orders = sorted(orders, key=lambda row: row[0])[:max(1, int(n_orders))]
    total_volume = sum(volume for _, volume in target_orders)
    base_pressure = total_volume / daily
    if len(target_orders) < 2:
        return base_pressure
    current_volume = 0
    wall_volume = 0
    for index in range(len(target_orders) - 1):
        current_price, current_quantity = target_orders[index]
        next_price, _ = target_orders[index + 1]
        current_volume += current_quantity
        if current_price < 5.0:
            gap_absolute, gap_relative = 0.10, 0.08
        elif current_price < 20.0:
            gap_absolute, gap_relative = 0.30, 0.05
        elif current_price < 100.0:
            gap_absolute, gap_relative = 1.0, 0.03
        elif current_price < 500.0:
            gap_absolute, gap_relative = 5.0, 0.02
        else:
            gap_absolute, gap_relative = 10.0, 0.015
        gap_threshold = max(gap_absolute, current_price * gap_relative)
        if next_price - current_price > gap_threshold:
            wall_volume = current_volume
            break
    if wall_volume > 0 and wall_volume <= max(3, daily * 0.15):
        return base_pressure * 0.4
    return base_pressure


def _convert_price(
    price: float,
    currency: Optional[str],
    valuation: dict,
) -> Optional[float]:
    code = str(currency or "").strip().upper()
    if not code:
        return None
    if code == CURRENCY_CNY:
        return price
    if code == CURRENCY_USD:
        rate = _number(valuation.get("usd_to_cny"))
        return price * rate if rate is not None and rate > 0 else None
    rates = _mapping(valuation.get("rates"))
    rate = _number(rates.get(code))
    return price * rate if rate is not None and rate > 0 else None


def buff_price_within_tolerance(lowest_price, plan_price, tolerance):
    return plan_price is None or lowest_price - plan_price <= tolerance


def adjust_reference_price(
    current_ref_price: Any,
    history_rows: Any,
    *,
    currency: Optional[str],
    evaluated_at: datetime,
    valuation: Optional[dict] = None,
) -> float:
    """Daily-high adjustment using only the supplied history and instant."""

    current = _number(current_ref_price)
    if current is None or current <= 0:
        return 0.0
    cutoff = evaluated_at.astimezone(timezone.utc) - timedelta(hours=24)
    prices: List[float] = []
    for row in _rows(history_rows):
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        parsed_at = parse_steam_history_date(str(row[0]))
        if parsed_at is None or parsed_at < cutoff:
            continue
        price = _number(row[1])
        if price is None:
            continue
        converted = _convert_price(price, currency, _mapping(valuation))
        if converted is not None:
            prices.append(converted)
    if len(prices) < 2:
        return current
    sorted_prices = sorted(prices)
    trimmed = sorted_prices[1:-1] if len(sorted_prices) >= 3 else sorted_prices
    if not trimmed:
        return current
    low, high = trimmed[0], trimmed[-1]
    if high <= low:
        return current
    daily_position = (current - low) / (high - low)
    if daily_position <= 0.6:
        return current
    daily_average = statistics.mean(trimmed)
    return min(current, (current + daily_average) / 2)


def _history_rate_map(currency: Any, valuation: dict) -> Tuple[Optional[Dict[str, float]], Optional[str]]:
    if not isinstance(currency, str) or not currency.strip():
        return None, "缺少历史行情币种"
    code = currency.strip().upper()
    if code == CURRENCY_CNY:
        return {}, None
    rates: Dict[str, float] = {}
    for raw_code, raw_rate in _mapping(valuation.get("rates")).items():
        rate = _number(raw_rate)
        if isinstance(raw_code, str) and rate is not None and rate > 0:
            rates[raw_code.strip().upper()] = rate
    usd_rate = _number(valuation.get("usd_to_cny"))
    if code == CURRENCY_USD and usd_rate is not None and usd_rate > 0:
        rates[CURRENCY_USD] = usd_rate
    if rates.get(code) is None:
        return None, f"缺少 {code} 到 CNY 的记录汇率"
    return rates, None


def _cost_ratio(plan_price: Any, reference_price: Any) -> Optional[float]:
    plan = _number(plan_price)
    reference = _number(reference_price)
    if plan is None or plan <= 0 or reference is None or reference <= 0:
        return None
    return (plan / reference) * STEAM_FEE_FACTOR


def _evaluation_datetime(value: Any) -> datetime:
    """Normalize the public UNIX-timestamp contract to an aware UTC datetime."""

    if isinstance(value, bool):
        raise ValueError("evaluated_at must be a UNIX timestamp or timezone-aware datetime")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("evaluated_at datetime must be timezone-aware")
        return value.astimezone(timezone.utc)
    number = _number(value)
    if number is None:
        raise ValueError("evaluated_at must be a UNIX timestamp or timezone-aware datetime")
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("evaluated_at is outside the supported range") from exc


def candidate_keyword_match(
    name: Any,
    name_cn: Any,
    keywords: Any,
) -> Tuple[str, Optional[str], Optional[str]]:
    """Shared keyword rule for the formal candidate scan and replay.

    Returns ``(status, matched_keyword, error)`` where status is one of
    ``pass``, ``reject`` or ``unavailable``.  The caller supplies only the
    candidate names, never a source URL or full upstream response.
    """

    if not isinstance(keywords, list):
        return "unavailable", None, "缺少关键词排除参数"
    names = [value.lower() for value in (name, name_cn) if isinstance(value, str) and value]
    if not names:
        return "unavailable", None, "缺少候选名称，无法重算关键词过滤"
    for raw_keyword in keywords:
        if not isinstance(raw_keyword, str) or not raw_keyword:
            return "unavailable", None, "关键词排除参数格式无效"
        keyword = raw_keyword.lower()
        if any(keyword in candidate_name for candidate_name in names):
            return "reject", raw_keyword, None
    return "pass", None, None


def candidate_top_n_decision(
    rank: Any,
    source_total: Any,
    top_n: Any,
) -> Tuple[str, Optional[dict], Optional[int], Optional[str]]:
    """Shared 1-based top-N rule over the original candidate list.

    A frozen rank and source total are required even when N is zero, because a
    preview may raise N above zero.  "Already entered per-item analysis" is
    not evidence for this rule.
    """

    try:
        if isinstance(top_n, bool):
            raise ValueError
        result_top_n = int(top_n)
        if result_top_n < 0 or float(top_n) != result_top_n:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        return "unavailable", None, None, "缺少有效的 SteamDT 候选上限参数"

    rank_number = _number(rank)
    total_number = _number(source_total)
    if (
        rank_number is None
        or total_number is None
        or rank_number != int(rank_number)
        or total_number != int(total_number)
        or int(rank_number) < 1
        or int(total_number) < 1
        or int(rank_number) > int(total_number)
    ):
        return "unavailable", None, result_top_n, "缺少有效的候选排名或原始候选总数"

    actual = {
        "rank": int(rank_number),
        "source_total": int(total_number),
    }
    if result_top_n == 0 or actual["rank"] <= result_top_n:
        return "pass", actual, result_top_n, None
    return "reject", actual, result_top_n, None


def candidate_basic_filter_decision(
    item: Any,
    candidate: Any = None,
) -> Tuple[str, Optional[dict], Optional[str]]:
    """Shared basic candidate validation over safe item identifiers."""

    item_map = _mapping(item)
    candidate_map = _mapping(candidate)
    if "name" not in item_map or not isinstance(item_map.get("name"), str) or not item_map["name"].strip():
        return "unavailable", None, "缺少候选名称，无法执行基础校验"
    if "min_price" not in item_map:
        return "unavailable", None, "缺少候选价格，无法执行基础校验"
    if "goods_id" not in item_map:
        return "unavailable", None, "缺少 Buff 商品 ID，无法执行基础校验"

    raw_price = item_map.get("min_price")
    raw_goods_id = item_map.get("goods_id")
    price = _number(raw_price)
    goods_id = _number(raw_goods_id)
    actual = {
        "min_price": price,
        "goods_id": int(goods_id) if goods_id is not None and goods_id == int(goods_id) else raw_goods_id,
    }
    if price is None or price <= 0:
        return "reject", actual, "候选价格无效"
    if goods_id is None or goods_id != int(goods_id) or int(goods_id) <= 0:
        return "reject", actual, "缺少有效的 Buff 商品 ID"
    if "has_buff_link" not in candidate_map:
        return "unavailable", actual, "缺少候选 Buff 链接证据，无法执行基础校验"
    has_buff_link = candidate_map.get("has_buff_link")
    actual["has_buff_link"] = has_buff_link
    if has_buff_link is not True:
        return "reject", actual, "候选缺少有效的 Buff 商品链接"
    return "pass", actual, None


def _safe_reason(value: Any, fallback: str) -> str:
    """Only persist short, stable business reasons from external adapters."""

    if not isinstance(value, str):
        return fallback
    reason = " ".join(value.strip().split())
    lowered = reason.lower()
    blocked = (
        "http://", "https://", "cookie", "token", "secret", "password",
        "passwd", "authorization", "session", "credential", "pay_url",
        "payment_url",
    )
    if not reason or len(reason) > 160 or any(part in lowered for part in blocked):
        return fallback
    return reason


def _ordered_step_indices(strategy: Optional[dict]) -> Dict[str, int]:
    indices: Dict[str, int] = {}
    for index, step in enumerate(_steps(strategy)):
        module_id = str(step.get("module_id") or "")
        if module_id and module_id not in indices:
            indices[module_id] = index
    return indices


def _slot_steps(strategy: Optional[dict]) -> List[dict]:
    slots = []
    for step in _steps(strategy):
        if step.get("enabled") is False:
            slots.append(_step(
                str(step.get("module_id") or ""),
                "disabled",
                "模块未启用",
            ))
        else:
            slots.append(_step(
                str(step.get("module_id") or ""),
                "not_run",
                "前置步骤未通过，未执行",
            ))
    return slots


def _set_step(
    steps: List[dict],
    indices: Dict[str, int],
    module_id: str,
    status: str,
    reason: str,
    **kwargs: Any,
) -> None:
    index = indices.get(module_id)
    if index is None:
        return
    steps[index] = _step(module_id, status, reason, **kwargs)


def _get_step(steps: List[dict], indices: Dict[str, int], module_id: str) -> Optional[dict]:
    index = indices.get(module_id)
    return steps[index] if index is not None else None


def _blocking_result(steps: Sequence[dict]) -> Dict[str, Any]:
    blockers = {"reject", "wait", "unavailable", "unsupported"}
    status = next(
        (step.get("status") for step in steps if step.get("status") in blockers),
        "pass",
    )
    return {"status": status, "steps": list(steps)}


def _money_quantity(target_balance: Any, accumulated: Any, unit_price: Any) -> Optional[int]:
    if any(value is None for value in (target_balance, accumulated, unit_price)):
        return None
    try:
        price = Decimal(str(unit_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        target = Decimal(str(target_balance)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        spent = Decimal(str(accumulated)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None
    if price <= 0:
        return None
    remaining = target - spent
    if remaining <= 0:
        return 0
    return max(0, int(remaining // price))


def compute_purchase_limit(
    *,
    lowest_price: Any,
    count_at_lowest: Any,
    daily_volume: Any,
    target_balance: Any,
    accumulated_spend: Any,
    held_same_name: Any,
    hard_cap_enabled: bool,
    liquidity_cap_enabled: bool,
    low_price_guard_enabled: bool,
    held_same_guard_enabled: bool,
    hard_qty_cap: Any = 50,
    liquidity_ratio: Any = 0.05,
    low_price_threshold: Any = 5.0,
    low_price_penalty: Any = 0.5,
    low_price_hard_cap: Any = 30,
    target_balance_enabled: bool = True,
) -> Dict[str, Any]:
    """Compute the shared purchase quantity rules.

    This function is used by both the preview evaluator and the checkout
    boundary.  It only computes a plan; callers retain their own side effects
    and status mapping.  Missing data required by an enabled rule is reported
    as ``unavailable`` instead of silently becoming a pass.
    """

    price = _number(lowest_price)
    count = _number(count_at_lowest)
    if price is None or price <= 0:
        return {
            "status": "unavailable",
            "failed_rule": "input",
            "reason": "缺少有效的 Buff 实时最低价",
            "planned_quantity": None,
        }
    if count is None or count < 1:
        return {
            "status": "unavailable",
            "failed_rule": "input",
            "reason": "缺少有效的同价可购买数量",
            "planned_quantity": None,
        }
    count = int(count)

    low_threshold: Optional[float] = None
    low_penalty: Optional[float] = None
    low_cap: Optional[int] = None
    is_low_price = False
    if low_price_guard_enabled:
        low_threshold = _number(low_price_threshold)
        if low_threshold is None:
            return {
                "status": "unavailable",
                "failed_rule": "low_price",
                "reason": "缺少低价采购阈值",
                "planned_quantity": None,
            }
        is_low_price = price < low_threshold
        low_cap_number = _number(low_price_hard_cap)
        if low_cap_number is None:
            return {
                "status": "unavailable",
                "failed_rule": "low_price",
                "reason": "缺少低价采购硬上限",
                "planned_quantity": None,
            }
        low_cap = int(low_cap_number)

    caps: List[int] = []
    hard_cap: Optional[int] = None
    if hard_cap_enabled:
        hard_cap_number = _number(hard_qty_cap)
        if hard_cap_number is None:
            return {
                "status": "unavailable",
                "failed_rule": "hard",
                "reason": "缺少单品硬数量上限",
                "planned_quantity": None,
            }
        hard_cap = int(hard_cap_number)
        caps.append(hard_cap)

    liquidity_cap: Optional[int] = None
    if liquidity_cap_enabled:
        daily = _number(daily_volume)
        ratio = _number(liquidity_ratio)
        if daily is None or ratio is None:
            return {
                "status": "unavailable",
                "failed_rule": "liquidity",
                "reason": "缺少日销量或流动性比例",
                "planned_quantity": None,
            }
        liquidity_cap = int(daily * ratio)
        if low_price_guard_enabled and is_low_price:
            low_penalty = _number(low_price_penalty)
            if low_penalty is None:
                return {
                    "status": "unavailable",
                    "failed_rule": "low_price",
                    "reason": "缺少低价流动性惩罚系数",
                    "planned_quantity": None,
                }
            liquidity_cap = int(liquidity_cap * low_penalty)
        caps.append(liquidity_cap)

    if low_price_guard_enabled and is_low_price and low_cap is not None:
        caps.append(low_cap)

    limit_before_held = max(min(caps), 0) if caps else count
    held: Optional[int] = None
    if held_same_guard_enabled:
        held_number = _number(held_same_name)
        if held_number is None:
            return {
                "status": "unavailable",
                "failed_rule": "held",
                "reason": "缺少同名持仓数量",
                "planned_quantity": None,
            }
        held = max(0, int(held_number))
    limit_after_held = max(0, limit_before_held - (held or 0))

    affordable = count
    if target_balance_enabled:
        affordable = _money_quantity(target_balance, accumulated_spend, price)
        if affordable is None:
            return {
                "status": "unavailable",
                "failed_rule": "target",
                "reason": "缺少目标余额、本轮累计支出或有效单价",
                "planned_quantity": None,
            }

    if affordable < 1:
        status = "wait"
        reason = "剩余预算不足一件"
        planned_quantity = 0
    elif limit_after_held < 1:
        status = "reject"
        reason = "采购上限为 0"
        planned_quantity = 0
    else:
        status = "pass"
        reason = "采购数量计算完成"
        planned_quantity = min(count, affordable, limit_after_held)

    return {
        "status": status,
        "reason": reason,
        "lowest_price": price,
        "count_at_lowest": count,
        "low_price": is_low_price,
        "hard_cap": hard_cap,
        "liquidity_cap": liquidity_cap,
        "low_price_cap": low_cap,
        "safe_limit": limit_before_held,
        "held_same_name": held or 0 if held is not None else None,
        "limit_before_held": limit_before_held,
        "limit_after_held": limit_after_held,
        "affordable_quantity": affordable,
        "planned_quantity": planned_quantity,
    }


def _history_output(report: dict) -> dict:
    return {
        key: report.get(key)
        for key in (
            "status", "count", "avg", "cv", "r_squared", "slope",
            "price_percentile", "recent_percentile", "ma7", "ma30",
            "is_stable",
        )
        if key in report
    }


def _declarative_missing_path(module: dict, context: dict, outputs: dict, params: dict) -> bool:
    from app import strategy_engine as se

    data = {"params": params, "outputs": outputs, **context}
    for condition in module.get("conditions") or []:
        if not isinstance(condition, dict):
            return True
        left = se._resolve_path(condition.get("left"), data)
        if left is se._MISSING or left is None:
            return True
        if "right_path" in condition:
            right = se._resolve_path(condition.get("right_path"), data)
            if right is se._MISSING or right is None:
                return True
    return False


def _evaluate_buy(inputs: dict, strategy: dict, evaluated_at: datetime, modules: Optional[dict]) -> Dict[str, Any]:
    item = _mapping((inputs or {}).get("item"))
    market = _mapping((inputs or {}).get("market"))
    steam = _mapping(market.get("steam"))
    buff = _mapping(market.get("buff"))
    history = _mapping(market.get("history"))
    account = _mapping((inputs or {}).get("account"))
    valuation = _mapping((inputs or {}).get("valuation"))
    policy = _mapping((inputs or {}).get("policy"))
    module_defs = modules if isinstance(modules, dict) else {}

    indices = _ordered_step_indices(strategy)
    steps = _slot_steps(strategy)
    enabled = _enabled_ids(strategy)

    def blocked(module_id: str, status: str, reason: str, **kwargs: Any) -> bool:
        _set_step(steps, indices, module_id, status, reason, **kwargs)
        return True

    # Candidate-level source/filter checks are recomputed from the frozen safe
    # candidate context.  Entering per-item analysis is not evidence that a
    # temporarily changed top-N or keyword parameter passed.
    candidate = _mapping((inputs or {}).get("candidate"))
    if "buy.steamdt_top_n" in enabled:
        params = _params(strategy, "buy.steamdt_top_n")
        status, actual, threshold, reason = candidate_top_n_decision(
            candidate.get("rank"),
            candidate.get("source_total"),
            params.get("iflow_top_n"),
        )
        _set_step(
            steps, indices, "buy.steamdt_top_n", status,
            reason or (
                "候选排名处于 SteamDT 截取范围内"
                if status == "pass" else "候选排名超出 SteamDT 截取范围"
            ),
            actual=actual,
            threshold=threshold,
        )
        if status != "pass":
            return _blocking_result(steps)

    if "buy.exclude_keywords" in enabled:
        params = _params(strategy, "buy.exclude_keywords")
        status, matched_keyword, error = candidate_keyword_match(
            item.get("name"),
            candidate.get("name_cn"),
            params.get("exclude_keywords"),
        )
        _set_step(
            steps, indices, "buy.exclude_keywords", status,
            error or (
                f"候选名称命中排除关键词：{matched_keyword}"
                if status == "reject" else "候选名称未命中排除关键词"
            ),
            actual={"matched_keyword": matched_keyword} if matched_keyword else None,
        )
        if status != "pass":
            return _blocking_result(steps)

    if "buy.basic_candidate_filter" in enabled:
        status, actual, error = candidate_basic_filter_decision(item, candidate)
        _set_step(
            steps, indices, "buy.basic_candidate_filter", status,
            error or "候选基础字段有效",
            actual=actual,
        )
        if status != "pass":
            return _blocking_result(steps)

    steam_enabled = "buy.steam_sell_depth" in enabled
    pressure_enabled = "guard.sell_pressure" in enabled
    discount_enabled = "guard.max_discount" in enabled
    smart_price: Optional[float] = None
    reference_price: Optional[float] = None
    raw_ratio: Optional[float] = None
    adjusted_ratio: Optional[float] = None
    steam_orders_count = 0

    if steam_enabled:
        orders = _order_rows(steam.get("sell_orders"))
        steam_orders_count = len(orders)
        smart_price = _number(steam.get("smart_price"))
        if steam.get("available") is False or smart_price is None or smart_price <= 0 or not orders:
            blocked(
                "buy.steam_sell_depth", "unavailable",
                _safe_reason(steam.get("reason"), "Steam 卖单或智能参考价不可用"),
                output={"order_count": len(orders)},
            )
            return _blocking_result(steps)
        raw_ratio = _cost_ratio(item.get("min_price"), smart_price)
        _set_step(
            steps, indices, "buy.steam_sell_depth", "pass",
            "Steam 卖单与智能参考价可用",
            actual=smart_price,
            output={
                "smart_price": smart_price,
                "sell_orders_count": len(orders),
                "sell_pressure": compute_sell_pressure(
                    steam.get("sell_orders"),
                    item.get("daily_volume"),
                    _integer(
                        _params(strategy, "guard.sell_pressure").get("sell_pressure_orders_n"),
                        5,
                    ) or 5,
                ),
                "reference_price": smart_price,
                "estimated_ratio": raw_ratio,
            },
        )
    elif pressure_enabled or discount_enabled:
        target = "guard.sell_pressure" if pressure_enabled else "guard.max_discount"
        blocked(
            target, "unavailable",
            "Steam 卖单深度模块未启用，无法计算依赖规则",
        )
        return _blocking_result(steps)

    sell_pressure_value: Optional[float] = None
    if pressure_enabled:
        params = _params(strategy, "guard.sell_pressure")
        threshold = _number(params.get("sell_pressure_threshold"))
        if threshold is None:
            blocked("guard.sell_pressure", "unavailable", "缺少卖压阈值")
            return _blocking_result(steps)
        sell_pressure_value = compute_sell_pressure(
            steam.get("sell_orders"),
            item.get("daily_volume"),
            _integer(params.get("sell_pressure_orders_n"), 5) or 5,
        )
        if threshold <= 0 or _integer(item.get("daily_volume")) <= 0:
            _set_step(
                steps, indices, "guard.sell_pressure", "pass",
                "卖压阈值或日销量为 0，未形成卖压阻断",
                actual=sell_pressure_value,
                threshold=threshold,
                output={"sell_pressure": sell_pressure_value, "limit": threshold},
            )
        else:
            pressure = sell_pressure_value
            if pressure is None:
                blocked(
                    "guard.sell_pressure", "unavailable",
                    "无法从 Steam 卖单计算卖压",
                    threshold=threshold,
                )
                return _blocking_result(steps)
            status = "reject" if pressure > threshold else "pass"
            _set_step(
                steps, indices, "guard.sell_pressure", status,
                "卖压高于允许上限" if status == "reject" else "卖压未超过允许上限",
                actual=pressure,
                threshold=threshold,
                output={"sell_pressure": pressure, "limit": threshold},
            )
            if status == "reject":
                return _blocking_result(steps)

    max_discount: Optional[float] = None
    if discount_enabled:
        max_discount = _number(_params(strategy, "guard.max_discount").get("max_discount"))
        plan_price = _number(item.get("min_price"))
        raw_ratio = _cost_ratio(plan_price, smart_price)
        if max_discount is None:
            blocked("guard.max_discount", "unavailable", "缺少成本率上限")
            return _blocking_result(steps)
        if raw_ratio is None:
            blocked("guard.max_discount", "unavailable", "缺少有效购买价或 Steam 参考价，无法计算成本率", threshold=max_discount)
            return _blocking_result(steps)
        if raw_ratio >= max_discount:
            blocked(
                "guard.max_discount", "reject", "成本率高于或等于允许上限",
                actual=raw_ratio,
                threshold=max_discount,
                output={"estimated_ratio": raw_ratio, "limit": max_discount, "phase": "raw_price"},
            )
            return _blocking_result(steps)

        adjustment_rows = history.get("adjustment_rows", history.get("rows"))
        adjustment_currency = history.get("adjustment_currency", history.get("currency"))
        if _rows(adjustment_rows):
            _, rate_error = _history_rate_map(adjustment_currency, valuation)
            if rate_error:
                blocked(
                    "guard.max_discount", "unavailable",
                    f"{rate_error}，无法修正历史参考价",
                    threshold=max_discount,
                )
                return _blocking_result(steps)
        reference_price = adjust_reference_price(
            smart_price,
            adjustment_rows,
            currency=adjustment_currency,
            evaluated_at=evaluated_at,
            valuation=valuation,
        )
        adjusted_ratio = _cost_ratio(plan_price, reference_price)
        if adjusted_ratio is None:
            blocked("guard.max_discount", "unavailable", "缺少有效参考价，无法计算调整后成本率", threshold=max_discount)
            return _blocking_result(steps)
        if adjusted_ratio >= max_discount:
            blocked(
                "guard.max_discount", "reject", "按调整后参考价计算的成本率高于或等于上限",
                actual=adjusted_ratio,
                threshold=max_discount,
                output={
                    "estimated_ratio": adjusted_ratio,
                    "limit": max_discount,
                    "phase": "adjusted_reference",
                    "reference_price": reference_price,
                },
            )
            return _blocking_result(steps)
        _set_step(
            steps, indices, "guard.max_discount", "pass", "成本率低于允许上限",
            actual=adjusted_ratio,
            threshold=max_discount,
            output={
                "estimated_ratio": adjusted_ratio,
                "limit": max_discount,
                "phase": "adjusted_reference",
                "reference_price": reference_price,
            },
        )

    history_ids = [module_id for module_id in HISTORY_MODULE_IDS if module_id in enabled]
    if history_ids:
        history_rows = _rows(history.get("rows"))
        if history.get("available") is False or not history_rows:
            blocked(
                history_ids[0], "unavailable",
                _safe_reason(history.get("reason"), "缺少历史行情数据"),
            )
            return _blocking_result(steps)
        rate_map, rate_error = _history_rate_map(history.get("currency"), valuation)
        if rate_error:
            blocked(history_ids[0], "unavailable", rate_error)
            return _blocking_result(steps)

        legacy_enabled = "guard.history_stability" in enabled
        volatility_enabled = "guard.volatility_cv" in enabled
        trend_enabled = "guard.trend_quality" in enabled
        position_enabled = "guard.price_position" in enabled
        data_enabled = "guard.history_data_window" in enabled

        def history_param(key: str, default: Any) -> Any:
            for module_id in history_ids:
                params = _params(strategy, module_id)
                if key in params:
                    return params[key]
            return default

        days = max(1, _integer(history_param("days", 30), 30))
        use_vwap = bool(history_param("use_vwap", True))
        dynamic_position_ceil = (
            _number(history_param("price_percentile_ceil", 0.8))
            if position_enabled else 999.0
        )
        if (
            position_enabled
            and discount_enabled
            and adjusted_ratio is not None
            and max_discount is not None
        ):
            huge_offset = _number(policy.get("huge_profit_offset"))
            if huge_offset is None or huge_offset < 0:
                blocked("guard.price_position", "unavailable", "缺少巨额利润放宽参数")
                return _blocking_result(steps)
            huge_ratio = max_discount - huge_offset
            high_ratio = max_discount - (huge_offset / 2.0)
            if adjusted_ratio < huge_ratio:
                dynamic_position_ceil = 0.88
            elif adjusted_ratio < high_ratio:
                dynamic_position_ceil = max(dynamic_position_ceil or 0.0, 0.85)
        report = analyze_by_time(
            history_rows,
            days=days,
            currency=history.get("currency"),
            usd_to_cny=_number(valuation.get("usd_to_cny")) or 0.0,
            cv_threshold=_number(history_param("cv_threshold", 0.05)) if volatility_enabled else 999.0,
            r2_threshold=_number(history_param("r2_threshold", 0.6)) if trend_enabled else 2.0,
            min_daily_trades=_number(history_param("min_daily_trades", 5)) if data_enabled else 0,
            current_price=smart_price,
            price_percentile_ceil=dynamic_position_ceil if position_enabled else 999.0,
            r2_rising_threshold=_number(history_param("r2_rising_threshold", 0.8)) if trend_enabled else -1.0,
            slope_pct_ceil=_number(history_param("slope_pct_ceil", 0.01)) if trend_enabled else 999.0,
            ma_deviation_ceil=_number(history_param("ma_deviation_ceil", 1.1)) if position_enabled else 999.0,
            last_price_ma30_ceil=_number(history_param("last_price_ma30_ceil", 1.05)) if position_enabled else 999.0,
            slope_stable_floor=_number(history_param("slope_stable_floor", -0.005)) if trend_enabled else -999.0,
            price_percentile_ceil_rising=_number(history_param("price_percentile_ceil_rising", 0.5)) if position_enabled else 999.0,
            use_vwap=use_vwap,
            evaluated_at=evaluated_at,
            rate_map=rate_map,
        )
        if not report.get("valid"):
            blocked(
                history_ids[0], "unavailable",
                _safe_reason(report.get("msg"), "历史行情不足或分析失败"),
            )
            return _blocking_result(steps)

        if legacy_enabled:
            status = "pass" if report.get("is_stable") else "reject"
            _set_step(
                steps, indices, "guard.history_stability", status,
                "历史稳定性验证通过" if status == "pass" else _safe_reason(report.get("msg"), "历史稳定性未通过"),
                output=_history_output(report),
            )
            if status == "reject":
                return _blocking_result(steps)

        if data_enabled:
            _set_step(
                steps, indices, "guard.history_data_window", "pass",
                "历史窗口数据满足最低成交要求",
                actual=report.get("count"),
                threshold=history_param("min_daily_trades", 5),
                output=_history_output(report),
            )

        if volatility_enabled:
            threshold = _number(history_param("cv_threshold", 0.05))
            actual_cv = _number(report.get("cv"))
            status = "unavailable" if threshold is None or actual_cv is None else (
                "reject" if actual_cv > threshold else "pass"
            )
            _set_step(
                steps, indices, "guard.volatility_cv", status,
                "历史波动率过高" if status == "reject" else (
                    "历史波动率验证通过" if status == "pass" else "缺少波动率数据或阈值"
                ),
                actual=actual_cv,
                threshold=threshold,
            )
            if status != "pass":
                return _blocking_result(steps)

        if trend_enabled:
            threshold = _number(history_param("slope_stable_floor", -0.005))
            r2_rising = _number(history_param("r2_rising_threshold", 0.8))
            slope_ceil = _number(history_param("slope_pct_ceil", 0.01))
            slope = _number(report.get("slope"))
            r2 = _number(report.get("r_squared"))
            status = "pass"
            reason = "趋势质量验证通过"
            if report.get("status") == "FALLING":
                status, reason = "reject", "历史趋势下跌"
            elif report.get("status") == "RISING" and r2 is not None and r2_rising is not None and r2 > r2_rising and slope is not None and slope_ceil is not None and slope > slope_ceil:
                status, reason = "reject", "历史上涨斜率过高"
            elif report.get("status") == "STABLE" and threshold is not None and slope is not None and slope < threshold:
                status, reason = "reject", "稳定趋势斜率低于下限"
            _set_step(
                steps, indices, "guard.trend_quality", status, reason,
                actual={"status": report.get("status"), "slope": slope, "r_squared": r2},
                threshold={"r2": r2_rising, "slope_pct": slope_ceil, "stable_slope": threshold},
            )
            if status == "reject":
                return _blocking_result(steps)

        if position_enabled:
            percentile_ceil = dynamic_position_ceil
            rising_ceil = _number(history_param("price_percentile_ceil_rising", 0.5))
            ma_ceil = _number(history_param("ma_deviation_ceil", 1.1))
            last_ceil = _number(history_param("last_price_ma30_ceil", 1.05))
            percentile = _number(report.get("price_percentile"))
            recent = _number(report.get("recent_percentile"))
            ma7 = _number(report.get("ma7"))
            bb_upper = _number(report.get("bb_upper"))
            last_ratio = _number(report.get("last_price_ma30_ratio"))
            chosen_ceil = rising_ceil if report.get("status") == "RISING" else percentile_ceil
            exceeded = any((
                chosen_ceil is not None and percentile is not None and percentile > chosen_ceil,
                chosen_ceil is not None and recent is not None and recent > chosen_ceil,
                ma_ceil is not None and ma7 is not None and bb_upper is not None and ma7 > bb_upper,
                last_ceil is not None and last_ratio is not None and last_ratio > last_ceil,
            ))
            status = "reject" if exceeded else "pass"
            _set_step(
                steps, indices, "guard.price_position", status,
                "当前价格位置偏高" if exceeded else "价格位置验证通过",
                actual={
                    "price_percentile": percentile,
                    "recent_percentile": recent,
                    "last_price_ma30_ratio": last_ratio,
                },
                threshold={
                    "price_percentile": chosen_ceil,
                    "last_price_ma30": last_ceil,
                },
                output=_history_output(report),
            )
            if status == "reject":
                return _blocking_result(steps)

    # Declarative modules execute after the built-in checks and read the same
    # semantic outputs as the formal runtime, not UI-only step payloads.
    outputs: Dict[str, Any] = {}
    if steam_enabled:
        pressure_n = _integer(
            _params(strategy, "guard.sell_pressure").get("sell_pressure_orders_n"),
            5,
        ) or 5
        steam_pressure = compute_sell_pressure(
            steam.get("sell_orders"),
            item.get("daily_volume"),
            pressure_n,
        )
        if pressure_enabled:
            steam_pressure = sell_pressure_value
        outputs["buy.steam_sell_depth"] = {
            "smart_price": smart_price,
            "sell_orders_count": steam_orders_count,
            "sell_pressure": steam_pressure,
            "reference_price": reference_price if reference_price is not None else smart_price,
            "estimated_ratio": adjusted_ratio if adjusted_ratio is not None else raw_ratio,
        }
    if pressure_enabled:
        outputs["guard.sell_pressure"] = {
            "sell_pressure": sell_pressure_value,
            "limit": _number(_params(strategy, "guard.sell_pressure").get("sell_pressure_threshold")),
        }
    if discount_enabled:
        outputs["guard.max_discount"] = {
            "estimated_ratio": adjusted_ratio if adjusted_ratio is not None else raw_ratio,
            "limit": max_discount,
        }
    if history_ids:
        report_step = next(
            (step for step in steps if step.get("module_id") in HISTORY_MODULE_IDS and step.get("output")),
            None,
        )
        if report_step is not None:
            history_output = report_step.get("output")
            for module_id in HISTORY_MODULE_IDS:
                if module_id in enabled:
                    outputs[module_id] = dict(history_output)
    # Reconstruct only strategy-owned business config; never persist or load
    # credentials/full runtime config just to support declarative paths.
    business_config = {"pipeline": dict(policy), "stability": {}, "buff": {}}
    if "price_tolerance" in policy:
        business_config["buff"]["price_tolerance"] = policy["price_tolerance"]
    for definition in _steps(strategy):
        mid = definition.get("module_id") or ""
        if definition.get("enabled") is False or not mid.startswith(("buy.", "guard.")):
            continue
        section = "stability" if mid in HISTORY_MODULE_IDS else "pipeline"
        business_config[section].update(definition.get("params") or {})
    safe_context = {
        "config": business_config,
        "item": item,
        "market": {"buff": buff, "steam": steam, "history": history},
        "account": account,
        "valuation": valuation,
    }
    for step_def in _steps(strategy):
        module_id = str(step_def.get("module_id") or "")
        if step_def.get("enabled") is False or module_id not in enabled:
            continue
        module = _mapping(module_defs.get(module_id))
        is_builtin = module_id.startswith(("buy.", "guard.", "action.", "pricing.", "sell."))
        if module_id in {
            "buy.steamdt_top_n", "buy.exclude_keywords", "buy.basic_candidate_filter",
            "buy.buff_realtime_price", "buy.steam_sell_depth", "guard.sell_pressure",
            "guard.max_discount", *HISTORY_MODULE_IDS, *PURCHASE_LIMIT_MODULE_IDS,
            "guard.target_balance", "action.buff_lock_pay",
        }:
            continue
        from app import strategy_engine as se

        if not module or not se._is_declarative_user_module(module):
            blocked(module_id, "unsupported", "该模块不支持安全纯计算执行")
            return _blocking_result(steps)
        if not se._module_stage_matches(module, "buy", "buy.candidate_guard"):
            blocked(module_id, "unsupported", "该声明式模块不适用于买入候选检查")
            return _blocking_result(steps)

        params = step_def.get("params") if isinstance(step_def.get("params"), dict) else {}
        if not module.get("conditions") or _declarative_missing_path(module, safe_context, outputs, params):
            blocked(module_id, "unavailable", "声明式模块所需业务输入缺失")
            return _blocking_result(steps)

        evaluated = se._evaluate_declarative_module(
            module,
            params,
            context=safe_context,
            outputs=outputs,
        )
        status = str(evaluated.get("status") or "reject")
        if status == "error":
            status = "unsupported"
        _set_step(
            steps, indices, module_id, status,
            _safe_reason(evaluated.get("reason"), "声明式模块判断完成"),
            output=evaluated.get("output"),
        )
        outputs[module_id] = evaluated.get("output")
        if status != "pass":
            return _blocking_result(steps)

    buff_lowest: Optional[float] = None
    buff_count: Optional[int] = None
    if "buy.buff_realtime_price" in enabled:
        buff_orders = _order_rows(buff.get("orders"))
        buff_lowest = _number(buff.get("lowest_price"))
        buff_count = _integer(buff.get("count_at_lowest")) if buff.get("count_at_lowest") is not None else None
        if buff_lowest is None and buff_orders:
            buff_lowest = min(price for price, _ in buff_orders)
        if buff_count is None and buff_lowest is not None and buff_orders:
            buff_count = sum(quantity for price, quantity in buff_orders if price == buff_lowest)
        if buff.get("available") is False or buff_lowest is None or buff_lowest <= 0 or not buff_count:
            blocked(
                "buy.buff_realtime_price", "unavailable",
                _safe_reason(buff.get("reason"), "Buff 实时卖单不可用"),
                output={"lowest_price": buff_lowest, "order_count": len(buff_orders), "available": False},
            )
            return _blocking_result(steps)
        tolerance = _number(policy.get("price_tolerance"))
        if tolerance is not None and not buff_price_within_tolerance(buff_lowest, _number(item.get("min_price")), tolerance):
            blocked("buy.buff_realtime_price", "reject", "BUFF 实时价超过记录的价格容忍差额",
                    actual={"feed_price": item.get("min_price"), "lowest_price": buff_lowest}, threshold=tolerance)
            return _blocking_result(steps)
        realtime_ratio = None
        if discount_enabled and reference_price is not None:
            realtime_ratio = _cost_ratio(buff_lowest, reference_price)
            if realtime_ratio is None:
                blocked("buy.buff_realtime_price", "unavailable", "缺少 Buff 实时价或参考价")
                return _blocking_result(steps)
            if max_discount is not None and realtime_ratio >= max_discount:
                blocked(
                    "buy.buff_realtime_price", "reject",
                    "Buff 实时价未通过成本率复检",
                    actual=realtime_ratio,
                    threshold=max_discount,
                    output={"lowest_price": buff_lowest, "count_at_lowest": buff_count},
                )
                return _blocking_result(steps)
        _set_step(
            steps, indices, "buy.buff_realtime_price", "pass",
            "Buff 实时卖单与成本率复检通过",
            actual=buff_lowest,
            output={
                "lowest_price": buff_lowest,
                "order_count": len(buff_orders),
                "available": True,
                "count_at_lowest": buff_count,
                "estimated_ratio": realtime_ratio,
            },
        )

    limit_ids = [module_id for module_id in PURCHASE_LIMIT_MODULE_IDS if module_id in enabled]
    legacy_enabled = "guard.safe_purchase_limit" in enabled
    hard_enabled = legacy_enabled or "guard.purchase_hard_cap" in enabled
    liquidity_enabled = legacy_enabled or "guard.purchase_liquidity_cap" in enabled
    low_enabled = legacy_enabled or "guard.low_price_purchase_guard" in enabled
    held_enabled = legacy_enabled or "guard.held_same_item_guard" in enabled

    def limit_param(key: str, default: Any) -> Any:
        for module_id in (
            "guard.safe_purchase_limit",
            "guard.purchase_hard_cap",
            "guard.purchase_liquidity_cap",
            "guard.low_price_purchase_guard",
        ):
            if module_id not in enabled:
                continue
            params = _params(strategy, module_id)
            if key in params:
                return params[key]
        return default

    if limit_ids:
        if buff_lowest is None or not buff_count:
            blocked(limit_ids[0], "unavailable", "缺少 Buff 实时最低价，无法计算采购上限")
            return _blocking_result(steps)

        hard_id = "guard.safe_purchase_limit" if legacy_enabled else "guard.purchase_hard_cap"
        liquidity_id = "guard.safe_purchase_limit" if legacy_enabled else "guard.purchase_liquidity_cap"
        low_id = "guard.safe_purchase_limit" if legacy_enabled else "guard.low_price_purchase_guard"
        held_id = "guard.safe_purchase_limit" if legacy_enabled else "guard.held_same_item_guard"
        ratio = _number(limit_param("safe_purchase_liquidity_ratio", 0.05))
        low_threshold = _number(limit_param("safe_purchase_low_price_threshold", 5.0))
        limit_args = dict(
            lowest_price=buff_lowest,
            count_at_lowest=buff_count,
            daily_volume=item.get("daily_volume"),
            target_balance=account.get("target_balance"),
            accumulated_spend=account.get("accumulated_spend"),
            held_same_name=account.get("held_same_name"),
            hard_cap_enabled=hard_enabled,
            liquidity_cap_enabled=liquidity_enabled,
            low_price_guard_enabled=low_enabled,
            held_same_guard_enabled=held_enabled,
            hard_qty_cap=limit_param("safe_purchase_hard_qty_cap", 50),
            liquidity_ratio=ratio,
            low_price_threshold=low_threshold,
            low_price_penalty=limit_param("safe_purchase_low_price_penalty", 0.5),
            low_price_hard_cap=limit_param("safe_purchase_low_price_hard_cap", 30),
        )
        cap_plan = compute_purchase_limit(**limit_args, target_balance_enabled=False)
        if cap_plan["status"] == "unavailable":
            failed_rule = cap_plan.get("failed_rule")
            failed_id = {
                "hard": hard_id,
                "liquidity": liquidity_id,
                "low_price": low_id,
                "held": held_id,
            }.get(failed_rule, limit_ids[0])
            blocked(failed_id, "unavailable", str(cap_plan["reason"]))
            return _blocking_result(steps)

        if hard_enabled:
            hard_cap = cap_plan["hard_cap"]
            status = "reject" if hard_cap <= 0 else "pass"
            _set_step(
                steps, indices, hard_id, status,
                "单品硬数量上限为 0" if status == "reject" else "单品硬数量上限可用",
                actual=hard_cap,
                threshold=hard_cap,
            )
            if status == "reject":
                return _blocking_result(steps)

        if liquidity_enabled:
            volume_cap = cap_plan["liquidity_cap"]
            status = "reject" if volume_cap <= 0 else "pass"
            _set_step(
                steps, indices, liquidity_id, status,
                "流动性采购上限为 0" if status == "reject" else "流动性采购上限可用",
                actual=volume_cap,
                threshold=ratio,
            )
            if status == "reject":
                return _blocking_result(steps)

        if low_enabled:
            low_cap = cap_plan["low_price_cap"]
            is_low_price = cap_plan["low_price"]
            status = "reject" if is_low_price and low_cap <= 0 else "pass"
            _set_step(
                steps, indices, low_id, status,
                "低价采购上限为 0" if status == "reject" else (
                    "低价保护已收紧采购上限" if is_low_price else "当前价格未触发低价保护"
                ),
                actual={"lowest_price": buff_lowest, "low_price": is_low_price, "hard_cap": low_cap},
                threshold=low_threshold,
            )
            if status == "reject":
                return _blocking_result(steps)

        if held_enabled:
            held = cap_plan["held_same_name"]
            limit_after_held = cap_plan["limit_after_held"]
            status = "reject" if limit_after_held <= 0 else "pass"
            _set_step(
                steps, indices, held_id, status,
                "扣除同名持仓后采购上限为 0" if status == "reject" else "同名持仓已从采购上限扣除",
                actual={
                    "held_same_name": held,
                    "limit_before": cap_plan["limit_before_held"],
                    "limit_after": limit_after_held,
                },
            )
            if status == "reject":
                return _blocking_result(steps)

    if "guard.target_balance" in enabled:
        target = _number(account.get("target_balance"))
        accumulated = _number(account.get("accumulated_spend"))
        full_plan = compute_purchase_limit(
            lowest_price=buff_lowest,
            count_at_lowest=buff_count,
            daily_volume=item.get("daily_volume"),
            target_balance=target,
            accumulated_spend=accumulated,
            held_same_name=account.get("held_same_name"),
            hard_cap_enabled=(
                "guard.safe_purchase_limit" in enabled
                or "guard.purchase_hard_cap" in enabled
            ),
            liquidity_cap_enabled=(
                "guard.safe_purchase_limit" in enabled
                or "guard.purchase_liquidity_cap" in enabled
            ),
            low_price_guard_enabled=(
                "guard.safe_purchase_limit" in enabled
                or "guard.low_price_purchase_guard" in enabled
            ),
            held_same_guard_enabled=(
                "guard.safe_purchase_limit" in enabled
                or "guard.held_same_item_guard" in enabled
            ),
            hard_qty_cap=limit_param("safe_purchase_hard_qty_cap", 50),
            liquidity_ratio=limit_param("safe_purchase_liquidity_ratio", 0.05),
            low_price_threshold=limit_param("safe_purchase_low_price_threshold", 5.0),
            low_price_penalty=limit_param("safe_purchase_low_price_penalty", 0.5),
            low_price_hard_cap=limit_param("safe_purchase_low_price_hard_cap", 30),
            target_balance_enabled=True,
        )
        if full_plan["status"] == "unavailable":
            blocked("guard.target_balance", "unavailable", str(full_plan["reason"]))
            return _blocking_result(steps)
        affordable = full_plan["affordable_quantity"]
        if affordable < 1:
            blocked(
                "guard.target_balance", "wait", "剩余预算不足一件，等待或结束本轮",
                actual={"target_balance": target, "accumulated_spend": accumulated, "lowest_price": buff_lowest},
                threshold=target,
            )
            return _blocking_result(steps)
        planned_quantity = full_plan["planned_quantity"]
        if planned_quantity < 1:
            blocked(
                "guard.target_balance", "wait", "采购限制使可买数量为 0",
                actual={"affordable": affordable, "safe_limit": full_plan["limit_after_held"]},
            )
            return _blocking_result(steps)
        _set_step(
            steps, indices, "guard.target_balance", "pass",
            "本轮累计支出未达到目标余额",
            actual={
                "target_balance": target,
                "accumulated_spend": accumulated,
                "affordable_quantity": affordable,
                "planned_quantity": planned_quantity,
            },
            threshold=target,
        )

    if "action.buff_lock_pay" in enabled:
        _set_step(
            steps, indices, "action.buff_lock_pay", "not_run",
            "策略评估只解释规则，不执行锁单或付款",
        )

    # A configured module that could not be interpreted is never allowed to
    # become an implicit pass.
    for index, step in enumerate(steps):
        if step.get("status") == "not_run":
            module_id = step.get("module_id") or ""
            if module_id != "action.buff_lock_pay":
                _set_step(steps, indices, module_id, "unsupported", "该模块尚未接入纯计算评估")
                break

    return _blocking_result(steps)


def evaluate(
    inputs: dict,
    strategy: dict,
    evaluated_at: Any,
    modules: Optional[dict] = None,
) -> Dict[str, Any]:
    """Evaluate the buy rules against a frozen input snapshot.

    ``evaluated_at`` is a float UNIX timestamp, also accepting an aware
    datetime for compatibility with callers already using datetimes.
    """

    evaluation_time = _evaluation_datetime(evaluated_at)
    return _evaluate_buy(inputs, strategy, evaluation_time, modules)
