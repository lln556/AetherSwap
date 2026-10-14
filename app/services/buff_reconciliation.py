"""Recover a verified paid single checkout without buying it a second time."""

from app.config_loader import get_buff_credentials, load_app_config_validated
from app.services.buff_auth import get_buff_auth_lock
from app.services.buff_checkout_guard import buff_activity_guard, get_unresolved_checkout, resolve_checkout
from app.services.buff_client import create_buff_client_from_config
from app.state import get_purchases, get_state, log


def reconcile_single_payment(intent_id: str) -> dict:
    from app.pipeline import is_pipeline_running, is_shutdown_pending
    from app.pipeline_steps import _do_wait_payment_and_append

    with get_buff_auth_lock():
        with buff_activity_guard():
            if is_pipeline_running() or is_shutdown_pending():
                return {"ok": False, "error": "请等待购买任务停止后再核对已付款订单"}
            checkout = get_unresolved_checkout()
            if not checkout or checkout.get("intent_id") != intent_id:
                return {"ok": False, "error": "订单门禁已变化，请刷新后重试"}
            if checkout.get("kind") != "single" or checkout.get("quantity") != 1:
                return {"ok": False, "error": "此入口仅核对单件订单"}
            order_id = str(checkout.get("order_id") or "")
            goods_id = int(checkout.get("goods_id") or 0)
            price = float(checkout.get("price") or 0)
            if not order_id or goods_id <= 0 or price <= 0:
                return {"ok": False, "error": "订单缺少可核对的身份或金额"}
            cfg = load_app_config_validated()
            game = (cfg.get("buff") or {}).get("game", "csgo")
            client = create_buff_client_from_config(get_buff_credentials(), cfg)
            try:
                proof = client.get_order_payment(game, order_id, goods_id, str(price))
                if proof.get("closed") is True:
                    resolve_checkout("single_order_verified_closed", expected_intent_id=intent_id)
                    get_state().set_status("idle", "ORDER_CLOSED")
                    log(f"[Buff 对账] 已核实订单 {order_id} 已结束: {proof.get('state_text')}",
                        "info", category="buff")
                    return {"ok": True, "order_id": order_id, "closed": True,
                            "state_text": proof.get("state_text"), "recorded": False}
                if proof.get("paid") is not True:
                    return {"ok": False, "error": "BUFF 尚未返回可确认的已付款证据，保留门禁",
                            "order_id": order_id, "state": proof.get("state")}
                defer_offer = (proof.get("offer_started") is not True
                               and (checkout.get("stage") == "manual_offer_required"
                                    or proof.get("state_text") == "等待你发起报价"))
                name = proof.get("market_hash_name") or ""
                if not name:
                    names = {p.get("name") for p in get_purchases()
                             if p.get("goods_id") == goods_id and p.get("name")}
                    if len(names) == 1:
                        name = names.pop()
                if not name:
                    return {"ok": False, "error": "已付款但无法确定物品英文名称，保留门禁"}
                state = get_state()
                amount = _do_wait_payment_and_append(
                    client, {"name": name, "steam_market_name": name}, cfg,
                    price, 1, goods_id, "", "alipay", order_id, intent_id, 0,
                    state.set_pending_payment, state.wait_payment_confirm,
                    state.confirm_payment, state.is_stop_requested,
                    state.append_purchase,
                    lambda msg, level: log(msg, level, category="buff"),
                    game, sell_order_id=str(checkout.get("sell_order_id") or ""),
                    payment_verified=True,
                    shipping_verified=proof.get("offer_started") is True,
                    defer_offer=defer_offer,
                )
                state.set_status("idle", "PAYMENT_RECONCILED")
                log(f"[Buff 对账] 已核实并记录已付款订单 {order_id}，金额={amount:.2f}",
                    "info", category="buff")
                return {"ok": True, "order_id": order_id, "amount": amount,
                        "pending_receipt": True, "offer_deferred": defer_offer}
            finally:
                client.close()
