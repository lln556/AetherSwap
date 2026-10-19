"""Operations release their owned HTTPX pools on success and early exits."""
from unittest.mock import Mock

import httpx
import pytest

from utils import http_client as http


@pytest.fixture
def sessions(monkeypatch):
    created = []
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda *_: httpx.Response(200))

    def factory(*args, **kwargs):
        session = http.Client(trust_env=False)
        session.get('https://steamcommunity.com/')
        created.append((session, list(session._clients.values())))
        return session

    yield factory
    assert created
    for session, pools in created:
        try:
            assert session._closed
            assert all(pool.is_closed for pool in pools)
        finally:
            session.close()


@pytest.mark.parametrize('fails', [False, True])
def test_inventory_closes_session_on_success_and_exception(monkeypatch, sessions, fails):
    from app import inventory_cs2
    monkeypatch.setattr(inventory_cs2, 'get_steam', lambda: {'steam_id': 'test-account', 'cookies': 'test'})
    monkeypatch.setattr(inventory_cs2, 'create_market_session', sessions)
    fetch = Mock(side_effect=RuntimeError('test') if fails else None, return_value=None)
    monkeypatch.setattr(inventory_cs2, 'fetch_cs2_inventory', fetch)
    if fails:
        with pytest.raises(RuntimeError):
            inventory_cs2.scan_cs2_inventory()
    else:
        assert inventory_cs2.scan_cs2_inventory()[0] is False


@pytest.mark.parametrize('fails', [False, True])
def test_price_enrichment_closes_session_and_releases_inflight_waiters(monkeypatch, sessions, fails):
    from app import shared_market
    monkeypatch.setattr('steam.session.create_market_session', sessions)
    query = Mock(side_effect=RuntimeError('test') if fails else None, return_value=2.0)
    monkeypatch.setattr(shared_market, 'get_steam_smart_price_cny', query)
    assert shared_market._fetch_one_deduped('test-item', 730, 'test', 'test-account') == (
        'test-item', None if fails else 2.0,
    )
    assert (730, 'test-item') not in shared_market._inflight_fetches


@pytest.mark.parametrize('fails', [False, True])
def test_purchase_market_check_closes_session_on_empty_result_and_error(monkeypatch, sessions, fails):
    from app.pipeline_steps import _fetch_steam_sell_data
    monkeypatch.setattr('app.config_loader.get_steam_credentials', lambda: {
        'steam_id': 'test-account', 'cookies': 'test',
    })
    monkeypatch.setattr('steam.session.create_market_session', sessions)
    query = Mock(side_effect=RuntimeError('test') if fails else None, return_value=(None, 'empty'))
    monkeypatch.setattr('steam.market_orders.get_sell_orders_cny', query)
    assert _fetch_steam_sell_data('test-item', {}, return_error=True)[0] is None


@pytest.mark.parametrize('fails', [False, True])
def test_sell_phase_closes_session_when_inventory_empty_or_raises(monkeypatch, sessions, fails):
    from app import sell_pipeline
    monkeypatch.setattr(sell_pipeline, 'apply_strategy_to_config', lambda cfg, _: cfg)
    monkeypatch.setattr(sell_pipeline, 'get_steam_credentials', lambda: {})
    monkeypatch.setattr(sell_pipeline, '_resolve_steam_session', lambda *_: (sessions(), 'test'))
    query = Mock(side_effect=RuntimeError('test') if fails else None, return_value=[])
    monkeypatch.setattr(sell_pipeline, '_get_inventory', query)
    if fails:
        with pytest.raises(RuntimeError):
            sell_pipeline._run_sell_phase_impl({}, Mock(), 'test')
    else:
        sell_pipeline._run_sell_phase_impl({}, Mock(), 'test')
