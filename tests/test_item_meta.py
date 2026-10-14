import json

import pytest

from app import inventory_cs2
from app.services import item_meta


@pytest.fixture
def meta_cache(monkeypatch, tmp_path):
    path = tmp_path / "item_meta_cache.json"
    monkeypatch.setattr(item_meta, "_PATH", path)
    return path


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    from app import database

    old_engine = database._engine
    if old_engine is not None:
        old_engine.dispose()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(database, "_engine", None)
    database.init_db()
    yield database
    database.get_engine().dispose()


def test_remember_and_lookup_item_meta(meta_cache):
    item_meta.remember_items([
        {"market_hash_name": "AK-47 | Redline (Field-Tested)",
         "icon_url": "-abc", "item_type": "步枪", "exterior": "久经沙场"},
        {"market_hash_name": "no metadata item"},
    ])
    stored = json.loads(meta_cache.read_text(encoding="utf-8"))
    assert stored == {"AK-47 | Redline (Field-Tested)": {
        "icon_url": "-abc", "item_type": "步枪", "exterior": "久经沙场"}}
    assert item_meta.get_item_meta({"AK-47 | Redline (Field-Tested)", "missing"}) == {
        "AK-47 | Redline (Field-Tested)": {
            "icon_url": "-abc", "item_type": "步枪", "exterior": "久经沙场"}}


def test_corrupt_cache_is_ignored_and_replaced(meta_cache):
    meta_cache.write_text("corrupt")
    assert item_meta.get_item_meta({"item"}) == {}
    item_meta.remember_items([{"market_hash_name": "item", "icon_url": "-x"}])
    assert json.loads(meta_cache.read_text(encoding="utf-8"))["item"]["icon_url"] == "-x"


def test_partial_updates_preserve_other_names(meta_cache):
    item_meta.remember_items([{"market_hash_name": "first", "icon_url": "-1"}])
    item_meta.remember_items([{"market_hash_name": "second", "item_type": "步枪"}])
    stored = json.loads(meta_cache.read_text(encoding="utf-8"))
    assert stored["first"]["icon_url"] == "-1"
    assert stored["second"]["item_type"] == "步枪"


def _desc(**overrides):
    desc = {
        "icon_url": "-icon",
        "type": "Classified Rifle",
        "tags": [
            {"category": "Type", "internal_name": "CSGO_Type_Rifle", "localized_name": "Rifle"},
            {"category": "Exterior", "internal_name": "tag_WearCategory2", "localized_name": "Field-Tested"},
        ],
    }
    desc.update(overrides)
    return desc


def test_display_meta_prefers_steam_tags():
    meta = inventory_cs2._item_display_meta(_desc(), "AK-47 | Redline (Field-Tested)", "0.2")
    assert meta == {"icon_url": "-icon", "item_type": "步枪", "exterior": "久经沙场"}


def test_display_meta_falls_back_to_name_suffix_then_float():
    meta = inventory_cs2._item_display_meta({"icon_url": "-icon"}, "AK-47 | Redline (Well-Worn)", None)
    assert meta["exterior"] == "破损不堪"
    assert meta["item_type"] == ""
    meta = inventory_cs2._item_display_meta({}, "★ Bayonet", "0.5")
    assert meta["exterior"] == "战痕累累"
    assert meta["icon_url"] == ""
    meta = inventory_cs2._item_display_meta({}, "Sticker | Titan (Holo)", None)
    assert meta["exterior"] == ""


