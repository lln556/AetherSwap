import pytest

from app.state import State


def test_dashboard_keeps_holdings_separate_from_realized_results(trading_db):
    from app.routes import transactions

    for row in [
        {"name": "Holding", "price": 20, "at": 100},
        {"name": "Pending", "price": 30, "at": 300, "pending_receipt": True, "sale_price": 0},
        {"name": "Sold", "price": 50, "at": 200, "sale_price": 115},
    ]:
        trading_db.db_append_purchase(row)
    result = transactions.api_stats()

    assert result["holding_cost"] == 50
    assert result["holding_count"] == 2
    assert result["total_purchased"] == 50  # Existing clients keep sold-cost semantics.
    assert result["total_sold"] == 115
    assert result["total_profit"] == pytest.approx(50)
    assert result["sold_count"] == 1
    assert result["discount_ratio"] == pytest.approx(0.5)
    assert result["last_purchase_at"] == 300
    assert [p["name"] for p in result["recent_purchases"]] == ["Pending", "Sold", "Holding"]
    assert result["recent_purchases"][0]["pending_receipt"] is True
    assert result["recent_purchases"][1]["sold"] is True


def test_empty_dashboard_has_no_purchase_date_or_discount(trading_db):
    from app.routes import transactions

    result = transactions.api_stats()
    assert result["holding_cost"] == 0
    assert result["holding_count"] == 0
    assert result["last_purchase_at"] is None
    assert result["discount_ratio"] is None
    assert result["recent_purchases"] == []


def test_scan_counts_final_outcome_once_and_survives_log_rotation():
    state = State()
    state.begin_scan()
    state.set_scan_candidates(3)
    state.record_scan_result("one", "discount")
    state.record_scan_result("one", "discount")
    state.record_scan_result("two", "passed")
    state.record_scan_result("three", "history_data")
    for i in range(600):
        state.log(str(i))
    state.clear_log()
    state.set_status("running", "WAITING_RETRY")
    summary = state.get_status()["scan_summary"]
    assert summary["candidates"] == 3
    assert summary["checked"] == 3
    assert summary["passed"] == 1
    assert summary["reasons"] == {"discount": 1, "history_data": 1}

    state.begin_scan()
    summary = state.get_status()["scan_summary"]
    assert summary["checked"] == summary["candidates"] == summary["passed"] == 0
    assert summary["reasons"] == {}


def test_dashboard_status_exposes_thread_activity_and_session_metadata(monkeypatch):
    from app.routes import status
    from app import accounts, pipeline
    from app.services import steam_auth

    monkeypatch.setattr(status, "get_status", lambda: {"status": "stopped"})
    monkeypatch.setattr(pipeline, "is_pipeline_running", lambda: True)
    monkeypatch.setattr(status, "get_buff", lambda: {"cookies": "private-buff-cookie"})
    monkeypatch.setattr(status, "get_steam", lambda: {"cookies": "private-steam-cookie"})
    monkeypatch.setattr(steam_auth, "steam_cookie_expiry", lambda _cookies: 1792136183)
    monkeypatch.setattr(accounts, "get_current_account", lambda: {
        "password": "private-password", "currency_code": "CNY", "region_code": "CN", "region_check_ok": True,
    })
    monkeypatch.setattr(status, "load_app_config_validated", lambda: {"system": {"steam_session_keepalive_minutes": 0}})

    result = status.api_status()
    assert result["pipeline_active"] is True  # Stop requested, thread still cleaning up.
    assert result["steam_session_expires_at"] == 1792136183
    assert result["steam_keepalive_enabled"] is False
    assert result["buff_no_cookie"] is False
    assert result["account_region"] == {"currency_code": "CNY", "region_code": "CN", "region_check_ok": True}
    assert "private" not in str(result)


@pytest.fixture
def trading_db(tmp_path, monkeypatch):
    from sqlmodel import create_engine
    from app import database

    engine = create_engine(f"sqlite:///{tmp_path / 'dashboard.db'}")
    monkeypatch.setattr(database, "_engine", engine)
    database.init_db()
    monkeypatch.setattr("app.config_loader.get_steam_credentials", lambda: {"steam_id": "account"})
    yield database
    engine.dispose()


