import json

import pytest
import httpx
from utils import http_client as http

from steam import client


def response(status=200, body=None, headers=None):
    result = httpx.Response(200)
    result.status_code = status
    result._content = json.dumps(body).encode()
    result.headers.update({"Content-Type": "application/json", **(headers or {})})
    return result


def test_history_can_report_429_without_exposing_response_secrets(monkeypatch):
    raw = response(429, {"cookie": "secret-cookie"}, {"Retry-After": "120"})
    monkeypatch.setattr(client.http, "get", lambda *_args, **_kwargs: raw)

    with pytest.raises(client.SteamHistoryError) as caught:
        client.fetch_history("Test Item", raise_on_error=True)

    assert caught.value.status_code == 429
    assert caught.value.retry_after == 120
    assert "HTTP 429" in str(caught.value)
    assert "body=object" in str(caught.value)
    assert "application/json" in str(caught.value)
    assert "secret-cookie" not in str(caught.value)
    assert client.fetch_history("Test Item") is None


@pytest.mark.parametrize(
    ("body", "reason"),
    [(None, "unexpected_json"), ([], "unexpected_json"),
     ({"success": False}, "unsuccessful_response"),
     ({"success": True, "prices": "invalid"}, "invalid_prices")],
)
def test_history_reports_invalid_success_responses(monkeypatch, body, reason):
    monkeypatch.setattr(client.http, "get", lambda *_args, **_kwargs: response(body=body))
    with pytest.raises(client.SteamHistoryError, match=reason):
        client.fetch_history("Test Item", raise_on_error=True)
    assert client.fetch_history("Test Item") is None


def test_history_transport_errors_do_not_leak_credentials(monkeypatch):
    def fail(*_args, **_kwargs):
        raise httpx.ProxyError("http://user:secret-password@example.com")

    monkeypatch.setattr(client.http, "get", fail)
    with pytest.raises(client.SteamHistoryError, match="ProxyError") as caught:
        client.fetch_history("Test Item", raise_on_error=True)
    assert "secret-password" not in str(caught.value)


@pytest.mark.parametrize("return_currency", [False, True])
def test_history_success_contract_is_unchanged(monkeypatch, return_currency):
    prices = [["Aug 01 2026 01: +0", 10.5, "2"]]
    raw = response(body={"success": True, "prices": prices, "price_prefix": "$"})
    sent = []

    def get(_url, **kwargs):
        sent.append(kwargs)
        return raw

    monkeypatch.setattr(client.http, "get", get)
    result = client.fetch_history("Test Item", return_currency=return_currency)
    assert result == ({"history": prices, "currency": "USD"} if return_currency else prices)
    assert "Accept-Language" not in sent[0]["headers"]
    assert sent[0]["headers"]["Accept"] == "application/json, text/javascript, */*; q=0.01"
    assert sent[0]["headers"]["User-Agent"] == client.HISTORY_USER_AGENT


def test_history_preserves_explicit_headers(monkeypatch):
    sent = []

    def get(_url, **kwargs):
        sent.append(kwargs)
        return response(body={"success": True, "prices": []})

    monkeypatch.setattr(client.http, "get", get)
    headers = {
        "Accept-Language": "zh-CN",
        "Accept": "application/json",
        "User-Agent": "custom-agent",
    }
    assert client.fetch_history("Test Item", headers=headers) == []
    for name, value in headers.items():
        assert sent[0]["headers"][name] == value
    assert client.DEFAULT_HEADERS["Accept-Language"] == "en-US,en;q=0.9"


@pytest.mark.parametrize('prefix,suffix,hint,expected', [
    ('\u00a5', '', 'CNY', 'CNY'),
    ('\u00a5', '', 'JPY', 'JPY'),
    ('\u00a5', '', None, None),
    ('\u00a5', ' JPY', 'CNY', 'JPY'),
    ('CN\u00a5', '', 'JPY', 'CNY'),
    ('$', '', 'CNY', 'USD'),
    ('', '', 'CNY', None),
    (None, None, 'CNY', None),
])
def test_history_currency_hint_disambiguates_symbol_without_overriding_evidence(monkeypatch, prefix, suffix, hint, expected):
    monkeypatch.setattr(client.http, 'get', lambda *a, **kw: response(body={
        'success': True, 'prices': [], 'price_prefix': prefix, 'price_suffix': suffix,
    }))
    result = client.fetch_history('Test Item', return_currency=True, currency_hint=hint)
    assert result == {'history': [], 'currency': expected}
