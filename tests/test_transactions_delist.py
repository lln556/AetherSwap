import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def no_repricing_job(monkeypatch):
    monkeypatch.setattr("app.services.repricing.item_has_job", lambda purchase_id: False)


def _listing(db_id: int = 7, assetid: str = "asset-old", **overrides):
    purchase = {
        "_db_id": db_id,
        "name": "Test Item",
        "price": 10.0,
        "at": 123.0,
        "assetid": assetid,
        "listing": True,
    }
    purchase.update(overrides)
    return purchase


def test_delist_failure_is_structured_instead_of_internal_server_error(monkeypatch):
    """Regression for the old inner import that shadowed get_purchases."""
    from app.routes import transactions
    from app import steam_delist

    monkeypatch.setattr(transactions, "get_purchases", lambda: [_listing()])
    monkeypatch.setattr(transactions, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        steam_delist,
        "delist_item",
        lambda *args, **kwargs: (False, None, "steam failed"),
    )

    result = transactions.api_delist_purchase(0)

    assert result == {"ok": False, "error": "steam failed"}


def test_delist_resolves_and_updates_by_stable_database_id(monkeypatch):
    from app.routes import transactions
    from app import steam_delist

    calls = []
    purchases = [
        _listing(db_id=11, assetid="wrong-asset"),
        _listing(db_id=22, assetid="target-asset", name="Target Item", at=456.0),
    ]
    monkeypatch.setattr(transactions, "get_purchases", lambda: purchases)
    monkeypatch.setattr(transactions, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        steam_delist,
        "delist_item",
        lambda assetid, name, log_fn=None: (
            calls.append(("delist", assetid, name)) or (True, "asset-new", None)
        ),
    )
    monkeypatch.setattr(
        transactions,
        "update_purchase_by_id",
        lambda db_id, data: calls.append(("update", db_id, data)) or True,
    )
    monkeypatch.setattr(
        transactions,
        "update_purchase",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("positional update must not be used")
        ),
    )

    result = transactions.api_delist_purchase(0, db_id=22)

    assert result == {
        "ok": True,
        "assetid": "asset-new",
        "record_updated": True,
    }
    assert calls == [
        ("delist", "target-asset", "Target Item"),
        (
            "update",
            22,
            {
                "assetid": "asset-new",
                "listing": False,
                "listing_status": None,
            },
        ),
    ]


def test_delist_rejects_stale_database_id_without_calling_steam(monkeypatch):
    from app.routes import transactions
    from app import steam_delist

    monkeypatch.setattr(transactions, "get_purchases", lambda: [_listing(db_id=7)])
    monkeypatch.setattr(
        steam_delist,
        "delist_item",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Steam must not be called for a stale record")
        ),
    )

    result = transactions.api_delist_purchase(0, db_id=999)

    assert result["ok"] is False
    assert "刷新" in result["error"]


def test_delist_converts_unexpected_steam_exception_to_business_error(monkeypatch):
    from app.routes import transactions
    from app import steam_delist

    monkeypatch.setattr(transactions, "get_purchases", lambda: [_listing()])
    monkeypatch.setattr(transactions, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        steam_delist,
        "delist_item",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("timeout")),
    )

    result = transactions.api_delist_purchase(0)

    assert result["ok"] is False
    assert "TimeoutError" in result["error"]


def test_delist_reports_remote_success_when_local_update_fails(monkeypatch):
    from app.routes import transactions
    from app import steam_delist

    monkeypatch.setattr(transactions, "get_purchases", lambda: [_listing()])
    monkeypatch.setattr(transactions, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        steam_delist,
        "delist_item",
        lambda *args, **kwargs: (True, "asset-new", None),
    )
    monkeypatch.setattr(
        transactions,
        "update_purchase_by_id",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("database is locked")
        ),
    )

    result = transactions.api_delist_purchase(0)

    assert result["ok"] is True
    assert result["record_updated"] is False
    assert "勿重复下架" in result["warning"]


