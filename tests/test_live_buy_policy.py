import copy
from unittest.mock import Mock

import pytest

from app import pipeline, pipeline_steps as steps, strategy_engine as se
from app.config_schema import DEFAULTS
from app.services import buy_policy
from app.services.buy_policy import BuyPolicyMonitor, POLICY_CHANGED
from test_buff_purchase_flow_safety import _config, _item, _checkout_args, _FakeState, _FakeContext


@pytest.fixture
def saved_policy(monkeypatch, tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg['pipeline']['max_discount'] = 0.7
    monkeypatch.setattr(se, 'STRATEGY_STORE_PATH', tmp_path / 'strategies.json')
    monkeypatch.setattr(se, 'USER_MODULE_STORE_PATH', tmp_path / 'modules.json')
    monkeypatch.setattr(buy_policy, 'load_app_config_validated', lambda: copy.deepcopy(cfg))
    monkeypatch.setattr(se, 'load_app_config_validated', lambda: copy.deepcopy(cfg))
    monkeypatch.setattr(se, 'save_app_config_validated', lambda value: (cfg.clear(), cfg.update(copy.deepcopy(value))))
    from app.services import buff_checkout_guard
    monkeypatch.setattr(buff_checkout_guard, '_GUARD_PATH', tmp_path / 'checkout.json')
    return cfg


def test_saved_limit_changes_live_snapshot_in_both_directions(saved_policy):
    cfg = se.apply_strategy_to_config(saved_policy, 'buy')
    monitor = BuyPolicyMonitor(cfg)
    for limit in (0.75, 0.65):
        saved_policy['pipeline']['max_discount'] = limit
        assert not monitor.is_current()
        assert monitor.status()['pending']
        cfg, changed = monitor.refresh(cfg)
        assert changed
        assert cfg['pipeline']['max_discount'] == limit
        assert monitor.is_current()
        assert monitor.status()['max_discount'] == limit
    assert not monitor.refresh(cfg)[1]


def test_inactive_custom_save_does_not_restart_active_scan(saved_policy):
    monitor = BuyPolicyMonitor(se.apply_strategy_to_config(saved_policy, 'buy'))
    draft = copy.deepcopy(se.get_strategy('system.buy.default'))
    draft['id'] = 'custom.buy.new'
    saved = se.save_strategy(draft)
    assert monitor.is_current()
    se.activate_strategy(saved['id'], risk_confirmed=True)
    assert not monitor.is_current()
    cfg, _ = monitor.refresh(se.apply_strategy_to_config(saved_policy, 'buy'))
    assert cfg['_strategy_runtime']['buy']['active_strategy_id'] == saved['id']


def test_active_custom_override_wins_over_shared_config(saved_policy):
    draft = copy.deepcopy(se.get_strategy('system.buy.default'))
    draft['id'] = 'custom.buy.live'
    next(step for step in draft['steps'] if step['module_id'] == 'guard.max_discount')['params'] = {'max_discount': 0.75}
    se.save_strategy(draft)
    se.activate_strategy(draft['id'], risk_confirmed=True)
    cfg = se.apply_strategy_to_config(saved_policy, 'buy')
    monitor = BuyPolicyMonitor(cfg)
    saved_policy['pipeline']['max_discount'] = 0.9
    assert monitor.is_current()
    next(step for step in draft['steps'] if step['module_id'] == 'guard.max_discount')['params'] = {'max_discount': 0.66}
    se.save_strategy(draft)
    assert not monitor.is_current()
    assert monitor.refresh(cfg)[0]['pipeline']['max_discount'] == 0.66


def test_unrelated_sell_and_notify_changes_do_not_interrupt_buy(saved_policy):
    monitor = BuyPolicyMonitor(se.apply_strategy_to_config(saved_policy, 'buy'))
    saved_policy['pipeline']['sell_price_offset'] = 0.1
    saved_policy['notify']['holdings_report_interval_hours'] = 2
    assert monitor.is_current()


def test_retry_wait_wakes_for_new_policy(monkeypatch):
    from app.pipeline_context import PipelineContext
    import app.pipeline_context as context
    ctx = PipelineContext(_FakeState(), 'test')
    checks = iter([True, False])
    ctx.is_policy_current = lambda: next(checks)
    sleeps = []
    monkeypatch.setattr(context.time, 'sleep', sleeps.append)
    assert ctx.wait_retry(900) is False
    assert sleeps == [1]


def test_candidate_scan_stops_before_next_request_when_policy_changes():
    chosen, rejected = steps.pick_stable_item(
        [{'goods_id': 1}], {}, Mock(), Mock(), lambda: False,
        is_policy_current=lambda: False,
    )
    assert chosen is None
    assert rejected == set()


def test_policy_change_after_candidate_analysis_prevents_checkout(monkeypatch):
    ctx = _FakeContext(_FakeState())
    checks = iter([True, False])
    ctx.is_policy_current = lambda: next(checks)
    checkout = Mock(side_effect=AssertionError('must not purchase with stale policy'))
    monkeypatch.setattr(pipeline, 'pick_stable_item', lambda *a, **kw: ({'name': 'Test'}, set()))
    monkeypatch.setattr(pipeline, 'lock_and_confirm_payment', checkout)
    result = pipeline._process_deals_for_target(ctx, [], {}, 100, 25, 2, None, None, None, set(), set(), set())
    assert result == (25, 2, POLICY_CHANGED)
    checkout.assert_not_called()


def test_change_during_completed_purchase_preserves_spend(monkeypatch):
    ctx = _FakeContext(_FakeState())
    current = [True]
    ctx.is_policy_current = lambda: current[0]
    monkeypatch.setattr(pipeline, 'pick_stable_item', lambda *a, **kw: ({'name': 'Test', 'goods_id': 1}, set()))
    def paid(*args, **kwargs):
        current[0] = False
        return 10
    monkeypatch.setattr(pipeline, 'lock_and_confirm_payment', paid)
    monkeypatch.setattr(pipeline, 'jittered_sleep', lambda *args: None)
    result = pipeline._process_deals_for_target(ctx, [], {}, 100, 25, 2, None, None, None, set(), set(), set())
    assert result == (35, 3, POLICY_CHANGED)


def test_change_during_single_preflight_sends_no_order(saved_policy):
    current = [True]
    class Client:
        def prepare_single_buy(self, *args):
            current[0] = False
            return {'success': True, 'preview': {}}
        def lock_and_get_pay_url(self, *args, **kwargs):
            pytest.fail('stale policy must not create an order')
    kwargs, _, purchases = _checkout_args()
    result = steps.lock_and_confirm_payment(Client(), _item([{'id': 'test-sell', 'price': '10'}]), _config(),
        is_policy_current=lambda: current[0], **kwargs)
    from app.services.buff_checkout_guard import get_unresolved_checkout
    assert result is POLICY_CHANGED
    assert not purchases
    assert get_unresolved_checkout() is None


def test_single_low_level_rechecks_policy_after_preview(monkeypatch):
    from buff.buyer import BuffBuyer
    buyer = object.__new__(BuffBuyer)
    current = [True]
    def preview(*args):
        current[0] = False
        return {'code': 'OK', 'data': {}}
    monkeypatch.setattr(buyer, 'preview_buy', preview)
    monkeypatch.setattr(buyer, '_preview_payment_error', lambda data: None)
    post = Mock(side_effect=AssertionError('POST must not run'))
    monkeypatch.setattr(buyer, '_make_request', post)
    result = buyer.lock_and_get_pay_url('csgo', 1, 'test-sell', '10', can_create=lambda: current[0])
    assert result['created'] is False
    post.assert_not_called()


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -0.1, 1.01, True])
def test_invalid_cost_limit_is_rejected(value):
    errors = se._validate_step_params('guard.max_discount', se.BUILTIN_MODULES['guard.max_discount'], {'max_discount': value})
    assert errors


