import asyncio
import base64
import copy
import json
import os
import re
import signal
import socket
import threading
import time
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

ROOT = Path(__file__).resolve().parent.parent
TLS_FILES = (
    "app/gift_engine.py",
    "app/services/steam_auth.py",
    "app/steam_confirm.py",
    "app/steam_delist.py",
    "app/steam_listings.py",
    "app/repair_error_records.py",
    "app/receive_flow.py",
    "app/services/steam_deals.py",
    "steam/market_orders.py",
)
_VERIFY_DISABLED = re.compile(r"verify\s*=\s*False")
_REMOTE_ERROR = "拒绝未认证的远程访问：请设置 AETHERSWAP_AUTH_PASS，或仅在本机回环地址访问"
_CONFIG_FILES = (
    ROOT / "config" / "app_config.json",
    ROOT / "config" / "credentials.json",
    ROOT / "config" / "accounts.json",
)


def _fingerprint(path: Path):
    if not path.exists():
        return None
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


@pytest.fixture
def config_files_unchanged():
    before = {path: _fingerprint(path) for path in _CONFIG_FILES}
    yield
    after = {path: _fingerprint(path) for path in _CONFIG_FILES}
    assert after == before


@pytest.fixture
def no_remote_auth(monkeypatch):
    monkeypatch.delenv("AETHERSWAP_AUTH_PASS", raising=False)
    monkeypatch.delenv("AETHERSWAP_AUTH_USER", raising=False)
    monkeypatch.delenv("AETHERSWAP_ALLOW_UNAUTHENTICATED", raising=False)


def _basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _request(path, host="testclient", headers=None):
    raw = []
    for key, value in (headers or {}).items():
        raw.append((key.lower().encode("latin-1"), value.encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw,
        "client": None if host is None else (host, 50000),
        "server": ("testserver", 80),
    }
    return Request(scope)


def _dispatch(path, host="testclient", headers=None):
    from app.api import _require_api_auth

    async def call_next(_request):
        return JSONResponse({"ok": True})

    return asyncio.run(_require_api_auth(_request(path, host, headers), call_next))


def _assert_security_headers(response):
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"


def _public_dns(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))]


def _stored_secrets():
    return {
        "steam_guard": {"shared_secret": "real-shared"},
        "steam_confirm": {
            "enabled": True,
            "identity_secret": "real-identity",
            "device_id": "real-device",
        },
        "notify": {
            "pushplus_token": "real-token",
            "email_user": "owner@example.com",
            "email_pass": "real-mail-pass",
        },
        "proxy_pool": {
            "webshare_api_key": "real-key",
            "test_url": "https://example.com/ip",
            "proxies": [
                {
                    "host": "proxy.example",
                    "port": 8080,
                    "username": "user",
                    "password": "real-proxy-pass",
                }
            ],
        },
        "pipeline": {"max_discount": 0.8},
    }


def test_tls_verification_stays_enabled():
    for relative in TLS_FILES:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert _VERIFY_DISABLED.search(text) is None, relative


def test_api_module_drops_unused_listing_worker_import():
    text = (ROOT / "app" / "api.py").read_text(encoding="utf-8")
    assert "listing_check_worker" not in text
    assert "def listing_check_worker" in (ROOT / "app" / "services" / "workers.py").read_text(encoding="utf-8")


def test_loopback_and_remote_access_predicates():
    from app.api_auth import (
        allow_unauthenticated_remote,
        is_loopback_client,
        remote_access_blocked,
    )

    for host in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1", "testclient", "127.8.8.8", "[::1]"):
        assert is_loopback_client(host) is True
    assert is_loopback_client(None) is False
    assert is_loopback_client("8.8.8.8") is False
    assert is_loopback_client("10.0.0.1") is False

    assert allow_unauthenticated_remote(env={}) is False
    assert allow_unauthenticated_remote(env={"AETHERSWAP_ALLOW_UNAUTHENTICATED": "yes"}) is True
    assert allow_unauthenticated_remote(env={"AETHERSWAP_ALLOW_UNAUTHENTICATED": "0"}) is False
    assert allow_unauthenticated_remote(env={"AETHERSWAP_ALLOW_UNAUTHENTICATED": "maybe"}) is False

    assert remote_access_blocked("/api/status", "8.8.8.8", None, env={}) is True
    assert remote_access_blocked("/api/status", "127.0.0.1", None, env={}) is False
    assert remote_access_blocked("/api/status", "testclient", None, env={}) is False
    assert remote_access_blocked("/api/runtime", "8.8.8.8", None, env={}) is False
    assert remote_access_blocked("/api/status", "8.8.8.8", ("aetherswap", "pw"), env={}) is False
    assert remote_access_blocked(
        "/api/status", "8.8.8.8", None, env={"AETHERSWAP_ALLOW_UNAUTHENTICATED": "1"}
    ) is False
    assert remote_access_blocked("/", "8.8.8.8", None, env={}) is False
    assert remote_access_blocked("/api/status", None, None, env={}) is False
    assert remote_access_blocked("/api/status", "  ", None, env={}) is False


