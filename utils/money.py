import json
import math
import re
import time
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Dict, List, Optional, Tuple
STEAM_FEE_FACTOR = 1.15
USD_TO_CNY_DEFAULT = 7.2
CURRENCY_CNY = "CNY"
CURRENCY_USD = "USD"
_EXCHANGE_RATE_FILE = Path(__file__).resolve().parent.parent / "config" / "exchange_rate.json"
_EXCHANGE_RATE_TTL = 300
_exchange_rate_cache: Tuple[float, Dict[str, float]] = (0.0, {})
g_rgWalletInfo = {
    "rwgrsn": -2,
    "success": True,
    "wallet_balance": "14669",
    "wallet_country": "CN",
    "wallet_currency": 23,
    "wallet_currency_increment": "1",
    "wallet_delayed_balance": "356",
    "wallet_fee": "1",
    "wallet_fee_base": "0",
    "wallet_fee_minimum": "7",
    "wallet_fee_percent": "0.05",
    "wallet_market_minimum": "7",
    "wallet_max_balance": "1400000",
    "wallet_publisher_fee_percent_default": "0.10",
    "wallet_state": "",
    "wallet_trade_max_balance": "1260000"
}


def _exact_fee(base_amt: int, pct: float, fee_minimum: int) -> int:
    """Percentage fee floored in exact decimal arithmetic (no float drift)."""
    if pct is None or float(pct) <= 0:
        return 0
    amount = Decimal(int(base_amt)) * Decimal(str(pct))
    return max(int(fee_minimum), int(amount.to_integral_value(rounding=ROUND_FLOOR)))


def calculate_fee(base_amt: int, pct: float, rg_wallet: dict) -> int:
    return _exact_fee(base_amt, pct, int(rg_wallet['wallet_fee_minimum']))


def buyer_total_minor(seller_minor: int, *, fee_minimum: int = 7,
                      publisher_pct: float = 0.10, steam_pct: float = 0.05) -> int:
    """Total the buyer pays when the seller receives ``seller_minor``."""
    seller = int(seller_minor)
    return (seller + _exact_fee(seller, publisher_pct, fee_minimum)
            + _exact_fee(seller, steam_pct, fee_minimum))


