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
