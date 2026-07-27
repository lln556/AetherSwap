import time
from datetime import timedelta, timezone

import pytest

from utils.time import (
    cutoff_days_ago,
    parse_steam_cooldown,
    parse_steam_history_date,
    resolve_configured_timezone,
    timestamp_in_configured_timezone,
    utc_timestamp,
)


def test_steam_history_parser_preserves_instant_and_returns_utc():
    parsed = parse_steam_history_date("Jul 09 2026 12: +8")

    assert parsed is not None
    assert parsed.tzinfo is timezone.utc
    assert parsed.isoformat() == "2026-07-09T04:00:00+00:00"


@pytest.mark.parametrize(
    "raw",
    [
        "Jul 09 2026 04: +0",
        "Jul 09 2026 04: +00",
        "Jul 09 2026 04: +0000",
        "Jul 09 2026 04",
    ],
)
def test_steam_history_utc_variants_are_timezone_aware(raw):
    parsed = parse_steam_history_date(raw)

    assert parsed is not None
    assert parsed.tzinfo is timezone.utc
    assert parsed.isoformat() == "2026-07-09T04:00:00+00:00"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not a date",
        "Jul 09 2026 04: +1500",
        "Jul 09 2026 04: +1460",
    ],
)
def test_invalid_steam_history_dates_are_rejected(raw):
    assert parse_steam_history_date(raw) is None


def test_steam_cooldown_is_explicit_utc():
    parsed = parse_steam_cooldown("Jul 09, 2026 04:16:03 GMT")

    assert parsed is not None
    assert parsed.tzinfo is timezone.utc
    assert parsed.isoformat() == "2026-07-09T04:16:03+00:00"


def test_utc_timestamp_matches_epoch_in_non_utc_host_timezones():
    assert utc_timestamp() == pytest.approx(time.time(), abs=0.1)


def test_cutoff_days_ago_is_aware_utc():
    before = time.time()
    cutoff = cutoff_days_ago(2)
    after = time.time()

    assert cutoff.tzinfo is timezone.utc
    assert cutoff.timestamp() == pytest.approx(
        (before + after) / 2 - 2 * 86400,
        abs=0.1,
    )
    assert cutoff.utcoffset() == timedelta(0)


def test_epoch_formatting_uses_configured_timezone_not_host_timezone():
    configured_timezone, label = resolve_configured_timezone(
        {
            "timezone": "Asia/Shanghai",
            "timezone_offset_minutes": 480,
        }
    )

    rendered = timestamp_in_configured_timezone(
        1783570563,
        configured_timezone,
    )

    assert label == "Asia/Shanghai"
    assert rendered.strftime("%Y-%m-%d %H:%M:%S") == "2026-07-09 12:16:03"
