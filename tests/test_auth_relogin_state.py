import sys
import types
import threading


def test_manual_buff_cookie_requires_session(monkeypatch):
    from app.routes import auth

    saved = []
    monkeypatch.setattr(auth, "update_buff_creds", lambda cookie: saved.append(cookie))

    result = auth.api_auth_manual_cookie("buff", auth.ManualCookieBody(cookies="csrf_token=abc"))

    assert result["ok"] is False
    assert "session" in result["error"]
    assert saved == []


def test_relogin_finish_surfaces_worker_error(monkeypatch):
    from app.routes import auth

    done = threading.Event()
    done.set()
    wake = threading.Event()

    monkeypatch.setattr(auth, "_relogin_context", object())
    monkeypatch.setattr(auth, "_relogin_error", "missing login cookie")
    monkeypatch.setattr(auth, "_relogin_done", done)
    monkeypatch.setattr(auth, "_relogin_wake", wake)

    result = auth._relogin_finish(True)

    assert result == {"ok": False, "error": "missing login cookie"}
    assert wake.is_set()


def test_buff_auto_relogin_success_clears_auth_and_verification(monkeypatch):
    from app.services import buff_auth

    calls = []

    class FakePage:
        def goto(self, *args, **kwargs):
            return None

        def wait_for_timeout(self, *args, **kwargs):
            return None

    class FakeContext:
        pages = [FakePage()]

        def cookies(self):
            return [
                {"name": "session", "value": "ok"},
                {"name": "csrf_token", "value": "csrf"},
            ]

        def close(self):
            return None

    class FakeChromium:
        def launch_persistent_context(self, *args, **kwargs):
            return FakeContext()

    class FakePlaywright:
        def __enter__(self):
            return types.SimpleNamespace(chromium=FakeChromium())

        def __exit__(self, exc_type, exc, tb):
            return False

    playwright_pkg = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakePlaywright()
    monkeypatch.setitem(sys.modules, "playwright", playwright_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.setattr(buff_auth, "get_buff_credentials", lambda: {"cookies": "session=old"})
    monkeypatch.setattr(buff_auth, "update_buff_creds", lambda cookie: calls.append(("update", cookie)))
    monkeypatch.setattr(buff_auth, "set_buff_auth_expired", lambda value: calls.append(("auth", value)))
    monkeypatch.setattr(
        buff_auth,
        "set_buff_verification_required",
        lambda value, reason="": calls.append(("verify", value, reason)),
    )

    result = buff_auth._try_buff_auto_relogin_impl()

    assert result[0] is True
    assert ("auth", False) in calls
    assert ("verify", False, "") in calls


def test_relogin_context_retries_with_temp_profile(monkeypatch, tmp_path):
    from app.routes import auth

    calls = []

    class FakeChromium:
        def launch_persistent_context(self, profile_dir, **kwargs):
            calls.append(profile_dir)
            if len(calls) == 1:
                raise RuntimeError("BrowserType.launch_persistent_context: Target page, context or browser has been closed")
            return object()

    temp_profile = tmp_path / "temp_profile"
    monkeypatch.setattr(auth.tempfile, "mkdtemp", lambda prefix, dir: str(temp_profile))

    context, temp_dir = auth._launch_relogin_context(
        types.SimpleNamespace(chromium=FakeChromium()),
        tmp_path / "playwright_buff",
        "buff",
    )

    assert context is not None
    assert temp_dir == temp_profile
    assert len(calls) == 2


def test_browser_launch_error_is_user_friendly():
    from app.routes import auth

    raw = (
        "BrowserType.launch_persistent_context: Target page, context or browser has been closed\n"
        "Browser logs:\n<launching> very long chromium command"
    )

    message = auth._friendly_browser_launch_error(RuntimeError(raw), "buff", retried=True)

    assert "Buff" in message
    assert "完整错误见调试日志" in message
    assert "Browser logs" not in message
