"""Behavior tests for read-only decision record refreshes."""

import copy
import threading
import time

import pytest

from app.decision_records import DecisionError
from app.decision_versions import rules_version


class RecordStore:
    def __init__(self, records):
        self.records = {record["id"]: copy.deepcopy(record) for record in records}
        self.added = []

    def get_record(self, record_id):
        record = self.records.get(record_id)
        if record is None:
            raise DecisionError("record not found")
        return copy.deepcopy(record)

    def add_version(self, record_id, inputs, evaluated_at):
        if record_id not in self.records:
            raise DecisionError("record not found")
        version = {
            "id": f"version-{len(self.added) + 1}",
            "created_at": evaluated_at,
            "inputs": copy.deepcopy(inputs),
            "evaluated_at": evaluated_at,
        }
        self.added.append({"record_id": record_id, "version": copy.deepcopy(version)})
        return copy.deepcopy(version)

    def delete(self, record_id):
        self.records.pop(record_id, None)

    def install(self, monkeypatch):
        from app import decision_records

        monkeypatch.setattr(decision_records, "get_record", self.get_record)
        monkeypatch.setattr(decision_records, "add_version", self.add_version)


def _buy_inputs():
    return {
        "item": {
            "name": "AK-47 | Redline",
            "market_hash_name": "AK-47 | Redline",
            "goods_id": 123,
            "assetid": "987654",
            "min_price": 12.34,
            "daily_volume": 90,
            "ratio": 0.8,
        },
        "market": {
            "buff": {
                "available": True,
                "reason": None,
                "lowest_price": 10.0,
                "count_at_lowest": 1,
                "orders": [{"price": 10.0, "quantity": 1}],
            },
            "steam": {
                "available": True,
                "reason": None,
                "smart_price": 18.0,
                "sell_orders": [{"price": 18.0, "quantity": 2}],
            },
            "history": {
                "available": False,
                "reason": "not fetched",
                "currency": "CNY",
                "rows": [],
            },
        },
        "account": {
            "target_balance": 500.0,
            "accumulated_spend": 120.0,
            "held_same_name": 2,
        },
        "valuation": {"usd_to_cny": 7.2},
    }


def _buy_record(record_id="rec-buy"):
    inputs = _buy_inputs()
    return {
        "id": record_id,
        "kind": "buy",
        "rules_version": rules_version("buy"),
        "account_id": "acct-a",
        "item": copy.deepcopy(inputs["item"]),
        "strategy": {
            "id": "system.buy.default",
            "strategy_type": "buy",
            "steps": [{
                "module_id": "buy.steam_sell_depth", "enabled": True,
                "params": {"sell_price_wall_volume": 20, "sell_price_max_ignore_volume": 4},
            }],
        },
        "modules": {},
        "inputs": inputs,
        "evaluated_at": 1000.0,
        "result": {"status": "unavailable"},
        "schema_version": 1,
    }


def _sell_inputs():
    return {
        "item": {
            "name": "AWP | Asiimov",
            "market_hash_name": "AWP | Asiimov",
            "goods_id": 456,
            "assetid": "111222",
        },
        "inventory": {"can_sell": True, "appid": 730},
        "ownership": {
            "eligible": True,
            "purchase_id": 42,
            "purchase_price_cny": 88.8,
            "market_price_cny_at_purchase": 99.9,
        },
        "listing": {"same_name_count": 1, "round_same_name_count": 0, "source": "steam"},
        "market": {
            "source": "steam_market",
            "captured_at": 1000.0,
            "currency": "CNY",
            "available": False,
            "sell_orders": [],
            "error": "not fetched",
        },
        "trend": {
            "source": "steam_history",
            "captured_at": 1000.0,
            "window_days": 7,
            "available": False,
            "change": None,
            "points": [],
        },
        "currency": {"code": "CNY", "account_per_cny": 1.0},
    }


def _sell_record(record_id="rec-sell"):
    inputs = _sell_inputs()
    return {
        "id": record_id,
        "kind": "sell",
        "rules_version": rules_version("sell"),
        "account_id": "acct-a",
        "item": copy.deepcopy(inputs["item"]),
        "strategy": {
            "id": "system.sell.immediate",
            "strategy_type": "sell",
            "steps": [
                {"module_id": "pricing.steam_wall_price", "enabled": True,
                 "params": {"sell_price_wall_volume": 20, "sell_price_max_ignore_volume": 4}},
                {"module_id": "guard.rising_trend_wait", "enabled": True,
                 "params": {"sell_trend_days": 7}},
            ],
        },
        "modules": {},
        "inputs": inputs,
        "evaluated_at": 1000.0,
        "result": {"status": "unavailable"},
        "schema_version": 1,
    }


