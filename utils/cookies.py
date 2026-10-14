"""Cookie string helpers shared by the Steam integration modules."""


def cookies_str_to_dict(cookies) -> dict:
    """Parse a ``a=1; b=2`` cookie string into a dict (tolerant)."""
    if isinstance(cookies, dict):
        return dict(cookies)
    out = {}
    for part in str(cookies or "").split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            name = name.strip()
            if name:
                out[name] = value.strip()
    return out
