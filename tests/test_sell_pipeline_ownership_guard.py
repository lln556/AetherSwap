from unittest.mock import MagicMock, patch

from app.sell_pipeline import _build_listing_plan, _find_buy_record


ITEM_NAME = "AK-47 | Redline (Field-Tested)"


def _purchase(assetid="asset-owned", **overrides):
    record = {
        "_db_id": 1,
        "name": ITEM_NAME,
        "assetid": assetid,
        "price": 100.0,
        "pending_receipt": False,
        "listing": False,
    }
    record.update(overrides)
    return record


def _inventory_item(assetid="asset-inventory"):
    return {
        "name": ITEM_NAME,
        "market_hash_name": ITEM_NAME,
        "assetid": assetid,
        "can_sell": True,
        "appid": 730,
        "contextid": "2",
    }


def _ctx():
    ctx = MagicMock()
    ctx.is_stop_requested.return_value = False
    return ctx


def test_historical_sold_same_name_does_not_authorize_different_asset():
    historical = _purchase(
        assetid="asset-already-sold",
        sale_price=120.0,
        sold_at=1_700_000_000.0,
    )

    assert _find_buy_record([historical], "personal-drop", ITEM_NAME) is None


def test_unsold_same_name_does_not_authorize_different_asset():
    # Even an unsold same-name row is not proof that this particular inventory
    # asset came from AetherSwap.
    assert _find_buy_record([_purchase()], "personal-drop", ITEM_NAME) is None


def test_exact_asset_must_still_be_a_current_holding():
    assert _find_buy_record([_purchase(sale_price=120.0)], "asset-owned", ITEM_NAME) is None
    assert _find_buy_record([_purchase(sold_at=1_700_000_000.0)], "asset-owned", ITEM_NAME) is None
    assert _find_buy_record([_purchase(pending_receipt=True)], "asset-owned", ITEM_NAME) is None
    assert _find_buy_record([_purchase(listing=True)], "asset-owned", ITEM_NAME) is None
    assert _find_buy_record([_purchase(sale_price="invalid")], "asset-owned", ITEM_NAME) is None


def test_exact_current_asset_is_authorized():
    current = _purchase()

    assert _find_buy_record([current], " asset-owned ", ITEM_NAME) is current


def test_missing_inventory_assetid_fails_closed():
    assert _find_buy_record([_purchase()], "", ITEM_NAME) is None


def test_listing_plan_never_prices_or_lists_same_name_personal_item():
    ctx = _ctx()
    historical = _purchase(
        assetid="asset-already-sold",
        sale_price=120.0,
        sold_at=1_700_000_000.0,
    )

    with patch(
        "app.sell_pipeline.get_sell_orders_cny",
        side_effect=AssertionError("unsafe item reached Steam pricing"),
    ):
        result = _build_listing_plan(
            ctx=ctx,
            cfg={"pipeline": {}},
            session=MagicMock(),
            sellable=[_inventory_item("personal-drop")],
            sell_strategy=1,
            pipeline_cfg={},
            purchases_snapshot=[historical],
            ok_listings=True,
            active_listing_ids=set(),
            listing_assetid_to_name={},
            assetid_to_name_map={},
            account_currency="CNY",
            rate_map={},
        )

    assert result == []
