"""Cross-module regressions: price parsing, fees, DB safety, binding, workers."""
from types import SimpleNamespace

import pytest

ITEM = "Danger Zone Case"


# --------------------------------------------------------------------------
# Price parsing / sale history / sold-map conversion
# --------------------------------------------------------------------------

def _history_page(price_html: str) -> dict:
    return {
        "success": True,
        "total_count": 1,
        "results_html": (
            '<div class="market_listing_row" id="history_row_1_1">'
            '<div class="market_listing_listed_date_combined">Sold: 2026-10-18</div>'
            f'<span class="market_listing_price">{price_html}</span></div>'
        ),
        "hovers": "CreateItemHoverFromContainer( g_rgAssets, 'history_row_1_1_name', 730, '2', '1001');",
    }


def test_sale_history_records_seller_proceeds_for_dual_amount():
    from app import steam_sale_history as history

    records, _ids = history.parse_history(_history_page("¥ 1.15 (¥ 1.00)"), {"1001": 1})
    assert records[0]["proceeds_minor"] == 100

    single, _ids = history.parse_history(_history_page("¥ 8.21"), {"1001": 1})
    assert single[0]["proceeds_minor"] == 821


def test_fetch_my_history_sold_handles_dual_amount_and_broken_rate_file(monkeypatch):
    from app import steam_listings

    class BrokenPath:
        def __init__(self, *_args, **_kwargs):
            pass

        def resolve(self):
            return self

        @property
        def parent(self):
            return self

        def __truediv__(self, _other):
            return self

        def exists(self):
            return True

    def run(price_html):
        response = SimpleNamespace(status_code=200, text="{}",
                                   json=lambda: _history_page(price_html))
        monkeypatch.setattr(steam_listings, "_get_with_retry", lambda *_a, **_k: response)
        monkeypatch.setattr("pathlib.Path", BrokenPath)
        return steam_listings.fetch_my_history_sold("steamLoginSecure=token")

    ok, sold, error = run("¥ 1.15 (¥ 1.00)")
    assert ok and not error and sold == {"1001": 1.15}
    ok, sold, error = run("¥ 8.21")
    assert ok and not error and sold == {"1001": 9.44}


# --------------------------------------------------------------------------
# Fee model unification
# --------------------------------------------------------------------------

def test_get_item_price_from_total_matches_seller_for_budget():
    from utils.money import buyer_total_minor, get_item_price_from_total, g_rgWalletInfo
    from app.services.repricing import buyer_total, seller_for_budget

    for total in range(21, 5001):
        assert get_item_price_from_total(total, g_rgWalletInfo) == seller_for_budget(total)
    assert get_item_price_from_total(105, g_rgWalletInfo) == 89
    assert buyer_total(89) == 104 and buyer_total_minor(89) == 104
    assert buyer_total(90) == 106 and buyer_total_minor(90) == 106
    assert get_item_price_from_total(106, g_rgWalletInfo) == 90
    assert get_item_price_from_total(107, g_rgWalletInfo) == 91


# --------------------------------------------------------------------------
# listing_check_worker: history failure must not mark an item as error
# --------------------------------------------------------------------------

def test_listing_check_history_failure_does_not_mark_error(monkeypatch):
    from app.services import workers

    updates, logs, loop = [], [], {"n": 0}

    def shutting_down():
        loop["n"] += 1
        return loop["n"] > 1

    monkeypatch.setattr(workers, "is_shutting_down", shutting_down)
    monkeypatch.setattr(workers, "wait_for_shutdown", lambda _seconds: False)
    monkeypatch.setattr(workers, "is_steam_background_allowed", lambda: True)
    monkeypatch.setattr(workers, "load_app_config_validated", lambda: {"pipeline": {}})
    monkeypatch.setattr(workers, "get_steam_credentials", lambda: {"cookies": "steamLoginSecure=x"})
    monkeypatch.setattr(workers, "get_purchases",
                        lambda: [{"_db_id": 1, "listing": True, "assetid": "asset-1"}])
    monkeypatch.setattr(workers, "update_purchase_by_id", lambda *a, **k: updates.append(a) or True)
    monkeypatch.setattr(workers, "update_purchase", lambda *a, **k: updates.append(a) or True)
    monkeypatch.setattr(workers, "log", lambda msg, *_a, **_k: logs.append(msg))
    monkeypatch.setattr("app.pipeline_context.PipelineContext", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr("app.state.get_state", lambda: object())
    monkeypatch.setattr("app.services.listing_confirmation.sync_listing_confirmations",
                        lambda *_a, **_k: (True, {}, ""))
    monkeypatch.setattr("app.steam_listings.fetch_my_history_sold",
                        lambda *_a, **_k: (False, {}, "boom"))

    workers.listing_check_worker()

    assert updates == []
    assert any("成交历史读取失败" in message for message in logs)


# --------------------------------------------------------------------------
# SQLite durability and purchase integrity
# --------------------------------------------------------------------------

@pytest.fixture
def db_engine(monkeypatch, tmp_path):
    from sqlmodel import create_engine
    from app import database

    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}",
                           connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "_engine", engine)
    database.init_db()
    yield database
    engine.dispose()


