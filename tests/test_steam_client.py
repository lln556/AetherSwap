import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steam.client import (  # noqa: E402
    market_hash_name_from_listing_url,
    resolve_market_hash_name_from_listing_url,
)


def _ssr_html(queries):
    ctx = {
        "queryData": json.dumps({"queries": queries}, ensure_ascii=False),
        "localizationSettings": {},
    }
    return f"window.SSR.renderContext=JSON.parse({json.dumps(json.dumps(ctx, ensure_ascii=False))});"


def _description(name, tags):
    return {
        "queryKey": ["market", "description", 730, name],
        "state": {
            "data": {
                "market_hash_name": name,
                "market_bucket_group_id": "G1802202E3004",
                "tags": [{"internal_name": tag} for tag in tags],
            }
        },
    }


def _orderbook(name):
    return {
        "queryKey": ["market", "orderbook", 730, name],
        "state": {"data": {"rgCompactSellOrders": [158, 1], "eCurrency": 23}},
    }


class DummyResponse:
    status_code = 200

    def __init__(self, text):
        self.text = text


class DummySession:
    def __init__(self, html):
        self.html = html
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return DummyResponse(self.html)


def test_market_hash_name_from_old_listing_url():
    url = "https://steamcommunity.com/market/listings/730/AK-47%20%7C%20Redline%20%28Field-Tested%29"

    assert market_hash_name_from_listing_url(url) == "AK-47 | Redline (Field-Tested)"


def test_market_hash_name_from_group_listing_url_is_not_used_as_name():
    url = "https://steamcommunity.com/market/listings/730/G1802202E3004"

    assert market_hash_name_from_listing_url(url) is None


def test_resolve_group_listing_url_uses_prefetched_orderbook_name():
    html = _ssr_html(
        [
            _description("Souvenir Dual Berettas | Contractor (Minimal Wear)", ["tournament", "WearCategory1"]),
            _description("Dual Berettas | Contractor (Field-Tested)", ["normal", "WearCategory2"]),
            _orderbook("Dual Berettas | Contractor (Factory New)"),
        ]
    )
    session = DummySession(html)

    name = resolve_market_hash_name_from_listing_url(
        "https://steamcommunity.com/market/listings/730/G1802202E3004",
        session=session,
    )

    assert name == "Dual Berettas | Contractor (Factory New)"
    assert session.urls == ["https://steamcommunity.com/market/listings/730/G1802202E3004"]


def test_resolve_group_listing_url_honors_category_filters():
    html = _ssr_html(
        [
            _description("Souvenir Dual Berettas | Contractor (Minimal Wear)", ["tournament", "WearCategory1"]),
            _description("Dual Berettas | Contractor (Minimal Wear)", ["normal", "WearCategory1"]),
            _orderbook("Dual Berettas | Contractor (Factory New)"),
        ]
    )

    name = resolve_market_hash_name_from_listing_url(
        "https://steamcommunity.com/market/listings/730/G1802202E3004"
        "?category_730_Exterior=tag_WearCategory1&category_730_Quality=tag_normal",
        session=DummySession(html),
    )

    assert name == "Dual Berettas | Contractor (Minimal Wear)"
