"""D-049 E2E: record once → template → N data rows → evidence document.

Giri's actual requirement: record a sample purchase order creation, hand
a template of vendor details to the business, replay once per row, and
produce a document for AUDITORS AND TESTERS evidencing every execution.

The properties under test are the ones that requirement implies:
  - the template carries the transaction's own field names and the
    recorded sample values, so a functional user can fill it in unaided;
  - each row produces its own overrides, and blanks never silently send an
    empty value into a business transaction;
  - a failing row is recorded and the run CONTINUES (row 5 must not cost
    rows 6-20);
  - the document ties each iteration's inputs to that iteration's
    screenshots — a pile of screenshots proves nothing about which input
    produced which result.
"""
import os
import sys
import tempfile
from pathlib import Path

BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
work = tempfile.mkdtemp()
os.chdir(work)
sys.path.insert(0, BACKEND)

from engine import datarun  # noqa: E402
from data_run_report import DataRunReportGenerator  # noqa: E402

ok = True


def check(n, c, d=""):
    global ok
    ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))


# A recorded PO-creation flow, in the shape describe_steps() produces.
recorded_steps = [
    {"index": 0, "act": "start_tcode", "target": "Command field", "value": "ME21N"},
    {"index": 1, "act": "set", "target": "Vendor", "value": "100000"},
    {"index": 2, "act": "set", "target": "Purchasing Org", "value": "1000"},
    {"index": 3, "act": "set", "target": "Material", "value": "RAW-001"},
    {"index": 4, "act": "set", "target": "Quantity", "value": "10"},
    {"index": 5, "act": "set", "target": "Delivery Date", "value": "01.09.2026"},
    {"index": 6, "act": "set", "target": "Confirm", "value": True},   # checkbox
    {"index": 7, "act": "press", "target": "Save", "value": None},
    {"index": 8, "act": "capture", "target": "—", "value": None},
]

# ---- 1-4: binding suggestion ----
bindings = datarun.suggest_bindings(recorded_steps)
cols = [b.column for b in bindings]
check("1. Only editable data fields proposed (not tcode/press/capture)",
      len(bindings) == 5, cols)
check("2. Column names read like the transaction, not like step numbers",
      cols == ["Vendor", "Purchasing_Org", "Material", "Quantity", "Delivery_Date"], cols)
check("3. Checkbox step excluded — it's part of the path, not the data",
      all(b.step_index != 6 for b in bindings), cols)
check("4. Recorded sample values retained for the template",
      bindings[0].recorded_value == "100000"
      and bindings[4].recorded_value == "01.09.2026",
      [b.recorded_value for b in bindings])

dupes = datarun.suggest_bindings([
    {"index": 1, "act": "set", "target": "Date", "value": "a"},
    {"index": 2, "act": "set", "target": "Date", "value": "b"},
    {"index": 3, "act": "set", "target": "", "value": "c"},
])
check("5. Duplicate and blank labels still yield unique usable columns",
      len({d.column for d in dupes}) == 3 and "" not in {d.column for d in dupes},
      [d.column for d in dupes])

# ---- 6-8: the template a functional user receives ----
tpl = datarun.write_template(bindings, Path(work) / "po_template.xlsx")
check("6. Excel template generated", Path(tpl).exists())
from openpyxl import load_workbook  # noqa: E402
wb = load_workbook(tpl)
ws = wb["Data"]
header = [c.value for c in ws[1]]
check("7. Header row is the transaction's own field names", header == cols, header)
check("8. Row 1 pre-filled with the recorded sample, showing expected formats",
      [c.value for c in ws[2]][:2] == ["100000", "1000"], [c.value for c in ws[2]])
check("9. A Notes sheet explains each column", "Notes" in wb.sheetnames
      and wb["Notes"].cell(row=2, column=1).value == "Vendor")

# ---- 10-13: filled dataset -> per-row overrides ----
ws.cell(row=2, column=1, value="100000")
ws.cell(row=3, column=1, value="200000")
for col in range(2, 6):
    ws.cell(row=3, column=col, value=ws.cell(row=2, column=col).value)
ws.cell(row=4, column=1, value="300000")      # only vendor given
wb.save(tpl)

