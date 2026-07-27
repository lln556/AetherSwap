import hashlib
import threading
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from app.services.buff_auth import get_buff_auth_lock
from buff import (
    BuffBuyer,
    BuffWriteResultUnknown,
    PAY_METHOD_ALIPAY,
    PAY_METHOD_WECHAT,
)


logger = logging.getLogger(__name__)
buff_timeout = 15
BUFF_ACCOUNT_ID = "default"


def count_lowest_price_orders(orders: List[dict]) -> Tuple[float, int]:
    if not orders:
        return 0.0, 0
    lowest = float(orders[0].get("price", 0))
    if lowest <= 0:
        return 0.0, 0
    count = 0
    for order in orders:
        try:
            price = float(order.get("price", 0))
        except (ValueError, TypeError):
            continue
        if abs(price - lowest) < 1e-6:
            count += 1
        elif price < lowest:
            lowest = price
            count = 1
    return lowest, count


def first_order_at_price(orders: List[dict], price: float) -> Optional[dict]:
    for order in orders:
        try:
            order_price = float(order.get("price", 0))
        except (ValueError, TypeError):
            continue
        if abs(order_price - price) < 1e-6:
            return order
    return None


class BuffClient:
    """Generation-aware BUFF facade.

    One authentication lock covers each logical operation so browser login,
    keepalive and checkout cannot mutate the same account session concurrently.
    Non-idempotent writes intentionally have no generic retry decorator.
    """

    def __init__(
        self,
        cookies: str,
        pay_method: str = "alipay",
        timeout_sec: int = buff_timeout,
        *,
        user_agent: Optional[str] = None,
        credential_generation: int = 0,
        credentials_provider: Optional[Callable[[], dict]] = None,
        credentials_update_callback: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._pay_method = (pay_method or "alipay").strip().lower()
        self._pay_method_id = (
            PAY_METHOD_WECHAT if self._pay_method == "wechat" else PAY_METHOD_ALIPAY
        )
        self._timeout = timeout_sec
        self._credentials_provider = credentials_provider
        self._credentials_update_callback = credentials_update_callback
        self._credential_generation = self._as_generation(credential_generation)
        self._cookies = cookies or ""
        self._user_agent = (user_agent or "").strip() or None
        self._client_lock = threading.RLock()
        self._auth_lock = get_buff_auth_lock()
        self._buyer = self._new_buyer(self._cookies, self._user_agent)

    @staticmethod
    def _as_generation(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _new_buyer(self, cookies: str, user_agent: Optional[str]) -> BuffBuyer:
        return BuffBuyer(
            cookies,
            pay_method=self._pay_method_id,
            user_agent=user_agent,
            account_id=BUFF_ACCOUNT_ID,
            request_timeout=self._timeout,
        )

    def _ensure_current_buyer(self) -> BuffBuyer:
        if self._credentials_provider is None:
            return self._buyer
        credentials = self._credentials_provider() or {}
        generation = self._as_generation(credentials.get("generation"))
        cookies = str(credentials.get("cookies") or "")
        user_agent = str(credentials.get("user_agent") or "").strip() or None
        if (
            generation == self._credential_generation
            and cookies == self._cookies
            and user_agent == self._user_agent
        ):
            return self._buyer

        old_buyer = self._buyer
        self._buyer = self._new_buyer(cookies, user_agent)
        self._cookies = cookies
        self._user_agent = user_agent
        self._credential_generation = generation
        close = getattr(old_buyer, "close", None)
        if callable(close):
            close()
        return self._buyer

    def _persist_rotated_cookies(self, buyer: BuffBuyer) -> None:
        if self._credentials_update_callback is None:
            return
        latest = buyer.export_cookie_string()
        if not latest or latest == self._cookies:
            return
        self._credentials_update_callback(latest, buyer.user_agent)
        self._cookies = latest
        if self._credentials_provider is not None:
            current = self._credentials_provider() or {}
            self._credential_generation = self._as_generation(
                current.get("generation")
            )

    def get_credential_identity(self) -> Dict[str, Any]:
        """Return a non-secret identity for checkout continuity checks."""

        with self._auth_lock:
            with self._client_lock:
                if self._credentials_provider is not None:
                    credentials = self._credentials_provider() or {}
                    generation = self._as_generation(credentials.get("generation"))
                    cookies = str(credentials.get("cookies") or "")
                    user_agent = (
                        str(credentials.get("user_agent") or "").strip() or None
                    )
                else:
                    generation = self._credential_generation
                    cookies = self._cookies
                    user_agent = self._user_agent
                digest = hashlib.sha256(
                    f"{cookies}\0{user_agent or ''}".encode(
                        "utf-8", errors="replace"
                    )
                ).hexdigest()
                return {
                    "credential_generation": generation,
                    "credential_fingerprint": digest,
                }

    def _run(self, operation: Callable[[BuffBuyer], Any]) -> Any:
        with self._auth_lock:
            with self._client_lock:
                buyer = self._ensure_current_buyer()
                try:
                    return operation(buyer)
                finally:
                    try:
                        self._persist_rotated_cookies(buyer)
                    except Exception as exc:
                        # Cookie persistence is important but must never replace
                        # a rate-limit/risk/write-unknown exception from BUFF.
                        logger.exception("持久化 BUFF 轮换 Cookie 失败: %s", exc)

    def close(self) -> None:
        with self._client_lock:
            close = getattr(self._buyer, "close", None)
            if callable(close):
                close()

    def get_sell_orders(self, goods_id: int, game: str = "csgo") -> Optional[list]:
        return self._run(lambda buyer: buyer.get_sell_orders(goods_id, game))

    def verify_session(self, game: str = "csgo") -> bool:
        return bool(self._run(lambda buyer: buyer.verify_session(game)))

    def get_steam_trades(self) -> Optional[list]:
        return self._run(lambda buyer: buyer.get_steam_trades())

    def get_goods_steam_price_cny(
        self, search_name: str, game: str = "csgo"
    ) -> Optional[float]:
        return self._run(
            lambda buyer: buyer.get_goods_steam_price_cny(search_name, game)
        )

    def ask_seller_to_send(
        self, bill_order_id_or_ids: Union[str, List[str]], game: str = "csgo"
    ) -> bool:
        return self._run(
            lambda buyer: buyer.ask_seller_to_send(bill_order_id_or_ids, game)
        )

    def lock_and_get_pay_url(
        self,
        game: str,
        goods_id: int,
        sell_order_id: str,
        price: str,
        *,
        on_created: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        return self._run(
            lambda buyer: buyer.lock_and_get_pay_url(
                game,
                goods_id,
                sell_order_id,
                price,
                on_created=on_created,
            )
        )

    def try_batch_buy(
        self,
        goods_id: int,
        game: str,
        orders: List[dict],
        unit_price: float,
        num: int,
        *,
        on_created: Optional[Callable[[str], None]] = None,
    ) -> Optional[Dict[str, Any]]:
        del orders  # The live order snapshot is validated during finalization.
        if num < 1 or self._pay_method_id != PAY_METHOD_WECHAT:
            return {
                "success": False,
                "code": "NOT_SUPPORTED",
                "created": False,
                "safe_to_fallback": True,
            }

        def operation(buyer: BuffBuyer) -> Dict[str, Any]:
            batch_id = buyer.batch_buy_create(goods_id, unit_price, num, game)
            if not batch_id:
                # A POST was attempted. Without an idempotency key or order
                # reconciliation endpoint its server-side result is unknown.
                return {
                    "success": False,
                    "code": "UNKNOWN_AFTER_SEND",
                    "created": None,
                }
            if on_created is not None:
                try:
                    on_created(batch_id)
                except Exception as exc:
                    from buff import BuffWriteResultUnknown

                    error = BuffWriteResultUnknown(
                        "BUFF 批次已创建，但批次号未能写入本地对账门禁",
                        method="POST",
                    )
                    error.batch_id = str(batch_id)
                    raise error from exc
            try:
                pay_url = buyer.batch_buy_wx_qrcode(batch_id, game)
            except Exception as exc:
                # The batch id proves the write committed.  Any later read
                # failure is a created checkout, never a reason to create a
                # second batch after re-authentication.
                return {
                    "success": False,
                    "code": "CREATED_WITHOUT_PAY_URL",
                    "created": True,
                    "batch_id": batch_id,
                    "msg": str(exc) or type(exc).__name__,
                }
            if not pay_url:
                return {
                    "success": False,
                    "code": "CREATED_WITHOUT_PAY_URL",
                    "created": True,
                    "batch_id": batch_id,
                }
            return {
                "success": True,
                "pay_url": pay_url,
                "pay_type": "wechat",
                "batch_id": batch_id,
                "unit_price": unit_price,
                "num": num,
                "total_price": unit_price * num,
                "created": True,
            }

        return self._run(operation)

    def batch_buy_find_and_finalize(
        self,
        goods_id: int,
        game: str,
        max_price: float,
        num: int,
        batch_id: str,
        *,
        on_match: Optional[
            Callable[[Dict[str, Any], List[Dict[str, Any]]], None]
        ] = None,
    ) -> List[Dict[str, Any]]:
        def operation(buyer: BuffBuyer) -> List[Dict[str, Any]]:
            matched: List[Dict[str, Any]] = []
            seen_sell_order_ids = set()
            seen_bill_order_ids = set()
            try:
                orders = buyer.get_sell_orders(goods_id, game)
            except Exception as exc:
                setattr(exc, "partial_results", list(matched))
                setattr(exc, "batch_id", str(batch_id))
                raise
            if not orders:
                return []
            for order in orders:
                if len(matched) >= num:
                    break
                sell_order_id = str(order.get("id") or "").strip()
                if (
                    not sell_order_id
                    or sell_order_id == "0"
                    or sell_order_id in seen_sell_order_ids
                ):
                    continue
                try:
                    price = float(order.get("price", 0))
                except (ValueError, TypeError):
                    continue
                if price <= max_price:
                    seen_sell_order_ids.add(sell_order_id)
                    try:
                        bill_order_id = buyer.batch_buy_finalize(
                            game,
                            goods_id,
                            sell_order_id,
                            str(order.get("price", "")),
                            batch_id,
                        )
                    except Exception as exc:
                        # Preserve every already-created bill for immediate
                        # persistence by the pipeline before it halts.
                        setattr(exc, "partial_results", list(matched))
                        setattr(exc, "batch_id", str(batch_id))
                        raise
                    if bill_order_id:
                        normalized_bill_id = str(bill_order_id).strip()
                        if (
                            not normalized_bill_id
                            or normalized_bill_id == "0"
                            or normalized_bill_id in seen_bill_order_ids
                        ):
                            error = BuffWriteResultUnknown(
                                "BUFF 批量核销返回了空值或重复订单号，无法确认完整件数",
                                method="POST",
                            )
                            error.partial_results = list(matched)
                            error.batch_id = str(batch_id)
                            raise error
                        seen_bill_order_ids.add(normalized_bill_id)
                        match = {
                            "id": sell_order_id,
                            "price": price,
                            "bill_order_id": normalized_bill_id,
                        }
                        matched.append(match)
                        if on_match is not None:
                            try:
                                # Persist the newly-created external id before
                                # attempting another non-idempotent finalize.
                                on_match(dict(match), list(matched))
                            except Exception as exc:
                                setattr(exc, "partial_results", list(matched))
                                setattr(exc, "batch_id", str(batch_id))
                                raise
                    else:
                        # A definitive non-OK response may be safe for this
                        # sell order, but continuing through more POSTs in the
                        # same paid batch is not.  Return the known partial set
                        # and require reconciliation.
                        break
            return matched

        return self._run(operation)


def create_buff_client_from_config(credentials: dict, config: dict) -> BuffClient:
    from app.config_loader import get_buff_credentials, update_buff_creds

    credentials = credentials or {}
    buff_cfg = config.get("buff", {})

    def persist_rotated_cookies(cookies: str, user_agent: str) -> None:
        # Preserve the authentication source; only the server-issued CookieJar
        # and the UA bound to it are refreshed here.
        update_buff_creds(cookies, user_agent=user_agent)

    return BuffClient(
        str(credentials.get("cookies") or ""),
        pay_method=buff_cfg.get("pay_method", "alipay"),
        timeout_sec=buff_timeout,
        user_agent=str(credentials.get("user_agent") or "").strip() or None,
        credential_generation=credentials.get("generation", 0),
        credentials_provider=get_buff_credentials,
        credentials_update_callback=persist_rotated_cookies,
    )
