"""Derive ahc/layout.json from an SAP Automated Health Check workbook.

The direct pull (D-051) must produce a workbook with EXACTLY the sample's
structure. This tool records that structure — tab order, column widths,
styles, Monitors rows and every table's column headers — without copying
any customer data (no values, hostnames or SIDs). Re-run it against a newer
sample if SAP's format changes:

    python -m ahc.tools.extract_layout /path/to/Automated_Health_Check_Report.XLS
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE.parent))

from health.analyze import cell, find_monitors  # noqa: E402
from health.workbook import read_workbook  # noqa: E402

# Selection-period text that names a customer object is replaced by a neutral one.
NEUTRAL_PERIOD = {"RFCs in table ZCAS_AHC_FILTER": "All RFC destinations"}


def _row_meta(ws: str) -> dict:
    """Title-row details that differ per tab: subtitle merge width, the blank
    row shape after the titles (SM51 has a single blank row instead of the
    usual two-row span) and the first header row's height."""
    rows = re.findall(r"<Row\b[^>]*?(?:/>|>.*?</Row>)", ws, re.S)
    meta = {"sub_merge": None, "span_blank": True, "header_height": None}
    if len(rows) < 4 or 'ss:StyleID="s73"' not in rows[0]:
        return meta
    m = re.search(r'ss:MergeAcross="(\d+)"', rows[1])
    meta["sub_merge"] = int(m.group(1)) if m else 0
    meta["span_blank"] = 'ss:Span="1"' in rows[2]
    h = re.match(r"<Row\b[^>]*ss:Height=\"([\d.]+)\"", rows[3])
    meta["header_height"] = h.group(1) if h else None
    return meta


def main(path: str) -> None:
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    styles = text[text.index("<Styles>"): text.index("</Styles>") + len("</Styles>")]
    sheets = read_workbook(raw, Path(path).name)
    mon, hi = find_monitors(sheets)
    hdr = [x.lower() for x in mon.rows[hi]]
    ix = {k: hdr.index(k) for k in ("monitoring task", "transaction", "description", "selection period")}
    monitors = []
    for r in mon.rows[hi + 1:]:
        if not (cell(r, ix["transaction"]) or cell(r, ix["monitoring task"])):
            continue
        period = cell(r, ix["selection period"])
        monitors.append({"task": cell(r, ix["monitoring task"]), "tcode": cell(r, ix["transaction"]),
                         "description": cell(r, ix["description"]),
                         "period": NEUTRAL_PERIOD.get(period, period)})
    out = {"styles": styles, "monitors": monitors, "sheets": []}
    for name, sh in sheets.items():
        i = text.index(f'ss:Name="{name}"')
        ws = text[i: text.index("</Worksheet>", i)]
        cols = "".join(re.findall(r"<Column [^>]*/>", ws))
        out["sheets"].append({
            "name": name, "columns_xml": cols,
            "tables": [t.columns for t in sh.tables],
            **_row_meta(ws),
        })
    dest = HERE / "layout.json"
    dest.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {dest}: {len(out['sheets'])} sheets, {len(monitors)} monitors rows")


if __name__ == "__main__":
    main(sys.argv[1])
