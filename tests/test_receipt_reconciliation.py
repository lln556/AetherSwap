import copy
import pytest
from app.services import receipt_reconciliation as receipt

NAME = "R8 Revolver | Amber Fade (Field-Tested)"


def sample():
    purchase = {"_db_id": 1, "buff_order_id": "bill", "goods_id": 36327,
                "name": NAME, "price": 6.12, "pending_receipt": True}
    order = {"id": "bill", "goods_id": 36327, "price": "6.12", "game": "csgo",
             "buyer_steamid": "buyer", "buyer_pay_time": 100, "state": "SUCCESS",
             "_market_hash_name": NAME, "asset_info": {"assetid": "seller-old-id",
             "classid": "class", "paintwear": "0.20000000298023224",
             "info": {"paintseed": 653}}}
    item = {"assetid": "our-current-id", "appid": 730, "contextid": "16",
            "market_hash_name": NAME, "classid": "class", "paintseed": 653,
            "paintwear": "0.200000003", "can_sell": False}
    return purchase, order, item


def test_completed_order_recovers_exact_protected_asset_and_ignores_same_name_personal_item():
    p, o, i = sample()
    personal = dict(i, assetid="personal", paintseed=654)
    received, closed = receipt.build_reconciliation_plan([p], [o], [i, personal], "buyer")
    assert received == [{"purchase": p, "assetid": "our-current-id"}]
    assert closed == []


@pytest.mark.parametrize("field,value", [
    ("buyer_steamid", "other"), ("goods_id", 42), ("price", "6.13"),
    ("state", "TO_DELIVER"), ("buyer_pay_time", 0), ("buyer_pay_time", True),
    ("_market_hash_name", "Other Item"), ("game", "dota2"), ("asset_info", []),
])
def test_wrong_unpaid_uncompleted_or_malformed_order_cannot_authorize_an_asset(field, value):
    p, o, i = sample(); o[field] = value
    assert receipt.build_reconciliation_plan([p], [o], [i], "buyer") == ([], [])


@pytest.mark.parametrize("change", [
    {"paintwear": ".21"}, {"paintseed": 654}, {"classid": "wrong"},
    {"paintwear": "nan"}, {"paintseed": True}, {"paintwear": None}, {"appid": 570},
])
def test_name_alone_or_invalid_fingerprint_never_claims_a_personal_asset(change):
    p, o, i = sample(); i.update(change)
    assert receipt.build_reconciliation_plan([p], [o], [i], "buyer") == ([], [])


def test_duplicate_fingerprints_order_ids_or_asset_claims_are_ambiguous():
    p, o, i = sample()
    assert receipt.build_reconciliation_plan([p], [o], [i, dict(i, assetid="second")], "buyer") == ([], [])
    assert receipt.build_reconciliation_plan([p], [o, copy.deepcopy(o)], [i], "buyer") == ([], [])
    p2 = dict(p, _db_id=2, buff_order_id="bill2"); o2 = dict(o, id="bill2")
    assert receipt.build_reconciliation_plan([p, p2], [o, o2], [i], "buyer") == ([], [])


def test_already_owned_asset_is_not_assigned_again():
    p, o, i = sample()
    owner = {"_db_id": 2, "assetid": i["assetid"], "pending_receipt": False}
    assert receipt.build_reconciliation_plan([p, owner], [o], [i], "buyer") == ([], [])


def test_refunded_order_is_archived_without_assuming_receipt_or_inventory():
    p, o, _ = sample(); o.update(state="FAIL", state_text="购买失败-已退款", error_text="报价超时")
    received, closed = receipt.build_reconciliation_plan([p], [o], [], "buyer")
    assert received == [] and closed[0]["purchase"] == p


def test_database_compare_and_update_is_idempotent_and_rejects_stale_or_shared_assets(monkeypatch):
    from app import database as db
    from sqlmodel import SQLModel, create_engine
    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "_engine", engine); SQLModel.metadata.create_all(engine)
    p, _, _ = sample(); db.db_append_purchase(p); original = db.db_get_purchases()[0]
    assert db.db_apply_receipt_reconciliation(original, assetid="owned")
    assert not db.db_apply_receipt_reconciliation(original, assetid="owned")
    db.db_append_purchase(dict(p, buff_order_id="second")); second = db.db_get_purchases()[1]
    assert not db.db_apply_receipt_reconciliation(second, assetid="owned")
    db.db_update_purchase_by_id(second["_db_id"], {"price": 7})
    assert not db.db_apply_receipt_reconciliation(second, assetid="unused")
    latest = db.db_get_purchases()[1]
    assert db.db_apply_receipt_reconciliation(latest, closed=True)
    assert len(db.db_get_purchases()) == 1


def test_cooldown_refresh_runs_after_pending_receipts_are_empty(monkeypatch):
    from app.services import workers
    from app import receive_flow, sell_pipeline
    monkeypatch.setattr(workers, "is_steam_background_allowed", lambda: True)
    monkeypatch.setattr(workers, "_buff_background_request_is_safe", lambda: True)
    monkeypatch.setattr(receipt, "reconcile_receipts_once", lambda _: {
        "ok": True, "received": 0, "closed": 0, "inventory": None})
    monkeypatch.setattr(receive_flow, "try_receive_once", lambda *_a, **_k: 0)
    monkeypatch.setattr(workers, "get_purchases", lambda: [{"assetid": "owned"}])
    scans, sales = [], []
    def scan():
        scans.append(True); return True, [{"assetid": "owned", "can_sell": True}], ""
    monkeypatch.setattr(workers, "scan_cs2_inventory", scan)
    monkeypatch.setattr(workers, "set_inventory", lambda _: None)
    monkeypatch.setattr(sell_pipeline, "run_sell_phase_on_inventory_update", sales.append)
    assert workers.run_receive_check_once(object(), refresh_inventory=True)["inventory_refreshed"]
    assert len(scans) == len(sales) == 1
    assert not workers.run_receive_check_once(object())["inventory_refreshed"]
    assert len(scans) == len(sales) == 1


def test_raw_inventory_properties_stay_bound_to_the_exact_asset_and_context():
    from steam.inventory import _fetch_full_context
    class Response:
        status_code = 200
        def json(self):
            return {"success": 1, "assets": [{"appid": 730, "contextid": "16", "assetid": "a"}],
                    "descriptions": [], "asset_properties": [
                        {"appid": 730, "contextid": "2", "assetid": "a", "asset_properties": [{"propertyid": 1, "int_value": "wrong"}]},
                        {"appid": 730, "contextid": "16", "assetid": "a", "asset_properties": [{"propertyid": 1, "int_value": "653"}]},
                    ]}
    class Session:
        def get(self, *_a, **_k): return Response()
    assets, _ = _fetch_full_context(Session(), "buyer", 730, 16)
    assert assets[0]["asset_properties"] == [{"propertyid": 1, "int_value": "653"}]
