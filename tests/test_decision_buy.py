import copy
from datetime import datetime, timedelta, timezone

import pytest

from app.decision_buy import evaluate


def _cost_strategy(limit):
    return {
        "id": "test.buy.cost",
        "strategy_type": "buy",
        "steps": [
            {"module_id": "buy.steam_sell_depth", "enabled": True, "params": {}},
            {
                "module_id": "guard.sell_pressure",
                "enabled": True,
                "params": {
                    "sell_pressure_orders_n": 5,
                    "sell_pressure_threshold": 10,
                },
            },
            {
                "module_id": "guard.max_discount",
                "enabled": True,
                "params": {"max_discount": limit},
            },
            {
                "module_id": "guard.history_data_window",
                "enabled": True,
                "params": {"days": 30, "min_daily_trades": 5, "use_vwap": True},
            },
        ],
    }


def _cost_inputs():
    # 74 / 115 * 1.15 = 0.74; literal inputs keep the assertion independent
    # from the implementation formula.
    return {
        "item": {
            "name": "Test Item",
            "market_hash_name": "Test Item",
            "goods_id": 123,
            "min_price": 74.0,
            "daily_volume": 100,
            "ratio": 0.74,
        },
        "market": {
            "steam": {
                "available": True,
                "smart_price": 115.0,
                "sell_orders": [{"price": 115.0, "quantity": 1}],
            },
            "history": {"available": False, "reason": "not requested"},
        },
        "account": {},
        "valuation": {"usd_to_cny": 7.2},
    }


def test_cost_ratio_rejects_at_70_then_passes_at_75_without_inventing_history():
    sid = "system.buy.default"
    evaluated_at = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    original_inputs = _cost_inputs()

    rejected = evaluate(
        copy.deepcopy(original_inputs),
        _cost_strategy(0.70),
        evaluated_at,
        modules={},
    )
    cost_step = next(s for s in rejected["steps"] if s["module_id"] == "guard.max_discount")
    assert rejected["status"] == "reject"
    assert cost_step["status"] == "reject"
    assert cost_step["actual"] == pytest.approx(0.74)
    assert cost_step["threshold"] == pytest.approx(0.70)
    history_step = next(s for s in rejected["steps"] if s["module_id"] == "guard.history_data_window")
    assert history_step["status"] == "not_run"
    assert [s["module_id"] for s in rejected["steps"]] == [
        step["module_id"] for step in _cost_strategy(0.70)["steps"]
    ]

    relaxed = evaluate(
        copy.deepcopy(original_inputs),
        _cost_strategy(0.75),
        evaluated_at,
        modules={},
    )
    cost_step = next(s for s in relaxed["steps"] if s["module_id"] == "guard.max_discount")
    history_step = next(s for s in relaxed["steps"] if s["module_id"] == "guard.history_data_window")
    assert cost_step["status"] == "pass"
    assert history_step["status"] == "unavailable"
    assert relaxed["status"] == "unavailable"
    assert all(s["status"] != "pass" or s["module_id"] != "guard.history_data_window" for s in relaxed["steps"])


def test_evaluate_marks_disabled_steps_and_every_step_after_first_blocker_not_run():
    strategy = _cost_strategy(0.70)
    strategy["steps"].insert(1, {
        "module_id": "guard.disabled_example",
        "enabled": False,
        "params": {},
    })
    result = evaluate(_cost_inputs(), strategy, 1793188800.0, modules={})
    by_id = {step["module_id"]: step for step in result["steps"]}
    assert [step["module_id"] for step in result["steps"]] == [
        step["module_id"] for step in strategy["steps"]
    ]
    assert by_id["guard.disabled_example"]["status"] == "disabled"
    assert by_id["guard.max_discount"]["status"] == "reject"
    assert by_id["guard.history_data_window"]["status"] == "not_run"


