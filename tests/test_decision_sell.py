from copy import deepcopy

from unittest.mock import MagicMock


def _strategy():
    return {
        "id": "system.sell.immediate",
        "strategy_type": "sell",
        "pipeline": {
            "sell_strategy": 1,
            "max_listings_per_item": 5,
            "sell_price_wall_volume": 20,
            "sell_price_max_ignore_volume": 4,
            "sell_price_offset": 0,
            "sell_trend_days": 7,
            "profit_ratio_multiplier": 1.05,
        },
        "steps": [
            {"module_id": "sell.sellable_inventory_filter", "enabled": True, "params": {}},
            {"module_id": "guard.max_listings_per_item", "enabled": True, "params": {"max_listings_per_item": 5}},
            {"module_id": "pricing.steam_wall_price", "enabled": True, "params": {"sell_price_wall_volume": 20, "sell_price_max_ignore_volume": 4}},
            {"module_id": "pricing.price_offset", "enabled": True, "params": {"sell_price_offset": 0}},
            {"module_id": "action.steam_list", "enabled": True, "params": {}},
        ],
    }


def _modules():
    return {
        "sell.sellable_inventory_filter": {
            "id": "sell.sellable_inventory_filter",
            "name": "可出售库存",
            "strategy_types": ["sell"],
        },
        "guard.max_listings_per_item": {
            "id": "guard.max_listings_per_item",
            "name": "同名在售上限",
            "strategy_types": ["sell"],
        },
        "pricing.steam_wall_price": {
            "id": "pricing.steam_wall_price",
            "name": "价格墙定价",
            "strategy_types": ["sell"],
        },
        "pricing.price_offset": {
            "id": "pricing.price_offset",
            "name": "上架价补偿",
            "strategy_types": ["sell"],
        },
        "action.steam_list": {
            "id": "action.steam_list",
            "name": "Steam 上架",
            "strategy_types": ["sell"],
        },
    }


def _inputs():
    return {
        "item": {
            "name": "AK-47 | Redline",
            "market_hash_name": "AK-47 | Redline",
            "goods_id": 123,
            "assetid": "987654",
        },
        "inventory": {"can_sell": True, "appid": 730},
        "ownership": {
            "eligible": True,
            "purchase_id": 42,
            "purchase_price_cny": 12.34,
            "market_price_cny_at_purchase": 18.0,
        },
        "listing": {"same_name_count": 1, "round_same_name_count": 0, "source": "steam"},
        "market": {
            "source": "steam_market",
            "captured_at": 1793260800,
            "currency": "CNY",
            "available": True,
            "sell_orders": [
                {"price": 20.0, "quantity": 3},
                {"price": 21.0, "quantity": 3},
                {"price": 22.0, "quantity": 50},
            ],
            "error": None,
        },
        "trend": {
            "source": "steam_history",
            "captured_at": 1793260800,
            "window_days": 7,
            "available": True,
            "change": -0.02,
            "points": [],
        },
        "currency": {"code": "CNY", "cny_per_account": 1.0},
    }


def test_evaluate_passes_listing_with_shared_pricing_and_no_io(monkeypatch):
    from app.decision_sell import evaluate

    def forbidden(*_args, **_kwargs):
        raise AssertionError("pure sell decision must not use network or current clock")

    monkeypatch.setattr("time.time", forbidden)
    monkeypatch.setattr("steam.market_orders.get_sell_orders_cny", forbidden)

    result = evaluate(
        deepcopy(_inputs()),
        deepcopy(_strategy()),
        evaluated_at=1793260800,
        modules=deepcopy(_modules()),
    )

    assert result["status"] == "pass"
    assert [step["module_id"] for step in result["steps"]] == [
        step["module_id"] for step in _strategy()["steps"]
    ]
    pricing = result["steps"][2]
    assert pricing["status"] == "pass"
    assert pricing["output"]["list_price"] == 21.99
    assert pricing["output"]["reason"] == "单档无断层"
    assert result["steps"][-1]["status"] == "pass"
    assert "不提交" in result["steps"][-1]["reason"]


def test_early_stop_keeps_every_later_step_as_not_run_and_disabled_is_explicit():
    from app.decision_sell import evaluate

    strategy = _strategy()
    strategy["steps"][1]["params"]["max_listings_per_item"] = 1
    strategy["steps"][2]["enabled"] = False
    inputs = _inputs()
    inputs["listing"]["same_name_count"] = 1

    result = evaluate(inputs, strategy, evaluated_at=1793260800.1, modules=_modules())

    assert result["status"] == "reject"
    assert [step["module_id"] for step in result["steps"]] == [
        step["module_id"] for step in strategy["steps"]
    ]
    assert [step["status"] for step in result["steps"]] == [
        "pass", "reject", "disabled", "not_run", "not_run",
    ]
    assert "未启用" in result["steps"][2]["reason"]
    assert all("前置" in step["reason"] for step in result["steps"][3:])


def test_missing_market_data_blocks_pricing_and_following_actions_without_fetching():
    from app.decision_sell import evaluate

    inputs = _inputs()
    inputs["market"].update({"available": False, "sell_orders": [], "error": "timeout"})

    result = evaluate(inputs, _strategy(), evaluated_at=1793260800, modules=_modules())

    assert result["status"] == "unavailable"
    assert [step["status"] for step in result["steps"]] == [
        "pass", "pass", "unavailable", "not_run", "not_run",
    ]
    assert "timeout" in result["steps"][2]["reason"]


