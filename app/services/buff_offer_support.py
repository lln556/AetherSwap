"""Remember sellers who explicitly require the BUFF app to send offers."""

import json
import os
from pathlib import Path
import tempfile
import threading
import time

from buff import BuffRequestBlocked

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


def remember_app_only_seller(seller_id: str, order_id: str, reason: str) -> None:
    seller_id = str(seller_id or "").strip()
    if not seller_id or "不接受卖家发报价" not in reason:
        return
    with _LOCK:
        data = _read()
        data[seller_id] = {"order_id": str(order_id), "reason": reason, "at": time.time()}
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


def filter_automatable_sellers(orders: list) -> list:
    with _LOCK:
        rejected = _read()
    return [order for order in orders
            if str(order.get("user_id") or "").strip() not in rejected]