def test_history_analysis_uses_recorded_exchange_rate_without_current_rates(monkeypatch):
    evaluated_at = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    rows = []
    for day in range(30):
        stamp = (evaluated_at - timedelta(days=day)).strftime("%b %d %Y %H: +0")
        rows.append([stamp, 100.0, "10"])
    inputs = {
        "item": {"name": "USD Item", "goods_id": 1, "min_price": 500.0},
        "market": {
            "history": {
                "available": True,
                "currency": "USD",
                "rows": rows,
            },
        },
        "valuation": {"usd_to_cny": 8.0},
    }
    strategy = {
        "id": "test.buy.usd-history",
        "strategy_type": "buy",
        "steps": [{
            "module_id": "guard.history_data_window",
            "enabled": True,
            "params": {"days": 30, "min_daily_trades": 5, "use_vwap": True},
        }],
    }
    monkeypatch.setattr(
        "utils.money._load_exchange_rates",
        lambda: pytest.fail("preview must not read current exchange rates"),
    )

    result = evaluate(inputs, strategy, evaluated_at.timestamp(), modules={})

    history_step = result["steps"][0]
    assert result["status"] == "pass"
    assert history_step["output"]["avg"] == pytest.approx(800.0)


@pytest.mark.parametrize(
    "history_currency,valuation,expected_reason",
    [
        (None, {"usd_to_cny": 8.0}, "缺少历史行情币种"),
        ("USD", {}, "缺少 USD 到 CNY 的记录汇率"),
        ("EUR", {"rates": {"USD": 8.0}}, "缺少 EUR 到 CNY 的记录汇率"),
    ],
)
def test_history_missing_currency_or_recorded_rate_is_unavailable_without_current_rates(
    monkeypatch,
    history_currency,
    valuation,
    expected_reason,
):
    evaluated_at = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    history = {
        "available": True,
        "rows": [
            [f"Oct {day:02d} 2026 12: +0", 100.0, "10"]
            for day in range(1, 31)
        ],
    }
    if history_currency is not None:
        history["currency"] = history_currency
    strategy = {
        "id": "test.buy.missing-history-rate",
        "strategy_type": "buy",
        "steps": [
            {
                "module_id": "guard.history_data_window",
                "enabled": True,
                "params": {"days": 30, "min_daily_trades": 5},
            },
            {
                "module_id": "guard.volatility_cv",
                "enabled": True,
                "params": {"cv_threshold": 0.05},
            },
        ],
    }
    monkeypatch.setattr(
        "utils.money._load_exchange_rates",
        lambda: pytest.fail("missing recorded rates must not load current rates"),
    )

    result = evaluate(
        {
            "item": {"name": "Foreign Item", "goods_id": 1, "min_price": 500.0},
            "market": {"history": history},
            "valuation": valuation,
        },
        strategy,
        evaluated_at.timestamp(),
        modules={},
    )

    assert result["status"] == "unavailable"
    assert result["steps"][0]["module_id"] == "guard.history_data_window"
    assert result["steps"][0]["status"] == "unavailable"
    assert expected_reason in result["steps"][0]["reason"]
    assert result["steps"][1]["status"] == "not_run"


def test_cost_adjustment_without_history_currency_is_unavailable_not_cny(monkeypatch):
    evaluated_at = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    inputs = _cost_inputs()
    inputs["market"]["history"] = {
        "available": True,
        "rows": [[f"Oct 28 2026 {hour:02d}: +0", 100.0, "10"] for hour in range(24)],
    }
    strategy = _cost_strategy(0.80)
    monkeypatch.setattr(
        "utils.money._load_exchange_rates",
        lambda: pytest.fail("missing history currency must not load current rates"),
    )

    result = evaluate(inputs, strategy, evaluated_at.timestamp(), modules={})

    by_id = {step["module_id"]: step for step in result["steps"]}
    assert result["status"] == "unavailable"
    assert by_id["guard.max_discount"]["status"] == "unavailable"
    assert "缺少历史行情币种" in by_id["guard.max_discount"]["reason"]
    assert by_id["guard.history_data_window"]["status"] == "not_run"


def test_history_analysis_uses_the_frozen_evaluation_instant():
    from analysis.stability import analyze_by_time

    history = [
        [f"Jan {day:02d} 2025 00: +0", 100.0 + day, "10"]
        for day in range(1, 11)
    ]
    report = analyze_by_time(
        history,
        days=30,
        min_daily_trades=0,
        evaluated_at=datetime(2025, 1, 15, tzinfo=timezone.utc).timestamp(),
    )
    assert report["valid"] is True
    assert report["count"] == 10


