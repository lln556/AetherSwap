"""Remember sellers who explicitly require the BUFF app to send offers."""

import json
import os
from pathlib import Path
import tempfile
import threading
import time

from buff import BuffRequestBlocked
from app.config_loader import get_steam_credentials

_PATH = Path(__file__).resolve().parents[2] / "config" / "buff_offer_rejections.json"
_LOCK = threading.RLock()


def _read() -> dict:
    if not _PATH.exists():
        return {}
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid seller registry")
        return data
    except (OSError, ValueError) as exc:
        raise BuffRequestBlocked("卖家报价能力记录无法读取，暂停自动购买") from exc


def remember_app_only_seller(
    seller_id: str, order_id: str, reason: str, *, buyer_offer_failed: bool = False,
) -> None:
    seller_id = str(seller_id or "").strip()
    if not seller_id or "不接受卖家发报价" not in reason:
        return
    with _LOCK:
        data = _read()
        prior = data.get(seller_id) or {}
        data[seller_id] = {"order_id": str(order_id), "reason": reason, "at": time.time(),
                           "buyer_offer_failed": buyer_offer_failed or bool(prior.get("buyer_offer_failed"))}
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=_PATH.parent, prefix=".buff-offer-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, _PATH)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def buyer_offer_available(config: dict) -> bool:
    if (config.get("buff") or {}).get("auto_send_buyer_offer") is not True:
        return False
    from buff.buyer_offer import buyer_session
    from app.services.steam_auth import steam_cookie_expiry
    creds = get_steam_credentials() or {}
    try:
        buyer_session(creds.get("cookies") or "", str(creds.get("steam_id") or ""))
        expiry = steam_cookie_expiry(creds.get("cookies") or "")
        if expiry is not None and expiry <= time.time() + 60:
            return False
        import cryptography  # noqa: F401 - verify the optional capability before checkout
        return True
    except (ValueError, ImportError):
        return False


def try_send_buyer_offer(client, purchase: dict, config: dict, reason: str, log_fn=None) -> bool:
    """Fallback only for an explicit buyer-offer request, never a generic error."""
    reason = str(reason or "")
    if not any(s in reason for s in ("不接受卖家发报价", "手动发送报价", "等待你发起报价")):
        return False
    sender = getattr(client, "send_buyer_offer", None)
    if not callable(sender) or not buyer_offer_available(config):
        return False
    creds = get_steam_credentials() or {}
    result = sender(
        (config.get("buff") or {}).get("game", "csgo"),
        str(purchase.get("buff_order_id") or ""), int(purchase.get("goods_id") or 0),
        str(purchase.get("price") or ""), creds.get("cookies") or "",
        str(creds.get("steam_id") or ""), str(purchase.get("name") or ""),
    )
    submitted = isinstance(result, dict) and result.get("submitted") is True
    if log_fn:
        order_id = purchase.get("buff_order_id")
        if submitted:
            msg = "报价已开始，无需重复发起" if result.get("already_started") else "已提交买家报价，等待 BUFF 确认并由卖家接受"
            log_fn(f"[Buff 自动报价] 订单 {order_id}：{msg}", "info")
        else:
            msg = result.get("reason") if isinstance(result, dict) else "返回格式异常，请核对订单"
            log_fn(f"[Buff 自动报价] 订单 {order_id}：{msg}", "warn")
    return submitted


def filter_automatable_sellers(orders: list, *, allow_buyer_offer: bool = False) -> list:
    with _LOCK:
        rejected = _read()
    allowed = []
    for order in orders:
        seller_id = str(order.get("user_id") or "").strip()
        prior = rejected.get(seller_id)
        if seller_id not in rejected or (
            allow_buyer_offer and isinstance(prior, dict) and not prior.get("buyer_offer_failed")
        ):
            allowed.append(order)
    return allowed