def test_trend_wait_keeps_action_explicitly_not_run():
    from app.decision_sell import evaluate

    strategy = {
        "id": "system.sell.trend",
        "strategy_type": "sell",
        "pipeline": {"sell_trend_days": 7},
        "steps": [
            {"module_id": "sell.sellable_inventory_filter", "enabled": True, "params": {}},
            {"module_id": "guard.max_listings_per_item", "enabled": True, "params": {}},
            {"module_id": "pricing.steam_wall_price", "enabled": True, "params": {}},
            {"module_id": "pricing.price_offset", "enabled": True, "params": {}},
            {"module_id": "guard.rising_trend_wait", "enabled": True, "params": {}},
            {"module_id": "action.steam_list", "enabled": True, "params": {}},
        ],
    }
    modules = {**(_modules()), "guard.rising_trend_wait": {
        "id": "guard.rising_trend_wait", "strategy_types": ["sell"],
    }}
    inputs = _inputs()
    inputs["trend"].update({"currency": "CNY", "change": 0.08})

    result = evaluate(inputs, strategy, evaluated_at=1793260800, modules=modules)

    assert result["status"] == "wait"
    assert [step["status"] for step in result["steps"]] == [
        "pass", "pass", "pass", "pass", "wait", "not_run",
    ]
    assert result["steps"][-1]["module_id"] == "action.steam_list"


def test_trend_policy_uses_raw_history_and_parameter_window_at_latest_sample():
    from app.decision_sell import evaluate

    strategy = _strategy()
    strategy["steps"].insert(4, {
        "module_id": "guard.rising_trend_wait",
        "enabled": True,
        "params": {"sell_trend_days": 7},
    })
    modules = {
        **_modules(),
        "guard.rising_trend_wait": {
            "id": "guard.rising_trend_wait",
            "strategy_types": ["sell"],
        },
    }
    newest = 1793260800
    day = 86400
    inputs = _inputs()
    inputs["trend"] = {
        "source": "steam_history",
        "captured_at": newest,
        "available": True,
        "currency": "CNY",
        "points": [
            {"captured_at": newest - 20 * day, "price": 1.0, "volume": 1},
            {"captured_at": newest - 10 * day, "price": 2.0, "volume": 1},
            {"captured_at": newest - 5 * day, "price": 3.0, "volume": 1},
            {"captured_at": newest, "price": 4.0, "volume": 1},
        ],
    }

    short = evaluate(inputs, strategy, evaluated_at=newest, modules=modules)

    assert short["status"] == "pass"
    assert short["steps"][4]["output"]["trend_days"] == 7

    strategy["steps"][4]["params"]["sell_trend_days"] = 30
    long = evaluate(inputs, strategy, evaluated_at=newest, modules=modules)

    assert long["status"] == "wait"
    assert long["steps"][4]["status"] == "wait"
    assert long["steps"][4]["output"]["trend_days"] == 30


def test_trend_preview_excludes_points_after_evaluated_at_and_keeps_input_immutable():
    from app.decision_sell import evaluate

    strategy = _strategy()
    strategy["steps"].insert(4, {
        "module_id": "guard.rising_trend_wait",
        "enabled": True,
        "params": {"sell_trend_days": 7},
    })
    modules = {
        **_modules(),
        "guard.rising_trend_wait": {
            "id": "guard.rising_trend_wait",
            "strategy_types": ["sell"],
        },
    }
    evaluated_at = 1793260800
    day = 86400
    inputs = _inputs()
    inputs["trend"] = {
        "source": "steam_history",
        "captured_at": evaluated_at + day,
        "available": True,
        "currency": "CNY",
        "points": [
            {"captured_at": evaluated_at - 10 * day, "price": 2.0, "volume": 1},
            {"captured_at": evaluated_at - 5 * day, "price": 3.0, "volume": 1},
            {"captured_at": evaluated_at, "price": 4.0, "volume": 1},
            {"captured_at": evaluated_at + day, "price": 100.0, "volume": 1},
        ],
    }
    original = deepcopy(inputs)

    result = evaluate(inputs, strategy, evaluated_at=evaluated_at, modules=modules)

    assert result["status"] == "pass"
    assert result["steps"][4]["status"] == "pass"
    assert result["steps"][4]["actual"]["sample_count"] == 2
    assert inputs == original


def test_trend_source_currency_is_converted_to_cny_before_dynamic_threshold():
    from app.decision_sell import evaluate

    strategy = _strategy()
    strategy["steps"].insert(4, {
        "module_id": "guard.rising_trend_wait",
        "enabled": True,
        "params": {"sell_trend_days": 7},
    })
    modules = {
        **_modules(),
        "guard.rising_trend_wait": {
            "id": "guard.rising_trend_wait",
            "strategy_types": ["sell"],
        },
    }
    evaluated_at = 1793260800
    day = 86400
    inputs = _inputs()
    inputs["currency"] = {"code": "USD", "cny_per_account": 7.2}
    inputs["trend"] = {
        "source": "steam_history",
        "captured_at": evaluated_at,
        "available": True,
        "currency": "USD",
        "points": [
            {"captured_at": evaluated_at - 2 * day, "price": 1.0, "volume": 1},
            {"captured_at": evaluated_at - day, "price": 1.01, "volume": 1},
            {"captured_at": evaluated_at, "price": 1.02, "volume": 1},
        ],
    }
    original = deepcopy(inputs)

    result = evaluate(inputs, strategy, evaluated_at=evaluated_at, modules=modules)

    assert result["status"] == "wait"
    trend_step = result["steps"][4]
    assert trend_step["status"] == "wait"
    assert trend_step["actual"]["change"] == 1
    assert trend_step["actual"]["cny_per_trend_unit"] == 7.2
    assert inputs == original


