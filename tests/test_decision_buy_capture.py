"""Real candidate workflow through durable public decision APIs."""
import copy
from types import SimpleNamespace

from tests.test_decision_records import decisions
from app import pipeline_steps as steps, strategy_engine as se
from app.config_schema import DEFAULTS
from app.services.analysis_client import StabilityAnalyzer


def candidate_config():
    cfg = copy.deepcopy(DEFAULTS)
    cfg['pipeline']['max_discount'] = .70
    cfg['pipeline']['sell_pressure_threshold'] = 10
    cfg['stability']['request_interval_seconds'] = 0
    cfg['stability']['request_failure_delay_seconds'] = 0
    cfg['_decision_accumulated_spend'] = 0
    return se.apply_strategy_to_config(cfg, 'buy')


def candidate(cfg):
    return steps.filter_iflow_rows([SimpleNamespace(
        name='Test Item', name_cn='测试商品', min_price=74, sell_ratio=.74,
        platform='https://buff.163.com/goods/123', volume=100,
        steam_link='https://steamcommunity.com/market/listings/730/Test%20Item',
    )], cfg)[0]


def test_real_early_cost_rejection_is_recorded_and_can_be_previewed(decisions, monkeypatch):
    records, client = decisions
    cfg = candidate_config()
    item = candidate(cfg)
    monkeypatch.setattr(steps, 'get_purchases', lambda: [])
    monkeypatch.setattr(steps, '_fetch_steam_sell_data', lambda *a, **kw: ({'smart_price': 115, 'sell_orders': [(115, 10)]}, None))
    class NoHistory:
        def fetch_history(self, *a, **kw):
            raise AssertionError('early rejection must not fetch history')
    chosen, failed = steps.pick_stable_item([item], cfg, NoHistory(), StabilityAnalyzer(), lambda: False)
    assert chosen is None and 123 in failed
    listing = client.get('/api/strategy-decisions?kind=buy').json()
    assert listing['total'] == 1
    record = records.get_record(listing['items'][0]['id'])
    assert record['result']['status'] == 'reject'
    idx = next(i for i, step in enumerate(record['strategy']['steps']) if step['module_id'] == 'guard.max_discount')
    compared = client.post('/api/strategies/simulate', json={'record_id': record['id'], 'params': {str(idx): {'max_discount': .75}}}).json()
    assert compared['ok'], compared
    assert compared['original'] == record['result']
    assert compared['preview']['steps'][idx]['status'] == 'pass'
    assert compared['preview']['status'] == 'unavailable'
    assert 'https://' not in str(record)


