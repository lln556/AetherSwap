import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_static_route_serves_files_inside_web_root(tmp_path, monkeypatch):
    from app.routes import static

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    asset = web_dir / "app.js"
    asset.write_text("console.log('ok')", encoding="utf-8")
    monkeypatch.setattr(static, "WEB_DIR", web_dir)

    response = static.static_or_index("app.js")

    assert Path(response.path) == asset


def test_static_route_blocks_path_traversal(tmp_path, monkeypatch):
    from app.routes import static

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    index = web_dir / "index.html"
    index.write_text("<html></html>", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(static, "WEB_DIR", web_dir)

    response = static.static_or_index("../secret.txt")

    assert Path(response.path) == index
    assert Path(response.path) != secret
