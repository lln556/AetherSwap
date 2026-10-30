"""Public session APIs over real BUFF parsing, with only HTTP replaced."""
import json
import threading

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def session_api(tmp_path, monkeypatch):
    import buff
    import config
    from app import accounts, state, pipeline
    from app.routes.buff_session import router
    from app.services import buff_session
    from app.services import buff_checkout_guard
    monkeypatch.setattr(config, '_CREDENTIALS_FILE', tmp_path / 'credentials.json')
    monkeypatch.setattr(config, '_cache', {})
    monkeypatch.setattr(buff_session, 'STATUS_PATH', tmp_path / 'status.json')
    monkeypatch.setattr(buff_checkout_guard, '_GUARD_PATH', tmp_path / 'checkout.json')
    monkeypatch.setattr(state, 'get_status', lambda: {'status': 'idle'})
    monkeypatch.setattr(state, 'get_pending_payment', lambda: None)
    monkeypatch.setattr(pipeline, 'is_pipeline_running', lambda: False)
    monkeypatch.setattr(pipeline, 'is_shutdown_pending', lambda: False)
    monkeypatch.setattr(pipeline, 'start_pipeline', lambda *a, **k: pytest.fail('credential update must not start trading'))
    account = {'id': 'local-a', 'steam_id': '76561198000000001'}
    monkeypatch.setattr(accounts, 'get_current_account', lambda: dict(account))
    policy = buff.BuffRequestPolicy(min_interval=0, state_path=None, persist=False)
    monkeypatch.setattr(buff, 'get_global_policy', lambda: policy)
    from utils import http_client
    response = {'status': 200, 'body': {'code': 'OK', 'data': {'user_info': {'steamid': account['steam_id']}}}, 'headers': {}}
    calls = []
    def request(self, method, url, **kwargs):
        assert method == 'GET' and str(url).endswith('/account/api/user/info/v2')
        calls.append((method, url))
        if 'handler' in response:
            return response['handler'](method, url)
        return httpx.Response(response['status'], json=response['body'], headers=response['headers'], request=httpx.Request(method, url))
    monkeypatch.setattr(http_client.Client, 'request', request)
    app = FastAPI(); app.include_router(router)
    with TestClient(app, headers={'X-AetherSwap-Intent': 'buff-session'}) as client:
        yield client, response, calls, account, policy, config


def test_status_does_not_probe_and_success_belongs_to_current_credentials(session_api):
    client, response, calls, account, policy, config = session_api
    assert client.get('/api/auth/buff/session').json()['session']['status'] == 'unconfigured'
    config.update_buff_credentials('session=old; csrf_token=csrf', user_agent='Chrome/test', source='manual')
    assert client.get('/api/auth/buff/session').json()['session']['status'] == 'unchecked'
    assert not calls
    result = client.post('/api/auth/buff/session/check', json={}).json()
    assert result['ok'], result
    assert result['session']['status'] == 'valid'
    assert result['session']['checked_at']
    assert len(calls) == 1
    assert 'session=old' not in json.dumps(result)
    assert client.get('/api/auth/buff/session').json()['session']['status'] == 'valid'
    config.update_buff_credentials('session=new; csrf_token=csrf')
    assert client.get('/api/auth/buff/session').json()['session']['status'] == 'unchecked'
    assert len(calls) == 1


def test_import_validates_account_and_preserves_old_credentials_on_failure(session_api):
    client, response, calls, account, policy, config = session_api
    config.update_buff_credentials('session=old; csrf_token=old', source='manual')
    assert client.post('/api/auth/buff/session/check', json={}).json()['ok']
    saved = config.get_buff()
    response['body']['data']['user_info']['steamid'] = '76561198000000002'
    result = client.post('/api/auth/buff/session/import', json={
        'cookies': 'session=candidate; csrf_token=csrf', 'user_agent': 'Chrome/test', 'source': 'extension', 'recovery': True,
    }).json()
    assert not result['ok'] and result['code'] == 'account_mismatch', result
    assert config.get_buff() == saved
    assert result['session']['status'] == 'valid'
    response['body']['data']['user_info']['steamid'] = account['steam_id']
    result = client.post('/api/auth/buff/session/import', json={
        'cookies': 'session=candidate; csrf_token=csrf', 'user_agent': 'Chrome/test', 'source': 'extension', 'recovery': True,
    }).json()
    assert result['ok'], result
    assert 'session=candidate' in config.get_buff()['cookies'].split('; ')
    assert config.get_buff()['source'] == 'extension'
    assert result['session']['status'] == 'valid'
    assert 'candidate' not in json.dumps(result)


