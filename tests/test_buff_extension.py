import contextlib
import http.server
import json
import shutil
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extensions" / "buff-session"


_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>AetherSwap extension test</title></head>
<body>
<h1>AetherSwap</h1>
<script>
  window.__aetherMessages = [];
  window.addEventListener("message", (event) => {
    if (event.source === window && event.origin === location.origin) {
      window.__aetherMessages.push(event.data);
    }
  });
  window.postAether = (type, requestId) => {
    window.postMessage({source: "aetherswap-buff-page", type, requestId}, location.origin);
  };
</script>
</body>
</html>"""


class _FakeService:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.request_started = threading.Event()
        self.release_response = threading.Event()
        self.release_response.set()
        self.response: dict = {
            "status": 200,
            "body": {
                "ok": False,
                "code": "not_configured",
                "error": "test response not configured",
            },
        }


def _start_service() -> tuple[_FakeService, http.server.ThreadingHTTPServer, str]:
    service = _FakeService()
    expected_authorization = "Basic YWV0aGVyOnN3YXA="  # aether:swap

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.headers.get("authorization") != expected_authorization:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="AetherSwap"')
                self.end_headers()
                return
            if self.path != "/":
                self.send_error(404)
                return
            body = _PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length)
            service.requests.append(
                {
                    "path": self.path,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                    "body": body,
                }
            )
            if self.headers.get("authorization") != expected_authorization:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="AetherSwap"')
                self.end_headers()
                return
            if self.path != "/api/auth/buff/session/import":
                self.send_error(404)
                return
            service.request_started.set()
            if not service.release_response.wait(timeout=5):
                self.send_error(504)
                return
            if callable(getattr(service, "import_handler", None)):
                status, response_body = service.import_handler(json.loads(body), dict(self.headers))
            else:
                status, response_body = int(service.response["status"]), service.response["body"]
            payload = json.dumps(response_body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if 300 <= status < 400:
                self.send_header("Location", "/redirected")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return service, server, f"http://127.0.0.1:{port}"


def _prepare_test_extension(tmp_path: Path) -> Path:
    extension = tmp_path / "buff-session"
    shutil.copytree(EXTENSION, extension)
    manifest_path = extension / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Chromium cannot automate its native optional-permission prompt. The test
    # copy starts with the same host pattern granted so the production
    # application-layer exact-origin checks are still exercised for real.
    manifest["host_permissions"].append("http://127.0.0.1/*")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (extension / "test-harness.html").write_text(
        """<!doctype html><html><body><pre id="result">waiting</pre>
