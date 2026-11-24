"""Static file serving routes – must be registered LAST (catch-all)."""
from pathlib import Path
from typing import Optional
from fastapi import APIRouter
from fastapi.responses import FileResponse
router = APIRouter()
WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"


_HTML_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' https: data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'self'"
)


def _web_file_response(path: Path) -> FileResponse:
    headers = {}
    suffix = path.suffix.lower()
    if suffix in {".html", ".js"}:
        headers["Cache-Control"] = "no-cache"
    if suffix == ".html":
        headers["Content-Security-Policy"] = _HTML_CSP
    return FileResponse(path, headers=headers)


def _safe_web_file(path: str) -> Optional[Path]:
    root = WEB_DIR.resolve()
    candidate = (root / (path or "")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


@router.get("/")
def index():
    f = WEB_DIR / "index.html"
    if f.exists():
        return _web_file_response(f)
    return {"app": "aetherswap", "ui": "web/index.html not found"}
@router.get("/{path:path}")
def static_or_index(path: str):
    f = _safe_web_file(path)
    if f:
        return _web_file_response(f)
    if (WEB_DIR / "index.html").exists():
        return _web_file_response(WEB_DIR / "index.html")
    return {"error": "not found"}
