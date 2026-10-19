import httpx
import json
import re
import time
from typing import Callable, Dict, Optional, Set, Tuple
from utils import http_client as http
from bs4 import BeautifulSoup
STEAM_REQUEST_TIMEOUT = 25
STEAM_REQUEST_RETRIES = 2
STEAM_REQUEST_RETRY_DELAY = 3
from utils.proxy_manager import get_proxy_manager, mask_proxy_url
from utils.money import currency_code_from_price_text, parse_price_text
MYLISTINGS_URL = "https://steamcommunity.com/market/mylistings/"
MYLISTINGS_HTML_URL = "https://steamcommunity.com/market/mylistings/"
MYHISTORY_RENDER_URL = "https://steamcommunity.com/market/myhistory/render/"
RETRY_STATUS_CODES = (502, 503, 504)
def _get_with_retry(url: str, params: dict, headers: dict, cookies: dict, debug_fn: Optional[Callable[[str], None]]) -> httpx.Response:
    last_err = None
    pm = get_proxy_manager()
    for attempt in range(STEAM_REQUEST_RETRIES + 1):
        try:
            proxies = pm.get_proxies_for_request(failed=(attempt > 0))
            if proxies:
                _debug(debug_fn, f"[Steam 请求] 正在使用代理池 {mask_proxy_url(proxies.get('http'))} 访问: {url}")
            r = http.get(url, params=params, headers=headers, cookies=cookies, proxies=proxies, verify=False, timeout=STEAM_REQUEST_TIMEOUT)
            if r.status_code in RETRY_STATUS_CODES and attempt < STEAM_REQUEST_RETRIES:
                _debug(debug_fn, f"[Steam 请求] HTTP {r.status_code}，{STEAM_REQUEST_RETRY_DELAY}s 后重试 ({attempt + 1}/{STEAM_REQUEST_RETRIES})")
                time.sleep(STEAM_REQUEST_RETRY_DELAY)
                continue
            return r
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ProxyError) as e:
            last_err = e
            if attempt < STEAM_REQUEST_RETRIES:
                _debug(debug_fn, f"[Steam 请求] {type(e).__name__}，{STEAM_REQUEST_RETRY_DELAY}s 后重试 ({attempt + 1}/{STEAM_REQUEST_RETRIES})")
                time.sleep(STEAM_REQUEST_RETRY_DELAY)
            else:
                raise
    if last_err:
        raise last_err
    return None
def _cookies_to_dict(cookies) -> dict:
    if isinstance(cookies, dict):
        return dict(cookies)
    out = {}
    for part in (cookies or "").split(";"):
        s = part.strip()
        if "=" in s:
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip()
    return out
def _extract_js_var(html: str, var_name: str) -> str:
    prefix = f"var {var_name} = "
    i = html.find(prefix)
    if i < 0:
        return ""
    i += len(prefix)
    if i >= len(html) or html[i] != "{":
        return ""
    depth = 0
    start = i
    j = i
    while j < len(html):
        ch = html[j]
        if ch == "{":
            depth += 1
            j += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start : j + 1]
            j += 1
        elif ch in ('"', "'"):
            q = ch
            k = j + 1
            while k < len(html):
                if html[k] == "\\":
                    k += 2
                    continue
                if html[k] == q:
                    k += 1
                    break
                k += 1
            j = k
        else:
            j += 1
    return ""
def _debug(debug_fn: Optional[Callable[[str], None]], msg: str) -> None:
    if debug_fn:
        debug_fn(msg)
def _parse_assets_730_2(market_data: dict, debug_fn: Optional[Callable[[str], None]] = None) -> Dict[str, dict]:
    assets_dict = (market_data.get("assets") or {}).get("730", {}).get("2", {})
    if not isinstance(assets_dict, dict):
        return {}
    out = {}
    for aid, info in assets_dict.items():
        if not isinstance(info, dict):
            continue
        canonical_id = str((info.get("id") or aid)).strip()
        if canonical_id:
            out[canonical_id] = info
    return out
def _parse_sell_listings_from_html(html: str, debug_fn: Optional[Callable[[str], None]] = None) -> Tuple[Set[str], Dict[str, str]]:
    assetids: Set[str] = set()
    name_by_assetid: Dict[str, str] = {}
    listing_js = _extract_js_var(html, "g_rgListingInfo")
    inventory_js = _extract_js_var(html, "g_rgInventory")
    if not listing_js or not inventory_js:
        return assetids, name_by_assetid
    try:
        listings = json.loads(listing_js)
    except (json.JSONDecodeError, TypeError):
        return assetids, name_by_assetid
    try:
        inventory = json.loads(inventory_js)
    except (json.JSONDecodeError, TypeError):
        inventory = {}
    item_db: Dict[str, dict] = {}
    for appid, ctx_dict in (inventory if isinstance(inventory, dict) else {}).items():
        if not isinstance(ctx_dict, dict):
            continue
        for ctx, asset_dict in ctx_dict.items():
            if not isinstance(asset_dict, dict):
                continue
            for aid, details in asset_dict.items():
                if isinstance(details, dict):
                    item_db[str(aid)] = details
    for list_id, info in (listings if isinstance(listings, dict) else {}).items():
        if not isinstance(info, dict):
            continue
        asset = info.get("asset")
        if not isinstance(asset, dict):
            continue
        aid = str(asset.get("id", "")).strip()
        if not aid:
            continue
        assetids.add(aid)
        details = item_db.get(aid, {})
        h = (details.get("market_hash_name") or "").strip()
        if h:
            name_by_assetid[aid] = h
    return assetids, name_by_assetid