<script src="test-harness.js"></script></body></html>""",
        encoding="utf-8",
    )
    (extension / "test-harness.js").write_text(
        """(async () => {
  const output = document.querySelector("#result");
  const origin = new URLSearchParams(location.search).get("origin");
  try {
    const tabs = await chrome.tabs.query({});
    const target = tabs.find((tab) => {
      try { return tab.url && new URL(tab.url).origin === origin; } catch { return false; }
    });
    if (!target) throw new Error("target tab not found");
    const result = await chrome.runtime.sendMessage({
      type: "AUTHORIZE_ORIGIN",
      origin,
      tabId: target.id,
    });
    output.textContent = JSON.stringify(result);
  } catch (error) {
    output.textContent = JSON.stringify({ok: false, error: String(error)});
  }
  document.body.dataset.done = "1";
})();""",
        encoding="utf-8",
    )
    return extension


@contextlib.contextmanager
def _loaded_extension(extension: Path, allowed_origins: list[str], profile: Path):
    playwright_api = __import__("playwright.sync_api", fromlist=["sync_playwright"])
    with playwright_api.sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            channel="chromium",
            headless=True,
            http_credentials={"username": "aether", "password": "swap"},
            args=[
                f"--disable-extensions-except={extension}",
                f"--load-extension={extension}",
            ],
        )

        def isolate(route):
            url = route.request.url
            if url.startswith("chrome-extension://") or any(
                url == origin or url.startswith(origin + "/") for origin in allowed_origins
            ):
                route.continue_()
            else:
                route.abort()

        context.route("**/*", isolate)
        service_worker = (
            context.service_workers[0]
            if context.service_workers
            else context.wait_for_event("serviceworker", timeout=15_000)
        )
        extension_id = service_worker.url.split("/")[2]
        yield context, service_worker, extension_id
        context.close()


def _authorize(context, extension_id: str, origin: str) -> dict:
    harness = context.new_page()
    harness.goto(
        f"chrome-extension://{extension_id}/test-harness.html?origin={quote(origin, safe='')}"
    )
    harness.locator("body[data-done='1']").wait_for(timeout=10_000)
    return json.loads(harness.locator("#result").text_content())


def _send_page_message(page, message_type: str, request_id: str) -> None:
    page.evaluate(
        "([type, id]) => window.postAether(type, id)", [message_type, request_id]
    )


def _wait_thread_event(page, event: threading.Event, timeout_ms: int = 5_000) -> bool:
    for _ in range(max(1, timeout_ms // 50)):
        if event.is_set():
            return True
        page.wait_for_timeout(50)
    return event.is_set()


def _wait_page_message(page, message_type: str, request_id: str, timeout: int = 10_000) -> dict:
    page.wait_for_function(
        """([type, id]) => window.__aetherMessages.some((message) =>
          message && message.source === "aetherswap-buff-extension" &&
          message.type === type && message.requestId === id)""",
        arg=[message_type, request_id],
        timeout=timeout,
    )
    return page.evaluate(
        """([type, id]) => window.__aetherMessages.find((message) =>
          message && message.source === "aetherswap-buff-extension" &&
          message.type === type && message.requestId === id)""",
        [message_type, request_id],
    )


def _request_import_and_wait_confirmation(
    context, extension_id: str, page, request_id: str, timeout: int = 10_000
):
    prefix = f"chrome-extension://{extension_id}/confirm.html?id="
    with context.expect_page(timeout=timeout) as page_info:
        _send_page_message(page, "IMPORT", request_id)
    confirmation = page_info.value
    confirmation.wait_for_url(prefix + "**", timeout=timeout)
    confirmation.wait_for_load_state("domcontentloaded")
    return confirmation


def _manifest() -> dict:
    return json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_scopes_cookie_host_and_makes_service_origins_optional():
    manifest = _manifest()

    assert manifest["manifest_version"] == 3
    assert manifest["incognito"] == "not_allowed"
    assert "cookies" in manifest["permissions"]
    assert "storage" in manifest["permissions"]
    assert "scripting" in manifest["permissions"]
    assert "activeTab" in manifest["permissions"]
    assert manifest["host_permissions"] == ["https://buff.163.com/*"]
    assert set(manifest["optional_host_permissions"]) == {
        "http://*/*",
        "https://*/*",
    }
    assert manifest["background"] == {
        "service_worker": "background.js",
        "type": "module",
    }
    assert manifest["action"]["default_popup"] == "popup.html"

    for name in (
        "background.js",
        "common.js",
        "content-script.js",
        "confirm.html",
        "confirm.js",
        "popup.html",
        "popup.js",
    ):
        assert (EXTENSION / name).is_file(), name


def test_installed_extension_authorizes_only_the_exact_source_port(tmp_path):
    first_service, first_server, first_origin = _start_service()
    second_service, second_server, second_origin = _start_service()
    extension = _prepare_test_extension(tmp_path)
    try:
        with _loaded_extension(
            extension,
            [first_origin, second_origin],
            tmp_path / "profile",
        ) as (context, _service_worker, extension_id):
            first_page = context.new_page()
            first_page.goto(first_origin)
            authorized = _authorize(context, extension_id, first_origin)
            assert authorized["ok"] is True, authorized

            _send_page_message(first_page, "PING", "ping-authorized")
            pong = _wait_page_message(first_page, "PONG", "ping-authorized")
            assert pong["ok"] is True

            second_page = context.new_page()
            second_page.goto(second_origin)
            _send_page_message(second_page, "PING", "ping-other-port")
            denied = _wait_page_message(second_page, "PONG", "ping-other-port")
            assert denied["ok"] is False
            assert denied["error"]
            _send_page_message(second_page, "IMPORT", "import-other-port")
            rejected = _wait_page_message(second_page, "RESULT", "import-other-port")
            assert rejected["ok"] is False
            assert "授权" in rejected["error"]
            assert not any(
                item.url.startswith(f"chrome-extension://{extension_id}/confirm.html")
                for item in context.pages
            )

            assert first_service.requests == []
            assert second_service.requests == []
    finally:
        first_server.shutdown()
        second_server.shutdown()


def test_popup_authorizes_and_revokes_only_the_current_source(tmp_path):
    service, server, origin = _start_service()
    extension = _prepare_test_extension(tmp_path)
    try:
        with _loaded_extension(
            extension, [origin], tmp_path / "profile"
        ) as (context, service_worker, extension_id):
            page = context.new_page()
            page.goto(origin)

            page.bring_to_front()
            with context.expect_page(timeout=10_000) as page_info:
                service_worker.evaluate(
                    "chrome.tabs.create({url: chrome.runtime.getURL('popup.html'), active: false})"
                )
            popup = page_info.value
            popup.wait_for_url(f"chrome-extension://{extension_id}/popup.html")
            popup.wait_for_load_state("domcontentloaded")
            popup.locator("#current-origin").wait_for(state="visible")
            assert popup.locator("#current-origin").text_content() == origin

            popup.locator("#authorize").click()
            popup.wait_for_function(
                "() => document.querySelector('#status')?.textContent.includes('已授权')"
            )
            _send_page_message(page, "PING", "ping-after-authorize")
            pong = _wait_page_message(page, "PONG", "ping-after-authorize")
            assert pong["ok"] is True

            popup.locator("#revoke").click()
            popup.wait_for_function(
                "() => document.querySelector('#status')?.textContent.includes('已撤销')"
            )
            _send_page_message(page, "PING", "ping-after-revoke")
            denied = _wait_page_message(page, "PONG", "ping-after-revoke")
            assert denied["ok"] is False
            _send_page_message(page, "IMPORT", "import-after-revoke")
            rejected = _wait_page_message(page, "RESULT", "import-after-revoke")
            assert rejected["ok"] is False
            assert "授权" in rejected["error"]
            confirmation_prefix = f"chrome-extension://{extension_id}/confirm.html"
            assert not any(
                candidate.url.startswith(confirmation_prefix) for candidate in context.pages
            )
            assert service.requests == []
    finally:
        server.shutdown()


def test_cancel_before_confirmation_never_reads_cookies_or_posts(tmp_path):
    service, server, origin = _start_service()
    extension = _prepare_test_extension(tmp_path)
    try:
        with _loaded_extension(
            extension, [origin], tmp_path / "profile"
        ) as (context, service_worker, extension_id):
            context.add_cookies(
                [
                    {
                        "name": "session",
                        "value": "synthetic-cancelled-session",
                        "domain": "buff.163.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    }
                ]
            )
            page = context.new_page()
            page.goto(origin)
            authorized = _authorize(context, extension_id, origin)
            assert authorized["ok"] is True, authorized
            service_worker.evaluate(
                """() => {
                  const read = chrome.cookies.getAll.bind(chrome.cookies);
                  globalThis.__buffCookieReads = 0;
                  chrome.cookies.getAll = (...args) => {
                    globalThis.__buffCookieReads += 1;
                    return read(...args);
                  };
                }"""
            )

            confirmation = _request_import_and_wait_confirmation(
                context, extension_id, page, "import-cancelled"
            )
            assert service_worker.evaluate("globalThis.__buffCookieReads") == 0
            confirmation.locator("#cancel").click()

            result = _wait_page_message(page, "RESULT", "import-cancelled")
            assert result["ok"] is False
            assert result["code"] == "cancelled"
            assert service_worker.evaluate("globalThis.__buffCookieReads") == 0
            assert service.requests == []
    finally:
        server.shutdown()


def test_page_cancel_closes_confirmation_without_reading_or_sending_cookies(tmp_path):
    service, server, origin = _start_service()
    extension = _prepare_test_extension(tmp_path)
    try:
        with _loaded_extension(
            extension, [origin], tmp_path / "profile"
        ) as (context, service_worker, extension_id):
            context.add_cookies(
                [
                    {
                        "name": "session",
                        "value": "synthetic-cancel-bridge-session",
                        "domain": "buff.163.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    }
                ]
            )
            page = context.new_page()
            page.goto(origin)
            authorized = _authorize(context, extension_id, origin)
            assert authorized["ok"] is True, authorized
            service_worker.evaluate(
                """() => {
                  const read = chrome.cookies.getAll.bind(chrome.cookies);
                  globalThis.__buffCookieReads = 0;
                  chrome.cookies.getAll = (...args) => {
                    globalThis.__buffCookieReads += 1;
                    return read(...args);
                  };
                }"""
            )

            confirmation = _request_import_and_wait_confirmation(
                context, extension_id, page, "import-page-cancel"
            )
            with confirmation.expect_event("close", timeout=5_000):
                _send_page_message(page, "CANCEL", "import-page-cancel")
            result = _wait_page_message(page, "RESULT", "import-page-cancel")

            assert result["ok"] is False
            assert result["code"] == "cancelled"
            assert "未读取或发送" in result["error"]
            assert service_worker.evaluate("globalThis.__buffCookieReads") == 0
            assert service.requests == []
            stored = service_worker.evaluate("chrome.storage.session.get(null)")
            assert not any(key.startswith("pendingImport:") for key in stored)
    finally:
        server.shutdown()


def test_cancel_after_import_post_starts_reports_result_unconfirmed(tmp_path):
    service, server, origin = _start_service()
    service.release_response.clear()
    service.response = {
        "status": 200,
        "body": {
            "ok": True,
            "session": {"status": "valid", "checked_at": None},
        },
    }
    extension = _prepare_test_extension(tmp_path)
    try:
        with _loaded_extension(
            extension, [origin], tmp_path / "profile"
        ) as (context, _service_worker, extension_id):
            context.add_cookies(
                [
                    {
                        "name": "session",
                        "value": "synthetic-inflight-session",
                        "domain": "buff.163.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    }
                ]
            )
            page = context.new_page()
            page.goto(origin)
            authorized = _authorize(context, extension_id, origin)
            assert authorized["ok"] is True, authorized

            confirmation = _request_import_and_wait_confirmation(
                context, extension_id, page, "import-inflight-cancel"
            )
            confirmation.locator("#confirm").click()
            assert _wait_thread_event(page, service.request_started)
            _send_page_message(page, "CANCEL", "import-inflight-cancel")
            result = _wait_page_message(page, "RESULT", "import-inflight-cancel")

            assert result["ok"] is False
            assert result["code"] == "result_unconfirmed"
            assert "结果未确认" in result["error"]
            assert "GET /api/auth/buff/session" in result["error"]
            service.release_response.set()
            page.wait_for_timeout(500)
            matching_results = page.evaluate(
                """() => window.__aetherMessages.filter((message) =>
                  message?.type === "RESULT" &&
                  message.requestId === "import-inflight-cancel")"""
            )
            assert len(matching_results) == 1
    finally:
        service.release_response.set()
        server.shutdown()


def test_missing_buff_login_returns_login_guidance_to_page(tmp_path):
    service, server, origin = _start_service()
    extension = _prepare_test_extension(tmp_path)
    try:
        with _loaded_extension(
            extension, [origin], tmp_path / "profile"
        ) as (context, _service_worker, extension_id):
            page = context.new_page()
            page.goto(origin)
            authorized = _authorize(context, extension_id, origin)
            assert authorized["ok"] is True, authorized

            confirmation = _request_import_and_wait_confirmation(
                context, extension_id, page, "import-not-logged-in"
            )
            with confirmation.expect_event("close", timeout=5_000):
                confirmation.locator("#confirm").click()
            result = _wait_page_message(page, "RESULT", "import-not-logged-in")
            assert result["ok"] is False
            assert result["code"] == "buff_login_required"
            assert "先在 Chrome 打开 https://buff.163.com/" in result["error"]
            assert service.requests == []
    finally:
        server.shutdown()


def test_import_from_subframe_is_ignored(tmp_path):
    service, server, origin = _start_service()
    extension = _prepare_test_extension(tmp_path)
    try:
        with _loaded_extension(
            extension, [origin], tmp_path / "profile"
        ) as (context, service_worker, extension_id):
            page = context.new_page()
            page.goto(origin)
            authorized = _authorize(context, extension_id, origin)
            assert authorized["ok"] is True, authorized

            page.evaluate(
                """() => {
                  const frame = document.createElement('iframe');
                  document.body.append(frame);
                  frame.contentWindow.postMessage({
                    source: 'aetherswap-buff-page',
                    type: 'IMPORT',
                    requestId: 'import-from-subframe',
                  }, location.origin);
                }"""
            )
            page.wait_for_timeout(750)
            assert service.requests == []
            assert not any(
                item.url.startswith(f"chrome-extension://{extension_id}/confirm.html")
                for item in context.pages
            )
            session_storage = service_worker.evaluate("chrome.storage.session.get(null)")
            assert not any(key.startswith("pendingImport:") for key in session_storage)
    finally:
        server.shutdown()


def test_failed_import_returns_only_safe_error_and_status_fields(tmp_path):
    service, server, origin = _start_service()
    session_secret = "synthetic-failed-session-b4d1"
    service.response = {
        "status": 200,
        "body": {
            "ok": False,
            "code": "account_mismatch",
            "error": "候选会话绑定的 Steam 账号与当前账号不一致",
            "session": {
                "status": "account_mismatch",
                "checked_at": None,
                "message": "未替换原会话",
                "can_replace": True,
                "retry_after": None,
                "replace_block_reason": "",
                "cookies": f"session={session_secret}",
                "debug": "must-not-cross-page-boundary",
            },
        },
    }
    extension = _prepare_test_extension(tmp_path)
    try:
        with _loaded_extension(
            extension, [origin], tmp_path / "profile"
        ) as (context, _service_worker, extension_id):
            context.add_cookies(
                [
                    {
                        "name": "session",
                        "value": session_secret,
                        "domain": "buff.163.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    }
                ]
            )
            page = context.new_page()
            page.goto(origin)
            authorized = _authorize(context, extension_id, origin)
            assert authorized["ok"] is True, authorized

            confirmation = _request_import_and_wait_confirmation(
                context, extension_id, page, "import-account-mismatch"
            )
            confirmation.locator("#confirm").click()
            result = _wait_page_message(page, "RESULT", "import-account-mismatch")
            assert result == {
                "source": "aetherswap-buff-extension",
                "type": "RESULT",
                "requestId": "import-account-mismatch",
                "ok": False,
                "code": "account_mismatch",
                "error": "候选会话绑定的 Steam 账号与当前账号不一致",
                "session": {
                    "status": "account_mismatch",
                    "checked_at": None,
                    "message": "未替换原会话",
                    "can_replace": True,
                    "replace_block_reason": "",
                },
            }
            assert session_secret not in json.dumps(result)
            assert session_secret not in page.content()
    finally:
        server.shutdown()


def test_target_navigation_cancels_pending_import_before_cookie_read(tmp_path):
    service, server, origin = _start_service()
    extension = _prepare_test_extension(tmp_path)
    try:
        with _loaded_extension(
            extension, [origin], tmp_path / "profile"
        ) as (context, service_worker, extension_id):
            context.add_cookies(
                [
                    {
                        "name": "session",
                        "value": "synthetic-navigation-session",
                        "domain": "buff.163.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    }
                ]
            )
            page = context.new_page()
            page.goto(origin)
            authorized = _authorize(context, extension_id, origin)
            assert authorized["ok"] is True, authorized

            confirmation = _request_import_and_wait_confirmation(
                context, extension_id, page, "import-before-navigation"
            )
            with confirmation.expect_event("close", timeout=5_000):
                page.goto(origin + "/navigated")
            page.wait_for_timeout(250)
            assert service.requests == []
            session_storage = service_worker.evaluate("chrome.storage.session.get(null)")
            assert not any(key.startswith("pendingImport:") for key in session_storage)
    finally:
        server.shutdown()


def test_import_rejects_redirect_without_following_it(tmp_path):
    service, server, origin = _start_service()
    session_secret = "synthetic-redirect-session-98e2"
    service.response = {
        "status": 302,
        "body": {"ok": False, "error": f"session={session_secret}"},
    }
    extension = _prepare_test_extension(tmp_path)
    try:
        with _loaded_extension(
            extension, [origin], tmp_path / "profile"
        ) as (context, _service_worker, extension_id):
            context.add_cookies(
                [
                    {
                        "name": "session",
                        "value": session_secret,
                        "domain": "buff.163.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    }
                ]
            )
            page = context.new_page()
            page.goto(origin)
            authorized = _authorize(context, extension_id, origin)
            assert authorized["ok"] is True, authorized

            confirmation = _request_import_and_wait_confirmation(
                context, extension_id, page, "import-redirect"
            )
            confirmation.locator("#confirm").click()
            result = _wait_page_message(page, "RESULT", "import-redirect")
            assert result["ok"] is False
            assert "重定向" in result["error"]
            assert "session" not in result
            assert session_secret not in json.dumps(result)
            assert len(service.requests) == 1
            assert service.requests[0]["path"] == "/api/auth/buff/session/import"
    finally:
        server.shutdown()


def test_import_reads_httponly_cookie_after_confirmation_and_returns_only_status(tmp_path):
    service, server, origin = _start_service()
    session_secret = "synthetic-session-secret-7f3a"
    csrf_secret = "synthetic-csrf-secret-a19c"
    service.response = {
        "status": 200,
        "body": {
            "ok": True,
            "session": {
                "status": "valid",
                "checked_at": 1793452800.25,
                "message": "BUFF 会话已验证",
                "retry_after": 42.5,
                "can_replace": False,
                "replace_block_reason": "买入任务正在运行",
                "cookies": f"session={session_secret}",
                "debug": "must-not-cross-page-boundary",
            },
        },
    }
    extension = _prepare_test_extension(tmp_path)
    try:
        with _loaded_extension(
            extension, [origin], tmp_path / "profile"
        ) as (context, service_worker, extension_id):
            context.add_cookies(
                [
                    {
                        "name": "session",
                        "value": session_secret,
                        "domain": "buff.163.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "Lax",
                    },
                    {
                        "name": "csrf_token",
                        "value": csrf_secret,
                        "domain": "buff.163.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": False,
                        "sameSite": "Lax",
                    },
                    {
                        "name": "aetherswap_only",
                        "value": "must-not-be-imported",
                        "url": origin,
                        "httpOnly": True,
                    },
                ]
            )
            page = context.new_page()
            page.goto(origin)
            authorized = _authorize(context, extension_id, origin)
            assert authorized["ok"] is True, authorized

            confirmation = _request_import_and_wait_confirmation(
                context, extension_id, page, "import-success"
            )
            assert origin in confirmation.locator("#target-origin").text_content()
            confirmation_content = confirmation.content()
            assert session_secret not in confirmation_content
            assert csrf_secret not in confirmation_content
            confirmation.locator("#confirm").click()

            result = _wait_page_message(page, "RESULT", "import-success", timeout=15_000)
            assert result == {
                "source": "aetherswap-buff-extension",
                "type": "RESULT",
                "requestId": "import-success",
                "ok": True,
                "session": {
                    "status": "valid",
                    "checked_at": 1793452800.25,
                    "message": "BUFF 会话已验证",
                    "retry_after": 42.5,
                    "can_replace": False,
                    "replace_block_reason": "买入任务正在运行",
                },
            }

            assert len(service.requests) == 1
            request = service.requests[0]
            assert request["path"] == "/api/auth/buff/session/import"
            assert request["headers"]["content-type"] == "application/json"
            assert request["headers"]["x-aetherswap-intent"] == "buff-session"
            assert request["headers"]["authorization"] == "Basic YWV0aGVyOnN3YXA="
            payload = json.loads(request["body"])
            assert payload["source"] == "extension"
            assert payload["recovery"] is True
            assert payload["user_agent"] == page.evaluate("navigator.userAgent")
            assert f"session={session_secret}" in payload["cookies"]
            assert f"csrf_token={csrf_secret}" in payload["cookies"]
            assert "aetherswap_only" not in payload["cookies"]

            page_messages = page.evaluate("window.__aetherMessages")
            assert session_secret not in json.dumps(page_messages)
            assert csrf_secret not in json.dumps(page_messages)
            assert session_secret not in page.content()
            assert csrf_secret not in page.content()
            stored = service_worker.evaluate(
                "Promise.all([chrome.storage.local.get(null), chrome.storage.session.get(null)])"
            )
            assert session_secret not in json.dumps(stored)
            assert csrf_secret not in json.dumps(stored)
    finally:
        server.shutdown()


def test_late_response_from_cancelled_import_does_not_finish_new_import(tmp_path):
    service, server, origin = _start_service()
    service.release_response.clear()
    service.response = {"status": 200, "body": {"ok": True, "session": {"status": "valid"}}}
    extension = _prepare_test_extension(tmp_path)
    try:
        with _loaded_extension(extension, [origin], tmp_path / "profile") as (context, worker, extension_id):
            context.add_cookies([{"name": "session", "value": "synthetic-late", "domain": "buff.163.com", "path": "/", "secure": True, "httpOnly": True}])
            page = context.new_page()
            page.goto(origin)
            assert _authorize(context, extension_id, origin)["ok"]
            first = _request_import_and_wait_confirmation(context, extension_id, page, "late-A")
            first.locator("#confirm").click()
            assert _wait_thread_event(page, service.request_started)
            _send_page_message(page, "CANCEL", "late-A")
            assert _wait_page_message(page, "RESULT", "late-A")["code"] == "result_unconfirmed"
            second = _request_import_and_wait_confirmation(context, extension_id, page, "new-B")
            with page.expect_response(lambda response: response.url.endswith('/api/auth/buff/session/import')) as response_info:
                service.release_response.set()
            response_info.value.finished()
            page.wait_for_timeout(150)
            assert page.evaluate("window.__aetherMessages.filter(m => m.type === 'RESULT' && m.requestId === 'new-B')") == []
            second.locator("#cancel").click()
            assert _wait_page_message(page, "RESULT", "new-B")["code"] == "cancelled"
    finally:
        service.release_response.set()
        server.shutdown()


def test_cancel_immediately_rejects_credentials_while_background_reply_is_delayed(tmp_path):
    service, server, origin = _start_service()
    extension = _prepare_test_extension(tmp_path)
    try:
        with _loaded_extension(extension, [origin], tmp_path / "profile") as (context, worker, extension_id):
            page = context.new_page()
            page.goto(origin)
            assert _authorize(context, extension_id, origin)["ok"]
            confirmation = _request_import_and_wait_confirmation(context, extension_id, page, "cancel-race")
            pending = worker.evaluate("async () => Object.values(await chrome.storage.session.get(null)).find(p => p.requestId === 'cancel-race')")
            worker.evaluate("""async tabId => chrome.scripting.executeScript({
              target: {tabId}, func: () => {
                const send = chrome.runtime.sendMessage.bind(chrome.runtime);
                chrome.runtime.sendMessage = async message => {
                  if (message.type === 'CANCEL_PENDING_FROM_CONTENT') {
                    await new Promise(resolve => { globalThis.releaseCancel = resolve; });
                  }
                  return send(message);
                };
              }
            })""", pending["tabId"])
            _send_page_message(page, "CANCEL", "cancel-race")
            # Wait for the cancellation handler to block, without releasing it.
            worker.evaluate("""async tabId => chrome.scripting.executeScript({
              target: {tabId}, func: async () => {
                while (!globalThis.releaseCancel) await new Promise(r => setTimeout(r, 10));
              }
            })""", pending["tabId"])
            results = worker.evaluate("""async p => {
              const message = {requestId: p.requestId, documentNonce: p.documentNonce, origin: p.origin};
              const verified = await chrome.tabs.sendMessage(p.tabId, {...message, type: 'VERIFY_DOCUMENT'}, {frameId: 0});
              const accepted = await chrome.tabs.sendMessage(p.tabId, {...message, type: 'BUFF_IMPORT_COOKIES', cookies: 'session=synthetic-race'}, {frameId: 0});
              return {verified, accepted};
            }""", pending)
            worker.evaluate("""tabId => chrome.scripting.executeScript({target: {tabId}, func: () => globalThis.releaseCancel()})""", pending["tabId"])
            _wait_page_message(page, "RESULT", "cancel-race")
            assert results["verified"]["ok"] is False
            assert results["accepted"]["ok"] is False
            assert service.requests == []
    finally:
        server.shutdown()


def test_concurrent_origin_revocations_do_not_leave_authorization_or_registration(tmp_path):
    service_a, server_a, origin_a = _start_service()
    service_b, server_b, origin_b = _start_service()
    extension = _prepare_test_extension(tmp_path)
    try:
        with _loaded_extension(extension, [origin_a, origin_b], tmp_path / "profile") as (context, worker, extension_id):
            page_a = context.new_page()
            page_a.goto(origin_a)
            page_b = context.new_page()
            page_b.goto(origin_b)
            assert _authorize(context, extension_id, origin_a)["ok"]
            assert _authorize(context, extension_id, origin_b)["ok"]
            harnesses = [p for p in context.pages if '/test-harness.html?' in p.url]
            # Distinct extension windows issue concurrent revocations. Slow the
            # real storage write so both operations overlap deterministically.
            worker.evaluate("""async () => {
              for (const tab of await chrome.tabs.query({})) {
                if (tab.url?.includes('/test-harness.html?')) await chrome.windows.create({tabId: tab.id, type: 'normal'});
              }
              const set = chrome.storage.local.set.bind(chrome.storage.local);
              chrome.storage.local.set = async values => {
                await new Promise(resolve => setTimeout(resolve, 150));
                return set(values);
              };
            }""")
            for harness, origin in zip(harnesses, [origin_a, origin_b]):
                harness.evaluate("""origin => {
                  globalThis.revocation = chrome.runtime.sendMessage({type: 'REVOKE_ORIGIN', origin});
                }""", origin)
            for harness in harnesses:
                assert harness.evaluate("globalThis.revocation")["ok"]
            states = harnesses[0].evaluate("""origins => Promise.all(origins.map(origin =>
              chrome.runtime.sendMessage({type: 'GET_ORIGIN_STATE', origin})))""", [origin_a, origin_b])
            assert [state["authorized"] for state in states] == [False, False]
            assert worker.evaluate("chrome.scripting.getRegisteredContentScripts()") == []
            assert worker.evaluate("chrome.storage.local.get('authorizedOrigins')") == {"authorizedOrigins": []}
    finally:
        server_a.shutdown()
        server_b.shutdown()