@pytest.fixture
def account_state(monkeypatch):
    state = {"current_id": "acct-a"}
    accounts = {
        "acct-a": {"id": "acct-a", "steam_id": "76561198000000001"},
        "acct-b": {"id": "acct-b", "steam_id": "76561198000000002"},
    }
    monkeypatch.setattr("app.accounts.get_current_id", lambda: state["current_id"])
    monkeypatch.setattr("app.accounts.get_current_account", lambda: accounts.get(state["current_id"]))
    monkeypatch.setattr("app.accounts.get_account", lambda account_id: accounts.get(account_id))
    monkeypatch.setattr(
        "app.config_loader.get_steam_credentials",
        lambda: {
            "cookies": "sessionid=x; steamLoginSecure=76561198000000001||secret",
            "steam_id": accounts[state["current_id"]]["steam_id"],
        },
    )
    return state


@pytest.fixture
def refresh_module(monkeypatch):
    import app.decision_refresh as refresh

    monkeypatch.setattr(refresh, "_clock", lambda: 2000.0)
    with refresh._JOBS_LOCK:
        refresh._JOBS.clear()
        refresh._FINISHED_JOBS.clear()
    yield refresh
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with refresh._JOBS_LOCK:
            if not any(job.get("thread") and job["thread"].is_alive() for job in refresh._JOBS.values()):
                break
        time.sleep(0.01)


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value is not None and value is not False:
            return value
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def _sources():
    return {
        "buff": {
            "available": True,
            "reason": None,
            "orders": [
                {"price": 11.5, "quantity": 3},
                {"price": 12.0, "quantity": 8},
            ],
        },
        "steam": {
            "available": True,
            "reason": None,
            "smart_price": 19.5,
            "sell_orders": [
                {"price": 20.0, "quantity": 4},
                {"price": 21.0, "quantity": 9},
            ],
        },
        "history": {
            "available": True,
            "reason": None,
            "currency": "CNY",
            "rows": [[900.0, 18.0, 2], [950.0, 19.0, 3]],
        },
    }


def test_refresh_is_deduplicated_and_adds_an_immutable_buy_version(
    account_state, refresh_module, monkeypatch
):
    record = _buy_record()
    original = copy.deepcopy(record)
    store = RecordStore([record])
    store.install(monkeypatch)
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    fetch_calls = []

    def fetch(_record):
        fetch_calls.append(_record["id"])
        fetch_started.set()
        assert release_fetch.wait(2.0)
        return _sources()

    monkeypatch.setattr(refresh_module, "_fetch_sources", fetch)

    started = refresh_module.start_refresh(record["id"])
    assert started["id"] == record["id"]
    assert started["status"] in {"queued", "running"}
    assert fetch_started.wait(2.0)

    duplicate = refresh_module.start_refresh(record["id"])
    assert duplicate["id"] == record["id"]
    assert duplicate["status"] in {"queued", "running"}
    assert fetch_calls == [record["id"]]

    release_fetch.set()
    finished = _wait_until(
        lambda: (
            status
            if (status := refresh_module.refresh_status(record["id"]))
            and status["status"] in {"succeeded", "failed"}
            else None
        )
    )

    assert finished["status"] == "succeeded"
    assert finished["version_id"] == "version-1"
    assert fetch_calls == [record["id"]]
    assert store.records[record["id"]] == original
    assert len(store.added) == 1

    version = store.added[0]["version"]
    assert version["evaluated_at"] == 2000.0
    inputs = version["inputs"]
    assert inputs["account"] == original["inputs"]["account"]
    assert inputs["valuation"] == original["inputs"]["valuation"]
    assert inputs["item"]["min_price"] == 11.5
    assert inputs["item"]["daily_volume"] == original["inputs"]["item"]["daily_volume"]
    assert inputs["item"]["ratio"] == original["inputs"]["item"]["ratio"]
    assert inputs["item"]["name"] == original["inputs"]["item"]["name"]
    assert inputs["item"]["market_hash_name"] == original["inputs"]["item"]["market_hash_name"]
    assert inputs["item"]["goods_id"] == original["inputs"]["item"]["goods_id"]
    assert inputs["item"]["assetid"] == original["inputs"]["item"]["assetid"]
    assert inputs["market"]["buff"]["orders"] == [
        {"price": 11.5, "quantity": 3},
        {"price": 12.0, "quantity": 8},
    ]
    assert inputs["market"]["steam"]["sell_orders"] == [
        {"price": 20.0, "quantity": 4},
        {"price": 21.0, "quantity": 9},
    ]
    assert inputs["market"]["history"]["rows"] == [
        [900.0, 18.0, 2],
        [950.0, 19.0, 3],
    ]
    for source in ("buff", "steam", "history"):
        assert inputs["market"][source]["captured_at"] == 2000.0
    assert inputs["refresh_meta"]["record_evaluated_at"] == 1000.0
    assert inputs["refresh_meta"]["sources"]["steam"] == "available"
    assert "item.min_price" in inputs["refresh_meta"]["updated"]
    assert "item.daily_volume" in inputs["refresh_meta"]["preserved"]
    assert inputs["data_version"].startswith("refresh:")


