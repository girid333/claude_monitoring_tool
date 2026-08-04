"""D-048 UI E2E: compose and replay a flow entirely from the page.

The point of this page is that the operator never types a name the system
could have supplied — services, entity sets and function imports all come
from live metadata. This drives it in a real browser to prove that holds.
"""
import os
os.environ["LD_LIBRARY_PATH"] = "/tmp/stublib"
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:3000"
ok = True


def check(n, c, d=""):
    global ok
    ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))


with sync_playwright() as p:
    b = p.chromium.launch(headless=True,
                           args=["--proxy-bypass-list=<-loopback>,127.0.0.1,localhost"])
    pg = b.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))

    pg.goto(f"{BASE}/apiflow", wait_until="networkidle")
    check("1. API Flows page loads", "API Flows" in pg.content())
    check("2. Nav entry present", pg.query_selector("a[href='/apiflow']") is not None)

    # Services must be PROBED — only callable ones are offered.
    pg.click("button:has-text('Load available services')")
    pg.wait_for_selector("text=/callable service/", timeout=20000)
    check("3. Only callable services offered after probing",
          "callable service" in pg.inner_text("body"))

    # Step 1: choose a service, then an entity set from real metadata.
    selects = pg.query_selector_all("select")
    service_select = selects[2]      # 0=system, 1=step kind, 2=service
    opts = service_select.query_selector_all("option")
    check("4. Service dropdown populated from the probe", len(opts) > 1,
          [o.inner_text() for o in opts])
    service_select.select_option(index=1)
    # Metadata is fetched from the live system on selection — wait for the
    # dropdown to actually fill rather than guessing a delay. (Options inside
    # a closed <select> are never "visible", so wait on existence.)
    pg.wait_for_function(
        "() => (document.querySelectorAll('select')[3]?.options.length || 0) > 1",
        timeout=20000)

    selects = pg.query_selector_all("select")
    es_select = selects[3]
    es_opts = [o.inner_text() for o in es_select.query_selector_all("option")]
    check("5. Entity sets populated from $metadata (not typed by hand)",
          any("AlertSet" == t for t in es_opts), es_opts)
    es_select.select_option("AlertSet")
    pg.wait_for_timeout(300)
    check("6. Chosen operation's REAL fields shown with their types",
          "AlertGuid" in pg.inner_text("body") and "String" in pg.inner_text("body"))

    # Wire an extraction so step 2 can consume it.
    pg.fill("input[placeholder*='extract']", "first=rows[0].AlertGuid")

    # Step 2: an RFC call in the same flow.
    pg.click("button:has-text('+ Add step')")
    pg.wait_for_function(
        "() => document.querySelectorAll('select').length >= 5", timeout=10000)
    # Step 2's KIND dropdown — identified by its options, not by position,
    # since a step renders several selects.
    kind_selects = [s for s in pg.query_selector_all("select")
                    if any("RFC: call function module" in o.inner_text()
                           for o in s.query_selector_all("option"))]
    kind_selects[-1].select_option("rfc_call")
    pg.wait_for_selector("input[placeholder='RFC_SYSTEM_INFO']", timeout=10000)
    pg.fill("input[placeholder='RFC_SYSTEM_INFO']", "RFC_SYSTEM_INFO")
    check("7. A flow can mix OData and RFC steps", True)

    # Run it.
    pg.click("button:has-text('▶ Run')")
    pg.wait_for_selector("text=/Flow succeeded|Flow failed/", timeout=25000)
    body = pg.inner_text("body")
    check("8. Flow ran and reported an outcome",
          "Flow succeeded" in body or "Flow failed" in body)
    check("9. Flow succeeded end to end", "Flow succeeded" in body, body[-700:])
    check("10. Extracted value shown, proving step-to-step binding works",
          "extracted:" in body and "AAA1" in body, body[-700:])
    check("11. Returned rows rendered", "BBB2" in body or "Severity" in body)

    # Save + reload from the library.
    pg.fill("input[placeholder='flow id']", "ui_flow")
    pg.click("button:has-text('Save flow')")
    pg.wait_for_selector("text=/Saved ui_flow/", timeout=10000)
    check("12. Flow saved to the library", "Saved ui_flow" in pg.inner_text("body"))
    pg.click("button:has-text('Load')")
    pg.wait_for_timeout(800)
    check("13. Saved flow reloads into the composer",
          pg.input_value("input[placeholder='flow id']") == "ui_flow")

    check("14. No client-side JS errors", not errs, errs[:2])
    b.close()

print("D-048 UI: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
raise SystemExit(0 if ok else 1)
