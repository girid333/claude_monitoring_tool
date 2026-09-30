"""D-036: flow delete, Core BASIS off by default, flow search, report tabs+dates."""
import os, json, urllib.request
os.environ["LD_LIBRARY_PATH"]="/tmp/stublib"
from playwright.sync_api import sync_playwright
BASE="http://127.0.0.1:3000"; ok=True
def check(n,c,d=""):
    global ok; ok=ok and bool(c)
    print(("PASS " if c else "FAIL ")+n+("" if c else f"  << {d}"))
def post(p, body):
    urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:8000{p}",
        data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}), timeout=10)
post("/api/systems", {"sid":"UX1","system_name":"UX Test","webgui_url":"http://127.0.0.1:8098/webgui",
   "client":"100","hostname":"127.0.0.1","sap_credential":{"username":"M","password":"p"},
   "ssh_credential":{"auth_method":"password","username":"","password":"","key_path":""},
   "servers":[],"db_hosts":[],"url_checks":[]})

# seed reports of BOTH kinds so grouping/tabs have data
import sys, sqlite3
sys.path.insert(0,"/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend")
from pathlib import Path
Path("/tmp/e2e/data/reports").mkdir(parents=True, exist_ok=True)
for n in ("cb.docx","cb.pdf","fl.docx","fl.pdf"):
    Path("/tmp/e2e/data/reports", n).write_bytes(b"x"*2048)
con = sqlite3.connect("/tmp/e2e/data/runs.db")
con.execute("INSERT OR REPLACE INTO report_runs (run_id,sid,label,kind,docx_path,pdf_path,created_at) VALUES (?,?,?,?,?,?,?)",
            ("runCB","UX1","","core_basis","reports/cb.docx","reports/cb.pdf","2026-08-02 09:00:00"))
con.execute("INSERT OR REPLACE INTO report_runs (run_id,sid,label,kind,docx_path,pdf_path,created_at) VALUES (?,?,?,?,?,?,?)",
            ("runFL","UX1","se16_check","flow","reports/fl.docx","reports/fl.pdf","2026-08-01 10:00:00"))
con.commit(); con.close()

with sync_playwright() as p:
    b=p.chromium.launch(headless=True,args=["--proxy-bypass-list=<-loopback>,127.0.0.1,localhost"])
    pg=b.new_page(); errs=[]
    pg.on("pageerror", lambda e: errs.append(str(e)))
    # D-050: Core BASIS lives on /basis, the flow library on /replay.
    pg.goto(f"{BASE}/basis", wait_until="networkidle")

    # 2) Core BASIS unchecked by default
    cb = pg.query_selector("input[type=checkbox]")
    check("1. Core BASIS is UNCHECKED by default", cb and not cb.is_checked())

    pg.goto(f"{BASE}/replay", wait_until="networkidle")

    # 3) search + bulk select present
    check("2. Flow search box present",
          pg.query_selector("input[placeholder*='Search by name']") is not None)
    check("3. Select-all / Clear buttons present",
          pg.query_selector("button:has-text('Select all')") is not None
          and pg.query_selector("button:has-text('Clear')") is not None)
    chips = pg.query_selector_all("span.btn:has-text('_')")
    n_before = len([c for c in chips if "steps" in c.inner_text() or "SE16" in c.inner_text() or "SM37" in c.inner_text()])
    print(f"      flow chips visible: {n_before}")
    if n_before >= 2:
        pg.fill("input[placeholder*='Search by name']", "se16")
        pg.wait_for_timeout(400)
        txt = pg.inner_text("body")
        check("4. Search filters the flow list", "of " in txt and "shown" in txt, txt[:80])
        pg.fill("input[placeholder*='Search by name']", "")
        pg.wait_for_timeout(300)

    # 1) delete a flow
    before = json.loads(urllib.request.urlopen("http://127.0.0.1:8000/api/flows", timeout=10).read())
    if before:
        target = before[0]["flow_id"]
        chip = pg.query_selector(f"span.btn:has-text('{target}')")
        check("5. Delete control exists on a flow chip",
              chip is not None and "🗑" in chip.inner_text(), chip.inner_text() if chip else "no chip")
        chip.query_selector("span:text('🗑')").click()
        pg.wait_for_timeout(300)
        check("6. Delete asks to confirm", pg.query_selector("span:text('delete?')") is not None)
        pg.query_selector("span:text('delete?')").click()
        pg.wait_for_timeout(1200)
        after = json.loads(urllib.request.urlopen("http://127.0.0.1:8000/api/flows", timeout=10).read())
        check("7. Flow actually deleted from disk", len(after) == len(before)-1,
              f"{len(before)} -> {len(after)}")
        check("8. Chip removed from UI",
              pg.query_selector(f"span.btn:has-text('{target}')") is None)

    # 4) Reports: tabs + date grouping + filters
    pg.goto(f"{BASE}/reports", wait_until="networkidle")
    check("9. Separate Core BASIS / Functional flow tabs",
          pg.query_selector("button:has-text('Core BASIS (')") is not None
          and pg.query_selector("button:has-text('Functional flows (')") is not None)
    check("10. Date filter present", pg.query_selector("input[type=date]") is not None)
    check("11. Text search present",
          pg.query_selector("input[placeholder*='Search by']") is not None)
    body = pg.inner_text("body")
    check("12. Reports grouped under a date heading",
          ("Today ·" in body) or ("Yesterday ·" in body) or ("2026-" in body), body[:150])
    pg.click("button:has-text('Functional flows (')")
    pg.wait_for_timeout(400)
    fbody = pg.inner_text("body")
    check("13. Flow tab shows the flow report by NAME", "se16_check" in fbody, fbody[:150])
    pg.fill("input[type=date]", "2026-08-01")
    pg.wait_for_timeout(400)
    check("14. Date filter narrows results", "1 shown" in pg.inner_text("body"),
          pg.inner_text("body")[:120])
    pg.fill("input[type=date]", "2020-01-01")
    pg.wait_for_timeout(400)
    check("15. Non-matching date yields empty state", "0 shown" in pg.inner_text("body"))
    check("16. No client JS errors", len(errs)==0, "; ".join(errs)[:200])
    b.close()
print("D-036: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