@pytest.mark.parametrize('status,body,expected', [
    (200, {'code': 'Login Required'}, 'expired'),
    (429, {'code': 'Too Many Requests'}, 'rate_limited'),
    (403, {'code': 'Forbidden', 'msg': '访问验证'}, 'verification'),
    (503, {'code': 'ERROR'}, 'error'),
])
def test_detection_distinguishes_auth_challenge_limits_and_errors(session_api, status, body, expected):
    client, response, calls, account, policy, config = session_api
    config.update_buff_credentials('session=old; csrf_token=old')
    response.update(status=status, body=body, headers={'Retry-After': '120'})
    result = client.post('/api/auth/buff/session/check', json={}).json()
    assert result['session']['status'] == expected, result
    assert not result['ok']
    assert len(calls) == 1


def test_recovery_probe_cannot_open_trading_circuit_until_verified(session_api):
    import buff
    from buff.request_policy import account_fingerprint
    client, response, calls, account, policy, config = session_api
    key = account_fingerprint({}, account_id='default')
    policy.trip_verification(key, 'Needs verification')
    original = response['body']
    def request(method, url):
        with pytest.raises(buff.BuffRequestBlocked):
            policy.raise_if_blocked(key)
        return httpx.Response(200, json=original, request=httpx.Request(method, url))
    response['handler'] = request
    result = client.post('/api/auth/buff/session/import', json={
        'cookies': 'session=verified; csrf_token=csrf', 'source': 'manual', 'recovery': True,
    }).json()
    assert result['ok'], result
    assert len(calls) == 1
    policy.raise_if_blocked(key)


def test_recovery_never_bypasses_active_rate_limit(session_api):
    from buff.request_policy import account_fingerprint
    client, response, calls, account, policy, config = session_api
    policy.trip_rate_limit(account_fingerprint({}, account_id='default'), 120)
    result = client.post('/api/auth/buff/session/import', json={
        'cookies': 'session=verified; csrf_token=csrf', 'source': 'manual', 'recovery': True,
    }).json()
    assert not result['ok'] and result['code'] == 'rate_limited', result
    assert not calls
    assert not config.get_buff().get('cookies')


@pytest.mark.parametrize('guard', ['running', 'payment', 'unresolved'])
def test_import_is_blocked_before_network_during_transaction_activity(session_api, monkeypatch, guard):
    from app import state
    from app.services import buff_checkout_guard
    client, response, calls, account, policy, config = session_api
    if guard == 'running':
        monkeypatch.setattr(state, 'get_status', lambda: {'status': 'running'})
    elif guard == 'payment':
        monkeypatch.setattr(state, 'get_pending_payment', lambda: {'order_id': 'pending'})
    else:
        monkeypatch.setattr(buff_checkout_guard, 'get_unresolved_checkout', lambda: {'stage': 'unknown'})
    # buff_auth imports these two state functions directly.
    from app.services import buff_auth
    monkeypatch.setattr(buff_auth, 'get_status', state.get_status)
    monkeypatch.setattr(buff_auth, 'get_pending_payment', state.get_pending_payment)
    result = client.post('/api/auth/buff/session/import', json={'cookies': 'session=new; csrf_token=x'}).json()
    assert not result['ok'] and result['code'] == 'checkout_pending'
    assert not calls
    assert not result['session']['can_replace']


