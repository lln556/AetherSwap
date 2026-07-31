import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_validate_and_fill_preserves_real_boolean_values():
    from app.config_schema import DEFAULTS, merge, validate_and_fill

    cfg = validate_and_fill(
        merge(
            DEFAULTS,
            {
                "buff": {"auto_ask_seller_to_send": True},
                "stability": {"use_vwap": False},
                "pipeline": {
                    "verbose_debug": True,
                    "steam_listings_debug": True,
                    "start_time_limit_enabled": True,
                },
                "notify": {"holdings_report_drop_enabled": False},
                "steam_confirm": {"enabled": True},
                "system": {"buff_session_keepalive_enabled": True},
                "proxy_pool": {"enabled": True},
                "steam_deals": {"enabled": True},
            },
        )
    )

    assert cfg["buff"]["auto_ask_seller_to_send"] is True
    assert cfg["stability"]["use_vwap"] is False
    assert cfg["pipeline"]["verbose_debug"] is True
    assert cfg["pipeline"]["steam_listings_debug"] is True
    assert cfg["pipeline"]["start_time_limit_enabled"] is True
    assert cfg["notify"]["holdings_report_drop_enabled"] is False
    assert cfg["steam_confirm"]["enabled"] is True
    assert cfg["system"]["buff_session_keepalive_enabled"] is True
    assert cfg["proxy_pool"]["enabled"] is True
    assert cfg["steam_deals"]["enabled"] is True


def test_load_app_config_validated_applies_range_validation(monkeypatch):
    from app import config_loader

    monkeypatch.setattr(config_loader, "_config_cache", {})
    monkeypatch.setattr(config_loader, "_config_cache_ts", 0.0)
    monkeypatch.setattr(config_loader, "load_app_config", lambda: {"pipeline": {"max_discount": 9}})

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        cfg = config_loader.load_app_config_validated()

    assert cfg["pipeline"]["max_discount"] == 1.0


def test_save_app_config_validated_applies_range_validation(monkeypatch):
    from app import config_loader

    saved = {}
    monkeypatch.setattr(config_loader, "save_app_config", lambda data: saved.update(data))

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        config_loader.save_app_config_validated({"buff": {"price_tolerance": -1}})

    assert saved["buff"]["price_tolerance"] == 0.0


def test_config_cache_reloads_external_file_changes_and_returns_copies(
    monkeypatch,
    tmp_path,
):
    import config as config_store
    from app import config_loader

    config_file = tmp_path / "app_config.json"
    monkeypatch.setattr(config_store, "_APP_CONFIG_FILE", config_file)
    config_loader._invalidate_config_cache()

    try:
        config_store.save_app_config(
            {"pipeline": {"start_time_limit_enabled": False}}
        )
        first = config_loader.load_app_config_validated()
        first["pipeline"]["start_time_limit_enabled"] = True

        cached = config_loader.load_app_config_validated()
        assert cached["pipeline"]["start_time_limit_enabled"] is False

        # Simulate a user editing app_config.json while the process is alive.
        # The size change also makes this deterministic on coarse-mtime filesystems.
        config_store.save_app_config(
            {"pipeline": {"start_time_limit_enabled": True}}
        )
        refreshed = config_loader.load_app_config_validated()

        assert refreshed["pipeline"]["start_time_limit_enabled"] is True
    finally:
        config_loader._invalidate_config_cache()
