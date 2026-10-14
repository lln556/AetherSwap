import time

import pytest

from buff.buyer import API_HISTORY, BuffBuyer
from buff import BuffVerificationRequired, BuffRequestBlocked
from app import pipeline_steps as steps
from app.services import buff_checkout_guard as guard
from app.services import buff_offer_support as offer_support
from test_buff_request_policy import FakeResponse, FakeSession, no_wait_policy
from test_buff_purchase_flow_safety import _config, _item, _checkout_args


def history_item(**overrides):
    now = int(time.time())
    return dict({"id": "bill-auto", "goods_id": 123, "price": "10.00",
                 "state": "PAYING", "state_text": "等待你发起报价",
                 "created_at": now - 5, "buyer_pay_time": now,
                 "pay_expire_timeout": -1, "error_text": None}, **overrides)


def lookup(*items):
    session = FakeSession(FakeResponse({"code": "OK", "data": {
        "items": list(items), "total_page": 1,
        "goods_infos": {"123": {"market_hash_name": "Test Item"}},
    }}))
    buyer = BuffBuyer("session=s; csrf_token=c", session=session, request_policy=no_wait_policy())
    proof = buyer.get_order_payment("csgo", "bill-auto", 123, "10.00")
    assert [(method, url) for method, url, _ in session.calls] == [("GET", API_HISTORY)]
    return proof


def test_paid_order_can_still_have_paying_state_and_no_payment_deadline():
    assert lookup(history_item())["paid"] is True


def test_paid_order_waiting_for_seller_offer_remains_reconcilable():
    assert lookup(history_item(state="TO_DELIVER", state_text="等待卖家发起报价"))["paid"] is True


@pytest.mark.parametrize("changes", [
    {"buyer_pay_time": 0}, {"buyer_pay_time": None}, {"buyer_pay_time": "nan"},
    {"buyer_pay_time": True}, {"goods_id": True},
    {"buyer_pay_time": time.time() + 3600}, {"created_at": 0},
    {"pay_expire_timeout": 100}, {"goods_id": 999}, {"price": "10.01"},
    {"state": "CANCELLED"}, {"state": "REFUNDED"}, {"state": "UNKNOWN"},
    {"error_text": "付款失败"},
])
def test_unpaid_mismatched_closed_or_malformed_order_is_never_confirmed(changes):
    assert lookup(history_item(**changes))["paid"] is False


def test_a_different_paid_order_does_not_confirm_this_order():
    assert lookup(history_item(id="different-bill"))["paid"] is False


def test_duplicate_order_identity_is_ambiguous():
    assert lookup(history_item(), history_item())["paid"] is False


def test_refunded_order_with_old_payment_timestamp_is_closed_not_paid():
    proof = lookup(history_item(state="FAIL", state_text="购买失败-已退款",
                                error_text="买家发送报价超时"))
    assert proof["closed"] is True and proof["paid"] is False


def test_exact_order_can_be_found_on_a_later_page():
    session = FakeSession(
        FakeResponse({"code": "OK", "data": {"items": [history_item(id="other")], "total_page": 2}}),
        FakeResponse({"code": "OK", "data": {"items": [history_item()], "total_page": 2}}),
    )
    buyer = BuffBuyer("session=s; csrf_token=c", session=session, request_policy=no_wait_policy())
    assert buyer.get_order_payment("csgo", "bill-auto", 123, "10.00")["paid"] is True
    assert [c[2]["params"]["page_num"] for c in session.calls] == ["1", "2"]


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "_GUARD_PATH", tmp_path / "checkout.json")
    monkeypatch.setattr(offer_support, "_PATH", tmp_path / "offer-rejections.json")
    monkeypatch.setattr(steps, "jittered_sleep", lambda *_: None)
    monkeypatch.setattr(steps, "_fetch_smart_market_price", lambda *_a, **_k: 15.0)
    monkeypatch.setattr(steps, "get_purchases", lambda: [])


class AutoClient:
    supports_batch_buy = False

    def __init__(self, *, paid=True, pay_url=None, blocked=False):
        self.paid = paid
        self.pay_url = pay_url
        self.blocked = blocked
        self.lock_calls = 0
        self.lookup_calls = 0
        self.shipping_calls = []

    def lock_and_get_pay_url(self, *_args, on_created=None, **_kwargs):
        self.lock_calls += 1
        if on_created:
            on_created("bill-auto")
        return {"success": bool(self.pay_url), "created": True,
                "code": "CREATED_WITHOUT_PAY_URL", "order_id": "bill-auto",
                "pay_type": "alipay", "pay_url": self.pay_url}

    def get_order_payment(self, *_args):
        self.lookup_calls += 1
        if self.blocked:
            raise BuffVerificationRequired("需要安全验证")
        return {"paid": self.paid and self.lookup_calls >= 2}

    def ask_seller_to_send(self, order_id, game):
        self.shipping_calls.append((order_id, game))
        return True


