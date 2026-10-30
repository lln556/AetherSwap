"""Browser coverage for the persistent BUFF session card on the dashboard."""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import pytest


WEB = Path(__file__).resolve().parents[1] / "web"
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"
)


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch(headless=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Playwright Chromium is not installed")
            raise
        yield browser
        browser.close()


def _json(route, payload, status=200):
    route.fulfill(
        status=status,
        body=json.dumps(payload, ensure_ascii=False),
        content_type="application/json; charset=utf-8",
    )


def _session(status="unconfigured", *, checked_at=None, message="", can_replace=True,
             replace_block_reason="", **extra):
    payload = {
        "status": status,
        "checked_at": checked_at,
        "message": message,
        "can_replace": can_replace,
        "replace_block_reason": replace_block_reason,
    }
    payload.update(extra)
    return payload


@pytest.fixture
def page_factory(browser):
    contexts = []

    def make(*, session=None, session_sequence=None, check_result=None,
             import_result=None, extension=None, width=1440, height=1000,
             mobile=False, user_agent=None):
        context_options = {"viewport": {"width": width, "height": height}}
        if mobile:
            context_options.update({
                "is_mobile": True,
                "has_touch": True,
                "user_agent": user_agent or MOBILE_USER_AGENT,
            })
        elif user_agent:
            context_options["user_agent"] = user_agent
        context = browser.new_context(**context_options)
        contexts.append(context)
        page = context.new_page()
        requests = []
        errors = []
        console = []
        cached_responses = list(session_sequence or [])
        cached_index = 0
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: console.append(message.text))
        if extension is not None:
            page.add_init_script(script=extension)

        def route_request(route):
            request = route.request
            url = urlparse(request.url)
            if url.hostname != "aetherswap.test":
                route.abort()
                return
            path = url.path
            if path.startswith("/api/"):
                requests.append({
                    "method": request.method,
                    "path": path,
                    "headers": request.headers,
                    "body": request.post_data_json,
                })
                if path == "/api/auth/buff/session" and request.method == "GET":
                    nonlocal cached_index
                    if cached_responses:
                        cached = cached_responses[min(cached_index, len(cached_responses) - 1)]
                        cached_index += 1
                    else:
                        cached = session or _session()
                    _json(route, {"ok": True, "session": cached})
                    return
                if path == "/api/auth/buff/session/check" and request.method == "POST":
                    _json(route, check_result or {"ok": True, "session": _session("valid", checked_at=1_780_000_000)})
                    return
                if path == "/api/auth/buff/session/import" and request.method == "POST":
                    _json(route, import_result or {"ok": True, "session": _session("valid", checked_at=1_780_000_000)})
                    return
                if path == "/api/auth/buff/extension.zip":
                    route.fulfill(status=200, body=b"zip", content_type="application/zip")
                    return
                _json(route, {"ok": False, "error": "unexpected API request"}, status=404)
                return

            file_path = WEB / path.lstrip("/") if path != "/" else WEB / "index.html"
            file_path = file_path.resolve()
            if WEB not in file_path.parents or not file_path.is_file():
                route.fulfill(status=404, body="")
                return
            if file_path.suffix == ".js":
                # Keep the real page shell and session module, without unrelated pollers.
                if file_path.name not in {"utils.js", "buff-session.js", "dashboard.js"}:
                    route.fulfill(body="", content_type="application/javascript")
                    return
            route.fulfill(
                path=str(file_path),
                content_type=mimetypes.guess_type(file_path)[0] or "application/octet-stream",
            )

        page.route("**/*", route_request)
        page.goto("http://aetherswap.test/", wait_until="domcontentloaded")
        page.wait_for_function(
            "window.BuffSession && document.getElementById('buff-session-badge')?.dataset.status !== 'loading'"
        )
        return page, requests, errors, console

    yield make
    for context in contexts:
        context.close()


def test_cached_session_is_persistent_and_does_not_trigger_verification(page_factory):
    cached = _session(
        "valid",
        checked_at=1_790_000_000,
        message="最近一次只读检测通过",
        can_replace=False,
        replace_block_reason="买入任务运行中，结束任务后才能替换会话",
    )
    page, requests, errors, _ = page_factory(session=cached)

    page.locator("#buff-session-card").wait_for(state="visible")
    page.locator("#buff-session-badge").wait_for(state="visible")
    assert page.locator("#buff-session-badge").inner_text() == "最近检测有效"
    assert "2026" in page.locator("#buff-session-checked-at").inner_text()
    assert page.locator("#buff-session-message").inner_text() == "最近一次只读检测通过"
    assert "买入任务运行中" in page.locator("#buff-replace-reason").inner_text()
    assert page.locator("#btn-buff-import").is_disabled()
    assert page.locator("#btn-buff-manual").is_disabled()
    assert not page.locator("#btn-buff-check").is_disabled()

    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        "document.getElementById('buff-session-badge')?.dataset.status !== 'loading'"
    )
    page.wait_for_timeout(150)
    session_requests = [r for r in requests if r["path"] == "/api/auth/buff/session"]
    assert len(session_requests) == 2
    assert all(r["method"] == "GET" for r in session_requests)
    assert not [r for r in requests if r["path"].startswith("/api/auth/buff/session/") and r["method"] == "POST"]
    assert page.locator("#buff-session-badge").inner_text() == "最近检测有效"
    assert page.locator("#buff-session-message").inner_text() == "最近一次只读检测通过"
    assert not [r for r in requests if r["path"] in {"/api/purchase", "/api/start", "/api/pipeline/start"}]
    assert errors == []


