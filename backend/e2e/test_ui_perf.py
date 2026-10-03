"""Real-browser suite for D-052 /performance. Needs the full backend (main.py)
on :8000 and the frontend on :3000 (UI phase of the harness). Starts
fake_sap_ahc_pull.py on :8091 and registers a system through the API."""

import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parents[1]))

import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

BASE = os.environ.get("SX_FRONTEND", "http://127.0.0.1:3000")
API = os.environ.get("SX_BACKEND", "http://127.0.0.1:8000")
SHOTS = Path(os.environ.get("SX_SHOTS", "/tmp/sx_shots"))
PORT = 8091
FAKE = f"http://127.0.0.1:{PORT}"
WEBGUI = f"{FAKE}/sap/bc/gui/sap/its/webgui"


def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def fake(path):
    urllib.request.urlopen(FAKE + path, timeout=5).read()


@pytest.fixture(scope="module", autouse=True)
def env():
    p = subprocess.Popen([sys.executable, str(Path(__file__).parent / "fake_sap_ahc_pull.py"), str(PORT)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    st, body = call("POST", "/api/systems", {"sid": "AAP", "system_name": "Perf test", "webgui_url": WEBGUI,
                                             "client": "100", "hostname": "127.0.0.1",
                                             "sap_credential": {"username": "MONITOR", "password": "secret"}})
    assert st in (200, 201), body
    yield
    call("DELETE", "/api/systems/AAP")
    p.kill()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--proxy-bypass-list=<-loopback>,127.0.0.1,localhost"])
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    fake("/reset")
    ctx = browser.new_context(viewport={"width": 1360, "height": 900}, accept_downloads=True)
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    # 501 = SMON scheduling not enabled (expected); 502 = the harness's other registered system (F4H)
    # has no performance data, and the page auto-loads the first system before the test picks AAP.
    pg.on("console", lambda m: m.type == "error" and "501" not in m.text and "502" not in m.text and pg.errors.append(m.text))
    yield pg
    ctx.close()


def open_perf(pg):
    pg.goto(BASE + "/performance")
    pg.get_by_test_id("px-sid").select_option("AAP")
    expect(pg.get_by_test_id("px-headline")).to_be_visible(timeout=60000)
    expect(pg.get_by_test_id("px-kpi-dia_resp")).to_contain_text("850", timeout=30000)


def test_01_live_view(page):
    open_perf(page)
    expect(page.get_by_test_id("px-kpi-buffers")).to_contain_text("Critical")
    expect(page.get_by_test_id("px-kpi-hana_mem")).to_contain_text("81.2")
    assert page.locator("[data-testid^=px-kpi-]").count() >= 15
    page.locator(".px-chart svg").first.scroll_into_view_if_needed()
    box = page.locator(".px-chart svg").first.bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.55)
    expect(page.locator(".px-tip")).to_contain_text("dialog steps")
    assert page.locator("nav[aria-label=Main] a", has_text="Performance").get_attribute("aria-current") == "page"
    assert page.locator(".sx-top").count() == 1                 # Sentinel page, not inside the app shell
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / "20_performance_live.png"), full_page=True)
    assert not page.errors, page.errors


def test_02_day_analysis(page):
    open_perf(page)
    page.get_by_test_id("px-mode-day").click()
    page.get_by_test_id("px-date").fill((dt.date.today() - dt.timedelta(days=3)).isoformat())
    expect(page.get_by_test_id("px-kpi-dia_resp")).to_contain_text("1,530", timeout=30000)
    expect(page.get_by_test_id("px-kpi-dia_resp")).to_contain_text("Serious")
    expect(page.get_by_test_id("px-headline")).to_have_text("Performance needs attention")
    page.screenshot(path=str(SHOTS / "21_performance_day.png"), full_page=True)
    assert not page.errors, page.errors


def test_03_exports(page, tmp_path):
    open_perf(page)
    for tid, ext, magic in (("px-xlsx", ".xlsx", b"PK"), ("px-pdf", ".pdf", b"%PDF-")):
        with page.expect_download() as dl:
            page.get_by_test_id(tid).click()
        d = dl.value
        assert d.suggested_filename.endswith(ext) and d.suggested_filename.startswith("Performance_QAS_")   # SID as SAP reports it
        f = tmp_path / d.suggested_filename
        d.save_as(f)
        assert f.read_bytes()[:len(magic)] == magic
    from openpyxl import load_workbook
    assert "Overview" in load_workbook(tmp_path / next(p.name for p in tmp_path.iterdir() if p.suffix == ".xlsx")).sheetnames


def test_04_auto_refresh_toggle(page):
    open_perf(page)
    page.get_by_test_id("px-auto").select_option("30")
    expect(page.locator(".px-status")).to_contain_text("every 30 s")
    page.get_by_test_id("px-auto").select_option("0")
    expect(page.locator(".px-pulse")).to_have_count(0)


def test_05_smon_offer_decline_and_schedule(page):
    fake("/perf/today/off")
    open_perf(page)
    expect(page.get_by_test_id("px-smon-offer")).to_be_visible()
    page.get_by_test_id("px-smon-decline").click()
    expect(page.get_by_text("SMON not scheduled — showing the live view from read-only sources.")).to_be_visible()
    page.reload()
    page.get_by_test_id("px-sid").select_option("AAP")
    expect(page.get_by_test_id("px-smon-offer")).to_be_visible(timeout=60000)
    page.once("dialog", lambda d: d.accept())
    page.get_by_test_id("px-smon-schedule").click()
    expect(page.locator(".px-smon .px-note")).to_contain_text("/SDF/SMON")


def test_07_landscape(page):
    fake("/perf/multi/on")
    open_perf(page)
    expect(page.get_by_test_id("ls-nearest-name")).to_have_text("qas_QAS_01")
    expect(page.get_by_test_id("ls-lat-qas_QAS_02")).to_contain_text("Serious")
    expect(page.locator("[data-testid^=ls-app-]")).to_have_count(3)
    expect(page.get_by_test_id("ls-app-qas_QAS_01")).to_contain_text("★")
    expect(page.get_by_test_id("ls-host-hdb3")).to_contain_text("standby")
    expect(page.get_by_test_id("ls-hsr")).to_contain_text("hdb2:30003")
    expect(page.get_by_test_id("px-kpi-hsr_status")).to_contain_text("Active")
    expect(page.get_by_test_id("px-kpi-app_db_rtt")).to_contain_text("Serious")
    bar = page.get_by_test_id("ls-lat-qas_QAS_00")
    bar.scroll_into_view_if_needed()
    bar.hover()
    expect(page.locator(".ls-bars .px-tip")).to_contain_text("SMON NETRTT")
    page.get_by_test_id("ls-nearest").screenshot(path=str(SHOTS / "22_performance_landscape.png"))
    page.get_by_test_id("px-mode-day").click()
    page.get_by_test_id("px-date").fill((dt.date.today() - dt.timedelta(days=3)).isoformat())
    expect(page.get_by_test_id("ls-takeover")).to_contain_text("SEC → PRIM", timeout=30000)
    assert not page.errors, page.errors


def test_06_mobile(browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True)
    pg = ctx.new_page()
    open_perf(pg)
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    ctx.close()