def test_refresh_preserves_non_market_item_context_from_safe_record_item(
    account_state, refresh_module, monkeypatch
):
    record = _buy_record()
    record["item"] = {
        key: record["item"][key]
        for key in ("name", "market_hash_name", "goods_id", "assetid")
    }
    original_inputs = copy.deepcopy(record["inputs"])
    store = RecordStore([record])
    store.install(monkeypatch)
    monkeypatch.setattr(refresh_module, "_fetch_sources", lambda _record: _sources())

    refresh_module.start_refresh(record["id"])
    finished = _wait_until(
        lambda: (
            status
            if (status := refresh_module.refresh_status(record["id"]))
            and status["status"] in {"succeeded", "failed"}
            else None
        )
    )

    assert finished["status"] == "succeeded"
    item = store.added[0]["version"]["inputs"]["item"]
    assert item["min_price"] == 11.5
    assert {key: item[key] for key in ("name", "market_hash_name", "goods_id", "assetid")} == {
        key: original_inputs["item"][key]
        for key in ("name", "market_hash_name", "goods_id", "assetid")
    }
    assert item["daily_volume"] == original_inputs["item"]["daily_volume"]
    assert item["ratio"] == original_inputs["item"]["ratio"]


def test_duplicate_start_keeps_active_job_when_account_changes(
    account_state, refresh_module, monkeypatch
):
    record = _buy_record()
    store = RecordStore([record])
    store.install(monkeypatch)
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    fetch_calls = []

    def fetch(_record):
        fetch_calls.append(_record["id"])
        fetch_started.set()
        assert release_fetch.wait(2.0)
        return _sources()

    monkeypatch.setattr(refresh_module, "_fetch_sources", fetch)

    first = refresh_module.start_refresh(record["id"])
    assert first["status"] in {"queued", "running"}
    assert fetch_started.wait(2.0)
    account_state["current_id"] = "acct-b"

    duplicate = refresh_module.start_refresh(record["id"])
    try:
        assert duplicate["status"] in {"queued", "running"}
        assert fetch_calls == [record["id"]]
    finally:
        release_fetch.set()

    finished = _wait_until(
        lambda: (
            status
            if (status := refresh_module.refresh_status(record["id"]))
            and status["status"] in {"succeeded", "failed"}
            else None
        )
    )
    assert finished["status"] == "failed"
    assert "账号" in finished["error"]
    assert store.added == []


def test_cleaned_record_is_not_resurrected_by_same_id_replacement(
    account_state, refresh_module, monkeypatch
):
    record = _buy_record()
    store = RecordStore([record])
    store.install(monkeypatch)
    fetch_started = threading.Event()
    release_fetch = threading.Event()

    def fetch(_record):
        fetch_started.set()
        assert release_fetch.wait(2.0)
        return _sources()

    monkeypatch.setattr(refresh_module, "_fetch_sources", fetch)

    refresh_module.start_refresh(record["id"])
    assert fetch_started.wait(2.0)
    with refresh_module._JOBS_LOCK:
        worker = refresh_module._JOBS[record["id"]]["thread"]

    replacement = _buy_record()
    replacement["evaluated_at"] = 3000.0
    replacement["item"]["name"] = "M4A1-S | Printstream"
    replacement["inputs"]["item"]["name"] = "M4A1-S | Printstream"
    store.delete(record["id"])
    store.records[record["id"]] = copy.deepcopy(replacement)

    release_fetch.set()
    worker.join(2.0)

    assert not worker.is_alive()
    assert store.added == []
    assert store.records[record["id"]] == replacement
    assert refresh_module.refresh_status(record["id"]) is None


def test_finished_status_is_not_reused_for_same_id_replacement(
    account_state, refresh_module, monkeypatch
):
    record = _buy_record()
    store = RecordStore([record])
    store.install(monkeypatch)
    monkeypatch.setattr(refresh_module, "_fetch_sources", lambda _record: _sources())

    refresh_module.start_refresh(record["id"])
    _wait_until(
        lambda: (
            status
            if (status := refresh_module.refresh_status(record["id"]))
            and status["status"] in {"succeeded", "failed"}
            else None
        )
    )
    replacement = _buy_record()
    replacement["evaluated_at"] = 3000.0
    store.delete(record["id"])
    store.records[record["id"]] = copy.deepcopy(replacement)

    assert refresh_module.refresh_status(record["id"]) is None
    with refresh_module._JOBS_LOCK:
        assert record["id"] not in refresh_module._JOBS


