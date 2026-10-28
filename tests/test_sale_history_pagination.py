"""Offline regressions for the market-history pagination boundary."""
from types import SimpleNamespace

import pytest

from app import steam_sale_history as history


def history_page(start, count, total):
    """Put the owned sale on the final page; earlier rows are unrelated history."""
    ids = range(start, min(start + count, total))
    return {
        "success": True,
        "total_count": total,
        "results_html": "".join(
            f'<div class="market_listing_row" id="history_row_{i}_1">'
            '<div class="market_listing_listed_date_combined">Sold: 2026-10-18</div>'
            '<span class="market_listing_price">¥ 8.21</span></div>' for i in ids
        ),
        "hovers": "".join(
            f"CreateItemHoverFromContainer( g_rgAssets, 'history_row_{i}_1_name', 730, '2', '{i}');"
            for i in ids
        ),
    }


def install_history(monkeypatch, total, transform=None, page_size=100):
    calls = []

    def get_page(url, params, *args):
        start, count = params["start"], params["count"]
        calls.append(start)
        data = history_page(start, min(count, page_size), total)
        if transform:
            data = transform(start, data)
        return SimpleNamespace(status_code=200, json=lambda: data)

    monkeypatch.setattr(history, "_get_with_retry", get_page)
    return calls


@pytest.mark.parametrize("total,page_size", [(40, 1), (41, 1), (4000, 100), (4001, 100)])
def test_history_reaches_owned_sale_beyond_forty_pages(monkeypatch, total, page_size):
    calls = install_history(monkeypatch, total, page_size=page_size)
    ok, records, error = history.fetch_sale_history("test-cookie", {str(total - 1): 1})
    assert ok, error
    assert len(records) == 1
    assert records[0]["external_id"] == f"history_row_{total - 1}_1"
    assert records[0]["proceeds_minor"] == 821
    assert calls == list(range(0, total, page_size))


@pytest.mark.parametrize("owned", [{"0": 1}, {"200": 1}, {"absent": 1}])
def test_offsets_include_unowned_rows(monkeypatch, owned):
    calls = install_history(monkeypatch, 201)
    ok, records, error = history.fetch_sale_history("test-cookie", owned)
    assert ok, error
    assert calls == [0, 100, 200]
    assert len(records) == (0 if "absent" in owned else 1)


@pytest.mark.parametrize("fault", ["repeat", "empty", "duplicate", "grow", "shrink", "overrun"])
def test_inconsistent_pages_discard_partial_evidence(monkeypatch, fault):
    def corrupt(start, data):
        if start == 100:
            if fault == "repeat":
                return history_page(0, 100, 201)
            if fault == "empty":
                data["results_html"] = ""
            if fault == "duplicate":
                data["results_html"] += data["results_html"]
            if fault in ("grow", "shrink"):
                data["total_count"] = 301 if fault == "grow" else 100
            if fault == "overrun":
                return history_page(100, 102, 202) | {"total_count": 201}
        return data

    calls = install_history(monkeypatch, 201, corrupt)
    ok, records, error = history.fetch_sale_history("test-cookie", {"0": 1})
    assert not ok
    assert records == []
    assert error
    assert calls == [0, 100]


@pytest.mark.parametrize("total", [None, -1, True, 1.5, "invalid"])
def test_invalid_total_cannot_certify_complete_history(monkeypatch, total):
    calls = install_history(monkeypatch, 1, lambda start, data: data | {"total_count": total})
    ok, records, error = history.fetch_sale_history("test-cookie", {"0": 1})
    assert not ok and not records and error
    assert calls == [0]


def test_empty_history_completes_in_one_request(monkeypatch):
    calls = install_history(monkeypatch, 0)
    assert history.fetch_sale_history("test-cookie", {"0": 1}) == (True, [], "")
    assert calls == [0]


