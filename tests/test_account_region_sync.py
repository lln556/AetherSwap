import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _ProxyManager:
    def get_proxies_for_request(self, failed=False):
        return None


class _Response:
    status_code = 200

    def __init__(self, text="", payload=None):
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_refresh_account_region_currency_success_marks_account_safe(monkeypatch):
    from app.services import account_region
    from app import gift_engine

    updates = {}
    monkeypatch.setattr(account_region, "get_account", lambda account_id: {"id": account_id})
    monkeypatch.setattr(account_region, "get_steam_credentials", lambda: {"cookies": "steamLoginSecure=x"})
    monkeypatch.setattr(
        account_region,
        "update_account",
        lambda account_id, **kwargs: updates.setdefault("payload", kwargs) or {"id": account_id, **kwargs},
    )
    monkeypatch.setattr(gift_engine, "get_wallet_balance", lambda cookies: {"currency_code": "CNY", "currency_id": 23})

    result = account_region.refresh_account_region_currency("acc1")

    assert result["ok"] is True
    assert result["region_code"] == "CN"
    assert result["currency_code"] == "CNY"
    assert updates["payload"]["region_check_ok"] is True
    assert updates["payload"]["region_code"] == "CN"
    assert updates["payload"]["currency_code"] == "CNY"


def test_refresh_account_region_currency_uses_settlement_currency_as_region_source(monkeypatch):
    from app.services import account_region
    from app import gift_engine

    updates = {}
    monkeypatch.setattr(account_region, "get_account", lambda account_id: {"id": account_id})
    monkeypatch.setattr(account_region, "get_steam_credentials", lambda: {"cookies": "steamLoginSecure=x"})
    monkeypatch.setattr(
        account_region,
        "update_account",
        lambda account_id, **kwargs: updates.setdefault("payload", kwargs) or {"id": account_id, **kwargs},
    )
    monkeypatch.setattr(
        gift_engine,
        "get_wallet_balance",
        lambda cookies: {"currency_code": "CNY", "currency_id": 23, "wallet_country": "IN"},
    )

    result = account_region.refresh_account_region_currency("acc1")

    assert result["ok"] is True
    assert result["currency_code"] == "CNY"
    assert result["region_code"] == "CN"
    assert updates["payload"]["region_code"] == "CN"
    assert updates["payload"]["region_check_ok"] is True


def test_refresh_account_region_currency_can_fallback_to_wallet_country_for_unknown_currency(monkeypatch):
    from app.services import account_region
    from app import gift_engine

    updates = {}
    monkeypatch.setattr(account_region, "get_account", lambda account_id: {"id": account_id})
    monkeypatch.setattr(account_region, "get_steam_credentials", lambda: {"cookies": "steamLoginSecure=x"})
    monkeypatch.setattr(
        account_region,
        "update_account",
        lambda account_id, **kwargs: updates.setdefault("payload", kwargs) or {"id": account_id, **kwargs},
    )
    monkeypatch.setattr(
        gift_engine,
        "get_wallet_balance",
        lambda cookies: {"currency_code": "XYZ", "currency_id": 99, "wallet_country": "CN"},
    )

    result = account_region.refresh_account_region_currency("acc1")

    assert result["ok"] is True
    assert result["currency_code"] == "XYZ"
    assert result["region_code"] == "CN"
    assert updates["payload"]["region_check_ok"] is True


def test_refresh_account_region_currency_marks_missing_currency_unsafe(monkeypatch):
    from app.services import account_region
    from app import gift_engine

    updates = {}
    monkeypatch.setattr(account_region, "get_account", lambda account_id: {"id": account_id})
    monkeypatch.setattr(account_region, "get_steam_credentials", lambda: {"cookies": "steamLoginSecure=x"})
    monkeypatch.setattr(
        account_region,
        "update_account",
        lambda account_id, **kwargs: updates.setdefault("payload", kwargs) or {"id": account_id, **kwargs},
    )
    monkeypatch.setattr(gift_engine, "get_wallet_balance", lambda cookies: {"currency_id": 0})

    result = account_region.refresh_account_region_currency("acc1")

    assert result["ok"] is False
    assert "结算币种" in result["error"]
    assert updates["payload"]["region_check_ok"] is False


