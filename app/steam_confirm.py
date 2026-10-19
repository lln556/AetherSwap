import base64
import hashlib
import hmac
import struct
import time
from typing import Dict, List, Tuple, Union
from utils import http_client as http
import urllib.parse
from steampy.guard import generate_device_id
def _cookies_to_dict(cookie_str: str) -> dict:
    """将 Cookie 字符串转换为字典"""
    out = {}
    for part in (cookie_str or "").split(";"):
        s = part.strip()
        if "=" in s:
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip()
    return out
class SteamConfirmer:
    def __init__(self, identity_secret: str, device_id: str, steam_id: str, cookies: Union[str, dict]) -> None:
        raw_secret = (identity_secret or "").strip()
        raw_secret = raw_secret.replace("\\u002B", "+").replace("\\u002b", "+")
        raw_secret = raw_secret.replace("\u002B", "+").replace("\u002b", "+")
        raw_secret = raw_secret.replace("\\/", "/")
        self.identity_secret = raw_secret
        self.steam_id = str(steam_id or "").strip()
        self.device_id = urllib.parse.unquote((device_id or "").strip()) or generate_device_id(self.steam_id)
        self.session = http.Client()
        self.session.verify = False
        if isinstance(cookies, str):
            ck_dict = _cookies_to_dict(cookies)
        else:
            ck_dict = cookies or {}
        self.session.cookies.update(ck_dict)
        self.session.headers.update({
            "User-Agent": "Steam Mobile/10372190 CFNetwork/3860.100.1 Darwin/25.0.0",
            "Accept": "application/json, text/plain, */*",
        })
    def _signature(self, tag: str, timestamp: int) -> str:
        secret_bytes = base64.b64decode(self.identity_secret)
        time_bytes = struct.pack(">Q", int(timestamp))
        if tag:
            time_bytes += tag.encode("ascii", errors="ignore")
        mac = hmac.new(secret_bytes, time_bytes, hashlib.sha1).digest()
        return base64.b64encode(mac).decode("utf-8")
    def get_confirmations(self) -> Tuple[bool, List[dict], str]:
        ts = int(time.time())
        try:
            sig = self._signature("conf", ts)
        except Exception as e:
            return False, [], f"签名失败: {e}"
        params = {
            "p": self.device_id,
            "a": self.steam_id,
            "k": sig,
            "t": ts,
            "m": "android",
            "tag": "conf",
        }
        url = "https://steamcommunity.com/mobileconf/getlist"
        try:
            r = self.session.get(url, params=params, timeout=20)
            try:
                data = r.json()
            except (ValueError, TypeError):
                return False, [], f"确认列表响应无法解析 (HTTP {r.status_code})"
            if r.status_code != 200 or not isinstance(data, dict) or data.get("success") is not True:
                return False, [], f"Steam 未返回有效确认列表 (HTTP {r.status_code})"
            confs = data.get("conf", [])
            if not isinstance(confs, list) or any(not isinstance(c, dict) for c in confs):
                return False, [], "确认列表格式异常"
            return True, confs, ""
        except Exception as e:
            return False, [], f"确认列表请求失败 ({type(e).__name__})"
    def accept_all(self, conf_list: List[dict]) -> Tuple[bool, int, str]:
        if not conf_list:
            return True, 0, ""
        ts = int(time.time())
        try:
            sig = self._signature("allow", ts)
        except Exception as e:
            return False, 0, f"签名失败: {e}"
        params = {
            "p": self.device_id,
            "a": self.steam_id,
            "k": sig,
            "t": ts,
            "m": "android",
            "tag": "allow",
            "op": "allow",
        }
        multipart = []
        for c in conf_list:
            multipart.append(("cid[]", (None, str(c.get("id")))))
            multipart.append(("ck[]", (None, str(c.get("nonce")))))
        url = "https://steamcommunity.com/mobileconf/multiajaxop"
        try:
            r = self.session.post(url, params=params, files=multipart, timeout=25)
            try:
                data = r.json()
            except (ValueError, TypeError):
                return False, 0, f"确认响应无法解析 (HTTP {r.status_code})"
            if r.status_code != 200 or not isinstance(data, dict) or data.get("success") is not True:
                return False, 0, f"Steam 未接受确认请求 (HTTP {r.status_code})"
            return True, len(conf_list), ""
        except Exception as e:
            return False, 0, f"确认请求失败 ({type(e).__name__})"
def auto_confirm_once(identity_secret: str, device_id: str, steam_id: str, cookies: Union[str, dict], *, listing_ids=None) -> Tuple[bool, int, str]:
    # A mobile confirmation list can also contain trades and personal listings.
    # Never approve it without an explicit set of owned market listing IDs.
    targets = {str(lid) for lid in (listing_ids or []) if lid}
    if not targets:
        return True, 0, ""
    bot = SteamConfirmer(identity_secret, device_id, steam_id, cookies)
    try:
        ok, confs, err = bot.get_confirmations()
        if not ok:
            return False, 0, f"获取列表失败: {err}"
        selected = [c for c in confs if str(c.get("type")) == "3"
                    and str(c.get("creator_id") or "") in targets]
        if any(not c.get("id") or not c.get("nonce") for c in selected):
            return False, 0, "目标上架确认缺少确认凭据"
        ok2, n, err2 = bot.accept_all(selected)
        if not ok2:
            return False, 0, f"确认操作失败: {err2}"
        return True, n, ""
    finally:
        bot.session.close()
