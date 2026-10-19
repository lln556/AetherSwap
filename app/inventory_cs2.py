import re
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from config import get_steam
from steam.inventory import CS2_APP_ID, fetch_cs2_inventory
from steam.session import create_market_session
_ITEM_TYPE_NAMES = {
    "CSGO_Type_Pistol": "手枪",
    "CSGO_Type_SMG": "微型冲锋枪",
    "CSGO_Type_Rifle": "步枪",
    "CSGO_Type_SniperRifle": "狙击步枪",
    "CSGO_Type_Shotgun": "霰弹枪",
    "CSGO_Type_Machinegun": "机枪",
    "CSGO_Type_Knife": "匕首",
    "CSGO_Type_Hands": "手套",
    "CSGO_Type_C4": "C4",
    "CSGO_Type_Equipment": "装备",
    "CSGO_Type_Grenade": "手雷",
    "CSGO_Type_MusicKit": "音乐盒",
    "CSGO_Type_Collectible": "收藏品",
    "CSGO_Type_Container": "武器箱",
    "CSGO_Type_Key": "钥匙",
    "CSGO_Type_Pass": "通行证",
    "CSGO_Type_Sticker": "印花",
    "CSGO_Tool_Sticker": "印花",
    "CSGO_Tool_Patch": "布章",
    "CSGO_Tool_Charm": "挂件",
    "CSGO_Type_Tool": "工具",
    "CSGO_Type_Tag": "布章",
    "CSGO_Type_Spray": "涂鸦",
    "CSGO_Type_Charm": "挂件",
    "CSGO_Type_CustomPlayer": "探员",
    "CSGO_Type_Agent": "探员",
}
_WEAR_NAMES = {
    "tag_WearCategory0": "崭新出厂",
    "tag_WearCategory1": "略有磨损",
    "tag_WearCategory2": "久经沙场",
    "tag_WearCategory3": "破损不堪",
    "tag_WearCategory4": "战痕累累",
    "Factory New": "崭新出厂",
    "Minimal Wear": "略有磨损",
    "Field-Tested": "久经沙场",
    "Well-Worn": "破损不堪",
    "Battle-Scarred": "战痕累累",
}
_WEAR_FLOAT_RANGES = (
    (0.07, "崭新出厂"),
    (0.15, "略有磨损"),
    (0.38, "久经沙场"),
    (0.45, "破损不堪"),
)
_WEAR_SUFFIX_RE = re.compile(r"\(([^()]*)\)\s*$")


_TYPE_FIELD_KEYWORDS = (
    ("Sticker", "印花"),
    ("Graffiti", "涂鸦"),
    ("Music Kit", "音乐盒"),
    ("Collectible", "收藏品"),
    ("Container", "武器箱"),
    ("Patch", "布章"),
    ("Agent", "探员"),
    ("Charm", "挂件"),
    ("Pass", "通行证"),
    ("Key", "钥匙"),
)


def _type_from_field(raw) -> str:
    """Map the English ``type`` field to Chinese when the tag lookup misses."""
    text = str(raw or "").strip()
    if not text:
        return ""
    for keyword, label in _TYPE_FIELD_KEYWORDS:
        if keyword in text:
            return label
    return text


def _item_display_meta(desc: dict, market_hash_name: str, paintwear: Optional[str]) -> Dict[str, str]:
    """Derive icon / item type / wear category from a Steam description."""
    icon_url = str(desc.get("icon_url") or "")
    item_type = ""
    exterior = ""
    tags = desc.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            category = str(tag.get("category") or "")
            internal = str(tag.get("internal_name") or "")
            localized = str(tag.get("localized_name") or "")
            if category == "Type" and not item_type:
                item_type = _ITEM_TYPE_NAMES.get(internal) or localized
            elif category == "Exterior" and not exterior:
                exterior = _WEAR_NAMES.get(internal) or localized
    if not exterior:
        suffix = _WEAR_SUFFIX_RE.search(market_hash_name or "")
        if suffix:
            exterior = _WEAR_NAMES.get(suffix.group(1).strip(), "")
    if not exterior and paintwear not in (None, ""):
        try:
            wear = float(paintwear)
        except (TypeError, ValueError):
            wear = None
        if wear is not None and math.isfinite(wear) and 0 <= wear <= 1:
            exterior = "战痕累累"
            for limit, label in _WEAR_FLOAT_RANGES:
                if wear < limit:
                    exterior = label
                    break
    if not item_type:
        item_type = _type_from_field(desc.get("type"))
    return {"icon_url": icon_url, "item_type": item_type, "exterior": exterior}