@pytest.mark.parametrize("pay_url", [None, "https://pay.example/auto"])
def test_async_auto_payment_records_once_without_waiting_for_email(isolated, monkeypatch, pay_url):
    client = AutoClient(pay_url=pay_url)
    kwargs, pending, purchases = _checkout_args()
    kwargs["wait_payment_confirm"] = lambda **_: pytest.fail("paid order must not wait for confirmation")
    monkeypatch.setattr(steps, "_do_payment_notify_and_wait", lambda *_a, **_k: pytest.fail("paid order must not request payment"))
    amount = steps.lock_and_confirm_payment(client, _item([{"id": "sell", "price": "10.00"}]),
                                            _config(), **kwargs)
    assert amount == 10.0
    assert client.lock_calls == 1
    assert client.lookup_calls == 2
    assert len(purchases) == 1 and purchases[0]["buff_order_id"] == "bill-auto"
    assert purchases[0]["pending_receipt"] is True
    assert client.shipping_calls == [("bill-auto", "csgo")]
    assert pending == []
    assert guard.get_unresolved_checkout() is None


def test_no_link_and_unpaid_retains_checkout_and_never_rebuys(isolated):
    client = AutoClient(paid=False)
    kwargs, _, purchases = _checkout_args()
    with pytest.raises(steps.PurchaseOrderCreatedPending):
        steps.lock_and_confirm_payment(client, _item([{"id": "sell", "price": "10.00"}]),
                                       _config(), **kwargs)
    assert client.lock_calls == 1 and client.lookup_calls == 4
    assert not purchases and not client.shipping_calls
    assert guard.get_unresolved_checkout()["order_id"] == "bill-auto"


def test_manual_payment_link_keeps_existing_confirmation_flow(isolated):
    client = AutoClient(paid=False, pay_url="https://pay.example/manual")
    kwargs, pending, purchases = _checkout_args(wait_result=True)
    assert steps.lock_and_confirm_payment(client, _item([{"id": "sell", "price": "10.00"}]),
                                          _config(), **kwargs) == 10.0
    assert pending[0]["pay_url"] == "https://pay.example/manual"
    assert len(purchases) == 1


def test_verification_after_creation_preserves_order_identity(isolated):
    client = AutoClient(blocked=True)
    kwargs, _, purchases = _checkout_args()
    with pytest.raises(BuffVerificationRequired):
        steps.lock_and_confirm_payment(client, _item([{"id": "sell", "price": "10.00"}]),
                                       _config(), **kwargs)
    assert not purchases and client.lock_calls == 1
    assert guard.get_unresolved_checkout()["order_id"] == "bill-auto"


def test_paid_record_is_durable_even_when_user_stops_during_lookup(isolated):
    client = AutoClient()
    kwargs, _, purchases = _checkout_args()
    kwargs["is_stop_requested"] = lambda: client.lookup_calls >= 2
    assert steps.lock_and_confirm_payment(client, _item([{"id": "sell", "price": "10.00"}]),
                                          _config(), **kwargs) == 10.0
    assert len(purchases) == 1


def test_recovery_does_not_duplicate_a_purchase_record(isolated, monkeypatch):
    client = AutoClient()
    kwargs, _, purchases = _checkout_args()
    monkeypatch.setattr(steps, "get_purchases", lambda: [
        {"buff_order_id": "bill-auto", "goods_id": 123, "price": 10.0}])
    assert steps.lock_and_confirm_payment(client, _item([{"id": "sell", "price": "10.00"}]),
                                          _config(), **kwargs) == 10.0
    assert purchases == []


def test_rejected_seller_offer_keeps_paid_order_and_allows_next_checkout(isolated):
    class AppOnlyClient(AutoClient):
        def ask_seller_to_send(self, order_id, game):
            return False

        def get_last_offer_error(self, order_id):
            return "部分卖家不接受卖家发报价，请在5分钟内前往APP订单详情手动发送报价"

    client = AppOnlyClient()
    kwargs, _, purchases = _checkout_args()
    item = _item([{"id": "sell", "price": "10.00", "user_id": "app-only-seller"}])
    assert steps.lock_and_confirm_payment(client, item, _config(), **kwargs) == 10.0
    assert len(purchases) == 1 and client.lock_calls == 1
    assert purchases[0]["pending_receipt"] is True
    assert guard.get_unresolved_checkout() is None
    assert offer_support.filter_automatable_sellers(item["_buff_sell_orders"]) == []
    class NextClient(AutoClient):
        def lock_and_get_pay_url(self, *args, on_created=None, **kwargs):
            result = super().lock_and_get_pay_url(*args, **kwargs)
            result["order_id"] = "next-bill"
            if on_created:
                on_created("next-bill")
            return result

    next_client = NextClient()
    isolated_purchases = list(purchases)
    assert steps.lock_and_confirm_payment(next_client, _item([
        {"id": "other-sell", "price": "10.00", "user_id": "other-seller"}
    ]), _config(), **kwargs) == 10.0
    assert next_client.lock_calls == 1 and guard.get_unresolved_checkout() is None
    assert purchases[0] == isolated_purchases[0]
    assert purchases[1]["buff_order_id"] == "next-bill"


