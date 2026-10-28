"""Public decision APIs using an isolated database and real strategy snapshots."""
import copy
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

from app import database, strategy_engine as se
from app.config_schema import DEFAULTS


@pytest.fixture
def decisions(tmp_path, monkeypatch):
    from app import decision_records as records
    from app.routes.strategies import router
    url = os.environ.get('TEST_DECISION_DATABASE_URL') or f"sqlite:///{tmp_path / 'decisions.db'}"
    engine = create_engine(url, **({'connect_args': {'check_same_thread': False}} if url.startswith('sqlite:') else {}))
    monkeypatch.setattr(database, "_engine", engine)
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(se, "STRATEGY_STORE_PATH", tmp_path / 'strategies.json')
    monkeypatch.setattr(se, "USER_MODULE_STORE_PATH", tmp_path / 'modules.json')
    from app import accounts
    monkeypatch.setattr(accounts, "get_current_account", lambda: {"id": "account-a"})
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield records, client
    engine.dispose()


def capture(records, **kwargs):
    cfg = se.apply_strategy_to_config(copy.deepcopy(DEFAULTS), 'buy')
    return records.capture_decision(
        cfg, 'buy', {'name': 'Test item', 'goods_id': 123, 'cookie': 'secret-cookie'},
        {'buff_price': 74, 'steam_price': 115, 'currency': 'CNY', 'token': 'secret-token', 'api_key': 'secret-api', 'privateKey': 'secret-private', 'auth': 'secret-auth', 'payment_link': 'secret-link', 'note': 'https://pay.example/secret-link?token=secret-value'},
        {'status': 'reject', 'steps': [{'module_id': 'guard.max_discount', 'status': 'reject', 'reason': '成本率高于上限'}]},
        **kwargs,
    )


def test_record_is_immutable_persistent_safe_and_filterable(decisions, monkeypatch):
    records, client = decisions
    rid = capture(records)
    assert rid
    payload = client.get(f'/api/strategy-decisions/{rid}').json()
    assert payload['ok']
    assert payload['record']['item']['name'] == 'Test item'
    assert payload['record']['account_id'] == 'account-a'
    assert 'secret-' not in str(payload)
    assert payload['record']['strategy']['steps']
    listing = client.get('/api/strategy-decisions?kind=buy&name=Test&status=reject').json()
    assert listing['total'] == 1
    assert listing['items'][0]['id'] == rid
    assert client.get('/api/strategy-decisions?kind=sell').json()['total'] == 0
    database.get_engine().dispose()  # New connections read the committed record.
    assert client.get(f'/api/strategy-decisions/{rid}').json()['record'] == payload['record']


def test_legacy_simulation_requires_real_record(decisions):
    _, client = decisions
    result = client.post('/api/strategies/simulate', json={'strategy_id': 'system.buy.default'}).json()
    assert result['ok'] is False
    assert '检查记录' in result['error']
    assert not result.get('results')


def test_save_copy_uses_recorded_parameters_without_activating(decisions, monkeypatch):
    records, client = decisions
    cfg = se.apply_strategy_to_config(copy.deepcopy(DEFAULTS), 'buy')
    strategy, modules = records.snapshot_strategy(cfg, 'buy')
    idx = next(i for i, step in enumerate(strategy['steps']) if step['module_id'] == 'guard.max_discount')
    rid = records.capture_decision(cfg, 'buy', {'name': 'Recorded'}, {}, {'status': 'reject', 'steps': []})
    original = client.get(f'/api/strategy-decisions/{rid}').json()['record']['strategy']
    monkeypatch.setattr(se, 'save_app_config_validated', lambda *a, **k: pytest.fail('must not change active config'))
    payload = {'name': original['name'], 'params': {str(idx): {'max_discount': 0.75}}}
    result = client.post(f'/api/strategy-decisions/{rid}/copy', json=payload).json()
    assert result['ok'], result
    assert result['strategy']['id'] != original['id']
    assert result['strategy']['steps'][idx]['params']['max_discount'] == .75
    assert se.get_strategy(result['strategy']['id']) == result['strategy']
    second = client.post(f'/api/strategy-decisions/{rid}/copy', json=payload).json()
    assert second['strategy']['id'] != result['strategy']['id']
    assert client.get(f'/api/strategy-decisions/{rid}').json()['record']['strategy'] == original
    bad = client.post(f'/api/strategy-decisions/{rid}/copy', json={'name': 'Bad', 'params': {str(idx): {'enabled': False}}}).json()
    assert bad['ok'] is False