def test_trend_replay_uses_recorded_rate_without_loading_current_exchange_file(monkeypatch):
    from app.decision_sell import evaluate

    strategy = _strategy()
    strategy["steps"].insert(4, {
        "module_id": "guard.rising_trend_wait",
        "enabled": True,
        "params": {"sell_trend_days": 7},
    })
    modules = {
        **_modules(),
        "guard.rising_trend_wait": {"id": "guard.rising_trend_wait"},
    }
    evaluated_at = 1793260800
    day = 86400
    inputs = _inputs()
    inputs["currency"] = {"code": "CNY", "cny_per_account": 1.0}
    inputs["trend"] = {
        "source": "steam_history",
        "captured_at": evaluated_at,
        "available": True,
        "currency": "USD",
        "cny_per_currency": 8.45,
        "points": [
            {"captured_at": evaluated_at - 2 * day, "price": 1.0, "volume": 1},
            {"captured_at": evaluated_at - day, "price": 1.01, "volume": 1},
            {"captured_at": evaluated_at, "price": 1.02, "volume": 1},
        ],
    }

    def forbidden(*_args, **_kwargs):
        raise AssertionError("decision replay must never read current exchange rates")

    monkeypatch.setattr("utils.money._load_exchange_rates", forbidden)
    monkeypatch.setattr("utils.money.load_exchange_rates", forbidden)

    result = evaluate(inputs, strategy, evaluated_at=evaluated_at, modules=modules)

    assert result["status"] == "wait"
    assert result["steps"][4]["actual"]["cny_per_trend_unit"] == 8.45


def test_trend_without_currency_is_unavailable_instead_of_silently_assuming_cny():
    from app.decision_sell import evaluate

    strategy = _strategy()
    strategy["steps"].insert(4, {
        "module_id": "guard.rising_trend_wait",
        "enabled": True,
        "params": {"sell_trend_days": 7},
    })
    modules = {
        **_modules(),
        "guard.rising_trend_wait": {"id": "guard.rising_trend_wait"},
    }
    evaluated_at = 1793260800
    inputs = _inputs()
    inputs["trend"] = {
        "source": "steam_history",
        "captured_at": evaluated_at,
        "available": True,
        "points": [
            {"captured_at": evaluated_at - 2 * 86400, "price": 1.0, "volume": 1},
            {"captured_at": evaluated_at - 86400, "price": 1.01, "volume": 1},
            {"captured_at": evaluated_at, "price": 1.02, "volume": 1},
        ],
    }

    result = evaluate(inputs, strategy, evaluated_at=evaluated_at, modules=modules)

    assert result["status"] == "unavailable"
    assert result["steps"][4]["status"] == "unavailable"
    assert "币种" in result["steps"][4]["reason"]
    assert result["steps"][5]["status"] == "not_run"


def test_trend_after_evaluation_is_unavailable_without_reusing_old_change():
    from app.decision_sell import evaluate

    strategy = _strategy()
    strategy["steps"].insert(4, {
        "module_id": "guard.rising_trend_wait",
        "enabled": True,
        "params": {"sell_trend_days": 7},
    })
    modules = {
        **_modules(),
        "guard.rising_trend_wait": {"id": "guard.rising_trend_wait"},
    }
    evaluated_at = 1793260800
    inputs = _inputs()
    inputs["trend"] = {
        "source": "steam_history",
        "captured_at": evaluated_at + 86400,
        "available": True,
        "currency": "CNY",
        "change": -1,
        "points": [{
            "captured_at": evaluated_at + 86400,
            "price": 20.0,
            "volume": 1,
        }],
    }

    result = evaluate(inputs, strategy, evaluated_at=evaluated_at, modules=modules)

    assert result["status"] == "unavailable"
    assert result["steps"][4]["status"] == "unavailable"
    assert result["steps"][4]["actual"]["sample_count"] == 0
    assert result["steps"][5]["status"] == "not_run"


def test_usd_display_price_uses_cny_per_account_without_treating_cny_as_usd():
    from app.decision_sell import evaluate

    inputs = _inputs()
    inputs["currency"] = {"code": "USD", "cny_per_account": 7.2}

    result = evaluate(inputs, _strategy(), evaluated_at=1793260800, modules=_modules())

    assert result["status"] == "pass"
    currency_evidence = result["steps"][3]
    assert currency_evidence["actual"]["cny_per_account"] == 7.2
    assert currency_evidence["output"]["list_price_cny"] == 21.99
    assert currency_evidence["output"]["display_price"] == 3.05
    assert currency_evidence["output"]["currency"] == "USD"
    assert result["steps"][-1]["output"]["display_price"] == 3.05


def test_unknown_and_external_modules_are_unsupported_not_passed():
    from app.decision_sell import evaluate

    strategy = _strategy()
    strategy["steps"].insert(2, {
        "module_id": "user.external_guard", "enabled": True, "params": {},
    })
    modules = {
        **_modules(),
        "user.external_guard": {
            "id": "user.external_guard",
            "origin": "user",
            "module_kind": "external_code",
            "strategy_types": ["sell"],
            "stages": ["sell.listing_guard"],
            "code": "run()",
        },
    }

    result = evaluate(_inputs(), strategy, evaluated_at=1793260800, modules=modules)

    assert result["status"] == "unsupported"
    assert [step["status"] for step in result["steps"]] == [
        "pass", "pass", "unsupported", "not_run", "not_run", "not_run",
    ]
    assert "不支持" in result["steps"][2]["reason"]


