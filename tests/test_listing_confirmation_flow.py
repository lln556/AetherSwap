import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import httpx
from utils import http_client as http


def response(data, status=200):
    return SimpleNamespace(status_code=status, json=lambda: data, text=json.dumps(data))


def market_row(aid, lid):
    return {"listingid": lid, "asset": {"id": aid, "appid": 730, "contextid": "2"}}


def test_market_snapshot_separates_pending_from_active_and_ignores_metadata(monkeypatch):
    from app import steam_listings

    get = Mock(return_value=response({
        "success": True,
        "listings": [market_row("active", "listing-a")],
        "listings_to_confirm": {"pending": market_row("pending", "listing-b")},
        "assets": {"730": {"2": {
            "active": {"market_hash_name": "Owned item"},
            "personal": {"market_hash_name": "Not listed"},
        }}},
        "num_active_listings": 1,
    }))
    monkeypatch.setattr(steam_listings, "_get_with_retry", get)

    ok, snapshot, err = steam_listings.fetch_listing_snapshot("steamLoginSecure=token")

    assert ok and not err
    assert set(snapshot) == {"active", "pending"}
    assert snapshot["active"] == {"listing_id": "listing-a", "status": "active", "name": "Owned item"}
    assert snapshot["pending"]["status"] == "pending_confirmation"


def test_market_snapshot_reads_every_page_before_inferencing_missing_items(monkeypatch):
    from app import steam_listings

    get = Mock(side_effect=[response({
        "success": True, "num_active_listings": 101,
        "listings": [market_row(str(i), f"listing-{i}") for i in range(100)],
    }), response({
        "success": True, "num_active_listings": 101,
        "listings": [market_row("100", "listing-100")],
    })])
    monkeypatch.setattr(steam_listings, "_get_with_retry", get)

    ok, snapshot, _ = steam_listings.fetch_listing_snapshot("steamLoginSecure=token")

    assert ok and len(snapshot) == 101
    assert get.call_args_list[1].args[1]["start"] == 100


@pytest.mark.parametrize("body", [
    {"success": True, "assets": {}},
    {"success": True, "listings": [market_row("", "listing")]},
    {"success": True, "num_active_listings": 10, "listings": []},
    {"success": False, "listings": []},
])
def test_incomplete_market_snapshot_cannot_claim_items_are_absent(monkeypatch, body):
    from app import steam_listings

    monkeypatch.setattr(steam_listings, "_get_with_retry", lambda *args: response(body))
    ok, snapshot, err = steam_listings.fetch_listing_snapshot("steamLoginSecure=token")
    assert not ok and snapshot == {} and err


def test_empty_market_is_a_valid_snapshot(monkeypatch):
    from app import steam_listings

    monkeypatch.setattr(steam_listings, "_get_with_retry", lambda *args: response({"success": True, "listings": []}))
    assert steam_listings.fetch_listing_snapshot("steamLoginSecure=token") == (True, {}, "")


def test_repeated_last_page_is_not_mistaken_for_complete_market(monkeypatch):
    from app import steam_listings

    page = {"success": True, "num_active_listings": 200,
            "listings": [market_row(str(i), f"listing-{i}") for i in range(100)]}
    monkeypatch.setattr(steam_listings, "_get_with_retry", Mock(side_effect=[response(page), response(page)]))
    ok, snapshot, _ = steam_listings.fetch_listing_snapshot("steamLoginSecure=token")
    assert not ok and snapshot == {}


def test_auto_confirmation_generates_device_and_accepts_only_selected_market_listing(monkeypatch):
    from app import steam_confirm
    from steampy.guard import generate_device_id

    session = Mock()
    session.get.return_value = response({"success": True, "conf": [
        {"id": "owned", "nonce": "owned-nonce", "type": 3, "creator_id": "owned-listing"},
        {"id": "personal", "nonce": "personal-nonce", "type": 3, "creator_id": "personal-listing"},
        {"id": "trade", "nonce": "trade-nonce", "type": 2, "creator_id": "owned-listing"},
    ]})
    session.post.return_value = response({"success": True})
    monkeypatch.setattr(steam_confirm.http, "Client", lambda: session)

    assert steam_confirm.auto_confirm_once("a2V5", "", "76561198000000000", {}, listing_ids={"owned-listing"}) == (True, 1, "")
    assert session.get.call_args.kwargs["params"]["p"] == generate_device_id("76561198000000000")
    params = session.post.call_args.kwargs["params"]
    assert params["tag"] == "allow" and params["m"] == "android" and params["op"] == "allow"
    assert session.post.call_args.kwargs["files"] == [
        ("cid[]", (None, "owned")), ("ck[]", (None, "owned-nonce")),
    ]


