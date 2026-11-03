"""Read-only evidence for completed Steam item receipts.

Credentials, the web API token, and the API key are used only in memory.
Malformed, ambiguous, incomplete, or failed responses return a static error.
"""

import base64
from collections.abc import Mapping
import json
import re
import time
from html.parser import HTMLParser

from steam.session import create_market_session


REQUEST_TIMEOUT = 15
_COMMUNITY = "https://steamcommunity.com/my/tradehistory/"
_API_KEY = "https://steamcommunity.com/dev/apikey?l=english"
_HISTORY = "https://api.steampowered.com/IEconService/GetTradeHistory/v1/"
_STATUS = "https://api.steampowered.com/IEconService/GetTradeStatus/v1/"
_MAX_PAGES = 5
_MAX_STATUS_TRADES = 10

_UNAVAILABLE = "Steam trade evidence is unavailable"
_IDENTITY = "Steam trade evidence identity mismatch"
_INVALID = "Steam trade evidence response is invalid"
_AMBIGUOUS = "Steam trade evidence is ambiguous"
_INCOMPLETE = "Steam trade evidence is incomplete"
_STEAM_ID = re.compile(r"\bg_steamID\s*=\s*(?:[\"'](\d+)[\"']|(\d+))")
_API_KEY_VALUE = re.compile(r"(?<![0-9A-Fa-f])Key:\s*([0-9A-Fa-f]{32})(?![0-9A-Fa-f])")


def read_receipt_evidence(
    credentials, old_asset_ids, *, session_factory=create_market_session, now=None
):
    """Return ``mappings``, ``pending_protection``, and a static ``error``.

    The caller owns caching and persistence. This function never registers an
    API key, writes account state, or includes upstream details in errors.
    """
    try:
        return _read(credentials, old_asset_ids, session_factory, now)
    except _Failure as exc:
        return _result(error=str(exc))
    except Exception:
        return _result(error=_UNAVAILABLE)


class _Failure(Exception):
    pass


