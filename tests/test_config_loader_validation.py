import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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
