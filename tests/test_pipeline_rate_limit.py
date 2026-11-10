from types import SimpleNamespace

import pytest

from app import pipeline
from app.pipeline_context import PipelineContext
from buff import BuffRateLimited
from tests.test_buff_purchase_flow_safety import _FakeState, _item


@pytest.fixture
def runtime(monkeypatch):
    state = _FakeState()
    monkeypatch.setattr(pipeline, "get_state", lambda: state)
    monkeypatch.setattr(pipeline, "get_buff_credentials", lambda: {"cookies": "test"})
    monkeypatch.setattr(pipeline, "get_proxy_manager", lambda: SimpleNamespace(is_proxy_enabled=lambda: False))
    monkeypatch.setattr(pipeline, "get_network_checker", lambda: SimpleNamespace(report_success=lambda: None))
    monkeypatch.setattr(pipeline, "SteamClient", lambda: None)
    monkeypatch.setattr(pipeline, "StabilityAnalyzer", lambda **kw: None)
    monkeypatch.setattr(pipeline, "create_buff_client_from_config", lambda *a: None)
    monkeypatch.setattr(pipeline, "apply_strategy_to_config", lambda cfg, kind: cfg)
    monkeypatch.setattr(pipeline, "BuyPolicyMonitor", lambda cfg: SimpleNamespace(
        is_current=lambda: True, refresh=lambda cfg: (cfg, False)))
    monkeypatch.setattr(pipeline, "get_unresolved_checkout", lambda: None)
    monkeypatch.setattr(pipeline, "_active_buy_policy", None)
    monkeypatch.setattr(pipeline, "jittered_sleep", lambda *a: None)
    monkeypatch.setattr(pipeline, "_fetch_and_filter_deals", lambda *a: ([_item([])], False))
    return state


@pytest.mark.parametrize("limited_at", ["scan", "checkout"])
def test_limit_preserves_completed_spend_before_rescanning(monkeypatch, runtime, limited_at):
    item = _item([])
    scans = 0
    checkouts = 0

    def pick(*args, **kwargs):
        nonlocal scans
        scans += 1
        if scans == 2 and limited_at == "scan":
            raise BuffRateLimited(2.1)
        return item, set()

    def checkout(*args, **kwargs):
        nonlocal checkouts
        checkouts += 1
        if checkouts == 2:
            raise BuffRateLimited(2.1)
        return 10

    monkeypatch.setattr(pipeline, "pick_stable_item", pick)
    monkeypatch.setattr(pipeline, "lock_and_confirm_payment", checkout)
    acc, bought, outcome = pipeline._process_deals_for_target(
        PipelineContext(runtime, "test"), [item], {}, 100, 20, 2,
        None, None, None, set(), set(), set())
    assert (acc, bought) == (30, 3)
    assert isinstance(outcome, BuffRateLimited)


def test_pipeline_waits_then_rescans_with_preserved_budget(monkeypatch, runtime):
    from app import pipeline_context
    calls = []
    waits = []

    def process(*args, **kwargs):
        calls.append((args[4], args[5]))
        if len(calls) == 1:
            return 30, 3, BuffRateLimited(2.1)
        return 100, 10, False

    def sleep(seconds):
        assert runtime.status == ("running", "BUFF_RATE_LIMIT_WAIT")
        waits.append(seconds)

    monkeypatch.setattr(pipeline, "_process_deals_for_target", process)
    monkeypatch.setattr(pipeline_context.time, "sleep", sleep)
    pipeline._run_pipeline({"pipeline": {"target_balance": 100}})
    assert calls == [(0, 0), (30, 3)]
    assert sum(waits) >= 2.1
    assert runtime.status == ("idle", "")


def test_user_can_stop_during_rate_limit_wait(monkeypatch, runtime):
    from app import pipeline_context
    stopped = False
    calls = []

    def process(*args, **kwargs):
        calls.append(1)
        return 0, 0, BuffRateLimited(300)

    def sleep(seconds):
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(runtime, "is_stop_requested", lambda: stopped)
    monkeypatch.setattr(pipeline, "_process_deals_for_target", process)
    monkeypatch.setattr(pipeline_context.time, "sleep", sleep)
    pipeline._run_pipeline({})
    assert calls == [1]
    assert runtime.status == ("stopped", "已停止")


def test_rate_limit_with_unresolved_order_does_not_resume(monkeypatch, runtime):
    monkeypatch.setattr(pipeline, "get_unresolved_checkout", lambda: {"stage": "created_pending"})

    def process(*args, **kwargs):
        raise BuffRateLimited(30)

    monkeypatch.setattr(pipeline, "_process_deals_for_target", process)
    pipeline._run_pipeline({})
    assert runtime.status == ("error", "BUFF_RATE_LIMITED")


def test_policy_change_cannot_shorten_rate_limit_wait(monkeypatch, runtime):
    from app import pipeline_context
    ctx = PipelineContext(runtime, "test")
    ctx.is_policy_current = lambda: False
    sleeps = []
    monkeypatch.setattr(pipeline_context.time, "sleep", sleeps.append)
    assert ctx.wait_retry(3, step="BUFF_RATE_LIMIT_WAIT", interrupt_on_policy_change=False) is False
    assert sleeps == [1, 1, 1]


def test_closed_time_window_after_cooldown_prevents_new_scan(monkeypatch, runtime):
    from app import pipeline_context
    window_open = True
    scans = []

    def process(*args, **kwargs):
        scans.append(1)
        return 0, 0, BuffRateLimited(1)

    def sleep(seconds):
        nonlocal window_open
        window_open = False

    original_wait = PipelineContext.wait_retry

    def wait(ctx, seconds, **kwargs):
        if not kwargs:
            assert runtime.status == ("running", "TIME_LIMIT_WAIT")
            runtime.set_status("stopped", "已停止")
            return True
        return original_wait(ctx, seconds, **kwargs)

    monkeypatch.setattr(pipeline, "_is_in_time_window", lambda *a: window_open)
    monkeypatch.setattr(pipeline, "_process_deals_for_target", process)
    monkeypatch.setattr(pipeline_context.time, "sleep", sleep)
    monkeypatch.setattr(PipelineContext, "wait_retry", wait)
    pipeline._run_pipeline({"pipeline": {"start_time_limit_enabled": True}})
    assert scans == [1]
    assert runtime.status == ("stopped", "已停止")