def test_batch_preflight_change_cancels_without_single_fallback(saved_policy):
    current = [True]
    class Client:
        _pay_method = 'wechat'
        def try_batch_buy(self, *args, on_created=None, can_create=None):
            current[0] = False
            assert can_create is not None and not can_create()
            return {'success': False, 'created': False, 'code': 'BATCH_CANCELLED'}
        def lock_and_get_pay_url(self, *args, **kwargs):
            pytest.fail('must not fallback after a policy change')
    kwargs, _, purchases = _checkout_args()
    result = steps.lock_and_confirm_payment(Client(), _item([{'id': 'test-a', 'price': '10'}, {'id': 'test-b', 'price': '10'}]), _config('wechat'),
        is_policy_current=lambda: current[0], **kwargs)
    from app.services.buff_checkout_guard import get_unresolved_checkout
    assert result is POLICY_CHANGED
    assert purchases == []
    assert get_unresolved_checkout() is None


def test_unknown_order_outcome_still_blocks_when_policy_changes(saved_policy):
    from buff import BuffWriteResultUnknown
    from app.services.buff_checkout_guard import get_unresolved_checkout
    current = [True]
    class Client:
        def lock_and_get_pay_url(self, *args, **kwargs):
            current[0] = False
            raise BuffWriteResultUnknown('ambiguous result', method='POST')
    kwargs, _, _ = _checkout_args()
    with pytest.raises(steps.PurchaseWriteResultUnknown):
        steps.lock_and_confirm_payment(Client(), _item([{'id': 'test', 'price': '10'}]), _config(),
            is_policy_current=lambda: current[0], **kwargs)
    assert get_unresolved_checkout() is not None


def test_running_pipeline_reloads_saved_limit_without_waiting_or_restart(saved_policy, monkeypatch):
    from app import pipeline_context
    state = _FakeState()
    stop = [False]
    state.is_stop_requested = lambda: stop[0]
    monkeypatch.setattr(pipeline, 'get_state', lambda: state)
    monkeypatch.setattr(pipeline, 'get_buff_credentials', lambda: {'cookies': 'test-only'})
    monkeypatch.setattr(pipeline, 'create_buff_client_from_config', lambda *args: object())
    monkeypatch.setattr(pipeline, 'SteamClient', lambda: object())
    monkeypatch.setattr(pipeline, 'StabilityAnalyzer', lambda **kwargs: object())
    proxy = Mock()
    proxy.is_proxy_enabled.return_value = False
    monkeypatch.setattr(pipeline, 'get_proxy_manager', lambda: proxy)
    monkeypatch.setattr(pipeline, 'get_network_checker', Mock())
    sleeps = Mock(side_effect=AssertionError('new strategy should wake the retry wait'))
    monkeypatch.setattr(pipeline_context.time, 'sleep', sleeps)
    limits = []
    def scan(ctx, cfg, delay):
        limits.append(cfg['pipeline']['max_discount'])
        if len(limits) == 1:
            saved_policy['pipeline']['max_discount'] = 0.75
        else:
            stop[0] = True
        return [], False
    monkeypatch.setattr(pipeline, '_fetch_and_filter_deals', scan)
    pipeline._run_pipeline(copy.deepcopy(saved_policy))
    assert limits == [0.7, 0.75]
    assert any('买入策略已实时更新' in entry[0] for entry in state.logs)
    sleeps.assert_not_called()
