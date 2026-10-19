"""Transport regressions using real HTTPX encoding and cookie processing."""
import time
from urllib.parse import parse_qs

import httpx
import pytest

from utils import http_client as http


@pytest.fixture
def wire(monkeypatch):
    seen = []
    responses = []

    def handle(_transport, request):
        request.read()
        seen.append(request)
        return responses.pop(0) if responses else httpx.Response(200, json={'success': True})

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', handle)
    return seen, responses


def test_form_encoding_preserves_rsa_bytes_repeated_values_and_omits_none(wire):
    seen, _ = wire
    with http.Client(trust_env=False) as client:
        data = {'encrypted_password': b'abc+/=', 'flag': True, 'missing': None,
                'ids[]': [1, 2, None], 'name': '中文'}
        prepared = client.build_request('POST', 'https://test.example', data=data)
        client.post('https://test.example', data)
    assert seen[0].content == prepared.content
    assert parse_qs(seen[0].content.decode()) == {
        'encrypted_password': ['abc+/='], 'flag': ['True'],
        'ids[]': ['1', '2'], 'name': ['中文'],
    }
    assert seen[0].headers['Content-Type'] == 'application/x-www-form-urlencoded'


def test_rotated_cookies_survive_proxy_transport_change_and_are_account_local(wire):
    seen, responses = wire
    responses.append(httpx.Response(200, headers=[('set-cookie', 'sessionid=new; Path=/; Secure')]))
    with http.Client(trust_env=False) as client, http.Client(trust_env=False) as other:
        client.cookies.set('sessionid', 'old', domain='steamcommunity.com', secure=True)
        client.get('https://steamcommunity.com/market')
        client.get('https://steamcommunity.com/market', proxies={'https': 'http://proxy.example:8080'})
        client.get('http://steamcommunity.com/market')
        other.get('https://steamcommunity.com/market')
        assert client.cookies.get_dict(domain='steamcommunity.com')['sessionid'] == 'new'
    assert seen[0].headers['cookie'] == 'sessionid=old'
    assert seen[1].headers['cookie'] == 'sessionid=new'
    assert 'cookie' not in seen[2].headers
    assert 'cookie' not in seen[3].headers


def test_cookie_expiry_and_path_are_respected(wire):
    seen, _ = wire
    with http.Client(trust_env=False) as client:
        client.cookies.set('expired', 'gone', domain='test.example', expires=int(time.time()) - 30)
        client.cookies.set('narrow', 'yes', domain='test.example', path='/private')
        client.cookies.set('other', 'no', domain='other.example')
        client.get('https://test.example/')
        client.get('https://test.example/private/detail')
        client.cookies.clear_expired_cookies()
        assert 'expired' not in client.cookies
    assert 'cookie' not in seen[0].headers
    assert seen[1].headers['cookie'] == 'narrow=yes'


def test_session_reuses_pool_and_releases_all_transports(monkeypatch, wire):
    closed = []
    original_close = httpx.Client.close

    def close(client):
        closed.append(client)
        original_close(client)

    monkeypatch.setattr(httpx.Client, 'close', close)
    with http.Client(trust_env=False) as client:
        for _ in range(3):
            client.get('https://test.example', timeout=(2, 4))
        assert len(client._clients) == 1
        for n in range(5):
            client.get('https://test.example', proxies={'https': f'http://proxy{n}.example:8080'})
        assert len(client._clients) == 4
    assert len(closed) == 6 and all(c.is_closed for c in closed)
    assert wire[0][0].extensions['timeout'] == {'connect': 2, 'read': 4, 'write': 4, 'pool': 2}


def test_buff_real_httpx_rotates_session_and_csrf_before_write(wire, tmp_path):
    from buff.buyer import BuffBuyer
    from buff.request_policy import BuffRequestPolicy

    seen, responses = wire
    responses.extend([
        httpx.Response(200, json={'code': 'OK', 'data': {}}, headers=[
            ('set-cookie', 'session=rotated; Domain=.buff.163.com; Path=/; Secure'),
            ('set-cookie', 'csrf_token=new-csrf; Domain=.buff.163.com; Path=/; Secure'),
        ]),
        httpx.Response(200, json={'code': 'OK', 'data': {}}),
    ])
    policy = BuffRequestPolicy(min_interval=0, state_path=tmp_path / 'policy.json')
    buyer = BuffBuyer('session=old; csrf_token=old-csrf', request_policy=policy)
    try:
        buyer._make_request('GET', 'https://buff.163.com/api/read')
        buyer._make_request('POST', 'https://buff.163.com/api/write', json={'ids': ['one']})
        assert buyer.session.trust_env is False
    finally:
        buyer.close()
    assert seen[1].headers['x-csrftoken'] == 'new-csrf'
    assert seen[1].headers['cookie'].count('session=') == 1
    assert 'session=rotated' in seen[1].headers['cookie']
    assert 'old' not in seen[1].headers['cookie']


