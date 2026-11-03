"""Receipt history retries operate only at the safe HTTP GET boundary."""
import httpx
import pytest
from buff import BuffAuthExpired, BuffRateLimited, BuffBuyer
from tests.test_buff_request_policy import FakeSession, FakeResponse, no_wait_policy, account_fingerprint


def history_page(items=None, total_page=1):
    return FakeResponse({'code': 'OK', 'data': {'items': items or [{'id': 'wanted', 'goods_id': 1}],
                                              'goods_infos': {'1': {'market_hash_name': 'Test item'}}, 'total_page': total_page}})


def test_history_get_recovers_once_from_ssl_eof_without_replaying_writes():
    session = FakeSession(httpx.ConnectError('[SSL: UNEXPECTED_EOF_WHILE_READING] private-cookie'), history_page())
    buyer = BuffBuyer('session=test; csrf_token=test', session=session, request_policy=no_wait_policy())
    orders = buyer.get_purchase_history_orders('csgo', ['wanted'])
    assert [row['id'] for row in orders] == ['wanted']
    assert len(session.calls) == 2
    assert all(method == 'GET' and url.endswith('/api/market/buy_order/history') for method, url, _ in session.calls)
    assert session.calls[0][2]['params'] == session.calls[1][2]['params']


def test_persistent_history_connection_failure_reports_safe_endpoint_and_stops():
    session = FakeSession(httpx.ConnectError('session=secret-one'), httpx.ConnectError('[SSL: UNEXPECTED_EOF_WHILE_READING] session=secret-two'))
    buyer = BuffBuyer('session=test; csrf_token=test', session=session, request_policy=no_wait_policy())
    with pytest.raises(Exception) as caught:
        buyer.get_purchase_history_orders('csgo', ['wanted'])
    assert len(session.calls) == 2
    message = str(caught.value)
    assert '/api/market/buy_order/history' in message
    assert 'TLS' in message
    assert 'secret-' not in message
    assert '重试' in message


@pytest.mark.parametrize('failure,expected', [
    (FakeResponse({'code': 'Login Required'}), BuffAuthExpired),
    (FakeResponse({'code': 'Too Many Requests'}, status_code=429, headers={'Retry-After': '120'}), BuffRateLimited),
    (httpx.ReadTimeout('read timeout'), httpx.ReadTimeout),
])
def test_history_does_not_retry_auth_rate_limit_or_other_errors(failure, expected):
    session = FakeSession(failure)
    buyer = BuffBuyer('session=test; csrf_token=test', session=session, request_policy=no_wait_policy())
    with pytest.raises(expected):buyer.get_purchase_history_orders('csgo', ['wanted'])
    assert len(session.calls) == 1


def test_history_retry_obeys_existing_global_pacing():
    from buff import BuffRequestPolicy
    clock = [10.0]; waits=[]
    def sleep(seconds):
        waits.append(seconds); clock[0] += seconds
    policy = BuffRequestPolicy(min_interval=2, state_path=None, persist=False, clock=lambda: clock[0], sleeper=sleep)
    session = FakeSession(httpx.ConnectTimeout('timeout'), history_page())
    buyer = BuffBuyer('session=test; csrf_token=test', session=session, request_policy=policy)
    assert buyer.get_purchase_history_orders('csgo', ['wanted'])
    assert waits == [2]


def test_receive_check_reports_failed_stage_without_exposing_request_secrets(monkeypatch):
    from app.services import workers, receipt_reconciliation
    from tests.test_receipt_reconciliation import sample
    purchase, _, _ = sample()
    monkeypatch.setattr(workers, 'is_steam_background_allowed', lambda: True)
    monkeypatch.setattr(workers, '_buff_background_request_is_safe', lambda: True)
    monkeypatch.setattr(receipt_reconciliation, 'get_purchases', lambda: [purchase])
    class Client:
        def get_purchase_history_orders(self, *args):
            raise httpx.ConnectError('secret-cookie SSL EOF', request=httpx.Request('GET', 'https://buff.163.com/api/market/buy_order/history?token=secret-token'))
    with pytest.raises(RuntimeError) as caught:
        workers.run_receive_check_once(Client())
    assert '购买历史' in str(caught.value)
    assert 'secret-' not in str(caught.value)
    assert 'GET' in str(caught.value)


def test_worker_reports_stable_unmatched_reason_once_without_repeated_alert(monkeypatch):
    import threading
    from unittest.mock import Mock
    from app.services import workers, buff_client, lifecycle
    monkeypatch.setattr(lifecycle, '_shutdown', threading.Event())
    buyer, logs, alert = Mock(), Mock(), Mock()
    monkeypatch.setattr(workers, 'load_app_config_validated', lambda: {})
    monkeypatch.setattr(workers, 'get_buff_credentials', lambda: {'cookies': 'test'})
    monkeypatch.setattr(workers, 'is_steam_background_allowed', lambda: True)
    monkeypatch.setattr(workers, '_buff_background_request_is_safe', lambda: True)
    monkeypatch.setattr(buff_client, 'create_buff_client_from_config', lambda *_: buyer)
    monkeypatch.setattr(workers, 'log', logs)
    monkeypatch.setattr(workers, '_worker_alert', alert)
    monkeypatch.setattr(workers, 'run_receive_check_once', lambda *_a, **_k: {'received': 0, 'unmatched_reasons': {'missing_item_fingerprint': 10}})
    rounds = []
    def wait(seconds):
        rounds.append(seconds)
        return len(rounds) >= 4
    monkeypatch.setattr(workers, 'wait_for_shutdown', wait)
    workers.receive_worker()
    reasons = [str(c.args[0]) for c in logs.call_args_list if '唯一特征' in str(c.args[0])]
    assert len(reasons) == 1
    assert '10' in reasons[0]
    alert.assert_not_called()
    buyer.close.assert_called_once()


def test_failure_after_first_page_never_returns_partial_receipt_history():
    first = history_page([{'id': 'first', 'goods_id': 1}], total_page=2)
    session = FakeSession(first, httpx.ConnectError('EOF'), httpx.ConnectError('EOF'))
    buyer = BuffBuyer('session=test; csrf_token=test', session=session, request_policy=no_wait_policy())
    with pytest.raises(httpx.ConnectError) as caught:
        buyer.get_purchase_history_orders('csgo', ['first', 'wanted'])
    assert '第2页' in str(caught.value)
    assert len(session.calls) == 3


def test_history_recovery_rechecks_rate_limit_before_second_request():
    from buff import BuffRequestPolicy
    from buff.request_policy import account_fingerprint
    policy = BuffRequestPolicy(min_interval=0, state_path=None, persist=False)
    key = account_fingerprint({}, account_id='default')
    class Session(FakeSession):
        def request(self, *a, **kw):
            policy.trip_rate_limit(key, 120)
            return super().request(*a, **kw)
    session = Session(httpx.ConnectError('EOF'), history_page())
    buyer = BuffBuyer('session=test; csrf_token=test', session=session, request_policy=policy, account_id='default')
    with pytest.raises(BuffRateLimited):buyer.get_purchase_history_orders('csgo', ['wanted'])
    assert len(session.calls) == 1