def fetch_listing_snapshot(cookies, debug_fn=None):
    """Return actual market rows, including pending confirmations, by asset ID.

    The assets dictionary is metadata, not proof that an item is listed.
    An incomplete page must not be used to infer a sale or a successful delist.
    """
    c = _cookies_to_dict(cookies)
    if not c.get("steamLoginSecure"):
        return False, {}, "未登录 Steam"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    snapshot = {}
    seen_active_listings = set()
    start = 0
    try:
        for _ in range(100):
            r = _get_with_retry(MYLISTINGS_URL, {"norender": "1", "start": start, "count": 100}, headers, c, debug_fn)
            if r.status_code != 200:
                return False, {}, f"HTTP {r.status_code}"
            data = r.json()
            if (not isinstance(data, dict) or data.get("success") not in (True, 1)
                    or "listings" not in data):
                return False, {}, "Steam 未返回有效挂单列表"
            assets = _parse_assets_730_2(data)
            active_count = 0
            new_active = 0
            for key, state in (("listings", "active"), ("listings_to_confirm", "pending_confirmation")):
                raw = data.get(key) or []
                rows = list(raw.values()) if isinstance(raw, dict) else raw
                if not isinstance(rows, list):
                    return False, {}, "Steam 挂单列表格式异常"
                if key == "listings":
                    active_count = len(rows)
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("asset"), dict):
                        return False, {}, "Steam 挂单缺少资产信息"
                    asset = row["asset"]
                    aid = str(asset.get("id") or "").strip()
                    lid = str(row.get("listingid") or "").strip()
                    if not aid or not lid:
                        return False, {}, "Steam 挂单缺少唯一标识"
                    if key == "listings" and lid not in seen_active_listings:
                        new_active += 1
                        seen_active_listings.add(lid)
                    if str(asset.get("appid", "730")) != "730" or str(asset.get("contextid", "2")) != "2":
                        continue
                    details = assets.get(aid, {})
                    snapshot[aid] = {
                        "listing_id": lid,
                        "status": state,
                        "name": (asset.get("market_hash_name") or details.get("market_hash_name") or details.get("name") or "").strip(),
                    }
                    if row.get("price") is not None and row.get("fee") is not None:
                        seller, fee = int(row["price"]), int(row["fee"])
                        if seller > 0 and fee >= 0:
                            snapshot[aid].update(seller_minor=seller, buyer_minor=seller + fee)
                    if row.get("currencyid") is not None:
                        cid = int(row["currencyid"]) % 2000
                        snapshot[aid]["currency"] = {23: "CNY", 1: "USD", 3: "EUR", 19: "HKD"}.get(cid, f"STEAM_{cid}")
                    if row.get("time_created"):
                        snapshot[aid]["created_at"] = float(row["time_created"])
            start += active_count
            if len(seen_active_listings) != start:
                return False, {}, "Steam 挂单分页重复或发生变化，请稍后重试"
            total = int(data.get("num_active_listings", start))
            if start >= total and ("num_active_listings" in data or active_count < 100):
                return True, snapshot, ""
            # A repeated page must not silently hide items beyond the first 100.
            if not active_count or (start > active_count and not new_active):
                return False, {}, "Steam 挂单分页不完整"
        return False, {}, "Steam 挂单分页超出限制"
    except Exception as exc:
        return False, {}, f"读取 Steam 挂单失败 ({type(exc).__name__})"


def fetch_my_listings(cookies, debug_fn: Optional[Callable[[str], None]] = None) -> Tuple[bool, Set[str], str, Dict[str, str]]:
    ok, snapshot, error = fetch_listing_snapshot(cookies, debug_fn)
    if ok:
        names = {aid: row["name"] for aid, row in snapshot.items() if row["name"]}
        return True, set(snapshot), "", names
    return False, set(), error, {}
