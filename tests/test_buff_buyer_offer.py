import base64
import copy
import json
import time

import pytest
import requests

from buff import BuffRequestBlocked, BuffWriteResultUnknown
from buff import buyer_offer
from buff.buyer import API_BUYER_SEND_OFFER, BuffBuyer

STEAM_ID = "76561198000000001"
COOKIE = f"sessionid=session-test; steamLoginSecure={STEAM_ID}%7C%7Ctest-token; other=private"


@pytest.fixture(autouse=True)
def isolated_journal(monkeypatch, tmp_path):
    monkeypatch.setattr(buyer_offer, "_JOURNAL_PATH", tmp_path / "buyer-offers.json")


def order(**changes):
    now = time.time()
    result = {
        "id": "bill-1", "game": "csgo", "goods_id": 123, "price": "2.10",
        "buyer_steamid": STEAM_ID, "buyer_pay_time": now - 5, "created_at": now - 10,
        "state": "PAYING", "state_text": "等待你发起报价", "pay_expire_timeout": -1,
        "buyer_send_offer_timeout": 240, "has_sent_offer": False,
        "_market_hash_name": "Test Item", "error_text": None,
    }
    result.update(changes)
    return result


def client(monkeypatch, *, orders=None, response=None, exception=None):
    buyer = object.__new__(BuffBuyer)
    calls = []
    history = [order()] if orders is None else orders
    monkeypatch.setattr(buyer, "get_purchase_history_orders", lambda *_: copy.deepcopy(history))

    def send(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if exception:
            raise exception
        return response if response is not None else {"code": "OK", "data": {"bill-1": "OK"}}

    monkeypatch.setattr(buyer, "_make_request", send)
    return buyer, calls


def send(buyer, **changes):
    args = dict(game="csgo", order_id="bill-1", goods_id=123, price="2.10",
                steam_cookies=COOKIE, steam_id=STEAM_ID, market_hash_name="Test Item")
    args.update(changes)
    return buyer.send_buyer_offer(**args)


def test_envelope_round_trip_and_fresh_randomness(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    public = private.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setattr(buyer_offer, "_PUBLIC_KEY", base64.b64encode(public).decode())
    cookie = buyer_offer.buyer_session(COOKIE, STEAM_ID)
    assert "other" not in cookie
    a, b = buyer_offer.encrypt_buyer_info(cookie), buyer_offer.encrypt_buyer_info(cookie)
    assert a != b and cookie not in a
    payload = base64.b64decode(a)
    key = private.decrypt(payload[:512], PKCS1v15())
    decryptor = Cipher(algorithms.AES(key), modes.CBC(payload[512:528])).decryptor()
    padded = decryptor.update(payload[528:]) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    assert (unpadder.update(padded) + unpadder.finalize()).decode() == cookie


def test_exact_paid_order_is_submitted_once_without_plaintext(monkeypatch):
    buyer, calls = client(monkeypatch)
    assert send(buyer)["submitted"] is True
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert (method, url) == ("POST", API_BUYER_SEND_OFFER)
    assert kwargs["allow_redirects"] is False
    body = json.loads(kwargs["data"])
    assert body["bill_orders"] == ["bill-1"]
    assert set(body) == {"bill_orders", "buyer_info"}
    assert "test-token" not in kwargs["data"]
    persisted = buyer_offer._JOURNAL_PATH.read_text()
    assert "test-token" not in persisted and "session-test" not in persisted
    assert json.loads(persisted)["bill-1"]["state"] == "submitted"
    # A new adapter object models a process restart; the durable claim wins.
    another, repeated = client(monkeypatch)
    assert send(another)["submitted"] is False
    assert repeated == []


@pytest.mark.parametrize("changes", [
    {"buyer_pay_time": 0}, {"buyer_pay_time": True}, {"buyer_pay_time": "nan"},
    {"buyer_pay_time": time.time() + 3600}, {"created_at": 0},
    {"goods_id": 456}, {"goods_id": True}, {"price": "2.11"}, {"price": "nan"},
    {"game": "dota2"}, {"buyer_steamid": "76561198000000002"},
    {"_market_hash_name": "Other Item"}, {"error_text": "买家发送报价超时"},
    {"state": "FAIL"}, {"state": "CANCELLED"}, {"state": "UNKNOWN"},
    {"buyer_send_offer_timeout": 0}, {"buyer_send_offer_timeout": -1},
    {"buyer_send_offer_timeout": True}, {"buyer_send_offer_timeout": "nan"},
    {"pay_expire_timeout": 100},
])
def test_unpaid_closed_expired_or_mismatched_orders_never_post(monkeypatch, changes):
    buyer, calls = client(monkeypatch, orders=[order(**changes)])
    assert send(buyer)["submitted"] is False
    assert not calls and not buyer_offer._JOURNAL_PATH.exists()


@pytest.mark.parametrize("changes", [
    {"has_sent_offer": True}, {"tradeofferid": "offer-existing"},
    {"is_seller_asked_to_send_offer": True}, {"state": "DELIVERING"}, {"state": "SUCCESS"},
])
def test_existing_delivery_is_not_repeated(monkeypatch, changes):
    buyer, calls = client(monkeypatch, orders=[order(**changes)])
    assert send(buyer) == {"submitted": True, "already_started": True}
    assert calls == []


@pytest.mark.parametrize("history", [[], [order(), order()], [order(id="other-bill")]])
def test_missing_or_ambiguous_order_identity_is_rejected(monkeypatch, history):
    buyer, calls = client(monkeypatch, orders=history)
    assert send(buyer)["submitted"] is False and calls == []


def test_wrong_steam_cookie_owner_does_not_even_query_buff(monkeypatch):
    buyer, calls = client(monkeypatch)
    monkeypatch.setattr(buyer, "get_purchase_history_orders", lambda *_: pytest.fail("account mismatch"))
    assert send(buyer, steam_id="76561198000000002")["submitted"] is False
    assert calls == []


def test_unknown_write_result_is_durable_and_never_retried(monkeypatch):
    buyer, calls = client(monkeypatch, exception=requests.Timeout("after send"))
    with pytest.raises(BuffWriteResultUnknown):
        send(buyer)
    assert len(calls) == 1
    another, repeated = client(monkeypatch)
    assert send(another)["submitted"] is False and repeated == []
    assert json.loads(buyer_offer._JOURNAL_PATH.read_text())["bill-1"]["state"] == "sending_or_unknown"


@pytest.mark.parametrize("response", [
    {"code": "Rejected", "data": {}}, {"code": "OK", "data": {"bill-1": "拒绝"}},
])
def test_rejection_is_not_mistaken_for_success_or_retried(monkeypatch, response):
    buyer, calls = client(monkeypatch, response=response)
    assert send(buyer)["submitted"] is False
    assert send(buyer)["submitted"] is False
    assert len(calls) == 1


def test_corrupted_journal_blocks_post(monkeypatch):
    buyer_offer._JOURNAL_PATH.write_text('{"broken": true}')
    buyer, calls = client(monkeypatch)
    with pytest.raises(BuffRequestBlocked):
        send(buyer)
    assert calls == []


def test_parallel_claims_allow_only_one_sender():
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: buyer_offer.claim_buyer_offer("bill-1", STEAM_ID), range(12)))
    assert sum(results) == 1


