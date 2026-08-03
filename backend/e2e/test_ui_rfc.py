import os
os.environ["LD_LIBRARY_PATH"] = "/tmp/stublib"
from playwright.sync_api import sync_playwright
BASE = "http://127.0.0.1:3000"
ok = True
def check(n, c, d=""):
    global ok; ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--proxy-bypass-list=<-loopback>,127.0.0.1,localhost"])
    pg = b.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f"{BASE}/rfc", wait_until="networkidle")
    check("1. RFC Checks page loads", "RFC Checks" in pg.content())
    check("2. Nav entry present", pg.query_selector("a[href='/rfc']") is not None)
    pg.wait_for_selector("text=Test connection", timeout=8000)
    # checks list loaded from the API
    boxes = pg.query_selector_all("input[type=checkbox]")
    check("3. Check list rendered from /api/rfc/checks", len(boxes) >= 5, f"{len(boxes)} boxes")
    check("4. 'unverified' confidence badge shown", "unverified" in pg.content())
    pg.click("button:has-text('Test connection')")
    pg.wait_for_selector("text=/Connected to|Could not connect/", timeout=15000)
    check("5. Connection test renders a result", "Connected to F4H" in pg.content(),
          pg.inner_text("body")[:300])
    pg.click("button:has-text('Run ')")
    pg.wait_for_selector("text=/checks succeeded/", timeout=20000)
    body = pg.inner_text("body")
    check("6. Sweep ran and reported per-check outcome", "checks succeeded" in body)
    check("7. SM50 work processes rendered", "Work processes" in body and "3011" in body, body[:400])
    check("8. CANCELLED job anomaly surfaced in red list", "Z_FAILED_LOAD" in body and "CANCELLED" in body)
    # --- diagnostic panel: must work from the PAGE, no Swagger/curl needed ---
    pg.wait_for_selector("text=Diagnose a function module", timeout=5000)
    check("10. Diagnostic panel present on the page", True)
    fn = pg.query_selector("input[placeholder='TH_SERVER_LIST']")
    tb = pg.query_selector("input[placeholder='SERVER_LIST, DATA']")
    fn.fill("TH_WPINFO")
    tb.fill("WPLIST")
    pg.click("button:has-text('Call')")
    pg.wait_for_selector("text=returned these top-level fields", timeout=15000)
    body2 = pg.inner_text("body")
    check("11. Diagnose shows the real response field names + row counts",
          "WPLIST" in body2 and "3 rows" in body2, body2[-500:])
    pg.click("button:has-text('Show raw XML')")
    pg.wait_for_selector("pre", timeout=5000)
    check("12. Raw XML viewable for deeper diagnosis",
          "WPLIST" in pg.inner_text("pre"), pg.inner_text("pre")[:200])
    check("13. Still no client-side JS errors after diagnosing", not errs, errs[:2])
    b.close()
print("D-045 UI: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
