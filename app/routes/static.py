"""Static file serving routes – must be registered LAST (catch-all)."""
from pathlib import Path
from typing import Optional
from fastapi import APIRouter
from fastapi.responses import FileResponse
router = APIRouter()
WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"


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
        return FileResponse(f)
    return {"app": "aetherswap", "ui": "web/index.html not found"}
@router.get("/{path:path}")
def static_or_index(path: str):
    f = _safe_web_file(path)
    if f:
        return FileResponse(f)
    if (WEB_DIR / "index.html").exists():
        return FileResponse(WEB_DIR / "index.html")
    return {"error": "not found"}