def test_sell_refresh_preserves_owned_context_and_updates_only_market_evidence(
    account_state, refresh_module, monkeypatch
):
    record = _sell_record()
    original = copy.deepcopy(record)
    store = RecordStore([record])
    store.install(monkeypatch)
    monkeypatch.setattr(refresh_module, "_fetch_sources", lambda _record: _sources())

    refresh_module.start_refresh(record["id"])
    finished = _wait_until(
        lambda: (
            status
            if (status := refresh_module.refresh_status(record["id"]))
            and status["status"] in {"succeeded", "failed"}
            else None
        )
    )

    assert finished["status"] == "succeeded"
    assert store.records[record["id"]] == original
    inputs = store.added[0]["version"]["inputs"]
    for key in ("item", "inventory", "ownership", "listing", "currency"):
        assert inputs[key] == original["inputs"][key]
    assert inputs["market"]["captured_at"] == 2000.0
    assert inputs["market"]["sell_orders"] == [
        {"price": 20.0, "quantity": 4},
        {"price": 21.0, "quantity": 9},
    ]
    assert inputs["trend"]["captured_at"] == 2000.0
    assert inputs["trend"]["points"] == [
        {"captured_at": 900.0, "price": 18.0, "volume": 2},
        {"captured_at": 950.0, "price": 19.0, "volume": 3},
    ]
    assert inputs["refresh_meta"]["preserved"] == [
        "item", "inventory", "ownership", "listing", "currency",
    ]
    assert inputs["refresh_meta"]["updated"] == ["market", "trend"]


def test_buy_refresh_marks_steam_unavailable_without_smart_price(
    account_state, refresh_module, monkeypatch
):
    record = _buy_record()
    store = RecordStore([record])
    store.install(monkeypatch)
    sources = _sources()
    sources["steam"]["smart_price"] = None
    monkeypatch.setattr(refresh_module, "_fetch_sources", lambda _record: sources)

    refresh_module.start_refresh(record["id"])
    finished = _wait_until(
        lambda: (
            status
            if (status := refresh_module.refresh_status(record["id"]))
            and status["status"] in {"succeeded", "failed"}
            else None
        )
    )

    assert finished["status"] == "succeeded"
    steam = store.added[0]["version"]["inputs"]["market"]["steam"]
    assert steam["available"] is False
    assert steam["sell_orders"]
    assert steam["reason"]


def test_buy_refresh_marks_unavailable_buff_price_as_preserved(
    account_state, refresh_module, monkeypatch
):
    record = _buy_record()
    store = RecordStore([record])
    store.install(monkeypatch)
    sources = _sources()
    sources["buff"] = {"available": False, "reason": "not fetched", "orders": []}
    monkeypatch.setattr(refresh_module, "_fetch_sources", lambda _record: sources)

    refresh_module.start_refresh(record["id"])
    finished = _wait_until(
        lambda: (
            status
            if (status := refresh_module.refresh_status(record["id"]))
            and status["status"] in {"succeeded", "failed"}
            else None
        )
    )

    assert finished["status"] == "succeeded"
    inputs = store.added[0]["version"]["inputs"]
    assert inputs["item"]["min_price"] == record["inputs"]["item"]["min_price"]
    assert "item.min_price" in inputs["refresh_meta"]["preserved"]
    assert "item.min_price" not in inputs["refresh_meta"]["updated"]


def test_account_guard_rejects_switch_before_market_adapter(
    account_state, refresh_module, monkeypatch
):
    record = _buy_record()
    original = copy.deepcopy(record)
    store = RecordStore([record])
    store.install(monkeypatch)
    account_state["current_id"] = "acct-b"

    def forbidden(_record):
        raise AssertionError("market adapter must not run for another account")

    monkeypatch.setattr(refresh_module, "_fetch_sources", forbidden)

    started = refresh_module.start_refresh(record["id"])

    assert started["status"] == "failed"
    assert "账号" in started["error"]
    assert store.added == []
    assert store.records[record["id"]] == original


def test_rate_limited_adapter_fails_without_adding_a_version(
    account_state, refresh_module, monkeypatch
):
    record = _buy_record()
    original = copy.deepcopy(record)
    store = RecordStore([record])
    store.install(monkeypatch)

    def limited(_record):
        raise refresh_module._RefreshFailure("Steam 行情接口受限，请稍后重试")

    monkeypatch.setattr(refresh_module, "_fetch_sources", limited)

    refresh_module.start_refresh(record["id"])
    finished = _wait_until(
        lambda: (
            status
            if (status := refresh_module.refresh_status(record["id"]))
            and status["status"] in {"succeeded", "failed"}
            else None
        )
    )

    assert finished["status"] == "failed"
    assert "受限" in finished["error"]
    assert store.added == []
    assert store.records[record["id"]] == original