@pytest.mark.parametrize("currency", ["CNY", "USD"])
def test_dashboard_and_trading_agree_after_verified_sale(trading_db, currency):
    from app.routes.transactions import api_stats
    from app.trading_ledger import record_settlements, trading_overview

    trading_db.db_append_purchase({"name": "Sold", "price": 5, "at": 100, "assetid": "asset", "account_id": "account"})
    record_settlements([{"assetid": "asset", "external_id": "sale-1", "proceeds_minor": 821,
                         "currency": currency, "sold_day": "2026-10-18"}], "account")
    trading = trading_overview()
    assert trading["items"][0]["state"] == "sold"
    stats = api_stats()
    assert stats["sold_count"] == 1
    assert stats["holding_count"] == 0
    assert stats["holding_cost"] == 0
    assert stats["recent_purchases"][0]["sold"] is True


def test_settlement_not_legacy_sale_price_is_the_sale_evidence(trading_db):
    from app.routes.transactions import api_stats
    from app.trading_ledger import record_settlements, trading_overview

    trading_db.db_append_purchase({"name": "Sold", "price": 5, "at": 100, "assetid": "asset"})
    record_settlements([{"assetid": "asset", "external_id": "sale-1", "proceeds_minor": 821,
                         "currency": "CNY"}], "account")
    with trading_db.get_session() as s:
        row = s.get(trading_db.Purchase, 1)
        row.sale_price = None
        s.add(row)
        s.commit()
    assert trading_overview()["items"][0]["state"] == "sold"
    assert api_stats()["sold_count"] == 1


def test_archiving_sale_preserves_dashboard_history_but_not_holdings(trading_db):
    from app.routes.transactions import api_stats
    from app.trading_ledger import trading_overview

    trading_db.db_append_purchase({"name": "Sold", "price": 50, "at": 100, "sale_price": 115})
    trading_db.db_append_purchase({"name": "Archived holding", "price": 20, "at": 200})
    trading_db.db_delete_purchase_by_id(1)
    trading_db.db_delete_purchase_by_id(2)
    trading = trading_overview()
    assert trading["settlements_total"] == 1
    assert {p["state"] for p in trading["items"]} == {"sold", "archived"}
    stats = api_stats()
    assert stats["sold_count"] == 1
    assert stats["holding_count"] == 0
    assert stats["total_purchased"] == 50


@pytest.mark.parametrize("proceeds", [0, 1, 821, None])
def test_settlement_existence_controls_state_not_amount(trading_db, proceeds):
    from app.routes.transactions import api_stats
    from app.trading_ledger import trading_overview
    from app.trading_models import TradeSettlement

    trading_db.db_append_purchase({"name": "Sold", "price": 5, "at": 100})
    with trading_db.get_session() as s:
        s.add(TradeSettlement(purchase_id=1, proceeds_minor=proceeds, currency="USD", amount_source="steam_history"))
        s.commit()
    assert trading_overview()["items"][0]["state"] == "sold"
    assert api_stats()["sold_count"] == 1


def test_net_proceeds_and_currencies_are_not_roundtripped_or_mixed(trading_db):
    from app.routes.transactions import api_stats
    from app.trading_models import TradeSettlement

    for index, (cost, currency, proceeds) in enumerate([(0.01, "CNY", 4), (30, "USD", 1000)], 1):
        trading_db.db_append_purchase({"name": currency, "price": cost, "at": index})
        with trading_db.get_session() as s:
            s.add(TradeSettlement(purchase_id=index, proceeds_minor=proceeds, currency=currency, amount_source="steam_history"))
            s.commit()
    stats = api_stats()
    assert stats["sold_count"] == 2 and stats["holding_count"] == 0
    assert stats["total_purchased"] == 30.01  # All original costs are CNY.
    assert stats["total_profit"] == 0.03  # USD proceeds and their cost are excluded.
    assert stats["discount_ratio"] == 0.25
    assert stats["proceeds_by_currency"]["CNY"]["proceeds_minor"] == 4
    assert stats["proceeds_by_currency"]["USD"]["proceeds_minor"] == 1000
    assert stats["cny_settled_count"] == 1