def test_retention_cleans_versions_without_touching_purchase_history(decisions, monkeypatch):
    records, client = decisions
    from sqlmodel import Session
    fixed = time.time()
    monkeypatch.setattr(records.time, 'time', lambda: fixed)
    rid = capture(records)
    records.add_version(rid, {'buff_price': 75}, fixed)
    with Session(database.get_engine()) as session:
        session.add(database.Purchase(name='Keep trade', price=74, at=fixed))
        session.commit()
    fixed += 7 * 86400 - 1
    assert client.get(f'/api/strategy-decisions/{rid}').json()['ok']
    fixed += 1
    result = client.get('/api/strategy-decisions').json()
    assert result['total'] == 0
    assert client.get(f'/api/strategy-decisions/{rid}').json()['ok'] is False
    with pytest.raises(records.DecisionError):
        records.add_version(rid, {'buff_price': 76}, fixed)
    from sqlmodel import select
    from app.decision_models import StrategyDecisionVersion
    with Session(database.get_engine()) as session:
        assert session.exec(select(database.Purchase)).one().name == 'Keep trade'
        assert session.exec(select(StrategyDecisionVersion)).all() == []


def test_ten_thousand_record_limit_evicts_oldest_and_associated_versions(decisions):
    records, client = decisions
    from sqlmodel import Session
    from app.decision_models import StrategyDecision
    oldest = capture(records)
    records.add_version(oldest, {'buff_price': 75}, time.time())
    with Session(database.get_engine()) as session:
        template = session.get(StrategyDecision, oldest)
        template.created_at = time.time() - 100
        session.add(template)
        for i in range(9999):
            session.add(StrategyDecision(id=f'seed-{i}', kind='buy', account_id='a', name='Seed', status='reject', evaluated_at=time.time(), created_at=template.created_at + 1 + i / 1000, payload=template.payload))
        session.commit()
    newest = capture(records)
    assert newest
    listing = client.get('/api/strategy-decisions?page_size=1').json()
    assert listing['total'] == 10_000
    assert listing['items'][0]['id'] == newest
    assert client.get(f'/api/strategy-decisions/{oldest}').json()['ok'] is False
    assert client.get('/api/strategy-decisions?page=10000&page_size=1').json()['items'][0]['id'] == 'seed-0'


def test_recording_failure_is_visible_and_read_failure_is_not_empty(decisions, monkeypatch):
    records, client = decisions
    def unavailable():
        raise RuntimeError('password=do-not-return')
    with monkeypatch.context() as patch:
        patch.setattr(database, 'get_engine', unavailable)
        assert capture(records) is None
        result = client.get('/api/strategy-decisions').json()
        assert result['ok'] is False
        assert 'do-not-return' not in str(result)
    assert '写入失败' in client.get('/api/strategy-decisions').json()['recording_error']


def test_unsupported_record_keeps_explanation_but_forbids_copy(decisions):
    records, client = decisions
    from sqlmodel import Session
    from app.decision_models import StrategyDecision
    rid = capture(records)
    with Session(database.get_engine()) as session:
        row = session.get(StrategyDecision, rid)
        row.schema_version = 999
        session.add(row)
        session.commit()
    assert client.get(f'/api/strategy-decisions/{rid}').json()['record']['result']['status'] == 'reject'
    assert client.post('/api/strategies/simulate', json={'record_id': rid}).json()['ok'] is False
    assert client.post(f'/api/strategy-decisions/{rid}/copy', json={'name': 'Unsupported'}).json()['ok'] is False