def test_check_failure_shows_reason_without_rewriting_cached_result(page_factory):
    cached = _session(
        "expired",
        checked_at=1_790_000_000,
        message="保留的最近检测结果",
        can_replace=True,
    )
    page, requests, errors, _ = page_factory(
        session=cached,
        check_result={
            "ok": False,
            "code": "error",
            "error": "BUFF 检测请求返回 503，请稍后重试",
            "session": cached,
        },
    )

    page.locator("#btn-buff-check").click()
    page.wait_for_function(
        "document.getElementById('buff-session-message').textContent.includes('503')"
    )

    assert page.locator("#buff-session-badge").inner_text() == "最近检测已过期"
    assert "2026" in page.locator("#buff-session-checked-at").inner_text()
    assert page.locator("#buff-session-message").inner_text() == "BUFF 检测请求返回 503，请稍后重试"
    assert not page.locator("#btn-buff-check").is_disabled()
    checks = [r for r in requests if r["path"] == "/api/auth/buff/session/check"]
    assert len(checks) == 1
    assert not [r for r in requests if r["path"] in {"/api/purchase", "/api/start", "/api/pipeline/start"}]
    assert errors == []


def test_cached_status_refresh_preserves_explicit_check_failure(page_factory):
    cached = _session(
        "expired",
        checked_at=1_790_000_000,
        message="保留的最近检测结果",
        can_replace=True,
    )
    page, _, errors, _ = page_factory(
        session=cached,
        check_result={
            "ok": False,
            "code": "error",
            "error": "BUFF 检测请求返回 503，请稍后重试",
            "session": cached,
        },
    )

    page.locator("#btn-buff-check").click()
    page.wait_for_function(
        "document.getElementById('buff-session-message').textContent.includes('503')"
    )
    page.evaluate("BuffSession.refreshCachedSession()")
    page.wait_for_timeout(120)

    assert page.locator("#buff-session-badge").inner_text() == "最近检测已过期"
    assert page.locator("#buff-session-message").inner_text() == "BUFF 检测请求返回 503，请稍后重试"
    assert errors == []


def test_rapid_check_clicks_issue_one_request_and_disable_control_while_pending(page_factory):
    page, requests, errors, _ = page_factory(
        session=_session("expired", checked_at=1_790_000_000, message="旧结果"),
        check_result={
            "ok": True,
            "session": _session("valid", checked_at=1_780_000_000, message="新结果"),
        },
    )

    pending = page.evaluate("""() => {
      const button = document.getElementById('btn-buff-check');
      button.click();
      const state = { disabled: button.disabled, text: button.textContent };
      button.click();
      button.click();
      return state;
    }""")
    assert pending == {"disabled": True, "text": "检测中…"}
    page.wait_for_function(
        "document.getElementById('buff-session-badge').textContent === '最近检测有效'"
    )
    page.wait_for_timeout(120)
    checks = [r for r in requests if r["path"] == "/api/auth/buff/session/check"]
    assert len(checks) == 1
    assert checks[0]["method"] == "POST"
    assert checks[0]["headers"].get("x-aetherswap-intent") == "buff-session"
    assert checks[0]["body"] == {}
    assert not page.locator("#btn-buff-check").is_disabled()
    assert not [r for r in requests if r["path"] in {"/api/purchase", "/api/start", "/api/pipeline/start"}]
    assert errors == []


def test_dashboard_status_refresh_reloads_cached_session_and_replacement_protection(page_factory):
    first = _session(
        "valid", checked_at=1_790_000_000, message="旧缓存", can_replace=True
    )
    blocked = _session(
        "valid",
        checked_at=1_790_000_000,
        message="缓存仍有效",
        can_replace=False,
        replace_block_reason="买入任务运行中，结束任务后才能替换会话",
    )
    page, requests, errors, _ = page_factory(
        session_sequence=[first, blocked],
    )
    assert not page.locator("#btn-buff-import").is_disabled()

    page.evaluate("Dashboard.renderStatus({})")
    page.wait_for_function(
        "document.getElementById('btn-buff-import').disabled === true"
    )
    assert page.locator("#buff-session-message").inner_text() == "缓存仍有效"
    assert "买入任务运行中" in page.locator("#buff-replace-reason").inner_text()
    cached = [r for r in requests if r["path"] == "/api/auth/buff/session" and r["method"] == "GET"]
    assert len(cached) == 2
    assert not [r for r in requests if r["path"].startswith("/api/auth/buff/session/") and r["method"] == "POST"]
    assert not [r for r in requests if r["path"] in {"/api/purchase", "/api/start", "/api/pipeline/start"}]
    assert errors == []