def test_account_switch_during_import_keeps_original_credentials(session_api):
    client, response, calls, account, policy, config = session_api
    config.update_buff_credentials('session=old; csrf_token=old')
    before = config.get_buff()
    body = response['body']
    def changed(method, url):
        account['steam_id'] = '76561198000000002'
        return httpx.Response(200, json=body, request=httpx.Request(method, url))
    response['handler'] = changed
    result = client.post('/api/auth/buff/session/import', json={'cookies': 'session=new; csrf_token=x'}).json()
    assert not result['ok'] and result['code'] == 'context_changed', result
    assert config.get_buff() == before


def test_duplicate_detection_does_not_queue_another_platform_request(session_api):
    from concurrent.futures import ThreadPoolExecutor
    client, response, calls, account, policy, config = session_api
    config.update_buff_credentials('session=old; csrf_token=old')
    entered, release = threading.Event(), threading.Event()
    body = response['body']
    def wait(method, url):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json=body, request=httpx.Request(method, url))
    response['handler'] = wait
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(lambda: client.post('/api/auth/buff/session/check', json={}).json())
        assert entered.wait(3)
        try:
            result = client.post('/api/auth/buff/session/check', json={}).json()
            assert not result['ok'] and result['code'] == 'busy'
        finally:
            release.set()
        assert first.result()['ok']
    assert len(calls) == 1


def test_explicit_intent_and_origin_are_required_for_post(session_api):
    client, response, calls, account, policy, config = session_api
    config.update_buff_credentials('session=old; csrf_token=old')
    assert client.post('/api/auth/buff/session/check', json={}, headers={'X-AetherSwap-Intent': ''}).status_code == 403
    assert client.post('/api/auth/buff/session/check', json={}, headers={'Origin': 'https://attacker.example'}).status_code == 403
    assert not calls


def test_failed_save_does_not_clear_existing_verification_circuit(session_api, monkeypatch):
    import buff
    from app import config_loader
    from buff.request_policy import account_fingerprint
    client, response, calls, account, policy, config = session_api
    key = account_fingerprint({}, account_id='default')
    policy.trip_verification(key)
    def fail(*a, **k):
        raise OSError('disk failed secret-cookie')
    monkeypatch.setattr(config_loader, 'update_buff_creds', fail)
    result = client.post('/api/auth/buff/session/import', json={'cookies': 'session=new; csrf_token=x', 'recovery': True}).json()
    assert not result['ok'] and result['code'] == 'save_failed'
    assert 'secret-cookie' not in json.dumps(result)
    with pytest.raises(buff.BuffRequestBlocked):
        policy.raise_if_blocked(key)


def test_background_auth_failure_is_visible_without_another_probe(session_api, monkeypatch):
    from app import state
    client, response, calls, account, policy, config = session_api
    config.update_buff_credentials('session=old; csrf_token=csrf')
    assert client.post('/api/auth/buff/session/check', json={}).json()['ok']
    monkeypatch.setattr(state, 'get_status', lambda: {'status': 'error', 'buff_auth_expired': True})
    status = client.get('/api/auth/buff/session').json()['session']
    assert status['status'] == 'expired'
    assert len(calls) == 1


def test_risk_recovery_failure_keeps_circuit_and_old_session(session_api):
    import buff
    from buff.request_policy import account_fingerprint
    client, response, calls, account, policy, config = session_api
    config.update_buff_credentials('session=old; csrf_token=csrf')
    saved = config.get_buff()
    key = account_fingerprint({}, account_id='default')
    policy.trip_verification(key)
    response['body'] = {'code': 'Login Required'}
    result = client.post('/api/auth/buff/session/import', json={'cookies': 'session=bad; csrf_token=csrf', 'recovery': True}).json()
    assert not result['ok'] and result['code'] == 'expired'
    assert config.get_buff() == saved
    with pytest.raises(buff.BuffRequestBlocked):
        policy.raise_if_blocked(key)