def test_declarative_sell_guard_runs_before_action_and_blocks_it():
    from app.decision_sell import evaluate

    strategy = _strategy()
    strategy["steps"].insert(4, {
        "module_id": "user.min_profit", "enabled": True, "params": {"minimum": 30},
    })
    modules = {
        **_modules(),
        "user.min_profit": {
            "id": "user.min_profit",
            "name": "最低价保护",
            "origin": "user",
            "module_kind": "declarative",
            "strategy_types": ["sell"],
            "stages": ["sell.listing_guard"],
            "effect": "guard",
            "logic": "all",
            "fail_status": "reject",
            "message": "建议价低于临时最低价",
            "conditions": [{
                "left": "listing.list_price",
                "op": "gte",
                "right_path": "params.minimum",
            }],
        },
    }

    result = evaluate(_inputs(), strategy, evaluated_at=1793260800, modules=modules)

    assert result["status"] == "reject"
    assert result["steps"][4]["status"] == "reject"
    assert result["steps"][4]["reason"] == "建议价低于临时最低价"
    assert result["steps"][5]["status"] == "not_run"


def test_reprice_strategy_is_never_treated_as_ordinary_sell_preview():
    from app.decision_sell import evaluate

    strategy = {
        "id": "system.sell.trend",
        "strategy_type": "reprice",
        "steps": [{"module_id": "guard.rising_trend_wait", "enabled": True, "params": {}}],
    }

    result = evaluate(_inputs(), strategy, evaluated_at=1793260800, modules={})

    assert result["status"] == "unsupported"
    assert result["steps"] == [{
        "module_id": "guard.rising_trend_wait",
        "status": "unsupported",
        "reason": "自动调价仅支持解释记录，不支持作为普通上架策略预演",
    }]


def test_listing_plan_uses_shared_evaluator_and_captures_safe_context(monkeypatch):
    import sys
    import types
    from unittest.mock import MagicMock

    from app import sell_pipeline

    capture_calls, snapshot_calls = [], []
    contract = types.ModuleType("app.decision_records")

    def snapshot_strategy(config, kind):
        snapshot_calls.append((deepcopy(config), kind))
        return deepcopy(_strategy()), deepcopy(_modules())

    def capture_decision(config, kind, item, inputs, result, evaluated_at=None):
        capture_calls.append({
            "config": deepcopy(config),
            "kind": kind,
            "item": deepcopy(item),
            "inputs": deepcopy(inputs),
            "result": deepcopy(result),
            "evaluated_at": evaluated_at,
        })
        return "decision-1"

    contract.snapshot_strategy = snapshot_strategy
    contract.capture_decision = capture_decision
    monkeypatch.setitem(sys.modules, "app.decision_records", contract)

    ctx = MagicMock()
    ctx.is_stop_requested.return_value = False
    monkeypatch.setattr(
        sell_pipeline,
        "get_sell_orders_cny",
        lambda *_a, **_kw: {
            "sell_orders": [
                {"price": 20.0, "quantity": 3},
                {"price": 21.0, "quantity": 3},
                {"price": 22.0, "quantity": 50},
            ]
        },
    )

    plan = sell_pipeline._build_listing_plan(
        ctx=ctx,
        cfg={"pipeline": {"sell_strategy": 1}, "steam": {"cookies": "must-not-leak"}},
        session=object(),
        sellable=[{
            "name": "AK-47 | Redline",
            "market_hash_name": "AK-47 | Redline",
            "assetid": "987654",
            "appid": 730,
            "can_sell": True,
        }],
        sell_strategy=1,
        pipeline_cfg={"sell_strategy": 1, "max_listings_per_item": 5},
        purchases_snapshot=[{
            "_db_id": 42,
            "assetid": "987654",
            "market_hash_name": "AK-47 | Redline",
            "name": "AK-47 | Redline",
            "price": 12.34,
            "market_price": 18.0,
        }],
        ok_listings=True,
        active_listing_ids=set(),
        listing_assetid_to_name={},
        assetid_to_name_map={},
        account_currency="CNY",
        rate_map={"CNY": 1.0},
    )

    assert [entry["list_price"] for entry in plan] == [21.99]
    assert snapshot_calls and snapshot_calls[0][1] == "sell"
    assert len(capture_calls) == 1
    captured = capture_calls[0]
    assert captured["kind"] == "sell"
    assert set(captured["item"]) == {"name", "market_hash_name", "goods_id", "assetid"}
    assert captured["item"]["assetid"] == "987654"
    assert set(captured["inputs"]) == {
        "item", "inventory", "ownership", "listing", "market", "trend", "currency",
    }
    assert "cookies" not in repr(captured)
    assert captured["result"]["status"] == "pass"
    assert captured["result"]["steps"][-1]["module_id"] == "action.steam_list"