def test_scan_extracts_display_metadata(monkeypatch):
    data = {
        "assets": [
            {"assetid": "1", "appid": 730, "contextid": "2", "classid": "c1", "instanceid": "0",
             "asset_properties": [
                 {"propertyid": "1", "int_value": 321},
                 {"propertyid": "2", "float_value": 0.234},
             ]},
        ],
        "descriptions": [
            {"classid": "c1", "instanceid": "0", "name": "AK-47 | Redline",
             "market_hash_name": "AK-47 | Redline (Field-Tested)",
             "marketable": 1, "tradable": 1, "icon_url": "-icon",
             "tags": [
                 {"category": "Type", "internal_name": "CSGO_Type_Rifle", "localized_name": "Rifle"},
                 {"category": "Exterior", "internal_name": "tag_WearCategory2", "localized_name": "Field-Tested"},
             ]},
        ],
    }
    monkeypatch.setattr(inventory_cs2, "get_steam", lambda: {"steam_id": "1", "cookies": "x"})
    monkeypatch.setattr(inventory_cs2, "create_market_session", lambda *args, **kwargs: object())
    monkeypatch.setattr(inventory_cs2, "fetch_cs2_inventory", lambda *args, **kwargs: data)
    persisted = []
    monkeypatch.setattr(inventory_cs2, "_persist_item_meta", persisted.append)

    ok, items, err = inventory_cs2.scan_cs2_inventory()

    assert ok and not err
    [item] = items
    assert item["icon_url"] == "-icon"
    assert item["item_type"] == "步枪"
    assert item["exterior"] == "久经沙场"
    assert item["paintseed"] == 321
    assert item["paintwear"] == "0.234"
    assert persisted == [items]


def test_enrich_purchases_fills_asset_and_name_matches(isolated_db):
    database = isolated_db
    database.db_append_purchase({"name": "AK-47 | Redline (Field-Tested)",
                                 "assetid": "asset-1", "price": 1.0, "at": 1.0})
    database.db_append_purchase({"name": "AK-47 | Redline (Field-Tested)",
                                 "price": 2.0, "at": 2.0, "pending_receipt": True})
    database.db_append_purchase({"name": "Other item", "price": 3.0, "at": 3.0})
    items = [{"assetid": "asset-1", "market_hash_name": "AK-47 | Redline (Field-Tested)",
              "icon_url": "-icon", "item_type": "步枪", "exterior": "久经沙场",
              "paintwear": "0.234"}]

    item_meta.enrich_purchases_from_inventory(items)

    owned, pending, other = database.db_get_purchases()
    assert (owned["icon_url"], owned["item_type"], owned["exterior"], owned["paintwear"]) == (
        "-icon", "步枪", "久经沙场", "0.234")
    assert (pending["icon_url"], pending["item_type"], pending["exterior"]) == (
        "-icon", "步枪", "久经沙场")
    assert "paintwear" not in pending
    assert "icon_url" not in other

    item_meta.enrich_purchases_from_inventory(items)
    owned, pending, other = database.db_get_purchases()
    assert owned["paintwear"] == "0.234"
    assert "paintwear" not in pending


def test_enrich_transaction_rows_uses_inventory_and_cache(monkeypatch, meta_cache):
    from app import state

    monkeypatch.setattr(state, "get_inventory", lambda: [
        {"assetid": "asset-1", "market_hash_name": "Held item", "name": "Held item",
         "icon_url": "-held", "item_type": "手枪", "exterior": "崭新出厂",
         "paintwear": "0.01"},
    ])
    item_meta.remember_items([{"market_hash_name": "Sold item", "icon_url": "-sold",
                               "item_type": "步枪", "exterior": "久经沙场"}])
    rows = [
        {"type": "purchase", "name": "Held item", "assetid": "asset-1"},
        {"type": "purchase", "name": "Sold item", "assetid": ""},
        {"type": "sale", "name": "Held item", "assetid": "asset-1"},
    ]

    item_meta.enrich_transaction_rows(rows)

    assert (rows[0]["icon_url"], rows[0]["item_type"], rows[0]["exterior"], rows[0]["paintwear"]) == (
        "-held", "手枪", "崭新出厂", "0.01")
    assert (rows[1]["icon_url"], rows[1]["item_type"], rows[1]["exterior"]) == (
        "-sold", "步枪", "久经沙场")
    assert "paintwear" not in rows[1]
    assert "icon_url" not in rows[2]


def test_sticker_type_tag_and_field_fallback():
    desc = {"icon_url": "-icon", "type": "High Grade Sticker",
            "tags": [{"category": "Type", "internal_name": "CSGO_Tool_Sticker",
                      "localized_name": None}]}
    assert inventory_cs2._item_display_meta(desc, "Sticker | Water Gun", None)["item_type"] == "印花"
    assert inventory_cs2._item_display_meta({"type": "Base Grade Container"}, "Case", None)["item_type"] == "武器箱"
    assert inventory_cs2._item_display_meta({"type": "Classified Rifle"}, "AK", None)["item_type"] == "Classified Rifle"