def seller_for_budget_minor(buyer_minor: int, *, fee_minimum: int = 7,
                            market_minimum: int = 1, increment: int = 1,
                            publisher_pct: float = 0.10,
                            steam_pct: float = 0.05) -> int:
    """Largest valid seller price whose buyer total stays within ``buyer_minor``."""
    budget = int(buyer_minor)
    step = max(1, int(increment))
    floor_price = max(1, int(market_minimum))

    def affordable(price: int) -> bool:
        return buyer_total_minor(price, fee_minimum=fee_minimum,
                                 publisher_pct=publisher_pct,
                                 steam_pct=steam_pct) <= budget

    if not affordable(floor_price):
        return floor_price
    low, high = 0, max(0, (budget - floor_price) // step)
    while low < high:
        mid = (low + high + 1) // 2
        if affordable(floor_price + mid * step):
            low = mid
        else:
            high = mid - 1
    return floor_price + low * step


def get_total_with_fees(base_amt: int, ppct: float, spct: float, rg_wallet: dict) -> int:
    n_base = base_amt
    n_pub_fee = calculate_fee(base_amt, ppct, rg_wallet)
    n_steam_fee = calculate_fee(base_amt, spct, rg_wallet)
    return n_base + n_pub_fee + n_steam_fee


def to_valid_market_price(n_price: int, rg_wallet: dict) -> int:
    n_floor = int(rg_wallet['wallet_market_minimum'])
    n_increment = int(rg_wallet['wallet_currency_increment'])
    if n_price <= n_floor:
        return n_floor
    if n_increment > 1:
        d_amount = n_price / n_increment
        d_sign = -1 if d_amount < 0 else 1
        d_amount = (d_sign * math.floor(abs(d_amount) + 0.5)) * n_increment
        return int(d_amount)
    return n_price


def get_item_price_from_total(n_total: int, rg_wallet: dict) -> int:
    return seller_for_budget_minor(
        n_total,
        fee_minimum=int(rg_wallet['wallet_fee_minimum']),
        market_minimum=int(rg_wallet['wallet_market_minimum']),
        increment=max(1, int(rg_wallet.get('wallet_currency_increment') or 1)),
        publisher_pct=float(rg_wallet['wallet_publisher_fee_percent_default']),
        steam_pct=float(rg_wallet['wallet_fee_percent']),
    )


def usd_to_cny(amount: float, rate: float = USD_TO_CNY_DEFAULT) -> float:
    return amount * rate
def parse_price_text(raw: str) -> Optional[float]:
    """Parse the first amount in a price string.

    Only the first numeric token is read, so dual-amount cells such as
    "¥ 1.15 (¥ 1.00)" yield 1.15 instead of concatenating both numbers.
    Handles "1,234.56" and "1.234,56" separator conventions.
    """
    match = re.search(r"\d+(?:[.,]\d+)*", str(raw or ""))
    if not match:
        return None
    token = match.group(0)
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        if re.fullmatch(r"\d{1,3}(,\d{3})+", token):
            token = token.replace(",", "")
        else:
            token = token.replace(",", ".")
    try:
        value = float(token)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_steam_listing_prices(raw: str) -> Tuple[Optional[float], Optional[float]]:
    """Return ``(buyer_price, seller_proceeds)`` from a Steam price cell.

    Dual-amount cells show the buyer price outside parentheses and the seller's
    proceeds inside, e.g. "¥ 1.15 (¥ 1.00)" -> (1.15, 1.00). A single amount is
    treated as seller proceeds: "¥ 8.21" -> (None, 8.21).
    """
    text = str(raw or "")
    match = re.search(r"[(（]([^()（）]*)[)）]", text)
    if match:
        inner = parse_price_text(match.group(1))
        outer = parse_price_text(text[:match.start()])
        if outer is not None and inner is not None:
            return outer, inner
        fallback = outer if outer is not None else inner
        return (None, fallback) if fallback is not None else (None, None)
    amount = parse_price_text(text)
    if amount is None:
        return None, None
    return None, amount


def currency_code_from_price_text(text: str) -> str:
    """Infer the currency code from a Steam market price string."""
    s = text or ""
    if "¥" in s or "￥" in s or "CNY" in s or "RMB" in s:
        return "CNY"
    if "HK" in s and "$" in s:
        return "HKD"
    if "US$" in s or "USD" in s:
        return "USD"
    if "R$" in s:
        return "BRL"
    if "S$" in s:
        return "SGD"
    if "A$" in s:
        return "AUD"
    if "C$" in s:
        return "CAD"
    if "₹" in s:
        return "INR"
    if "₽" in s:
        return "RUB"
    if "€" in s:
        return "EUR"
    if "$" in s:
        return "USD"
    return "CNY"
def _load_exchange_rates() -> Dict[str, float]:
    global _exchange_rate_cache
    now = time.time()
    ts, cached = _exchange_rate_cache
    if cached and now - ts < _EXCHANGE_RATE_TTL:
        return cached
    rates: Dict[str, float] = {}
    try:
        if _EXCHANGE_RATE_FILE.exists():
            with open(_EXCHANGE_RATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw_rates = data.get("rates") if isinstance(data, dict) else None
            if isinstance(raw_rates, dict):
                rates = {
                    str(k).upper(): float(v)
                    for k, v in raw_rates.items()
                    if isinstance(v, (int, float)) and float(v) > 0
                }
    except Exception:
        rates = {}
    _exchange_rate_cache = (now, rates)
    return rates
def load_exchange_rates() -> Dict[str, float]:
    """Public wrapper around the cached exchange-rate loader."""
    return _load_exchange_rates()
def apply_currency(
    prices: List[float],
    currency: Optional[str],
    usd_to_cny_rate: float = USD_TO_CNY_DEFAULT,
    rate_map: Optional[Dict[str, float]] = None,
) -> Tuple[List[float], str]:
    code = (currency or CURRENCY_CNY).strip().upper()
    if code == CURRENCY_CNY:
        return prices, CURRENCY_CNY
    rates = rate_map if rate_map is not None else _load_exchange_rates()
    if code == CURRENCY_USD:
        rate = rates.get(CURRENCY_USD) or usd_to_cny_rate
        return [p * rate for p in prices], CURRENCY_CNY
    rate = rates.get(code)
    if rate:
        return [p * rate for p in prices], CURRENCY_CNY
    return prices, code
def yuan_to_cents(yuan: float) -> int:
    return max(1, int(round(yuan * 100)))


def list_price_display_to_cents(display_amount: float, account_currency: str = "CNY") -> int:
    display_amount = round(display_amount, 2)
    total_cents = max(1, int(round(display_amount * 100)))
    
    wallet_info = dict(g_rgWalletInfo)
    if account_currency.upper() != "CNY":
        wallet_info["wallet_fee_minimum"] = "1"
        wallet_info["wallet_market_minimum"] = "1"
        
    return get_item_price_from_total(total_cents, wallet_info)
def cents_to_yuan(cents: int) -> float:
    return cents / 100.0