def test_live_listing_capture_preserves_dated_history_and_source_currency(monkeypatch):
    import sys
    import types
    from datetime import datetime, timezone

    from app import sell_pipeline
    from app.services.steam_client import SteamClient

    evaluated_at = datetime(2026, 10, 28, 2, tzinfo=timezone.utc).timestamp()
    strategy = _strategy()
    strategy["steps"].insert(4, {
        "module_id": "guard.rising_trend_wait",
        "enabled": True,
        "params": {"sell_trend_days": 7},
    })
    modules = {
        **_modules(),
        "guard.rising_trend_wait": {"id": "guard.rising_trend_wait"},
    }
    capture_calls = []
    contract = types.ModuleType("app.decision_records")
    contract.snapshot_strategy = lambda config, kind: (deepcopy(strategy), deepcopy(modules))
    contract.capture_decision = lambda config, kind, item, inputs, result, evaluated_at=None: (
        capture_calls.append({
            "config": deepcopy(config),
            "kind": kind,
            "item": deepcopy(item),
            "inputs": deepcopy(inputs),
            "result": deepcopy(result),
            "evaluated_at": evaluated_at,
        }) or "decision-history"
    )
    monkeypatch.setitem(sys.modules, "app.decision_records", contract)
    monkeypatch.setattr(sell_pipeline.time, "time", lambda: evaluated_at)
    monkeypatch.setattr(sell_pipeline, "get_current_account", lambda: {"id": "account-a"})
    monkeypatch.setattr(
        sell_pipeline,
        "get_sell_orders_cny",
        lambda *_a, **_kw: {"sell_orders": [
            {"price": 20.0, "quantity": 3},
            {"price": 21.0, "quantity": 3},
            {"price": 22.0, "quantity": 50},
        ]},
    )
    monkeypatch.setattr(
        SteamClient,
        "fetch_history",
        lambda self, *_a, **_kw: {
            "history": [
                ["Oct 25 2026 01: +0", 1.0, "10"],
                ["Oct 26 2026 01: +0", 0.99, "11"],
                ["Oct 27 2026 01: +0", 0.98, "12"],
                ["Oct 28 2026 01: +0", 0.97, "13"],
            ],
            "currency": "USD",
        },
    )
    monkeypatch.setattr("utils.money.load_exchange_rates", lambda: {"USD": 7.2})
    ctx = MagicMock()
    ctx.is_stop_requested.return_value = False

    plan = sell_pipeline._build_listing_plan(
        ctx=ctx,
        cfg={"pipeline": {"sell_strategy": 2}},
        session=object(),
        sellable=[{
            "name": "AK-47 | Redline",
            "market_hash_name": "AK-47 | Redline",
            "assetid": "987654",
            "appid": 730,
            "can_sell": True,
        }],
        sell_strategy=2,
        pipeline_cfg={"sell_strategy": 2, "max_listings_per_item": 5},
        purchases_snapshot=[{
            "_db_id": 42,
            "assetid": "987654",
            "market_hash_name": "AK-47 | Redline",
            "name": "AK-47 | Redline",
            "price": 12.34,
            "market_price": 18.0,
        }],
        ok_listings=True,
        active_listing_ids=set(),
        listing_assetid_to_name={},
        assetid_to_name_map={},
        account_currency="USD",
        rate_map={"USD": 7.2},
    )

    assert [entry["list_price"] for entry in plan] == [21.99]
    assert len(capture_calls) == 1
    trend = capture_calls[0]["inputs"]["trend"]
    assert trend["currency"] == "USD"
    assert trend["source_currency"] == "USD"
    assert trend["currency_source"] == "history"
    assert trend["cny_per_currency"] == 7.2
    assert trend["exchange_rate_source"] == "exchange_rate_file"
    assert trend["points"] == [
        {
            "captured_at": datetime(2026, 10, day, 1, tzinfo=timezone.utc).timestamp(),
            "price": price,
            "volume": volume,
        }
        for day, price, volume in [
            (25, 1.0, 10),
            (26, 0.99, 11),
            (27, 0.98, 12),
            (28, 0.97, 13),
        ]
    ]
    assert capture_calls[0]["result"]["steps"][4]["actual"]["sample_count"] == 4


