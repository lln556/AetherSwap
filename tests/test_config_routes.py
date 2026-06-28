import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_full_import_uses_validated_config_save(monkeypatch):
    from app.routes import config

    calls = {"validated": None, "raw": False}
    monkeypatch.setattr(config, "save_app_config_validated", lambda data: calls.__setitem__("validated", data))
    monkeypatch.setattr(config, "save_credentials", lambda data: None)
    monkeypatch.setattr(config, "replace_transactions", lambda purchases, sales: None)
    monkeypatch.setattr(config, "accounts_replace_all", lambda data: None)
    monkeypatch.setattr(config, "replace_log", lambda data: None)

    body = config.ImportFullBody(app_config={"pipeline": {"max_discount": 9}})
    result = config.api_import_full(body)

    assert result["ok"] is True
    assert calls["validated"] == {"pipeline": {"max_discount": 9}}
