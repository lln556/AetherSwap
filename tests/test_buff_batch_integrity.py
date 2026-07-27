import json
from decimal import Decimal

import pytest

from app.pipeline_steps import (
    _affordable_quantity,
    _validate_unique_batch_matches,
)
from app.services.buff_client import BuffClient
from buff import BuffWriteResultUnknown
from buff.buyer import BuffBuyer, PAY_METHOD_WECHAT


def test_batch_frozen_amount_uses_exact_currency_arithmetic(monkeypatch):
    buyer = object.__new__(BuffBuyer)
    buyer.pay_method = PAY_METHOD_WECHAT
    captured = {}

    def make_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["payload"] = json.loads(kwargs["data"])
        return {"code": "OK", "data": {"id": "batch-1"}}

    monkeypatch.setattr(buyer, "_make_request", make_request)

    batch_id = buyer.batch_buy_create(
        goods_id=42,
        max_price=0.29,
        num=3,
    )

    assert batch_id == "batch-1"
    assert captured["payload"]["frozen_amount"] == 0.87
    assert Decimal(str(captured["payload"]["frozen_amount"])) == (
        Decimal(captured["payload"]["max_price"]) * 3
    )


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