def test_stale_cached_get_cannot_overwrite_newer_check_result(page_factory):
    old = _session("expired", checked_at=1_790_000_000, message="旧缓存", can_replace=True)
    checked = _session("valid", checked_at=1_780_000_000, message="新检测结果", can_replace=True)
    page, requests, errors, _ = page_factory(
        session=old,
        check_result={"ok": True, "session": checked},
    )
    page.evaluate("""stale => {
      window.__staleGetResolvers = [];
      const realFetch = window.fetch.bind(window);
      window.fetch = (url, options = {}) => {
        const method = String(options.method || 'GET').toUpperCase();
        if (String(url).endsWith('/api/auth/buff/session') && method === 'GET') {
          return new Promise(resolve => {
            window.__staleGetResolvers.push(() => resolve(new Response(
              JSON.stringify({ok: true, session: stale}),
              {status: 200, headers: {'Content-Type': 'application/json'}}
            )));
          });
        }
        return realFetch(url, options);
      };
      void BuffSession.refreshCachedSession();
    }""", old)
    page.wait_for_function("window.__staleGetResolvers.length === 1")

    page.locator("#btn-buff-check").click()
    page.wait_for_function(
        "document.getElementById('buff-session-badge').textContent === '最近检测有效'"
    )
    page.evaluate("window.__staleGetResolvers[0]()")
    page.wait_for_timeout(120)
    assert page.locator("#buff-session-badge").inner_text() == "最近检测有效"
    assert page.locator("#buff-session-message").inner_text() == "新检测结果"
    checks = [r for r in requests if r["path"] == "/api/auth/buff/session/check"]
    assert len(checks) == 1
    assert not [r for r in requests if r["path"] in {"/api/purchase", "/api/start", "/api/pipeline/start"}]
    assert errors == []


def test_check_runs_independently_and_uses_intent_header_once(page_factory):
    cached = _session(
        "expired",
        checked_at=1_790_000_000,
        message="旧会话已失效",
        can_replace=False,
        replace_block_reason="买入运行中",
    )
    checked = _session(
        "valid",
        checked_at=1_780_000_000,
        message="只读检测通过",
        can_replace=True,
    )
    page, requests, errors, _ = page_factory(
        session=cached,
        check_result={"ok": True, "session": checked},
    )
    page.locator("#btn-buff-import").wait_for(state="visible")
    assert page.locator("#btn-buff-import").is_disabled()
    assert not page.locator("#btn-buff-check").is_disabled()

    page.locator("#btn-buff-check").click()
    page.wait_for_function("document.getElementById('buff-session-badge').textContent === '最近检测有效'")
    assert page.locator("#buff-session-message").inner_text() == "只读检测通过"
    assert "2026" in page.locator("#buff-session-checked-at").inner_text()
    assert "当前未阻止替换" in page.locator("#buff-replace-reason").inner_text()
    assert not page.locator("#btn-buff-import").is_disabled()
    assert not page.locator("#btn-buff-manual").is_disabled()

    page.wait_for_timeout(150)
    checks = [r for r in requests if r["path"] == "/api/auth/buff/session/check"]
    assert len(checks) == 1
    assert checks[0]["method"] == "POST"
    assert checks[0]["body"] == {}
    assert checks[0]["headers"].get("x-aetherswap-intent") == "buff-session"
    assert not [r for r in requests if r["path"] in {"/api/purchase", "/api/start", "/api/pipeline/start"}]
    assert errors == []


def test_all_session_statuses_keep_history_semantics_and_show_retry(page_factory):
    page, _, errors, _ = page_factory(session=_session("unchecked"))
    expected = {
        "unconfigured": "未配置",
        "unchecked": "尚未检测",
        "valid": "最近检测有效",
        "expired": "最近检测已过期",
        "verification": "需要安全验证",
        "rate_limited": "请求受限",
        "error": "检测异常",
        "account_mismatch": "账号不匹配",
    }
    for status, label in expected.items():
        page.evaluate(
            "session => BuffSession.renderSession(session)",
            _session(
                status,
                checked_at=1_700_000_000,
                message=f"{status} 的最近检测说明",
                can_replace=status not in {"verification", "account_mismatch"},
                replace_block_reason="需要先解决会话问题",
                retry_after=45 if status == "rate_limited" else None,
            ),
        )
        assert page.locator("#buff-session-badge").inner_text() == label
        assert page.locator("#buff-session-message").inner_text() == f"{status} 的最近检测说明"
        assert "2023" in page.locator("#buff-session-checked-at").inner_text()
        if status == "rate_limited":
            assert page.locator("#buff-session-retry").is_visible()
            assert "45 秒" in page.locator("#buff-session-retry").inner_text()
        else:
            assert page.locator("#buff-session-retry").is_hidden()
    assert errors == []