def test_confirmation_without_target_ids_never_fetches_or_accepts_anything(monkeypatch):
    from app import steam_confirm

    session = Mock(side_effect=AssertionError("must not access confirmation API"))
    monkeypatch.setattr(steam_confirm.http, "Client", session)
    assert steam_confirm.auto_confirm_once("a2V5", "", "76561198000000000", {}) == (True, 0, "")
    session.assert_not_called()


def test_confirmation_error_does_not_expose_signed_url(monkeypatch):
    from app import steam_confirm

    session = Mock()
    session.get.side_effect = httpx.ConnectError("https://steamcommunity.com/mobileconf/getlist?k=secret-signature")
    monkeypatch.setattr(steam_confirm.http, "Client", lambda: session)
    ok, _, err = steam_confirm.SteamConfirmer("a2V5", "", "76561198000000000", {}).get_confirmations()
    assert not ok and "ConnectError" in err and "secret-signature" not in err


class MemoryState:
    def __init__(self, purchases):
        self.purchases = {p["_db_id"]: dict(p) for p in purchases}

    def get_purchases(self):
        return [dict(p) for p in self.purchases.values()]

    def update_purchase_by_id(self, db_id, data):
        self.purchases[db_id].update(data)
        return True


@pytest.fixture
def confirmation_flow(monkeypatch):
    from app.services import listing_confirmation as flow

    cred = {"steam_id": "76561198000000000", "cookies": "steamLoginSecure=token"}
    monkeypatch.setattr(flow, "get_steam_credentials", lambda: dict(cred))
    state = MemoryState([
        {"_db_id": 1, "assetid": "owned", "listing": True, "listing_status": "submitted"},
        {"_db_id": 2, "assetid": "sold", "listing": False, "sale_price": 10},
    ])
    ctx = SimpleNamespace(state=state, log=Mock())
    def record_snapshot(rows, account, *, purchase_ids):
        for p in state.get_purchases():
            row = rows.get(p.get("assetid"))
            if row and p["_db_id"] in purchase_ids and p.get("listing"):
                state.update_purchase_by_id(p["_db_id"], {"listing_status": row["status"]})
    monkeypatch.setattr(flow, "record_snapshot", record_snapshot)
    pending = {"owned": {"listing_id": "owned-listing", "status": "pending_confirmation", "name": "Owned"},
               "personal": {"listing_id": "personal-listing", "status": "pending_confirmation", "name": "Personal"}}
    active = {"owned": {"listing_id": "owned-listing", "status": "active", "name": "Owned"}}
    fetch = Mock(side_effect=[(True, pending, ""), (True, active, "")])
    confirm = Mock(return_value=(True, 1, ""))
    monkeypatch.setattr(flow, "fetch_listing_snapshot", fetch)
    monkeypatch.setattr(flow, "auto_confirm_once", confirm)
    cfg = {"steam_confirm": {"enabled": True, "identity_secret": "a2V5", "device_id": ""}}
    return SimpleNamespace(flow=flow, cred=cred, state=state, ctx=ctx, pending=pending,
                           active=active, fetch=fetch, confirm=confirm, cfg=cfg)


def test_confirmation_flow_verifies_active_state_and_only_confirms_owned_rows(confirmation_flow):
    f = confirmation_flow
    ok, snapshot, _ = f.flow.sync_listing_confirmations(f.ctx, f.cfg, f.cred, assetids=["owned"])
    assert ok and snapshot == f.active
    assert f.confirm.call_args.kwargs["listing_ids"] == {"owned-listing"}
    assert f.confirm.call_args.args[1] == ""  # missing device ID is allowed
    assert f.state.purchases[1]["listing_status"] == "active"
    assert f.state.purchases[2]["listing"] is False


