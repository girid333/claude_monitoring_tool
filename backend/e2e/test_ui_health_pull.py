"""Real-browser suite for D-051: "Pull from SAP" on /health and the Excel
export. Needs the full backend (main.py) on :8000 and the frontend on :3000
(run in the UI phase of the harness). Starts fake_sap_ahc_pull.py itself on
:8091 and registers two systems through the real Systems API — one good,
one with a wrong password — then removes them again."""

import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parents[1]))

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from health.workbook import read_workbook

BASE = os.environ.get("SX_FRONTEND", "http://127.0.0.1:3000")
API = os.environ.get("SX_BACKEND", "http://127.0.0.1:8000")
SHOTS = Path(os.environ.get("SX_SHOTS", "/tmp/sx_shots"))
PORT = 8091
WEBGUI = f"http://127.0.0.1:{PORT}/sap/bc/gui/sap/its/webgui"
LAYOUT = json.loads((Path(__file__).resolve().parents[1] / "ahc" / "layout.json").read_text())


def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def system(sid, password):
    return {"sid": sid, "system_name": f"{sid} pull test", "webgui_url": WEBGUI, "client": "100",
            "hostname": "127.0.0.1", "sap_credential": {"username": "MONITOR", "password": password}}


@pytest.fixture(scope="module", autouse=True)
def env():
    fake = subprocess.Popen([sys.executable, str(Path(__file__).parent / "fake_sap_ahc_pull.py"), str(PORT)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    before = {r["id"] for r in call("GET", "/api/health/reports")[1]}
    for sid, pw in (("QAP", "secret"), ("QAX", "wrong")):
        st, body = call("POST", "/api/systems", system(sid, pw))
        assert st in (200, 201), body
    yield
    for r in call("GET", "/api/health/reports")[1]:
        if r["id"] not in before:
            call("DELETE", f"/api/health/reports/{r['id']}")
    for sid in ("QAP", "QAX"):
        call("DELETE", f"/api/systems/{sid}")
    fake.kill()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--proxy-bypass-list=<-loopback>,127.0.0.1,localhost"])
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    ctx = browser.new_context(viewport={"width": 1360, "height": 900}, accept_downloads=True)
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and "502" not in m.text and pg.errors.append(m.text))
    yield pg
    ctx.close()


def shot(pg, name):
    pg.wait_for_timeout(1200)
    SHOTS.mkdir(parents=True, exist_ok=True)
    pg.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def test_01_pull_shows_dashboard_and_exports_workbook(page, tmp_path):
    page.goto(BASE + "/health")
    sel = page.get_by_test_id("sx-pull-sid")
    expect(sel).to_be_visible()
    sel.select_option("QAP")
    page.get_by_test_id("sx-pull").click()
    expect(page.get_by_test_id("sx-verdict")).to_be_visible(timeout=60000)
    expect(page.get_by_text("Pulled directly from SAP")).to_be_visible()
    expect(page.get_by_role("combobox", name="Imported health checks")).to_contain_text("QAS")
    # every check from the sample is on the dashboard
    assert page.locator("[data-testid^=sx-tile-]").count() == 33
    shot(page, "11_pulled_dashboard")

    with page.expect_download() as dl:
        page.get_by_test_id("sx-export").click()
    d = dl.value
    assert d.suggested_filename.endswith(".xlsx")
    path = tmp_path / d.suggested_filename
    d.save_as(path)
    from openpyxl import load_workbook
    wb = load_workbook(path)                                    # a real Office Open XML workbook
    assert wb.sheetnames == [s["name"] for s in LAYOUT["sheets"]]
    sheets = read_workbook(path.read_bytes(), path.name)
    assert list(sheets) == [s["name"] for s in LAYOUT["sheets"]]
    import zipfile
    with zipfile.ZipFile(path) as z:
        assert not any(b"secret" in z.read(n) for n in z.namelist())
    assert not page.errors, page.errors


def test_02_failed_pull_shows_actionable_error(page):
    page.goto(BASE + "/health")
    page.get_by_test_id("sx-pull-sid").select_option("QAX")
    page.get_by_test_id("sx-pull").click()
    alert = page.locator("p.sx-error[role=alert]")
    expect(alert).to_contain_text("Could not pull from QAX", timeout=60000)
    assert "wrong" not in alert.inner_text()
    shot(page, "12_pull_error")