@pytest.mark.parametrize(
    ("width", "height", "viewport_name"),
    [(1440, 1000, "desktop"), (390, 844, "mobile")],
    ids=["desktop", "mobile"],
)
def test_session_card_renders_every_state_without_overflow_on_desktop_and_mobile(
    page_factory, tmp_path, width, height, viewport_name
):
    page, requests, errors, _ = page_factory(
        session=_session("unchecked", can_replace=True),
        width=width,
        height=height,
        mobile=viewport_name == "mobile",
    )
    expected = {
        "unconfigured": "未配置",
        "unchecked": "尚未检测",
        "valid": "最近检测有效",
        "expired": "最近检测已过期",
        "verification": "需要安全验证",
        "rate_limited": "请求受限",
        "error": "检测异常",
        "account_mismatch": "账号不匹配",
    }
    for status, label in expected.items():
        can_replace = status not in {"verification", "account_mismatch"}
        page.evaluate(
            "session => BuffSession.renderSession(session)",
            _session(
                status,
                checked_at=1_700_000_000,
                message=f"{viewport_name} · {status}",
                can_replace=can_replace,
                replace_block_reason="" if can_replace else "当前状态阻止替换",
                retry_after=17 if status == "rate_limited" else None,
            ),
        )
        assert page.locator("#buff-session-badge").inner_text() == label
        assert page.locator("#btn-buff-check").is_visible()
        assert page.locator("#btn-buff-manual").is_visible()
        assert page.locator("#btn-buff-manual").is_disabled() is (not can_replace)
        if viewport_name == "mobile":
            assert page.locator("#btn-buff-import").is_disabled()
            assert "仅桌面" in page.locator("#btn-buff-import").inner_text()
        else:
            assert page.locator("#btn-buff-import").is_disabled() is (not can_replace)
        assert page.locator("#buff-replace-reason").inner_text()
        if status == "rate_limited":
            assert page.locator("#buff-session-retry").is_visible()
            assert "17 秒" in page.locator("#buff-session-retry").inner_text()
        else:
            assert page.locator("#buff-session-retry").is_hidden()

        metrics = page.evaluate("""() => {
          const card = document.getElementById('buff-session-card');
          const actions = card.querySelector('.buff-session-actions');
          const rect = card.getBoundingClientRect();
          return {
            cardOverflow: card.scrollWidth > card.clientWidth + 1,
            actionsOverflow: actions.scrollWidth > actions.clientWidth + 1,
            left: rect.left,
            right: rect.right,
            viewport: window.innerWidth,
          };
        }""")
        assert not metrics["cardOverflow"]
        assert not metrics["actionsOverflow"]
        assert metrics["left"] >= -1
        assert metrics["right"] <= metrics["viewport"] + 1
        page.screenshot(
            path=tmp_path / f"{viewport_name}-{status}.png",
            full_page=True,
        )

    page.evaluate(
        "session => BuffSession.renderSession(session)",
        _session("unchecked", checked_at=None, message="可手动填写", can_replace=True),
    )
    page.locator("#btn-buff-manual").click()
    assert page.locator("#buff-manual-dialog").is_visible()
    page.locator("#btn-buff-manual-cancel").click()
    assert page.locator("#buff-manual-dialog").is_hidden()
    assert not [r for r in requests if r["path"].startswith("/api/auth/buff/session/") and r["method"] == "POST"]
    assert not [r for r in requests if r["path"] in {"/api/purchase", "/api/start", "/api/pipeline/start"}]
    assert errors == []


def test_narrow_desktop_window_keeps_browser_import_enabled(page_factory):
    page, _, errors, _ = page_factory(
        session=_session("valid", checked_at=1_780_000_000, can_replace=True),
        width=390,
        height=844,
    )

    import_button = page.locator("#btn-buff-import")
    assert not import_button.is_disabled()
    assert import_button.inner_text() == "从浏览器导入"
    assert errors == []


def test_mobile_user_agent_disables_browser_import_even_in_wide_viewport(page_factory):
    page, _, errors, _ = page_factory(
        session=_session("valid", checked_at=1_780_000_000, can_replace=True),
        width=1440,
        height=1000,
        mobile=True,
    )

    import_button = page.locator("#btn-buff-import")
    assert import_button.is_disabled()
    assert import_button.inner_text() == "浏览器导入仅桌面"
    assert errors == []


def test_manual_import_cancel_and_success_keep_cookie_out_of_storage_and_logs(page_factory):
    secret = "session=test-cookie-value; buvid=abc"
    user_agent = "Test Browser 1.0"
    imported = _session(
        "valid",
        checked_at=1_780_000_000,
        message="手动会话已由服务端验证",
        can_replace=True,
    )
    page, requests, errors, console = page_factory(
        session=_session("unconfigured", can_replace=True),
        import_result={"ok": True, "session": imported},
    )
    assert page.locator("#buff-manual-dialog").count() == 1

    page.locator("#btn-buff-manual").click()
    page.locator("#buff-manual-advanced summary").click()
    assert page.locator("#buff-manual-dialog").is_visible()
    page.locator("#buff-cookies-input").fill(secret)
    page.locator("#buff-user-agent-input").fill(user_agent)
    page.locator("#btn-buff-manual-cancel").click()
    assert page.locator("#buff-manual-dialog").is_hidden()
    assert not [r for r in requests if r["path"] == "/api/auth/buff/session/import"]

    page.locator("#btn-buff-manual").click()
    page.locator("#buff-cookies-input").fill(secret)
    page.locator("#buff-user-agent-input").fill(user_agent)
    page.evaluate("BuffSession.setActionMessage('旧的检测错误', 'error')")
    page.locator("#btn-buff-manual-submit").click()
    page.wait_for_function(
        "document.getElementById('buff-session-badge').textContent === '最近检测有效'"
    )

    assert page.locator("#buff-manual-dialog").is_hidden()
    assert page.locator("#buff-cookies-input").input_value() == ""
    assert page.locator("#buff-session-message").inner_text() == "手动会话已由服务端验证"
    imports = [r for r in requests if r["path"] == "/api/auth/buff/session/import"]
    assert len(imports) == 1
    assert imports[0]["method"] == "POST"
    assert imports[0]["headers"].get("x-aetherswap-intent") == "buff-session"
    assert imports[0]["body"] == {
        "cookies": secret,
        "user_agent": user_agent,
        "source": "manual",
        "recovery": True,
    }
    assert secret not in "\n".join(console)
    assert page.evaluate("Object.keys(localStorage).length + Object.keys(sessionStorage).length") == 0
    assert not [r for r in requests if r["path"] in {"/api/purchase", "/api/start", "/api/pipeline/start"}]
    assert errors == []


