"""D-049c E2E: the real loop — record a transaction, then replay it once
per data row against a live (fake) SAP, in a real browser.

This is the test that matters for Giri's requirement, because it exercises
the things that only break when you actually loop:

  - row 2 must NOT inherit row 1's substituted values;
  - a BLOCKED VENDOR must fail the row even though every step technically
    succeeded (SAP reports business errors on a working screen);
  - the modal that failure raises must be cleared, or row 3 types into a
    blocked screen and fails for the wrong reason — the silent corruption
    per-row recovery exists to prevent;
  - each iteration's screenshots must be separable, or the evidence
    document cannot prove which input produced which result;
  - the run must COMPLETE all rows despite the failure in the middle.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from cryptography.fernet import Fernet

os.environ["LD_LIBRARY_PATH"] = "/tmp/stublib"
os.environ["REGISTRY_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
work = tempfile.mkdtemp()
os.chdir(work)
os.environ["SYSTEMS_PATH"] = str(Path(work) / "systems.json")
os.environ["RUNS_DB_PATH"] = str(Path(work) / "runs.db")
os.environ["FLOWS_DIR"] = str(Path(work) / "flows")
sys.path.insert(0, BACKEND)

from engine.registry import Registry, SystemEntry, SapCredential  # noqa: E402
from engine import crypto  # noqa: E402

PORT = 8094
Registry(systems=[SystemEntry(
    sid="F4H", client="100", hostname="127.0.0.1",
    webgui_url=f"http://127.0.0.1:{PORT}/webgui",
    sap_credential=SapCredential(username="MON", password_enc=crypto.encrypt("p")),
)]).save(os.environ["SYSTEMS_PATH"])

from engine import datarun  # noqa: E402
from engine.datarun_exec import execute_data_run  # noqa: E402
from data_run_report import DataRunReportGenerator  # noqa: E402

ok = True


def check(n, c, d=""):
    global ok
    ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))


srv = subprocess.Popen([sys.executable, f"{BACKEND}/e2e/fake_sap_transaction.py"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.2)


async def main():
    from playwright.async_api import async_playwright
    from engine.login.basic import BasicLoginPlugin
    from engine.login.base import SystemTarget
    from engine.registry import RegistryCredentialProvider
    from engine.probe import probe
    from engine import recorder_service as rs

    # ---- 1. RECORD the golden transaction, exactly as a user would ----
    s = await rs.start_recording("F4H", registry_path=os.environ["SYSTEMS_PATH"])
    await asyncio.sleep(0.6)
    for field, value in (("#vendor", "100000"), ("#material", "RAW-001"), ("#qty", "10")):
        box = await s.page.locator(field).bounding_box()
        await rs.dispatch_input(s.recording_id,
                                 {"kind": "click", "x": box["x"] + 5, "y": box["y"] + 5})
        await rs.dispatch_input(s.recording_id, {"kind": "text", "text": value})
    btn = await s.page.locator("#btn_save").bounding_box()
    await rs.dispatch_input(s.recording_id,
                             {"kind": "click", "x": btn["x"] + 5, "y": btn["y"] + 5})
    await asyncio.sleep(0.5)
    rs.add_capture_step(s.recording_id, "result")
    saved = await rs.stop_recording(s.recording_id, flow_id="po_create",
                                     flows_dir=os.environ["FLOWS_DIR"])
    flow_dict = json.loads(Path(saved["path"]).read_text())
    check("1. Golden transaction recorded", len(flow_dict["steps"]) >= 4,
          [st["act"] for st in flow_dict["steps"]])

    # ---- 2-3. bindings + template, straight from the recording ----
    from engine.recorder.recorder import describe_steps
    from engine.schema import Flow
    described = describe_steps(Flow(**flow_dict).steps)
    bindings = datarun.suggest_bindings(described)
    check("2. Data-driven fields derived from the recording",
          len(bindings) == 3, [b.column for b in bindings])
    tpl = datarun.write_template(bindings, Path(work) / "template.xlsx")
    check("3. Template generated for the business user", Path(tpl).exists())

    # Three rows: pass, BLOCKED vendor, pass. The middle row is the point.
    cols = [b.column for b in bindings]
    rows = [
        {cols[0]: "100000", cols[1]: "RAW-001", cols[2]: "10"},
        {cols[0]: "200000", cols[1]: "RAW-002", cols[2]: "5"},    # blocked
        {cols[0]: "300000", cols[1]: "RAW-003", cols[2]: "7"},
    ]

    # ---- 4+. run the loop against the live session ----
    reg = Registry.load(os.environ["SYSTEMS_PATH"])
    events = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await b.new_page()
        entry = reg.get("F4H")
        await BasicLoginPlugin(SystemTarget("F4H", entry.webgui_url, "100"),
                                RegistryCredentialProvider(reg)).login(page)
        pr = await probe(page)
        result = await execute_data_run(
            page=page, mode=pr.mode, run_id="dr1", sid="F4H",
            flow_dict=flow_dict, bindings=bindings, rows=rows,
            webgui_url=entry.webgui_url, client="100",
            artifact_root=str(Path(work) / "artifacts"),
            on_progress=events.append)
        await b.close()

    print("      " + json.dumps(result.as_dict(), default=str)[:600])

    check("4. ALL rows ran despite the failure in the middle",
          result.total == 3 and len(result.iterations) == 3,
          f"total={result.total} ran={len(result.iterations)}")
    check("5. Pass/fail tally is correct",
          result.passed == 2 and result.failed == 1,
          f"passed={result.passed} failed={result.failed}")

    it1, it2, it3 = result.iterations
    check("6. Row 1 succeeded and captured the created document number",
          it1.ok and "45000012" in it1.outcome, it1.as_dict())
    check("7. THE KEY CASE: blocked vendor FAILED even though every step "
          "technically succeeded", not it2.ok, it2.as_dict())
    check("8. ...and the business error is recorded verbatim for the tester",
          "blocked for purchasing" in (it2.error or ""), it2.error)
    check("9. RECOVERY WORKED: row 3 succeeded after row 2's modal",
          it3.ok and "45000012" in it3.outcome, it3.as_dict())
    check("10. Row 3 got its OWN document number, not row 1's",
          it1.outcome != it3.outcome, (it1.outcome, it3.outcome))

    # no cross-row contamination
    check("11. Each row recorded its own inputs",
          it1.inputs[cols[0]] == "100000" and it3.inputs[cols[0]] == "300000",
          [i.inputs for i in result.iterations])

    # evidence separable per iteration
    dirs = {Path(s).parent.name for i in result.iterations for s in i.screenshots}
    check("12. Screenshots kept in a separate directory PER iteration",
          len(dirs) == 3 and all(d.startswith("iter_") for d in dirs), dirs)
    check("13. Every iteration produced evidence",
          all(len(i.screenshots) >= 1 for i in result.iterations),
          [len(i.screenshots) for i in result.iterations])

    check("14. Progress events emitted for a live UI",
          len([e for e in events if e["type"] == "iteration_done"]) == 3,
          [e["type"] for e in events])

    # ---- the deliverable ----
    gen = DataRunReportGenerator(result.as_dict(), [b.as_dict() for b in bindings],
                                  sid="F4H", output_dir=str(Path(work) / "reports"),
                                  description="Purchase order creation — vendor variations",
                                  executed_by="GIRID")
    docx = gen.generate_word()
    from docx import Document
    d = Document(docx)
    text = "\n".join(p.text for p in d.paragraphs)
    tbl = "\n".join(c.text for t in d.tables for r in t.rows for c in r.cells)
    check("15. Evidence document generated from the REAL run",
          Path(docx).exists() and "Passed: 2" in text and "Failed: 1" in text, text[:300])
    check("16. Document shows the real document numbers and the real error",
          "45000012" in tbl and "blocked for purchasing" in (tbl + text), tbl[:400])
    check("17. Screenshots from the real run embedded",
          len(d.inline_shapes) >= 3, len(d.inline_shapes))
    print(f"      report: {docx}")


try:
    asyncio.run(main())
finally:
    srv.terminate()

print("D-049c LIVE DATA RUN: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
