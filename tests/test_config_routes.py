import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_full_import_uses_validated_config_save(monkeypatch):
    from app.routes import config

    calls = {
        "validated": None,
        "credentials": 0,
        "transactions": 0,
        "accounts": 0,
        "log": 0,
    }
    monkeypatch.setattr(config, "save_app_config_validated", lambda data: calls.__setitem__("validated", data))
    monkeypatch.setattr(config, "save_credentials", lambda data: calls.__setitem__("credentials", calls["credentials"] + 1))
    monkeypatch.setattr(config, "replace_transactions", lambda purchases, sales: calls.__setitem__("transactions", calls["transactions"] + 1))
    monkeypatch.setattr(config, "accounts_replace_all", lambda data: calls.__setitem__("accounts", calls["accounts"] + 1))
    monkeypatch.setattr(config, "replace_log", lambda data: calls.__setitem__("log", calls["log"] + 1))

    body = config.ImportFullBody(app_config={"pipeline": {"max_discount": 9}})
    result = config.api_import_full(body)

    assert result["ok"] is True
    assert calls["validated"] == {"pipeline": {"max_discount": 9}}
    assert calls["credentials"] == 0
    assert calls["transactions"] == 0
    assert calls["accounts"] == 0
    assert calls["log"] == 0


def test_full_credentials_import_holds_buff_auth_lock(monkeypatch):
    from app.routes import config
    from app.services import buff_auth

    events = []

    class Lock:
        def acquire(self, blocking=True):
            events.append(("lock_acquire", blocking))
            return True

        def release(self):
            events.append("lock_release")

    monkeypatch.setattr(buff_auth, "get_buff_auth_lock", lambda: Lock())
    monkeypatch.setattr(config, "save_credentials", lambda data: events.append(("save", data)))
    monkeypatch.setattr(config, "replace_transactions", lambda *args: None)
    monkeypatch.setattr(config, "accounts_replace_all", lambda *args: None)
    monkeypatch.setattr(config, "replace_log", lambda *args: None)

    body = config.ImportFullBody(credentials={"buff": {"cookies": "session=new"}})
    result = config.api_import_full(body)

    assert result["ok"] is True
    assert events == [
        ("lock_acquire", False),
        ("save", {"buff": {"cookies": "session=new"}}),
        "lock_release",
    ]


def test_full_import_explicit_empty_sections_clear_only_those_sections(
    monkeypatch,
):
    from app.routes import config

    calls = []
    monkeypatch.setattr(
        config,
        "save_credentials",
        lambda data: calls.append(("credentials", data)),
    )
    monkeypatch.setattr(
        config,
        "replace_transactions",
        lambda purchases, sales: calls.append(
            ("transactions", purchases, sales)
        ),
    )
    monkeypatch.setattr(
        config,
        "accounts_replace_all",
        lambda data: calls.append(("accounts", data)),
    )
    monkeypatch.setattr(
        config,
        "replace_log",
        lambda data: calls.append(("log", data)),
    )

    result = config.api_import_full(
        config.ImportFullBody(
            credentials={},
            transactions={},
            accounts={},
            log=[],
        )
    )

    assert result["ok"] is True
    assert calls == [
        ("credentials", {}),
        ("transactions", [], []),
        ("accounts", {}),
        ("log", []),
    ]