def test_public_comparison_uses_fixed_evidence_and_new_versions(decisions, monkeypatch):
    records, client = decisions
    from datetime import datetime, timezone
    import socket
    strategy = se.get_strategy('system.buy.default')
    idx = next(i for i, step in enumerate(strategy['steps']) if step['module_id'] == 'guard.max_discount')
    strategy['steps'][idx]['params']['max_discount'] = .70
    cfg = se.apply_strategy_to_config(copy.deepcopy(DEFAULTS), 'buy', strategy_override=strategy)
    inputs = {
        'item': {'name': 'Test Item', 'market_hash_name': 'Test Item', 'goods_id': 123, 'min_price': 74, 'daily_volume': 100, 'ratio': .74},
        'market': {'steam': {'available': True, 'smart_price': 115, 'sell_orders': [{'price': 115, 'quantity': 1}], 'source': 'steam_market', 'captured_at': time.time(), 'currency': 'CNY'},
                   'history': {'available': False, 'reason': '原检查提前停止，未获取历史行情'}},
        'account': {}, 'valuation': {'usd_to_cny': 7.2},
        'candidate': {'rank': 1, 'source_total': 100, 'name_cn': '测试商品', 'has_buff_link': True},
    }
    evaluated_at = datetime(2026, 10, 28, 12, tzinfo=timezone.utc).timestamp()
    from app.decision_buy import evaluate
    snap, modules = records.snapshot_strategy(cfg, 'buy')
    decision = evaluate(inputs, snap, evaluated_at, modules=modules)
    rid = records.capture_decision(cfg, 'buy', inputs['item'], inputs, decision, evaluated_at=evaluated_at)
    original = client.get(f'/api/strategy-decisions/{rid}').json()['record']
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('fixed preview must not use network'))
    request = {'record_id': rid, 'params': {str(idx): {'max_discount': .75}}}
    result = client.post('/api/strategies/simulate', json=request).json()
    assert result['ok'], result
    before = next(s for s in result['original']['steps'] if s['module_id'] == 'guard.max_discount')
    after = next(s for s in result['preview']['steps'] if s['module_id'] == 'guard.max_discount')
    assert before['status'] == 'reject'
    assert after['status'] == 'pass'
    assert before['actual'] == pytest.approx(.74)
    assert result['preview']['status'] == 'unavailable'
    assert result['evaluated_at'] == evaluated_at
    assert result['original'] == decision
    monkeypatch.setattr(se, 'load_app_config_validated', lambda: pytest.fail('must not read current strategy config'))
    assert client.post('/api/strategies/simulate', json=request).json() == result
    updated = copy.deepcopy(inputs)
    updated['item']['min_price'] = 60
    version = records.add_version(rid, updated, evaluated_at + 60)
    current = client.post('/api/strategies/simulate', json={**request, 'version_id': version['id']}).json()
    assert current['ok'], current
    assert current['version_id'] == version['id']
    for side in ('original', 'preview'):
        step = next(s for s in current[side]['steps'] if s['module_id'] == 'guard.max_discount')
        assert step['actual'] == pytest.approx(.60)
    assert client.get(f'/api/strategy-decisions/{rid}').json()['record'] == original


def test_repricing_is_read_only_on_public_endpoints(decisions):
    records, client = decisions
    cfg = se.apply_strategy_to_config(copy.deepcopy(DEFAULTS), 'sell')
    rid = records.capture_decision(cfg, 'reprice', {'name': 'Listed item'}, {}, {'status': 'wait', 'steps': []})
    assert client.get(f'/api/strategy-decisions/{rid}').json()['ok']
    for url, payload in [('/api/strategies/simulate', {'record_id': rid}),
                         (f'/api/strategy-decisions/{rid}/copy', {'name': 'Forbidden'})]:
        response = client.post(url, json=payload).json()
        assert response['ok'] is False
        assert '自动调价' in response['error']


def test_public_refresh_publishes_a_version_and_preserves_record(decisions, monkeypatch):
    records, client = decisions
    from app import decision_refresh as refresh
    rid = capture(records)
    original = records.get_record(rid)
    monkeypatch.setattr(refresh, '_account_guard', lambda record: None)
    # External read adapter is the only replacement; record persistence, thread,
    # public API and version publication all execute normally.
    monkeypatch.setattr(refresh, '_fetch_sources', lambda record: {
        'steam': {'available': True, 'smart_price': 115, 'sell_orders': [{'price': 115, 'quantity': 1}], 'currency': 'CNY'},
        'history': {'available': True, 'rows': [['Oct 27 2026 01: +0', 115, 10]], 'currency': 'CNY'},
        'buff': {'available': True, 'min_price': 60},
    })
    started = client.post(f'/api/strategy-decisions/{rid}/refresh').json()
    assert started['ok'], started
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        detail = client.get(f'/api/strategy-decisions/{rid}').json()
        state = detail.get('refresh') or {}
        if state.get('status') not in {'queued', 'running'}:
            break
        time.sleep(.01)
    assert state['status'] == 'succeeded', state
    assert detail['record'] == original
    assert len(detail['versions']) == 1
    assert detail['versions'][0]['id'] == state['version_id']


