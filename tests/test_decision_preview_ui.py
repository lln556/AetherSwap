"""Browser acceptance tests for the strategy decision preview UI.

The tests load the real page and application scripts, but every API request is
fulfilled from an isolated in-memory contract fixture. Nothing here touches a
real account, strategy configuration, database, or trading service.
"""
from __future__ import annotations

import json
import mimetypes
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest


WEB = Path(__file__).resolve().parents[1] / "web"
BASE_RECORD = {
    "id": "original",
    "kind": "buy",
    "item": {
        "name": "AK-47 | Redline",
        "market_hash_name": "AK-47 | Redline (Field-Tested)",
    },
    "evaluated_at": "2026-10-28T02:30:00+00:00",
    "strategy": {
        "id": "strategy-stable-buy",
        "name": "稳健购入",
        "strategy_type": "buy",
        "steps": [
            {
                "module_id": "guard.max_discount",
                "name": "成本率上限",
                "enabled": True,
                "params": {"max_discount": 0.70},
            },
            {
                "module_id": "guard.history_data_window",
                "name": "历史稳定性",
                "enabled": True,
                "params": {"history_days": 7},
            },
        ],
    },
    "result": {
        "status": "reject",
        "steps": [
            {
                "module_id": "guard.max_discount",
                "module_name": "成本率上限",
                "status": "reject",
                "actual": 0.74,
                "threshold": 0.70,
                "reason": "成本率 74% 高于上限 70%",
            },
            {
                "module_id": "guard.history_data_window",
                "module_name": "历史稳定性",
                "status": "not_run",
                "reason": "前序检查已阻断，本步骤未执行",
            },
        ],
    },
    "inputs": {
        "cost_ratio": 0.74,
        "currency": "CNY",
        "source": "Steam 市场",
        "collected_at": "2026-10-28T02:29:40+00:00",
        "window": "24h",
        "history": [
            {"at": "2026-10-27T00:00:00Z", "price": 100, "volume": 4},
            None,
            {"at": "2026-10-28T00:00:00Z", "price": 108, "volume": None},
        ],
    },
    "modules": [],
    "schema_version": 1,
}
VERSION_ID = "e498dcee-1e42-4a44-9528-4c9a193170d4"
VERSION = {
    "id": VERSION_ID,
    "created_at": "2026-10-28T03:00:00+00:00",
    "evaluated_at": "2026-10-28T03:00:01+00:00",
    "inputs": {
        "cost_ratio": 0.74,
        "currency": "CNY",
        "source": "Steam 市场（主动刷新）",
        "collected_at": "2026-10-28T02:59:50+00:00",
        "window": "7d",
        "history": [
            {"at": "2026-10-27T00:00:00Z", "price": 101, "volume": 7},
            {"at": "2026-10-27T12:00:00Z", "price": 105, "volume": 4},
            {"at": "2026-10-28T00:00:00Z", "price": 109, "volume": 9},
        ],
    },
}


def _record_for(kind: str) -> dict:
    record = deepcopy(BASE_RECORD)
    record["kind"] = kind
    if kind == "reprice":
        record["strategy"]["strategy_type"] = "sell"
        record["result"] = {
            "status": "wait",
            "steps": [
                {
                    "module_id": "pricing.auto_reprice",
                    "module_name": "自动调价",
                    "status": "wait",
                    "actual": 0.04,
                    "threshold": 0.08,
                    "reason": "价差不足，保持原价",
                }
            ],
        }
    return record


def _new_state(record: dict | None = None, *, total: int | None = None) -> dict:
    record = deepcopy(record or BASE_RECORD)
    item = {
        "id": record["id"],
        "kind": record["kind"],
        "item": deepcopy(record["item"]),
        "evaluated_at": record["evaluated_at"],
        "strategy": {
            "id": record["strategy"]["id"],
            "name": record["strategy"]["name"],
        },
        "result": {"status": record["result"]["status"]},
    }
    return {
        "record": record,
        "items": [item],
        "total": 1 if total is None else total,
        "requests": [],
        "refresh_started": False,
        "refresh_polls": 0,
        "detail_polls": 0,
        "page": 1,
        "refresh_error": None,
        "refresh_lost": False,
        "recording_error": "",
        "simulate_error": None,
        "copy_error": None,
    }


