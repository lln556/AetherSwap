import importlib
import json
from unittest.mock import Mock

import pytest

from app.services import market_price_cache as cache


@pytest.fixture
def quote_cache(monkeypatch, tmp_path):
    path = tmp_path / "quotes.json"
    monkeypatch.setattr(cache, "_PATH", path)
    monkeypatch.setattr(cache, "get_steam_credentials", lambda: {"steam_id": "account-a"})
    return path


def test_quotes_survive_module_restart(monkeypatch, quote_cache):
    cache.remember_market_prices({"held item": 23.0}, "account-a")
    importlib.reload(cache)
    monkeypatch.setattr(cache, "_PATH", quote_cache)
    monkeypatch.setattr(cache, "get_steam_credentials", lambda: {"steam_id": "account-a"})
    prices, timestamp = cache.get_cached_market_prices({"held item"})
    assert prices == {"held item": 23.0}
    assert timestamp > 0


def test_cache_is_scoped_by_account_and_game(monkeypatch, quote_cache):
    cache.remember_market_prices({"item": 23.0}, "account-a")
    assert cache.get_cached_market_prices({"item"}, app_id=570) == ({}, None)
    monkeypatch.setattr(cache, "get_steam_credentials", lambda: {"steam_id": "account-b"})
    assert cache.get_cached_market_prices({"item"}) == ({}, None)


def test_partial_updates_preserve_other_quotes(quote_cache):
    cache.remember_market_prices({"first": 23, "second": 9}, "account-a")
    cache.remember_market_prices({"first": 24}, "account-a")
    assert cache.get_cached_market_prices({"first", "second"})[0] == {"first": 24, "second": 9}


def test_invalid_prices_are_never_displayed(quote_cache):
    cache.remember_market_prices({"zero": 0, "negative": -1, "bool": True,
                                  "nan": float("nan"), "inf": float("inf"), "valid": 3.5}, "account-a")
    assert cache.get_cached_market_prices({"zero", "negative", "bool", "nan", "inf", "valid"})[0] == {"valid": 3.5}


def test_corrupt_cache_can_be_replaced_with_live_quotes(quote_cache):
    quote_cache.write_text("corrupt")
    assert cache.get_cached_market_prices({"item"}) == ({}, None)
    cache.remember_market_prices({"item": 23}, "account-a")
    assert json.loads(quote_cache.read_text())["730:account-a"]["item"]["price"] == 23


@pytest.mark.parametrize("allowed", [False, True])
def test_busy_or_failed_refresh_retains_last_quote(monkeypatch, quote_cache, allowed):
    from app.routes import inventory
    cache.remember_market_prices({"held item": 23}, "account-a")
    fetch = Mock(return_value={})
    monkeypatch.setattr(inventory, "get_inventory", lambda: [])
    monkeypatch.setattr(inventory, "is_steam_background_allowed", lambda: allowed)
    monkeypatch.setattr(inventory, "batch_fetch_prices", fetch)
    monkeypatch.setattr("app.state.get_purchases", lambda: [{"name": "held item"}])
    result = inventory.api_market_prices()
    assert result["prices"] == {"held item": 23}
    assert result["source"] == "cache"
    assert result["updated_at"] > 0
    if allowed:
        fetch.assert_called_once()
        assert result["error"]
    else:
        fetch.assert_not_called()
        assert result["refresh_deferred"]


def test_unmarketable_personal_items_are_excluded_but_protected_holdings_are_quoted(monkeypatch, quote_cache):
    from app.routes import inventory
    fetch = Mock(return_value={"protected holding": 23, "sticker": 1})
    monkeypatch.setattr(inventory, "get_inventory", lambda: [
        {"market_hash_name": "personal badge", "marketable": 0},
        {"market_hash_name": "protected holding", "marketable": 0},
        {"market_hash_name": "sticker", "marketable": 1},
    ])
    monkeypatch.setattr(inventory, "is_steam_background_allowed", lambda: True)
    monkeypatch.setattr(inventory, "batch_fetch_prices", fetch)
    monkeypatch.setattr("app.state.get_purchases", lambda: [{"name": "protected holding"}])
    result = inventory.api_market_prices()
    fetch.assert_called_once_with({"protected holding", "sticker"})
    assert result["prices"] == {"protected holding": 23, "sticker": 1}
    assert not result.get("error")


def test_fresh_quotes_within_max_age_skip_steam(monkeypatch, quote_cache):
    from app.routes import inventory
    cache.remember_market_prices({"held item": 23}, "account-a")
    fetch = Mock(return_value={})
    monkeypatch.setattr(inventory, "get_inventory", lambda: [])
    monkeypatch.setattr(inventory, "is_steam_background_allowed", lambda: True)
    monkeypatch.setattr(inventory, "batch_fetch_prices", fetch)
    monkeypatch.setattr("app.state.get_purchases", lambda: [{"name": "held item"}])
    result = inventory.api_market_prices(max_age=600)
    fetch.assert_not_called()
    assert result["prices"] == {"held item": 23}
    assert result["source"] == "cache"
    assert not result.get("error")


def test_stale_quotes_are_refetched_with_max_age(monkeypatch, quote_cache):
    import time as _time
    from app.routes import inventory
    quote_cache.write_text(json.dumps({"730:account-a": {
        "held item": {"price": 23, "at": _time.time() - 3600},
    }}))
    fetch = Mock(return_value={"held item": 25})
    monkeypatch.setattr(inventory, "get_inventory", lambda: [])
    monkeypatch.setattr(inventory, "is_steam_background_allowed", lambda: True)
    monkeypatch.setattr(inventory, "batch_fetch_prices", fetch)
    monkeypatch.setattr("app.state.get_purchases", lambda: [{"name": "held item"}])
    result = inventory.api_market_prices(max_age=600)
    fetch.assert_called_once_with({"held item"})
    assert result["prices"] == {"held item": 25}
    assert result["source"] == "live"


def test_only_stale_names_are_refetched(monkeypatch, quote_cache):
    import time as _time
    from app.routes import inventory
    quote_cache.write_text(json.dumps({"730:account-a": {
        "fresh item": {"price": 10, "at": _time.time()},
        "stale item": {"price": 20, "at": _time.time() - 3600},
    }}))
    fetch = Mock(return_value={"stale item": 21})
    monkeypatch.setattr(inventory, "get_inventory", lambda: [])
    monkeypatch.setattr(inventory, "is_steam_background_allowed", lambda: True)
    monkeypatch.setattr(inventory, "batch_fetch_prices", fetch)
    monkeypatch.setattr("app.state.get_purchases",
                        lambda: [{"name": "fresh item"}, {"name": "stale item"}])
    result = inventory.api_market_prices(max_age=600)
    fetch.assert_called_once_with({"stale item"})
    assert result["prices"] == {"fresh item": 10, "stale item": 21}