def test_stability_analyzer_forwards_recorded_rates_without_loading_current_rates(monkeypatch):
    from app.services.analysis_client import StabilityAnalyzer

    evaluated_at = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    history = [
        [(evaluated_at - timedelta(days=day)).strftime("%b %d %Y %H: +0"), 10.0, "10"]
        for day in range(30)
    ]
    monkeypatch.setattr(
        "utils.money._load_exchange_rates",
        lambda: pytest.fail("recorded rate map must not fall back to current rates"),
    )

    report = StabilityAnalyzer().analyze(
        history,
        days=30,
        currency="EUR",
        min_daily_trades=0,
        evaluated_at=evaluated_at.timestamp(),
        rate_map={"EUR": 9.0},
    )

    assert report["valid"] is True
    assert report["avg"] == pytest.approx(90.0)


@pytest.mark.parametrize(
    "currency,rate_map,expected_reason",
    [
        (None, {"USD": 8.0}, "缺少历史行情币种"),
        ("USD", {"EUR": 9.0}, "缺少 USD 到 CNY 的记录汇率"),
    ],
)
def test_stability_analyzer_does_not_default_missing_recorded_rates(
    monkeypatch,
    currency,
    rate_map,
    expected_reason,
):
    from app.services.analysis_client import StabilityAnalyzer

    evaluated_at = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    history = [
        [(evaluated_at - timedelta(days=day)).strftime("%b %d %Y %H: +0"), 10.0, "10"]
        for day in range(30)
    ]
    monkeypatch.setattr(
        "utils.money._load_exchange_rates",
        lambda: pytest.fail("missing recorded rates must not load current rates"),
    )

    report = StabilityAnalyzer().analyze(
        history,
        days=30,
        currency=currency,
        min_daily_trades=0,
        evaluated_at=evaluated_at.timestamp(),
        rate_map=rate_map,
    )

    assert report["valid"] is False
    assert expected_reason in report["msg"]


def _candidate_strategy(*, exclude_keywords=None, top_n=50):
    return {
        "id": "test.buy.candidate",
        "strategy_type": "buy",
        "steps": [
            {
                "module_id": "buy.steamdt_top_n",
                "enabled": True,
                "params": {"iflow_top_n": top_n},
            },
            {
                "module_id": "buy.exclude_keywords",
                "enabled": True,
                "params": {"exclude_keywords": list(exclude_keywords or [])},
            },
            {
                "module_id": "buy.basic_candidate_filter",
                "enabled": True,
                "params": {},
            },
        ],
    }


def _candidate_inputs():
    return {
        "item": {
            "name": "AK-47 | Redline",
            "market_hash_name": "AK-47 | Redline (Field-Tested)",
            "goods_id": 123,
            "assetid": None,
            "min_price": 74.0,
            "daily_volume": 100,
            "ratio": 0.74,
        },
        "candidate": {
            "rank": 60,
            "source_total": 200,
            "name_cn": "AK-47 | 红线",
            "has_buff_link": True,
        },
        "market": {},
        "account": {},
        "valuation": {"usd_to_cny": 7.2},
    }


def test_candidate_keyword_filter_recomputes_against_temporary_params():
    inputs = _candidate_inputs()
    original = _candidate_strategy(exclude_keywords=["印花"], top_n=100)
    changed = _candidate_strategy(exclude_keywords=["Redline"], top_n=100)

    original_result = evaluate(copy.deepcopy(inputs), original, 1793188800.0, modules={})
    changed_result = evaluate(copy.deepcopy(inputs), changed, 1793188800.0, modules={})

    original_step = next(s for s in original_result["steps"] if s["module_id"] == "buy.exclude_keywords")
    changed_step = next(s for s in changed_result["steps"] if s["module_id"] == "buy.exclude_keywords")
    assert original_result["status"] == "pass"
    assert original_step["status"] == "pass"
    assert changed_result["status"] == "reject"
    assert changed_step["status"] == "reject"
    basic_step = next(s for s in changed_result["steps"] if s["module_id"] == "buy.basic_candidate_filter")
    assert basic_step["status"] == "not_run"


def test_candidate_top_n_recomputes_from_frozen_rank():
    inputs = _candidate_inputs()
    rejected = evaluate(
        copy.deepcopy(inputs),
        _candidate_strategy(top_n=50),
        1793188800.0,
        modules={},
    )
    relaxed = evaluate(
        copy.deepcopy(inputs),
        _candidate_strategy(top_n=100),
        1793188800.0,
        modules={},
    )

    rejected_step = next(s for s in rejected["steps"] if s["module_id"] == "buy.steamdt_top_n")
    relaxed_step = next(s for s in relaxed["steps"] if s["module_id"] == "buy.steamdt_top_n")
    assert rejected["status"] == "reject"
    assert rejected_step["actual"] == {"rank": 60, "source_total": 200}
    assert rejected_step["threshold"] == 50
    assert relaxed["status"] == "pass"
    assert relaxed_step["status"] == "pass"