rows = datarun.read_dataset(tpl)
check("10. Filled template read back as rows", len(rows) == 3, rows)

ov, notes = datarun.overrides_for_row(bindings, rows[0])
check("11. A full row maps to overrides keyed by recorded step index",
      ov == {"1": "100000", "2": "1000", "3": "RAW-001", "4": "10", "5": "01.09.2026"},
      ov)
ov3, notes3 = datarun.overrides_for_row(bindings, rows[2])
check("12. Blank cells keep the RECORDED value instead of sending empty",
      "1" in ov3 and len(ov3) == 1, ov3)
check("13. ...and that substitution is reported, never silent",
      len(notes3) == 4 and "empty" in notes3[0], notes3)

missing_col, notes_mc = datarun.overrides_for_row(bindings, {"Vendor": "900000"})
check("14. Missing columns reported rather than assumed",
      missing_col == {"1": "900000"} and any("missing" in n for n in notes_mc), notes_mc)

# ---- 15-17: capturing the SAP outcome for the summary table ----
# Real confirmation formats from a spread of modules — the point being
# that this must work for ANY functional T-code, not just PO-shaped ones.
messages = [
    ("ME21N", "Standard PO created under the number 4500001234", "4500001234"),
    ("VA01", "Standard Order 12345 has been saved", "12345"),
    ("FB01", "Document 1900004567 was posted in company code 1000", "1900004567"),
    ("MM01", "Material RAW-001 created", "RAW-001"),          # ALPHANUMERIC
    ("XD01", "Customer 0000012345 created", "0000012345"),
    ("MIGO", "Material document 5000001234 posted", "5000001234"),
    ("IE01", "Equipment EQ-100-A created", "EQ-100-A"),        # letters + dashes
    ("CS01", "BOM 00012345 was created", "00012345"),
    ("Z_CUST", "Z-Object ABC/2026/0042 generated successfully", "ABC/2026/0042"),
]
missed = [(t, m) for t, m, key in messages if key not in datarun.extract_outcome(m)]
check("15. Confirmation captured across many transaction types, incl. "
      "ALPHANUMERIC keys", not missed, missed)
check("16. A success message with no document key is still recorded",
      "saved" in datarun.extract_outcome("Data was saved").lower(),
      datarun.extract_outcome("Data was saved"))
check("17. No false positive on an error message",
      datarun.extract_outcome("Please enter a vendor") == "",
      datarun.extract_outcome("Please enter a vendor"))
check("18. A flow can override the pattern entirely (custom Z / non-English)",
      datarun.extract_outcome("Beleg 4711 wurde gebucht",
                               pattern=r"Beleg (\d+) wurde gebucht") == "4711",
      datarun.extract_outcome("Beleg 4711 wurde gebucht",
                               pattern=r"Beleg (\d+) wurde gebucht"))
check("19. A malformed custom pattern fails safe, never breaks the run",
      datarun.extract_outcome("anything", pattern="([unclosed") == "")

# Business errors on a technically-successful screen must not read as PASS.
errs = ["Vendor 200000 is blocked for purchasing",
        "No authorization for company code 1000",
        "Material RAW-999 does not exist",
        "Please enter a delivery date"]
check("20. Business-error messages recognised as failures",
      all(datarun.looks_like_error(e) for e in errs),
      [e for e in errs if not datarun.looks_like_error(e)])
check("21. ...and a success message is not mistaken for one",
      not datarun.looks_like_error("Standard PO created under the number 4500001234"))

# Toggles are excluded by DEFAULT but bindable when a flag is the data.
with_toggles = datarun.suggest_bindings(recorded_steps, include_toggles=True)
check("22. Checkbox can be bound when a flag IS the varying data",
      any(b.step_index == 6 for b in with_toggles)
      and len(with_toggles) == len(bindings) + 1,
      [b.column for b in with_toggles])

# ---- 23-33: the auditor/tester evidence document ----
from PIL import Image as PILImage  # noqa: E402
shots = []
for i in range(1, 4):
    p = Path(work) / f"iter{i}.png"
    PILImage.new("RGB", (900, 480), (235, 240, 248)).save(p)
    shots.append(str(p))

