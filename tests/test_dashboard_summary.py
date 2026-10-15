import pytest

from app.state import State


def test_dashboard_keeps_holdings_separate_from_realized_results(monkeypatch):
    from app.routes import transactions

    monkeypatch.setattr(transactions, "get_purchases", lambda: [
        {"name": "Holding", "price": 20, "at": 100},
        {"name": "Pending", "price": 30, "at": 300, "pending_receipt": True, "sale_price": 0},
        {"name": "Sold", "price": 50, "at": 200, "sale_price": 115},
    ])
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


def test_empty_dashboard_has_no_purchase_date_or_discount(monkeypatch):
    from app.routes import transactions

    monkeypatch.setattr(transactions, "get_purchases", lambda: [])
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
