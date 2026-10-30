"""Normalize user-supplied browser session fields without logging them."""
import re


def normalize_cookie_input(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^\s*cookie\s*:\s*", "", text, flags=re.I)
    pieces = []
    for part in re.split(r";|\r?\n", text):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if name:
            pieces.append(f"{name}={value}")
    return "; ".join(pieces)
def normalize_user_agent(raw: str) -> str:
    # A header value must be one line.  Keep a generous upper bound while
    # preventing accidental pasted request dumps from entering credentials.
    return " ".join((raw or "").split())[:1024]
def cookie_value(cookie_str: str, name: str) -> str:
    wanted = name.lower()
    for part in (cookie_str or "").split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        if key.strip().lower() == wanted:
            return value.strip()
    return ""
