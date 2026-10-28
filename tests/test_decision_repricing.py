"""Public automatic-repricing decision explanations, without real trading."""
import time

import pytest
from sqlmodel import SQLModel, create_engine

from app import accounts, database, strategy_engine as se
from app.trading_models import MarketListing, TradingPolicy


@pytest.fixture
def reprice_env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'repricing.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(database, "_engine", engine)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(se, "STRATEGY_STORE_PATH", tmp_path / "strategies.json")
    monkeypatch.setattr(se, "USER_MODULE_STORE_PATH", tmp_path / "modules.json")
    monkeypatch.setattr(accounts, "get_current_account", lambda: {"id": "app-account"})
    yield engine
    engine.dispose()


def _config(*, sell_strategy=1, **policy):
    return {
        "pipeline": {
            "sell_strategy": sell_strategy,
            "max_listings_per_item": 5,
            "sell_price_wall_volume": 20,
            "sell_price_max_ignore_volume": 4,
            "sell_price_offset": 0,
            "sell_trend_days": 7,
        },
        **policy,
    }


def _seed_listing(*, due=True, price=10.0, next_evaluation_at=None):
    now = time.time()
    with database.get_session() as session:
        session.add(TradingPolicy(
            id=1, enabled=True, age_hours=24, max_loss_percent=30,
            min_change_minor=10, min_change_percent=1.0,
            reevaluate_minutes=60, check_interval_seconds=600,
        ))
        purchase = database.Purchase(
            name="AK-47 | Redline", goods_id=123, price=price, at=now - 86400,
            assetid="asset-1", listing=True, listing_status="active",
            account_id="account",
        )
        session.add(purchase)
        session.flush()
        listing = MarketListing(
            purchase_id=purchase.id, account_id="account", assetid="asset-1",
            steam_listing_id="remote-1", status="active",
            buyer_minor=1500, seller_minor=1305, currency="CNY",
            activated_at=now - 90000,
            next_evaluation_at=(now - 1 if due else now + 3600),
        )
        session.add(listing)
        session.commit()
        return purchase.id, listing.id


def _snapshot():
    return {
        "asset-1": {
            "listing_id": "remote-1", "status": "active", "currency": "CNY",
            "buyer_minor": 1500, "seller_minor": 1305,
        }
    }


def _records():
    from app import decision_records
    listing = decision_records.list_records(kind="reprice", page_size=100)
    return [decision_records.get_record(item["id"]) for item in listing["items"]]