def test_version_save_failure_keeps_old_record_and_redacts_secret(
    account_state, refresh_module, monkeypatch
):
    record = _buy_record()
    original = copy.deepcopy(record)
    store = RecordStore([record])
    store.install(monkeypatch)

    def fail_save(_record_id, _inputs, _evaluated_at):
        raise RuntimeError("authorization=Bearer super-secret")

    monkeypatch.setattr(refresh_module, "_fetch_sources", lambda _record: _sources())
    from app import decision_records

    monkeypatch.setattr(decision_records, "add_version", fail_save)

    refresh_module.start_refresh(record["id"])
    finished = _wait_until(
        lambda: (
            status
            if (status := refresh_module.refresh_status(record["id"]))
            and status["status"] in {"succeeded", "failed"}
            else None
        )
    )

    assert finished["status"] == "failed"
    assert "super-secret" not in finished["error"]
    assert "authorization" not in finished["error"].lower()
    assert store.added == []
    assert store.records[record["id"]] == original


def test_unsupported_schema_is_rejected_before_adapter(
    account_state, refresh_module, monkeypatch
):
    record = _buy_record()
    record["schema_version"] = 2
    store = RecordStore([record])
    store.install(monkeypatch)

    def forbidden(_record):
        raise AssertionError("adapter must not run for an unsupported schema")

    monkeypatch.setattr(refresh_module, "_fetch_sources", forbidden)

    started = refresh_module.start_refresh(record["id"])

    assert started["status"] == "failed"
    assert "版本" in started["error"]
    assert store.added == []


def test_lost_memory_status_is_unknown_and_refresh_is_retryable(
    account_state, refresh_module, monkeypatch
):
    record = _buy_record()
    original = copy.deepcopy(record)
    store = RecordStore([record])
    store.install(monkeypatch)
    monkeypatch.setattr(refresh_module, "_fetch_sources", lambda _record: _sources())

    refresh_module.start_refresh(record["id"])
    first = _wait_until(
        lambda: (
            status
            if (status := refresh_module.refresh_status(record["id"]))
            and status["status"] in {"succeeded", "failed"}
            else None
        )
    )
    assert first["status"] == "succeeded"
    with refresh_module._JOBS_LOCK:
        refresh_module._JOBS.clear()  # Process restart: queued/running cannot survive.

    assert refresh_module.refresh_status(record["id"]) is None
    assert store.records[record["id"]] == original
    assert len(store.added) == 1

    refresh_module.start_refresh(record["id"])
    second = _wait_until(
        lambda: (
            status
            if (status := refresh_module.refresh_status(record["id"]))
            and status["status"] in {"succeeded", "failed"}
            else None
        )
    )
    assert second["status"] == "succeeded"
    assert len(store.added) == 2
    assert store.records[record["id"]] == original


def test_finished_job_history_keeps_only_recent_limit(
    account_state, refresh_module, monkeypatch
):
    assert refresh_module._MAX_FINISHED_JOBS == 200
    monkeypatch.setattr(refresh_module, "_MAX_FINISHED_JOBS", 2)
    records = [_buy_record(f"rec-{index}") for index in range(3)]
    store = RecordStore(records)
    store.install(monkeypatch)
    monkeypatch.setattr(refresh_module, "_fetch_sources", lambda _record: _sources())

    for record in records:
        refresh_module.start_refresh(record["id"])
        finished = _wait_until(
            lambda record_id=record["id"]: (
                status
                if (status := refresh_module.refresh_status(record_id))
                and status["status"] in {"succeeded", "failed"}
                else None
            )
        )
        assert finished["status"] == "succeeded"

    with refresh_module._JOBS_LOCK:
        assert list(refresh_module._JOBS) == ["rec-1", "rec-2"]


def test_status_for_cleaned_record_drops_its_finished_job(
    account_state, refresh_module, monkeypatch
):
    record = _buy_record()
    store = RecordStore([record])
    store.install(monkeypatch)
    monkeypatch.setattr(refresh_module, "_fetch_sources", lambda _record: _sources())

    refresh_module.start_refresh(record["id"])
    _wait_until(
        lambda: (
            status
            if (status := refresh_module.refresh_status(record["id"]))
            and status["status"] in {"succeeded", "failed"}
            else None
        )
    )
    store.delete(record["id"])

    assert refresh_module.refresh_status(record["id"]) is None
    with refresh_module._JOBS_LOCK:
        assert record["id"] not in refresh_module._JOBS


def test_start_for_cleaned_record_drops_its_finished_job(
    account_state, refresh_module, monkeypatch
):
    record = _buy_record()
    store = RecordStore([record])
    store.install(monkeypatch)
    monkeypatch.setattr(refresh_module, "_fetch_sources", lambda _record: _sources())

    refresh_module.start_refresh(record["id"])
    _wait_until(
        lambda: (
            status
            if (status := refresh_module.refresh_status(record["id"]))
            and status["status"] in {"succeeded", "failed"}
            else None
        )
    )
    store.delete(record["id"])

    started = refresh_module.start_refresh(record["id"])

    assert started["status"] == "failed"
    with refresh_module._JOBS_LOCK:
        assert record["id"] not in refresh_module._JOBS