@pytest.mark.parametrize("outcome", ["submitted", "unknown", "rejected"])
def test_checkout_falls_back_after_seller_rejection(monkeypatch, outcome):
    from app import pipeline_steps as steps
    from app.services import buff_checkout_guard as guard, buff_offer_support as support
    from test_buff_automatic_payment import AutoClient
    from test_buff_purchase_flow_safety import _config, _item, _checkout_args

    monkeypatch.setattr(guard, "_GUARD_PATH", buyer_offer._JOURNAL_PATH.with_name("checkout.json"))
    monkeypatch.setattr(support, "_PATH", buyer_offer._JOURNAL_PATH.with_name("sellers.json"))
    monkeypatch.setattr(support, "get_steam_credentials", lambda: {"cookies": COOKIE, "steam_id": STEAM_ID})
    monkeypatch.setattr(steps, "jittered_sleep", lambda *_: None)
    monkeypatch.setattr(steps, "_fetch_smart_market_price", lambda *_a, **_k: 15)
    monkeypatch.setattr(steps, "get_purchases", lambda: [])
    kwargs, _, purchases = _checkout_args()
    attempts = []

    class BuyerClient(AutoClient):
        def ask_seller_to_send(self, order_id, game):
            return False

        def get_last_offer_error(self, order_id):
            return "部分卖家不接受卖家发报价，请手动发送报价"

        def send_buyer_offer(self, *args):
            assert len(purchases) == 1  # Charge must be recorded before sending.
            assert guard.get_unresolved_checkout()["order_id"] == "bill-auto"
            attempts.append(args)
            if outcome == "unknown":
                raise BuffWriteResultUnknown("报价结果未知", method="POST", url=API_BUYER_SEND_OFFER)
            return {"submitted": outcome == "submitted", "reason": "未完成"}

    cfg = _config()
    cfg["buff"]["auto_send_buyer_offer"] = True
    item = _item([{"id": "sell-1", "price": "10.00", "user_id": "seller-1"}])
    if outcome == "unknown":
        with pytest.raises(steps.PurchaseWriteResultUnknown) as caught:
            steps.lock_and_confirm_payment(BuyerClient(), item, cfg, **kwargs)
        assert caught.value.committed_amount == 10
        assert guard.get_unresolved_checkout()["stage"] == "shipping_reminder_unknown"
    else:
        assert steps.lock_and_confirm_payment(BuyerClient(), item, cfg, **kwargs) == 10
        assert guard.get_unresolved_checkout() is None
    assert len(attempts) == 1 and len(purchases) == 1
    assert attempts[0][0:4] == ("csgo", "bill-auto", 123, "10.0")
    if outcome == "rejected":
        assert support.filter_automatable_sellers(item["_buff_sell_orders"], allow_buyer_offer=True) == []
    else:
        assert not support._PATH.exists()