def test_candidate_top_n_without_rank_is_unavailable_and_stops_safely():
    inputs = _candidate_inputs()
    del inputs["candidate"]["rank"]
    result = evaluate(inputs, _candidate_strategy(top_n=50), 1793188800.0, modules={})

    top_step = next(s for s in result["steps"] if s["module_id"] == "buy.steamdt_top_n")
    assert result["status"] == "unavailable"
    assert top_step["status"] == "unavailable"
    assert [s["status"] for s in result["steps"][1:]] == ["not_run", "not_run"]


def test_basic_candidate_filter_rejects_invalid_price_and_missing_identity():
    invalid_price = _candidate_inputs()
    invalid_price["item"]["min_price"] = 0
    result = evaluate(
        invalid_price,
        _candidate_strategy(top_n=100),
        1793188800.0,
        modules={},
    )
    basic_step = next(s for s in result["steps"] if s["module_id"] == "buy.basic_candidate_filter")
    assert result["status"] == "reject"
    assert basic_step["status"] == "reject"
    assert basic_step["actual"] == {"min_price": 0.0, "goods_id": 123}

    missing_identity = _candidate_inputs()
    del missing_identity["item"]["goods_id"]
    result = evaluate(
        missing_identity,
        _candidate_strategy(top_n=100),
        1793188800.0,
        modules={},
    )
    basic_step = next(s for s in result["steps"] if s["module_id"] == "buy.basic_candidate_filter")
    assert result["status"] == "unavailable"
    assert basic_step["status"] == "unavailable"


def test_basic_candidate_filter_requires_buff_link_evidence():
    inputs = _candidate_inputs()
    del inputs["candidate"]["has_buff_link"]
    result = evaluate(
        inputs,
        _candidate_strategy(top_n=100),
        1793188800.0,
        modules={},
    )

    basic_step = next(s for s in result["steps"] if s["module_id"] == "buy.basic_candidate_filter")
    assert result["status"] == "unavailable"
    assert basic_step["status"] == "unavailable"


def _declarative_outputs_strategy():
    return {
        "id": "test.buy.outputs",
        "strategy_type": "buy",
        "steps": [
            {"module_id": "buy.steam_sell_depth", "enabled": True, "params": {}},
            {
                "module_id": "guard.sell_pressure",
                "enabled": True,
                "params": {
                    "sell_pressure_orders_n": 5,
                    "sell_pressure_threshold": 2.0,
                },
            },
            {
                "module_id": "guard.max_discount",
                "enabled": True,
                "params": {"max_discount": 0.9},
            },
            {"module_id": "user.buy.output_guard", "enabled": True, "params": {}},
        ],
    }


def _declarative_outputs_module():
    return {
        "id": "user.buy.output_guard",
        "name": "Output guard",
        "origin": "user",
        "module_kind": "declarative",
        "stages": ["buy.candidate_guard"],
        "logic": "all",
        "fail_status": "reject",
        "message": "required outputs present",
        "conditions": [
            {
                "left": "outputs.buy.steam_sell_depth.estimated_ratio",
                "op": "lte",
                "right_path": "outputs.guard.max_discount.limit",
            },
            {
                "left": "outputs.guard.sell_pressure.sell_pressure",
                "op": "lte",
                "right_path": "outputs.guard.sell_pressure.limit",
            },
            {
                "left": "outputs.buy.steam_sell_depth.reference_price",
                "op": "gte",
                "value": 1,
            },
        ],
    }


def _declarative_item_module():
    return {
        "id": "user.buy.item_guard",
        "name": "Item guard",
        "origin": "user",
        "module_kind": "declarative",
        "stages": ["buy.candidate_guard"],
        "logic": "all",
        "fail_status": "reject",
        "message": "daily volume sufficient",
        "conditions": [{"left": "item.daily_volume", "op": "gte", "value": 100}],
    }


