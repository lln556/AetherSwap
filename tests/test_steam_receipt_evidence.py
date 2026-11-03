import base64
import html
import json

import httpx
import pytest

from app.services import steam_receipt_evidence as evidence


ACCOUNT = "76561198000000001"
OTHER_ACCOUNT = "76561198000000002"
NOW = 20_000
TRADE_HISTORY_URL = "https://api.steampowered.com/IEconService/GetTradeHistory/v1/"
TRADE_STATUS_URL = "https://api.steampowered.com/IEconService/GetTradeStatus/v1/"
COMMUNITY_HISTORY_URL = "https://steamcommunity.com/my/tradehistory/"
API_KEY_URL = "https://steamcommunity.com/dev/apikey?l=english"
API_KEY = "0123456789abcdef0123456789abcdef"


def make_token(account=ACCOUNT):
    def encoded(value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encoded({'alg': 'none'})}.{encoded({'sub': account})}.not-a-signature"


def community_html(account=ACCOUNT, token=None):
    token = token or make_token(account)
    store = html.escape(json.dumps({"webapi_token": token}), quote=True)
    return (
        f'<script>var g_steamID = "{account}";</script>'
        f'<div id="application_config" data-loyaltystore="{store}"></div>'
    )


def api_key_html(account=ACCOUNT):
    return f'<script>var g_steamID = "{account}";</script><p>Key: {API_KEY}</p>'


def asset(
    old_assetid,
    *,
    new_assetid=None,
    classid="class-1",
    amount="1",
    appid=730,
    contextid="2",
    new_contextid="2",
    rollback=False,
):
    value = {
        "assetid": old_assetid,
        "classid": classid,
        "amount": amount,
        "appid": appid,
        "contextid": contextid,
        "rollback": rollback,
    }
    if new_assetid is not None:
        value["new_assetid"] = new_assetid
    if new_contextid is not None:
        value["new_contextid"] = new_contextid
    return value


def trade(
    tradeid,
    *,
    time_init=1_000,
    time_settlement=1_100,
    steamid_other=OTHER_ACCOUNT,
    status=3,
    received=None,
    rollback=False,
):
    return {
        "tradeid": str(tradeid),
        "time_init": time_init,
        "time_settlement": time_settlement,
        "steamid_other": steamid_other,
        "status": status,
        "rollback": rollback,
        "assets_received": received or [],
    }


def history_response(trades, *, more=False):
    return {"response": {"trades": trades, "more": more, "total": len(trades)}}


def status_response(trades):
    return {"response": {"trades": trades}}


def detailed_trade(history_trade, received=None):
    result = dict(history_trade)
    result["assets_received"] = received or []
    return result


class FakeResponse:
    def __init__(self, *, payload=None, text="", status_code=200):
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("response was not JSON")
        return self._payload


class FakeSession:
    def __init__(self, handler, calls):
        self.handler = handler
        self.calls = calls
        self.verify = None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.handler(url, kwargs)


def invoke(handler, old_asset_ids=("101",), *, account=ACCOUNT, now=NOW):
    calls = []
    session = FakeSession(handler, calls)
    factory_calls = []

    def factory(cookies, steam_id, **kwargs):
        factory_calls.append((cookies, steam_id, kwargs))
        session.verify = kwargs.get("verify")
        return session

    result = evidence.read_receipt_evidence(
        {"cookies": "sessionid=secret-cookie; steamLoginSecure=secret", "steam_id": account},
        list(old_asset_ids),
        session_factory=factory,
        now=now,
    )
    return result, calls, factory_calls, session


def history_input(call):
    return json.loads(call[1]["params"]["input_json"])


def history_calls(calls):
    return [call for call in calls if call[0] == TRADE_HISTORY_URL]


def status_calls(calls):
    return [call for call in calls if call[0] == TRADE_STATUS_URL]


def api_key_calls(calls):
    return [call for call in calls if call[0] == API_KEY_URL]


def assert_safe_error(result):
    assert result["mappings"] == []
    assert isinstance(result["error"], str) and result["error"]
    for secret in ("secret-cookie", "secret", "not-a-signature", API_KEY, "example.com"):
        assert secret not in result["error"]