def test_middleware_blocks_remote_unauthenticated_and_sets_headers(no_remote_auth):
    blocked = _dispatch("/api/status", "8.8.8.8")
    assert blocked.status_code == 403
    assert json.loads(blocked.body) == {"ok": False, "error": _REMOTE_ERROR}
    _assert_security_headers(blocked)

    allowed = _dispatch("/api/status", "127.0.0.1")
    assert allowed.status_code == 200
    assert json.loads(allowed.body)["ok"] is True
    _assert_security_headers(allowed)

    runtime = _dispatch("/api/runtime", "8.8.8.8")
    assert runtime.status_code == 200
    _assert_security_headers(runtime)

    static = _dispatch("/", "8.8.8.8")
    assert static.status_code == 200


def test_middleware_blocks_forwarded_public_client_without_trusting_it_to_allow(no_remote_auth):
    exposed = _dispatch("/api/accounts", "127.0.0.1", {"X-Forwarded-For": "127.0.0.1, 8.8.8.8"})
    assert exposed.status_code == 403
    assert json.loads(exposed.body)["error"] == _REMOTE_ERROR

    forwarded = _dispatch(
        "/api/config",
        "testclient",
        {"Forwarded": 'for=8.8.8.8;proto=https, for="[::1]"'},
    )
    assert forwarded.status_code == 403

    local = _dispatch("/api/status", "127.0.0.1", {"X-Forwarded-For": "unknown, 127.0.0.1"})
    assert local.status_code == 200

    spoofed_loopback = _dispatch("/api/status", "8.8.8.8", {"X-Forwarded-For": "127.0.0.1"})
    assert spoofed_loopback.status_code == 403


def test_middleware_keeps_basic_auth_for_remote_when_password_is_set(monkeypatch):
    monkeypatch.setenv("AETHERSWAP_AUTH_PASS", "pw")
    monkeypatch.delenv("AETHERSWAP_ALLOW_UNAUTHENTICATED", raising=False)

    missing = _dispatch("/api/status", "8.8.8.8")
    assert missing.status_code == 401
    _assert_security_headers(missing)

    accepted = _dispatch("/api/status", "8.8.8.8", {"Authorization": _basic("aetherswap", "pw")})
    assert accepted.status_code == 200
    _assert_security_headers(accepted)


def test_middleware_allows_remote_when_explicitly_disabled(monkeypatch):
    monkeypatch.delenv("AETHERSWAP_AUTH_PASS", raising=False)
    monkeypatch.setenv("AETHERSWAP_ALLOW_UNAUTHENTICATED", "on")

    response = _dispatch("/api/status", "8.8.8.8", {"X-Forwarded-For": "8.8.8.8"})
    assert response.status_code == 200