def test_declarative_module_missing_context_is_unavailable_not_pass():
    inputs = _candidate_inputs()
    del inputs["item"]["daily_volume"]
    strategy = _candidate_strategy(top_n=100)
    strategy["steps"].append({
        "module_id": "user.buy.item_guard",
        "enabled": True,
        "params": {},
    })

    result = evaluate(
        inputs,
        strategy,
        1793188800.0,
        modules={"user.buy.item_guard": _declarative_item_module()},
    )

    guard = next(s for s in result["steps"] if s["module_id"] == "user.buy.item_guard")
    assert result["status"] == "unavailable"
    assert guard["status"] == "unavailable"
    assert "缺失" in guard["reason"]


def test_declarative_module_reads_semantic_outputs_from_preceding_builtins():
    inputs = _cost_inputs()
    inputs["item"]["daily_volume"] = 100
    inputs["item"]["min_price"] = 74
    inputs["market"]["steam"]["sell_orders"] = [{"price": 115, "quantity": 2}]
    result = evaluate(
        inputs,
        _declarative_outputs_strategy(),
        1793188800.0,
        modules={"user.buy.output_guard": _declarative_outputs_module()},
    )

    assert result["status"] == "pass"
    by_id = {step["module_id"]: step for step in result["steps"]}
    steam_output = by_id["buy.steam_sell_depth"]["output"]
    assert steam_output["estimated_ratio"] == pytest.approx(0.74)
    assert steam_output["sell_pressure"] == pytest.approx(0.02)
    assert steam_output["reference_price"] == pytest.approx(115.0)
    assert by_id["guard.max_discount"]["output"]["limit"] == pytest.approx(0.9)
    assert by_id["guard.sell_pressure"]["output"]["limit"] == pytest.approx(2.0)
    assert by_id["user.buy.output_guard"]["status"] == "pass"


def _history_inputs_with_huge_profit():
    evaluated_at = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    history = []
    for day in range(30):
        stamp = (evaluated_at - timedelta(days=day)).strftime("%b %d %Y %H: +0")
        price = 100.0 if day % 2 == 0 else 118.0
        history.append([stamp, price, "10"])
    return {
        "item": {
            "name": "History Item",
            "market_hash_name": "History Item",
            "goods_id": 456,
            "min_price": 80.0,
            "daily_volume": 100,
            "ratio": 0.8,
        },
        "market": {
            "steam": {
                "available": True,
                "smart_price": 115.0,
                "sell_orders": [{"price": 115.0, "quantity": 1}],
            },
            "history": {
                "available": True,
                "currency": "CNY",
                "rows": history,
            },
        },
        "account": {},
        "valuation": {"usd_to_cny": 7.2},
        "policy": {"huge_profit_offset": 0.05},
    }


def test_history_price_position_uses_same_huge_profit_relaxation_as_formal_flow():
    strategy = {
        "id": "test.buy.history-relaxation",
        "strategy_type": "buy",
        "steps": [
            {"module_id": "buy.steam_sell_depth", "enabled": True, "params": {}},
            {
                "module_id": "guard.max_discount",
                "enabled": True,
                "params": {"max_discount": 0.9},
            },
            {
                "module_id": "guard.price_position",
                "enabled": True,
                "params": {
                    "price_percentile_ceil": 0.8,
                    "price_percentile_ceil_rising": 0.5,
                    "ma_deviation_ceil": 1.1,
                    "last_price_ma30_ceil": 1.05,
                },
            },
        ],
    }
    evaluated_at = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    result = evaluate(
        _history_inputs_with_huge_profit(),
        strategy,
        evaluated_at,
        modules={},
    )

    position = next(s for s in result["steps"] if s["module_id"] == "guard.price_position")
    assert result["status"] == "pass"
    assert position["status"] == "pass"
    assert position["threshold"]["price_percentile"] == pytest.approx(0.88)

    missing_policy = _history_inputs_with_huge_profit()
    del missing_policy["policy"]
    unavailable = evaluate(missing_policy, strategy, evaluated_at, modules={})
    position = next(s for s in unavailable["steps"] if s["module_id"] == "guard.price_position")
    assert unavailable["status"] == "unavailable"
    assert position["status"] == "unavailable"


