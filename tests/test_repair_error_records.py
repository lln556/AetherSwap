import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _prepare_repair(monkeypatch, tmp_path, purchases, *, inventory=None, sold=None, sold_names=None, listings=None, listing_names=None):
    from app import repair_error_records
    from app import inventory_cs2
    from app import steam_listings
    from app import state

    credentials_file = tmp_path / "credentials.json"
    credentials_file.write_text(
        json.dumps({"steam": {"cookies": "steamLoginSecure=x; sessionid=y"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(repair_error_records, "CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr(state, "get_purchases", lambda: copy.deepcopy(purchases))
    monkeypatch.setattr(state, "get_sales", lambda: [])
    monkeypatch.setattr(
        inventory_cs2,
        "scan_cs2_inventory",
        lambda: (True, copy.deepcopy(inventory or []), ""),
    )
    monkeypatch.setattr(
        repair_error_records,
        "_fetch_sold_with_names",
        lambda cookies: (copy.deepcopy(sold or {}), copy.deepcopy(sold_names or {})),
    )
    monkeypatch.setattr(
        steam_listings,
        "fetch_my_listings",
        lambda cookies, debug_fn=None: (
            True,
            set(listings or []),
            "",
            copy.deepcopy(listing_names or {}),
        ),
    )
    replaced = {}
    monkeypatch.setattr(
        state,
        "replace_transactions",
        lambda new_purchases, new_sales: replaced.setdefault(
            "purchases",
            copy.deepcopy(new_purchases),
        ),
    )
    return repair_error_records, replaced


def test_rebuild_preserves_sold_record_when_history_misses_it_even_if_inventory_has_same_name(monkeypatch, tmp_path):
    purchases = [
        {
            "name": "AK-47 | Redline",
            "assetid": "sold-aid",
            "sale_price": 12.34,
            "sold_at": 1000,
            "listing": False,
        },
        {
            "name": "USP-S | Flashback",
            "assetid": "stale-aid",
            "listing": True,
        },
    ]
    repair, replaced = _prepare_repair(
        monkeypatch,
        tmp_path,
        purchases,
        inventory=[
            {"assetid": "new-inventory-aid", "market_hash_name": "AK-47 | Redline"},
            {"assetid": "fresh-aid", "market_hash_name": "USP-S | Flashback"},
        ],
    )

    ok, result = repair.run()

    assert ok is True
    assert result["total"] == 2
    assert result["filled"] == 1
    assert result["preserved_sold"] == 1
    fixed = replaced["purchases"]
    assert fixed[0]["assetid"] == "sold-aid"
    assert fixed[0]["sale_price"] == 12.34
    assert fixed[0]["sold_at"] == 1000
    assert fixed[1]["assetid"] == "fresh-aid"
    assert fixed[1]["listing"] is False
    assert fixed[1]["listing_status"] is None
    assert fixed[1].get("sale_price") is None


def test_rebuild_marks_original_assetid_sold_when_assetid_is_in_sold_history(monkeypatch, tmp_path):
    purchases = [
        {
            "name": "Glock-18 | Umbral Rabbit",
            "assetid": "sold-aid",
            "listing": False,
            "listing_status": "error",
        }
    ]
    repair, replaced = _prepare_repair(
        monkeypatch,
        tmp_path,
        purchases,
        sold={"sold-aid": 7.89},
        sold_names={"sold-aid": "Glock-18 | Umbral Rabbit"},
    )

    ok, result = repair.run()

    assert ok is True
    assert result["filled"] == 1
    fixed = replaced["purchases"][0]
    assert fixed["assetid"] == "sold-aid"
    assert fixed["sale_price"] == 7.89
    assert fixed["sold_at"] is not None
    assert fixed["listing"] is False
    assert fixed["listing_status"] is None


def test_rebuild_uses_exact_name_matching_for_missing_assetid(monkeypatch, tmp_path):
    purchases = [
        {
            "name": "AK-47 | Redline (Minimal Wear)",
            "listing_status": "error",
        }
    ]
    repair, replaced = _prepare_repair(
        monkeypatch,
        tmp_path,
        purchases,
        inventory=[{"assetid": "wrong-aid", "market_hash_name": "AK-47 | Redline"}],
    )

    ok, result = repair.run()

    assert ok is True
    assert result["filled"] == 0
    assert result["missing"] == 1
    fixed = replaced["purchases"][0]
    assert fixed.get("assetid") is None
    assert fixed["listing_status"] == "error"


def test_rebuild_clears_stale_unsold_assetid_when_not_seen_anywhere(monkeypatch, tmp_path):
    purchases = [
        {
            "name": "M4A1-S | Guardian",
            "assetid": "ghost-aid",
            "listing": True,
            "listing_status": None,
            "sale_price": None,
        }
    ]
    repair, replaced = _prepare_repair(monkeypatch, tmp_path, purchases)

    ok, result = repair.run()

    assert ok is True
    assert result["filled"] == 0
    assert result["missing"] == 1
    fixed = replaced["purchases"][0]
    assert fixed["assetid"] is None
    assert fixed["listing"] is False
    assert fixed["listing_status"] == "error"
    assert fixed["sale_price"] is None


def test_rebuild_keeps_pending_receipt_when_not_seen_anywhere(monkeypatch, tmp_path):
    purchases = [
        {
            "name": "Desert Eagle | Trigger Discipline",
            "assetid": "pending-aid",
            "pending_receipt": True,
            "listing": False,
        }
    ]
    repair, replaced = _prepare_repair(monkeypatch, tmp_path, purchases)

    ok, result = repair.run()

    assert ok is True
    assert result["filled"] == 0
    assert result["missing"] == 1
    assert "purchases" not in replaced


def test_fetch_sold_with_names_paginates_history(monkeypatch):
    from app import repair_error_records

    calls = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    def row(row_id, name, price):
        return f"""
        <div class="market_listing_row" id="{row_id}">
          <span class="market_listing_item_name">{name}</span>
          <div class="market_listing_listed_date_combined">Sold</div>
          <span class="market_listing_price">¥ {price}</span>
        </div>
        """

    pages = [
        {
            "success": True,
            "total_count": 501,
            "results_html": row("history_row_1_0", "AK-47 | Redline", "10.00"),
            "hovers": "CreateItemHoverFromContainer( g_rgAssets, 'history_row_1_0_name', 730, '2', '1001' );",
        },
        {
            "success": True,
            "total_count": 501,
            "results_html": row("history_row_2_0", "USP-S | Flashback", "20.00"),
            "hovers": "CreateItemHoverFromContainer( g_rgAssets, 'history_row_2_0_name', 730, '2', '1002' );",
        },
    ]

    def fake_get(url, params=None, **kwargs):
        calls.append(params["start"])
        return FakeResponse(pages[len(calls) - 1])

    monkeypatch.setattr(repair_error_records.requests, "get", fake_get)
    monkeypatch.setattr(repair_error_records, "_load_rate_map", lambda: {})
    monkeypatch.setattr(repair_error_records, "HISTORY_PAGE_SIZE", 500)
    monkeypatch.setattr(repair_error_records, "HISTORY_MAX_PAGES", 5)

    sold, names = repair_error_records._fetch_sold_with_names({"steamLoginSecure": "x"})

    assert calls == [0, 500]
    assert sold == {"1001": 11.5, "1002": 23.0}
    assert names == {"1001": "AK-47 | Redline", "1002": "USP-S | Flashback"}