def test_shared_evaluator_failure_is_recorded_as_unavailable_not_success(monkeypatch):
    import sys
    import types

    from app import sell_pipeline

    strategy = _strategy()
    modules = _modules()
    capture_calls = []
    contract = types.ModuleType("app.decision_records")
    contract.snapshot_strategy = lambda config, kind: (deepcopy(strategy), deepcopy(modules))
    contract.capture_decision = lambda config, kind, item, inputs, result, evaluated_at=None: (
        capture_calls.append(deepcopy(result)) or "decision-failed"
    )
    monkeypatch.setitem(sys.modules, "app.decision_records", contract)
    monkeypatch.setattr(sell_pipeline, "_evaluate_listing", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(
        sell_pipeline,
        "get_sell_orders_cny",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not fetch after evaluator failure")),
    )
    ctx = MagicMock()
    ctx.is_stop_requested.return_value = False

    plan = sell_pipeline._build_listing_plan(
        ctx=ctx,
        cfg={"pipeline": {"sell_strategy": 1}},
        session=object(),
        sellable=[{
            "name": "AK-47 | Redline",
            "market_hash_name": "AK-47 | Redline",
            "assetid": "987654",
            "appid": 730,
            "can_sell": True,
        }],
        sell_strategy=1,
        pipeline_cfg={"sell_strategy": 1, "max_listings_per_item": 5},
        purchases_snapshot=[{
            "_db_id": 42,
            "assetid": "987654",
            "market_hash_name": "AK-47 | Redline",
            "name": "AK-47 | Redline",
            "price": 12.34,
            "market_price": 18.0,
        }],
        ok_listings=True,
        active_listing_ids=set(),
        listing_assetid_to_name={},
        assetid_to_name_map={},
        account_currency="CNY",
        rate_map={"CNY": 1.0},
    )

    assert plan == []
    assert len(capture_calls) == 1
    result = capture_calls[0]
    assert result["status"] == "unavailable"
    assert [step["module_id"] for step in result["steps"]] == [
        step["module_id"] for step in strategy["steps"]
    ]
    assert result["steps"][0]["status"] == "unavailable"
    assert all(step["status"] == "not_run" for step in result["steps"][1:])
    assert result["steps"][-1]["module_id"] == "action.steam_list"


def test_each_live_item_binds_decision_account_before_its_requests(monkeypatch):
    import sys
    import types

    from app import sell_pipeline

    strategy = _strategy()
    modules = _modules()
    capture_calls = []
    contract = types.ModuleType("app.decision_records")
    contract.snapshot_strategy = lambda config, kind: (deepcopy(strategy), deepcopy(modules))
    contract.capture_decision = lambda config, kind, item, inputs, result, evaluated_at=None: (
        capture_calls.append({
            "account_id": config.get("_decision_account_id"),
            "assetid": item.get("assetid"),
        }) or "decision-account"
    )
    monkeypatch.setitem(sys.modules, "app.decision_records", contract)

    current = {"id": "account-a"}
    monkeypatch.setattr(sell_pipeline, "get_current_account", lambda: deepcopy(current))
    request_count = {"value": 0}

    def fetch_orders(*_args, **_kwargs):
        request_count["value"] += 1
        if request_count["value"] == 1:
            current["id"] = "account-b"
        return {"sell_orders": [
            {"price": 20.0, "quantity": 3},
            {"price": 21.0, "quantity": 3},
            {"price": 22.0, "quantity": 50},
        ]}

    monkeypatch.setattr(sell_pipeline, "get_sell_orders_cny", fetch_orders)
    ctx = MagicMock()
    ctx.is_stop_requested.return_value = False

    plan = sell_pipeline._build_listing_plan(
        ctx=ctx,
        cfg={"pipeline": {"sell_strategy": 1}},
        session=object(),
        sellable=[
            {"name": "AK-47 | Redline", "market_hash_name": "AK-47 | Redline",
             "assetid": "asset-a", "appid": 730, "can_sell": True},
            {"name": "AK-47 | Redline", "market_hash_name": "AK-47 | Redline",
             "assetid": "asset-b", "appid": 730, "can_sell": True},
        ],
        sell_strategy=1,
        pipeline_cfg={"sell_strategy": 1, "max_listings_per_item": 5},
        purchases_snapshot=[
            {"_db_id": 1, "assetid": "asset-a", "market_hash_name": "AK-47 | Redline",
             "name": "AK-47 | Redline", "price": 12.34, "market_price": 18.0},
            {"_db_id": 2, "assetid": "asset-b", "market_hash_name": "AK-47 | Redline",
             "name": "AK-47 | Redline", "price": 12.34, "market_price": 18.0},
        ],
        ok_listings=True,
        active_listing_ids=set(),
        listing_assetid_to_name={},
        assetid_to_name_map={},
        account_currency="CNY",
        rate_map={"CNY": 1.0},
    )

    assert [entry["aid"] for entry in plan] == ["asset-a", "asset-b"]
    assert capture_calls == [
        {"account_id": "account-a", "assetid": "asset-a"},
        {"account_id": "account-b", "assetid": "asset-b"},
    ]


def test_live_trend_wait_uses_final_evaluator_blocker_not_stale_missing_data(monkeypatch):
    import sys
    import types

    from app import sell_pipeline

    evaluated_at = 1793260800.0
    strategy = _strategy()
    strategy["steps"].insert(4, {
        "module_id": "guard.rising_trend_wait",
        "enabled": True,
        "params": {"sell_trend_days": 7},
    })
    modules = {**_modules(), "guard.rising_trend_wait": {"id": "guard.rising_trend_wait"}}
    captured = []
    decision_sink = []
    contract = types.ModuleType("app.decision_records")
    contract.snapshot_strategy = lambda config, kind: (deepcopy(strategy), deepcopy(modules))
    contract.capture_decision = lambda config, kind, item, inputs, result, evaluated_at=None: (
        captured.append(deepcopy(result)) or "decision-trend"
    )
    monkeypatch.setitem(sys.modules, "app.decision_records", contract)
    monkeypatch.setattr(sell_pipeline.time, "time", lambda: evaluated_at)
    monkeypatch.setattr(
        sell_pipeline,
        "get_sell_orders_cny",
        lambda *_a, **_kw: {"sell_orders": [
            {"price": 20.0, "quantity": 3},
            {"price": 21.0, "quantity": 3},
            {"price": 22.0, "quantity": 50},
        ]},
    )
    monkeypatch.setattr(
        sell_pipeline,
        "_steam_latest_price_and_trend",
        lambda *_a, **_kw: (21.99, 1, {
            "currency": "CNY",
            "source_currency": "CNY",
            "points": [
                {"captured_at": evaluated_at - 2 * 86400, "price": 10.0, "volume": 1},
                {"captured_at": evaluated_at - 86400, "price": 10.2, "volume": 1},
                {"captured_at": evaluated_at, "price": 10.4, "volume": 1},
            ],
        }),
    )
    ctx = MagicMock()
    ctx.is_stop_requested.return_value = False

    plan = sell_pipeline._build_listing_plan(
        ctx=ctx,
        cfg={"pipeline": {"sell_strategy": 2}},
        session=object(),
        sellable=[{
            "name": "AK-47 | Redline", "market_hash_name": "AK-47 | Redline",
            "assetid": "987654", "appid": 730, "can_sell": True,
        }],
        sell_strategy=2,
        pipeline_cfg={"sell_strategy": 2, "max_listings_per_item": 5, "sell_trend_days": 7},
        purchases_snapshot=[{
            "_db_id": 42, "assetid": "987654", "market_hash_name": "AK-47 | Redline",
            "name": "AK-47 | Redline", "price": 12.34, "market_price": 18.0,
        }],
        ok_listings=True,
        active_listing_ids=set(),
        listing_assetid_to_name={},
        assetid_to_name_map={},
        account_currency="CNY",
        rate_map={"CNY": 1.0},
        decision_sink=decision_sink,
    )

    assert plan == []
    assert captured[0]["status"] == "wait"
    assert captured[0]["steps"][4]["status"] == "wait"
    assert decision_sink == captured
    skip_logs = [str(call.args[0]) for call in ctx.log.call_args_list if call.args]
    assert any("价格上涨，暂缓出售" in message for message in skip_logs)
    assert not any("历史行情不可用" in message for message in skip_logs)


def test_capture_failure_warns_but_does_not_change_live_listing(monkeypatch):
    import sys
    import types

    from app import sell_pipeline

    contract = types.ModuleType("app.decision_records")
    contract.snapshot_strategy = lambda config, kind: (
        deepcopy(_strategy()), deepcopy(_modules())
    )
    contract.capture_decision = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(RuntimeError("record store unavailable"))
    )
    monkeypatch.setitem(sys.modules, "app.decision_records", contract)
    monkeypatch.setattr(sell_pipeline, "get_current_account", lambda: {"id": "account-a"})
    monkeypatch.setattr(
        sell_pipeline,
        "get_sell_orders_cny",
        lambda *_a, **_kw: {"sell_orders": [
            {"price": 20.0, "quantity": 3},
            {"price": 21.0, "quantity": 3},
            {"price": 22.0, "quantity": 50},
        ]},
    )
    ctx = MagicMock()
    ctx.is_stop_requested.return_value = False

    plan = sell_pipeline._build_listing_plan(
        ctx=ctx,
        cfg={"pipeline": {"sell_strategy": 1}},
        session=object(),
        sellable=[{
            "name": "AK-47 | Redline", "market_hash_name": "AK-47 | Redline",
            "assetid": "987654", "appid": 730, "can_sell": True,
        }],
        sell_strategy=1,
        pipeline_cfg={"sell_strategy": 1, "max_listings_per_item": 5},
        purchases_snapshot=[{
            "_db_id": 42, "assetid": "987654", "market_hash_name": "AK-47 | Redline",
            "name": "AK-47 | Redline", "price": 12.34, "market_price": 18.0,
        }],
        ok_listings=True,
        active_listing_ids=set(),
        listing_assetid_to_name={},
        assetid_to_name_map={},
        account_currency="CNY",
        rate_map={"CNY": 1.0},
    )

    assert [entry["list_price"] for entry in plan] == [21.99]
    assert any(
        call.args
        and len(call.args) > 1
        and "检查记录写入异常" in str(call.args[0])
        and call.args[1] == "warn"
        for call in ctx.log.call_args_list
    )