class _LoyaltyStore(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.value = None

    def handle_starttag(self, _tag, attrs):
        if self.value is None:
            self.value = next(
                (value for name, value in attrs
                 if name.lower() == "data-loyaltystore" and value),
                None,
            )


def _result(*, mappings=None, pending_protection=None, error=""):
    return {
        "mappings": mappings or [],
        "pending_protection": pending_protection or [],
        "error": error,
    }


def _fail(message):
    raise _Failure(message)


def _text(value):
    if value is None or isinstance(value, bool) or str(value) == "":
        _fail(_INVALID)
    return str(value)


def _int(value):
    if isinstance(value, bool) or value is None:
        _fail(_INVALID)
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        _fail(_INVALID)
    if isinstance(value, float) and not value.is_integer():
        _fail(_INVALID)
    return number


def _positive(value):
    number = _int(value)
    if number <= 0:
        _fail(_INVALID)
    return number


def _time(value, *, default_zero=False):
    if value is None and default_zero:
        return 0
    if isinstance(value, bool) or value is None:
        _fail(_INVALID)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            _fail(_INVALID)
        return value
    return _int(value)


def _get(session, url, *, params=None, json_response=True):
    response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    if getattr(response, "status_code", None) != 200:
        _fail(_UNAVAILABLE)
    if not json_response:
        return response
    try:
        payload = response.json()
    except Exception:
        _fail(_INVALID)
    if not isinstance(payload, dict):
        _fail(_INVALID)
    return payload


def _steam_ids(page):
    found = {a or b for a, b in _STEAM_ID.findall(page or "")}
    if len(found) != 1:
        _fail(_IDENTITY)
    return next(iter(found))


def _community_identity(page):
    if not isinstance(page, str) or not page:
        _fail(_INVALID)
    account = _steam_ids(page)
    parser = _LoyaltyStore()
    try:
        parser.feed(page)
        store = json.loads(parser.value) if parser.value else None
    except Exception:
        _fail(_INVALID)
    token = store.get("webapi_token") if isinstance(store, dict) else None
    if not isinstance(token, str) or not token:
        _fail(_INVALID)
    return account, token


def _jwt_subject(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        _fail(_IDENTITY)
    if not isinstance(claims, dict) or claims.get("sub") is None:
        _fail(_IDENTITY)
    return str(claims["sub"])


def _targets(values):
    if isinstance(values, (str, bytes)) or not isinstance(
        values, (list, tuple, set, frozenset)
    ):
        _fail(_INVALID)
    result = [_text(value) for value in values]
    if len(result) != len(set(result)):
        _fail(_AMBIGUOUS)
    return result


def _history_payload(cursor=None):
    payload = {
        "max_trades": 100,
        "include_failed": True,
        "get_descriptions": False,
    }
    if cursor is not None:
        payload.update({
            "start_after_time": cursor[0],
            "start_after_tradeid": cursor[1],
            "navigating_back": False,
        })
    return payload


def _cursor(record):
    tradeid = _text(record.get("tradeid"))
    if not tradeid.isdigit():
        _fail(_INVALID)
    return _time(record.get("time_init")), int(tradeid)


def _received_target(item, old_assetid):
    return (
        _text(item.get("assetid")) == old_assetid
        and _int(item.get("amount")) == 1
        and _int(item.get("appid")) == 730
        and _int(item.get("contextid")) == 2
    )


def _history_candidates(session, token, targets, now):
    wanted = set(targets)
    found = set()
    pending = set()
    candidates = {}
    seen_tradeids = set()
    previous_cursor = None
    payload = _history_payload()

    for page_number in range(_MAX_PAGES):
        response = _get(session, _HISTORY, params={
            "access_token": token,
            "input_json": json.dumps(payload, separators=(",", ":")),
        }).get("response")
        if not isinstance(response, dict):
            _fail(_INVALID)
        trades = response.get("trades", [])
        if not isinstance(trades, list):
            _fail(_INVALID)

        for item in trades:
            if not isinstance(item, dict):
                _fail(_INVALID)
            tradeid = _text(item.get("tradeid"))
            if tradeid in seen_tradeids:
                _fail(_INVALID)
            seen_tradeids.add(tradeid)
            time_init = _positive(item.get("time_init"))
            status = _int(item.get("status"))
            received = item.get("assets_received", [])
            if not isinstance(received, list):
                _fail(_INVALID)

            for received_item in received:
                if not isinstance(received_item, dict):
                    _fail(_INVALID)
                old_assetid = _text(received_item.get("assetid"))
                if old_assetid not in wanted:
                    continue
                if _rolled_back(item) or _rolled_back(received_item):
                    _fail(_INVALID)
                if status != 3:
                    _fail(_AMBIGUOUS)
                if not _received_target(received_item, old_assetid):
                    continue
                if old_assetid in found:
                    _fail(_AMBIGUOUS)
                found.add(old_assetid)

                classid = _text(received_item.get("classid"))
                settlement = _time(item.get("time_settlement"), default_zero=True)
                if settlement > now:
                    pending.add(old_assetid)
                    continue
                candidate = {
                    "tradeid": tradeid,
                    "time_init": time_init,
                    "steamid_other": _text(item.get("steamid_other")),
                    "classid": classid,
                    "old_assetid": old_assetid,
                }
                candidates.setdefault(tradeid, []).append(candidate)

        if wanted <= found:
            break
        more = response.get("more", False)
        if not isinstance(more, bool):
            _fail(_INVALID)
        if not more:
            break
        if not trades or page_number == _MAX_PAGES - 1:
            _fail(_INCOMPLETE)

        cursor = _cursor(trades[-1])
        if previous_cursor is not None and cursor >= previous_cursor:
            _fail(_INVALID)
        previous_cursor = cursor
        payload = _history_payload(cursor)

    # The caller retains unmapped targets as pending receipt work. Cap only
    # this round's status requests so large batches drain over later rounds.
    return (
        dict(list(candidates.items())[:_MAX_STATUS_TRADES]),
        [value for value in targets if value in pending],
    )


def _api_key(session, account):
    page = _get(session, _API_KEY, json_response=False).text
    if _steam_ids(page) != account:
        _fail(_IDENTITY)
    match = _API_KEY_VALUE.search(page)
    if not match:
        _fail(_INVALID)
    return match.group(1)


def _rollback_value(value):
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return bool(value) and value != "0"
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return bool(value)


def _rolled_back(record):
    return any(
        str(name).lower().startswith("rollback") and _rollback_value(value)
        for name, value in record.items()
    )


def _mapping(account, record, detail, asset):
    classid = _text(asset.get("classid"))
    if classid != record["classid"] or _text(asset.get("assetid")) != record["old_assetid"]:
        _fail(_INVALID)
    if "new_classid" in asset and _text(asset["new_classid"]) != classid:
        _fail(_INVALID)
    if (_int(asset.get("amount")) != 1 or _int(asset.get("appid")) != 730
            or _int(asset.get("contextid")) != 2):
        _fail(_INVALID)
    if "new_appid" in asset and _int(asset["new_appid"]) != 730:
        _fail(_INVALID)
    if _rolled_back(asset) or _rolled_back(detail):
        _fail(_INVALID)

    new_assetid = _text(asset.get("new_assetid"))
    if not new_assetid.isdigit() or int(new_assetid) == 0:
        _fail(_INVALID)
    new_contextid = _int(asset.get("new_contextid"))
    if new_contextid not in (2, 16):
        _fail(_INVALID)
    return {
        "steam_id": account,
        "trade_id": record["tradeid"],
        "time_init": record["time_init"],
        "old_assetid": record["old_assetid"],
        "assetid": new_assetid,
        "contextid": str(new_contextid),
        "classid": classid,
        "appid": 730,
        "amount": 1,
    }


def _status_mappings(session, account, api_key, candidates, now):
    mappings = []
    for tradeid, records in candidates.items():
        response = _get(session, _STATUS, params={
            "key": api_key, "tradeid": tradeid, "get_descriptions": 0,
        }).get("response")
        # Steam can hide a trade's asset mapping even after its settlement
        # time. A successful empty response supplies no receipt evidence.
        if response == {}:
            continue
        trades = response.get("trades") if isinstance(response, dict) else None
        if not isinstance(trades, list) or len(trades) != 1 or not isinstance(trades[0], dict):
            _fail(_INVALID)
        detail = trades[0]
        first = records[0]
        if (_text(detail.get("tradeid")) != tradeid
                or _positive(detail.get("time_init")) != first["time_init"]
                or _int(detail.get("status")) != 3
                or _text(detail.get("steamid_other")) != first["steamid_other"]
                or _rolled_back(detail)
                or _time(detail.get("time_settlement"), default_zero=True) > now):
            _fail(_INVALID)
        assets = detail.get("assets_received")
        if not isinstance(assets, list):
            _fail(_INVALID)
        for record in records:
            matches = [
                asset for asset in assets
                if isinstance(asset, dict)
                and str(asset.get("assetid")) == record["old_assetid"]
            ]
            if len(matches) != 1:
                _fail(_INVALID)
            mappings.append(_mapping(account, record, detail, matches[0]))

    if (len({row["old_assetid"] for row in mappings}) != len(mappings)
            or len({row["assetid"] for row in mappings}) != len(mappings)):
        _fail(_AMBIGUOUS)
    return mappings


def _read(credentials, old_asset_ids, session_factory, now):
    if not isinstance(credentials, Mapping):
        _fail(_IDENTITY)
    cookies = credentials.get("cookies")
    account = credentials.get("steam_id")
    if not isinstance(cookies, str) or not cookies or account is None:
        _fail(_IDENTITY)
    account = str(account)
    if not account.isdigit():
        _fail(_IDENTITY)
    targets = _targets(old_asset_ids)
    if not targets:
        return _result()

    try:
        current = time.time() if now is None else float(now)
        if current != current or current in (float("inf"), float("-inf")):
            _fail(_INVALID)
    except (TypeError, ValueError, OverflowError):
        _fail(_INVALID)

    with session_factory(cookies, account, verify=True) as session:
        page_account, token = _community_identity(_get(
            session, _COMMUNITY, json_response=False
        ).text)
        if page_account != account or _jwt_subject(token) != account:
            _fail(_IDENTITY)
        candidates, pending = _history_candidates(session, token, targets, current)
        if not candidates:
            return _result(pending_protection=pending)
        key = _api_key(session, account)
        mappings = _status_mappings(session, account, key, candidates, current)
        return _result(mappings=mappings, pending_protection=pending)