def test_full_chain_uses_verified_account_paginates_and_builds_exact_mappings():
    first = trade(
        300,
        time_init=3_000,
        time_settlement=3_100,
        received=[asset("999", classid="unrelated")],
    )
    second = trade(
        202,
        time_init=2_000,
        time_settlement=2_100,
        received=[asset("101", classid="class-101")],
    )
    third = trade(
        103,
        time_init=1_000,
        time_settlement=1_100,
        received=[asset("102", classid="class-102")],
    )
    pages = [
        history_response([first], more=True),
        history_response([second], more=True),
        history_response([third], more=False),
    ]
    page_index = 0

    def handler(url, kwargs):
        nonlocal page_index
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            response = pages[page_index]
            page_index += 1
            return FakeResponse(payload=response)
        if url == API_KEY_URL:
            return FakeResponse(text=api_key_html())
        if url == TRADE_STATUS_URL:
            tradeid = str(kwargs["params"]["tradeid"])
            source = {"202": second, "103": third}[tradeid]
            received = {
                "202": [asset("101", new_assetid="1001", classid="class-101")],
                "103": [asset(
                    "102", new_assetid="1002", classid="class-102", new_contextid="16"
                )],
            }[tradeid]
            return FakeResponse(payload=status_response([detailed_trade(source, received)]))
        raise AssertionError(f"unexpected URL: {url}")

    result, calls, factory_calls, session = invoke(handler, ("101", "102"))

    assert result == {
        "mappings": [
            {
                "steam_id": ACCOUNT,
                "trade_id": "202",
                "time_init": 2_000,
                "old_assetid": "101",
                "assetid": "1001",
                "contextid": "2",
                "classid": "class-101",
                "appid": 730,
                "amount": 1,
            },
            {
                "steam_id": ACCOUNT,
                "trade_id": "103",
                "time_init": 1_000,
                "old_assetid": "102",
                "assetid": "1002",
                "contextid": "16",
                "classid": "class-102",
                "appid": 730,
                "amount": 1,
            },
        ],
        "pending_protection": [],
        "error": "",
    }
    assert factory_calls == [("sessionid=secret-cookie; steamLoginSecure=secret", ACCOUNT, {"verify": True})]
    assert session.verify is True and session.closed is True
    assert [call[0] for call in calls].count(COMMUNITY_HISTORY_URL) == 1
    assert len(history_calls(calls)) == 3
    assert len(api_key_calls(calls)) == 1
    assert len(status_calls(calls)) == 2
    assert all(call[1]["timeout"] == evidence.REQUEST_TIMEOUT for call in calls)
    assert all("register" not in call[0].lower() for call in calls)

    payloads = [history_input(call) for call in history_calls(calls)]
    assert payloads[0] == {
        "max_trades": 100,
        "include_failed": True,
        "get_descriptions": False,
    }
    assert payloads[1] == {
        "max_trades": 100,
        "include_failed": True,
        "get_descriptions": False,
        "start_after_time": 3_000,
        "start_after_tradeid": 300,
        "navigating_back": False,
    }
    assert payloads[2]["start_after_time"] == 2_000
    assert payloads[2]["start_after_tradeid"] == 202
    assert history_calls(calls)[0][1]["params"]["access_token"] == make_token()
    for call in status_calls(calls):
        assert call[1]["params"]["key"] == API_KEY
        assert call[1]["params"]["get_descriptions"] == 0
        assert call[1]["timeout"] == evidence.REQUEST_TIMEOUT


@pytest.mark.parametrize("kind", ["steamid", "jwt-sub"])
def test_community_identity_mismatch_stops_before_history_request(kind):
    page_account = OTHER_ACCOUNT if kind == "steamid" else ACCOUNT
    token_account = OTHER_ACCOUNT if kind == "jwt-sub" else ACCOUNT

    def handler(url, _kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html(page_account, make_token(token_account)))
        raise AssertionError("history must not be requested after identity mismatch")

    result, calls, _factory_calls, _session = invoke(handler)

    assert_safe_error(result)
    assert [call[0] for call in calls] == [COMMUNITY_HISTORY_URL]