def _json(route, payload, status=200):
    route.fulfill(
        status=status,
        content_type="application/json",
        body=json.dumps(payload, ensure_ascii=False),
    )


def _detail_payload(state: dict) -> dict:
    record = deepcopy(state["record"])
    versions = []
    refresh = None
    if state["refresh_started"]:
        state["detail_polls"] += 1
        if state["refresh_lost"]:
            refresh = None
        elif state["refresh_error"]:
            refresh = {"status": "failed", "id": "refresh-1", "error": state["refresh_error"]}
        elif state["refresh_polls"] == 0:
            state["refresh_polls"] += 1
            refresh = {"status": "running", "id": "refresh-1"}
        else:
            versions = [deepcopy(VERSION)]
            refresh = {
                "status": "succeeded",
                "id": "refresh-1",
                "version_id": VERSION_ID,
            }
    return {
        "ok": True,
        "record": record,
        "versions": versions,
        "refresh": refresh,
    }


def _simulate_payload(state: dict, body: dict) -> dict:
    if state["simulate_error"]:
        return {"ok": False, "error": state["simulate_error"]}
    record = state["record"]
    version_id = body.get("version_id")
    inputs = deepcopy(VERSION["inputs"] if version_id == VERSION_ID else record["inputs"])
    params = body.get("params") or {}
    max_discount = (
        params.get("0", {}).get("max_discount")
        if isinstance(params.get("0"), dict)
        else None
    )
    if max_discount is None:
        max_discount = record["strategy"]["steps"][0]["params"]["max_discount"]
    passes = float(max_discount) >= float(record["inputs"]["cost_ratio"])
    original_step = deepcopy(record["result"]["steps"][0])
    preview_step = {
        **original_step,
        "status": "pass" if passes else "reject",
        "actual": record["inputs"]["cost_ratio"],
        "threshold": max_discount,
        "reason": (
            "成本率 74% 未高于上限 75%"
            if passes
            else "成本率 74% 高于上限 70%"
        ),
    }
    steps = [preview_step, deepcopy(record["result"]["steps"][1])]
    if version_id:
        steps[1] = {
            **steps[1],
            "status": "pass",
            "reason": "新版本历史数据完整，稳定性检查通过",
        }
    preview_strategy = deepcopy(record["strategy"])
    preview_strategy["steps"][0]["params"]["max_discount"] = max_discount
    return {
        "ok": True,
        "record_id": record["id"],
        "version_id": version_id or record["id"],
        "evaluated_at": VERSION["evaluated_at"] if version_id else record["evaluated_at"],
        "original": deepcopy(record["result"]),
        "preview": {"status": "pass" if passes else "reject", "steps": steps},
        "strategy": deepcopy(record["strategy"]),
        "preview_strategy": preview_strategy,
        "inputs": inputs,
        "changed_steps": [0] if max_discount != record["strategy"]["steps"][0]["params"]["max_discount"] else [],
    }