def test_status_read_follows_global_cooldown_without_network(session_api):
    from buff.request_policy import account_fingerprint
    client, response, calls, account, policy, config = session_api
    config.update_buff_credentials('session=old; csrf_token=csrf')
    policy.trip_rate_limit(account_fingerprint({}, account_id='default'), 120)
    result = client.get('/api/auth/buff/session').json()['session']
    assert result['status'] == 'rate_limited'
    assert 0 < result['retry_after'] <= 120
    assert not calls


def test_existing_basic_auth_protects_all_session_routes(session_api, monkeypatch):
    from app.api import app
    monkeypatch.setenv('AETHERSWAP_AUTH_PASS', 'session-test-only')
    client = TestClient(app)
    for path in ['/api/auth/buff/session', '/api/auth/buff/extension.zip']:
        assert client.get(path).status_code == 401
    for path in ['/api/auth/buff/session/check', '/api/auth/buff/session/import']:
        assert client.post(path, json={}).status_code == 401
    result = client.get('/api/auth/buff/session', auth=('aetherswap', 'session-test-only'))
    assert result.status_code == 200
    assert result.headers['Cache-Control'] == 'no-store'
    client.close()


def test_detection_can_run_while_buying_but_cannot_replace_credentials(session_api, monkeypatch):
    from app.services import buff_auth
    client, response, calls, account, policy, config = session_api
    config.update_buff_credentials('session=old; csrf_token=csrf')
    before = config.get_buff()
    monkeypatch.setattr(buff_auth, 'get_status', lambda: {'status': 'running'})
    result = client.post('/api/auth/buff/session/check', json={}).json()
    assert result['ok'] and not result['session']['can_replace']
    assert config.get_buff() == before
    assert len(calls) == 1


def test_account_switch_invalidates_a_saved_success(session_api):
    client, response, calls, account, policy, config = session_api
    config.update_buff_credentials('session=old; csrf_token=csrf')
    assert client.post('/api/auth/buff/session/check', json={}).json()['ok']
    account['id'], account['steam_id'] = 'other', '76561198000000002'
    assert client.get('/api/auth/buff/session').json()['session']['status'] == 'unchecked'
    assert len(calls) == 1


def test_exception_messages_do_not_expose_candidate_cookie(session_api):
    client, response, calls, account, policy, config = session_api
    def fail(method, url):
        raise httpx.ConnectError('secret-session-value https://secret-proxy/')
    response['handler'] = fail
    result = client.post('/api/auth/buff/session/import', json={'cookies': 'session=secret-session-value; csrf_token=x', 'recovery': True}).json()
    assert result['code'] == 'error' and not result['ok']
    assert 'secret-' not in json.dumps(result)
    assert not config.get_buff().get('cookies')


def test_active_pipeline_thread_blocks_replacement_even_with_stale_status(session_api, monkeypatch):
    from app import pipeline
    client, response, calls, account, policy, config = session_api
    monkeypatch.setattr(pipeline, 'is_pipeline_running', lambda: True)
    result = client.post('/api/auth/buff/session/import', json={'cookies': 'session=new; csrf_token=x'}).json()
    assert not result['ok'] and result['code'] == 'checkout_pending'
    assert not calls


def test_old_valid_result_ages_out_without_network(session_api, monkeypatch):
    from app.services import buff_session
    client, response, calls, account, policy, config = session_api
    config.update_buff_credentials('session=old; csrf_token=x')
    assert client.post('/api/auth/buff/session/check', json={}).json()['ok']
    raw = json.loads(buff_session.STATUS_PATH.read_text())
    raw['checked_at'] = 1
    buff_session.STATUS_PATH.write_text(json.dumps(raw))
    result = client.get('/api/auth/buff/session').json()['session']
    assert result['status'] == 'unchecked'
    assert result['checked_at'] == 1
    assert len(calls) == 1


