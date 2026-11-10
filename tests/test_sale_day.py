"""Steam's Pacific date label must not be counted as a local calendar day."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from utils.sale_day import local_day_bounds, local_sale_window, pacific_day_window, unique_local_day

LOCAL = ZoneInfo("Asia/Shanghai")


def epoch(text):
    return datetime.fromisoformat(text).replace(tzinfo=LOCAL).timestamp()


def test_steam_label_day_is_pacific_not_local():
    start, end = pacific_day_window("2026-11-09")
    assert (end - start) == 24 * 3600
    # 2026-11-09 00:00 Pacific is 15:00 the same day in Shanghai.
    assert datetime.fromtimestamp(start, LOCAL).isoformat().startswith("2026-11-09T16:00")


def test_observed_window_yields_the_local_day():
    day = unique_local_day("2026-11-09", submitted_at=epoch("2026-11-10 09:13"),
                           detected_at=epoch("2026-11-10 09:35"))
    assert day == "2026-11-10"


def test_label_matching_the_local_day_is_unchanged():
    day = unique_local_day("2026-11-03", submitted_at=epoch("2026-11-03 20:13"),
                           detected_at=epoch("2026-11-03 22:38"))
    assert day == "2026-11-03"


def test_window_spanning_two_local_days_is_ambiguous():
    # Listed before midnight, found just after: either day is possible.
    day = unique_local_day("2026-11-02", submitted_at=epoch("2026-11-02 20:00"),
                           detected_at=epoch("2026-11-03 16:09"))
    assert day is None
    assert local_day_bounds("2026-11-02", submitted_at=epoch("2026-11-02 20:00"),
                            detected_at=epoch("2026-11-03 16:09")) == ("2026-11-02", "2026-11-03")


def test_label_that_contradicts_our_own_record_yields_no_day():
    # Sold before the listing request we ourselves made: the label cannot be
    # reconciled, so no local day is invented.
    assert unique_local_day("2026-10-18", submitted_at=epoch("2026-10-28 14:04"),
                            detected_at=epoch("2026-10-28 14:43")) is None
    assert local_sale_window("2026-10-18", submitted_at=epoch("2026-10-28 14:04"),
                             detected_at=epoch("2026-10-28 14:43")) is None


@pytest.mark.parametrize("label", [None, "", "yesterday", "2026-13-40"])
def test_unusable_labels_never_raise(label):
    assert pacific_day_window(label) is None
    assert unique_local_day(label, submitted_at=1, detected_at=2) is None
    assert local_day_bounds(label) is None


def test_missing_bounds_still_report_a_possible_day():
    # Without observed bounds only the label window is known; it never covers
    # more than two local days.
    bounds = local_day_bounds("2026-11-09")
    assert bounds == ("2026-11-09", "2026-11-10")


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from sqlmodel import create_engine
    from app import database
    engine = create_engine(f"sqlite:///{tmp_path / 'saleday.db'}")
    monkeypatch.setattr(database, "_engine", engine)
    database.init_db()
    yield database
    engine.dispose()


def test_backfill_only_fills_provable_days(ledger):
    from app.trading_ledger import backfill_sold_day_local, record_settlements
    from app.trading_models import MarketListing, TradeSettlement
    from datetime import datetime, timedelta

    submitted = datetime(2026, 11, 10, 9, 13, tzinfo=LOCAL).timestamp()
    ledger.db_append_purchase({"name": "Item", "price": 2.12, "at": submitted, "assetid": "a"})
    with ledger.get_session() as s:
        s.add(MarketListing(purchase_id=1, account_id="account", assetid="a",
                            submitted_at=submitted, status="active"))
        s.commit()
    record_settlements([{"external_id": "h1", "assetid": "a", "proceeds_minor": 282,
                         "currency": "CNY", "sold_day": "2026-11-09", "date_label": "Sold: 9 Nov"}], "account")
    with ledger.get_session() as s:
        s.get(TradeSettlement, 1).sold_day_local = None   # simulate a pre-migration row
        s.commit()
    backfill_sold_day_local()
    with ledger.get_session() as s:
        assert s.get(TradeSettlement, 1).sold_day_local == "2026-11-10"