def _make_page(browser, state: dict, *, width=1440, height=1000):
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route_request(route):
        request = route.request
        url = urlparse(request.url)
        if url.hostname != "aetherswap.test":
            route.fulfill(status=200, body="")
            return
        path = url.path
        method = request.method

        if path.startswith("/api/"):
            state["requests"].append(
                {
                    "method": method,
                    "path": path,
                    "query": parse_qs(url.query),
                    "body": request.post_data_json,
                }
            )
            if path == "/api/strategies" and method == "GET":
                _json(
                    route,
                    {
                        "ok": True,
                        "strategies": [],
                        "modules": [],
                        "active": {"buy": "", "sell": ""},
                        "limits": {"buy": 22, "sell": 14},
                        "available_data": {},
                    },
                )
                return
            if path == "/api/strategy-decisions" and method == "GET":
                page_no = int(parse_qs(url.query).get("page", ["1"])[0])
                state["page"] = page_no
                _json(
                    route,
                    {
                        "ok": True,
                        "items": deepcopy(state["items"]) if page_no == 1 else [],
                        "total": state["total"],
                        "page": page_no,
                        "page_size": 20,
                        "retention": {"days": 7, "max_records": 10000},
                        **({"recording_error": state["recording_error"]} if state["recording_error"] else {}),
                    },
                )
                return
            if path == f"/api/strategy-decisions/{state['record']['id']}" and method == "GET":
                _json(route, _detail_payload(state))
                return
            if path == f"/api/strategy-decisions/{state['record']['id']}/refresh" and method == "POST":
                state["refresh_started"] = True
                state["refresh_polls"] = 0
                _json(route, {"ok": True, "refresh": {"id": "refresh-1", "status": "queued"}})
                return
            if path == "/api/strategies/simulate" and method == "POST":
                _json(route, _simulate_payload(state, request.post_data_json or {}))
                return
            if path == f"/api/strategy-decisions/{state['record']['id']}/copy" and method == "POST":
                body = request.post_data_json or {}
                if state["copy_error"]:
                    _json(route, {"ok": False, "error": state["copy_error"]})
                else:
                    strategy = deepcopy(state["record"]["strategy"])
                    strategy.update({"id": "custom.copy.1", "name": body.get("name"), "enabled": False})
                    _json(route, {"ok": True, "strategy": strategy})
                return
            _json(route, {"ok": True})
            return

        rel = path.lstrip("/")
        if not rel:
            target = WEB / "index.html"
        else:
            target = (WEB / rel).resolve()
            assert WEB in target.parents
        if not target.is_file():
            route.fulfill(status=404, body="not found")
            return
        if target.suffix == ".js":
            name = target.name
            if name in {"utils.js", "strategies.js", "decision-preview.js"} or (
                name == "main.js"
            ):
                source = target.read_text(encoding="utf-8")
                if name == "main.js":
                    # Keep the real view helpers but let the test open the strategy panel.
                    source = source.rstrip().removesuffix("init();")
                route.fulfill(status=200, content_type="application/javascript", body=source)
                return
            route.fulfill(status=200, content_type="application/javascript", body="")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        route.fulfill(status=200, path=str(target), content_type=content_type)

    page.route("**/*", route_request)
    page.goto("http://aetherswap.test/", wait_until="domcontentloaded")
    page.evaluate(
        """() => {
          document.querySelectorAll('.panel').forEach((panel) => panel.classList.remove('active'));
          document.getElementById('panel-strategies').classList.add('active');
        }"""
    )
    return context, page, errors


def open_preview(page):
    tab = page.locator('[data-strategy-view="preview"]')
    assert tab.count() == 1
    assert "检查记录" in tab.inner_text()
    tab.click()
    page.locator("#strategy-preview-view").wait_for(state="visible")


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def preview_page(browser):
    pages = []

    def factory(state: dict, **viewport):
        context, page, errors = _make_page(browser, state, **viewport)
        pages.append((context, errors))
        return page

    yield factory
    for context, errors in pages:
        context.close()
        assert errors == []


def test_preview_replaces_example_simulation_with_explicit_empty_state(preview_page):
    state = _new_state()
    state["items"] = []
    state["total"] = 0
    page = preview_page(state)

    open_preview(page)
    page.locator("#decision-empty").wait_for(state="visible")

    assert page.locator("#strategy-btn-simulate").count() == 0
    assert "还没有检查记录" in page.locator("#decision-empty").inner_text()
    assert "从新功能启用后" in page.locator("#decision-empty").inner_text()
    assert "7 天" in page.locator("#decision-retention").inner_text()
    assert "10,000" in page.locator("#decision-retention").inner_text()
    assert not any(
        request["path"] == "/api/strategies/simulate" for request in state["requests"]
    )


