import os
os.environ["LD_LIBRARY_PATH"] = "/tmp/stublib"
from playwright.sync_api import sync_playwright
BASE = "http://127.0.0.1:3000"
results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  << {detail}"))
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--proxy-bypass-list=<-loopback>,127.0.0.1,localhost"])
    pg = b.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f"{BASE}/systems", wait_until="networkidle")
    pg.click("text=+ Add System")
    pg.wait_for_selector("h2:has-text('Add System')")
    pg.fill("xpath=//label[contains(.,'System name')]/following-sibling::input", "E2E Test System")
    pg.fill("xpath=//label[text()[contains(.,'SID')]]/following-sibling::input", "E2E")
    pg.fill("xpath=//label[text()='Client']/following-sibling::input", "100")
    pg.fill("xpath=//label[text()='WebGUI URL']/following-sibling::input", "https://sap-e2e-unreachable.example.com:44300/webgui")
    pg.fill("xpath=//label[text()='SAP username']/following-sibling::input", "MONITOR")
    pg.fill("xpath=//label[text()='SAP password']/following-sibling::input", "secret-pw")
    pg.click(".modal-content button:has-text('Add system')")
    pg.wait_for_selector("text=E2E Test System", timeout=10000)
    check("A1. Add System via modal -> row appears", True)
    pg.goto(f"{BASE}/runs", wait_until="networkidle")
    pg.wait_for_selector("text=Start a run")
    pg.click("button:has-text('E2E Test System')")
    btn = pg.wait_for_selector("button:has-text('Execute 1 system')", timeout=5000)
    check("A2. Runs launcher: chip select + Execute enabled", not btn.is_disabled())
    btn.click()
    try:
        pg.wait_for_selector("text=/Starting run|Processing E2E/", timeout=10000)
        check("A3. Execute -> live status appears", True)
    except Exception as e:
        check("A3. Execute -> live status appears", False, str(e)[:150])
    try:
        pg.wait_for_selector("text=Run finished with errors", timeout=25000)
        check("A4. Unreachable SAP -> visible red error panel", True)
    except Exception as e:
        check("A4. Unreachable SAP -> visible red error panel", False, str(e)[:150])
    check("A5. No client JS errors", len(errs) == 0, "; ".join(errs)[:200])
    b.close()
failed = [r for r in results if not r[1]]
print("PART A: " + ("ALL PASSED" if not failed else f"{len(failed)} FAILED"))
