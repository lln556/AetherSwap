import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buff.buyer import BuffBuyer
from buff import BuffWriteResultUnknown


def _buyer_with_responses(monkeypatch, responses):
    buyer = object.__new__(BuffBuyer)
    calls = []
    response_iter = iter(responses)

    def make_request(method, url, **kwargs):
        calls.append(
            {
                "method": method,
                "url": url,
                "payload": json.loads(kwargs["data"]),
            }
        )
        response = next(response_iter)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(buyer, "_make_request", make_request)
    return buyer, calls


def _requested_ids(calls):
    return [call["payload"]["bill_orders"][0] for call in calls]


def test_middle_bill_is_retried_instead_of_being_silently_skipped(monkeypatch):
    from buff import buyer as buyer_module

    buyer, calls = _buyer_with_responses(
        monkeypatch,
        [
            {"code": "OK"},
            {"code": "TemporaryError", "error": "稍后重试"},
            {"code": "OK"},
            {"code": "OK"},
        ],
    )
    sleeps = []
    monkeypatch.setattr(
        buyer_module,
        "jittered_sleep",
        lambda seconds: sleeps.append(seconds),
    )

    result = buyer.ask_seller_to_send(["bill-1", "bill-2", "bill-3"])

    assert result is True
    assert _requested_ids(calls) == [
        "bill-1",
        "bill-2",
        "bill-2",
        "bill-3",
    ]
    assert sleeps == [1.5, 3.0, 1.5]


def test_partial_success_is_not_reported_as_complete_success(monkeypatch):
    from buff import buyer as buyer_module

    buyer, calls = _buyer_with_responses(
        monkeypatch,
        [
            {"code": "OK"},
            {"code": "Rejected"},
            {"code": "Rejected"},
            {"code": "Rejected"},
            {"code": "OK"},
        ],
    )
    monkeypatch.setattr(buyer_module, "jittered_sleep", lambda _seconds: None)

    result = buyer.ask_seller_to_send(["bill-1", "bill-2", "bill-3"])

    assert result is False
    # A permanently failing middle bill does not prevent the last bill from
    # being prompted, but the aggregate result can no longer hide the failure.
    assert _requested_ids(calls) == [
        "bill-1",
        "bill-2",
        "bill-2",
        "bill-2",
        "bill-3",
    ]


def test_order_ids_are_normalized_and_deduplicated(monkeypatch):
    from buff import buyer as buyer_module

    buyer, calls = _buyer_with_responses(
        monkeypatch,
        [{"code": "OK"}, {"code": "OK"}],
    )
    monkeypatch.setattr(buyer_module, "jittered_sleep", lambda _seconds: None)

    result = buyer.ask_seller_to_send(
        [" bill-1 ", "", None, "bill-1", 0, "bill-2"],
    )

    assert result is True
    assert _requested_ids(calls) == ["bill-1", "bill-2"]


def test_unknown_write_result_is_never_retried(monkeypatch):
    from buff import buyer as buyer_module

    unknown = BuffWriteResultUnknown(
        "timeout after send",
        method="POST",
        url="https://buff.invalid/ask",
    )
    buyer, calls = _buyer_with_responses(monkeypatch, [unknown])
    monkeypatch.setattr(
        buyer_module,
        "jittered_sleep",
        lambda _seconds: (_ for _ in ()).throw(
            AssertionError("unknown writes must not be retried")
        ),
    )

    with pytest.raises(BuffWriteResultUnknown):
        buyer.ask_seller_to_send(["bill-1", "bill-2"])

    assert _requested_ids(calls) == ["bill-1"]
