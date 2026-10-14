import base64


def _header(user, password):
    raw = f"{user}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def test_auth_disabled_without_password():
    from app.api_auth import credentials_from_env, requires_auth

    creds = credentials_from_env(env={})
    assert creds is None
    assert requires_auth("/api/status", creds) is False
    assert requires_auth("/api/export_full", creds) is False


def test_auth_requires_credentials_for_api():
    from app.api_auth import basic_header_matches, credentials_from_env, requires_auth

    creds = credentials_from_env(env={"AETHERSWAP_AUTH_PASS": "pw"})
    assert creds == ("aetherswap", "pw")
    assert requires_auth("/api/status", creds) is True
    assert basic_header_matches("", creds) is False
    assert basic_header_matches(_header("aetherswap", "wrong"), creds) is False
    assert basic_header_matches(_header("other", "pw"), creds) is False
    assert basic_header_matches(_header("aetherswap", "pw"), creds) is True


def test_auth_exempts_runtime_and_static_paths():
    from app.api_auth import credentials_from_env, requires_auth

    creds = credentials_from_env(env={"AETHERSWAP_AUTH_PASS": "pw"})
    assert requires_auth("/api/runtime", creds) is False
    assert requires_auth("/js/main.js", creds) is False
    assert requires_auth("/", creds) is False


def test_auth_custom_username():
    from app.api_auth import credentials_from_env

    creds = credentials_from_env(env={"AETHERSWAP_AUTH_PASS": "pw", "AETHERSWAP_AUTH_USER": "ops"})
    assert creds == ("ops", "pw")


def test_malformed_basic_header_rejected():
    from app.api_auth import basic_header_matches, credentials_from_env

    creds = credentials_from_env(env={"AETHERSWAP_AUTH_PASS": "pw"})
    assert basic_header_matches("Basic !!!not-base64!!!", creds) is False
    assert basic_header_matches("Bearer token", creds) is False
    assert basic_header_matches("basic " + base64.b64encode(b"aetherswap:pw").decode(), creds) is True