def test_refresh_never_reads_formal_strategy_or_calls_trading_entrypoints(
    account_state, refresh_module, monkeypatch
):
    record = _sell_record()
    store = RecordStore([record])
    store.install(monkeypatch)
    monkeypatch.setattr(refresh_module, "_fetch_sources", lambda _record: _sources())

    from app import decision_records, sell_pipeline, strategy_engine
    from app.services.buff_client import BuffClient

    def forbidden(*_args, **_kwargs):
        raise AssertionError("refresh must stay on the read-only boundary")

    monkeypatch.setattr(strategy_engine, "get_strategy", forbidden)
    monkeypatch.setattr(decision_records, "snapshot_strategy", forbidden)
    monkeypatch.setattr(sell_pipeline, "_submit_listings", forbidden)
    monkeypatch.setattr(sell_pipeline, "_auto_confirm_listings", forbidden)
    monkeypatch.setattr(BuffClient, "prepare_single_buy", forbidden)
    monkeypatch.setattr(BuffClient, "get_order_payment", forbidden)

    refresh_module.start_refresh(record["id"])
    finished = _wait_until(
        lambda: (
            status
            if (status := refresh_module.refresh_status(record["id"]))
            and status["status"] in {"succeeded", "failed"}
            else None
        )
    )

    assert finished["status"] == "succeeded"
    assert len(store.added) == 1


def test_steam_read_adapter_computes_smart_price_from_order_dicts(
    account_state, refresh_module, monkeypatch
):
    class FakeSession:
        def close(self):
            pass

    monkeypatch.setattr("steam.session.create_market_session", lambda *_args: FakeSession())
    monkeypatch.setattr(
        "steam.market_orders.get_sell_orders_cny",
        lambda *_args, **_kwargs: (
            {
                "sell_orders": [
                    {"price": 20.0, "quantity": 4},
                    {"price": 21.0, "quantity": 9},
                ]
            },
            None,
        ),
    )

    source = refresh_module._fetch_steam_market(_buy_record())

    assert source["available"] is True
    assert source["smart_price"] == 20.0
    assert source["sell_orders"] == [
        {"price": 20.0, "quantity": 4},
        {"price": 21.0, "quantity": 9},
    ]