def test_strategy_one_does_not_request_history(monkeypatch):
    import sys
    import types

    from app import sell_pipeline

    contract = types.ModuleType("app.decision_records")
    contract.snapshot_strategy = lambda config, kind: (
        deepcopy(_strategy()), deepcopy(_modules())
    )
    contract.capture_decision = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "app.decision_records", contract)
    monkeypatch.setattr(sell_pipeline, "get_current_account", lambda: {"id": "account-a"})
    monkeypatch.setattr(
        sell_pipeline,
        "get_sell_orders_cny",
        lambda *_a, **_kw: {"sell_orders": [
            {"price": 20.0, "quantity": 3},
            {"price": 21.0, "quantity": 3},
            {"price": 22.0, "quantity": 50},
        ]},
    )
    monkeypatch.setattr(
        sell_pipeline,
        "_steam_latest_price_and_trend",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("strategy 1 must not request history")
        ),
    )
    ctx = MagicMock()
    ctx.is_stop_requested.return_value = False

    plan = sell_pipeline._build_listing_plan(
        ctx=ctx,
        cfg={"pipeline": {"sell_strategy": 1}},
        session=object(),
        sellable=[{
            "name": "AK-47 | Redline", "market_hash_name": "AK-47 | Redline",
            "assetid": "987654", "appid": 730, "can_sell": True,
        }],
        sell_strategy=1,
        pipeline_cfg={"sell_strategy": 1, "max_listings_per_item": 5},
        purchases_snapshot=[{
            "_db_id": 42, "assetid": "987654", "market_hash_name": "AK-47 | Redline",
            "name": "AK-47 | Redline", "price": 12.34, "market_price": 18.0,
        }],
        ok_listings=True,
        active_listing_ids=set(),
        listing_assetid_to_name={},
        assetid_to_name_map={},
        account_currency="CNY",
        rate_map={"CNY": 1.0},
        capture_decision=False,
    )

    assert [entry["list_price"] for entry in plan] == [21.99]