def test_filters_and_pagination_use_contract_query(preview_page):
    state = _new_state(total=21)
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record[data-record-id="original"]').wait_for()

    page.locator("#decision-filter-name").fill("AK")
    page.locator("#decision-filter-kind").select_option("buy")
    page.locator("#decision-filter-status").select_option("reject")
    page.locator("#decision-filter-submit").click()
    page.wait_for_function(
        """() => performance.getEntriesByType('resource')
          .some((entry) => entry.name.includes('/api/strategy-decisions?')
            && entry.name.includes('status=reject'))"""
    )
    queries = [
        request["query"]
        for request in state["requests"]
        if request["path"] == "/api/strategy-decisions"
    ]
    assert queries[-1] == {
        "kind": ["buy"],
        "name": ["AK"],
        "status": ["reject"],
        "page": ["1"],
        "page_size": ["20"],
    }

    page.locator("#decision-page-next").click()
    page.wait_for_function("() => document.getElementById('decision-page-label').textContent.includes('第 2')")
    assert state["page"] == 2
    next_query = [
        request["query"]
        for request in state["requests"]
        if request["path"] == "/api/strategy-decisions"
    ][-1]
    assert next_query["page"] == ["2"]
    assert next_query["page_size"] == ["20"]


def test_record_detail_explains_original_result_data_version_and_chart(preview_page):
    state = _new_state()
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record[data-record-id="original"]').click()
    page.locator("#decision-detail").wait_for(state="visible")

    detail = page.locator("#decision-detail")
    text = detail.inner_text()
    assert "AK-47 | Redline" in text
    assert "稳健购入" in text
    assert "拒绝" in text
    assert "成本率 74% 高于上限 70%" in text
    assert "74.00%" in text
    assert "70.00%" in text
    assert "Steam 市场" in text
    assert "24h" in text
    assert "CNY" in text
    assert "未执行" in page.locator("#decision-steps").inner_text()
    assert "成本率 74% 高于上限 70%" in page.locator("#decision-steps .decision-first-blocker").inner_text()

    chart = page.locator("#decision-chart")
    assert chart.get_attribute("data-version-id") == "original"
    assert "价格" in chart.inner_text()
    assert "成交量" in chart.inner_text()
    assert "数据缺口" in chart.inner_text()
    assert "部分数据点缺少成交量" in chart.inner_text()
    assert not any(
        request["path"].endswith("/refresh") for request in state["requests"]
    ), "Opening a record must not refresh market data implicitly"


def test_recording_failure_is_visible_without_hiding_existing_records(preview_page):
    state = _new_state()
    state["recording_error"] = "检查记录写入失败，交易判断仍已正常完成"
    page = preview_page(state)
    open_preview(page)

    warning = page.locator("#decision-recording-warning")
    warning.wait_for(state="visible")
    assert "检查记录写入失败" in warning.inner_text()
    assert page.locator('.decision-record[data-record-id="original"]').is_visible()


def test_parameter_preview_compares_original_and_preview_on_same_version(preview_page):
    state = _new_state()
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record[data-record-id="original"]').click()
    page.locator("#decision-detail").wait_for(state="visible")

    field = page.locator('input[data-preview-param-key="max_discount"][data-step-index="0"]')
    assert field.input_value() == "0.7"
    field.fill("0.75")
    page.locator("#decision-simulate").click()
    page.wait_for_function(
        "() => document.getElementById('decision-comparison').textContent.includes('75.00%')"
    )

    simulation = [
        request
        for request in state["requests"]
        if request["path"] == "/api/strategies/simulate"
    ][-1]
    assert simulation["body"]["record_id"] == "original"
    assert simulation["body"].get("version_id") in (None, "", "original")
    assert simulation["body"]["params"] == {"0": {"max_discount": 0.75}}

    comparison = page.locator("#decision-comparison").inner_text()
    assert "原参数" in comparison
    assert "临时参数" in comparison
    assert "拒绝" in comparison
    assert "通过" in comparison
    assert "参数已变化" in comparison
    assert "成本率 74% 未高于上限 75%" in comparison
    assert page.locator("#decision-original-panel").inner_text().count("拒绝") >= 1