def test_run_server_warns_when_binding_beyond_loopback_without_auth(monkeypatch, capsys):
    import app.main as main

    monkeypatch.delenv("AETHERSWAP_AUTH_PASS", raising=False)
    monkeypatch.delenv("AETHERSWAP_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.setattr(main.uvicorn, "run", lambda *args, **kwargs: None)

    main.run_server("0.0.0.0", 9)
    warning = capsys.readouterr().out
    assert "安全警告" in warning
    assert "AETHERSWAP_AUTH_PASS" in warning
    assert "/api/*" in warning

    main.run_server("127.0.0.1", 9)
    assert "安全警告" not in capsys.readouterr().out

    monkeypatch.setenv("AETHERSWAP_AUTH_PASS", "pw")
    main.run_server("0.0.0.0", 9)
    assert "安全警告" not in capsys.readouterr().out

    monkeypatch.delenv("AETHERSWAP_AUTH_PASS", raising=False)
    monkeypatch.setenv("AETHERSWAP_ALLOW_UNAUTHENTICATED", "yes")
    main.run_server("::", 9)
    assert "安全警告" not in capsys.readouterr().out


def test_account_routes_hide_passwords(monkeypatch):
    from app.routes import accounts

    stored = {"id": "a1", "username": "steam-user", "password": "secret", "steam_id": "1"}
    blank = {"id": "b2", "username": "blank", "password": "   ", "steam_id": ""}
    monkeypatch.setattr(accounts, "list_accounts", lambda: [dict(stored), dict(blank)])
    monkeypatch.setattr(accounts, "get_current_account", lambda: {"id": "a1"})

    listed = accounts.api_list_accounts()
    assert listed["current_id"] == "a1"
    assert "password" not in listed["accounts"][0]
    assert listed["accounts"][0]["has_password"] is True
    assert listed["accounts"][0]["username"] == "steam-user"
    assert listed["accounts"][1]["has_password"] is False

    monkeypatch.setattr(accounts, "add_account", lambda **kwargs: dict(stored))
    created = accounts.api_add_account(accounts.AccountBody(username="steam-user", password="secret"))
    assert created["ok"] is True
    assert "password" not in created["account"]
    assert created["account"]["has_password"] is True

    monkeypatch.setattr(accounts, "update_account", lambda account_id, **kwargs: dict(stored))
    updated = accounts.api_update_account("a1", accounts.AccountUpdateBody(display_name="Shown"))
    assert updated["account"]["has_password"] is True
    assert "password" not in updated["account"]


def test_config_secret_redaction_round_trip_and_routes(monkeypatch):
    from fastapi import Response
    from app.config_loader import REDACTED_SECRET, redact_config_secrets, restore_redacted_secrets
    from app.routes import config as config_routes

    assert redact_config_secrets({}) == {}
    original = _stored_secrets()
    masked = redact_config_secrets(original)
    assert original["steam_guard"]["shared_secret"] == "real-shared"
    assert masked["steam_guard"]["shared_secret"] == REDACTED_SECRET
    assert masked["steam_confirm"]["identity_secret"] == REDACTED_SECRET
    assert masked["steam_confirm"]["device_id"] == REDACTED_SECRET
    assert masked["steam_confirm"]["enabled"] is True
    assert masked["notify"]["pushplus_token"] == REDACTED_SECRET
    assert masked["notify"]["email_pass"] == REDACTED_SECRET
    assert masked["notify"]["email_user"] == "owner@example.com"
    assert masked["proxy_pool"]["webshare_api_key"] == REDACTED_SECRET
    assert masked["proxy_pool"]["proxies"][0]["password"] == REDACTED_SECRET
    assert masked["pipeline"]["max_discount"] == 0.8

    empty = redact_config_secrets({
        "steam_guard": {"shared_secret": ""},
        "notify": {"email_pass": None, "pushplus_token": ""},
        "proxy_pool": {"proxies": [{"host": "h", "port": 1, "username": "u", "password": ""}]},
    })
    assert empty["steam_guard"]["shared_secret"] == ""
    assert empty["notify"]["email_pass"] is None
    assert empty["proxy_pool"]["proxies"][0]["password"] == ""

    restored = restore_redacted_secrets(masked, original)
    assert restored["steam_guard"]["shared_secret"] == "real-shared"
    assert restored["notify"]["email_pass"] == "real-mail-pass"
    assert restored["proxy_pool"]["webshare_api_key"] == "real-key"
    assert restored["proxy_pool"]["proxies"][0]["password"] == "real-proxy-pass"

    changed = restore_redacted_secrets(
        {
            "steam_guard": {"shared_secret": "new-secret"},
            "steam_confirm": {"device_id": ""},
            "proxy_pool": {
                "proxies": [
                    {
                        "host": "proxy.example",
                        "port": "8080",
                        "username": "user",
                        "password": REDACTED_SECRET,
                    }
                ]
            },
        },
        original,
    )
    assert changed["steam_guard"]["shared_secret"] == "new-secret"
    assert changed["steam_confirm"]["device_id"] == ""
    assert changed["proxy_pool"]["proxies"][0]["password"] == "real-proxy-pass"

    monkeypatch.setattr(config_routes, "load_app_config_validated", lambda: copy.deepcopy(original))
    response = Response()
    fetched = config_routes.api_get_config(response)
    assert fetched["config"]["notify"]["pushplus_token"] == REDACTED_SECRET
    assert response.headers["cache-control"] == "no-store"

    monkeypatch.setattr(config_routes, "update_app_config_validated", lambda patch: copy.deepcopy(original))
    saved = config_routes.api_save_config(config_routes.ConfigBody(config={"pipeline": {"max_discount": 0.5}}))
    assert saved["ok"] is True
    assert saved["config"]["steam_guard"]["shared_secret"] == REDACTED_SECRET


def test_update_and_pipeline_start_preserve_masked_secrets(monkeypatch):
    from app import config_loader
    from app.routes import pipeline

    state = {"current": _stored_secrets()}

    def load():
        return copy.deepcopy(state["current"])

    def save(data):
        state["current"] = copy.deepcopy(data)

    monkeypatch.setattr(config_loader, "load_app_config_validated", load)
    monkeypatch.setattr(config_loader, "save_app_config_validated", save)
    updated = config_loader.update_app_config_validated({
        "steam_guard": {"shared_secret": config_loader.REDACTED_SECRET},
        "notify": {
            "pushplus_token": config_loader.REDACTED_SECRET,
            "email_pass": config_loader.REDACTED_SECRET,
            "email_user": "next@example.com",
        },
        "proxy_pool": {
            "webshare_api_key": config_loader.REDACTED_SECRET,
            "proxies": [{
                "host": "proxy.example",
                "port": 8080,
                "username": "user",
                "password": config_loader.REDACTED_SECRET,
            }],
        },
        "pipeline": {"max_discount": 0.5},
    })
    assert updated["steam_guard"]["shared_secret"] == "real-shared"
    assert updated["notify"]["pushplus_token"] == "real-token"
    assert updated["notify"]["email_pass"] == "real-mail-pass"
    assert updated["notify"]["email_user"] == "next@example.com"
    assert updated["proxy_pool"]["webshare_api_key"] == "real-key"
    assert updated["proxy_pool"]["proxies"][0]["password"] == "real-proxy-pass"
    assert updated["pipeline"]["max_discount"] == 0.5

    seen = {}
    monkeypatch.setattr(pipeline, "load_app_config_validated", lambda: _stored_secrets())
    monkeypatch.setattr(pipeline, "get_pipeline_start_blocker", lambda: {})

    def fake_start(config, **kwargs):
        seen["config"] = config
        return True

    monkeypatch.setattr(pipeline, "start_pipeline", fake_start)
    result = pipeline.api_pipeline_start(pipeline.ConfigBody(config={
        "steam_guard": {"shared_secret": config_loader.REDACTED_SECRET},
        "steam_confirm": {
            "identity_secret": config_loader.REDACTED_SECRET,
            "device_id": "replacement-device",
        },
        "notify": {"email_pass": config_loader.REDACTED_SECRET},
    }))
    assert result == {"ok": True}
    assert seen["config"]["steam_guard"]["shared_secret"] == "real-shared"
    assert seen["config"]["steam_confirm"]["identity_secret"] == "real-identity"
    assert seen["config"]["steam_confirm"]["device_id"] == "replacement-device"
    assert seen["config"]["notify"]["email_pass"] == "real-mail-pass"


def test_proxy_test_url_rejects_internal_targets_and_allows_public_names(monkeypatch):
    from utils.proxy_manager import test_one_proxy, validate_proxy_test_url

    rejected = [
        "http://127.0.0.1/",
        "http://127.0.0.1:80/latest",
        "https://localhost/x",
        "http://foo.localhost/",
        "http://printer.local/",
        "http://db.internal/latest",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://10.1.2.3/",
        "http://192.168.1.20/",
        "http://172.16.4.4/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://240.0.0.1/",
        "https://user:pass@169.254.169.254/latest",
        "file:///etc/passwd",
        "https:///missing-host",
        "http://[::ffff:10.0.0.9]/",
    ]
    for url in rejected:
        ok, err = validate_proxy_test_url(url)
        assert ok is False, url
        assert err

    monkeypatch.setattr("utils.proxy_manager.socket.getaddrinfo", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("literal")))
    assert validate_proxy_test_url("https://8.8.8.8/ip")[0] is True
    assert validate_proxy_test_url("http://[::ffff:1.1.1.1]/")[0] is True

    monkeypatch.setattr(
        "utils.proxy_manager.socket.getaddrinfo",
        lambda *_a, **_k: (_ for _ in ()).throw(socket.gaierror("offline")),
    )
    assert validate_proxy_test_url("https://ipv4.webshare.io/")[0] is True
    assert validate_proxy_test_url("https://example.com/health")[0] is True

    monkeypatch.setattr(
        "utils.proxy_manager.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.8.7", 0))],
    )
    ok, err = validate_proxy_test_url("https://evil.example/")
    assert ok is False
    assert "10.9.8.7" in err

    monkeypatch.setattr(
        "utils.proxy_manager.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", 0, 0, 0))],
    )
    assert validate_proxy_test_url("https://rebind.example/")[0] is False

    monkeypatch.setattr("utils.proxy_manager.socket.getaddrinfo", _public_dns)
    calls = []
    monkeypatch.setattr("utils.proxy_manager.http.get", lambda *args, **kwargs: calls.append((args, kwargs)))
    blocked = test_one_proxy({"host": "proxy", "port": 1}, "http://127.0.0.1/", 3)
    assert blocked["status"] == "failed"
    assert blocked["error"]
    assert calls == []

    class Response:
        status_code = 200
        text = "1.1.1.1\n"

    monkeypatch.setattr(
        "utils.proxy_manager.http.get",
        lambda url, **kwargs: calls.append((url, kwargs)) or Response(),
    )
    checked = test_one_proxy({"host": "proxy", "port": 9}, "https://example.com/ip", 3)
    assert checked["status"] == "ok"
    assert checked["ip_detected"] == "1.1.1.1"
    assert calls[-1][1]["follow_redirects"] is False


