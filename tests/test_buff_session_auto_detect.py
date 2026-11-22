"""Tests for automatic BUFF session detection, notifications, and pipeline halt."""
from __future__ import annotations

import json
import threading
import time
import warnings
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest


@pytest.fixture
def isolated_buff_env(tmp_path, monkeypatch):
    import buff
    import config
    from app import accounts, pipeline, state
    from app.services import buff_auth, buff_checkout_guard, buff_session

    monkeypatch.setattr(config, "_CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(config, "_cache", {})
    monkeypatch.setattr(buff_session, "STATUS_PATH", tmp_path / "buff_session_status.json")
    monkeypatch.setattr(buff_checkout_guard, "_GUARD_PATH", tmp_path / "checkout.json")

    test_state = state.State()
    monkeypatch.setattr(state, "_instance", test_state)

    account = {"id": "acc-1", "steam_id": "76561198000000001"}
    monkeypatch.setattr(accounts, "get_current_account", lambda: dict(account))

    policy = buff.BuffRequestPolicy(min_interval=0, state_path=None, persist=False)
    monkeypatch.setattr(buff, "get_global_policy", lambda: policy)

    monkeypatch.setattr(pipeline, "is_shutdown_pending", lambda: False)
    monkeypatch.setattr(pipeline, "is_pipeline_running", lambda: False)

    calls = []
    response = {
        "status": 200,
        "body": {"code": "OK", "data": {"user_info": {"steamid": account["steam_id"]}}},
        "headers": {},
    }

    from utils import http_client

    def fake_request(self, method, url, **kwargs):
        calls.append((method, str(url)))
        if "handler" in response:
            return response["handler"](method, str(url))
        return httpx.Response(
            response["status"],
            json=response["body"],
            headers=response["headers"],
            request=httpx.Request(method, str(url)),
        )

    monkeypatch.setattr(http_client.Client, "request", fake_request)
    config.update_buff_credentials("session=s1; csrf_token=c1", user_agent="Chrome/test", source="extension")
    return SimpleNamespace(
        config=config,
        state=test_state,
        account=account,
        policy=policy,
        calls=calls,
        response=response,
        buff_session=buff_session,
        buff_auth=buff_auth,
    )


def test_should_auto_check_session_gates(isolated_buff_env, monkeypatch):
    from buff.request_policy import account_fingerprint

    env = isolated_buff_env
    bs = env.buff_session

    # Initially configured and unchecked -> due
    ok, reason, _ = bs.should_auto_check_session(3600)
    assert (ok, reason) == (True, "due")

    # Unconfigured
    env.config.update_buff_credentials("")
    assert bs.should_auto_check_session(3600)[:2] == (False, "unconfigured")
    env.config.update_buff_credentials("session=s1; csrf_token=c1", user_agent="Chrome/test", source="extension")

    # Missing steam_id
    saved_sid = env.account["steam_id"]
    env.account["steam_id"] = ""
    assert bs.should_auto_check_session(3600)[:2] == (False, "missing_steam_id")
    env.account["steam_id"] = saved_sid

    # Checkout / shutdown busy gates
    with monkeypatch.context() as mp:
        mp.setattr(env.state, "get_pending_payment", lambda: {"order_id": "o1"})
        assert bs.should_auto_check_session(3600)[:2] == (False, "checkout_busy")

    env.state.set_status("running", "CHECKOUT_PENDING")
    assert bs.should_auto_check_session(3600)[:2] == (False, "checkout_busy")
    env.state.set_status("idle", "")

    # In-memory expired / verification flags
    env.state.set_buff_auth_expired(True)
    assert bs.should_auto_check_session(3600)[:2] == (False, "already_expired")
    env.state.set_buff_auth_expired(False)

    env.state.set_buff_verification_required(True, "captcha")
    assert bs.should_auto_check_session(3600)[:2] == (False, "already_verification")
    env.state.set_buff_verification_required(False)

    # Rate-limit and risk circuits
    key = account_fingerprint({}, account_id="default")
    env.policy.trip_rate_limit(key, 120, "429")
    ok, reason, wait_hint = bs.should_auto_check_session(3600)
    assert (ok, reason) == (False, "rate_limited")
    assert wait_hint >= 60.0
    env.policy.clear(key)

    env.policy.trip_verification(key, "verify")
    assert bs.should_auto_check_session(3600)[:2] == (False, "circuit_blocked")
    env.policy.clear(key)

    # Fresh persisted valid status skips check until interval elapses
    assert bs.check_session()["ok"] is True
    assert len(env.calls) == 1
    saved = json.loads(bs.STATUS_PATH.read_text(encoding="utf-8"))
    ok, reason, wait_hint = bs.should_auto_check_session(3600, now=saved["checked_at"] + 1800)
    assert (ok, reason) == (False, "fresh")
    assert 1700 <= wait_hint <= 1800

    # Past interval -> due again
    ok, reason, _ = bs.should_auto_check_session(3600, now=saved["checked_at"] + 3601)
    assert (ok, reason) == (True, "due")

    # Persisted expired syncs flag without network
    env.response["body"] = {"code": "Login Required"}
    assert bs.check_session()["code"] == "expired"
    env.state.set_buff_auth_expired(False)
    assert bs.should_auto_check_session(3600)[:2] == (False, "already_expired")
    assert env.state.get_status()["buff_auth_expired"] is True
    assert len(env.calls) == 2


def test_restore_persisted_auth_flags_requires_matching_identity(isolated_buff_env):
    env = isolated_buff_env
    bs = env.buff_session

    env.response["body"] = {"code": "Login Required"}
    assert bs.check_session()["code"] == "expired"
    assert len(env.calls) == 1

    # Simulate fresh restart state
    env.state.set_buff_auth_expired(False)
    assert bs.restore_persisted_auth_flags() == "expired"
    assert env.state.get_status()["buff_auth_expired"] is True
    assert len(env.calls) == 1

    # Verification status restoration
    env.state.set_buff_auth_expired(False)
    env.response["status"] = 403
    env.response["body"] = {"code": "Forbidden", "msg": "安全验证"}
    assert bs.check_session()["code"] == "verification"
    env.state.set_buff_verification_required(False)
    assert bs.restore_persisted_auth_flags() == "verification"
    assert env.state.get_status()["buff_verification_required"] is True

    # Identity mismatch (credential change) does not restore old flag
    env.state.set_buff_verification_required(False)
    env.config.update_buff_credentials("session=new; csrf_token=c2", user_agent="Chrome/test", source="extension")
    assert bs.restore_persisted_auth_flags() is None
    assert env.state.get_status()["buff_verification_required"] is False
    assert env.state.get_status()["buff_auth_expired"] is False


def test_check_session_notify_on_failure_and_cooldown_reset(isolated_buff_env, monkeypatch):
    from app import config_loader, notify

    env = isolated_buff_env
    bs = env.buff_session
    pushed = []
    monkeypatch.setattr(notify, "_last_manual_notify_time", {})
    monkeypatch.setattr(config_loader, "load_app_config_validated", lambda: {"notify": {"pushplus_token": "tok"}})
    monkeypatch.setattr(
        notify,
        "send_pushplus",
        lambda token, title, content, template="html": pushed.append((title, content)) or True,
    )

    # Manual check (notify_on_failure=False) does not send push notification
    env.response["body"] = {"code": "Login Required"}
    assert bs.check_session()["code"] == "expired"
    assert pushed == []

    # Auto check (notify_on_failure=True) notifies on expired
    assert bs.check_session(notify_on_failure=True)["code"] == "expired"
    assert len(pushed) == 1

    # Network error (503) does not notify
    env.response["status"] = 503
    env.response["body"] = {"code": "ERROR"}
    assert bs.check_session(notify_on_failure=True)["code"] == "error"
    assert len(pushed) == 1

    # Successful import resets cooldown so a future failure can notify immediately
    env.response["status"] = 200
    env.response["body"] = {"code": "OK", "data": {"user_info": {"steamid": env.account["steam_id"]}}}
    assert bs.import_session("session=fresh; csrf_token=c2", user_agent="Chrome/test", source="extension")["ok"] is True

    env.response["body"] = {"code": "Login Required"}
    assert bs.check_session(notify_on_failure=True)["code"] == "expired"
    assert len(pushed) == 2

    # Account mismatch notifies on auto check
    notify.reset_manual_notify_cooldown("Buff")
    env.response["body"] = {"code": "OK", "data": {"user_info": {"steamid": "76561198000000099"}}}
    assert bs.check_session(notify_on_failure=True)["code"] == "account_mismatch"
    assert len(pushed) == 3


def test_buff_session_monitor_worker_behavior(isolated_buff_env, monkeypatch):
    from app.services import lifecycle, workers

    env = isolated_buff_env
    bs = env.buff_session
    monkeypatch.setattr(lifecycle, "_shutdown", threading.Event())

    # Restore persisted flag at startup; interval=0 disables probing
    env.response["body"] = {"code": "Login Required"}
    bs.check_session()
    env.state.set_buff_auth_expired(False)
    calls_before = len(env.calls)

    monkeypatch.setattr(
        workers,
        "load_app_config_validated",
        lambda: {"system": {"buff_session_check_interval_minutes": 0}},
    )
    waits = []
    monkeypatch.setattr(workers, "wait_for_shutdown", lambda s: waits.append(s) or True)
    workers.buff_session_monitor_worker()
    assert env.state.get_status()["buff_auth_expired"] is True
    assert len(env.calls) == calls_before
    assert waits == [300]

    # When enabled and due, performs one check(notify_on_failure=True)
    env.state.set_buff_auth_expired(False)
    env.config.update_buff_credentials("session=new2; csrf_token=c2", user_agent="Chrome/test", source="extension")
    pushed = []
    from app import notify
    monkeypatch.setattr(notify, "_last_manual_notify_time", {})
    monkeypatch.setattr(notify, "notify_manual_intervention_required", lambda p, m: pushed.append((p, m)) or True)
    monkeypatch.setattr(
        workers,
        "load_app_config_validated",
        lambda: {"system": {"buff_session_check_interval_minutes": 60}},
    )
    waits2 = []

    def fake_wait(secs):
        waits2.append(secs)
        return len(waits2) >= 2

    monkeypatch.setattr(workers, "wait_for_shutdown", fake_wait)
    workers.buff_session_monitor_worker()
    assert waits2[0] == 30
    assert env.state.get_status()["buff_auth_expired"] is True
    assert len(pushed) == 1


def test_pipeline_context_wait_retry_exits_on_buff_auth_expired_without_request_stop(monkeypatch):
    from app import pipeline_context
    from app.state import State

    state = State()
    ctx = pipeline_context.PipelineContext(state, "flow-1")
    sleeps = []

    def flip_on_second_sleep(secs):
        sleeps.append(secs)
        if len(sleeps) == 1:
            state.set_buff_auth_expired(True)

    monkeypatch.setattr(pipeline_context.time, "sleep", flip_on_second_sleep)
    assert ctx.wait_retry(10) is True
    assert len(sleeps) == 1
    assert state.is_stop_requested() is False
    st = state.get_status()
    assert (st["status"], st["step"]) == ("error", "BUFF_AUTH_EXPIRED")


def test_pipeline_context_handles_minimal_state_double():
    from app.pipeline_context import PipelineContext

    minimal = SimpleNamespace(
        is_stop_requested=lambda: False,
        set_status=lambda *a, **kw: None,
        log=lambda *a, **kw: None,
    )
    ctx = PipelineContext(minimal, "minimal")
    assert ctx.buff_auth_block_reason() is None


def test_running_pipeline_stops_mid_scan_and_unblocks_replacement(isolated_buff_env, monkeypatch):
    from app import pipeline, pipeline_steps

    env = isolated_buff_env
    state = env.state
    monkeypatch.setattr(pipeline, "get_state", lambda: state)
    monkeypatch.setattr(pipeline, "get_buff_credentials", lambda: {"cookies": "session=s1; csrf_token=c1"})
    monkeypatch.setattr(pipeline, "get_proxy_manager", lambda: SimpleNamespace(is_proxy_enabled=lambda: False))
    monkeypatch.setattr(pipeline, "get_network_checker", lambda: SimpleNamespace(report_success=lambda: None))
    monkeypatch.setattr(pipeline, "SteamClient", lambda: object())
    monkeypatch.setattr(pipeline, "StabilityAnalyzer", lambda **kw: object())
    monkeypatch.setattr(pipeline, "create_buff_client_from_config", lambda *a: object())
    monkeypatch.setattr(pipeline, "apply_strategy_to_config", lambda cfg, kind: cfg)
    monkeypatch.setattr(
        pipeline,
        "BuyPolicyMonitor",
        lambda cfg: SimpleNamespace(is_current=lambda: True, refresh=lambda cfg: (cfg, False)),
    )
    monkeypatch.setattr(pipeline, "get_unresolved_checkout", lambda: None)
    monkeypatch.setattr(pipeline_steps, "is_strategy_module_enabled", lambda *a, **kw: False)
    monkeypatch.setattr(pipeline_steps, "jittered_sleep", lambda *a: None)
    monkeypatch.setattr(pipeline, "jittered_sleep", lambda *a: None)

    items = [
        {"name": "Item 1", "goods_id": 101, "min_price": 5.0},
        {"name": "Item 2", "goods_id": 102, "min_price": 5.0},
    ]
    monkeypatch.setattr(pipeline, "_fetch_and_filter_deals", lambda *a: (items, False))

    seen = []

    def fake_begin(config, item, get_purchases_fn):
        seen.append(item["goods_id"])
        state.set_buff_auth_expired(True)
        return SimpleNamespace(finish=lambda *_a, **_kw: None)

    import app.decision_capture_buy as dcb
    monkeypatch.setattr(dcb, "begin", fake_begin)
    monkeypatch.setattr(pipeline_steps, "_passes_custom_buy_modules", lambda *a, **kw: True)
    monkeypatch.setattr(pipeline_steps, "_passes_final_buff_precheck", lambda *a, **kw: True)
    monkeypatch.setattr(
        pipeline,
        "lock_and_confirm_payment",
        lambda *a, **kw: pytest.fail("expired auth must stop before checkout"),
    )

    with pipeline._pipeline_start_lock:
        pipeline._pipeline_thread = None
    assert pipeline.start_pipeline({"pipeline": {"target_balance": 100}}) is True
    with pipeline._pipeline_start_lock:
        thread = pipeline._pipeline_thread
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()

    st = state.get_status()
    assert (st["status"], st["step"]) == ("error", "BUFF_AUTH_EXPIRED")
    assert st["buff_auth_expired"] is True
    assert env.buff_auth.buff_credential_replacement_block_reason() == ""
    assert seen == [101]


def test_pipeline_and_receive_worker_notify_on_buff_auth_errors(isolated_buff_env, monkeypatch):
    from app import config_loader, notify, pipeline
    from app.services import lifecycle, workers
    from buff import BuffAuthExpired, BuffVerificationRequired

    env = isolated_buff_env
    pushed = []
    monkeypatch.setattr(notify, "_last_manual_notify_time", {})
    monkeypatch.setattr(config_loader, "load_app_config_validated", lambda: {"notify": {"pushplus_token": "tok"}})
    monkeypatch.setattr(
        notify,
        "send_pushplus",
        lambda token, title, content, template="html": pushed.append((title, content)) or True,
    )

    # Pipeline exception notifications
    monkeypatch.setattr(pipeline, "get_state", lambda: env.state)
    monkeypatch.setattr(pipeline, "get_buff_credentials", lambda: {"cookies": "session=s1; csrf_token=c1"})
    monkeypatch.setattr(pipeline, "get_proxy_manager", lambda: SimpleNamespace(is_proxy_enabled=lambda: False))
    monkeypatch.setattr(pipeline, "get_network_checker", lambda: SimpleNamespace(report_success=lambda: None))
    monkeypatch.setattr(pipeline, "SteamClient", lambda: object())
    monkeypatch.setattr(pipeline, "StabilityAnalyzer", lambda **kw: object())
    monkeypatch.setattr(pipeline, "create_buff_client_from_config", lambda *a: object())
    monkeypatch.setattr(pipeline, "apply_strategy_to_config", lambda cfg, kind: cfg)
    monkeypatch.setattr(
        pipeline,
        "BuyPolicyMonitor",
        lambda cfg: SimpleNamespace(is_current=lambda: True, refresh=lambda cfg: (cfg, False)),
    )
    monkeypatch.setattr(pipeline, "_fetch_and_filter_deals", lambda *a: ([{"name": "X"}], False))
    monkeypatch.setattr(
        pipeline,
        "_process_deals_for_target",
        Mock(side_effect=BuffAuthExpired("expired")),
    )
    pipeline._run_pipeline({})
    assert len(pushed) == 1
    assert env.state.get_status()["buff_auth_expired"] is True

    env.state.set_buff_auth_expired(False)
    notify.reset_manual_notify_cooldown("Buff")
    monkeypatch.setattr(
        pipeline,
        "_process_deals_for_target",
        Mock(side_effect=BuffVerificationRequired("verify needed")),
    )
    pipeline._run_pipeline({})
    assert len(pushed) == 2
    assert env.state.get_status()["buff_verification_required"] is True

    # Receive worker notifications
    env.state.set_buff_verification_required(False, "")
    monkeypatch.setattr(lifecycle, "_shutdown", threading.Event())
    monkeypatch.setattr(workers, "load_app_config_validated", lambda: {})
    monkeypatch.setattr(workers, "get_buff_credentials", lambda: {"cookies": "s=1"})
    monkeypatch.setattr(workers, "is_steam_background_allowed", lambda: True)
    monkeypatch.setattr(workers, "_buff_background_request_is_safe", lambda: True)
    from app.services import buff_client
    monkeypatch.setattr(buff_client, "create_buff_client_from_config", lambda *_: SimpleNamespace(close=lambda: None))

    notify.reset_manual_notify_cooldown("Buff")
    monkeypatch.setattr(workers, "run_receive_check_once", Mock(side_effect=BuffAuthExpired("expired")))
    waits = []
    monkeypatch.setattr(workers, "wait_for_shutdown", lambda s: waits.append(s) or len(waits) >= 2)
    workers.receive_worker()
    assert len(pushed) == 3
    assert env.state.get_status()["buff_auth_expired"] is True

    env.state.set_buff_auth_expired(False)
    notify.reset_manual_notify_cooldown("Buff")
    monkeypatch.setattr(workers, "run_receive_check_once", Mock(side_effect=BuffVerificationRequired("verify")))
    waits.clear()
    workers.receive_worker()
    assert len(pushed) == 4
    assert env.state.get_status()["buff_verification_required"] is True


def test_config_schema_validates_buff_session_check_interval():
    from app.config_schema import DEFAULTS, _validate_ranges, merge

    assert DEFAULTS["system"]["buff_session_check_interval_minutes"] == 60

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert _validate_ranges(merge(DEFAULTS, {"system": {"buff_session_check_interval_minutes": 0}}))["system"]["buff_session_check_interval_minutes"] == 0
        assert not any("buff_session_check_interval_minutes" in str(w.message) for w in caught)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        res = _validate_ranges(merge(DEFAULTS, {"system": {"buff_session_check_interval_minutes": 5}}))
        assert res["system"]["buff_session_check_interval_minutes"] == 15
        assert any("buff_session_check_interval_minutes" in str(w.message) for w in caught)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        res = _validate_ranges(merge(DEFAULTS, {"system": {"buff_session_check_interval_minutes": -10}}))
        assert res["system"]["buff_session_check_interval_minutes"] == 0
        assert any("buff_session_check_interval_minutes" in str(w.message) for w in caught)