def _forbid_trading(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("automatic repricing decisions must not trade")

    monkeypatch.setattr("steam.market.list_item", forbidden)
    monkeypatch.setattr("app.steam_delist.delist_item", forbidden)


def test_not_due_is_explained_without_market_read_or_trading(reprice_env, monkeypatch):
    from app.services.repricing import plan_repricing

    _seed_listing(due=False)
    _forbid_trading(monkeypatch)
    reads = []
    monkeypatch.setattr(
        "app.sell_pipeline.get_sell_orders_cny",
        lambda *args, **kwargs: reads.append((args, kwargs)),
    )

    plan_repricing(_snapshot(), {"steam_id": "account", "cookies": "sessionid=x"}, _config())

    assert reads == []
    records = _records()
    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "reprice"
    assert record["account_id"] == "account"
    assert record["item"]["assetid"] == "asset-1"
    assert record["result"]["status"] == "wait"
    assert record["result"]["steps"][0]["module_id"] == "schedule.reprice_due"
    assert "尚未到" in record["result"]["steps"][0]["reason"]
    assert record["inputs"]["listed_at"]
    assert record["inputs"]["purchase_price_minor"] == 1000
    assert record["inputs"]["current_buyer_minor"] == 1500
    assert record["result"]["execution"]["status"] == "not_started"


def test_missing_snapshot_is_explained_as_unavailable_without_quote(reprice_env, monkeypatch):
    from app.services.repricing import plan_repricing

    _seed_listing(due=True)
    _forbid_trading(monkeypatch)
    reads = []
    monkeypatch.setattr(
        "app.sell_pipeline.get_sell_orders_cny",
        lambda *args, **kwargs: reads.append((args, kwargs)),
    )

    plan_repricing({}, {"steam_id": "account", "cookies": "sessionid=x"}, _config())

    assert reads == []
    records = _records()
    assert len(records) == 1
    result = records[0]["result"]
    assert result["status"] == "unavailable"
    assert result["steps"][0]["module_id"] == "market.reprice_snapshot"
    assert "行情" in result["steps"][0]["reason"]
    assert result["suggestion"]["action"] == "none"
    assert result["execution"]["status"] == "not_started"


def test_paused_sell_strategy_is_explained_without_quote(reprice_env, monkeypatch):
    from app.services.repricing import plan_repricing

    _seed_listing(due=True)
    _forbid_trading(monkeypatch)
    reads = []
    monkeypatch.setattr(
        "app.sell_pipeline.get_sell_orders_cny",
        lambda *args, **kwargs: reads.append((args, kwargs)),
    )

    plan_repricing(
        _snapshot(), {"steam_id": "account", "cookies": "sessionid=x"},
        _config(sell_strategy=4),
    )

    assert reads == []
    records = _records()
    assert len(records) == 1
    result = records[0]["result"]
    assert result["status"] == "wait"
    assert result["steps"][0]["module_id"] == "action.pause_auto_sell"
    assert "暂停" in result["steps"][0]["reason"]
    assert result["suggestion"]["action"] == "none"
    assert result["execution"]["status"] == "not_started"


def test_unsupported_currency_is_explained_without_quote(reprice_env, monkeypatch):
    from app.services.repricing import plan_repricing

    _seed_listing(due=True)
    _forbid_trading(monkeypatch)
    reads = []
    monkeypatch.setattr(
        "app.sell_pipeline.get_sell_orders_cny",
        lambda *args, **kwargs: reads.append((args, kwargs)),
    )
    snapshot = _snapshot()
    snapshot["asset-1"]["currency"] = "USD"

    plan_repricing(
        snapshot, {"steam_id": "account", "cookies": "sessionid=x"}, _config(),
    )

    assert reads == []
    records = _records()
    assert len(records) == 1
    result = records[0]["result"]
    assert result["status"] == "unavailable"
    assert result["steps"][0]["module_id"] == "market.reprice_currency"
    assert "CNY" in result["steps"][0]["reason"]
    assert records[0]["inputs"]["currency"] == "USD"
    assert result["execution"]["status"] == "not_started"


def test_insufficient_price_delta_is_wait_without_creating_job(reprice_env, monkeypatch):
    from app.services.repricing import plan_repricing
    from app.trading_models import RepriceJob

    _seed_listing(due=True)
    _forbid_trading(monkeypatch)
    reads = []

    def orders(*args, **kwargs):
        reads.append((args, kwargs))
        return {"sell_orders": [[14.90, 20]]}

    monkeypatch.setattr("app.sell_pipeline.get_sell_orders_cny", orders)

    plan_repricing(
        _snapshot(), {"steam_id": "account", "cookies": "sessionid=x"}, _config(),
    )

    assert len(reads) == 1
    records = _records()
    assert len(records) == 1
    result = records[0]["result"]
    assert result["status"] == "wait"
    step = result["steps"][-1]
    assert step["module_id"] == "pricing.auto_reprice"
    assert "价差不足" in step["reason"]
    assert step["actual"]["delta_buyer_minor"] > 0
    assert step["actual"]["delta_buyer_minor"] < step["threshold"]["required_delta_buyer_minor"]
    assert result["suggestion"]["action"] == "none"
    assert result["suggestion"]["target_buyer_minor"] == 1489
    assert result["suggestion"]["floor_seller_minor"] == 700
    assert result["execution"]["status"] == "not_started"
    with database.get_session() as session:
        assert session.exec(__import__("sqlmodel").select(RepriceJob)).first() is None


def test_cost_floor_is_explained_and_creates_only_a_planned_job(reprice_env, monkeypatch):
    from app.services.repricing import plan_repricing
    from sqlmodel import select
    from app.trading_models import RepriceJob

    _seed_listing(due=True)
    _forbid_trading(monkeypatch)
    reads = []
    monkeypatch.setattr(
        "app.sell_pipeline.get_sell_orders_cny",
        lambda *args, **kwargs: reads.append((args, kwargs)) or {
            "sell_orders": [[5.00, 20]]
        },
    )

    plan_repricing(
        _snapshot(), {"steam_id": "account", "cookies": "sessionid=x"}, _config(),
    )

    assert len(reads) == 1
    record = _records()[0]
    result = record["result"]
    assert result["status"] == "pass"
    step = result["steps"][-1]
    assert step["module_id"] == "pricing.auto_reprice"
    assert "亏损底线" in step["reason"]
    assert step["threshold"]["max_loss_percent"] == 30
    assert result["suggestion"]["action"] == "adjust"
    assert result["suggestion"]["target_seller_minor"] == 700
    assert result["suggestion"]["target_seller_minor"] == result["suggestion"]["floor_seller_minor"]
    assert record["inputs"]["purchase_price_minor"] == 1000
    assert result["execution"]["status"] == "planned"
    assert result["execution"]["withdrawn"] is False
    assert result["execution"]["returned"] is False
    assert result["execution"]["relisted"] is False
    with database.get_session() as session:
        job = session.exec(select(RepriceJob)).one()
        assert job.status == "planned"
        assert job.target_seller_minor == 700


def test_missing_market_orders_are_unavailable_without_creating_job(reprice_env, monkeypatch):
    from app.services.repricing import plan_repricing
    from sqlmodel import select
    from app.trading_models import RepriceJob

    _seed_listing(due=True)
    _forbid_trading(monkeypatch)
    reads = []
    monkeypatch.setattr(
        "app.sell_pipeline.get_sell_orders_cny",
        lambda *args, **kwargs: reads.append((args, kwargs)) or {"sell_orders": []},
    )

    plan_repricing(
        _snapshot(), {"steam_id": "account", "cookies": "sessionid=x"}, _config(),
    )

    assert len(reads) == 1
    records = _records()
    assert len(records) == 1
    result = records[0]["result"]
    assert result["status"] == "unavailable"
    blocker = next(
        step for step in result["steps"]
        if step["status"] in {"reject", "wait", "unavailable", "unsupported"}
    )
    assert blocker["module_id"] == "pricing.steam_wall_price"
    assert "卖单" in blocker["reason"]
    assert result["steps"][-1]["module_id"] == "action.steam_list"
    assert result["steps"][-1]["status"] == "not_run"
    assert result["suggestion"]["action"] == "none"
    assert result["execution"]["status"] == "not_started"
    with database.get_session() as session:
        assert session.exec(select(RepriceJob)).first() is None



def test_strategy_listing_limit_is_explained_without_quote(reprice_env, monkeypatch):
    from app.services.repricing import plan_repricing
    from sqlmodel import select
    from app.trading_models import RepriceJob

    _seed_listing(due=True)
    _forbid_trading(monkeypatch)
    reads = []
    monkeypatch.setattr(
        "app.sell_pipeline.get_sell_orders_cny",
        lambda *args, **kwargs: reads.append((args, kwargs)) or {
            "sell_orders": [[13.00, 20]]
        },
    )
    snapshot = _snapshot()
    snapshot["asset-2"] = {
        "listing_id": "remote-2", "status": "active", "currency": "CNY",
        "buyer_minor": 1500, "seller_minor": 1305,
        "name": "AK-47 | Redline", "market_hash_name": "AK-47 | Redline",
    }

    cfg = _config()
    cfg["pipeline"]["max_listings_per_item"] = 1
    plan_repricing(
        snapshot, {"steam_id": "account", "cookies": "sessionid=x"}, cfg,
    )

    assert reads == []
    records = _records()
    assert len(records) == 1
    result = records[0]["result"]
    assert result["status"] == "reject"
    blocker = next(
        step for step in result["steps"]
        if step["status"] in {"reject", "wait", "unavailable", "unsupported"}
    )
    assert blocker["module_id"] == "guard.max_listings_per_item"
    assert "上限" in blocker["reason"]
    assert result["steps"][-1]["module_id"] == "action.steam_list"
    assert result["steps"][-1]["status"] == "not_run"
    assert result["suggestion"]["action"] == "none"
    assert result["execution"]["status"] == "not_started"
    with database.get_session() as session:
        assert session.exec(select(RepriceJob)).first() is None


def test_allowed_repricing_reports_suggestion_and_planned_job(reprice_env, monkeypatch):
    from app.services.repricing import plan_repricing
    from sqlmodel import select
    from app.trading_models import RepriceJob, TradeEvent

    _seed_listing(due=True)
    _forbid_trading(monkeypatch)
    reads = []
    monkeypatch.setattr(
        "app.sell_pipeline.get_sell_orders_cny",
        lambda *args, **kwargs: reads.append((args, kwargs)) or {
            "sell_orders": [[12.00, 20]]
        },
    )

    plan_repricing(
        _snapshot(), {"steam_id": "account", "cookies": "sessionid=x"}, _config(),
    )

    assert len(reads) == 1

    def execution_state():
        with database.get_session() as session:
            jobs = [
                (job.id, job.status, job.target_buyer_minor, job.target_seller_minor)
                for job in session.exec(select(RepriceJob).order_by(RepriceJob.id)).all()
            ]
            events = [
                (event.id, event.kind)
                for event in session.exec(select(TradeEvent).order_by(TradeEvent.id)).all()
            ]
        return jobs, events

    before_read = execution_state()
    records = _records()
    assert execution_state() == before_read  # Reading records never executes a trade.
    assert len(records) == 1
    result = records[0]["result"]
    assert result["status"] == "pass"
    assert result["suggestion"]["action"] == "adjust"
    assert result["suggestion"]["delta_buyer_minor"] >= result["steps"][-1]["threshold"]["required_delta_buyer_minor"]
    assert result["execution"]["status"] == "planned"
    assert result["execution"]["withdrawn"] is False
    assert result["execution"]["returned"] is False
    assert result["execution"]["relisted"] is False
    with database.get_session() as session:
        job = session.exec(select(RepriceJob)).one()
        assert job.status == "planned"
        assert job.target_buyer_minor == result["suggestion"]["target_buyer_minor"]
        assert job.target_seller_minor == result["suggestion"]["target_seller_minor"]
    assert execution_state() == before_read


def test_snapshot_failure_does_not_change_repricing_outcome(reprice_env, monkeypatch):
    from app.services.repricing import plan_repricing
    from sqlmodel import select
    from app.trading_models import RepriceJob

    _seed_listing(due=True)
    _forbid_trading(monkeypatch)
    reads = []
    monkeypatch.setattr(
        "app.sell_pipeline.get_sell_orders_cny",
        lambda *args, **kwargs: reads.append((args, kwargs)) or {
            "sell_orders": [[12.00, 20]]
        },
    )

    def unavailable_snapshot(*args, **kwargs):
        raise RuntimeError("strategy snapshot unavailable")

    monkeypatch.setattr("app.decision_records.snapshot_strategy", unavailable_snapshot)

    plan_repricing(
        _snapshot(), {"steam_id": "account", "cookies": "sessionid=x"}, _config(),
    )

    assert len(reads) == 1
    with database.get_session() as session:
        job = session.exec(select(RepriceJob)).one()
        assert job.status == "planned"
    assert _records() == []


def test_previous_completed_job_is_not_reported_as_current_execution(reprice_env, monkeypatch):
    from app.services.repricing import plan_repricing
    from sqlmodel import select
    from app.trading_models import RepriceJob

    purchase_id, listing_id = _seed_listing(due=True)
    with database.get_session() as session:
        session.add(RepriceJob(
            listing_id=listing_id, purchase_id=purchase_id, account_id="account",
            old_assetid="asset-1", assetid="asset-1", old_steam_listing_id="remote-1",
            status="completed", old_buyer_minor=1500, target_buyer_minor=1305,
            target_seller_minor=1135, floor_seller_minor=700,
        ))
        session.commit()
    _forbid_trading(monkeypatch)
    monkeypatch.setattr(
        "app.sell_pipeline.get_sell_orders_cny",
        lambda *args, **kwargs: {"sell_orders": [[12.00, 20]]},
    )

    plan_repricing(
        _snapshot(), {"steam_id": "account", "cookies": "sessionid=x"}, _config(),
    )

    result = _records()[0]["result"]
    assert result["suggestion"]["action"] == "adjust"
    assert result["execution"]["status"] == "not_started"
    with database.get_session() as session:
        job = session.exec(select(RepriceJob)).one()
        assert job.status == "completed"