def test_proxy_routes_mask_secrets_reject_ssrf_and_log_reload_errors(monkeypatch, config_files_unchanged):
    from app.config_loader import REDACTED_SECRET
    from app.routes import proxy

    stored = _stored_secrets()
    monkeypatch.setattr(proxy, "load_app_config_validated", lambda: copy.deepcopy(stored))
    fetched = proxy.api_get_proxy_config()
    assert fetched["proxy_pool"]["webshare_api_key"] == REDACTED_SECRET
    assert fetched["proxy_pool"]["proxies"][0]["password"] == REDACTED_SECRET
    assert stored["proxy_pool"]["webshare_api_key"] == "real-key"

    saves = []
    monkeypatch.setattr(proxy, "save_app_config_validated", lambda cfg: saves.append(copy.deepcopy(cfg)))
    rejected = proxy.api_save_proxy_config(proxy.ProxyPoolBody(proxy_pool={
        "test_url": "http://169.254.169.254/latest/meta-data",
        "webshare_api_key": "ignored",
    }))
    assert rejected["ok"] is False
    assert rejected["error"]
    assert saves == []

    monkeypatch.setattr(proxy, "load_app_config_validated", lambda: {
        "proxy_pool": {"test_url": "http://metadata.google.internal/", "proxies": [{"host": "h", "port": 1}]}
    })
    tested = proxy.api_test_proxies()
    assert tested == {"ok": False, "error": tested["error"], "results": []}
    assert tested["error"]

    logs = []

    def capture(msg, level="info", category="", flow_id=""):
        logs.append((level, category, msg))

    monkeypatch.setattr("app.state.log", capture)
    monkeypatch.setattr("utils.proxy_manager.socket.getaddrinfo", _public_dns)
    monkeypatch.setattr(proxy, "load_app_config_validated", lambda: copy.deepcopy(stored))

    class Manager:
        def reload(self):
            raise RuntimeError("reload failed")

    monkeypatch.setattr("utils.proxy_manager.get_proxy_manager", lambda: Manager())
    saved = proxy.api_save_proxy_config(proxy.ProxyPoolBody(proxy_pool={
        "enabled": True,
        "strategy": 2,
        "test_url": "https://example.com/ip",
        "timeout_seconds": 4,
        "webshare_api_key": REDACTED_SECRET,
        "proxies": [{
            "host": "proxy.example",
            "port": 8080,
            "username": "user",
            "password": REDACTED_SECRET,
        }],
    }))
    assert saved == {"ok": True}
    pool = saves[-1]["proxy_pool"]
    assert pool["webshare_api_key"] == "real-key"
    assert pool["proxies"][0]["password"] == "real-proxy-pass"
    assert pool["enabled"] is True
    assert any(level == "warn" and category == "proxy" and "保存配置" in msg for level, category, msg in logs)

    logs.clear()
    monkeypatch.setattr(proxy, "load_app_config_validated", lambda: copy.deepcopy(stored))
    cleared = proxy.api_clear_proxies()
    assert cleared["ok"] is True
    assert saves[-1]["proxy_pool"]["proxies"] == []
    assert saves[-1]["proxy_pool"]["webshare_api_key"] == "real-key"
    assert any("清空代理" in msg for _level, _category, msg in logs)

    class WebshareResponse:
        status_code = 200

        def json(self):
            return {
                "results": [{
                    "proxy_address": "8.8.8.8",
                    "port": 3128,
                    "username": "fetched-user",
                    "password": "fetched-pass",
                }],
                "next": None,
            }

    logs.clear()
    monkeypatch.setattr(proxy.http, "get", lambda *args, **kwargs: WebshareResponse())
    monkeypatch.setattr(proxy, "load_app_config_validated", lambda: copy.deepcopy(stored))
    fetched_remote = proxy.api_fetch_webshare()
    assert fetched_remote["ok"] is True
    assert saves[-1]["proxy_pool"]["proxies"][0]["password"] == "fetched-pass"
    assert any(level == "warn" and "获取 Webshare" in msg for level, _category, msg in logs)