def test_parameter_comparison_only_highlights_values_that_actually_changed(preview_page):
    state = _new_state()
    state["record"]["strategy"]["steps"][0]["params"]["safety_margin"] = 0.05
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record[data-record-id="original"]').click()
    page.locator("#decision-detail").wait_for(state="visible")

    page.locator('input[data-preview-param-key="max_discount"][data-step-index="0"]').fill("0.75")
    page.locator("#decision-simulate").click()
    page.wait_for_function(
        "() => document.getElementById('decision-comparison').textContent.includes('75.00%')"
    )

    max_discount_row = page.locator(".decision-param-compare-row").filter(has_text="成本率上限")
    safety_margin_row = page.locator(".decision-param-compare-row").filter(has_text="safety_margin")
    assert "changed" in (max_discount_row.get_attribute("class") or "")
    assert "changed" not in (safety_margin_row.get_attribute("class") or "")


def test_refresh_polling_recalculates_with_new_version_and_same_chart_data(preview_page):
    state = _new_state()
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record[data-record-id="original"]').click()
    page.locator("#decision-detail").wait_for(state="visible")

    page.locator("#decision-refresh").click()
    page.wait_for_function(
        """(versionId) => {
          const select = document.getElementById('decision-version');
          return select && select.value === versionId;
        }""",
        arg=VERSION_ID,
        timeout=10000,
    )
    page.wait_for_function(
        """(versionId) => {
          const chart = document.getElementById('decision-chart');
          return chart && chart.dataset.versionId === versionId;
        }""",
        arg=VERSION_ID,
        timeout=10000,
    )

    refresh_calls = [
        request
        for request in state["requests"]
        if request["path"] == "/api/strategy-decisions/original/refresh"
    ]
    assert len(refresh_calls) == 1
    assert state["detail_polls"] >= 2
    assert "已完成" in page.locator("#decision-refresh-status").inner_text()
    assert "新数据版本" in page.locator("#decision-version-note").inner_text()

    simulations = [
        request
        for request in state["requests"]
        if request["path"] == "/api/strategies/simulate"
    ]
    assert simulations[-1]["body"]["version_id"] == VERSION_ID
    assert "Steam 市场（主动刷新）" in page.locator("#decision-chart-meta").inner_text()
    assert "7d" in page.locator("#decision-chart-meta").inner_text()
    assert "拒绝" in page.locator("#decision-original-panel").inner_text()


def test_running_refresh_resumes_polling_when_detail_is_reopened(preview_page):
    state = _new_state()
    state["refresh_started"] = True
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record[data-record-id="original"]').click()
    page.locator("#decision-detail").wait_for(state="visible")

    page.wait_for_function(
        """(versionId) => document.getElementById('decision-version')?.value === versionId""",
        arg=VERSION_ID,
        timeout=5000,
    )
    assert state["detail_polls"] >= 2
    assert not any(
        request["path"] == "/api/strategy-decisions/original/refresh"
        for request in state["requests"]
    )


