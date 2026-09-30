"""D-049d UI E2E: the full data-run workflow from the Record and replay page
(the old Landscape page's flow panel, moved to /replay in D-050).

Mirrors what Giri will do for the SE16 test: pick a system, open a
recording's "run with data" panel, download the template, upload a filled
sheet, run it, watch rows tick past, and open the evidence document.
"""
import os
os.environ["LD_LIBRARY_PATH"] = "/tmp/stublib"
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:3000"
DATA_FILE = os.environ.get("DATARUN_XLSX", "/tmp/dr_data.xlsx")
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

    pg.goto(f"{BASE}/replay", wait_until="networkidle")
    check("1. Record and replay page loads with the recording listed",
          "se16_display" in pg.content(), pg.inner_text("body")[:200])

    # select the system card (the run needs a target)
    pg.click("text=F4H")
    pg.wait_for_timeout(300)

    # open the data-run panel from the flow chip
    pg.click("span[title^='Replay this recording']")
    pg.wait_for_selector("text=Run with data —", timeout=10000)
    check("2. Data-run panel opens for the recording", True)
    # bindings are fetched asynchronously — wait for the table, don't race it
    pg.wait_for_selector("text=Spreadsheet column", timeout=15000)
    body = pg.inner_text("body")
    check("3. Bindings derived from the recording are shown with the sample",
          "Table Name" in body and "T000" in body, body[-600:])

    # the template a business user would receive
    with pg.expect_download(timeout=15000) as dl:
        pg.click("button:has-text('Download template')")
    path = "/tmp/dl_template.xlsx"
    dl.value.save_as(path)          # downloads have no suffix; openpyxl needs one
    check("4. Excel template downloads", os.path.exists(path))
    from openpyxl import load_workbook
    wb = load_workbook(path)
    check("5. Template header is the screen field, pre-filled with the sample",
          [c.value for c in wb["Data"][1]] == ["Table_Name"]
          and wb["Data"].cell(row=2, column=1).value == "T000",
          [[c.value for c in r] for r in wb["Data"].iter_rows(max_row=2)])

    # upload the filled sheet and run
    pg.set_input_files("input[type=file]", DATA_FILE)
    pg.wait_for_timeout(200)
    pg.click("button:has-text('Run with data')")
    pg.wait_for_selector("text=/Evidence document/", timeout=90000)
    body = pg.inner_text("body")
    check("6. Rows reported individually as they completed",
          body.count("Row ") >= 3, body[-700:])
    check("7. All three rows passed", body.count("✓ Row") == 3, body[-700:])
    check("8. Evidence document offered when the run finished",
          "Evidence document" in body and "Word" in body)

    href = pg.get_attribute("a:has-text('Word')", "href")
    check("9. Word document link points at the run's report", "/report/" in (href or ""),
          href)
    check("10. No client-side JS errors", not errs, errs[:2])
    b.close()

print("D-049d UI DATA RUN: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
raise SystemExit(0 if ok else 1)