def _prepare_factory_reset(monkeypatch, tmp_path):
    from app.routes import config as config_routes
    import app.accounts as accounts_mod

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "log").mkdir()
    disclaimer = tmp_path / ".agreed_disclaimer"
    disclaimer.write_text("agreed", encoding="utf-8")
    events = {"drop": 0, "init": 0, "saves": [], "credentials": [], "accounts": [], "logs": [], "kills": [], "exits": []}
    previous_cache = accounts_mod._cache

    monkeypatch.setattr(config_routes, "clear_transactions", lambda: None)
    monkeypatch.setattr("app.state.clear_log", lambda: None)
    monkeypatch.setattr(config_routes, "accounts_replace_all", lambda data: events["accounts"].append(data))
    monkeypatch.setattr(config_routes, "save_credentials", lambda data: events["credentials"].append(data))
    monkeypatch.setattr(config_routes, "save_app_config_validated", lambda data: events["saves"].append(data))
    monkeypatch.setattr(
        config_routes,
        "log",
        lambda msg, level="info", category="", flow_id="": events["logs"].append((level, category, msg)),
    )
    monkeypatch.setattr("app.database.get_engine", lambda: object())
    monkeypatch.setattr("app.database.init_db", lambda: events.__setitem__("init", events["init"] + 1))
    monkeypatch.setattr(os, "_exit", lambda code: events["exits"].append(code))
    monkeypatch.setattr(os, "kill", lambda pid, sig: events["kills"].append((pid, sig)))
    return config_routes, events, disclaimer, accounts_mod, previous_cache