_HOVER_PATTERN = re.compile(
    r"CreateItemHoverFromContainer\s*\(\s*g_rgAssets\s*,\s*'(history_row_\d+_\d+)_name'\s*,\s*(\d+)\s*,\s*'(\d+)'\s*,\s*'(\d+)'"
)
def fetch_my_history_sold(cookies, debug_fn: Optional[Callable[[str], None]] = None) -> Tuple[bool, Dict[str, float], str]:
    try:
        c = _cookies_to_dict(cookies)
        if not c.get("steamLoginSecure"):
            _debug(debug_fn, "[myhistory] 无 steamLoginSecure，未登录")
            return False, {}, "未登录 Steam"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
        }
        params = {"query": "", "start": 0, "count": 100, "contextid": 2, "appid": 730}
        r = _get_with_retry(MYHISTORY_RENDER_URL, params, headers, c, debug_fn)
        _debug(debug_fn, f"[myhistory] 请求 status={r.status_code}")
        if r.status_code != 200:
            return False, {}, f"HTTP {r.status_code}"
        data = r.json() if r.text else {}
        if not data.get("success"):
            _debug(debug_fn, f"[myhistory] success=false, message={data.get('message', '')}")
            return False, {}, data.get("message", "请求失败")
        _parse_assets_730_2(data, debug_fn)
        hovers = data.get("hovers") or ""
        row_to_assetid = {}
        for m in _HOVER_PATTERN.finditer(hovers):
            row_id = m.group(1)
            asset_id = m.group(4)
            row_to_assetid[row_id] = str(asset_id)
        _debug(debug_fn, f"[myhistory] hovers 解析 row->assetid 共 {len(row_to_assetid)} 条, 示例 key: {list(row_to_assetid.keys())[:3]}")
        sold = {}
        html = data.get("results_html") or ""
        if not html:
            _debug(debug_fn, "[myhistory] results_html 为空")
            return True, sold, ""
        rate_map: Dict[str, float] = {}
        try:
            from pathlib import Path
            base_dir = Path(__file__).resolve().parent.parent / "config"
            fx_file = base_dir / "exchange_rate.json"
            if fx_file.exists():
                with open(fx_file, "r", encoding="utf-8") as f:
                    fx = json.load(f)
                if isinstance(fx, dict) and isinstance(fx.get("rates"), dict):
                    rate_map = {k: float(v) for k, v in fx["rates"].items() if isinstance(v, (int, float))}
            if rate_map:
                _debug(debug_fn, f"[myhistory] 汇率读取完成, 可用币种: {list(rate_map.keys())[:5]}")
        except Exception:
            rate_map = {}
            if "HK" in s and "$" in s:
                return "HKD"
            if "₹" in s:
                return "INR"
            if "₽" in s:
                return "RUB"
            if "€" in s:
                return "EUR"
            if "USD" in s or "US$" in s:
                return "USD"
            if "$" in s:
                return "USD"
            return "CNY"
        soup = BeautifulSoup(html, "html.parser")
        rows = [div for div in soup.find_all("div", class_="market_listing_row") if (div.get("id") or "").startswith("history_row_")]
        _debug(debug_fn, f"[myhistory] results_html 中 history_row_ 行数: {len(rows)}")
        hover_hits = 0
        fallback_hits = 0
        sold_rows = 0
        for row in rows:
            row_id = row.get("id") or ""
            assetid = row_to_assetid.get(row_id)
            if not assetid:
                row_str = str(row)
                fallback = re.search(r"assetid[\"']?\s*[:=]\s*[\"']?(\d+)[\"']?", row_str, re.I)
                if fallback:
                    assetid = str(fallback.group(1))
                    fallback_hits += 1
                else:
                    link = row.find("a", href=re.compile(r"assetid=\d+"))
                    if link and link.get("href"):
                        ma = re.search(r"assetid=(\d+)", link["href"])
                        if ma:
                            assetid = str(ma.group(1))
                            fallback_hits += 1
                if not assetid:
                    continue
            else:
                assetid = str(assetid)
                hover_hits += 1
            status_div = row.find("div", class_="market_listing_listed_date_combined")
            status_text = (status_div.get_text(strip=True) or "") if status_div else ""
            if not any(s in status_text for s in ("Sold", "已售出", "出售")):
                continue
            sold_rows += 1
            price_el = row.find("span", class_="market_listing_price")
            if not price_el:
                continue
            raw_text = price_el.get_text() or ""
            cur_code = currency_code_from_price_text(raw_text)
            raw = parse_price_text(raw_text)
            if raw is not None:
                cny_raw = raw
                if cur_code != "CNY":
                    rate = rate_map.get(cur_code)
                    if rate:
                        cny_raw = raw * rate
                        _debug(debug_fn, f"[myhistory] {cur_code} {raw} -> CNY {cny_raw:.4f}")
                sold[assetid] = round(cny_raw * 1.15, 2)
        _debug(debug_fn, f"[myhistory] 行->assetid: hover {hover_hits}, fallback {fallback_hits}; Sold 行 {sold_rows}, sold_map 条数 {len(sold)}, 示例: {list(sold.items())[:5]}")
        return True, sold, ""
    except Exception as e:
        _debug(debug_fn, f"[myhistory] 异常: {type(e).__name__}: {e}")
        return False, {}, str(e)[:120]
