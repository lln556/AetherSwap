"""Steam session keepalive: cookie expiry parsing and forced refresh."""
import base64
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import steam_auth
from app.state import State


def _jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig"


def _cookie(exp: float, sep: str = "%7C%7C") -> str:
    return f"sessionid=abc; steamLoginSecure=76561198000000000{sep}{_jwt(exp)}"


def test_cookie_expiry_reads_percent_encoded_separator():
    exp = time.time() + 3600
    assert steam_auth.steam_cookie_expiry(_cookie(exp)) == pytest.approx(exp, abs=1)


def test_cookie_expiry_reads_literal_separator_and_bare_token():
    exp = time.time() + 7200
    assert steam_auth.steam_cookie_expiry(_cookie(exp, sep="||")) == pytest.approx(exp, abs=1)
    assert steam_auth.steam_cookie_expiry(f"steamLoginSecure={_jwt(exp)}") == pytest.approx(exp, abs=1)


def test_cookie_expiry_returns_none_when_unreadable():
    assert steam_auth.steam_cookie_expiry("") is None
    assert steam_auth.steam_cookie_expiry("sessionid=abc") is None
    assert steam_auth.steam_cookie_expiry("steamLoginSecure=not-a-jwt") is None
    broken = base64.urlsafe_b64encode(b"not json").decode().rstrip("=")
    assert steam_auth.steam_cookie_expiry(f"steamLoginSecure=76561198000000000%7C%7Cxx.{broken}.sig") is None


def test_state_exposes_steam_auth_expired():
    st = State()
    assert st.get_status()["steam_auth_expired"] is False
    st.set_steam_auth_expired(True)
    assert st.get_status()["steam_auth_expired"] is True
    st.set_steam_auth_expired(False)
    assert st.get_status()["steam_auth_expired"] is False


@pytest.fixture
def auth_env(monkeypatch):
    account = {"id": "account-1", "username": "user", "password": "password",
               "steam_id": "76561198000000000"}
    saved = {"cookies": "steamLoginSecure=76561198000000000%7C%7Ctest-token; sessionid=test"}
    login = Mock(return_value=(False, "auth_pending: pending", {}))
    writes = Mock()
    monkeypatch.setattr(steam_auth, "get_account", lambda _id: dict(account, id=_id))
    monkeypatch.setattr(steam_auth, "get_current_account", lambda: account)
    monkeypatch.setattr(steam_auth, "set_current", lambda _id: True)
    monkeypatch.setattr(steam_auth, "get_steam_credentials", lambda: saved)
    monkeypatch.setattr(steam_auth, "load_app_config_validated", lambda: {})
    monkeypatch.setattr(steam_auth, "_verify_steam_cookies_valid", lambda *_args: False)
    monkeypatch.setattr(steam_auth, "_do_steampy_login", login)
    monkeypatch.setattr(steam_auth, "update_steam_creds", writes)
    monkeypatch.setattr(steam_auth, "fetch_steam_profile_via_api", lambda *_args: ("", ""))
    monkeypatch.setattr(steam_auth, "update_account", lambda *_args, **_kw: None)
    monkeypatch.setattr(steam_auth, "log", lambda *_args, **_kw: None)
    return SimpleNamespace(account=account, saved=saved, login=login, writes=writes)


def test_default_relogin_reuses_valid_cookie_without_login(monkeypatch, auth_env):
    monkeypatch.setattr(steam_auth, "_verify_steam_cookies_valid", lambda *_args: True)

    ok, status, _message = steam_auth.try_steam_auto_relogin()

    assert ok is True and status == "session_valid"
    auth_env.login.assert_not_called()
    auth_env.writes.assert_not_called()


def test_forced_relogin_skips_valid_cookie_and_renews_credentials(monkeypatch, auth_env):
    # The keepalive worker refreshes proactively, so a still-valid cookie must
    # not short-circuit the password login.
    monkeypatch.setattr(steam_auth, "_verify_steam_cookies_valid", lambda *_args: True)
    auth_env.login.return_value = (
        True, None,
        {"steamLoginSecure": "76561198000000000%7C%7Cnew-token", "sessionid": "new-session"},
    )

    ok, status, _message = steam_auth.try_steam_auto_relogin(force=True)

    assert ok is True and status == "auto_ok"
    auth_env.login.assert_called_once()
    auth_env.writes.assert_called_once()