def _persist_item_meta(items: List[Dict[str, Any]]) -> None:
    """Best-effort: remember display metadata and backfill purchase records."""
    try:
        from app.services import item_meta
        item_meta.remember_items(items)
        item_meta.enrich_purchases_from_inventory(items)
    except Exception as exc:
        try:
            from app.state import log
            log(f"item_meta: 库存元数据写入失败 {type(exc).__name__}: {exc}", "warn", category="inventory")
        except Exception:
            pass


def _safe_iso(ts: float) -> Optional[str]:
    if not ts:
        return None
    try:
        return datetime.utcfromtimestamp(ts).isoformat() + "Z"
    except (OSError, OverflowError, ValueError):
        return None
def _parse_cooldown(owner_descriptions: List[dict]) -> Tuple[str, float]:
    text = ""
    ts = 0.0
    for d in owner_descriptions or []:
        val = d.get("value") or ""
        if "trade-protected" not in val:
            continue
        text = val
        date_match = re.search(r"\[date\](\d+)\[/date\]", val)
        if date_match:
            ts = float(date_match.group(1))
            break
        m = re.search(r"until (.+?) GMT", val)
        if not m:
            break
        raw = m.group(1)
        raw = raw.replace(" (", " ").replace(")", "")
        try:
            dt = datetime.strptime(raw, "%b %d, %Y %H:%M:%S").replace(tzinfo=timezone.utc)
            ts = dt.timestamp()
        except Exception:
            ts = 0.0
        break
    return text, ts
def scan_cs2_inventory() -> Tuple[bool, List[Dict[str, Any]], str]:
    cred = get_steam()
    steam_id = cred.get("steam_id")
    cookies = cred.get("cookies")
    if not steam_id or not cookies:
        return False, [], "未配置 Steam steam_id 或 cookies"
    session = create_market_session(cookies, steam_id)
    try:
        data = fetch_cs2_inventory(session, steam_id)
    finally:
        session.close()
    if not data:
        return False, [], "获取 CS2 库存失败"
    if isinstance(data, dict) and data.get("auth_expired"):
        return False, [], "登录已过期，请重新登录"
    items: List[Dict[str, Any]] = []
    now = time.time()
    desc_map: Dict[tuple, Dict[str, Any]] = {}
    for d in data.get("descriptions") or []:
        cid = d.get("classid")
        if cid:
            iid = d.get("instanceid") or "0"
            desc_map[(str(cid), str(iid))] = d
    for asset in data.get("assets") or []:
        cid = str(asset.get("classid", ""))
        iid = str(asset.get("instanceid") or "0")
        desc = desc_map.get((cid, iid)) or desc_map.get((cid, "0"))
        if not desc:
            continue
        name = desc.get("name") or ""
        market_hash_name = desc.get("market_hash_name") or desc.get("market_name") or name
        marketable = int(desc.get("marketable", 0))
        tradable = int(desc.get("tradable", 0))
        owner_desc = desc.get("owner_descriptions") or []
        cd_text, cd_ts = _parse_cooldown(owner_desc)
        can_trade = tradable == 1 and (not cd_ts or now >= cd_ts)
        can_sell = marketable == 1 and can_trade
        properties = {}
        for prop in asset.get("asset_properties") or []:
            if isinstance(prop, dict):
                properties.setdefault(str(prop.get("propertyid")), []).append(prop)
        fingerprint = {}
        if len(properties.get("1", [])) == 1 and len(properties.get("2", [])) == 1:
            seed = properties["1"][0].get("int_value")
            wear = properties["2"][0].get("float_value")
            try:
                if (not isinstance(seed, bool) and not isinstance(wear, bool)
                        and str(seed).isdigit() and 0 <= int(seed) <= 1000
                        and math.isfinite(float(wear)) and 0 <= float(wear) <= 1):
                    fingerprint = {"paintseed": int(seed), "paintwear": str(wear)}
            except (ValueError, TypeError):
                pass
        display = _item_display_meta(desc, market_hash_name, fingerprint.get("paintwear"))
        items.append(
            {
                "name": name,
                "market_hash_name": market_hash_name,
                "assetid": str(asset.get("assetid", "")),
                "appid": int(asset.get("appid", CS2_APP_ID)),
                "contextid": str(asset.get("contextid", "")),
                "classid": cid,
                **fingerprint,
                "icon_url": display["icon_url"],
                "item_type": display["item_type"],
                "exterior": display["exterior"],
                "marketable": marketable,
                "tradable": tradable,
                "cooldown_text": cd_text,
                "cooldown_at": cd_ts or None,
                "cooldown_at_iso": _safe_iso(cd_ts),
                "can_sell": can_sell,
                "can_trade": can_trade,
            }
        )
    items.sort(key=lambda x: (not x["can_sell"], not x["can_trade"], x["name"]))
    _persist_item_meta(items)
    return True, items, ""
