"""D-031: T-code progress UI, run state surviving navigation, single launcher."""
import os, urllib.request, json
os.environ["LD_LIBRARY_PATH"] = "/tmp/stublib"
from playwright.sync_api import sync_playwright
BASE = "http://127.0.0.1:3000"
results = []
def check(n, c, d=""):
    results.append((n, bool(c)))
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))

body = {"sid":"PRG","system_name":"Progress Test","webgui_url":"http://127.0.0.1:8099/sap/bc/gui/sap/its/webgui",
        "client":"100","hostname":"127.0.0.1","sap_credential":{"username":"M","password":"p"},
        "ssh_credential":{"auth_method":"password","username":"","password":"","key_path":""},
        "servers":[],"db_hosts":[],"url_checks":[]}
try:
    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8000/api/systems",
        data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}), timeout=10)
except Exception as e: print("seed:", e)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--proxy-bypass-list=<-loopback>,127.0.0.1,localhost"])
    pg = b.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))

    # 1. Runs page must NOT have a launcher anymore
    pg.goto(f"{BASE}/runs", wait_until="networkidle")
    # precise: no Execute control and no system-select chips on this page
    check("1. Runs page is history-only (no Execute control)",
          pg.query_selector("button:has-text('Execute')") is None
          and pg.query_selector("input[type=checkbox]") is None)
    check("2. Runs page points to Landscape", pg.query_selector("text=/start a run from/i") is not None)

    # 2. Landscape: select + execute
    pg.goto(f"{BASE}/", wait_until="networkidle")
    pg.wait_for_selector("text=PRG", timeout=8000).click()
    check("3. Landscape has the Execute control", pg.query_selector("button:has-text('Execute')") is not None)
    pg.click("button:has-text('Execute')")

    # 3. T-code checklist appears with real T-codes
    try:
        pg.wait_for_selector(".tchip", timeout=25000)
        chips = pg.query_selector_all(".tchip")
        labels = [c.inner_text().strip() for c in chips[:5]]
        check("4. T-code checklist renders", len(chips) >= 3, f"{len(chips)} chips")
        print("      first chips:", labels)
    except Exception as e:
        check("4. T-code checklist renders", False, str(e)[:150])

    # 4. progress: at least one chip goes running/done
    try:
        pg.wait_for_selector(".tchip.running, .tchip.done", timeout=25000)
        check("5. T-codes light up as they execute", True)
    except Exception as e:
        check("5. T-codes light up as they execute", False, str(e)[:150])

    # 5. progress bar + phase text present
    check("6. Progress bar rendered", pg.query_selector(".pbar") is not None)
    check("7. Phase text shown", "T-code" in pg.inner_text("body") or "Capturing" in pg.inner_text("body"))

    # 6. NAVIGATE AWAY mid-run and come back — state must survive
    pg.click("a[href='/systems']")
    pg.wait_for_selector("text=+ Add System", timeout=8000)
    nav_dot = pg.query_selector("nav .pulse")
    check("8. Sidebar shows live run indicator from another page", nav_dot is not None)
    pg.click("a[href='/']")
    pg.wait_for_selector("text=Landscape", timeout=8000)
    try:
        pg.wait_for_selector(".tchip", timeout=6000)
        chips_after = pg.query_selector_all(".tchip")
        check("9. RUN STATE SURVIVES NAVIGATION (checklist still there)", len(chips_after) >= 3,
              f"{len(chips_after)} chips after nav")
    except Exception as e:
        check("9. RUN STATE SURVIVES NAVIGATION (checklist still there)", False, str(e)[:150])

    # 7. run completes with visible outcome
    try:
        pg.wait_for_selector("text=/Run complete|Run finished with errors/", timeout=40000)
        check("10. Run ends with a visible outcome", True)
    except Exception as e:
        check("10. Run ends with a visible outcome", False, str(e)[:150])

    check("11. No client JS errors", len(errs) == 0, "; ".join(errs)[:200])
    b.close()

failed = [r for r in results if not r[1]]
print("D-031 UI: " + ("ALL PASSED" if not failed else f"{len(failed)} FAILED"))
