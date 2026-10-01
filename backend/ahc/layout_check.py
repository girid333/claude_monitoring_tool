"""Row-layout signature of an AHC SpreadsheetML workbook (D-051).

Used by the tests to compare a generated workbook with SAP's own export
without depending on cell values: per sheet, the style of the title cell and
the 1-based row number of the first table header (style s69).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from health.workbook import _ILLEGAL_RAW, _ILLEGAL_REF

SS = "urn:schemas-microsoft-com:office:spreadsheet"


def _a(el, name):
    return el.get(f"{{{SS}}}{name}")


def sheet_signature(data: bytes) -> dict[str, dict]:
    text = data.decode("utf-8-sig", errors="replace")
    root = ET.fromstring(_ILLEGAL_RAW.sub("", _ILLEGAL_REF.sub("", text)))
    out: dict[str, dict] = {}
    for ws in root.findall(f"{{{SS}}}Worksheet"):
        table = ws.find(f"{{{SS}}}Table")
        rowno, header, title = 0, None, None
        for row in (table if table is not None else []):
            if row.tag != f"{{{SS}}}Row":
                continue
            idx = _a(row, "Index")
            rowno = int(idx) if idx else rowno + 1
            cells = row.findall(f"{{{SS}}}Cell")
            if title is None and cells:
                title = _a(cells[0], "StyleID")
            if header is None and cells and _a(cells[0], "StyleID") == "s69":
                header = rowno
            span = _a(row, "Span")
            if span:
                rowno += int(span)
        out[_a(ws, "Name")] = {"title_style": title, "header_row": header}
    return out