def test_known_app_only_seller_is_skipped_before_another_charge(isolated):
    offer_support.remember_app_only_seller("app-only-seller", "old-bill", "不接受卖家发报价")
    client = AutoClient()
    kwargs, _, purchases = _checkout_args()
    item = _item([{"id": "sell", "price": "10.00", "user_id": "app-only-seller"}])
    steps.lock_and_confirm_payment(client, item, _config(), **kwargs)
    assert client.lock_calls == 0 and not purchases
    assert guard.get_unresolved_checkout() is None


def test_transient_offer_error_does_not_exclude_a_seller(isolated):
    offer_support.remember_app_only_seller("seller", "bill", "暂时不可用")
    orders = [{"user_id": "seller"}]
    assert offer_support.filter_automatable_sellers(orders) == orders


def test_corrupted_seller_registry_stops_purchase_before_post(isolated):
    offer_support._PATH.write_text("[]")
    client = AutoClient()
    kwargs, _, purchases = _checkout_args()
    with pytest.raises(BuffRequestBlocked):
        steps.lock_and_confirm_payment(client, _item([{"id": "sell", "price": "10.00"}]),
                                       _config(), **kwargs)
    assert client.lock_calls == 0 and not purchases


def test_verified_existing_offer_is_not_sent_again(isolated):
    client = AutoClient()
    kwargs, _, purchases = _checkout_args()
    checkout = guard.begin_checkout("single", 123, quantity=1, price=10)
    steps._do_wait_payment_and_append(
        client, {"name": "Test Item"}, _config(), 10, 1, 123, "", "alipay",
        "bill-auto", checkout["intent_id"], 0, kwargs["set_pending_payment"],
        kwargs["wait_payment_confirm"], kwargs["confirm_payment"],
        kwargs["is_stop_requested"], kwargs["append_purchase"], None, "csgo",
        payment_verified=True, shipping_verified=True,
    )
    assert len(purchases) == 1 and client.shipping_calls == []
    assert guard.get_unresolved_checkout() is None


@pytest.mark.parametrize("paid,closed,offer_started,manual_required", [
    (True, False, False, False), (False, False, False, False), (False, True, False, False),
    (True, False, False, True), (True, False, True, True),
])
def test_reconcile_existing_checkout_verifies_before_recording(
    isolated, monkeypatch, paid, closed, offer_started, manual_required,
):
    from app.services import buff_reconciliation as recovery
    from app import pipeline
    from test_buff_purchase_flow_safety import _FakeState

    state = _FakeState()
    monkeypatch.setattr(recovery, "get_state", lambda: state)
    monkeypatch.setattr(recovery, "get_purchases", lambda: state.purchases)
    monkeypatch.setattr(steps, "get_purchases", lambda: state.purchases)
    monkeypatch.setattr(recovery, "load_app_config_validated", _config)
    monkeypatch.setattr(recovery, "get_buff_credentials", lambda: {})
    monkeypatch.setattr(recovery, "log", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline, "is_pipeline_running", lambda: False)
    monkeypatch.setattr(pipeline, "is_shutdown_pending", lambda: False)

    class RecoveryClient(AutoClient):
        def get_order_payment(self, *_args):
            return {"paid": paid, "closed": closed, "state": "FAIL" if closed else "PAYING",
                    "offer_started": offer_started,
                    "state_text": "购买失败-已退款" if closed else "等待你发起报价",
                    "market_hash_name": "Test Item"}

        def close(self):
            pass

    client = RecoveryClient()
    monkeypatch.setattr(recovery, "create_buff_client_from_config", lambda *_: client)
    checkout = guard.begin_checkout("single", 123, quantity=1, price=10.0, sell_order_id="sell")
    guard.update_checkout(order_id="bill-auto",
                          stage="manual_offer_required" if manual_required else "order_created_pending")
    assert recovery.reconcile_single_payment("stale-intent")["ok"] is False
    result = recovery.reconcile_single_payment(checkout["intent_id"])
    can_record = paid
    assert result["ok"] is (can_record or closed)
    assert len(state.purchases) == int(can_record)
    assert client.lock_calls == 0
    if can_record:
        assert state.purchases[0]["buff_order_id"] == "bill-auto"
        assert guard.get_unresolved_checkout() is None
        assert recovery.reconcile_single_payment(checkout["intent_id"])["ok"] is False
        assert len(state.purchases) == 1
        if offer_started or manual_required or result.get("offer_deferred"):
            assert not client.shipping_calls
    elif closed:
        assert guard.get_unresolved_checkout() is None
        assert not client.shipping_calls and result["recorded"] is False
    else:
        assert guard.get_unresolved_checkout()["order_id"] == "bill-auto"
        assert not client.shipping_calls
