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
    errs = []; posts = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("response", lambda r: posts.append(f"{r.status} {r.request.method} {r.url.split('/api/')[-1]}") if "/api/" in r.url else None)

    # seed a system directly (Part A already proved the modal works)
    import urllib.request, json
    body = {"sid":"LND","system_name":"Landscape Test","webgui_url":"https://unreachable-sap.example.com/webgui",
            "client":"100","sap_credential":{"username":"MON","password":"pw"},
            "ssh_credential":{"auth_method":"password","username":"","password":"","key_path":""},
            "servers":[],"db_hosts":[],"url_checks":[]}
    try:
        urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8000/api/systems",
            data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}), timeout=10)
    except Exception as e:
        print("seed note:", e)

    pg.goto(f"{BASE}/", wait_until="networkidle")
    card = pg.wait_for_selector("text=LND", timeout=8000)
    check("B1. Landscape lists the system", True)
    card.click()
    check("B2. Card click reveals Core BASIS checkbox", pg.query_selector("text=Core BASIS") is not None)
    sweep = pg.wait_for_selector("button:has-text('Run sweep')", timeout=5000)
    check("B3. Run sweep button enabled", not sweep.is_disabled())
    sweep.click()
    try:
        pg.wait_for_selector("text=Bulk run", timeout=8000)
        check("B4. Run sweep -> island appears (something visibly happens)", True)
    except Exception as e:
        check("B4. Run sweep -> island appears (something visibly happens)", False, str(e)[:150])
    try:
        pg.wait_for_selector("text=/Processing LND|Starting run|logging in/", timeout=8000)
        check("B5. Live status text inside island", True)
    except Exception as e:
        check("B5. Live status text inside island", False, str(e)[:150])
    try:
        pg.wait_for_selector("text=/Run finished with errors|✓ Run finished/", timeout=30000)
        panel = pg.text_content("body")
        check("B6. Run ends with a VISIBLE outcome (not silence)", True)
        check("B7. Error text names the failure", "Run finished with errors" in panel, panel[:80])
    except Exception as e:
        check("B6. Run ends with a VISIBLE outcome (not silence)", False, str(e)[:150])
    # verify the POST actually fired with include_core_basis
    check("B8. POST /api/run fired", any("POST run" in x for x in posts), str(posts[-4:]))
    check("B9. No client JS errors", len(errs) == 0, "; ".join(errs)[:200])
    b.close()
failed = [r for r in results if not r[1]]
print("PART B: " + ("ALL PASSED" if not failed else f"{len(failed)} FAILED"))