def test_detection_of_network_error_does_not_report_previous_expiry_as_new_result(session_api, monkeypatch):
    from app import state
    client, response, calls, account, policy, config = session_api
    config.update_buff_credentials('session=old; csrf_token=x')
    monkeypatch.setattr(state, 'get_status', lambda: {'buff_auth_expired': True})
    response.update(status=503, body={})
    result = client.post('/api/auth/buff/session/check', json={}).json()
    assert result['code'] == 'error'
    assert result['session']['status'] == 'error'


def test_mutating_current_account_object_cannot_change_import_expected_identity(session_api, monkeypatch):
    from app import accounts
    client, response, calls, account, policy, config = session_api
    monkeypatch.setattr(accounts, 'get_current_account', lambda: account)
    def request(method, url):
        account['steam_id'] = '76561198000000002'
        return httpx.Response(200, json={'code': 'OK', 'data': {'user_info': {'steamid': account['steam_id']}}}, request=httpx.Request(method, url))
    response['handler'] = request
    result = client.post('/api/auth/buff/session/import', json={'cookies': 'session=new; csrf_token=x'}).json()
    assert not result['ok']
    assert not config.get_buff().get('cookies')


def test_empty_submitted_user_agent_uses_same_identity_for_probe_and_save(session_api, monkeypatch):
    from buff import BuffBuyer
    client, response, calls, account, policy, config = session_api
    config.update_buff_credentials('session=old; csrf_token=x', user_agent='OldBrowser/123')
    seen = []
    original = BuffBuyer.verify_session
    def verify(self, *a, **kw):
        seen.append(self.user_agent)
        return original(self, *a, **kw)
    monkeypatch.setattr(BuffBuyer, 'verify_session', verify)
    result = client.post('/api/auth/buff/session/import', json={'cookies': 'session=new; csrf_token=x'}).json()
    assert result['ok']
    assert seen == [config.get_buff()['user_agent']]


def test_failed_circuit_clear_restores_old_cookie_and_old_detection(session_api, monkeypatch):
    from app.services import buff_auth
    client, response, calls, account, policy, config = session_api
    config.update_buff_credentials('session=old; csrf_token=x', user_agent='Old/1')
    old = client.post('/api/auth/buff/session/check', json={}).json()['session']
    monkeypatch.setattr(buff_auth, 'clear_buff_request_policy_after_verification', lambda: False)
    result = client.post('/api/auth/buff/session/import', json={'cookies': 'session=new; csrf_token=x', 'user_agent': 'New/2'}).json()
    assert not result['ok']
    assert 'session=old' in config.get_buff()['cookies']
    assert config.get_buff()['user_agent'] == 'Old/1'
    assert result['session'] == old


def test_download_contains_installable_extension_only(session_api):
    from io import BytesIO
    from zipfile import ZipFile
    client, *_ = session_api
    response = client.get('/api/auth/buff/extension.zip')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/zip'
    with ZipFile(BytesIO(response.content)) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read('buff-session/manifest.json'))
        assert manifest['manifest_version'] == 3
        for entry in [manifest['background']['service_worker'], manifest['action']['default_popup']]:
            assert f'buff-session/{entry}' in names
        assert all(name.startswith('buff-session/') for name in names)
        assert not any('credential' in name or 'config/' in name or 'test-harness' in name for name in names)


def test_import_reports_status_persistence_failure_without_leaving_auth_error(session_api, monkeypatch):
    from app import state
    from app.services import buff_session
    client, response, calls, account, policy, config = session_api
    expired = {'value': True}
    monkeypatch.setattr(state, 'get_status', lambda: {'buff_auth_expired': expired['value']})
    monkeypatch.setattr(state, 'set_buff_auth_expired', lambda value: expired.update(value=value))
    monkeypatch.setattr(buff_session, 'atomic_write_json', lambda *a, **k: (_ for _ in ()).throw(OSError('full')))
    result = client.post('/api/auth/buff/session/import', json={'cookies': 'session=new; csrf_token=x'}).json()
    assert result['ok'] and result['credentials_saved']
    assert result['warning'] == 'status_save_failed'
    assert result['session']['status'] != 'expired'
    assert expired['value'] is False
    assert 'session=new' in config.get_buff()['cookies']