@pytest.mark.parametrize('params', [
    {'-1': {'max_discount': .75}}, {'999': {}}, {'0': {'enabled': False}},
])
def test_parameter_endpoint_rejects_structural_or_unknown_changes(decisions, params):
    records, client = decisions
    rid = capture(records)
    response = client.post('/api/strategies/simulate', json={'record_id': rid, 'params': params}).json()
    assert response['ok'] is False


def test_versions_remain_available_for_the_lifetime_of_the_record(decisions):
    records, client = decisions
    rid = capture(records)
    original = records.get_record(rid)
    versions = [records.add_version(rid, {'sequence': i}, time.time()) for i in range(21)]
    detail = client.get(f'/api/strategy-decisions/{rid}').json()
    assert detail['record'] == original
    assert len(detail['versions']) == 21
    assert versions[0]['id'] in {v['id'] for v in detail['versions']}
    assert detail['versions'][-1]['id'] == versions[-1]['id']


def test_api_rejects_invalid_values_and_changed_module_copy(decisions, monkeypatch):
    records, client = decisions
    rid = capture(records)
    record = records.get_record(rid)
    idx = next(i for i, step in enumerate(record['strategy']['steps']) if step['module_id'] == 'guard.max_discount')
    for value in (None, '', True, -1, 'bad'):
        response = client.post(f'/api/strategy-decisions/{rid}/copy', json={'name': 'Invalid', 'params': {str(idx): {'max_discount': value}}}).json()
        assert response['ok'] is False, (value, response)
    current = se._module_by_id()
    current['guard.max_discount']['description'] = 'changed implementation metadata'
    monkeypatch.setattr(se, '_module_by_id', lambda: current)
    result = client.post(f'/api/strategy-decisions/{rid}/copy', json={'name': 'Old version'}).json()
    assert result['ok'] is False
    assert '版本' in result['error']


def test_capture_binds_original_account_and_snapshots_disabled_steps(decisions, monkeypatch):
    records, client = decisions
    cfg = se.apply_strategy_to_config(copy.deepcopy(DEFAULTS), 'buy')
    cfg['_decision_account_id'] = 'original-account'
    cfg['_strategy_runtime']['buy']['strategy']['steps'][0]['enabled'] = False
    rid = records.capture_decision(cfg, 'buy', {'name': 'Bound'}, {}, {'status': 'unavailable', 'steps': []})
    cfg['_strategy_runtime']['buy']['strategy']['steps'][0]['enabled'] = True
    record = client.get(f'/api/strategy-decisions/{rid}').json()['record']
    assert record['account_id'] == 'original-account'
    assert record['strategy']['steps'][0]['enabled'] is False


def test_real_application_protects_decision_endpoints(decisions, monkeypatch):
    from app.api import app
    monkeypatch.setenv('AETHERSWAP_AUTH_PASS', 'preview-test-password')
    # No lifespan context: never start workers in this offline API test.
    client = TestClient(app)
    for method, path in [('get', '/api/strategy-decisions'),
                         ('get', '/api/strategy-decisions/missing'),
                         ('post', '/api/strategy-decisions/missing/refresh'),
                         ('post', '/api/strategy-decisions/missing/copy'),
                         ('post', '/api/strategies/simulate')]:
        assert getattr(client, method)(path).status_code == 401
    assert client.get('/api/strategy-decisions', auth=('aetherswap', 'preview-test-password')).json()['ok']
    client.close()


