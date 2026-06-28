import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config_schema import DEFAULTS, merge, validate_and_fill


def test_merge_deepcopies_nested_mutable_defaults():
    defaults = {"pipeline": {"exclude_keywords": ["stamp"]}}
    result = merge(defaults, {})
    result["pipeline"]["exclude_keywords"].append("sticker")
    assert defaults["pipeline"]["exclude_keywords"] == ["stamp"]


def test_validate_bool_strings_use_boolean_semantics():
    result = validate_and_fill(
        {
            "proxy_pool": {"enabled": "false"},
            "steam_confirm": {"enabled": "on"},
        },
        DEFAULTS,
    )
    assert result["proxy_pool"]["enabled"] is False
    assert result["steam_confirm"]["enabled"] is True
