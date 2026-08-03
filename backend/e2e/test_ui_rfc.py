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
    check("9. No client-side JS errors", not errs, errs[:2])
    b.close()
print("D-045 UI: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