def test_ownership_rejection_does_not_request_market_orders(monkeypatch):
    import sys
    import types

    from app import sell_pipeline

    contract = types.ModuleType("app.decision_records")
    contract.snapshot_strategy = lambda config, kind: (
        deepcopy(_strategy()), deepcopy(_modules())
    )
    contract.capture_decision = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "app.decision_records", contract)
    monkeypatch.setattr(sell_pipeline, "get_current_account", lambda: {"id": "account-a"})
    monkeypatch.setattr(
        sell_pipeline,
        "get_sell_orders_cny",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("ownership rejection must not request sell orders")
        ),
    )
    ctx = MagicMock()
    ctx.is_stop_requested.return_value = False
    decision_sink = []

    plan = sell_pipeline._build_listing_plan(
        ctx=ctx,
        cfg={"pipeline": {"sell_strategy": 1}},
        session=object(),
        sellable=[{
            "name": "AK-47 | Redline", "market_hash_name": "AK-47 | Redline",
            "assetid": "987654", "appid": 730, "can_sell": True,
        }],
        sell_strategy=1,
        pipeline_cfg={"sell_strategy": 1, "max_listings_per_item": 5},
        purchases_snapshot=[],
        ok_listings=True,
        active_listing_ids=set(),
        listing_assetid_to_name={},
        assetid_to_name_map={},
        account_currency="CNY",
        rate_map={"CNY": 1.0},
        capture_decision=False,
        decision_sink=decision_sink,
    )

    assert plan == []
    assert len(decision_sink) == 1
    assert decision_sink[0]["status"] == "reject"
    assert decision_sink[0]["steps"][0]["module_id"] == "sell.sellable_inventory_filter"
    assert decision_sink[0]["steps"][0]["status"] == "reject"


def test_steam_trend_default_contract_uses_latest_sample_window(monkeypatch):
    from datetime import datetime, timezone

    from app import sell_pipeline
    from app.services.steam_client import SteamClient

    history = [
        ["Oct 8 2026 01: +0", 1.0, "4"],
        ["Oct 18 2026 01: +0", 2.0, "5"],
        ["Oct 23 2026 01: +0", 3.0, "6"],
        ["Oct 28 2026 01: +0", 4.0, "7"],
    ]
    monkeypatch.setattr(
        SteamClient,
        "fetch_history",
        lambda self, *_a, **_kw: {"history": history, "currency": "CNY"},
    )

    latest_short, short_trend, short_points = sell_pipeline._steam_latest_price_and_trend(
        "AK-47 | Redline", trend_days=7
    )
    latest_long, long_trend, long_points = sell_pipeline._steam_latest_price_and_trend(
        "AK-47 | Redline", trend_days=30
    )
    _, _, complete_snapshot = sell_pipeline._steam_latest_price_and_trend(
        "AK-47 | Redline", trend_days=7, with_points=True
    )

    assert (latest_short, short_trend) == (4.0, 0)
    assert short_points == [3.0, 4.0]
    assert (latest_long, long_trend) == (4.0, 1)
    assert long_points == [1.0, 2.0, 3.0, 4.0]
    assert complete_snapshot["currency"] == "CNY"
    assert complete_snapshot["source_currency"] == "CNY"
    assert complete_snapshot["window_days"] == 7
    assert [point["price"] for point in complete_snapshot["points"]] == [
        1.0, 2.0, 3.0, 4.0,
    ]


def test_steam_history_snapshot_records_source_currency_and_actual_rate(monkeypatch):
    from app import sell_pipeline
    from app.services.steam_client import SteamClient

    monkeypatch.setattr(
        SteamClient,
        "fetch_history",
        lambda self, *_a, **_kw: {
            "history": [
                ["Oct 26 2026 01: +0", 1.0, "4"],
                ["Oct 27 2026 01: +0", 1.01, "5"],
                ["Oct 28 2026 01: +0", 1.02, "6"],
            ],
            "currency": "USD",
        },
    )
    monkeypatch.setattr(
        "utils.money.load_exchange_rates", lambda: {"USD": 8.45}, raising=False
    )

    latest, trend, snapshot = sell_pipeline._steam_latest_price_and_trend(
        "AK-47 | Redline", trend_days=7, with_points=True
    )

    assert latest == 1.02 * 8.45
    assert trend == 1
    assert snapshot["available"] is True
    assert snapshot["currency"] == "USD"
    assert snapshot["source_currency"] == "USD"
    assert snapshot["cny_per_currency"] == 8.45
    assert snapshot["exchange_rate_source"] == "exchange_rate_file"
    assert [point["price"] for point in snapshot["points"]] == [1.0, 1.01, 1.02]


def test_steam_history_missing_currency_marks_collector_default_explicitly(monkeypatch):
    from app import sell_pipeline
    from app.services.steam_client import SteamClient

    monkeypatch.setattr(
        SteamClient,
        "fetch_history",
        lambda self, *_a, **_kw: {
            "history": [
                ["Oct 26 2026 01: +0", 1.0, "4"],
                ["Oct 27 2026 01: +0", 1.01, "5"],
                ["Oct 28 2026 01: +0", 1.02, "6"],
            ],
        },
    )

    _, _, snapshot = sell_pipeline._steam_latest_price_and_trend(
        "AK-47 | Redline", trend_days=7, with_points=True
    )

    assert snapshot["available"] is True
    assert snapshot["currency"] == "CNY"
    assert snapshot["source_currency"] == "CNY"
    assert snapshot["currency_source"] == "default"
    assert snapshot["cny_per_currency"] == 1.0
