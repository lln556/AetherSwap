"""Browser regressions for automatic and user-initiated relogin prompts."""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import pytest


WEB = Path(__file__).resolve().parents[1] / "web"


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


@pytest.fixture
def auth_prompt_page(browser):
    contexts = []

    def make(*, status_payloads):
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        contexts.append(context)
        page = context.new_page()
        errors = []
        status_requests = 0
        payloads = list(status_payloads)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route_request(route):
            nonlocal status_requests
            request = route.request
            url = urlparse(request.url)
            if url.hostname != "aetherswap.test":
                route.abort()
                return
            path = url.path
            if path.startswith("/api/"):
                if path == "/api/status":
                    payload = payloads[min(status_requests, len(payloads) - 1)]
                    status_requests += 1
                    _json(route, payload)
                    return
                if path == "/api/auth/buff/session":
                    _json(route, {
                        "ok": True,
                        "session": {
                            "status": "expired",
                            "checked_at": 1_790_000_000,
                            "message": "BUFF 会话需要更新",
                            "can_replace": True,
                            "replace_block_reason": "",
                        },
                    })
                    return
                if path == "/api/pending_payment":
                    _json(route, {"pending": None})
                    return
                if path == "/api/stats":
                    _json(route, {})
                    return
                if path == "/api/auth/steam/relogin_finish":
                    _json(route, {"ok": True})
                    return
                _json(route, {"ok": False, "error": "unexpected API request"}, status=404)
                return

            file_path = WEB / path.lstrip("/") if path != "/" else WEB / "index.html"
            file_path = file_path.resolve()
            if WEB not in file_path.parents or not file_path.is_file():
                route.fulfill(status=404, body="")
                return
            if file_path.suffix == ".js":
                if file_path.name not in {"utils.js", "accounts.js", "buff-session.js", "dashboard.js", "main.js"}:
                    route.fulfill(body="", content_type="application/javascript")
                    return
                source = file_path.read_text(encoding="utf-8")
                if file_path.name == "main.js":
                    assert source.rstrip().endswith("init();")
                    source = source.rstrip().removesuffix("init();")
                route.fulfill(body=source, content_type="application/javascript")
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
        page.evaluate("""() => {
          _hasAnyAccount = true;
          accountsCurrentId = 'account-a';
        }""")
        return page, errors

    yield make
    for context in contexts:
        context.close()


@pytest.mark.parametrize(
    "buff_flag",
    [
        {"buff_auth_expired": True},
        {"buff_verification_required": True, "buff_verification_reason": "需要完成 BUFF 验证"},
    ],
)
def test_buff_session_entry_suppresses_automatic_legacy_modal(auth_prompt_page, buff_flag):
    status = {
        "status": "error",
        "step": "BUFF_AUTH_EXPIRED",
        "steam_auth_expired": False,
        **buff_flag,
    }
    page, errors = auth_prompt_page(status_payloads=[status])

    page.evaluate("_refreshStatusOnce()")

    assert page.locator("#buff-session-card").is_visible()
    assert page.locator("#buff-session-badge").inner_text() == "最近检测已过期"
    assert page.locator("#relogin-overlay").is_hidden()
    assert errors == []


def test_buff_rate_limit_is_displayed_as_automatic_wait(auth_prompt_page):
    page, errors = auth_prompt_page(status_payloads=[{
        "status": "running", "step": "BUFF_RATE_LIMIT_WAIT",
        "progress_item": "Retry in 300s", "pipeline_active": True,
    }])
    page.evaluate("_refreshStatusOnce()")
    assert page.locator("#task-stage").inner_text() == "等待 BUFF 冷却"
    assert page.locator("#task-state").inner_text() == "自动等待"
    assert "冷却结束后会自动重新筛选" in page.locator("#task-hint").inner_text()
    assert "300 秒后自动重新搜索" in page.locator("#task-current-item").inner_text()
    assert errors == []


def test_steam_expired_prompts_once_per_event_and_account_change(auth_prompt_page):
    expired = {
        "status": "error",
        "step": "STEAM_COOLDOWN",
        "steam_auth_expired": True,
        "buff_auth_expired": False,
    }
    healthy = {**expired, "steam_auth_expired": False}
    page, errors = auth_prompt_page(
        status_payloads=[expired, expired, expired, expired, healthy, expired],
    )

    page.evaluate("_refreshStatusOnce()")
    assert page.locator("#relogin-overlay").is_visible()
    assert page.locator("#relogin-title").inner_text() == "Steam 登录已过期"

    page.evaluate("hideReloginModal()")
    page.locator("#relogin-overlay").wait_for(state="hidden")
    page.evaluate("_refreshStatusOnce()")
    assert page.locator("#relogin-overlay").is_hidden()

    page.evaluate("accountsCurrentId = 'account-b'")
    page.evaluate("_refreshStatusOnce()")
    assert page.locator("#relogin-overlay").is_visible()

    page.evaluate("hideReloginModal()")
    page.locator("#relogin-overlay").wait_for(state="hidden")
    page.evaluate("_refreshStatusOnce()")
    assert page.locator("#relogin-overlay").is_hidden()

    page.evaluate("_refreshStatusOnce()")
    assert page.locator("#relogin-overlay").is_hidden()
    page.evaluate("_refreshStatusOnce()")
    assert page.locator("#relogin-overlay").is_visible()
    assert errors == []


def test_user_initiated_login_can_still_open_relogin_modal(auth_prompt_page):
    page, errors = auth_prompt_page(status_payloads=[{"status": "idle"}])

    page.evaluate("showReloginModal('steam')")

    assert page.locator("#relogin-overlay").is_visible()
    assert page.locator("#relogin-title").inner_text() == "Steam 登录已过期"
    assert errors == []


def test_steam_prompt_waits_until_buff_import_interaction_finishes(auth_prompt_page):
    page, errors = auth_prompt_page(status_payloads=[{'status': 'idle', 'steam_auth_expired': True}] * 4)
    page.evaluate("document.getElementById('buff-manual-dialog').showModal()")
    page.evaluate('_refreshStatusOnce()')
    assert page.locator('#relogin-overlay').is_hidden()
    page.evaluate("document.getElementById('buff-manual-dialog').close(); window.BuffSession.pendingExtension = {requestId:'waiting', phase:'import'}")
    page.evaluate('_refreshStatusOnce()')
    assert page.locator('#relogin-overlay').is_hidden()
    page.evaluate('window.BuffSession.pendingExtension = null')
    page.evaluate('_refreshStatusOnce()')
    assert page.locator('#relogin-overlay').is_visible()
