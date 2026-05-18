import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_新版订单簿支持INR转CNY(monkeypatch):
    from steam import market_orders

    html = (
        'window.SSR.renderContext=JSON.parse("'
        '{\\"queryData\\":\\"{\\\\\\"queries\\\\\\":[{\\\\\\"state\\\\\\":'
        '{\\\\\\"data\\\\\\":{\\\\\\"eCurrency\\\\\\":24,'
        '\\\\\\"amtMinSellOrder\\\\\\":6091,'
        '\\\\\\"rgCompactSellOrders\\\\\\":[6091,2,6516,4]}}}]}\\",'
        '\\"localizationSettings\\":{}}'
        '");'
    )
    monkeypatch.setattr(market_orders, "_load_exchange_rates", lambda: {"INR": 0.0709})

    result, error = market_orders._extract_ssr_orderbook_cny(html)

    assert error is None
    assert result["lowest_price"] == 4.32
    assert result["sell_orders"] == [(4.32, 2), (4.62, 4)]


def test_新版订单簿缺少非CNY汇率会报清晰原因(monkeypatch):
    from steam import market_orders

    html = (
        'window.SSR.renderContext=JSON.parse("'
        '{\\"queryData\\":\\"{\\\\\\"queries\\\\\\":[{\\\\\\"state\\\\\\":'
        '{\\\\\\"data\\\\\\":{\\\\\\"eCurrency\\\\\\":24,'
        '\\\\\\"rgCompactSellOrders\\\\\\":[6091,2]}}}]}\\",'
        '\\"localizationSettings\\":{}}'
        '");'
    )
    monkeypatch.setattr(market_orders, "_load_exchange_rates", lambda: {})

    result, error = market_orders._extract_ssr_orderbook_cny(html)

    assert result is None
    assert "INR" in error
    assert "exchange_rate.json" in error


def test_汇率文件里的Steam市场币种都有ECurrency映射():
    from steam import market_orders

    rate_codes = {
        "USD", "INR", "RUB", "HKD", "EUR", "KZT", "UAH", "TRY", "ARS",
        "VND", "IDR", "BRL", "CLP", "JPY", "PHP",
    }
    steam_codes = set(market_orders._STEAM_CURRENCY_CODES.values())

    assert rate_codes <= steam_codes


def test_汇率文件包含的非Steam本地市场币种不会误映射():
    from steam import market_orders

    steam_codes = set(market_orders._STEAM_CURRENCY_CODES.values())

    assert "PKR" not in steam_codes
    assert "AZN" not in steam_codes
