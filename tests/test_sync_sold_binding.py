"""Asset binding rules for sync_sold._fill_assetid_from_inventory."""


def _fill(purchases, inv_items):
    from app.sync_sold import _fill_assetid_from_inventory
    return _fill_assetid_from_inventory(purchases, inv_items)


def test_pending_receipt_rows_are_never_bound_by_name():
    purchases = [{"name": "A", "pending_receipt": True}, {"name": "B", "pending_receipt": False}]
    inv = [{"assetid": "1", "market_hash_name": "A"}, {"assetid": "2", "market_hash_name": "B"}]
    assert _fill(purchases, inv) == 1
    assert purchases[0].get("assetid") is None
    assert purchases[1]["assetid"] == "2"


def test_ambiguous_same_name_candidates_are_skipped():
    purchases = [{"name": "A", "pending_receipt": False}]
    inv = [{"assetid": "1", "market_hash_name": "A"}, {"assetid": "2", "market_hash_name": "A"}]
    assert _fill(purchases, inv) == 0
    assert purchases[0].get("assetid") is None


def test_unique_candidate_binds_and_clears_listing_state():
    purchases = [{"name": "A", "pending_receipt": False, "listing": True, "listing_status": "error"}]
    inv = [{"assetid": "7", "market_hash_name": "A"}]
    assert _fill(purchases, inv) == 1
    assert purchases[0]["assetid"] == "7"
    assert purchases[0]["listing"] is False
    assert purchases[0]["listing_status"] is None


def test_used_assetids_are_not_rebound():
    purchases = [{"name": "A", "assetid": "9"}, {"name": "A", "pending_receipt": False}]
    inv = [{"assetid": "9", "market_hash_name": "A"}]
    assert _fill(purchases, inv) == 0
    assert purchases[1].get("assetid") is None


def test_name_match_via_plain_name_field():
    purchases = [{"name": "A", "pending_receipt": False}]
    inv = [{"assetid": "3", "name": "A"}]
    assert _fill(purchases, inv) == 1
    assert purchases[0]["assetid"] == "3"
