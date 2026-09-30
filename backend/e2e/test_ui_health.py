"""Real-browser suite (D-030) for the landing page and health dashboard.
Needs the backend on :8000 (e2e/health_app.py) and the frontend on :3000;
run_ui_health.sh starts both in one shell (sandbox PID-namespace rule)."""

import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parents[1]))


import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from make_ahc_fixture import build  # noqa: E402

BASE = os.environ.get("SX_FRONTEND", "http://127.0.0.1:3000")
SHOTS = Path(os.environ.get("SX_SHOTS", "/tmp/sx_shots"))
REAL = os.environ.get("HEALTH_REAL_AHC")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--proxy-bypass-list=<-loopback>,127.0.0.1,localhost"])
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    ctx = browser.new_context(viewport={"width": 1360, "height": 900})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("console", lambda m: m.type == "error" and pg.errors.append(m.text))
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("response", lambda r: r.status >= 400 and pg.errors.append(f"HTTP {r.status} {r.url}")
          if not (r.status == 422 and "/api/health/reports" in r.url) else None)
    yield pg
    ctx.close()


def synth(tmp_path, name="ahc_synthetic.xls"):
    p = tmp_path / name
    p.write_text(build(), encoding="utf-8")
    return p


def shot(pg, name):
    pg.wait_for_timeout(1500)          # let the ring animation settle before capturing
    SHOTS.mkdir(parents=True, exist_ok=True)
    pg.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def no_hscroll(pg):
    return pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_01_landing_empty_state(page):
    page.goto(BASE + "/")
    expect(page.get_by_role("heading", level=1)).to_contain_text("Know which SAP alerts")
    expect(page.get_by_text("No health check yet")).to_be_visible()
    expect(page.get_by_role("link", name="BASIS monitoring report")).to_be_visible()
    expect(page.get_by_role("link", name="Health check dashboard")).to_be_visible()
    # nav link + landing row, both announce the Lab status with a real space
    expect(page.get_by_role("link", name="Record and replay Lab", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Record and replay Lab Record a transaction")).to_be_visible()
    shot(page, "01_landing_empty")
    page.get_by_role("navigation").get_by_role("link", name="Monitoring").click()
    expect(page).to_have_url(BASE + "/basis")
    expect(page.get_by_role("navigation").get_by_role("link", name="Monitoring")).to_have_attribute("aria-current", "page")
    assert not page.errors, page.errors


def test_02_health_empty_then_import(page, tmp_path):
    page.goto(BASE + "/health")
    expect(page.get_by_role("heading", name="Drop a health check workbook here")).to_be_visible()
    shot(page, "02_health_empty")
    page.get_by_test_id("sx-file").set_input_files(str(synth(tmp_path)))
    v = page.get_by_test_id("sx-verdict")
    expect(v).to_contain_text("SAP flagged 10 of 12 checks")
    expect(v).to_contain_text("7 need action")
    expect(page.get_by_role("heading", name="Needs action")).to_be_visible()
    assert page.locator("[data-testid^=sx-finding-]").count() == 7 + 2 + 2   # act + review + expected
    assert page.locator("[data-testid^=sx-tile-]").count() == 12
    assert "id=" in page.url
    shot(page, "03_health_loaded")
    assert not page.errors, page.errors


def test_03_drawer_evidence_and_keyboard(page):
    page.goto(BASE + "/health")
    card = page.get_by_test_id("sx-finding-sm21-sap-system-log")
    btn = card.get_by_role("button", name="View evidence")
    btn.click()
    dlg = page.get_by_role("dialog")
    expect(dlg).to_be_visible()
    expect(dlg).to_contain_text("SAP's verdict")
    expect(dlg).to_contain_text("R_JR_BTCJOBS_GENERATOR")
    expect(dlg.locator("table").first).to_be_visible()
    # regression: a wide evidence table scrolls inside its own box and never
    # widens the drawer (it used to push the drawer's content off-screen)
    page.wait_for_timeout(400)                      # slide-in animation settles
    geo = dlg.evaluate("d => [d.scrollWidth, d.clientWidth, d.getBoundingClientRect().right, window.innerWidth]")
    assert geo[0] <= geo[1] + 1 and geo[2] <= geo[3] + 1, geo
    expect(page.get_by_role("button", name="Close details")).to_be_focused()
    shot(page, "04_drawer_sm21")
    page.keyboard.press("Escape")
    expect(dlg).to_have_count(0)
    expect(btn).to_be_focused()
    # certificate drawer: derived table + report text
    page.locator("[data-testid^=sx-tile-certificate-info]").click()
    expect(page.get_by_role("dialog")).to_contain_text("OWN certificate")
    expect(page.get_by_role("dialog").locator("th", has_text="Days left")).to_be_visible()
    shot(page, "05_drawer_cert")
    page.get_by_role("button", name="Close details").click()
    assert not page.errors, page.errors


def test_04_filters(page):
    page.goto(BASE + "/health")
    page.get_by_role("group", name="Filter checks").get_by_role("button", name="Expected").click()
    assert page.locator("[data-testid^=sx-tile-]").count() == 2
    page.get_by_role("group", name="Filter checks").get_by_role("button", name="Passed").click()
    assert page.locator("[data-testid^=sx-tile-]").count() == 1
    page.get_by_role("group", name="Filter checks").get_by_role("button", name="All").click()
    assert page.locator("[data-testid^=sx-tile-]").count() == 12


def test_05_bad_file_shows_actionable_error(page, tmp_path):
    page.goto(BASE + "/health")
    bad = tmp_path / "legacy.xls"
    bad.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 200)
    page.get_by_test_id("sx-file").set_input_files(str(bad))
    expect(page.locator(".sx-error")).to_contain_text("XML Spreadsheet 2003")
    expect(page.locator(".sx-error")).to_have_attribute("role", "alert")
    # the previously loaded report is still shown
    expect(page.get_by_test_id("sx-verdict")).to_be_visible()


@pytest.mark.skipif(not REAL, reason="real export not provided")
def test_06_real_export_and_switching(page):
    page.goto(BASE + "/health")
    page.get_by_test_id("sx-file").set_input_files(REAL)
    expect(page.get_by_test_id("sx-verdict")).to_contain_text("SAP flagged 8 of 33 checks")
    shot(page, "06_real_dashboard")
    sel = page.get_by_role("combobox", name="Imported health checks")
    assert sel.locator("option").count() == 2
    qas = sel.locator("option", has_text="QAS").get_attribute("value")
    sel.select_option(qas)
    expect(page.get_by_test_id("sx-verdict")).to_contain_text("10 of 12")
    page.goto(BASE + "/")
    # latest by report date = the real one (Sept) not the synthetic (March)
    expect(page.get_by_role("link", name="Latest:")).to_contain_text("2026-09-23")
    shot(page, "07_landing_with_data")
    assert not page.errors, page.errors


def test_07_mobile_layout(browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True)
    pg = ctx.new_page()
    for path, name in [("/", "08_mobile_landing"), ("/health", "09_mobile_health")]:
        pg.goto(BASE + path)
        pg.wait_for_timeout(600)
        assert no_hscroll(pg), f"horizontal scroll on {path}"
        shot(pg, name)
    pg.locator("[data-testid^=sx-tile-]").first.click()
    box = pg.get_by_role("dialog").bounding_box()
    assert box and box["width"] >= 389
    shot(pg, "10_mobile_drawer")
    ctx.close()


def test_08_remove_import(page):
    page.goto(BASE + "/health")
    page.once("dialog", lambda d: d.accept())
    sel = page.get_by_role("combobox", name="Imported health checks")
    expect(page.get_by_test_id("sx-verdict")).to_be_visible()     # wait for load, then count
    n = sel.locator("option").count()
    assert n >= 1
    page.get_by_role("button", name="Remove this import").click()
    if n == 1:
        expect(page.get_by_role("heading", name="Drop a health check workbook here")).to_be_visible()
    else:
        expect(page.get_by_role("combobox", name="Imported health checks").locator("option")).to_have_count(n - 1)