def test_history_read_adapter_preserves_real_steam_dates_in_buy_version(
    account_state, refresh_module, monkeypatch
):
    from datetime import datetime, timezone
    from app.services.steam_client import SteamClient

    rows = [
        ["Oct 27 2026 01: +0", 19.0, "3"],
        ["Oct 26 2026 01: +0", 18.0, "2"],
        ["invalid date", 99.0, "1"],
    ]
    calls = []

    def fetch_history(self, name, app_id=730, return_currency=False, **kwargs):
        calls.append((name, app_id, return_currency))
        return {"history": copy.deepcopy(rows), "currency": "USD"}

    monkeypatch.setattr(SteamClient, "fetch_history", fetch_history)
    monkeypatch.setattr(refresh_module, "_fetch_steam_market", lambda record: _sources()["steam"])
    monkeypatch.setattr(refresh_module, "_fetch_buff_market", lambda record: _sources()["buff"])
    stamp = datetime(2026, 10, 28, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(refresh_module, "_clock", lambda: stamp)
    record = _buy_record()
    store = RecordStore([record])
    store.install(monkeypatch)
    refresh_module.start_refresh(record["id"])
    finished = _wait_until(lambda: (s if (s := refresh_module.refresh_status(record["id"]))
                                  and s["status"] in {"succeeded", "failed"} else None))
    assert finished["status"] == "succeeded"
    history = store.added[0]["version"]["inputs"]["market"]["history"]
    assert calls == [("AK-47 | Redline", 730, True)]
    assert history["available"] is True
    assert history["currency"] == "USD"
    assert history["rows"] == [
        ["Oct 26 2026 01: +0", 18.0, 2],
        ["Oct 27 2026 01: +0", 19.0, 3],
    ]


@pytest.mark.parametrize("rate_key", ["cny_per_account", "account_per_cny"])
def test_sell_refresh_keeps_full_dated_points_and_explicit_cny_market(
    account_state, refresh_module, monkeypatch, rate_key
):
    from datetime import datetime, timezone
    from app.services.steam_client import SteamClient

    def stamp(day):
        return datetime(2026, 10, day, 1, tzinfo=timezone.utc).timestamp()

    rows = [[f"Oct {day} 2026 01: +0", price, "3"]
            for day, price in [(1, 14), (25, 20), (26, 19), (27, 18), (29, 99)]]
    monkeypatch.setattr(SteamClient, "fetch_history", lambda self, *a, **kw:
                        {"history": rows, "currency": "USD"})
    class Session:
        def close(self):
            pass
    monkeypatch.setattr("steam.session.create_market_session", lambda *args: Session())
    monkeypatch.setattr("steam.market_orders.get_sell_orders_cny", lambda *a, **kw:
                        ({"sell_orders": [(20.0, 4), (21.0, 9)]}, None))
    monkeypatch.setattr(refresh_module, "_clock", lambda: stamp(28))
    record = _sell_record()
    record["strategy"] = {"steps": [
        {"module_id": "guard.rising_trend_wait", "enabled": True,
         "params": {"sell_trend_days": 3}},
    ]}
    record["inputs"]["currency"] = {"code": "USD", rate_key: 7.2}
    store = RecordStore([record])
    store.install(monkeypatch)
    refresh_module.start_refresh(record["id"])
    finished = _wait_until(lambda: (s if (s := refresh_module.refresh_status(record["id"]))
                                  and s["status"] in {"succeeded", "failed"} else None))
    assert finished["status"] == "succeeded"
    inputs = store.added[0]["version"]["inputs"]
    assert inputs["currency"] == record["inputs"]["currency"]
    assert inputs["market"]["currency"] == "CNY"
    assert inputs["market"]["sell_orders"][0]["price"] == 20.0
    assert inputs["trend"]["points"] == [
        {"captured_at": stamp(day), "price": price, "volume": 3}
        for day, price in [(1, 14), (25, 20), (26, 19), (27, 18), (29, 99)]
    ]
    assert "change" not in inputs["trend"]
    assert inputs["trend"]["currency"] == "USD"
    # Window metadata describes the snapshot; both previews still get all points.
    assert inputs["trend"]["window_days"] == 3

    from app.decision_sell import evaluate
    original = copy.deepcopy(inputs)
    short = evaluate(inputs, record["strategy"], stamp(28))
    long_strategy = copy.deepcopy(record["strategy"])
    long_strategy["steps"][0]["params"]["sell_trend_days"] = 30
    long = evaluate(inputs, long_strategy, stamp(28))
    assert short["status"] == "pass"
    assert short["steps"][0]["actual"]["change"] == -1
    assert long["status"] == "wait"
    assert long["steps"][0]["actual"]["change"] == 1
    assert inputs == original

    priced = evaluate(inputs, {"steps": [{
        "module_id": "pricing.steam_wall_price", "enabled": True,
        "params": {"sell_price_wall_volume": 20, "sell_price_max_ignore_volume": 4},
    }]}, stamp(28))
    pricing = priced["steps"][0]["output"]
    assert priced["status"] == "pass"
    assert pricing["list_price_cny"] == 19.99
    assert pricing["display_price"] == 2.78
    assert pricing["currency"] == "USD"


@pytest.mark.parametrize("wall,ignore,expected", [(4, 50, 20.0), (50, 0, 20.0), (50, 4, 30.0)])
def test_steam_read_adapter_uses_snapshot_step_pricing_params(
    account_state, refresh_module, monkeypatch, wall, ignore, expected
):
    class Session:
        def close(self):
            pass
    monkeypatch.setattr("steam.session.create_market_session", lambda *a: Session())
    monkeypatch.setattr("steam.market_orders.get_sell_orders_cny", lambda *a, **kw:
                        ({"sell_orders": [(20.0, 4), (30.0, 50)]}, None))
    record = _buy_record()
    record["strategy"] = {"steps": [
        {"module_id": "buy.steam_sell_depth", "enabled": True, "params": {
            "sell_price_wall_volume": wall, "sell_price_max_ignore_volume": ignore}},
        {"module_id": "unused", "enabled": False, "params": {
            "sell_price_wall_volume": 100, "sell_price_max_ignore_volume": 100}},
    ]}
    source = refresh_module._fetch_steam_market(record)
    assert source["smart_price"] == expected
    assert source["currency"] == "CNY"
    assert source["pricing_params"] == {
        "sell_price_wall_volume": wall,
        "sell_price_max_ignore_volume": ignore,
        "defaulted": [],
    }


def test_legacy_missing_pricing_params_have_explicit_provenance(
    account_state, refresh_module, monkeypatch
):
    class Session:
        def close(self):
            pass
    monkeypatch.setattr("steam.session.create_market_session", lambda *a: Session())
    monkeypatch.setattr("steam.market_orders.get_sell_orders_cny", lambda *a, **kw:
                        ({"sell_orders": [(20.0, 4), (30.0, 50)]}, None))
    monkeypatch.setattr(refresh_module, "_fetch_steam_history", lambda record: _sources()["history"])
    monkeypatch.setattr(refresh_module, "_fetch_buff_market", lambda record: _sources()["buff"])
    record = _buy_record()
    record["strategy"] = {"steps": [{"module_id": "buy.steam_sell_depth", "params": {}}]}
    store = RecordStore([record])
    store.install(monkeypatch)
    refresh_module.start_refresh(record["id"])
    finished = _wait_until(lambda: (s if (s := refresh_module.refresh_status(record["id"]))
                                  and s["status"] in {"succeeded", "failed"} else None))
    assert finished["status"] == "succeeded"
    steam = store.added[0]["version"]["inputs"]["market"]["steam"]
    assert steam["currency"] == "CNY"
    assert steam["pricing_params"] == {
        "sell_price_wall_volume": 20, "sell_price_max_ignore_volume": 4,
        "defaulted": ["sell_price_wall_volume", "sell_price_max_ignore_volume"],
    }


@pytest.mark.parametrize("version", [None, "previous-deployment"])
def test_changed_or_missing_rules_version_rejects_refresh_before_io(
    account_state, refresh_module, monkeypatch, version
):
    record = _buy_record()
    record["rules_version"] = version
    original = copy.deepcopy(record)
    store = RecordStore([record])
    store.install(monkeypatch)
    calls = []
    monkeypatch.setattr(refresh_module, "_fetch_sources", lambda record: calls.append(record) or _sources())
    status = refresh_module.start_refresh(record["id"])
    assert status["status"] == "failed"
    assert "规则" in status["error"]
    assert calls == []
    assert store.added == []
    assert store.records[record["id"]] == original


@pytest.mark.parametrize("step_params,expected", [({}, 20.0), (
    {"sell_price_wall_volume": 50, "sell_price_max_ignore_volume": 4}, 30.0)])
def test_steam_pricing_uses_recorded_policy_when_step_params_are_missing(
    account_state, refresh_module, monkeypatch, step_params, expected
):
    class Session:
        def close(self):
            pass
    monkeypatch.setattr("steam.session.create_market_session", lambda *a: Session())
    monkeypatch.setattr("steam.market_orders.get_sell_orders_cny", lambda *a, **kw:
                        ({"sell_orders": [(20.0, 4), (30.0, 50)]}, None))
    record = _buy_record()
    record["strategy"]["steps"][0]["params"] = step_params
    record["inputs"]["policy"] = {
        "sell_price_wall_volume": 4, "sell_price_max_ignore_volume": 0,
        "huge_profit_offset": 0.15,
    }
    original = copy.deepcopy(record)
    source = refresh_module._fetch_steam_market(record)
    assert source["smart_price"] == expected
    assert source["pricing_params"]["defaulted"] == []
    assert record == original


def test_buff_read_adapter_keeps_individual_orders_without_quantity(account_state, refresh_module, monkeypatch):
    from app.services.buff_client import BuffClient
    monkeypatch.setattr('app.config_loader.get_buff_credentials', lambda: {'cookies': 'offline-cookie'})
    monkeypatch.setattr(BuffClient, 'get_sell_orders', lambda *_args: [
        {'id': 'sell-1', 'price': '12.00', 'user_id': 'seller'},
        {'id': 'sell-2', 'price': '12.00', 'user_id': 'seller'},
    ])
    source = refresh_module._fetch_buff_market(_buy_record())
    assert source['available'] is True
    inputs = refresh_module._build_refreshed_inputs(_buy_record(), {**_sources(), 'buff': source}, 2000)
    assert inputs['item']['min_price'] == 12
    assert inputs['market']['buff']['count_at_lowest'] == 2
    assert inputs['market']['buff']['orders'] == [{'price': 12, 'quantity': 1}, {'price': 12, 'quantity': 1}]
    assert 'seller' not in str(inputs)


def test_refresh_history_uses_pinned_credentials_not_later_current_session(account_state, refresh_module, monkeypatch):
    from app.services import steam_client
    seen = []
    original_credentials = {'cookies': 'session-A', 'steam_id': '76561198000000001'}
    monkeypatch.setattr('app.config_loader.get_steam_credentials', lambda: original_credentials)
    monkeypatch.setattr('app.config_loader.get_buff_credentials', lambda: {'cookies': 'buff-A'})
    monkeypatch.setattr(steam_client, 'get_steam_credentials', lambda: {'cookies': 'session-B', 'steam_id': 'steam-b'})
    monkeypatch.setattr(refresh_module, '_account_guard', lambda record: None)
    monkeypatch.setattr(refresh_module, '_fetch_steam_market', lambda record: _sources()['steam'])
    monkeypatch.setattr(refresh_module, '_fetch_buff_market', lambda record: _sources()['buff'])
    def fetch_history(self, market_hash_name, app_id, return_currency, cookies):
        seen.append(cookies)
        return {'history': [['Oct 27 2026 01: +0', 100, 10]], 'currency': 'CNY'}
    monkeypatch.setattr(steam_client.SteamClient, '_fetch_history_impl', fetch_history)
    steam_client._history_cache.clear()
    sources = refresh_module._fetch_sources(_buy_record())
    assert seen == ['session-A']
    assert sources['history']['available'] is True
