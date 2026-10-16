"""BUFF's local envelope encryption and durable one-attempt-per-order journal.

The wire format is RSA-4096/PKCS1v15(key) + IV + AES-128-CBC/PKCS7(cookie),
base64 encoded. The public key/protocol is also used by Steamauto's BUFF
adapter: https://github.com/Steamauto/Steamauto/blob/master/utils/BuffApiCrypt.py
Only BUFF's official HTTPS endpoint receives the encrypted session.
"""

import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import unquote

from .request_policy import BuffRequestBlocked
from utils.cookies import cookies_str_to_dict
from utils.json_store import atomic_write_json

_JOURNAL_PATH = Path(__file__).resolve().parents[1] / "config" / "buff_buyer_offers.json"
_LOCK = threading.RLock()
_PUBLIC_KEY = """MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEArF75iD8PXTT+B5nAnnhw
qxg9I48t9uED7r6GuRcPYUZ0Ye3Vdvs71CVjuELyxALtj5cN+Pe1DwDSUAH1TF+9
dS7769gcJaMMdgEB6vyssm9fnPKB4KXqbUHdMT1MF2tylemDlqfsfpkV91wtAhHf
SkNtsQcPw4Juhn0IK+2xyvlm6HtXqFOkhial5T+miGBJk3snHfLPmQFsg/3EuHFM
tBzoLX29C46SNv/W33dwOk3mgIP1SMy4TLmm8CuyNiCuHPum53Q3RXSGrpR2nJps
4ICIWb0P3VZmPhCrDK1iWwwtVGj9jDkCT2zh+B18j26vfTkBDdac5s4sw739uAha
bH56BQflowPICHVWtptCEnORewxo/FDhFUtn4sjiQswgnTHJ6F/q0vwegRRsx0AT
f3SvpksR6dZuUqHzISthooQ/68PrJ8VaKfT17u43pif08/bFkZAkYdLev4Mk0SlZ
YOqpRoif+7Pi0yObTZ0bgpCwDb1kgAmqCHi9pFPS/LUMVqSqMa4maxAX2A8a/cbl
CJbjBHLn0zrZn3YW4hKlaVvGFG/Mmag+ALV5xII0y6JSoqdxlxpyhEmbOi/GCFMw
0Mn6lyvYDCvYVwS7UqLMw7NU3WXhbNUh8DgBSb5jo4yY9E42d24JiumZulzkSdgy
OSkVea8JGUUD8PliMtRJOQkCAwEAAQ=="""


def buyer_session(cookies: str, steam_id: str) -> str:
    """Return only the two community cookies, after checking their owner."""
    parsed = cookies_str_to_dict(cookies)
    secure = parsed.get("steamLoginSecure", "")
    if (not steam_id or not steam_id.isdigit() or len(steam_id) != 17
            or unquote(secure).split("||", 1)[0] != steam_id
            or not parsed.get("sessionid") or "||" not in unquote(secure)):
        raise ValueError("Steam 会话缺失或与当前账号不匹配")
    return f"sessionid={parsed['sessionid']}; steamLoginSecure={secure}"


def encrypt_buyer_info(cookies: str) -> str:
    from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    key, iv = os.urandom(16), os.urandom(16)
    public_key = load_der_public_key(base64.b64decode(_PUBLIC_KEY))
    padder = PKCS7(128).padder()
    padded = padder.update(cookies.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(public_key.encrypt(key, PKCS1v15()) + iv + ciphertext).decode("ascii")


@contextmanager
def _journal():
    # The app and an operational check can run in separate processes. Keep the
    # claim atomic across both, including when the JSON is replaced on disk.
    with _LOCK:
        _JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _JOURNAL_PATH.with_suffix(".lock").open("a+") as lock:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                if not lock.read(1):
                    lock.write("0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    data = json.loads(_JOURNAL_PATH.read_text()) if _JOURNAL_PATH.exists() else {}
                    if not isinstance(data, dict) or any(
                        not isinstance(k, str) or not k or not isinstance(v, dict)
                        or v.get("state") not in {"sending_or_unknown", "submitted", "rejected"}
                        or not v.get("steam_id") for k, v in data.items()
                    ):
                        raise ValueError("invalid journal")
                except (OSError, ValueError) as exc:
                    raise BuffRequestBlocked("买家报价记录无法读取，禁止重复发起报价") from exc
                yield data
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)


def _save_journal(data: dict) -> None:
    atomic_write_json(_JOURNAL_PATH, data)
    if os.name != "nt":
        directory = os.open(_JOURNAL_PATH.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def claim_buyer_offer(order_id: str, steam_id: str) -> bool:
    with _journal() as data:
        if order_id in data:
            return False
        data[order_id] = {"steam_id": steam_id, "state": "sending_or_unknown", "at": time.time()}
        _save_journal(data)
        return True


def finish_buyer_offer(order_id: str, state: str) -> None:
    with _journal() as data:
        if order_id not in data:
            raise BuffRequestBlocked("买家报价记录缺失，请核对订单")
        data[order_id].update(state=state, updated_at=time.time())
        _save_journal(data)