def test_lost_refresh_task_fails_fast_and_keeps_original_record(preview_page):
    state = _new_state()
    state["refresh_lost"] = True
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record[data-record-id="original"]').click()
    page.locator("#decision-detail").wait_for(state="visible")

    page.locator("#decision-refresh").click()
    page.wait_for_function(
        "() => document.getElementById('decision-refresh-status').textContent.includes('丢失')",
        timeout=3000,
    )
    assert "失败" in page.locator("#decision-refresh-status").inner_text()
    assert "成本率 74% 高于上限 70%" in page.locator("#decision-original-panel").inner_text()
    assert page.locator("#decision-refresh").is_enabled()

    detail_requests = [
        request
        for request in state["requests"]
        if request["path"] == "/api/strategy-decisions/original"
    ]
    page.wait_for_timeout(1000)
    assert len([
        request
        for request in state["requests"]
        if request["path"] == "/api/strategy-decisions/original"
    ]) == len(detail_requests), "A lost refresh task must not keep polling"


def test_legacy_simulation_wording_and_entry_are_removed():
    strategies_js = (WEB / "js" / "strategies.js").read_text(encoding="utf-8")
    index_html = (WEB / "index.html").read_text(encoding="utf-8")

    assert "strategy-btn-simulate" not in index_html
    assert "请先模拟运行" not in strategies_js
    assert "完成模拟运行" not in strategies_js
    assert "策略预演" in strategies_js


def test_copy_is_explicit_cancelable_and_never_enables_the_strategy(preview_page):
    state = _new_state()
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record[data-record-id="original"]').click()
    page.locator("#decision-detail").wait_for(state="visible")
    page.locator('input[data-preview-param-key="max_discount"][data-step-index="0"]').fill("0.75")

    page.locator("#decision-copy").click()
    page.locator(".app-modal-input").fill("宽松成本率")
    page.locator('.app-modal-actions button:has-text("取消")').click()
    assert not any(
        request["path"] == "/api/strategy-decisions/original/copy"
        for request in state["requests"]
    )

    page.locator("#decision-copy").click()
    page.locator(".app-modal-input").fill("宽松成本率")
    page.locator('.app-modal-actions button:has-text("保存副本")').click()
    page.wait_for_function(
        """() => performance.getEntriesByType('resource')
          .some((entry) => entry.name.endsWith('/copy'))"""
    )
    copy_request = [
        request
        for request in state["requests"]
        if request["path"] == "/api/strategy-decisions/original/copy"
    ][-1]
    assert copy_request["body"]["name"] == "宽松成本率"
    assert copy_request["body"]["params"] == {"0": {"max_discount": 0.75}}
    assert "尚未启用" in page.locator("#decision-detail").inner_text()


def test_chart_states_when_no_volume_exists_without_manufacturing_data(preview_page):
    state = _new_state()
    state["record"]["inputs"]["history"] = [
        {"at": "2026-10-27T00:00:00Z", "price": 100, "volume": None},
        {"at": "2026-10-28T00:00:00Z", "price": 108, "volume": None},
    ]
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record[data-record-id="original"]').click()
    page.locator("#decision-detail").wait_for(state="visible")

    note = page.locator("#decision-chart").inner_text()
    assert "记录中无可用成交量数据" in note
    assert "未补造" in note


def test_buy_chart_uses_real_market_history_rows_and_nested_metadata(preview_page):
    state = _new_state()
    state["record"]["inputs"].pop("history", None)
    state["record"]["inputs"]["market"] = {
        "history": {
            "available": True,
            "rows": [
                ["Oct 27 2026 01: +0", 115.25, 10],
                ["Oct 27 2026 02: +0", 116.5, 7],
                ["Oct 27 2026 03: +0", 117.75, 0],
            ],
            "currency": "CNY",
            "source": "steam_history",
            "captured_at": 1793068200.0,
        }
    }
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record[data-record-id="original"]').click()
    page.locator("#decision-detail").wait_for(state="visible")

    chart = page.locator("#decision-chart")
    assert chart.locator(".decision-chart-volume").count() == 3
    assert "3 个时间点" in chart.inner_text()
    meta = page.locator("#decision-chart-meta").inner_text()
    assert "steam_history" in meta
    assert "CNY" in meta
    assert "时间未知" not in meta
    assert "1970" not in meta