@pytest.mark.parametrize(
    "case,expected",
    [
        ("quantity", 4),
        ("hard", 7),
        ("liquidity", 5),
        ("low_price", 2),
        ("budget", 2),
    ],
)
def test_purchase_limit_shared_with_real_checkout_boundary(monkeypatch, case, expected):
    from app import pipeline_steps as steps
    from app.decision_buy import compute_purchase_limit
    from app.services import buff_checkout_guard

    monkeypatch.setattr(buff_checkout_guard, "_GUARD_PATH", __import__("pathlib").Path("/tmp") / f"decision-buy-{case}.json")
    lowest_price = 4.9 if case == "low_price" else 10.0
    daily_volume = 1000 if case == "hard" else 100
    held = 1 if case == "quantity" else 0
    target = 25.0 if case == "budget" else 100.0
    accumulated = 0.0
    count_at_lowest = 20
    orders = [
        {"id": f"sell-{index}", "price": str(lowest_price)}
        for index in range(count_at_lowest)
    ]
    enabled = {
        "quantity": ["guard.purchase_hard_cap", "guard.purchase_liquidity_cap", "guard.held_same_item_guard"],
        "hard": ["guard.purchase_hard_cap"],
        "liquidity": ["guard.purchase_liquidity_cap"],
        "low_price": ["guard.purchase_liquidity_cap", "guard.low_price_purchase_guard"],
        "budget": ["guard.target_balance"],
    }[case]
    config = {
        "buff": {"game": "csgo", "pay_method": "wechat", "price_tolerance": 0.5},
        "pipeline": {
            "buff_sell_orders_cache_ttl_seconds": 3,
            "safe_purchase_hard_qty_cap": 7,
            "safe_purchase_liquidity_ratio": 0.05,
            "safe_purchase_low_price_threshold": 5,
            "safe_purchase_low_price_penalty": 0.5,
            "safe_purchase_low_price_hard_cap": 30,
        },
        "_strategy_runtime": {"buy": {"enabled_modules": enabled}},
    }
    item = {
        "name": "Test Item",
        "steam_market_name": "Test Item",
        "goods_id": 123,
        "min_price": lowest_price,
        "daily_volume": daily_volume,
        "_buff_lowest_price": lowest_price,
        "_buff_sell_orders": orders,
        "_buff_sell_orders_fetched_at": __import__("time").time(),
    }
    monkeypatch.setattr(steps, "get_purchases", lambda: [{"name": "Test Item"}] * held)
    monkeypatch.setattr(steps, "_fetch_steam_sell_data", lambda *_a, **_k: None)

    preview = compute_purchase_limit(
        lowest_price=lowest_price,
        count_at_lowest=count_at_lowest,
        daily_volume=daily_volume,
        target_balance=target,
        accumulated_spend=accumulated,
        held_same_name=held,
        hard_cap_enabled="guard.purchase_hard_cap" in enabled,
        liquidity_cap_enabled="guard.purchase_liquidity_cap" in enabled,
        low_price_guard_enabled="guard.low_price_purchase_guard" in enabled,
        held_same_guard_enabled="guard.held_same_item_guard" in enabled,
        hard_qty_cap=7,
        liquidity_ratio=0.05,
        low_price_threshold=5,
        low_price_penalty=0.5,
        low_price_hard_cap=30,
    )
    assert preview["status"] == "pass"
    assert preview["planned_quantity"] == expected

    writes = []
    class Client:
        _pay_method = "wechat"
        supports_batch_buy = True

        def try_batch_buy(self, _goods, _game, _orders, _price, quantity, **_kwargs):
            writes.append(quantity)
            # Deliberately leave the write result unknown after observing the
            # quantity.  A safe fallback would correctly send a single-order
            # write, which this boundary test must not touch.
            return {"success": False, "created": None, "code": "unknown"}

        def lock_and_get_pay_url(self, *_args, **_kwargs):
            pytest.fail("batch safety probe must not fall through to a real single write")

    pending = []
    with pytest.raises(steps.PurchaseWriteResultUnknown):
        steps.lock_and_confirm_payment(
            Client(),
            item,
            config,
            target_balance=target,
            acc=accumulated,
            set_pending_payment=pending.append,
            wait_payment_confirm=lambda **_kwargs: False,
            confirm_payment=lambda _ok, **_kwargs: None,
            is_stop_requested=lambda: False,
            append_purchase=lambda _value: pytest.fail("no purchase should be recorded"),
        )

    assert writes == [expected]
