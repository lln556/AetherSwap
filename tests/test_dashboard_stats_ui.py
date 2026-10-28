"""Render the real dashboard against offline API-shaped settlement summaries."""
from pathlib import Path

import pytest


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


@pytest.fixture
def dashboard(browser):
    page = browser.new_page()
    page.route("**/*", lambda route: route.abort())
    web = Path(__file__).resolve().parents[1] / "web"
    page.set_content((web / "index.html").read_text())
    page.add_script_tag(content='const el = id => document.getElementById(id);')
    page.add_script_tag(content=(web / "js/dashboard.js").read_text())
    yield page
    page.close()


def test_dashboard_displays_net_amounts_per_currency_and_estimate_source(dashboard):
    dashboard.evaluate("stats => Dashboard.renderStats(stats)", {
        "holding_count": 0, "holding_cost": 0, "sold_count": 2, "total_purchased": 35,
        "total_sold": 99999,  # Legacy gross compatibility value must not be displayed.
        "total_profit": 3.21, "discount_ratio": 0.609,
        "proceeds_by_currency": {
            "CNY": {"proceeds_minor": 821, "estimated_count": 1, "unknown_count": 0},
            "USD": {"proceeds_minor": 1000, "estimated_count": 0, "unknown_count": 0},
        },
        "recent_purchases": [{"name": "Foreign sale", "price": 30, "at": 100, "sold": True}],
    })
    assert "CNY 到账 8.21" in dashboard.locator("#stat-proceeds").inner_text()
    assert "USD 到账 10.00" in dashboard.locator("#stat-proceeds").inner_text()
    assert "历史估算" in dashboard.locator("#stat-proceeds").inner_text()
    assert "其他币种未折算" in dashboard.locator("#stat-profit-note").inner_text()
    assert dashboard.locator("#stat-diff").inner_text() == "3.21"
    assert "99,999" not in dashboard.locator("body").inner_text()
    assert dashboard.locator(".dashboard-tag").inner_text() == "已售出"


def test_dashboard_unknown_amounts_and_archived_state_are_explicit(dashboard):
    dashboard.evaluate("stats => Dashboard.renderStats(stats)", {
        "sold_count": 1, "total_profit": None, "discount_ratio": None,
        "proceeds_by_currency": {"CNY": {"proceeds_minor": None, "unknown_count": 1}},
        "recent_purchases": [{"name": "Archived", "state": "archived", "price": 10, "at": 100}],
    })
    assert dashboard.locator("#stat-diff").inner_text() == "—"
    assert dashboard.locator("#stat-ratio").inner_text() == "—"
    assert "金额未知" in dashboard.locator("#stat-proceeds").inner_text()
    assert "未知未计入" in dashboard.locator("#stat-profit-note").inner_text()
    assert dashboard.locator(".dashboard-tag").inner_text() == "已归档"