def test_manual_dialog_stays_open_while_import_result_is_unconfirmed(page_factory):
    secret = "session=do-not-abort-after-submit"
    imported = _session(
        "valid",
        checked_at=1_780_000_000,
        message="请求完成后已刷新服务端状态",
        can_replace=True,
    )
    page, requests, errors, console = page_factory(
        session=_session("unconfigured", can_replace=True),
        import_result={"ok": True, "session": imported},
    )
    page.evaluate("""() => {
      const realFetch = window.fetch.bind(window);
      window.fetch = (url, options = {}) => {
        if (String(url).endsWith('/api/auth/buff/session/import')) {
          return new Promise((resolve, reject) => {
            window.__resolveManualImport = () => realFetch(url, options).then(resolve, reject);
          });
        }
        return realFetch(url, options);
      };
    }""")

    page.locator("#btn-buff-manual").click()
    page.locator("#buff-cookies-input").fill(secret)
    page.locator("#btn-buff-manual-submit").click()
    page.wait_for_function("window.__resolveManualImport && document.getElementById('btn-buff-manual-submit').disabled")
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)

    assert page.locator("#buff-manual-dialog").is_visible()
    assert page.locator("#buff-cookies-input").input_value() == secret
    assert page.evaluate("BuffSession.manualInFlight") is True

    page.evaluate("window.__resolveManualImport()")
    page.wait_for_function("document.getElementById('buff-session-badge').textContent === '最近检测有效'")
    assert page.locator("#buff-manual-dialog").is_hidden()
    assert secret not in "\n".join(console)
    assert errors == []


def test_manual_import_defaults_user_agent_without_requiring_advanced_override(page_factory):
    secret = "session=default-user-agent-cookie"
    imported = _session(
        "valid",
        checked_at=1_780_000_000,
        message="默认 User-Agent 导入成功",
        can_replace=True,
    )
    page, requests, errors, console = page_factory(
        session=_session("unconfigured", can_replace=True),
        import_result={"ok": True, "session": imported},
    )

    page.locator("#btn-buff-manual").click()
    advanced = page.locator("#buff-manual-advanced")
    assert advanced.count() == 1
    assert advanced.get_attribute("open") is None
    page.locator("#buff-cookies-input").fill(secret)
    page.locator("#btn-buff-manual-submit").click()
    page.wait_for_function(
        "document.getElementById('buff-session-badge').textContent === '最近检测有效'"
    )

    imports = [r for r in requests if r["path"] == "/api/auth/buff/session/import"]
    assert len(imports) == 1
    assert imports[0]["body"]["user_agent"] == page.evaluate("navigator.userAgent")
    assert imports[0]["body"]["cookies"] == secret
    assert secret not in "\n".join(console)
    assert errors == []


def test_manual_import_failure_exposes_server_reason_and_keeps_cached_session(page_factory):
    secret = "session=must-not-leak; csrf_token=also-secret"
    cached = _session(
        "valid",
        checked_at=1_780_000_000,
        message="原会话仍然有效",
        can_replace=True,
    )
    page, requests, errors, console = page_factory(
        session=cached,
        import_result={
            "ok": False,
            "code": "account_mismatch",
            "error": "导入的 BUFF 绑定 Steam 账号与当前账号不一致",
            "session": cached,
        },
    )

    page.locator("#btn-buff-manual").click()
    page.locator("#buff-cookies-input").fill(secret)
    page.locator("#buff-manual-advanced summary").click()
    page.locator("#buff-user-agent-input").fill("Test Browser 2.0")
    page.locator("#btn-buff-manual-submit").click()
    page.wait_for_function(
        "document.getElementById('buff-manual-error').textContent.includes('账号不一致')"
    )

    assert page.locator("#buff-manual-dialog").is_visible()
    assert page.locator("#buff-session-badge").inner_text() == "最近检测有效"
    assert page.locator("#buff-session-message").inner_text() == "原会话仍然有效"
    assert "账号不一致" in page.locator("#buff-manual-error").inner_text()
    imports = [r for r in requests if r["path"] == "/api/auth/buff/session/import"]
    assert len(imports) == 1
    assert imports[0]["body"]["user_agent"] == "Test Browser 2.0"
    assert secret not in "\n".join(console)
    assert page.evaluate("Object.keys(localStorage).length + Object.keys(sessionStorage).length") == 0
    assert not [r for r in requests if r["path"] in {"/api/purchase", "/api/start", "/api/pipeline/start"}]
    assert errors == []


def test_manual_network_failure_reports_unconfirmed_result_and_asks_for_status_refresh(page_factory):
    page, _, errors, console = page_factory(
        session=_session("valid", checked_at=1_780_000_000, message="旧缓存仍显示有效", can_replace=True),
    )
    page.evaluate("""() => {
      const realFetch = window.fetch.bind(window);
      window.fetch = (url, options = {}) => {
        if (String(url).endsWith('/api/auth/buff/session/import')) {
          return Promise.reject(new Error('network unavailable'));
        }
        return realFetch(url, options);
      };
    }""")

    page.locator("#btn-buff-manual").click()
    page.locator("#buff-cookies-input").fill("session=network-error-secret")
    page.locator("#btn-buff-manual-submit").click()
    page.wait_for_function(
        "document.getElementById('buff-manual-error').textContent.includes('结果未确认')"
    )

    error_text = page.locator("#buff-manual-error").inner_text()
    assert "检测" in error_text
    assert "刷新" in error_text
    assert "不能确认" in error_text
    assert "原会话未被替换" not in error_text
    assert page.locator("#buff-manual-dialog").is_visible()
    assert not page.locator("#btn-buff-manual-cancel").is_disabled()
    assert "network-error-secret" not in "\n".join(console)
    assert errors == []