def test_existing_app_only_seller_is_allowed_only_when_buyer_offer_is_available(monkeypatch):
    from app.services import buff_offer_support as support
    monkeypatch.setattr(support, "_PATH", buyer_offer._JOURNAL_PATH.with_name("sellers.json"))
    support.remember_app_only_seller("seller", "bill", "不接受卖家发报价")
    orders = [{"user_id": "seller"}]
    assert support.filter_automatable_sellers(orders) == []
    assert support.filter_automatable_sellers(orders, allow_buyer_offer=True) == orders
    support.remember_app_only_seller("seller", "bill2", "不接受卖家发报价", buyer_offer_failed=True)
    assert support.filter_automatable_sellers(orders, allow_buyer_offer=True) == []


def test_background_reconciliation_submits_an_eligible_missed_offer_once(monkeypatch):
    from app.services import receipt_reconciliation as receipt, workers, buff_offer_support as support
    from app.services import buff_checkout_guard as guard
    buyer, calls = client(monkeypatch)
    pending = [{"_db_id": 1, "buff_order_id": "bill-1", "goods_id": 123,
                "price": 2.1, "name": "Test Item", "pending_receipt": True}]
    monkeypatch.setattr(guard, "_GUARD_PATH", buyer_offer._JOURNAL_PATH.with_name("checkout.json"))
    monkeypatch.setattr(workers, "_buff_background_request_is_safe", lambda: True)
    monkeypatch.setattr(receipt, "get_purchases", lambda: pending)
    monkeypatch.setattr(receipt, "load_app_config_validated", lambda: {"buff": {"auto_send_buyer_offer": True}})
    monkeypatch.setattr(support, "get_steam_credentials", lambda: {"cookies": COOKIE, "steam_id": STEAM_ID})
    monkeypatch.setattr(receipt, "get_steam_credentials", lambda: {"steam_id": STEAM_ID})
    monkeypatch.setattr(receipt, "_archive_closed", lambda _: None)
    monkeypatch.setattr(receipt, "log", lambda *_a, **_k: None)
    # The worker uses the same tested low-level sender; the first GET does not
    # mark any receipt until BUFF/Steam have confirmed the transfer.
    assert receipt.reconcile_receipts_once(buyer)["pending"] == 1
    assert receipt.reconcile_receipts_once(buyer)["pending"] == 1
    assert len(calls) == 1


def test_feature_disabled_or_generic_error_does_not_forward_steam_session(monkeypatch):
    from app.services import buff_offer_support as support
    class NoSend:
        def send_buyer_offer(self, *_args):
            pytest.fail("must not send session")
    monkeypatch.setattr(support, "get_steam_credentials", lambda: {"cookies": COOKIE, "steam_id": STEAM_ID})
    assert not support.try_send_buyer_offer(NoSend(), {}, {}, "等待你发起报价")
    assert not support.try_send_buyer_offer(NoSend(), {}, {"buff": {"auto_send_buyer_offer": True}}, "服务器错误")