def test_existing_pending_listing_is_retried_without_new_submission(confirmation_flow):
    f = confirmation_flow
    f.state.purchases[1]["listing_status"] = "pending_confirmation"
    ok, _, _ = f.flow.sync_listing_confirmations(f.ctx, f.cfg, f.cred)
    assert ok and f.confirm.call_count == 1
    assert f.state.purchases[1]["listing_status"] == "active"


@pytest.mark.parametrize("confirmation_result", [(False, 0, "temporary failure"), (True, 0, "")])
def test_confirmation_failure_or_missing_mobile_entry_keeps_pending(confirmation_flow, confirmation_result):
    f = confirmation_flow
    f.confirm.return_value = confirmation_result
    f.flow.sync_listing_confirmations(f.ctx, f.cfg, f.cred)
    assert f.state.purchases[1]["listing_status"] == "pending_confirmation"
    assert f.state.purchases[1]["listing"] is True


def test_accepted_confirmation_does_not_prematurely_claim_active(confirmation_flow):
    f = confirmation_flow
    f.fetch.side_effect = [(True, f.pending, ""), (False, {}, "HTTP 429")]
    f.flow.sync_listing_confirmations(f.ctx, f.cfg, f.cred)
    assert f.state.purchases[1]["listing_status"] == "pending_confirmation"


def test_disabled_auto_confirmation_still_displays_pending_state(confirmation_flow):
    f = confirmation_flow
    f.cfg["steam_confirm"]["enabled"] = False
    f.flow.sync_listing_confirmations(f.ctx, f.cfg, f.cred)
    f.confirm.assert_not_called()
    assert f.state.purchases[1]["listing_status"] == "pending_confirmation"


def test_account_change_aborts_confirmation_before_any_request(confirmation_flow, monkeypatch):
    f = confirmation_flow
    monkeypatch.setattr(f.flow, "get_steam_credentials", lambda: {"steam_id": "other-account", "cookies": "different"})
    ok, _, _ = f.flow.sync_listing_confirmations(f.ctx, f.cfg, f.cred)
    assert not ok
    f.confirm.assert_not_called()
    f.fetch.assert_not_called()


def test_concurrent_delist_is_not_reverted_by_confirmation_reconciliation(confirmation_flow):
    f = confirmation_flow

    def fetch(*args):
        f.state.purchases[1]["listing"] = False
        return True, f.active, ""

    f.fetch.side_effect = fetch
    f.flow.sync_listing_confirmations(f.ctx, f.cfg, f.cred)
    assert f.state.purchases[1]["listing"] is False
    assert f.state.purchases[1]["listing_status"] == "submitted"
    f.confirm.assert_not_called()


def test_concurrent_delist_of_pending_listing_is_not_confirmed(confirmation_flow):
    f = confirmation_flow

    def fetch(*args):
        f.state.purchases[1]["listing"] = False
        return True, f.pending, ""

    f.fetch.side_effect = fetch
    f.flow.sync_listing_confirmations(f.ctx, f.cfg, f.cred)
    f.confirm.assert_not_called()


@pytest.mark.parametrize("market_snapshot, expected_ok", [({}, True),
    ({"old": {"listing_id": "listing-1", "status": "active"}}, False)])
def test_delist_retains_same_asset_only_after_market_removal_is_verified(monkeypatch, market_snapshot, expected_ok):
    from app import steam_delist

    session = Mock()
    session.post.return_value = response({"success": True})
    monkeypatch.setattr(steam_delist, "post_market", lambda *a, **kw: response({"success": True}))
    monkeypatch.setattr(steam_delist.http, "Client", lambda: session)
    monkeypatch.setattr(steam_delist, "get_steam", lambda: {
        "cookies": "steamLoginSecure=token; sessionid=session", "steam_id": "steam-id",
    })
    monkeypatch.setattr(steam_delist, "_get_mylistings_api", lambda s: {
        "old": {"listingid": "listing-1", "classid": "class-1", "instanceid": "0"},
    })
    inventory = Mock(side_effect=[{"old"}, {"old", "unrelated-new"}, {"old", "unrelated-new"}, {"old", "unrelated-new"}])
    monkeypatch.setattr(steam_delist, "_get_assetids_by_class_instance", inventory)
    monkeypatch.setattr(steam_delist, "fetch_listing_snapshot", lambda *args: (True, market_snapshot, ""))
    monkeypatch.setattr(steam_delist, "jittered_sleep", lambda *args: None)

    ok, assetid, error = steam_delist.delist_item("old", "Owned item")

    assert ok is expected_ok
    assert assetid == ("old" if expected_ok else None)
    if not expected_ok:
        assert "勿立即重复下架" in error