result = {
    "flow_id": "po_create", "sid": "F4H", "total": 3, "passed": 2, "failed": 1,
    "iterations": [
        {"index": 1, "inputs": {"Vendor": "100000", "Material": "RAW-001", "Quantity": "10"},
         "ok": True, "outcome": "Standard PO created under the number 4500001234",
         "screenshots": [shots[0]], "error": "", "notes": []},
        {"index": 2, "inputs": {"Vendor": "200000", "Material": "RAW-001", "Quantity": "10"},
         "ok": False, "outcome": "",
         "error": "Vendor 200000 is blocked for purchasing",
         "screenshots": [shots[1]], "notes": ["recovered to ME21N before next row"]},
        {"index": 3, "inputs": {"Vendor": "300000", "Material": "RAW-001", "Quantity": "10"},
         "ok": True, "outcome": "Standard PO created under the number 4500001236",
         "screenshots": [shots[2]], "error": "", "notes": []},
    ],
}
gen = DataRunReportGenerator(result, [b.as_dict() for b in bindings], sid="F4H",
                              output_dir=str(Path(work) / "reports"),
                              description="Purchase order creation — vendor variations",
                              executed_by="GIRID")
docx_path = gen.generate_word()
check("23. Word evidence document generated", Path(docx_path).exists()
      and Path(docx_path).stat().st_size > 20000, docx_path)

from docx import Document  # noqa: E402
d = Document(docx_path)
text = "\n".join(p.text for p in d.paragraphs)
tables_text = "\n".join(c.text for t in d.tables for r in t.rows for c in r.cells)
check("24. Header states system, executor and pass/fail counts",
      "F4H" in text and "GIRID" in text and "Passed: 2" in text and "Failed: 1" in text,
      text[:400])
check("25. Overall verdict reflects the failure",
      "FAILURES PRESENT" in text, text[:400])
check("26. Document states WHICH fields were data-driven",
      "Vendor" in tables_text and "Purchasing_Org" in tables_text)
check("27. Summary table carries per-row status and the created document number",
      "PASS" in tables_text and "FAIL" in tables_text
      and "4500001234" in tables_text, tables_text[:300])
check("28. Each iteration's own inputs appear with it",
      "200000" in tables_text and "300000" in tables_text)
check("29. The failure reason is recorded verbatim for the tester",
      "blocked for purchasing" in text, text[-500:])
check("30. Screenshots embedded, one set per iteration",
      len(d.inline_shapes) == 3, len(d.inline_shapes))

pdf_path = gen.generate_pdf()
pdf_bytes = Path(pdf_path).read_bytes() if Path(pdf_path).exists() else b""
# Size is a poor proxy here: flat-colour test PNGs compress to almost
# nothing. Assert on embedded image XObjects and page count instead.
check("31. PDF evidence document generated with the screenshots embedded",
      b"/Image" in pdf_bytes and pdf_bytes.count(b"/Type /Page") >= 3,
      f"{len(pdf_bytes)} bytes, images={b'/Image' in pdf_bytes}")

# a corrupt screenshot must not destroy the whole PDF (D-035 lesson)
bad = Path(work) / "broken.png"
bad.write_bytes(b"not a png")
result2 = {**result, "iterations": [{**result["iterations"][0],
                                      "screenshots": [str(bad)]}]}
gen2 = DataRunReportGenerator(result2, [b.as_dict() for b in bindings], sid="F4H",
                               output_dir=str(Path(work) / "reports"))
check("32. A corrupt screenshot does not abort the PDF",
      Path(gen2.generate_pdf()).exists())

# an empty run still produces a legible document rather than crashing
empty = {"flow_id": "po_create", "sid": "F4H", "total": 0, "passed": 0,
         "failed": 0, "iterations": []}
gen3 = DataRunReportGenerator(empty, [b.as_dict() for b in bindings], sid="F4H",
                               output_dir=str(Path(work) / "reports"))
p3 = gen3.generate_word()
check("33. A run with no data rows still yields a readable document",
      Path(p3).exists() and "NO DATA" in "\n".join(
          x.text for x in Document(p3).paragraphs))

print("D-049 DATA RUN: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