def test_sell_chart_uses_real_trend_points_and_window_metadata(preview_page):
    state = _new_state(_record_for("sell"))
    state["record"]["inputs"].pop("history", None)
    state["record"]["inputs"].pop("source", None)
    state["record"]["inputs"].pop("collected_at", None)
    state["record"]["inputs"].pop("window", None)
    state["record"]["inputs"]["market"] = {
        "available": True,
        "sell_orders": [{"price": 43.25, "quantity": 2}],
        "source": "steam_market",
        "captured_at": 1793068200.0,
        "currency": "CNY",
    }
    state["record"]["inputs"]["trend"] = {
        "available": True,
        "points": [
            ["Oct 27 2026 01: +0", 41.5, 3],
            {
                "captured_at": 1793061000.0,
                "price": 43.25,
                "volume": 5,
            },
        ],
        "source": "steam_history",
        "captured_at": 1793068200.0,
        "window_days": 7,
    }
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record[data-record-id="original"]').click()
    page.locator("#decision-detail").wait_for(state="visible")

    chart = page.locator("#decision-chart")
    assert chart.locator(".decision-chart-volume").count() == 2
    assert "2 个时间点" in chart.inner_text()
    meta = page.locator("#decision-chart-meta").inner_text()
    assert "steam_history" in meta
    assert "7 天" in meta
    assert "CNY" in meta
    assert "时间未知" not in meta
    assert "1970" not in meta


def test_buy_chart_explains_unavailable_nested_history_without_example_data(preview_page):
    state = _new_state()
    state["record"]["inputs"].pop("history", None)
    state["record"]["inputs"]["market"] = {
        "history": {
            "available": False,
            "reason": "Steam 历史行情暂时受限",
            "rows": [],
            "currency": "CNY",
            "source": "steam_history",
            "captured_at": 1793068200.0,
        }
    }
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record[data-record-id="original"]').click()
    page.locator("#decision-detail").wait_for(state="visible")

    text = page.locator("#decision-chart").inner_text()
    assert "Steam 历史行情暂时受限" in text
    assert "不会生成示例曲线" in text


def test_reprice_record_is_read_only(preview_page):
    state = _new_state(_record_for("reprice"))
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record[data-record-id="original"]').click()
    page.locator("#decision-detail").wait_for(state="visible")

    assert "自动调价记录只读" in page.locator("#decision-readonly").inner_text()
    assert page.locator("[data-preview-param-key]").count() == 0
    assert page.locator("#decision-simulate").count() == 0
    assert page.locator("#decision-copy").count() == 0
    assert "价差不足，保持原价" in page.locator("#decision-steps").inner_text()


def test_missing_statuses_are_distinct(preview_page):
    state = _new_state()
    state["record"]["result"]["steps"] = [
        {"module_id": "a", "module_name": "通过项", "status": "pass", "reason": "通过"},
        {"module_id": "b", "module_name": "等待项", "status": "wait", "reason": "等待"},
        {"module_id": "c", "module_name": "缺数据项", "status": "unavailable", "reason": "缺数据"},
        {"module_id": "d", "module_name": "不支持项", "status": "unsupported", "reason": "不支持"},
        {"module_id": "e", "module_name": "禁用项", "status": "disabled", "reason": "未启用"},
        {"module_id": "f", "module_name": "未执行项", "status": "not_run", "reason": "未执行"},
    ]
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record[data-record-id="original"]').click()
    page.locator("#decision-detail").wait_for(state="visible")
    text = page.locator("#decision-steps").inner_text()
    for label in ("通过", "等待", "数据不可用", "不支持", "未启用", "未执行"):
        assert label in text