def test_future_settlement_is_protected_without_requesting_api_key_or_status():
    future = trade(
        300,
        time_init=1_000,
        time_settlement=NOW + 1,
        received=[asset("101", classid="class-101")],
    )

    def handler(url, _kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            return FakeResponse(payload=history_response([future], more=False))
        raise AssertionError("future trade must not request the API key or trade status")

    result, calls, _factory_calls, _session = invoke(handler)

    assert result == {"mappings": [], "pending_protection": ["101"], "error": ""}
    assert len(history_calls(calls)) == 1
    assert api_key_calls(calls) == []
    assert status_calls(calls) == []


@pytest.mark.parametrize(
    "kind",
    [
        "missing-new-assetid",
        "missing-new-contextid",
        "zero-new-assetid",
        "nonnumeric-new-assetid",
        "invalid-new-contextid",
        "classid-mismatch",
        "new-classid-mismatch",
        "rollback",
        "asset-rollback",
        "rollback-assetid",
        "rollback-contextid",
        "rollback-new-assetid",
        "detail-rollback-assetid",
        "wrong-amount",
        "wrong-appid",
    ],
)
def test_missing_or_ambiguous_new_asset_fields_reject_all_mappings(kind):
    history_trade = trade(
        300,
        time_init=1_000,
        time_settlement=1_100,
        received=[asset("101", classid="class-101")],
    )
    detail_asset = asset(
        "101",
        new_assetid="1001",
        new_contextid="2",
        classid="class-101",
    )
    detail_trade = detailed_trade(history_trade, [detail_asset])

    if kind == "missing-new-assetid":
        detail_asset.pop("new_assetid")
    elif kind == "missing-new-contextid":
        detail_asset.pop("new_contextid")
    elif kind == "zero-new-assetid":
        detail_asset["new_assetid"] = "0"
    elif kind == "nonnumeric-new-assetid":
        detail_asset["new_assetid"] = "not-numeric"
    elif kind == "invalid-new-contextid":
        detail_asset["new_contextid"] = "3"
    elif kind == "classid-mismatch":
        detail_asset["classid"] = "class-other"
    elif kind == "new-classid-mismatch":
        detail_asset["new_classid"] = "class-other"
    elif kind == "rollback":
        detail_trade["rollback"] = True
    elif kind == "asset-rollback":
        detail_asset["rollback"] = True
    elif kind == "rollback-assetid":
        detail_asset["rollback_assetid"] = "1001"
    elif kind == "rollback-contextid":
        detail_asset["rollback_contextid"] = "16"
    elif kind == "rollback-new-assetid":
        detail_asset["rollback_new_assetid"] = "1001"
    elif kind == "detail-rollback-assetid":
        detail_trade["rollback_assetid"] = "1001"
    elif kind == "wrong-amount":
        detail_asset["amount"] = "2"
    elif kind == "wrong-appid":
        detail_asset["appid"] = 570

    def handler(url, kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            return FakeResponse(payload=history_response([history_trade], more=False))
        if url == API_KEY_URL:
            return FakeResponse(text=api_key_html())
        if url == TRADE_STATUS_URL:
            assert kwargs["params"]["tradeid"] == "300"
            return FakeResponse(payload=status_response([detail_trade]))
        raise AssertionError(f"unexpected URL: {url}")

    result, calls, _factory_calls, _session = invoke(handler)

    assert_safe_error(result)
    assert len(api_key_calls(calls)) == 1
    assert len(status_calls(calls)) == 1


def test_duplicate_old_asset_mapping_rejects_everything_before_reading_key():
    first = trade(
        300,
        time_init=3_000,
        time_settlement=3_100,
        received=[asset("101", classid="class-101")],
    )
    second = trade(
        200,
        time_init=2_000,
        time_settlement=2_100,
        received=[asset("101", classid="class-101", new_assetid="1001")],
    )

    def handler(url, _kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            return FakeResponse(payload=history_response([first, second], more=False))
        raise AssertionError("duplicate target evidence must not request the API key")

    result, calls, _factory_calls, _session = invoke(handler)

    assert_safe_error(result)
    assert api_key_calls(calls) == []
    assert status_calls(calls) == []


def test_duplicate_new_asset_mapping_rejects_everything():
    first = trade(
        300,
        time_init=3_000,
        time_settlement=3_100,
        received=[asset("101", classid="class-101")],
    )
    second = trade(
        200,
        time_init=2_000,
        time_settlement=2_100,
        received=[asset("102", classid="class-102")],
    )

    def handler(url, kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            return FakeResponse(payload=history_response([first, second], more=False))
        if url == API_KEY_URL:
            return FakeResponse(text=api_key_html())
        if url == TRADE_STATUS_URL:
            tradeid = kwargs["params"]["tradeid"]
            source = {"300": first, "200": second}[tradeid]
            old = "101" if tradeid == "300" else "102"
            classid = "class-101" if tradeid == "300" else "class-102"
            received = [asset(old, new_assetid="777", classid=classid)]
            return FakeResponse(payload=status_response([detailed_trade(source, received)]))
        raise AssertionError(f"unexpected URL: {url}")

    result, calls, _factory_calls, _session = invoke(handler, ("101", "102"))

    assert_safe_error(result)
    assert len(status_calls(calls)) == 2


@pytest.mark.parametrize("fail_at", ["community", "history", "api-key", "status"])
def test_network_failures_return_static_error_without_partial_mappings(fail_at, capsys):
    history_trade = trade(
        300,
        time_init=1_000,
        time_settlement=1_100,
        received=[asset("101", classid="class-101")],
    )
    detail = detailed_trade(
        history_trade,
        [asset("101", new_assetid="1001", classid="class-101")],
    )

    def fail():
        raise httpx.ConnectError(
            "https://secret-token-key-cookie@example.com/not-a-signature/secret-cookie"
        )

    def handler(url, _kwargs):
        if url == COMMUNITY_HISTORY_URL:
            if fail_at == "community":
                fail()
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            if fail_at == "history":
                fail()
            return FakeResponse(payload=history_response([history_trade], more=False))
        if url == API_KEY_URL:
            if fail_at == "api-key":
                fail()
            return FakeResponse(text=api_key_html())
        if url == TRADE_STATUS_URL:
            if fail_at == "status":
                fail()
            return FakeResponse(payload=status_response([detail]))
        raise AssertionError(f"unexpected URL: {url}")

    result, _calls, _factory_calls, _session = invoke(handler)

    assert_safe_error(result)
    assert capsys.readouterr().out == ""
    assert capsys.readouterr().err == ""


def test_repeated_history_page_is_rejected_without_using_partial_evidence():
    repeated = trade(
        300,
        time_init=1_000,
        time_settlement=1_100,
        received=[asset("999")],
    )

    def handler(url, _kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            return FakeResponse(payload=history_response([repeated], more=True))
        raise AssertionError("repeated history must not request API key")

    result, calls, _factory_calls, _session = invoke(handler)

    assert_safe_error(result)
    assert len(history_calls(calls)) == 2


def test_non_progress_history_cursor_is_rejected():
    pages = iter([
        history_response([
            trade(300, time_init=3_000, time_settlement=3_100, received=[asset("999")])
        ], more=True),
        history_response([
            trade(301, time_init=3_000, time_settlement=3_100, received=[asset("998")])
        ], more=True),
    ])

    def handler(url, _kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            return FakeResponse(payload=next(pages))
        raise AssertionError("non-progress history must not request API key")

    result, calls, _factory_calls, _session = invoke(handler)

    assert_safe_error(result)
    assert len(history_calls(calls)) == 2


def test_history_scan_is_bounded_to_five_pages():
    page_number = 0

    def handler(url, _kwargs):
        nonlocal page_number
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            page_number += 1
            item = trade(
                400 - page_number,
                time_init=6_000 - page_number * 1_000,
                time_settlement=5_900 - page_number * 1_000,
                received=[asset("999")],
            )
            return FakeResponse(payload=history_response([item], more=True))
        raise AssertionError("incomplete history must not request API key")

    result, calls, _factory_calls, _session = invoke(handler)

    assert_safe_error(result)
    assert len(history_calls(calls)) == 5


def test_wrong_old_asset_fields_are_not_candidates():
    wrong_appid = trade(
        402,
        time_init=3_000,
        time_settlement=3_100,
        received=[asset("101", appid=570)],
    )
    wrong_context = trade(
        403,
        time_init=2_000,
        time_settlement=2_100,
        received=[asset("101", contextid="16")],
    )
    wrong_amount = trade(
        404,
        time_init=1_000,
        time_settlement=1_100,
        received=[asset("101", amount="2")],
    )

    def handler(url, _kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            return FakeResponse(payload=history_response(
                [wrong_appid, wrong_context, wrong_amount], more=False
            ))
        raise AssertionError("invalid history assets must not request API key")

    result, calls, _factory_calls, _session = invoke(handler)

    assert result == {"mappings": [], "pending_protection": [], "error": ""}
    assert api_key_calls(calls) == []
    assert status_calls(calls) == []


def test_more_than_ten_settleable_trades_are_processed_across_rounds():
    trades = []
    for index in range(1, 12):
        trades.append(trade(
            300 - index,
            time_init=4_000 - index,
            time_settlement=3_900 - index,
            received=[asset(str(index), classid=f"class-{index}")],
        ))

    def handler(url, kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            return FakeResponse(payload=history_response(trades, more=False))
        if url == API_KEY_URL:
            return FakeResponse(text=api_key_html())
        if url == TRADE_STATUS_URL:
            tradeid = kwargs["params"]["tradeid"]
            source = next(item for item in trades if item["tradeid"] == tradeid)
            old_id = source["assets_received"][0]["assetid"]
            received = [asset(
                old_id,
                new_assetid=str(10_000 + int(old_id)),
                classid=f"class-{old_id}",
            )]
            return FakeResponse(payload=status_response([detailed_trade(source, received)]))
        raise AssertionError(f"unexpected URL: {url}")

    first, first_calls, _factory_calls, _session = invoke(
        handler, tuple(str(index) for index in range(1, 12))
    )
    assert first["error"] == ""
    assert [mapping["old_assetid"] for mapping in first["mappings"]] == [
        str(index) for index in range(1, 11)
    ]
    first_ids = [call[1]["params"]["tradeid"] for call in status_calls(first_calls)]
    assert first_ids == [str(300 - index) for index in range(1, 11)]

    second, second_calls, _factory_calls, _session = invoke(handler, ("11",))
    assert second == {
        "mappings": [{
            "steam_id": ACCOUNT,
            "trade_id": "289",
            "time_init": 3_989,
            "old_assetid": "11",
            "assetid": "10011",
            "contextid": "2",
            "classid": "class-11",
            "appid": 730,
            "amount": 1,
        }],
        "pending_protection": [],
        "error": "",
    }
    assert [call[1]["params"]["tradeid"] for call in status_calls(second_calls)] == ["289"]


def test_target_missing_at_end_of_history_returns_verified_mappings_only():
    found = trade(
        300,
        time_init=1_000,
        time_settlement=1_100,
        received=[asset("101", classid="class-101")],
    )

    def handler(url, kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            return FakeResponse(payload=history_response([found], more=False))
        if url == API_KEY_URL:
            return FakeResponse(text=api_key_html())
        if url == TRADE_STATUS_URL:
            assert kwargs["params"]["tradeid"] == "300"
            detail = detailed_trade(
                found, [asset("101", new_assetid="1001", classid="class-101")]
            )
            return FakeResponse(payload=status_response([detail]))
        raise AssertionError(f"unexpected URL: {url}")

    result, calls, _factory_calls, _session = invoke(handler, ("101", "102"))

    assert result["error"] == ""
    assert result["pending_protection"] == []
    assert [mapping["old_assetid"] for mapping in result["mappings"]] == ["101"]
    assert len(status_calls(calls)) == 1


def test_missing_time_settlement_in_history_and_status_defaults_to_zero():
    history_trade = trade(
        300,
        time_init=1_000,
        received=[asset("101", classid="class-101")],
    )
    history_trade.pop("time_settlement")
    detail = detailed_trade(
        history_trade, [asset("101", new_assetid="1001", classid="class-101")]
    )

    def handler(url, _kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            return FakeResponse(payload=history_response([history_trade], more=False))
        if url == API_KEY_URL:
            return FakeResponse(text=api_key_html())
        if url == TRADE_STATUS_URL:
            return FakeResponse(payload=status_response([detail]))
        raise AssertionError(f"unexpected URL: {url}")

    result, _calls, _factory_calls, _session = invoke(handler)

    assert result["error"] == ""
    assert result["mappings"] == [{
        "steam_id": ACCOUNT,
        "trade_id": "300",
        "time_init": 1_000,
        "old_assetid": "101",
        "assetid": "1001",
        "contextid": "2",
        "classid": "class-101",
        "appid": 730,
        "amount": 1,
    }]


def test_same_second_tradeid_cursor_orders_numerically_not_lexically():
    first = trade(
        10,
        time_init=3_000,
        time_settlement=3_100,
        received=[asset("999")],
    )
    second = trade(
        9,
        time_init=3_000,
        time_settlement=3_100,
        received=[asset("101", classid="class-101")],
    )
    pages = iter([
        history_response([first], more=True),
        history_response([second], more=False),
    ])

    def handler(url, kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            return FakeResponse(payload=next(pages))
        if url == API_KEY_URL:
            return FakeResponse(text=api_key_html())
        if url == TRADE_STATUS_URL:
            detail = detailed_trade(
                second, [asset("101", new_assetid="1001", classid="class-101")]
            )
            return FakeResponse(payload=status_response([detail]))
        raise AssertionError(f"unexpected URL: {url}")

    result, calls, _factory_calls, _session = invoke(handler)

    assert result["error"] == ""
    assert [mapping["assetid"] for mapping in result["mappings"]] == ["1001"]
    payloads = [history_input(call) for call in history_calls(calls)]
    assert payloads[1]["start_after_time"] == 3_000
    assert payloads[1]["start_after_tradeid"] == 10


def test_target_seen_in_noncompleted_trade_blocks_older_completed_mapping():
    cancelled = trade(
        300,
        time_init=2_000,
        time_settlement=2_100,
        status=9,
        received=[asset("101", classid="class-101")],
    )
    completed = trade(
        200,
        time_init=1_000,
        time_settlement=1_100,
        received=[asset("101", classid="class-101")],
    )

    def handler(url, _kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            return FakeResponse(payload=history_response([cancelled, completed], more=False))
        raise AssertionError("ambiguous target history must not request the API key")

    result, calls, _factory_calls, _session = invoke(handler)

    assert_safe_error(result)
    assert api_key_calls(calls) == []
    assert status_calls(calls) == []


def test_zero_or_empty_rollback_fields_do_not_reject_completed_trade():
    history_trade = trade(
        300,
        time_init=1_000,
        time_settlement=1_100,
        received=[asset("101", classid="class-101")],
    )
    detail_asset = asset(
        "101",
        new_assetid="1001",
        new_contextid="2",
        classid="class-101",
    )
    detail_asset.update({
        "rollback": False,
        "rollback_assetid": "0",
        "rollback_contextid": "",
        "rollback_new_assetid": 0,
    })
    detail_trade = detailed_trade(history_trade, [detail_asset])

    def handler(url, _kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            return FakeResponse(payload=history_response([history_trade], more=False))
        if url == API_KEY_URL:
            return FakeResponse(text=api_key_html())
        if url == TRADE_STATUS_URL:
            return FakeResponse(payload=status_response([detail_trade]))
        raise AssertionError(f"unexpected URL: {url}")

    result, _calls, _factory_calls, _session = invoke(handler)

    assert result["error"] == ""
    assert [mapping["assetid"] for mapping in result["mappings"]] == ["1001"]


@pytest.mark.parametrize("location,field", [
    ("trade", "rollback_contextid"),
    ("asset", "rollback_new_assetid"),
])
def test_nonempty_rollback_fields_in_history_block_before_key_request(location, field):
    history_trade = trade(
        300,
        time_init=1_000,
        time_settlement=1_100,
        received=[asset("101", classid="class-101")],
    )
    if location == "trade":
        history_trade[field] = "16"
    else:
        history_trade["assets_received"][0][field] = "1001"

    def handler(url, _kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            return FakeResponse(payload=history_response([history_trade], more=False))
        raise AssertionError("rollback history must not request the API key")

    result, calls, _factory_calls, _session = invoke(handler)

    assert_safe_error(result)
    assert api_key_calls(calls) == []
    assert status_calls(calls) == []


def test_settled_trade_hidden_by_legacy_api_remains_unmapped_without_claiming_an_api_failure():
    source = trade(123, received=[asset('101')])
    def handler(url, kwargs):
        if url == COMMUNITY_HISTORY_URL:
            return FakeResponse(text=community_html())
        if url == TRADE_HISTORY_URL:
            return FakeResponse(payload=history_response([source]))
        if url == API_KEY_URL:
            return FakeResponse(text=api_key_html())
        if url == TRADE_STATUS_URL:
            return FakeResponse(payload={'response': {}})
        raise AssertionError('unexpected endpoint')
    result, *_ = invoke(handler)
    assert result == {'mappings': [], 'pending_protection': [], 'error': ''}