def test_extension_messages_require_window_origin_request_id_and_never_carry_cookies(page_factory):
    extension = r"""
(() => {
  window.__buffExtensionMessages = [];
  window.addEventListener('message', event => {
    const data = event.data;
    if (!data || data.source !== 'aetherswap-buff-page') return;
    window.__buffExtensionMessages.push({...data});
    // The test sends PONG and RESULT explicitly after each trust check.
  });
})();
"""
    page, requests, errors, console = page_factory(
        session_sequence=[
            _session("expired", checked_at=1_700_000_000, message="旧结果", can_replace=True),
            _session("valid", checked_at=1_780_000_000, message="可信服务端状态", can_replace=True),
        ],
        extension=extension,
    )
    page.locator("#btn-buff-import").click()
    page.wait_for_function(
        "window.__buffExtensionMessages?.some(message => message.type === 'PING')"
    )
    ping = page.evaluate(
        "window.__buffExtensionMessages.find(message => message.type === 'PING')"
    )
    assert ping
    request_id = ping["requestId"]

    def fake_result(result_type="RESULT", *, origin=None, source="window", request_id_value=None):
        return page.evaluate("""args => {
          const data = args.type === 'PONG'
            ? {source: 'aetherswap-buff-extension', type: 'PONG', requestId: args.requestId, ok: true}
            : {source: 'aetherswap-buff-extension', type: 'RESULT', requestId: args.requestId, ok: true,
               session: {status: 'valid', checked_at: 1780000000, message: '伪造结果',
                 can_replace: true, replace_block_reason: ''}};
          const init = {data, origin: args.origin, source: args.source === 'window' ? window : null};
          if (args.source === 'window' && args.useDispatch) {
            window.dispatchEvent(new MessageEvent('message', init));
          } else if (args.source === 'window') {
            window.postMessage(data, location.origin);
          } else {
            window.dispatchEvent(new MessageEvent('message', init));
          }
        }""", {
            "type": result_type,
            "origin": origin or "http://aetherswap.test",
            "source": source,
            "requestId": request_id_value or request_id,
            "useDispatch": source == "window",
        })

    # Wrong source and origin PONGs must not advance to IMPORT.
    fake_result("PONG", source="null")
    fake_result("PONG", origin="https://evil.test")
    page.wait_for_timeout(80)
    assert page.locator("#btn-buff-import").inner_text() == "正在查找扩展…"
    assert [m["type"] for m in page.evaluate("window.__buffExtensionMessages")] == ["PING"]

    # A legitimate same-window PONG advances the protocol.
    page.evaluate("""requestId => window.postMessage({
      source: 'aetherswap-buff-extension', type: 'PONG', requestId, ok: true
    }, location.origin)""", request_id)
    page.wait_for_function(
        "document.getElementById('btn-buff-import').textContent === '等待扩展确认…'"
    )
    import_message = page.evaluate(
        "window.__buffExtensionMessages.find(message => message.type === 'IMPORT')"
    )
    assert import_message
    assert set(import_message) == {"source", "type", "requestId"}
    assert import_message["requestId"] == request_id

    # Wrong requestId, wrong origin and non-window source RESULTs are ignored.
    fake_result(request_id_value="wrong-request")
    fake_result(origin="https://evil.test")
    fake_result(source="null")
    page.wait_for_timeout(80)
    assert page.locator("#btn-buff-import").inner_text() == "等待扩展确认…"
    assert page.locator("#buff-session-badge").inner_text() == "正在导入"

    page.evaluate("""({requestId}) => window.postMessage({
      source: 'aetherswap-buff-extension', type: 'RESULT', requestId, ok: true,
      session: {status: 'valid', checked_at: 1780000000, message: '可信扩展结果',
        can_replace: true, replace_block_reason: ''}
    }, location.origin)""", {"requestId": request_id})
    page.wait_for_function(
        "document.getElementById('buff-session-message').textContent === '可信服务端状态'"
    )
    assert page.locator("#buff-session-badge").inner_text() == "最近检测有效"
    assert page.locator("#btn-buff-import").inner_text() == "从浏览器导入"
    sent = page.evaluate("window.__buffExtensionMessages")
    assert [m["type"] for m in sent] == ["PING", "IMPORT"]
    assert all(set(m) == {"source", "type", "requestId"} for m in sent)
    assert not [r for r in requests if r["path"] == "/api/auth/buff/session/import"]
    assert not [r for r in requests if r["path"] in {"/api/purchase", "/api/start", "/api/pipeline/start"}]
    assert page.evaluate("Object.keys(localStorage).length + Object.keys(sessionStorage).length") == 0
    assert "cookie" not in "\n".join(console).lower()
    assert errors == []