def test_steampy_login_replaces_requests_before_any_io(monkeypatch, wire):
    import requests
    from steampy.client import SteamClient
    from steampy.login import LoginExecutor
    from app.services.steam_auth import _do_steampy_login_once

    def forbidden(*args, **kwargs):
        raise AssertionError('requests must not send application traffic')

    def login(client):
        assert isinstance(client._session, http.Client)
        assert client.market._session is client._session
        executor = LoginExecutor('test-user', 'unused', 'unused', client._session)
        executor._api_call('POST', 'IAuthenticationService', 'BeginAuthSessionViaCredentials',
                           params={'encrypted_password': b'abc+/='})
        client._session.cookies.set('steamLoginSecure', 'test-token', domain='steamcommunity.com')
        client._session.cookies.set('sessionid', 'test-session', domain='steamcommunity.com')

    monkeypatch.setattr(requests.Session, 'request', forbidden)
    monkeypatch.setattr(SteamClient, 'login', login)
    monkeypatch.setattr(SteamClient, 'is_session_alive', lambda self: True)
    ok, err, cookies = _do_steampy_login_once('test-user', 'unused', None)
    assert ok, err
    assert cookies['steamLoginSecure'] == 'test-token'
    assert parse_qs(wire[0][0].content.decode()) == {'encrypted_password': ['abc+/=']}


def test_confirmation_multipart_preserves_all_ids_without_file_names(wire):
    from app.steam_confirm import SteamConfirmer
    bot = SteamConfirmer('a2V5', '', '76561198000000000', {})
    try:
        assert bot.accept_all([{'id': 'one', 'nonce': 'nonce1'}, {'id': 'two', 'nonce': 'nonce2'}]) == (True, 2, '')
    finally:
        bot.session.close()
    request = wire[0][0]
    assert request.headers['content-type'].startswith('multipart/form-data; boundary=')
    assert request.content.count(b'name="cid[]"') == 2
    assert request.content.count(b'name="ck[]"') == 2
    assert b'filename=' not in request.content


@pytest.fixture
def local_http():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    servers = []

    def start():
        seen = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_GET(self):
                seen.append((self.path, self.client_address))
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
        worker.start()
        servers.append((server, worker))
        return f'http://127.0.0.1:{server.server_port}', seen

    yield start
    for server, worker in servers:
        server.shutdown()
        server.server_close()
        worker.join()


def test_real_socket_is_reused_within_account_session(local_http):
    endpoint, seen = local_http()
    with http.Client(trust_env=False) as client:
        for _ in range(3):
            assert client.get(endpoint).status_code == 200
    assert len(seen) == 3
    assert len({addr for _, addr in seen}) == 1


def test_explicit_proxy_overrides_ambient_and_buff_can_disable_environment(monkeypatch, local_http):
    ambient, ambient_seen = local_http()
    explicit, explicit_seen = local_http()
    direct, direct_seen = local_http()
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('http_proxy', ambient)
    with http.Client() as client:
        client.get('http://upstream.invalid/ambient')
        client.get('http://upstream.invalid/explicit', proxies={'http': explicit})
    with http.Client(trust_env=False) as client:
        client.get(direct + '/direct')
    assert [p for p, _ in ambient_seen] == ['http://upstream.invalid/ambient']
    assert [p for p, _ in explicit_seen] == ['http://upstream.invalid/explicit']
    assert [p for p, _ in direct_seen] == ['/direct']


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_one_shot_requests_keep_scoped_cookies_across_redirects_without_leaking(monkeypatch, method):
    seen = []

    def handle(_transport, request):
        request.read()
        seen.append(request)
        if request.url.path == '/start':
            return httpx.Response(302, headers={
                'Location': '/eligibility', 'Set-Cookie': 'browserid=test-browser; Path=/; Secure',
            })
        if request.url.path == '/eligibility':
            assert 'steamLoginSecure=test-token' in request.headers.get('cookie', '')
            return httpx.Response(302, headers={
                'Location': '/market', 'Set-Cookie': 'sessionid=rotated; Path=/; Secure',
            })
        if request.url.path == '/market':
            assert 'steamLoginSecure=test-token' in request.headers.get('cookie', '')
            assert 'sessionid=rotated' in request.headers.get('cookie', '')
            assert 'sessionid=old' not in request.headers.get('cookie', '')
            return httpx.Response(302, headers={'Location': 'https://other.example/final'})
        assert 'cookie' not in request.headers
        return httpx.Response(200, json={'success': True})

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', handle)
    cookies = http.Cookies()
    cookies.set('steamLoginSecure', 'test-token', domain='steamcommunity.com', secure=True)
    cookies.set('sessionid', 'old', domain='steamcommunity.com', secure=True)
    send = http.get if method == 'GET' else http.post
    response = send('https://steamcommunity.com/start', cookies=cookies)
    assert response.status_code == 200
    assert len(response.history) == 3
    # A second one-shot call must not inherit any first-call account cookies.
    http.get('https://steamcommunity.com/independent')
    assert len(seen) == 5