def test_factory_reset_db_error_returns_failure_without_exit(monkeypatch, tmp_path, config_files_unchanged):
    import sqlmodel

    config_routes, events, disclaimer, accounts_mod, previous_cache = _prepare_factory_reset(monkeypatch, tmp_path)

    def boom(_engine):
        events["drop"] += 1
        raise RuntimeError("disk full")

    monkeypatch.setattr(sqlmodel.SQLModel.metadata, "drop_all", boom)
    try:
        result = config_routes._api_data_init_unlocked()
    finally:
        accounts_mod._cache = previous_cache

    assert result["ok"] is False
    assert result["error"] == "清空数据库失败: RuntimeError: disk full"
    assert events["drop"] == 1
    assert events["init"] == 0
    assert events["kills"] == []
    assert events["exits"] == []
    assert disclaimer.exists()
    assert any(level == "error" and category == "system" and "disk full" in msg for level, category, msg in events["logs"])
    assert "os._exit" not in (ROOT / "app" / "routes" / "config.py").read_text(encoding="utf-8")


def test_factory_reset_success_uses_sigint(monkeypatch, tmp_path, config_files_unchanged):
    import sqlmodel

    config_routes, events, disclaimer, accounts_mod, previous_cache = _prepare_factory_reset(monkeypatch, tmp_path)
    started = []

    class FakeThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            started.append(self)

        def start(self):
            return None

    monkeypatch.setattr(threading, "Thread", FakeThread)
    monkeypatch.setattr(sqlmodel.SQLModel.metadata, "drop_all", lambda _engine: events.__setitem__("drop", events["drop"] + 1))
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)
    try:
        result = config_routes._api_data_init_unlocked()
        assert result == {"ok": True}
        assert events["kills"] == []
        assert len(started) == 1
        started[0].target()
    finally:
        accounts_mod._cache = previous_cache

    assert events["init"] == 1
    assert events["kills"] == [(os.getpid(), signal.SIGINT)]
    assert events["exits"] == []
    assert disclaimer.exists() is False


