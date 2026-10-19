"""Read actual seller proceeds and date precision from Steam's paged market history."""
import calendar
import re
from datetime import datetime, date
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from app.steam_listings import _get_with_retry, _cookies_to_dict, _HOVER_PATTERN, MYHISTORY_RENDER_URL
from app.trading_ledger import minor
from utils.money import parse_price_text, currency_code_from_price_text


def history_day(label, *, now=None, purchased_at=None):
    """Only infer an omitted year if the known ownership interval admits one date."""
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    full = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", label)
    if full:
        try:
            return date(*map(int, full.groups())).isoformat()
        except ValueError:
            return None
    md = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})", label)
    month, day = (map(int, md.groups()) if md else (None, None))
    if not md:
        for m in range(1, 13):
            found = re.search(rf"(?:\b{calendar.month_abbr[m]}\w*\.?\s+(\d{{1,2}})|(\d{{1,2}})\s+{calendar.month_abbr[m]}\w*\.?)", label, re.I)
            if found:
                month, day = m, int(found.group(1) or found.group(2))
                break
    if not month or not purchased_at:
        return None
    earliest = datetime.fromtimestamp(purchased_at, now.tzinfo).date()
    year_match = re.search(r"\b(20\d{2})\b", label)
    years = [int(year_match.group(1))] if year_match else range(earliest.year, now.year + 1)
    candidates = []
    for year in years:
        try:
            d = date(year, month, day)
            if earliest <= d <= now.date():
                candidates.append(d)
        except ValueError:
            continue
    return candidates[0].isoformat() if len(candidates) == 1 else None


def parse_history(data, owned):
    row_ids = {m.group(1): m.group(4) for m in _HOVER_PATTERN.finditer(data.get("hovers") or "")}
    soup = BeautifulSoup(data.get("results_html") or "", "html.parser")
    results, row_ids_seen = [], []
    for row in soup.select('.market_listing_row[id^="history_row_"]'):
        rid = row.get("id")
        row_ids_seen.append(rid)
        aid = row_ids.get(rid)
        if aid not in owned:
            continue
        status, price = row.select_one(".market_listing_listed_date_combined"), row.select_one(".market_listing_price")
        label = status.get_text(" ", strip=True) if status else ""
        if not re.search(r"\bSold\b|已售出|出售", label, re.I) or not price:
            continue
        text = price.get_text(" ", strip=True)
        amount = parse_price_text(text)
        if amount is None or amount <= 0:
            continue
        results.append({"external_id": rid, "assetid": aid, "proceeds_minor": minor(amount),
                        "currency": currency_code_from_price_text(text), "date_label": label,
                        "sold_day": history_day(label, purchased_at=owned.get(aid))})
    return results, row_ids_seen


def fetch_sale_history(cookies, owned):
    if not owned:
        return True, [], ""
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9", "X-Requested-With": "XMLHttpRequest"}
    found, seen, start = [], set(), 0
    try:
        for _ in range(40):
            r = _get_with_retry(MYHISTORY_RENDER_URL, {"query": "", "start": start, "count": 100, "appid": 730, "contextid": 2},
                                headers, _cookies_to_dict(cookies), None)
            if r.status_code != 200:
                return False, [], f"成交历史 HTTP {r.status_code}"
            data = r.json()
            if not data.get("success") or "results_html" not in data:
                return False, [], "成交历史响应不完整"
            records, ids = parse_history(data, owned)
            if any(i in seen for i in ids):
                return False, [], "成交历史分页重复，稍后重试"
            seen.update(ids)
            found.extend(records)
            start += len(ids)
            total = int(data.get("total_count", start))
            if start >= total:
                return True, found, ""
            if not ids:
                return False, [], "成交历史分页缺失"
        return False, [], "成交历史超出分页上限"
    except Exception as exc:
        return False, [], f"成交历史暂时无法读取（{type(exc).__name__}）"