@pytest.mark.parametrize(('currency', 'code'), [(23, 'CNY'), (24, 'INR'), (1, 'USD')])
def test_wallet_query_survives_steam_eligibility_redirects(monkeypatch, currency, code):
    from app.gift_engine import get_wallet_balance
    from types import SimpleNamespace
    import utils.proxy_manager as proxy_manager

    seen = []

    def handle(_transport, request):
        seen.append(request)
        assert request.url.host == 'steamcommunity.com', 'fallback must not be needed'
        assert 'steamLoginSecure=test-token' in request.headers.get('cookie', '')
        if len(seen) == 1:
            return httpx.Response(302, headers={'Location': '/market/eligibilitycheck/'})
        if len(seen) == 2:
            return httpx.Response(302, headers={
                'Location': '/market/', 'Set-Cookie': 'webTradeEligibility=test; Path=/; Secure',
            })
        return httpx.Response(200, text=(
            'var g_rgWalletInfo = {"success":1,"wallet_balance":"0",'
            f'"wallet_delayed_balance":"0","wallet_currency":{currency},"wallet_country":"CN"}};'
        ))

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', handle)
    monkeypatch.setattr(proxy_manager, 'get_proxy_manager', lambda: SimpleNamespace(get_proxies_for_request=lambda: None))
    wallet = get_wallet_balance('steamLoginSecure=test-token; sessionid=test-session')
    assert wallet['currency_id'] == currency
    assert wallet['currency_code'] == code
    assert wallet['balance_raw'] == 0
    assert len(seen) == 3


def test_dictionary_cookies_rotate_delete_and_stay_on_their_host(wire):
    seen, responses = wire
    responses.extend([
        httpx.Response(302, headers={'location': '/market/', 'set-cookie': 'sessionid=new; Path=/; Secure'}),
        httpx.Response(200, headers={'set-cookie': 'steamLoginSecure=; Max-Age=0; Path=/; Secure'}),
    ])
    with http.Client(cookies={'sessionid': 'old', 'steamLoginSecure': 'test-token'}, trust_env=False) as client:
        client.get('https://steamcommunity.com/start')
        client.get('https://steamcommunity.com/market/')
        client.get('https://unrelated.example/')
        client.get('http://steamcommunity.com/market/')
        assert client.cookies.get('sessionid') == 'new'
        assert 'steamLoginSecure' not in client.cookies
    assert 'sessionid=old' not in seen[1].headers['cookie']
    assert seen[1].headers['cookie'].count('sessionid=') == 1
    assert seen[2].headers['cookie'] == 'sessionid=new'
    assert all('cookie' not in r.headers for r in seen[3:])


@pytest.mark.parametrize('redirect_fails', [False, True])
def test_per_request_cookies_survive_redirect_without_persisting_overrides(monkeypatch, redirect_fails):
    seen = []

    def handle(_transport, request):
        seen.append(request)
        if request.url.path == '/start':
            return httpx.Response(302, headers={
                'location': '/finish', 'set-cookie': 'sessionid=rotated; Path=/; Secure',
            })
        if request.url.path == '/finish' and redirect_fails:
            raise httpx.ReadTimeout('test timeout', request=request)
        return httpx.Response(200)

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', handle)
    with http.Client(cookies={'sessionid': 'old', 'steamLoginSecure': 'session-token'}, trust_env=False) as client:
        def send():
            return client.get('https://steamcommunity.com/start', cookies={
                'steamLoginSecure': 'override-token', 'transient': 'this-call-only',
            })
        if redirect_fails:
            with pytest.raises(httpx.ReadTimeout):
                send()
        else:
            send()
        assert client.cookies.get('sessionid') == 'rotated'
        client.get('https://steamcommunity.com/next')
        client.get('https://other.example/')
    assert 'steamLoginSecure=override-token' in seen[1].headers['cookie']
    assert 'transient=this-call-only' in seen[1].headers['cookie']
    assert 'sessionid=rotated' in seen[1].headers['cookie']
    assert 'steamLoginSecure=session-token' in seen[2].headers['cookie']
    assert 'transient=' not in seen[2].headers['cookie']
    assert 'cookie' not in seen[3].headers


def test_server_cookie_deletion_during_redirect_does_not_restore_request_override(wire):
    seen, responses = wire
    responses.extend([
        httpx.Response(302, headers={
            'location': '/finish', 'set-cookie': 'steamLoginSecure=; Max-Age=0; Path=/; Secure',
        }),
        httpx.Response(200),
    ])
    with http.Client(cookies={'steamLoginSecure': 'session-token'}, trust_env=False) as client:
        client.get('https://steamcommunity.com/start', cookies={'steamLoginSecure': 'override-token'})
        client.get('https://steamcommunity.com/next')
        assert 'steamLoginSecure' not in client.cookies
    assert all('cookie' not in r.headers for r in seen[1:])