def _wait_gift(task_id):
    from app.routes import gift

    deadline = time.monotonic() + 2
    task = None
    while time.monotonic() < deadline:
        task = gift._get_task(task_id)
        if task and task["status"] in {"done", "error"}:
            return task
        time.sleep(0.01)
    raise AssertionError(task)


def test_gift_debug_route_removed_and_tasks_are_bounded(monkeypatch):
    from app.routes import gift

    paths = {getattr(route, "path", None) for route in gift.router.routes}
    assert "/api/gift/debug-config" not in paths
    assert not hasattr(gift, "api_gift_debug_config")

    with gift._task_lock:
        gift._gift_tasks.clear()
    try:
        for index in range(55):
            gift._new_task(f"task-{index}")
        assert len(gift._gift_tasks) == 50
        assert "task-0" not in gift._gift_tasks
        assert "task-4" not in gift._gift_tasks
        assert "task-5" in gift._gift_tasks
        assert "task-54" in gift._gift_tasks
    finally:
        with gift._task_lock:
            gift._gift_tasks.clear()


def test_gift_thread_errors_finish_the_task(monkeypatch):
    from app.routes import gift

    monkeypatch.setattr(gift, "_get_cookies_raw", lambda: "steamLoginSecure=1")
    monkeypatch.setattr(gift, "_check_cookies", lambda _cookies: None)

    def explode(**_kwargs):
        yield {"step": "start", "ok": True, "message": "started"}
        raise RuntimeError("boom")

    monkeypatch.setattr(gift.gift_engine, "run_gift_flow", explode)
    with gift._task_lock:
        gift._gift_tasks.clear()
    try:
        created = gift.api_gift_send(gift.GiftBody(friend_steamid="1", item_id="2", item_type="package"))
        task = _wait_gift(created["task_id"])
        assert task["status"] == "error"
        assert task["progress"][-1]["done"] is True
        assert task["progress"][-1]["ok"] is False
        assert "赠礼流程异常: boom" in task["progress"][-1]["message"]
    finally:
        with gift._task_lock:
            gift._gift_tasks.clear()


def test_gift_thread_without_terminal_step_is_marked_error(monkeypatch):
    from app.routes import gift

    monkeypatch.setattr(gift, "_get_cookies_raw", lambda: "steamLoginSecure=1")
    monkeypatch.setattr(gift, "_check_cookies", lambda _cookies: None)

    def unfinished(**_kwargs):
        yield {"step": "start", "ok": True, "message": "still going"}

    monkeypatch.setattr(gift.gift_engine, "run_gift_flow", unfinished)
    with gift._task_lock:
        gift._gift_tasks.clear()
    try:
        created = gift.api_gift_send(gift.GiftBody(friend_steamid="9", item_id="8", item_type="sub"))
        task = _wait_gift(created["task_id"])
        assert task["status"] == "error"
        assert task["progress"][-1] == {
            "step": "error",
            "message": "赠礼流程未正常结束",
            "ok": False,
            "done": True,
        }
    finally:
        with gift._task_lock:
            gift._gift_tasks.clear()
