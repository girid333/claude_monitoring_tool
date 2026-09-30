"""D-032 UI: expandable anomaly summary + report delete, in a real browser."""
import os, urllib.request, json
os.environ["LD_LIBRARY_PATH"] = "/tmp/stublib"
from playwright.sync_api import sync_playwright
BASE = "http://127.0.0.1:3000"
ok = True
def check(n, c, d=""):
    global ok; ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))
body = {"sid":"DTL","system_name":"Detail Test","webgui_url":"http://127.0.0.1:8099/sap/bc/gui/sap/its/webgui",
        "client":"100","hostname":"127.0.0.1","sap_credential":{"username":"M","password":"p"},
        "ssh_credential":{"auth_method":"password","username":"","password":"","key_path":""},
        "servers":[],"db_hosts":[],"url_checks":[]}
try:
    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8000/api/systems",
        data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}), timeout=10)
except Exception as e: print("seed:", e)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--proxy-bypass-list=<-loopback>,127.0.0.1,localhost"])
    pg = b.new_page(); errs=[]
    pg.on("pageerror", lambda e: errs.append(str(e)))
    # run once so there's history + a report
    pg.goto(f"{BASE}/basis", wait_until="networkidle")   # D-050: Landscape moved from "/"
    pg.wait_for_selector("text=DTL", timeout=8000).click()
    pg.check("label:has-text('Core BASIS sweep') input[type=checkbox]")  # D-036: off by default
    pg.click("button:has-text('Execute')")
    pg.wait_for_selector("text=/Run complete|Run finished with errors/", timeout=40000)
    # every chip must be resolved (none left blue/running)
    running = pg.query_selector_all(".tchip.running")
    check("1. No T-code left stuck 'running' after completion", len(running) == 0, f"{len(running)} stuck")
    done = pg.query_selector_all(".tchip.done, .tchip.warn")
    check("2. All T-codes ended green/orange", len(done) >= 3, f"{len(done)} resolved")

    # Runs page: click a row -> anomaly summary
    pg.goto(f"{BASE}/runs", wait_until="networkidle")
    row = pg.wait_for_selector(".row:has-text('Core BASIS sweep')", timeout=8000)
    row.click()
    try:
        # the expanded panel lists per-T-code anomalies (or an all-clean note)
        pg.wait_for_selector("text=/RFC Error|No anomalies|Infrastructure|Clean:/", timeout=8000)
        check("3. Clicking a run row expands an anomaly summary", True)
        txt = pg.inner_text("body")
        check("4. Summary lists per-T-code detail",
              ("RFC Error" in txt) or ("No anomalies" in txt), txt[:120])
    except Exception as e:
        check("3. Clicking a run row expands an anomaly summary", False, str(e)[:150])

    # Reports page: delete flow
    pg.goto(f"{BASE}/reports", wait_until="networkidle")
    before = len([r for r in pg.query_selector_all(".row") if "run " in r.inner_text()])
    check("5. Reports page lists the generated report", before >= 1, f"{before} rows")
    pg.click("button:has-text('Delete')")
    check("6. Delete asks for confirmation", pg.query_selector("button:has-text('Confirm')") is not None)
    pg.click("button:has-text('Confirm')")
    pg.wait_for_timeout(2500)
    after = len([r for r in pg.query_selector_all(".row") if "run " in r.inner_text()])
    check("7. Report row removed after delete", after < before, f"{before} -> {after}")
    pg.reload(wait_until="networkidle")
    after2 = len([r for r in pg.query_selector_all(".row") if "run " in r.inner_text()])
    check("8. Deletion persists across reload", after2 == 0, f"{after2} rows remain")
    check("9. No client JS errors", len(errs) == 0, "; ".join(errs)[:200])
    b.close()
print("D-032 UI: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