def test_unknown_proceeds_are_not_a_zero_value_sale(trading_db):
    from app.routes.transactions import api_stats
    from app.trading_models import TradeSettlement

    trading_db.db_append_purchase({"name": "Unknown", "price": 10, "at": 100})
    with trading_db.get_session() as s:
        s.add(TradeSettlement(purchase_id=1, proceeds_minor=None, currency="CNY"))
        s.commit()
    stats = api_stats()
    assert stats["sold_count"] == 1 and stats["holding_count"] == 0
    assert stats["total_profit"] is None
    assert stats["discount_ratio"] is None
    assert stats["proceeds_by_currency"]["CNY"]["proceeds_minor"] is None
    assert stats["unknown_proceeds_count"] == 1


def test_manual_and_legacy_settlements_keep_source_information(trading_db):
    from app.routes.transactions import api_stats, api_update_transaction, TransactionUpdateBody
    from app.trading_ledger import trading_overview

    trading_db.db_append_purchase({"name": "Legacy", "price": 50, "at": 100, "sale_price": 115})
    trading_db.db_append_purchase({"name": "Manual", "price": 20, "at": 200})
    assert api_update_transaction(TransactionUpdateBody(type="purchase", db_id=2, sale_proceeds=30.01))["ok"]
    stats = api_stats()
    assert stats["sold_count"] == 2 and stats["holding_count"] == 0
    assert stats["total_profit"] == 60.01
    assert stats["proceeds_by_currency"]["CNY"]["proceeds_minor"] == 13001
    assert stats["estimated_sold_count"] == 1
    assert stats["proceeds_by_currency"]["CNY"]["estimated_count"] == 1
    assert {r["settlement"]["amount_source"] for r in trading_overview()["items"]} == {"estimated_legacy", "user_reported"}
    assert api_update_transaction(TransactionUpdateBody(type="purchase", db_id=2, sale_proceeds=0))["ok"]
    stats = api_stats()
    assert stats["sold_count"] == 1 and stats["holding_count"] == 1
    assert trading_overview()["items"][0]["state"] == "holding"


def test_missing_settlement_never_falls_back_to_stale_sale_price(trading_db):
    from app.routes.transactions import api_stats
    from app.trading_ledger import trading_overview
    from app.trading_models import TradeSettlement

    trading_db.db_append_purchase({"name": "Stale", "price": 10, "at": 100, "sale_price": 23})
    with trading_db.get_session() as s:
        s.delete(s.get(TradeSettlement, 1))
        s.commit()
    assert trading_overview()["items"][0]["state"] == "holding"
    assert api_stats()["sold_count"] == 0
    assert api_stats()["holding_count"] == 1


def test_pending_receipt_and_unconfirmed_listings_remain_unsold(trading_db):
    from app.routes.transactions import api_stats
    from app.trading_ledger import trading_overview

    trading_db.db_append_purchase({"name": "Pending", "price": 20, "at": 100, "pending_receipt": True})
    trading_db.db_append_purchase({"name": "Listed", "price": 30, "at": 200, "listing": True})
    stats = api_stats()
    assert stats["sold_count"] == trading_overview()["settlements_total"] == 0
    assert stats["holding_count"] == 2 and stats["holding_cost"] == 50
    assert all(not p["sold"] for p in stats["recent_purchases"])


def test_closed_order_is_not_a_holding_or_recent_purchase(trading_db):
    from app.routes.transactions import api_stats

    trading_db.db_append_purchase({"name": "Refunded", "price": 20, "at": 100,
                                   "archived_at": 200, "archive_reason": "order_closed"})
    stats = api_stats()
    assert stats["sold_count"] == stats["holding_count"] == 0
    assert stats["recent_purchases"] == [] and stats["last_purchase_at"] is None
