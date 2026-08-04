import pytest

from app.pipeline_steps import (
    _affordable_quantity,
    _validate_unique_batch_matches,
)
from app.services.buff_client import BuffClient
from buff import BuffRequestBlocked, BuffWriteResultUnknown
from buff.buyer import BuffBuyer, PAY_METHOD_WECHAT


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("batch_buy_create", (42, 0.29, 3, "csgo")),
        (
            "batch_buy_finalize",
            ("csgo", 42, "sell-1", "0.29", "batch-1"),
        ),
    ],
)
def test_legacy_batch_writes_are_blocked_before_http(monkeypatch, method_name, args):
    buyer = object.__new__(BuffBuyer)
    buyer.pay_method = PAY_METHOD_WECHAT
    calls = []

    def make_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        raise AssertionError("legacy batch flow must not issue HTTP")

    monkeypatch.setattr(buyer, "_make_request", make_request)

    with pytest.raises(BuffRequestBlocked):
        getattr(buyer, method_name)(*args)

    assert calls == []


def test_client_batch_buy_is_safe_not_supported_fallback_without_running_buyer():
    client = object.__new__(BuffClient)
    client._run = lambda _operation: (_ for _ in ()).throw(
        AssertionError("disabled batch flow must not reach BuffBuyer")
    )
    created_ids = []

    result = client.try_batch_buy(
        goods_id=42,
        game="csgo",
        orders=[{"id": "sell-1", "price": "0.29"}],
        unit_price=0.29,
        num=3,
        on_created=created_ids.append,
    )

    assert result["code"] == "NOT_SUPPORTED"
    assert result["created"] is False
    assert result["safe_to_fallback"] is True
    assert created_ids == []


def test_exact_budget_multiple_does_not_plan_one_item_too_few():
    # Binary float division produces 2.9999999999999996 here.
    assert 0.15 / 0.05 < 3
    assert _affordable_quantity(0.15, 0, 0.05) == 3
    assert _affordable_quantity(0.30, 0.10, 0.20) == 1


def _client_using(buyer):
    client = object.__new__(BuffClient)
    client._run = lambda operation: operation(buyer)
    return client


def test_finalize_skips_duplicate_sell_rows_without_sending_duplicate_post():
    class Buyer:
        def __init__(self):
            self.calls = []

        def get_sell_orders(self, *_args):
            return [
                {"id": "sell-1", "price": "0.29"},
                {"id": "sell-1", "price": "0.29"},
                {"id": "sell-2", "price": "0.29"},
            ]

        def batch_buy_finalize(
            self,
            _game,
            _goods_id,
            sell_order_id,
            _price,
            _batch_id,
        ):
            self.calls.append(sell_order_id)
            return f"bill-{sell_order_id}"

    buyer = Buyer()

    matched = _client_using(buyer).batch_buy_find_and_finalize(
        goods_id=42,
        game="csgo",
        max_price=0.29,
        num=2,
        batch_id="batch-1",
    )

    assert buyer.calls == ["sell-1", "sell-2"]
    assert [row["id"] for row in matched] == ["sell-1", "sell-2"]


def test_duplicate_bill_id_halts_with_only_prior_unique_results():
    class Buyer:
        def __init__(self):
            self.calls = []

        def get_sell_orders(self, *_args):
            return [
                {"id": "sell-1", "price": "0.29"},
                {"id": "sell-2", "price": "0.29"},
                {"id": "sell-3", "price": "0.29"},
            ]

        def batch_buy_finalize(
            self,
            _game,
            _goods_id,
            sell_order_id,
            _price,
            _batch_id,
        ):
            self.calls.append(sell_order_id)
            return "bill-1"

    buyer = Buyer()

    with pytest.raises(BuffWriteResultUnknown) as exc_info:
        _client_using(buyer).batch_buy_find_and_finalize(
            goods_id=42,
            game="csgo",
            max_price=0.29,
            num=3,
            batch_id="batch-1",
        )

    assert buyer.calls == ["sell-1", "sell-2"]
    assert exc_info.value.partial_results == [
        {
            "id": "sell-1",
            "price": 0.29,
            "bill_order_id": "bill-1",
        }
    ]


def test_pipeline_rejects_duplicate_ids_before_persisting_complete_batch():
    matches = [
        {"id": "sell-1", "price": 0.29, "bill_order_id": "bill-1"},
        {"id": "sell-2", "price": 0.29, "bill_order_id": "bill-1"},
        {"id": "sell-3", "price": 0.29, "bill_order_id": "bill-3"},
    ]

    with pytest.raises(BuffWriteResultUnknown) as exc_info:
        _validate_unique_batch_matches(matches, "batch-1")

    assert exc_info.value.batch_id == "batch-1"
    assert exc_info.value.partial_results == [matches[0], matches[2]]