def test_sqlite_wal_and_busy_timeout(monkeypatch, tmp_path):
    from app import database

    monkeypatch.delenv("AETHERSWAP_DATABASE_URL", raising=False)
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "wal.db")
    monkeypatch.setattr(database, "_engine", None)
    engine = database.get_engine()
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert str(mode).lower() == "wal"
    assert int(timeout) == 30000
    engine.dispose()
    monkeypatch.setattr(database, "_engine", None)


def test_active_assetid_index_and_duplicate_update_rejected(db_engine):
    db_engine.db_append_purchase({"name": "A", "price": 1.0, "at": 1.0, "assetid": "asset-a"})
    db_engine.db_append_purchase({"name": "B", "price": 2.0, "at": 2.0, "assetid": "asset-b"})
    rows = {row["name"]: row for row in db_engine.db_get_purchases()}

    with db_engine.get_engine().connect() as conn:
        indexes = {r[0] for r in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert "ux_purchase_active_assetid" in indexes

    assert db_engine.db_update_purchase_by_id(rows["B"]["_db_id"], {"assetid": "asset-a"}) is False
    assert db_engine.db_update_purchase(1, {"assetid": "asset-a"}) is False
    after = {row["name"]: row for row in db_engine.db_get_purchases()}
    assert after["A"]["assetid"] == "asset-a" and after["B"]["assetid"] == "asset-b"


def test_receipt_reconciliation_compares_integer_minor_units(db_engine):
    db_engine.db_append_purchase({
        "name": "Item", "price": 0.3, "at": 1.0, "pending_receipt": True,
        "buff_order_id": "bill-1", "goods_id": 7, "account_id": "acct",
    })
    row = db_engine.db_get_purchases()[0]
    expected = {"_db_id": row["_db_id"], "buff_order_id": "bill-1", "goods_id": 7,
                "price": 0.1 + 0.2, "name": "Item", "account_id": "acct"}
    assert db_engine.db_apply_receipt_reconciliation(expected, assetid="asset-1") is True
    updated = db_engine.db_get_purchases()[0]
    assert updated["assetid"] == "asset-1" and updated["pending_receipt"] is False


# --------------------------------------------------------------------------
# receive_flow: refuse ambiguous loose bindings
# --------------------------------------------------------------------------

def _receive(monkeypatch, purchases, task, scan_inventory):
    from app import receive_flow

    accepted = []
    monkeypatch.setattr(receive_flow, "fetch_buff_steam_trade", lambda _c: (True, [task], ""))
    monkeypatch.setattr(receive_flow, "accept_steam_trade_offer",
                        lambda *_a, **_k: accepted.append(True) or True)
    monkeypatch.setattr(receive_flow, "jittered_sleep", lambda *_a, **_k: None)
    received = receive_flow.try_receive_once(
        get_purchases=lambda: [dict(row) for row in purchases],
        update_purchase=lambda *_a, **_k: False,
        get_buff_client=lambda: object(),
        get_steam_credentials=lambda: {"cookies": "steamLoginSecure=x; sessionid=y"},
        scan_inventory=scan_inventory,
        update_purchase_by_id=lambda _db_id, data: _apply(purchases, _db_id, data),
    )
    return received, accepted


def _apply(purchases, db_id, data):
    for row in purchases:
        if row["_db_id"] == db_id:
            row.update(data)
            return True
    return False


def test_receive_flow_refuses_partial_offer_with_distinct_cost_basis(monkeypatch):
    purchases = [
        {"_db_id": 1, "goods_id": 42, "name": ITEM, "price": 10.0, "at": 1.0, "pending_receipt": True},
        {"_db_id": 2, "goods_id": 42, "name": ITEM, "price": 12.0, "at": 2.0, "pending_receipt": True},
    ]
    task = {"tradeofferid": "offer-1", "created_at": 100,
            "items": [{"assetid": "s1", "goods_id": 42, "market_hash_name": ITEM}]}
    scans = []

    received, accepted = _receive(
        monkeypatch, purchases, task,
        lambda: scans.append(True) or (True, [], ""),
    )

    assert received == 0 and accepted == [] and scans == []
    assert all(not row.get("assetid") and row["pending_receipt"] for row in purchases)


def test_receive_flow_leaves_pending_when_extra_copy_appears(monkeypatch):
    purchases = [
        {"_db_id": 1, "goods_id": 42, "name": ITEM, "price": 10.0, "at": 1.0, "pending_receipt": True},
    ]
    task = {"tradeofferid": "offer-1", "created_at": 100,
            "items": [{"assetid": "s1", "goods_id": 42, "market_hash_name": ITEM}]}
    accepted = {"value": False}

    def accept(*_a, **_k):
        accepted["value"] = True
        return True

    def scan():
        if not accepted["value"]:
            return True, [], ""
        return True, [
            {"assetid": "new-1", "market_hash_name": ITEM},
            {"assetid": "new-2", "market_hash_name": ITEM},
        ], ""

    from app import receive_flow
    monkeypatch.setattr(receive_flow, "fetch_buff_steam_trade", lambda _c: (True, [task], ""))
    monkeypatch.setattr(receive_flow, "accept_steam_trade_offer", accept)
    monkeypatch.setattr(receive_flow, "jittered_sleep", lambda *_a, **_k: None)
    received = receive_flow.try_receive_once(
        get_purchases=lambda: [dict(row) for row in purchases],
        update_purchase=lambda *_a, **_k: False,
        get_buff_client=lambda: object(),
        get_steam_credentials=lambda: {"cookies": "steamLoginSecure=x; sessionid=y"},
        scan_inventory=scan,
        update_purchase_by_id=lambda _db_id, data: _apply(purchases, _db_id, data),
    )

    assert received == 0
    assert purchases[0]["pending_receipt"] is True and not purchases[0].get("assetid")


# --------------------------------------------------------------------------
# sell_pipeline: "already have a listing" must not duplicate a Sale row
# --------------------------------------------------------------------------

class _SellState:
    def __init__(self, sales, purchases=None):
        self._sales = sales
        self._purchases = purchases or []
        self.updates = []

    def get_sales(self):
        return list(self._sales)

    def get_purchases(self):
        return list(self._purchases)

    def update_purchase_by_id(self, db_id, data):
        self.updates.append((db_id, data))
        return True

    def is_stop_requested(self):
        return False

    def log(self, *_args, **_kwargs):
        pass

    def set_status(self, *_args, **_kwargs):
        pass


def test_record_listing_success_skips_duplicate_sale(monkeypatch):
    from app import sell_pipeline

    appended = []
    monkeypatch.setattr(sell_pipeline, "append_sale", lambda sale: appended.append(sale))
    monkeypatch.setattr(sell_pipeline, "jittered_sleep", lambda *_a: None)
    state = _SellState([{"assetid": "asset-1"}], [{"_db_id": 1, "assetid": "asset-1"}])
    ctx = SimpleNamespace(state=state)

    sell_pipeline._record_listing_success(ctx, "asset-1", "Item", 10.0, 0)
    sell_pipeline._record_listing_success(ctx, "asset-2", "Item", 10.0, 0, True)
    assert appended == []
    sell_pipeline._record_listing_success(ctx, "asset-3", "Item", 10.0, 0)
    assert [sale["assetid"] for sale in appended] == ["asset-3"]


def test_submit_listing_already_have_listing_does_not_duplicate_sale(monkeypatch):
    from app import sell_pipeline
    from app.pipeline_context import PipelineContext
    from steam.request_policy import MarketCooldown

    appended = []
    monkeypatch.setattr(sell_pipeline, "append_sale", lambda sale: appended.append(sale))
    monkeypatch.setattr(sell_pipeline, "_listing_cooldown", MarketCooldown())
    monkeypatch.setattr(sell_pipeline, "jittered_sleep", lambda *_a: None)
    monkeypatch.setattr(sell_pipeline, "list_item", lambda *_a, **_k: {
        "status_code": 200, "text": '{"success": false, "message": "already have a listing"}'})
    ctx = PipelineContext(_SellState([{"assetid": "asset-1"}],
                                     [{"_db_id": 1, "assetid": "asset-1"}]), "test")
    entry = {"it": {"appid": 730, "contextid": "2", "assetid": "asset-1"},
             "list_price": 10.0, "reason": "t", "price_cents": 1000,
             "name": "Item", "aid": "asset-1"}

    assert sell_pipeline._submit_listings(ctx, [entry], object(), "sid", 0) == 1
    assert appended == []