def test_changed_rule_implementation_makes_old_record_read_only(decisions, monkeypatch):
    records, client = decisions
    rid = capture(records)
    original = records.get_record(rid)
    from app import decision_versions
    monkeypatch.setattr(decision_versions, 'rules_version', lambda kind: 'different-implementation')
    response = client.post('/api/strategies/simulate', json={'record_id': rid}).json()
    assert response['ok'] is False
    assert '版本' in response['error']
    assert client.post(f'/api/strategy-decisions/{rid}/copy', json={'name': 'Old code'}).json()['ok'] is False
    assert records.get_record(rid)['result'] == original['result']


def test_public_sell_preview_changes_price_without_listing(decisions, monkeypatch):
    records, client = decisions
    from app.decision_sell import evaluate
    cfg = se.apply_strategy_to_config(copy.deepcopy(DEFAULTS), 'sell', strategy_override=se.get_strategy('system.sell.immediate'))
    strategy, modules = records.snapshot_strategy(cfg, 'sell')
    inputs = {
        'item': {'name': 'Owned item', 'market_hash_name': 'Owned item', 'assetid': 'asset'},
        'inventory': {'can_sell': True, 'appid': 730},
        'ownership': {'eligible': True, 'purchase_id': 1, 'purchase_price_cny': 10, 'market_price_cny_at_purchase': 20},
        'listing': {'same_name_count': 0, 'round_same_name_count': 0, 'source': 'steam'},
        'market': {'available': True, 'currency': 'CNY', 'sell_orders': [{'price': 20, 'quantity': 100}]},
        'trend': {'available': False, 'points': []}, 'currency': {'code': 'CNY', 'cny_per_account': 1},
    }
    evaluated_at = time.time()
    original = evaluate(inputs, strategy, evaluated_at, modules=modules)
    rid = records.capture_decision(cfg, 'sell', inputs['item'], inputs, original, evaluated_at=evaluated_at)
    import steam.market
    monkeypatch.setattr(steam.market, 'list_item', lambda *a, **k: pytest.fail('preview must not list'))
    idx = next(i for i, step in enumerate(strategy['steps']) if step['module_id'] == 'pricing.price_offset')
    response = client.post('/api/strategies/simulate', json={'record_id': rid, 'params': {str(idx): {'sell_price_offset': .5}}}).json()
    assert response['ok'], response
    assert response['original'] == original
    assert response['original']['status'] == 'pass'
    before = response['original']['steps'][idx]['output']['list_price']
    after = response['preview']['steps'][idx]['output']['list_price']
    assert before == 19.99
    assert after == 20.49
    assert records.get_record(rid)['result'] == original


def test_declarative_sell_missing_input_cannot_be_configured_to_pass(decisions):
    from app.decision_sell import evaluate
    from tests.test_decision_sell import _inputs
    module = se.import_user_module({
        'id': 'custom.sell.missing', 'name': 'Missing input guard',
        'module_kind': 'declarative', 'strategy_types': ['sell'], 'stage': 'sell.listing_guard',
        'conditions': [{'left': 'item.unknown_business_field', 'op': 'gt', 'value': 1}],
        'fail_status': 'pass',
    })
    cfg = se.apply_strategy_to_config(copy.deepcopy(DEFAULTS), 'sell', strategy_override=se.get_strategy('system.sell.immediate'))
    strategy, modules = decisions[0].snapshot_strategy(cfg, 'sell')
    strategy['steps'].insert(-1, {'module_id': module['id'], 'enabled': True, 'params': {}})
    modules[module['id']] = module
    result = evaluate(_inputs(), strategy, time.time(), modules=modules)
    assert result['status'] == 'unavailable', result
    assert result['steps'][-1]['status'] == 'not_run'


def test_sell_explanation_includes_buyer_price_and_estimated_net_proceeds(decisions):
    from app.decision_sell import evaluate
    from tests.test_decision_sell import _inputs, _strategy, _modules
    inputs = _inputs()
    inputs['market']['sell_orders'] = [{'price': 1.18, 'quantity': 100}]
    result = evaluate(inputs, _strategy(), time.time(), modules=_modules())
    pricing = next(step for step in result['steps'] if 'estimated_proceeds' in (step.get('output') or {}))
    # Buyer pays 1.17 CNY: 1.00 to seller, 0.07 minimum Steam fee, 0.10 game fee.
    assert pricing['output']['display_price'] == 1.17
    assert pricing['output']['estimated_proceeds'] == 1.00
    assert pricing['output']['currency'] == 'CNY'