def test_extension_import_waits_for_single_import_result_without_cookie_data(page_factory):
    extension = r"""
(() => {
  window.__buffExtensionMessages = [];
  window.addEventListener('message', event => {
    const data = event.data;
    if (!data || data.source !== 'aetherswap-buff-page') return;
    window.__buffExtensionMessages.push({...data});
    if (data.type === 'PING') {
      window.postMessage({
        source: 'aetherswap-buff-extension', type: 'PONG',
        requestId: data.requestId, ok: true
      }, event.origin);
    }
    if (data.type === 'IMPORT') {
      window.postMessage({
        source: 'aetherswap-buff-extension', type: 'RESULT',
        requestId: data.requestId, ok: true,
        session: {
          status: 'valid', checked_at: 1780000000,
          message: '扩展与会话均已确认', can_replace: true,
          replace_block_reason: ''
        }
      }, event.origin);
    }
  });
})();
"""
    page, requests, errors, console = page_factory(
        session_sequence=[
            _session("expired", checked_at=1_700_000_000, message="旧结果", can_replace=True),
            _session("valid", checked_at=1_780_000_000, message="服务端最终状态", can_replace=True),
        ],
        extension=extension,
    )
    page.locator("#btn-buff-import").click()
    page.wait_for_function(
        "document.getElementById('buff-session-message').textContent === '服务端最终状态'"
    )
    messages = page.evaluate("window.__buffExtensionMessages")
    assert [message["type"] for message in messages] == ["PING", "IMPORT"]
    assert len({message["requestId"] for message in messages}) == 1
    assert page.locator("#buff-session-badge").inner_text() == "最近检测有效"
    assert page.locator("#btn-buff-import").inner_text() == "从浏览器导入"
    assert len([r for r in requests if r["path"] == "/api/auth/buff/session" and r["method"] == "GET"]) == 2
    assert not [r for r in requests if r["path"] == "/api/auth/buff/session/import"]
    assert not [r for r in requests if r["path"] in {"/api/purchase", "/api/start", "/api/pipeline/start"}]
    assert page.evaluate("Object.keys(localStorage).length + Object.keys(sessionStorage).length") == 0
    assert errors == []
    assert "cookie" not in "\n".join(console).lower()


def test_missing_extension_shows_download_and_installation_guide_without_import(page_factory):
    page, requests, errors, _ = page_factory(
        session=_session("expired", checked_at=1_700_000_000, message="旧结果", can_replace=True)
    )
    page.evaluate("""() => {
      window.__pageMessages = [];
      window.addEventListener('message', event => {
        if (event.data?.source === 'aetherswap-buff-page') window.__pageMessages.push({...event.data});
      });
    }""")
    page.locator("#btn-buff-import").click()
    page.locator("#buff-extension-help").wait_for(state="visible", timeout=5000)
    helper_text = page.locator("#buff-extension-help").inner_text()
    assert "未检测到" in helper_text or "尚未授权" in helper_text
    assert "授权当前服务" in helper_text
    assert "刷新页面" in helper_text
    assert page.locator("#buff-extension-help a[download]").get_attribute("href") == "/api/auth/buff/extension.zip"
    assert [message["type"] for message in page.evaluate("window.__pageMessages")] == ["PING"]

    page.locator("#btn-buff-extension-guide").click()
    guide = page.locator("#buff-extension-dialog")
    assert guide.is_visible()
    guide_text = guide.inner_text()
    assert "解压" in guide_text
    assert "chrome://extensions" in guide_text
    assert "开发者模式" in guide_text
    assert "加载已解压的扩展程序" in guide_text
    assert "扩展弹窗" in guide_text
    assert "当前 AetherSwap 地址" in guide_text
    assert "刷新页面" in guide_text
    assert not [r for r in requests if r["path"] == "/api/auth/buff/session/import"]
    assert not [r for r in requests if r["path"] in {"/api/purchase", "/api/start", "/api/pipeline/start"}]
    assert errors == []


def test_unauthorized_extension_prompts_for_authorization_without_import(page_factory):
    extension = r"""
(() => {
  window.__buffExtensionMessages = [];
  window.addEventListener('message', event => {
    const data = event.data;
    if (!data || data.source !== 'aetherswap-buff-page') return;
    window.__buffExtensionMessages.push({...data});
    if (data.type === 'PING') {
      window.postMessage({
        source: 'aetherswap-buff-extension', type: 'PONG',
        requestId: data.requestId, ok: false,
        error: '扩展尚未授权当前服务，请在扩展弹窗中授权并刷新页面。'
      }, event.origin);
    }
  });
})();
"""
    page, requests, errors, _ = page_factory(
        session=_session("unchecked", can_replace=True),
        extension=extension,
    )
    page.locator("#btn-buff-import").click()
    page.locator("#buff-extension-help").wait_for(state="visible", timeout=5000)
    assert "授权" in page.locator("#buff-extension-help").inner_text()
    assert "刷新页面" in page.locator("#buff-extension-help").inner_text()
    assert [message["type"] for message in page.evaluate("window.__buffExtensionMessages")] == ["PING"]
    assert not [r for r in requests if r["path"] == "/api/auth/buff/session/import"]
    assert errors == []