def test_submission_is_not_recorded_as_verified_active(monkeypatch):
    from app import sell_pipeline

    state = MemoryState([{"_db_id": 1, "assetid": "owned", "listing": False}])
    monkeypatch.setattr(sell_pipeline, "append_sale", Mock())
    monkeypatch.setattr(sell_pipeline, "jittered_sleep", lambda *args: None)
    sell_pipeline._record_listing_success(SimpleNamespace(state=state), "owned", "Owned item", 23.06, 0)
    assert state.purchases[1]["listing"] is True
    assert state.purchases[1]["listing_status"] == "submitted"


def test_manual_confirmation_uses_stable_record_and_only_selected_asset(monkeypatch):
    from app.routes import transactions
    from app.services import listing_confirmation

    monkeypatch.setattr(transactions, "get_purchases", lambda: [
        {"_db_id": 10, "listing": True, "assetid": "other"},
        {"_db_id": 20, "listing": True, "assetid": "owned"},
    ])
    monkeypatch.setattr(transactions, "load_app_config_validated", lambda: {"steam_confirm": {"enabled": False}})
    monkeypatch.setattr(transactions, "get_steam_credentials", lambda: {})
    sync = Mock(return_value=(True, {"owned": {"status": "active"}}, ""))
    monkeypatch.setattr(listing_confirmation, "sync_listing_confirmations", sync)

    result = transactions.api_confirm_purchase_listing(0, db_id=20)

    assert result["ok"] and result["status"] == "active"
    assert sync.call_args.kwargs["assetids"] == ["owned"]
    assert sync.call_args.args[1]["steam_confirm"]["enabled"] is True


def test_manual_confirmation_rejects_changed_record_without_calling_steam(monkeypatch):
    from app.routes import transactions
    from app.services import listing_confirmation

    monkeypatch.setattr(transactions, "get_purchases", lambda: [{"_db_id": 20, "listing": False, "assetid": "owned"}])
    sync = Mock()
    monkeypatch.setattr(listing_confirmation, "sync_listing_confirmations", sync)
    assert transactions.api_confirm_purchase_listing(0, db_id=20)["ok"] is False
    sync.assert_not_called()


@pytest.mark.parametrize("new_listing_during_check", [False, True])
def test_worker_retries_owned_confirmations_before_checking_for_sales(monkeypatch, new_listing_during_check):
    from app.services import workers, listing_confirmation
    from app import steam_listings

    class StopWorker(BaseException):
        pass

    sleeps = iter([None])

    def sleep(*args):
        try:
            next(sleeps)
        except StopIteration:
            raise StopWorker()

    monkeypatch.setattr(workers, "wait_for_shutdown", sleep)
    monkeypatch.setattr(workers, "load_app_config_validated", lambda: {})
    monkeypatch.setattr(workers, "is_steam_background_allowed", lambda: True)
    initial = [
        {"_db_id": 1, "assetid": "owned", "listing": True, "listing_status": "pending_confirmation"},
    ]
    refreshed = initial + ([{"_db_id": 2, "assetid": "just-submitted", "listing": True}] if new_listing_during_check else [])
    monkeypatch.setattr(workers, "get_purchases", Mock(side_effect=[initial, refreshed]))
    monkeypatch.setattr(workers, "get_steam_credentials", lambda: {"steam_id": "account", "cookies": "cookie"})
    sync = Mock(return_value=(True, {"owned": {"status": "pending_confirmation"}}, ""))
    history = Mock()
    monkeypatch.setattr(listing_confirmation, "sync_listing_confirmations", sync)
    monkeypatch.setattr(steam_listings, "fetch_my_history_sold", history)

    with pytest.raises(StopWorker):
        workers.listing_check_worker()

    sync.assert_called_once()
    history.assert_not_called()