@pytest.fixture
def market_cycle(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from sqlmodel import create_engine
    from app import database
    from app.services import repricing

    engine = create_engine(f"sqlite:///{tmp_path / 'history.db'}")
    monkeypatch.setattr(database, "_engine", engine)
    database.init_db()
    monkeypatch.setattr("app.config_loader.get_steam_credentials", lambda: {"steam_id": "account", "cookies": "test-cookie"})
    monkeypatch.setattr("app.config_loader.load_app_config_validated", lambda: {})
    monkeypatch.setattr("app.steam_listings.fetch_listing_snapshot", lambda *a: (True, {}, ""))
    monkeypatch.setattr("app.services.listing_confirmation.sync_listing_confirmations", lambda *a, **kw: (True, {}, ""))
    resume, plan = Mock(), Mock()
    monkeypatch.setattr(repricing, "resume_jobs", resume)
    monkeypatch.setattr(repricing, "plan_repricing", plan)
    yield database, resume, plan
    engine.dispose()


def test_large_history_cycle_records_sale_once_and_reaches_repricing(monkeypatch, market_cycle):
    from sqlmodel import select
    from app.services.trading_worker import run_trading_cycle
    from app.trading_models import TradeSettlement, TradeEvent

    database, resume, plan = market_cycle
    database.db_append_purchase({"name": "Item", "price": 5, "at": 1, "assetid": "4000", "account_id": "account"})
    calls = install_history(monkeypatch, 4001)
    first = run_trading_cycle()
    assert first["ok"], first["error"]
    assert first["sold"] == 1
    resume.assert_called_once()
    plan.assert_called_once()
    second = run_trading_cycle()
    assert second["ok"] and second["sold"] == 0
    assert calls == list(range(0, 4001, 100)) * 2
    with database.get_session() as s:
        settlements = s.exec(select(TradeSettlement)).all()
        assert len(settlements) == 1 and settlements[0].proceeds_minor == 821
        assert len(s.exec(select(TradeEvent).where(TradeEvent.kind == "sold")).all()) == 1


@pytest.mark.parametrize("failure", ["http", "exception", "duplicate", "missing", "changed"])
def test_incomplete_history_blocks_settlement_and_repricing(monkeypatch, market_cycle, failure):
    from sqlmodel import select
    from app.services.trading_worker import run_trading_cycle
    from app.trading_models import TradeSettlement

    database, resume, plan = market_cycle
    database.db_append_purchase({"name": "Item", "price": 5, "at": 1, "assetid": "0", "account_id": "account"})
    calls = []

    def get_page(url, params, *args):
        start = params["start"]
        calls.append(start)
        data = history_page(start, 100, 201)
        if start:
            if failure == "http":
                return SimpleNamespace(status_code=429)
            if failure == "exception":
                raise TimeoutError("offline simulated timeout")
            if failure == "duplicate":
                data = history_page(0, 100, 201)
            if failure == "missing":
                data["results_html"] = ""
            if failure == "changed":
                data["total_count"] = 100
        return SimpleNamespace(status_code=200, json=lambda: data)

    monkeypatch.setattr(history, "_get_with_retry", get_page)
    result = run_trading_cycle()
    assert not result["ok"] and result["sold"] == 0
    assert calls == [0, 100]
    resume.assert_not_called()
    plan.assert_not_called()
    with database.get_session() as s:
        assert s.exec(select(TradeSettlement)).all() == []
        assert s.get(database.Purchase, 1).sale_price is None


def test_missing_total_does_not_assume_first_page_is_complete(monkeypatch):
    def omit_total(start, data):
        data.pop("total_count")
        return data

    calls = install_history(monkeypatch, 201, omit_total)
    ok, records, error = history.fetch_sale_history("test-cookie", {"0": 1})
    assert not ok and not records and error
    assert calls == [0]


def test_numeric_string_total_and_short_pages_are_supported(monkeypatch):
    calls = install_history(monkeypatch, 3, lambda start, data: data | {"total_count": "3"}, page_size=1)
    ok, records, error = history.fetch_sale_history("test-cookie", {"2": 1})
    assert ok, error
    assert len(records) == 1 and calls == [0, 1, 2]


def test_shutdown_aborts_between_pages_without_partial_success(monkeypatch):
    calls = install_history(monkeypatch, 201)
    monkeypatch.setattr(history, "is_shutting_down", lambda: bool(calls))
    ok, records, error = history.fetch_sale_history("test-cookie", {"0": 1})
    assert not ok and not records and "停止" in error
    assert calls == [0]