def test_refresh_account_region_currency_skips_missing_cookie_during_startup(monkeypatch):
    from app.services import account_region

    called = {"updated": False}
    monkeypatch.setattr(account_region, "get_account", lambda account_id: {"id": account_id})
    monkeypatch.setattr(account_region, "get_steam_credentials", lambda: {"cookies": ""})
    monkeypatch.setattr(
        account_region,
        "update_account",
        lambda account_id, **kwargs: called.__setitem__("updated", True),
    )

    result = account_region.refresh_account_region_currency("acc1", skip_unconfigured=True)

    assert result["ok"] is False
    assert result["skipped"] is True
    assert result["status"] == "missing_cookie"
    assert called["updated"] is False


def test_refresh_account_region_currency_marks_missing_cookie_unsafe_when_strict(monkeypatch):
    from app.services import account_region

    updates = {}
    monkeypatch.setattr(account_region, "get_account", lambda account_id: {"id": account_id})
    monkeypatch.setattr(account_region, "get_steam_credentials", lambda: {"cookies": ""})
    monkeypatch.setattr(
        account_region,
        "update_account",
        lambda account_id, **kwargs: updates.setdefault("payload", kwargs) or {"id": account_id, **kwargs},
    )

    result = account_region.refresh_account_region_currency("acc1")

    assert result["ok"] is False
    assert updates["payload"]["region_check_ok"] is False
    assert "Cookie" in updates["payload"]["region_check_error"]


def test_verify_account_triggers_region_currency_refresh(monkeypatch):
    from app.routes import accounts

    monkeypatch.setattr(
        accounts,
        "verify_steam_auto_login",
        lambda account_id: {"ok": True, "status": "auto_ok", "message": "验证通过"},
    )
    monkeypatch.setattr(
        accounts,
        "refresh_account_region_currency",
        lambda account_id: {"ok": True, "region_code": "CN", "currency_code": "CNY"},
    )

    result = accounts.api_verify_account("acc1")

    assert result["ok"] is True
    assert result["region_sync"]["ok"] is True
    assert result["message"] == "验证通过"


def test_get_base_auth_status_strict_reads_store_user_config_country(monkeypatch):
    from app import gift_engine
    import utils.proxy_manager as proxy_manager

    config = html.escape(json.dumps({
        "webapi_token": "jwt-token",
        "account": {"country_code": "hk"},
    }), quote=True)
    page = f'<div data-store_user_config="{config}"></div>'

    monkeypatch.setattr(proxy_manager, "get_proxy_manager", lambda: _ProxyManager())
    monkeypatch.setattr(gift_engine.requests, "get", lambda *args, **kwargs: _Response(page))

    token, country_code, config_data = gift_engine.get_base_auth_status(
        "steamLoginSecure=x",
        require_country=True,
    )

    assert token == "jwt-token"
    assert country_code == "HK"
    assert config_data["account"]["country_code"] == "hk"


def test_get_wallet_balance_returns_wallet_country(monkeypatch):
    from app import gift_engine
    import utils.proxy_manager as proxy_manager

    page = (
        'var g_rgWalletInfo = {"success":1,"wallet_balance":409,'
        '"wallet_currency":23,"wallet_country":"CN"};'
    )

    monkeypatch.setattr(proxy_manager, "get_proxy_manager", lambda: _ProxyManager())
    monkeypatch.setattr(gift_engine.requests, "get", lambda *args, **kwargs: _Response(page))

    wallet = gift_engine.get_wallet_balance("steamLoginSecure=x")

    assert wallet["currency_code"] == "CNY"
    assert wallet["wallet_country"] == "CN"
    assert wallet["country_code"] == "CN"


def test_get_wallet_balance_rejects_unknown_currency_id(monkeypatch):
    from app import gift_engine
    import utils.proxy_manager as proxy_manager

    page = (
        'var g_rgWalletInfo = {"success":1,"wallet_balance":409,'
        '"wallet_currency":999,"wallet_country":"CN"};'
    )

    monkeypatch.setattr(proxy_manager, "get_proxy_manager", lambda: _ProxyManager())
    monkeypatch.setattr(gift_engine.requests, "get", lambda *args, **kwargs: _Response(page))

    try:
        gift_engine.get_wallet_balance("steamLoginSecure=x")
    except RuntimeError as exc:
        assert "未知 Steam 钱包币种 ID: 999" in str(exc)
    else:
        raise AssertionError("unknown wallet currency must not default to USD")
