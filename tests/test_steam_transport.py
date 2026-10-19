import gzip
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import Mock

import pytest
import httpx
from utils import http_client as http

from app.steam_transport import MarketRequestError, post_market
from app.steam_delist import _retry_after


@pytest.fixture
def endpoint():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, dict(self.headers), self.rfile.read(int(self.headers['Content-Length']))))
            status = int(self.path.strip('/'))
            self.send_response(status)
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Retry-After', '3600')
            self.send_header('Location', '/200')
            self.end_headers()
            body = b'null' if status == 429 else b'[]'
            if status == 200 and b'assetid=' in seen[-1][2]:
                body = b'{"success":true,"needs_mobile_confirmation":true}'
            self.wfile.write(gzip.compress(body))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    worker.start()
    yield f'http://127.0.0.1:{server.server_port}', seen
    server.shutdown()
    server.server_close()
    worker.join()


@pytest.mark.parametrize('status', [200, 429, 302])
def test_real_httpx_preserves_request_and_does_not_retry_or_redirect(endpoint, status, monkeypatch, tmp_path):
    url, seen = endpoint
    with http.Client(trust_env=False) as session:
        session.trust_env = False
        session.cookies.update({'sessionid': 'test-session', 'steamLoginSecure': 'test-token', 'browserid': 'test-browser'})
        session.headers['User-Agent'] = 'preflight-agent'
        outcome = {}
        response = post_market(session, f'{url}/{status}', data={'sessionid': 'test-session'},
                                  headers={'Origin': 'https://steamcommunity.com'}, timeout=3, outcome=outcome)
    assert response.status_code == status
    assert response.json() == (None if status == 429 else [])
    assert _retry_after(response) == 3600
    assert len(seen) == 1
    _, headers, body = seen[0]
    assert headers['User-Agent'] == 'preflight-agent'
    assert headers['Origin'] == 'https://steamcommunity.com'
    assert 'browserid=test-browser' in headers['Cookie']
    assert 'steamLoginSecure=test-token' in headers['Cookie']
    assert body == b'sessionid=test-session'
    assert outcome['request_sent'] and outcome['transport'] == 'httpx'


@pytest.mark.parametrize(('failure', 'sent'), [
    (httpx.ConnectError('secret'), False),
    (httpx.ConnectTimeout('secret'), False),
    (httpx.ProxyError('secret'), False),
    (httpx.PoolTimeout('secret'), False),
    (httpx.ReadTimeout('secret'), True),
    (httpx.WriteError('secret'), True),
    (httpx.RemoteProtocolError('secret'), True),
])
def test_transport_failure_does_not_leak_secrets_or_retry(monkeypatch, failure, sent):
    runner = Mock(side_effect=failure)
    monkeypatch.setattr(httpx.Client, 'send', runner)
    with http.Client(trust_env=False) as session:
        session.cookies['steamLoginSecure'] = 'secret'
        outcome = {}
        with pytest.raises(MarketRequestError) as exc:
            post_market(session, 'https://steamcommunity.com/market/removelisting/1',
                        data={'sessionid': 'secret'}, headers={}, timeout=3, outcome=outcome)
    assert 'secret' not in str(exc.value)
    assert outcome['request_sent'] is sent
    runner.assert_called_once()


@pytest.mark.parametrize('status', [200, 429])
def test_listing_uses_real_httpx(endpoint, monkeypatch, status):
    from steam import market
    from urllib.parse import parse_qs
    url, seen = endpoint
    monkeypatch.setattr(market, 'SELL_ITEM_URL', f'{url}/{status}')
    with http.Client(trust_env=False) as session:
        session.trust_env = False
        session.cookies['sessionid'] = 'test-session'
        out = market.list_item(session, 'test-session', 730, '2', 'asset-1', 1889)
    assert out['status_code'] == status
    assert out['request_sent'] and out['transport'] == 'httpx' and out['phase'] == 'listing'
    assert len(seen) == 1
    assert parse_qs(seen[0][2].decode()) == {
        'sessionid': ['test-session'], 'appid': ['730'], 'contextid': ['2'],
        'assetid': ['asset-1'], 'amount': ['1'], 'price': ['1889'],
    }
    if status == 200:
        assert json.loads(out['text']) == {'success': True, 'needs_mobile_confirmation': True}
    else:
        assert out['retry_after'] == '3600'


@pytest.mark.parametrize(('failure', 'sent'), [
    (httpx.ConnectError('secret'), False),
    (httpx.ReadTimeout('secret'), True),
])
def test_listing_transport_errors_preserve_submission_certainty(monkeypatch, failure, sent):
    from steam.market import list_item
    runner = Mock(side_effect=failure)
    monkeypatch.setattr(httpx.Client, 'send', runner)
    with http.Client(trust_env=False) as session:
        session.trust_env = False
        out = list_item(session, 'secret', 730, '2', 'asset', 1889)
    assert out['request_sent'] is sent
    assert out['status_code'] is None
    assert '上架' in out['error'] and '撤单' not in out['error'] and 'secret' not in out['error']
    runner.assert_called_once()