@pytest.mark.parametrize("width,height", [(1440, 1000), (390, 844)])
def test_preview_detail_is_readable_without_horizontal_overflow(preview_page, width, height):
    state = _new_state()
    page = preview_page(state, width=width, height=height)
    open_preview(page)
    page.locator('.decision-record[data-record-id="original"]').click()
    page.locator("#decision-detail").wait_for(state="visible")

    assert page.locator("#decision-simulate").is_visible()
    assert page.locator("#decision-refresh").is_visible()
    assert page.locator("#decision-chart").is_visible()
    metrics = page.evaluate(
        """() => {
          const view = document.getElementById('strategy-preview-view');
          const comparison = document.getElementById('decision-comparison');
          return {
            viewScroll: view.scrollWidth,
            viewClient: view.clientWidth,
            comparisonScroll: comparison.scrollWidth,
            comparisonClient: comparison.clientWidth,
          };
        }"""
    )
    assert metrics["viewScroll"] <= metrics["viewClient"] + 1
    assert metrics["comparisonScroll"] <= metrics["comparisonClient"] + 1


def test_repricing_distinguishes_suggestion_from_recorded_execution(preview_page):
    record = _record_for('reprice')
    record['inputs']['currency'] = 'CNY'
    record['result']['suggestion'] = {'action': 'adjust', 'target_buyer_minor': 1234}
    record['result']['execution'] = {
        'status': 'planned', 'job_id': 7, 'withdrawn': False, 'returned': False, 'relisted': False,
    }
    state = _new_state(record)
    page = preview_page(state)
    open_preview(page)
    page.select_option('#decision-filter-kind', 'reprice')
    page.click('#decision-filter-submit')
    page.locator('.decision-record').first.click()
    panel = page.locator('#decision-reprice-execution')
    panel.wait_for(state='visible')
    assert panel.count() == 1
    text = panel.inner_text()
    assert '建议调价' in text
    assert '12.34 CNY' in text
    assert '任务已计划，尚未撤单' in text
    assert '撤单：未发生' in text
    assert '重新上架：未发生' in text
    assert page.locator('#decision-simulate').count() == 0


def test_refresh_compare_failure_never_labels_previous_result_as_new_version(preview_page):
    state = _new_state()
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record').first.click()
    page.locator('#decision-simulate').click()
    page.locator('.decision-result-columns').wait_for()
    state['simulate_error'] = '新版本计算失败'
    page.locator('#decision-refresh').click()
    page.wait_for_function('id => document.getElementById("decision-version").value === id', arg=VERSION_ID)
    page.wait_for_function('() => document.body.innerText.includes("新版本计算失败")')
    assert page.locator('.decision-result-columns').count() == 0
    assert '尚未运行临时参数' in page.locator('#decision-comparison').inner_text()


def test_quantity_of_one_is_not_formatted_as_a_percentage(preview_page):
    state = _new_state()
    state['record']['result']['steps'] = [{
        'module_id': 'guard.purchase_hard_cap', 'status': 'pass',
        'reason': '可购一件', 'actual': 1, 'threshold': 1,
    }]
    page = preview_page(state)
    open_preview(page)
    page.locator('.decision-record').first.click()
    page.locator('#decision-detail').wait_for(state='visible')
    metrics = page.locator('#decision-steps .decision-step-metrics').inner_text()
    assert '100' not in metrics
    assert page.locator('#decision-steps .decision-step-metrics b').all_inner_texts() == ['1', '1']


def test_sell_detail_shows_buyer_and_net_amounts_with_currency(preview_page):
    record = _record_for('sell')
    record['result']['steps'] = [{
        'module_id': 'pricing.price_offset', 'status': 'pass', 'reason': '建议价格已计算',
        'output': {'display_price': 1.17, 'estimated_proceeds': 1, 'currency': 'CNY'},
    }]
    page = preview_page(_new_state(record))
    open_preview(page)
    page.select_option('#decision-filter-kind', 'sell')
    page.click('#decision-filter-submit')
    page.locator('.decision-record').first.click()
    page.locator('#decision-detail').wait_for(state='visible')
    text = page.locator('#decision-original-panel').inner_text()
    assert '买家价格' in text and '1.17 CNY' in text
    assert '预计净到账' in text and '1.00 CNY' in text
