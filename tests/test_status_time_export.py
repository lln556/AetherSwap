def test_log_export_uses_configured_browser_timezone(monkeypatch, tmp_path):
    from app.routes import status

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        status,
        "get_log",
        lambda _since=0: [
            {
                "t": 1783570563,
                "level": "info",
                "msg": "schedule check",
            }
        ],
    )
    monkeypatch.setattr(
        status,
        "load_app_config_validated",
        lambda: {
            "system": {
                "timezone": "Asia/Shanghai",
                "timezone_offset_minutes": 480,
            }
        },
    )

    result = status.api_log_export()
    content = (tmp_path / result["path"]).read_text(encoding="utf-8")

    assert result["ok"] is True
    assert "2026-07-09 12:16:03 [info] schedule check" in content
    assert "# timezone: Asia/Shanghai" in content