def test_extension_wait_can_be_cancelled_and_late_result_is_ignored(page_factory):
    extension = r"""
(() => {
  window.__buffExtensionMessages = [];
  window.addEventListener('message', event => {
    const data = event.data;
    if (!data || data.source !== 'aetherswap-buff-page') return;
    window.__buffExtensionMessages.push({...data});
    if (data.type === 'PING') {
      window.postMessage({
        source: 'aetherswap-buff-extension', type: 'PONG',
        requestId: data.requestId, ok: true
      }, event.origin);
    }
    if (data.type === 'CANCEL') {
      window.postMessage({
        source: 'aetherswap-buff-extension', type: 'RESULT',
        requestId: data.requestId, ok: false, code: 'cancelled',
        error: '已取消等待确认，未读取或发送 BUFF 会话。'
      }, event.origin);
    }
    // IMPORT deliberately remains pending until the user cancels.
  });
})();
"""
    page, _, errors, _ = page_factory(
        session=_session("expired", checked_at=1_700_000_000, message="保留旧结果", can_replace=True),
        extension=extension,
    )
    page.locator("#btn-buff-import").click()
    page.wait_for_function(
        "document.getElementById('btn-buff-import').textContent === '等待扩展确认…'"
    )
    cancel = page.locator("#btn-buff-cancel-import")
    assert cancel.is_visible()
    assert not page.locator("#btn-buff-check").is_disabled()
    cancel.click()
    page.wait_for_function(
        "document.getElementById('buff-session-message').textContent.includes('已取消等待确认')"
    )
    assert not page.locator("#btn-buff-import").is_disabled()
    assert page.locator("#buff-session-badge").inner_text() == "最近检测已过期"
    cancel_notice = page.locator("#buff-session-message").inner_text()
    assert "CANCEL" not in cancel_notice
    assert "已取消等待确认" in cancel_notice
    assert "未读取或发送" in cancel_notice

    page_messages = page.evaluate("window.__buffExtensionMessages")
    assert [message["type"] for message in page_messages] == ["PING", "IMPORT", "CANCEL"]
    assert len({message["requestId"] for message in page_messages}) == 1

    import_message = page.evaluate(
        "window.__buffExtensionMessages.find(message => message.type === 'IMPORT')"
    )
    assert import_message
    page.evaluate("""requestId => window.postMessage({
      source: 'aetherswap-buff-extension', type: 'RESULT', requestId, ok: true,
      session: {status: 'valid', checked_at: 1780000000, message: '迟到结果',
        can_replace: true, replace_block_reason: ''}
    }, location.origin)""", import_message["requestId"])
    page.wait_for_timeout(120)
    assert page.locator("#buff-session-badge").inner_text() == "最近检测已过期"
    assert len([m for m in page.evaluate("window.__buffExtensionMessages") if m["type"] == "IMPORT"]) == 1
    assert errors == []


def test_extension_cancel_result_unconfirmed_refreshes_cached_server_state(page_factory):
    extension = r"""
(() => {
  window.__buffExtensionMessages = [];
  window.addEventListener('message', event => {
    const data = event.data;
    if (!data || data.source !== 'aetherswap-buff-page') return;
    window.__buffExtensionMessages.push({...data});
    if (data.type === 'PING') {
      window.postMessage({
        source: 'aetherswap-buff-extension', type: 'PONG',
        requestId: data.requestId, ok: true
      }, event.origin);
    }
    if (data.type === 'CANCEL') {
      window.postMessage({
        source: 'aetherswap-buff-extension', type: 'RESULT',
        requestId: data.requestId, ok: false, code: 'result_unconfirmed',
        error: '导入请求已经发出，结果未确认，无法断言是否已保存。请刷新页面或 GET /api/auth/buff/session 查看最新状态。'
      }, event.origin);
    }
  });
})();
"""
    page, requests, errors, _ = page_factory(
        session_sequence=[
            _session("expired", checked_at=1_700_000_000, message="旧的过期结果", can_replace=True),
            _session("valid", checked_at=1_780_000_000, message="刷新后的服务端缓存", can_replace=True),
        ],
        extension=extension,
    )

    page.locator("#btn-buff-import").click()
    page.wait_for_function(
        "document.getElementById('btn-buff-import').textContent === '等待扩展确认…'"
    )
    page.locator("#btn-buff-cancel-import").click()
    page.wait_for_function(
        "document.getElementById('buff-session-message').textContent.includes('结果未确认')"
    )
    page.wait_for_function(
        "document.getElementById('buff-session-badge').textContent === '最近检测有效'"
    )

    notice = page.locator("#buff-session-message").inner_text()
    assert "结果未确认" in notice
    assert "GET /api/auth/buff/session" in notice
    cached = [r for r in requests if r["path"] == "/api/auth/buff/session" and r["method"] == "GET"]
    assert len(cached) == 2
    page_messages = page.evaluate("window.__buffExtensionMessages")
    assert [message["type"] for message in page_messages] == ["PING", "IMPORT", "CANCEL"]
    assert errors == []


def test_extension_import_timeout_sends_cancel_and_reports_unconfirmed_status(page_factory):
    extension = r"""
(() => {
  window.__buffExtensionMessages = [];
  window.addEventListener('message', event => {
    const data = event.data;
    if (!data || data.source !== 'aetherswap-buff-page') return;
    window.__buffExtensionMessages.push({...data});
    if (data.type === 'PING') {
      window.postMessage({
        source: 'aetherswap-buff-extension', type: 'PONG',
        requestId: data.requestId, ok: true
      }, event.origin);
    }
    // No RESULT for IMPORT or CANCEL: the page must fail closed after its timeout.
  });
})();
"""
    page, requests, errors, _ = page_factory(
        session=_session("expired", checked_at=1_700_000_000, message="旧结果", can_replace=True),
        extension=extension,
    )
    page.evaluate("""() => {
      BuffSession.extensionResultTimeout = 80;
      BuffSession.extensionCancelTimeout = 80;
    }""")
    page.locator("#btn-buff-import").click()
    page.wait_for_function(
        "window.__buffExtensionMessages.some(message => message.type === 'CANCEL')"
    )
    page.wait_for_function(
        "document.getElementById('buff-session-message').textContent.includes('取消结果未确认')"
    )

    page_messages = page.evaluate("window.__buffExtensionMessages")
    assert [message["type"] for message in page_messages] == ["PING", "IMPORT", "CANCEL"]
    assert len({message["requestId"] for message in page_messages}) == 1
    assert "检测" in page.locator("#buff-session-message").inner_text()
    cached = [r for r in requests if r["path"] == "/api/auth/buff/session" and r["method"] == "GET"]
    assert len(cached) == 2
    assert not [r for r in requests if r["path"].startswith("/api/auth/buff/session/") and r["method"] == "POST"]
    assert errors == []