def test_real_buy_success_keeps_history_budget_and_original_account(decisions, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import accounts
    records, client = decisions
    cfg = candidate_config()
    strategy = se.get_strategy('system.buy.default')
    next(s for s in strategy['steps'] if s['module_id'] == 'guard.max_discount')['params']['max_discount'] = .9
    cfg = se.apply_strategy_to_config(cfg, 'buy', strategy_override=strategy)
    item = candidate(cfg)
    now = datetime.now(timezone.utc)
    rows = [[(now - timedelta(hours=4*i)).strftime('%b %d %Y %H: +0'), 115, 100] for i in range(210)]
    class History:
        def fetch_history(self, *args, **kwargs):
            return {'history': rows, 'currency': 'CNY'}
    class Buff:
        def get_sell_orders(self, *args, **kwargs):
            return [{'id': 'order', 'price': '74.00', 'user_id': 'not-recorded'}]
    def market(*args, **kwargs):
        monkeypatch.setattr(accounts, 'get_current_account', lambda: {'id': 'later-account'})
        result = {'smart_price': 100, 'sell_orders': [(100, 10)]}
        return (result, None) if kwargs.get('return_error') else result
    monkeypatch.setattr(steps, '_fetch_steam_sell_data', market)
    monkeypatch.setattr(steps, 'SteamClient', History)
    monkeypatch.setattr(steps, 'get_purchases', lambda: [])
    logs = []
    chosen, _ = steps.pick_stable_item([item], cfg, History(), StabilityAnalyzer(), lambda: False, buff_client=Buff(), log_fn=lambda message, level: logs.append(message))
    assert chosen is item, logs
    record = records.get_record(records.list_records()['items'][0]['id'])
    assert record['account_id'] == 'account-a'
    assert record['inputs']['market']['history']['rows']
    assert record['inputs']['account']['accumulated_spend'] == 0
    assert record['result']['status'] == 'pass', record['result']
    compared = client.post('/api/strategies/simulate', json={'record_id': record['id']}).json()
    assert compared['ok'], compared
    assert compared['original'] == record['result']
    assert 'not-recorded' not in str(record)


def test_recording_failure_does_not_change_real_cost_rejection(decisions, monkeypatch):
    from app import database
    _, _client = decisions
    cfg = candidate_config()
    item = candidate(cfg)
    monkeypatch.setattr(steps, 'get_purchases', lambda: [])
    monkeypatch.setattr(steps, '_fetch_steam_sell_data', lambda *a, **kw: ({'smart_price': 115, 'sell_orders': [(115, 10)]}, None))
    monkeypatch.setattr(database, 'get_engine', lambda: (_ for _ in ()).throw(RuntimeError('db unavailable')))
    chosen, failed = steps.pick_stable_item([item], cfg, object(), StabilityAnalyzer(), lambda: False)
    assert chosen is None and failed == {123}


def test_buff_price_jump_uses_recorded_tolerance_in_real_and_preview(decisions, monkeypatch):
    records, client = decisions
    cfg = candidate_config()
    strategy = se.get_strategy('system.buy.default')
    for step in strategy['steps']:
        if step['module_id'] in {'guard.history_data_window', 'guard.volatility_cv', 'guard.trend_quality', 'guard.price_position'}:
            step['enabled'] = False
        if step['module_id'] == 'guard.max_discount':
            step['params']['max_discount'] = .9
    cfg = se.apply_strategy_to_config(cfg, 'buy', strategy_override=strategy)
    item = candidate(cfg)
    monkeypatch.setattr(steps, 'get_purchases', lambda: [])
    monkeypatch.setattr(steps, '_fetch_steam_sell_data', lambda *a, **kw: ({'smart_price': 115, 'sell_orders': [(115, 10)]}, None))
    class History:
        def fetch_history(self, *a, **kw):
            return None
    class Buff:
        def get_sell_orders(self, *a, **kw):
            return [{'price': '76'}]
    monkeypatch.setattr(steps, 'SteamClient', History)
    chosen, _ = steps.pick_stable_item([item], cfg, History(), StabilityAnalyzer(), lambda: False, buff_client=Buff())
    assert chosen is None
    record = records.get_record(records.list_records()['items'][0]['id'])
    assert record['result']['status'] == 'reject'
    assert '容忍' in str(record['result'])
    compared = client.post('/api/strategies/simulate', json={'record_id': record['id']}).json()
    assert compared['original'] == record['result']


def test_failed_adjustment_does_not_borrow_later_history_for_old_decision():
    from app.decision_buy import evaluate
    strategy = {'steps': [
        {'module_id': 'buy.steam_sell_depth', 'params': {}},
        {'module_id': 'guard.max_discount', 'params': {'max_discount': .8}},
    ]}
    inputs = {
        'item': {'min_price': 60},
        'market': {
            'steam': {'available': True, 'smart_price': 100, 'sell_orders': [{'price': 100, 'quantity': 10}]},
            'history': {'currency': 'CNY', 'available': True, 'adjustment_rows': [],
                        'rows': [['Oct 28 2026 01: +0', p, 1] for p in (10, 20, 30, 40)]},
        }, 'valuation': {'usd_to_cny': 7.2},
    }
    result = evaluate(inputs, strategy, 1793188800.0)
    assert result['status'] == 'pass'
    assert result['steps'][1]['actual'] == .69


def test_declarative_guard_reads_recorded_config_and_its_own_parameters(decisions):
    from app.decision_buy import evaluate
    module = se.import_user_module({
        'id': 'custom.buy.config_guard', 'name': '参数检查', 'module_kind': 'declarative',
        'strategy_types': ['buy'], 'stage': 'buy.candidate_guard',
        'params_schema': {'minimum': {'type': 'number', 'default': 10}},
        'conditions': [{'left': 'config.pipeline.iflow_top_n', 'op': 'gte', 'right_path': 'params.minimum'}],
    })
    strategy = {'strategy_type': 'buy', 'steps': [
        {'module_id': 'buy.steamdt_top_n', 'enabled': True, 'params': {'iflow_top_n': 20}},
        {'module_id': module['id'], 'enabled': True, 'params': {'minimum': 10}},
    ]}
    inputs = {'candidate': {'rank': 1, 'source_total': 100}, 'item': {}, 'policy': {}}
    result = evaluate(inputs, strategy, 1793188800, modules={module['id']: module})
    assert result['status'] == 'pass', result
    strategy['steps'][1]['params']['minimum'] = 30
    result = evaluate(inputs, strategy, 1793188800, modules={module['id']: module})
    assert result['status'] == 'reject'


def test_real_declarative_pressure_guard_agrees_with_saved_evaluation(decisions, monkeypatch):
    records, _ = decisions
    module = se.import_user_module({
        'id': 'custom.buy.pressure_limit', 'name': 'Shared pressure', 'module_kind': 'declarative',
        'strategy_types': ['buy'], 'stage': 'buy.candidate_guard',
        'uses_modules': ['buy.steam_sell_depth'],
        'conditions': [{'left': 'outputs.buy.steam_sell_depth.sell_pressure', 'op': 'lte', 'value': .05}],
        'fail_status': 'reject',
    })
    strategy = se.get_strategy('system.buy.default')
    for step in strategy['steps']:
        if step['module_id'] in {'guard.history_data_window', 'guard.volatility_cv', 'guard.trend_quality', 'guard.price_position'}:
            step['enabled'] = False
        if step['module_id'] == 'guard.max_discount':
            step['params']['max_discount'] = .9
    strategy['steps'].insert(-1, {'module_id': module['id'], 'enabled': True, 'params': {}})
    cfg = se.apply_strategy_to_config(candidate_config(), 'buy', strategy_override=strategy)
    item = candidate(cfg)
    monkeypatch.setattr(steps, 'get_purchases', lambda: [])
    monkeypatch.setattr(steps, '_fetch_steam_sell_data', lambda *a, **kw: ({'smart_price': 115, 'sell_orders': [(115, 10)]}, None))
    class History:
        def fetch_history(self, *a, **kw): return None
    monkeypatch.setattr(steps, 'SteamClient', History)
    chosen, _ = steps.pick_stable_item([item], cfg, History(), StabilityAnalyzer(), lambda: False)
    assert chosen is None
    record = records.get_record(records.list_records()['items'][0]['id'])
    assert record['result']['status'] == 'reject'
    guard = next(step for step in record['result']['steps'] if step['module_id'] == module['id'])
    assert guard['output']['conditions'][0]['left_value'] == .1