@pytest.mark.parametrize(
    ("response_text", "error_fragment"),
    [
        (
            "<html><title>Sign In</title><form id='login_form'></form></html>",
            "Cookie",
        ),
        ("failure", "下架失败"),
    ],
)
def test_delist_rejects_obvious_failure_after_http_200(
    monkeypatch,
    response_text,
    error_fragment,
):
    from app import steam_delist

    class Response:
        status_code = 200
        headers = {}

        def __init__(self):
            self.text = response_text

        def json(self):
            raise ValueError("not json")

    class Session:
        def __init__(self):
            self.headers = {}
            self.cookies = {}
            self.verify = True

        def get(self, *args, **kwargs):
            if kwargs.get("params"):
                return type(
                    "ListingsResponse",
                    (),
                    {
                        "status_code": 200,
                        "json": lambda self: {
                            "success": True,
                            "listings": [
                                {
                                    "listingid": "listing-1",
                                    "asset": {
                                        "id": "asset-old",
                                        "appid": "730",
                                        "contextid": "2",
                                        "classid": "class-1",
                                        "instanceid": "0",
                                    },
                                }
                            ],
                        },
                    },
                )()
            raise AssertionError("unexpected GET")

    monkeypatch.setattr(
        steam_delist,
        "get_steam",
        lambda: {
            "cookies": "steamLoginSecure=token; sessionid=session",
            "steam_id": "steam-id",
        },
    )
    monkeypatch.setattr(steam_delist.http, "Client", Session)
    monkeypatch.setattr(steam_delist, "post_market", lambda *args, **kwargs: Response())
    monkeypatch.setattr(
        steam_delist,
        "_get_assetids_by_class_instance",
        lambda *args, **kwargs: set(),
    )

    ok, new_assetid, error = steam_delist.delist_item(
        "asset-old",
        "Test Item",
    )

    assert ok is False
    assert new_assetid is None
    assert error_fragment in error


def test_inventory_query_distinguishes_failure_from_empty_inventory():
    from app import steam_delist

    class FailedSession:
        def get(self, *args, **kwargs):
            raise TimeoutError("timeout")

    class EmptyResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"success": 1, "assets": [], "more_items": False}

    class EmptySession:
        def get(self, *args, **kwargs):
            return EmptyResponse()

    args = ("steam-id", "730", "2", "class-id", "0")

    assert (
        steam_delist._get_assetids_by_class_instance(FailedSession(), *args)
        is None
    )
    assert steam_delist._get_assetids_by_class_instance(
        EmptySession(),
        *args,
    ) == set()


def test_inventory_diagnostics_preserve_throttle_and_retry_after():
    from app.steam_delist import _get_assetids_by_class_instance
    from unittest.mock import Mock
    session=Mock();session.get.return_value=Mock(status_code=429,headers={'Retry-After':'3600'})
    details={}
    assert _get_assetids_by_class_instance(session,'steam','730','2','class','0',diagnostics=details) is None
    assert details['http_status']==429 and details['retry_after']==3600
    assert 'HTTP 429' in details['reason']
    assert session.get.call_count==1


def test_delist_preflight_failure_proves_no_post_was_sent(monkeypatch):
    from app import steam_delist
    from unittest.mock import Mock
    session=Mock();monkeypatch.setattr(steam_delist.http,'Client',lambda:session)
    monkeypatch.setattr(steam_delist,'get_steam',lambda:{'cookies':'steamLoginSecure=x; sessionid=y','steam_id':'steam'})
    monkeypatch.setattr(steam_delist,'_get_mylistings_api',lambda s:{'a':{'listingid':'l','classid':'c','instanceid':'0'}})
    def unavailable(*args,diagnostics):
        diagnostics.update(http_status=429,retry_after=120,reason='库存接口 HTTP 429')
        return None
    monkeypatch.setattr(steam_delist,'_get_assetids_by_class_instance',unavailable)
    outcome={};ok,aid,error=steam_delist.delist_item('a','Item',outcome=outcome)
    assert not ok and aid is None and 'HTTP 429' in error
    assert outcome['request_sent'] is False
    session.post.assert_not_called();session.close.assert_called_once()
